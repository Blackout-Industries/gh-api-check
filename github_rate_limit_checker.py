#!/usr/bin/env python3
"""
GitHub API Rate Limit Checker - Multi-App Support
Monitors GitHub API rate limits for multiple GitHub App installations from a single process.
Useful for diagnosing KEDA or other automation issues related to API throttling.

Usage:
  # Single app using environment variables (backward compatible):
  export GITHUB_TOKEN=ghp_xxxxx
  python github_rate_limit_checker.py

  # Single GitHub App:
  export GITHUB_APP_ID=123456
  export GITHUB_APP_INSTALLATION_ID=987654
  export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/private-key.pem
  python github_rate_limit_checker.py

  # Multiple apps from config file:
  python github_rate_limit_checker.py --config-file /app/config/apps.json --prometheus-port 9090

  In Prometheus mode a background thread collects every --interval seconds and the
  HTTP endpoints /metrics, /healthz and /ready serve the last snapshot without
  calling GitHub.

  # Multiple apps from directory of credential files:
  python github_rate_limit_checker.py --apps-dir /app/secrets/apps --prometheus-port 9090

  # Continuous monitoring mode:
  python github_rate_limit_checker.py --watch --interval 60

Config file format (apps.json):
{
  "apps": [
    {
      "name": "devops-runner",
      "app_id": "1187645",
      "installation_id": "63220290",
      "private_key_path": "/app/secrets/app1.pem"
    },
    {
      "name": "ci-automation",
      "app_id": "1234567",
      "installation_id": "7654321",
      "private_key_path": "/app/secrets/app2.pem"
    }
  ]
}
"""

import argparse
import glob
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

try:
    import jwt  # PyJWT for GitHub App authentication
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    print("Warning: PyJWT not installed. GitHub App authentication unavailable.", file=sys.stderr)

__version__ = os.getenv('GH_API_CHECK_VERSION', 'dev')
USER_AGENT = f'gh-api-check/{__version__}'
LOGGER = logging.getLogger('gh-api-check')

TOKEN_REFRESH_BUFFER_SECONDS = 300
INSTALLATION_TOKEN_DEFAULT_TTL_SECONDS = 3600
FIRST_COLLECTION_DEADLINE_SECONDS = 5
READY_MISSED_CYCLES = 3


