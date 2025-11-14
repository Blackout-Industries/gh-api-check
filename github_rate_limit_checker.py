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
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Any, List
import requests

try:
    import jwt  # PyJWT for GitHub App authentication
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    print("Warning: PyJWT not installed. GitHub App authentication unavailable.", file=sys.stderr)


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
        self._setup_sessions()

    def _setup_sessions(self):
        """Initialize HTTP sessions for each app."""
        for app in self.apps:
            session = requests.Session()
            session.headers['Accept'] = 'application/vnd.github.v3+json'

            if app.token:
                # PAT authentication
                session.headers['Authorization'] = f'token {app.token}'
            elif app.app_id and app.private_key_path:
                # GitHub App authentication will be setup on first request
                pass

            self.sessions[app.name] = session

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

        # Check if cached token is still valid (with 5 min buffer)
        now = int(time.time())
        if app.cached_installation_token and app.token_expires_at > (now + 300):
            return app.cached_installation_token

        # Generate new JWT
        try:
            with open(app.private_key_path, 'r') as f:
                private_key = f.read()
        except Exception as e:
            print(f"Error reading private key for {app.name}: {e}", file=sys.stderr)
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
                response = requests.post(
                    f'https://api.github.com/app/installations/{app.installation_id}/access_tokens',
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                token_data = response.json()
                app.cached_installation_token = token_data['token']
                # Installation tokens expire after 1 hour
                app.token_expires_at = now + 3600
                return app.cached_installation_token
            except requests.exceptions.RequestException as e:
                print(f"Warning: Failed to get installation token for {app.name}: {e}", file=sys.stderr)
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
                    print(f"Error checking {app.name}: {e}", file=sys.stderr)
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
        rest_data = self.check_rate_limit(app)
        graphql_data = self.check_graphql_rate_limit(app)

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

    def export_prometheus_metrics(self, port: int = 9090):
        """
        Export rate limit metrics for all apps in Prometheus format via HTTP endpoint.

        Args:
            port: Port to expose metrics on
        """
        from http.server import HTTPServer, BaseHTTPRequestHandler

        checker = self

        class MetricsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/metrics':
                    self.send_response(404)
                    self.end_headers()
                    return

                all_app_data = checker.check_all_apps()
                metrics = []

                for app_name, app_data in all_app_data.items():
                    rest_data = app_data.get('rest_api', {})
                    graphql_data = app_data.get('graphql', {})
                    metadata = rest_data.get('app_metadata', {})

                    app_id = metadata.get('app_id', 'unknown')
                    installation_id = metadata.get('installation_id', 'unknown')

                    # App status metric (1=healthy, 0=error)
                    status = 0 if 'error' in rest_data else 1
                    metrics.append(
                        f'github_app_status{{app_name="{app_name}",app_id="{app_id}",installation_id="{installation_id}"}} {status}'
                    )

                    # REST API metrics
                    if 'resources' in rest_data:
                        for resource_name, limits in rest_data['resources'].items():
                            base_labels = f'resource="{resource_name}",app_name="{app_name}",app_id="{app_id}",installation_id="{installation_id}"'
                            metrics.append(f'github_rate_limit_limit{{{base_labels}}} {limits.get("limit", 0)}')
                            metrics.append(f'github_rate_limit_remaining{{{base_labels}}} {limits.get("remaining", 0)}')
                            metrics.append(f'github_rate_limit_used{{{base_labels}}} {limits.get("used", 0)}')
                            metrics.append(f'github_rate_limit_reset{{{base_labels}}} {limits.get("reset", 0)}')

                    # GraphQL metrics
                    if 'error' not in graphql_data and graphql_data:
                        base_labels = f'app_name="{app_name}",app_id="{app_id}",installation_id="{installation_id}"'
                        metrics.append(f'github_graphql_rate_limit_limit{{{base_labels}}} {graphql_data.get("limit", 0)}')
                        metrics.append(f'github_graphql_rate_limit_remaining{{{base_labels}}} {graphql_data.get("remaining", 0)}')
                        metrics.append(f'github_graphql_rate_limit_used{{{base_labels}}} {graphql_data.get("used", 0)}')

                response = '\n'.join(metrics) + '\n'

                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(response.encode())

            def log_message(self, format, *args):
                # Suppress default logging
                pass

        server = HTTPServer(('0.0.0.0', port), MetricsHandler)
        print(f"✅ Prometheus metrics server started on http://0.0.0.0:{port}/metrics")
        print(f"📊 Monitoring {len(self.apps)} GitHub App(s):")
        for app in self.apps:
            print(f"   - {app.name} (App ID: {app.app_id}, Installation ID: {app.installation_id})")
        print("\nPress Ctrl+C to stop\n")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n\n✅ Metrics server stopped")
            sys.exit(0)


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

    if token:
        return [AppCredentials(
            name='default',
            app_id='',
            installation_id='',
            private_key_path='',
            token=token
        )]
    elif app_id and private_key_path:
        return [AppCredentials(
            name='default',
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
    parser.add_argument('--config-file', help='Path to JSON config file with multiple apps')
    parser.add_argument('--apps-dir', help='Directory containing JSON files for multiple apps')
    parser.add_argument('--watch', action='store_true', help='Continuously monitor rate limits')
    parser.add_argument('--interval', type=int, default=60, help='Interval in seconds for watch mode (default: 60)')
    parser.add_argument('--prometheus-port', type=int, help='Export Prometheus metrics on specified port')
    parser.add_argument('--json', action='store_true', help='Output in JSON format')

    args = parser.parse_args()

    # Load app credentials
    apps = []

    if args.config_file:
        apps = load_apps_from_config_file(args.config_file)
    elif args.apps_dir:
        apps = load_apps_from_directory(args.apps_dir)
    elif args.token or args.app_id:
        # Single app from CLI args
        if args.token:
            apps = [AppCredentials(
                name='default',
                app_id='',
                installation_id='',
                private_key_path='',
                token=args.token
            )]
        elif args.app_id and args.private_key:
            apps = [AppCredentials(
                name='default',
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
        checker.export_prometheus_metrics(port=args.prometheus_port)
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