def configure_logging() -> None:
    """Send operational messages to stderr; CLI output stays on stdout."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format='%(asctime)s %(levelname)s %(name)s %(message)s'
    )


def build_session() -> requests.Session:
    """Build a session that identifies itself and retries transient GitHub 5xx."""
    session = requests.Session()
    session.headers['Accept'] = 'application/vnd.github.v3+json'
    session.headers['User-Agent'] = USER_AGENT
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def parse_expires_at(value: Optional[str], fallback: int) -> int:
    """Convert the ISO-8601 Z expiry GitHub returns into epoch seconds."""
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


class AppCredentials:
    """Container for GitHub App credentials."""

    def __init__(self, name: str, app_id: str, installation_id: str, private_key_path: str,
                 token: Optional[str] = None):
        self.name = name
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = private_key_path
        self.token = token  # For PAT authentication
        self.cached_installation_token = None
        self.token_expires_at = 0
        self.token_lock = threading.Lock()


class GitHubRateLimitChecker:
    """Monitor GitHub API rate limits for single or multiple GitHub Apps."""

    def __init__(self, apps: List[AppCredentials]):
        """
        Initialize checker with one or more app credentials.

        Args:
            apps: List of AppCredentials objects
        """
        self.apps = apps
        self.sessions = {}  # app_name -> requests.Session
        self._durations_lock = threading.Lock()
        self._last_durations: Dict[str, float] = {}
        self._token_session = build_session()
        self._setup_sessions()

    def _setup_sessions(self):
        """Initialize HTTP sessions for each app."""
        for app in self.apps:
            session = build_session()

            if app.token:
                # PAT authentication
                session.headers['Authorization'] = f'token {app.token}'
            elif app.app_id and app.private_key_path:
                # GitHub App authentication will be setup on first request
                pass

            self.sessions[app.name] = session

    def get_last_durations(self) -> Dict[str, float]:
        """Return how long the most recent check took per app, in seconds."""
        with self._durations_lock:
            return dict(self._last_durations)

    def _get_installation_token(self, app: AppCredentials) -> Optional[str]:
        """
        Get or refresh installation access token for a GitHub App.

        Args:
            app: AppCredentials object

        Returns:
            Installation token or None if failed
        """
        if not JWT_AVAILABLE:
            raise RuntimeError("PyJWT required for GitHub App auth. Install: pip install PyJWT cryptography")

        # The REST and GraphQL checks run concurrently, so only one of them may mint
        with app.token_lock:
            now = int(time.time())
            if app.cached_installation_token and app.token_expires_at > (now + TOKEN_REFRESH_BUFFER_SECONDS):
                return app.cached_installation_token

            # Generate new JWT
            try:
                with open(app.private_key_path, 'r') as f:
                    private_key = f.read()
            except OSError as e:
                LOGGER.error("Error reading private key for %s: %s", app.name, e)
                return None

            payload = {
                'iat': now,
                'exp': now + 600,  # 10 minutes
                'iss': app.app_id
            }

            jwt_token = jwt.encode(payload, private_key, algorithm='RS256')

            # Get installation access token
            if app.installation_id:
                headers = {
                    'Authorization': f'Bearer {jwt_token}',
                    'Accept': 'application/vnd.github.v3+json'
                }
                try:
                    response = self._token_session.post(
                        f'https://api.github.com/app/installations/{app.installation_id}/access_tokens',
                        headers=headers,
                        timeout=10
                    )
                    response.raise_for_status()
                    token_data = response.json()
                    app.cached_installation_token = token_data['token']
                    app.token_expires_at = parse_expires_at(
                        token_data.get('expires_at'),
                        now + INSTALLATION_TOKEN_DEFAULT_TTL_SECONDS
                    )
                    return app.cached_installation_token
                except requests.exceptions.RequestException as e:
                    LOGGER.warning("Failed to get installation token for %s: %s", app.name, e)
                    return None

            return jwt_token

    def _ensure_auth(self, app: AppCredentials):
        """Ensure the session has valid authentication."""
        session = self.sessions[app.name]

        # Skip if using PAT
        if app.token:
            return

        # Get/refresh installation token for GitHub App
        if app.app_id and app.private_key_path:
            token = self._get_installation_token(app)
            if token:
                session.headers['Authorization'] = f'token {token}'

    def check_rate_limit(self, app: AppCredentials) -> Dict[str, Any]:
        """
        Check current GitHub API rate limits for a specific app.

        Args:
            app: AppCredentials object

        Returns:
            Dict with rate limit information and app metadata
        """
        self._ensure_auth(app)
        session = self.sessions[app.name]

        try:
            response = session.get('https://api.github.com/rate_limit', timeout=10)
            response.raise_for_status()
            data = response.json()
            # Add app metadata
            data['app_metadata'] = {
                'name': app.name,
                'app_id': app.app_id,
                'installation_id': app.installation_id
            }
            return data
        except requests.exceptions.RequestException as e:
            return {
                'error': str(e),
                'app_metadata': {
                    'name': app.name,
                    'app_id': app.app_id,
                    'installation_id': app.installation_id
                }
            }

    def check_graphql_rate_limit(self, app: AppCredentials) -> Dict[str, Any]:
        """
        Check GraphQL API rate limits for a specific app.

        Args:
            app: AppCredentials object

        Returns:
            Dict with GraphQL rate limit information and app metadata
        """
        self._ensure_auth(app)
        session = self.sessions[app.name]

        query = """
        query {
          rateLimit {
            limit
            cost
            remaining
            resetAt
            used
            nodeCount
          }
        }
        """

        try:
            response = session.post(
                'https://api.github.com/graphql',
                json={'query': query},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if 'errors' in data:
                return {
                    'error': data['errors'],
                    'app_metadata': {
                        'name': app.name,
                        'app_id': app.app_id,
                        'installation_id': app.installation_id
                    }
                }

            result = data.get('data', {}).get('rateLimit', {})
            result['app_metadata'] = {
                'name': app.name,
                'app_id': app.app_id,
                'installation_id': app.installation_id
            }
            return result
        except requests.exceptions.RequestException as e:
            return {
                'error': str(e),
                'app_metadata': {
                    'name': app.name,
                    'app_id': app.app_id,
                    'installation_id': app.installation_id
                }
            }

    def check_all_apps(self) -> Dict[str, Dict[str, Any]]:
        """
        Check rate limits for all configured apps concurrently.

        Returns:
            Dict mapping app names to their rate limit data
        """
        results = {}

        with ThreadPoolExecutor(max_workers=min(len(self.apps), 10)) as executor:
            # Submit all tasks
            future_to_app = {
                executor.submit(self._check_app_limits, app): app
                for app in self.apps
            }

            # Collect results
            for future in as_completed(future_to_app):
                app = future_to_app[future]
                try:
                    results[app.name] = future.result()
                except Exception as e:
                    LOGGER.error("Error checking %s: %s", app.name, e)
                    results[app.name] = {
                        'error': str(e),
                        'app_metadata': {
                            'name': app.name,
                            'app_id': app.app_id,
                            'installation_id': app.installation_id
                        }
                    }

        return results

    def _check_app_limits(self, app: AppCredentials) -> Dict[str, Any]:
        """Helper to check both REST and GraphQL limits for an app."""
        started = time.monotonic()
        try:
            rest_data = self.check_rate_limit(app)
            graphql_data = self.check_graphql_rate_limit(app)
        finally:
            # timings live beside the payload so the CLI JSON output keeps its shape
            with self._durations_lock:
                self._last_durations[app.name] = time.monotonic() - started

        return {
            'rest_api': rest_data,
            'graphql': graphql_data
        }

    def format_reset_time(self, reset_timestamp: int) -> str:
        """Convert Unix timestamp to human-readable time."""
        reset_time = datetime.fromtimestamp(reset_timestamp, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = reset_time - now

        minutes = int(delta.total_seconds() / 60)
        seconds = int(delta.total_seconds() % 60)

        return f"{reset_time.strftime('%Y-%m-%d %H:%M:%S UTC')} (in {minutes}m {seconds}s)"

    def print_rate_limit_status(self, app_name: str, data: Dict[str, Any]):
        """Print formatted rate limit status for an app."""
        if 'error' in data:
            print(f"❌ Error checking rate limits for {app_name}: {data['error']}", file=sys.stderr)
            return

        metadata = data.get('app_metadata', {})
        print(f"\n{'=' * 80}")
        print(f"App: {app_name}")
        print(f"App ID: {metadata.get('app_id', 'N/A')}")
        print(f"Installation ID: {metadata.get('installation_id', 'N/A')}")
        print(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"{'=' * 80}\n")

        resources = data.get('resources', {})

        for resource_name, limits in resources.items():
            limit = limits.get('limit', 0)
            remaining = limits.get('remaining', 0)
            used = limits.get('used', 0)
            reset = limits.get('reset', 0)

            percentage_used = (used / limit * 100) if limit > 0 else 0
            percentage_remaining = (remaining / limit * 100) if limit > 0 else 0

            # Color coding based on remaining percentage
            if percentage_remaining > 50:
                status = "✅ HEALTHY"
            elif percentage_remaining > 20:
                status = "⚠️  WARNING"
            else:
                status = "🚨 CRITICAL"

            print(f"{resource_name.upper():20} {status}")
            print(f"  Limit:     {limit:>6}")
            print(f"  Used:      {used:>6} ({percentage_used:>5.1f}%)")
            print(f"  Remaining: {remaining:>6} ({percentage_remaining:>5.1f}%)")
            print(f"  Resets at: {self.format_reset_time(reset)}")
            print()

    def export_prometheus_metrics(self, port: int = 9090, interval: int = 60):
        """
        Serve cached rate limit metrics for all apps over HTTP.

        A background thread refreshes the snapshot every `interval` seconds, so
        scrapes and probes never wait on GitHub.

        Args:
            port: Port to expose metrics on
            interval: Seconds between background collections
        """
        interval = max(int(interval), 1)
        store = MetricsStore(self.apps)
        collector = CollectorThread(self, store, interval)
        server = create_metrics_server(store, port, interval)
        stop_event = threading.Event()

        server_thread = threading.Thread(target=server.serve_forever, name='metrics-http', daemon=True)
        server_thread.start()
        collector.start()

        LOGGER.info("Metrics server listening on http://0.0.0.0:%d (interval %ds)", port, interval)
        LOGGER.info("Endpoints: /metrics, /healthz, /ready")
        LOGGER.info("Monitoring %d GitHub App(s): %s", len(self.apps),
                    ', '.join(app.name for app in self.apps))

        install_signal_handlers(stop_event)

        # bounded wait so an unreachable GitHub cannot hold up the first scrape
        collector.first_cycle.wait(min(FIRST_COLLECTION_DEADLINE_SECONDS, interval))

        while not stop_event.wait(1.0):
            pass

        collector.stop()
        server.shutdown()
        server.server_close()
        LOGGER.info("Metrics server stopped")
        sys.exit(0)


@dataclass
class AppSnapshot:
    """Last known state of one app, as served to Prometheus."""

    app_name: str
    app_id: str
    installation_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    collected_at: float = 0.0
    duration_seconds: float = 0.0
    success: bool = False
    last_success: float = 0.0
    errors: int = 0


class MetricsStore:
    """Thread-safe holder for the most recent collection result."""

    def __init__(self, apps: List[AppCredentials]):
        self._lock = threading.Lock()
        self._apps: Dict[str, AppSnapshot] = {
            app.name: AppSnapshot(
                app_name=app.name,
                app_id=app.app_id,
                installation_id=app.installation_id
            )
            for app in apps
        }
        self._last_success = 0.0

    def record(self, results: Dict[str, Dict[str, Any]], durations: Dict[str, float],
               collected_at: Optional[float] = None) -> None:
        """Replace the snapshot with the result of one collection cycle."""
        collected_at = time.time() if collected_at is None else collected_at

        with self._lock:
            for app_name, app_data in results.items():
                snapshot = self._apps.get(app_name)
                if snapshot is None:
                    snapshot = AppSnapshot(app_name=app_name, app_id='unknown', installation_id='unknown')
                    self._apps[app_name] = snapshot

                rest_data = app_data.get('rest_api', {})
                metadata = rest_data.get('app_metadata', {})
                snapshot.app_id = metadata.get('app_id', snapshot.app_id)
                snapshot.installation_id = metadata.get('installation_id', snapshot.installation_id)
                snapshot.data = app_data
                snapshot.collected_at = collected_at
                snapshot.duration_seconds = durations.get(app_name, snapshot.duration_seconds)
                snapshot.success = bool(rest_data) and 'error' not in rest_data

                if snapshot.success:
                    snapshot.last_success = collected_at
                    self._last_success = max(self._last_success, collected_at)
                else:
                    snapshot.errors += 1

            for app_name, snapshot in self._apps.items():
                if app_name not in results:
                    snapshot.success = False
                    snapshot.collected_at = collected_at
                    snapshot.errors += 1

    def snapshot(self) -> List[AppSnapshot]:
        """Return a detached copy of the per-app state."""
        with self._lock:
            return [replace(app_snapshot) for app_snapshot in self._apps.values()]

    @property
    def last_success(self) -> float:
        """Epoch seconds of the most recent cycle in which any app succeeded."""
        with self._lock:
            return self._last_success


class SnapshotCollector:
    """Render the cached snapshot in the metric shape existing dashboards use."""

    def __init__(self, store: MetricsStore):
        self._store = store

    def collect(self) -> Iterable[Any]:
        rest_labels = ['resource', 'app_name', 'app_id', 'installation_id']
        app_labels = ['app_name', 'app_id', 'installation_id']

        status = GaugeMetricFamily(
            'github_app_status', 'App health status (1=healthy, 0=error)', labels=app_labels)
        limit = GaugeMetricFamily(
            'github_rate_limit_limit', 'REST rate limit maximum', labels=rest_labels)
        remaining = GaugeMetricFamily(
            'github_rate_limit_remaining', 'REST rate limit remaining calls', labels=rest_labels)
        used = GaugeMetricFamily(
            'github_rate_limit_used', 'REST rate limit used calls', labels=rest_labels)
        reset = GaugeMetricFamily(
            'github_rate_limit_reset', 'Unix timestamp when the REST limit resets', labels=rest_labels)
        gql_limit = GaugeMetricFamily(
            'github_graphql_rate_limit_limit', 'GraphQL rate limit maximum', labels=app_labels)
        gql_remaining = GaugeMetricFamily(
            'github_graphql_rate_limit_remaining', 'GraphQL rate limit remaining calls', labels=app_labels)
        gql_used = GaugeMetricFamily(
            'github_graphql_rate_limit_used', 'GraphQL rate limit used calls', labels=app_labels)
        scrape_duration = GaugeMetricFamily(
            'github_exporter_scrape_duration_seconds',
            'Duration of the last background collection per app', labels=['app_name'])
        last_success = GaugeMetricFamily(
            'github_exporter_last_success_timestamp_seconds',
            'Unix timestamp of the last successful collection per app', labels=['app_name'])
        errors = CounterMetricFamily(
            'github_exporter_scrape_errors_total',
            'Failed background collections per app', labels=['app_name'])

        for app in self._store.snapshot():
            app_values = [app.app_name, app.app_id, app.installation_id]
            status.add_metric(app_values, 1.0 if app.success else 0.0)

            rest_data = app.data.get('rest_api', {})
            for resource_name, limits in rest_data.get('resources', {}).items():
                resource_values = [resource_name] + app_values
                limit.add_metric(resource_values, _as_float(limits.get('limit')))
                remaining.add_metric(resource_values, _as_float(limits.get('remaining')))
                used.add_metric(resource_values, _as_float(limits.get('used')))
                reset.add_metric(resource_values, _as_float(limits.get('reset')))

            graphql_data = app.data.get('graphql', {})
            if graphql_data and 'error' not in graphql_data:
                gql_limit.add_metric(app_values, _as_float(graphql_data.get('limit')))
                gql_remaining.add_metric(app_values, _as_float(graphql_data.get('remaining')))
                gql_used.add_metric(app_values, _as_float(graphql_data.get('used')))

            scrape_duration.add_metric([app.app_name], app.duration_seconds)
            last_success.add_metric([app.app_name], app.last_success)
            errors.add_metric([app.app_name], float(app.errors))

        yield status
        yield limit
        yield remaining
        yield used
        yield reset
        yield gql_limit
        yield gql_remaining
        yield gql_used
        yield scrape_duration
        yield last_success
        yield errors


def _as_float(value: Any) -> float:
    """Metric values must be numeric even when GitHub omits a field."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class CollectorThread(threading.Thread):
    """Refresh the snapshot on a timer, independent of incoming requests."""

    def __init__(self, checker: 'GitHubRateLimitChecker', store: MetricsStore, interval: int):
        super().__init__(name='rate-limit-collector', daemon=True)
        self._checker = checker
        self._store = store
        self._interval = interval
        self._stop = threading.Event()
        self.first_cycle = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self.collect_once()
            self.first_cycle.set()
            self._stop.wait(self._interval)

    def collect_once(self) -> None:
        """Run one collection cycle and store whatever came back."""
        started = time.monotonic()
        try:
            results = self._checker.check_all_apps()
        except Exception as e:  # a broken cycle must not kill the collector
            LOGGER.error("Collection cycle failed: %s", e)
            results = {}

        self._store.record(results, self._checker.get_last_durations())
        LOGGER.info("Collected %d app(s) in %.2fs", len(results), time.monotonic() - started)

    def stop(self) -> None:
        self._stop.set()


class MetricsServer(ThreadingHTTPServer):
    """Threaded server so one slow client cannot block the others."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            LOGGER.warning("Client %s disconnected before the response was sent", client_address[0])
            return
        LOGGER.warning("Request from %s failed: %s", client_address[0], error)


def create_metrics_server(store: MetricsStore, port: int, interval: int) -> MetricsServer:
    """Build the HTTP server exposing /metrics, /healthz and /ready."""
    registry = CollectorRegistry()
    registry.register(SnapshotCollector(store))
    ready_max_age = max(int(interval), 1) * READY_MISSED_CYCLES

    class MetricsHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = f'gh-api-check/{__version__}'
        sys_version = ''

        def do_GET(self):
            path = self.path.split('?')[0]

            if path == '/metrics':
                self._respond(200, generate_latest(registry), CONTENT_TYPE_LATEST)
            elif path == '/healthz':
                self._respond(200, b'ok\n')
            elif path == '/ready':
                self._respond(*self._readiness())
            else:
                self._respond(404, b'not found\n')

        def _readiness(self):
            last_success = store.last_success
            if last_success <= 0:
                return 503, b'no successful collection yet\n'

            age = int(time.time() - last_success)
            if age > ready_max_age:
                return 503, f'last successful collection {age}s ago\n'.encode()
            return 200, b'ready\n'

        def _respond(self, status: int, body: bytes, content_type: str = 'text/plain; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.warning("Client %s hung up while the response was written", self.client_address[0])

        def log_message(self, format, *args):
            # Suppress default logging
            pass

    return MetricsServer(('0.0.0.0', port), MetricsHandler)


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Turn SIGTERM/SIGINT into an orderly shutdown instead of a SIGKILL wait."""

    def _handle(signum, _frame):
        LOGGER.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def load_apps_from_env() -> List[AppCredentials]:
    """
    Load single app credentials from environment variables (backward compatible).

    Returns:
        List with single AppCredentials or empty list
    """
    token = os.getenv('GITHUB_TOKEN')
    app_id = os.getenv('GITHUB_APP_ID')
    installation_id = os.getenv('GITHUB_APP_INSTALLATION_ID')
    private_key_path = os.getenv('GITHUB_APP_PRIVATE_KEY_PATH')
    app_name = os.getenv('GITHUB_APP_NAME', 'default')

    if token:
        return [AppCredentials(
            name=app_name,
            app_id='',
            installation_id='',
            private_key_path='',
            token=token
        )]
    elif app_id and private_key_path:
        return [AppCredentials(
            name=app_name,
            app_id=app_id,
            installation_id=installation_id or '',
            private_key_path=private_key_path
        )]

    return []


def load_apps_from_config_file(config_path: str) -> List[AppCredentials]:
    """
    Load multiple app credentials from JSON config file.

    Args:
        config_path: Path to JSON config file

    Returns:
        List of AppCredentials
    """
    with open(config_path, 'r') as f:
        config = json.load(f)

    apps = []
    for app_config in config.get('apps', []):
        apps.append(AppCredentials(
            name=app_config['name'],
            app_id=app_config['app_id'],
            installation_id=app_config['installation_id'],
            private_key_path=app_config['private_key_path']
        ))

    return apps


def load_apps_from_directory(apps_dir: str) -> List[AppCredentials]:
    """
    Load multiple app credentials from directory of JSON files.

    Args:
        apps_dir: Directory containing JSON files with app configs

    Returns:
        List of AppCredentials
    """
    apps = []
    json_files = glob.glob(os.path.join(apps_dir, '*.json'))

    for json_file in json_files:
        with open(json_file, 'r') as f:
            app_config = json.load(f)
            apps.append(AppCredentials(
                name=app_config.get('name', Path(json_file).stem),
                app_id=app_config['app_id'],
                installation_id=app_config['installation_id'],
                private_key_path=app_config['private_key_path']
            ))

    return apps


def main():
    parser = argparse.ArgumentParser(
        description='Monitor GitHub API rate limits for single or multiple GitHub Apps',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument('--token', help='GitHub Personal Access Token (or use GITHUB_TOKEN env var)')
    parser.add_argument('--app-id', help='GitHub App ID (or use GITHUB_APP_ID env var)')
    parser.add_argument('--installation-id', help='GitHub App Installation ID (or use GITHUB_APP_INSTALLATION_ID env var)')
    parser.add_argument('--private-key', help='Path to GitHub App private key (or use GITHUB_APP_PRIVATE_KEY_PATH env var)')
    parser.add_argument('--app-name', help='App name for single-app mode (or use GITHUB_APP_NAME env var, default: "default")')
    parser.add_argument('--config-file', help='Path to JSON config file with multiple apps')
    parser.add_argument('--apps-dir', help='Directory containing JSON files for multiple apps')
    parser.add_argument('--watch', action='store_true', help='Continuously monitor rate limits')
    parser.add_argument('--interval', type=int, default=60,
                        help='Seconds between rate limit collections in watch and Prometheus modes (default: 60)')
    parser.add_argument('--prometheus-port', type=int, help='Export Prometheus metrics on specified port')
    parser.add_argument('--json', action='store_true', help='Output in JSON format')

    args = parser.parse_args()
    configure_logging()

    # Load app credentials
    apps = []

    if args.config_file:
        apps = load_apps_from_config_file(args.config_file)
    elif args.apps_dir:
        apps = load_apps_from_directory(args.apps_dir)
    elif args.token or args.app_id:
        # Single app from CLI args
        app_name = args.app_name or os.getenv('GITHUB_APP_NAME', 'default')
        if args.token:
            apps = [AppCredentials(
                name=app_name,
                app_id='',
                installation_id='',
                private_key_path='',
                token=args.token
            )]
        elif args.app_id and args.private_key:
            apps = [AppCredentials(
                name=app_name,
                app_id=args.app_id,
                installation_id=args.installation_id or '',
                private_key_path=args.private_key
            )]
    else:
        # Try environment variables (backward compatible)
        apps = load_apps_from_env()

    if not apps:
        print("Error: No GitHub credentials provided.", file=sys.stderr)
        print("\nOptions:", file=sys.stderr)
        print("  1. Environment variables:", file=sys.stderr)
        print("     export GITHUB_TOKEN=ghp_xxxxx", file=sys.stderr)
        print("     OR", file=sys.stderr)
        print("     export GITHUB_APP_ID=123456", file=sys.stderr)
        print("     export GITHUB_APP_INSTALLATION_ID=987654", file=sys.stderr)
        print("     export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem", file=sys.stderr)
        print("\n  2. Config file:", file=sys.stderr)
        print("     python github_rate_limit_checker.py --config-file /app/config/apps.json", file=sys.stderr)
        print("\n  3. Apps directory:", file=sys.stderr)
        print("     python github_rate_limit_checker.py --apps-dir /app/secrets/apps", file=sys.stderr)
        sys.exit(1)

    try:
        checker = GitHubRateLimitChecker(apps=apps)
    except Exception as e:
        print(f"Error initializing checker: {e}", file=sys.stderr)
        sys.exit(1)

    # Prometheus export mode
    if args.prometheus_port:
        checker.export_prometheus_metrics(port=args.prometheus_port, interval=args.interval)
        return

    # Watch mode
    if args.watch:
        print(f"🔄 Monitoring {len(apps)} GitHub App(s) every {args.interval} seconds...")
        print("Press Ctrl+C to stop\n")

        try:
            while True:
                all_app_data = checker.check_all_apps()

                if args.json:
                    print(json.dumps({
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'apps': all_app_data
                    }, indent=2))
                else:
                    for app_name, app_data in all_app_data.items():
                        checker.print_rate_limit_status(app_name, app_data['rest_api'])
                        # GraphQL status printing omitted for brevity in multi-app mode

                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\n✅ Monitoring stopped")
            sys.exit(0)

    # Single check mode
    else:
        all_app_data = checker.check_all_apps()

        if args.json:
            print(json.dumps({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'apps': all_app_data
            }, indent=2))
        else:
            for app_name, app_data in all_app_data.items():
                checker.print_rate_limit_status(app_name, app_data['rest_api'])


if __name__ == '__main__':
    main()
