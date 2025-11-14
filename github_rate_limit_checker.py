#!/usr/bin/env python3
"""
GitHub API Rate Limit Checker
Monitors GitHub API rate limits for both REST API and GitHub App installations.
Useful for diagnosing KEDA or other automation issues related to API throttling.

Usage:
  # Using Personal Access Token:
  export GITHUB_TOKEN=ghp_xxxxx
  python github_rate_limit_checker.py

  # Using GitHub App credentials:
  export GITHUB_APP_ID=123456
  export GITHUB_APP_INSTALLATION_ID=987654
  export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/private-key.pem
  python github_rate_limit_checker.py

  # Continuous monitoring mode (checks every 60 seconds):
  python github_rate_limit_checker.py --watch --interval 60

  # Export metrics for Prometheus/monitoring:
  python github_rate_limit_checker.py --prometheus-port 9090
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any
import requests

try:
    import jwt  # PyJWT for GitHub App authentication
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    print("Warning: PyJWT not installed. GitHub App authentication unavailable.", file=sys.stderr)


class GitHubRateLimitChecker:
    """Monitor GitHub API rate limits."""

    def __init__(self, token: Optional[str] = None, app_id: Optional[str] = None,
                 private_key_path: Optional[str] = None, installation_id: Optional[str] = None):
        self.token = token
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.installation_id = installation_id
        self.session = requests.Session()

        if token:
            self.session.headers['Authorization'] = f'token {token}'
            self.session.headers['Accept'] = 'application/vnd.github.v3+json'
        elif app_id and private_key_path:
            if not JWT_AVAILABLE:
                raise RuntimeError("PyJWT required for GitHub App auth. Install: pip install PyJWT cryptography")
            self._setup_app_auth()

    def _setup_app_auth(self):
        """Generate JWT token for GitHub App authentication and get installation access token."""
        with open(self.private_key_path, 'r') as f:
            private_key = f.read()

        now = int(time.time())
        payload = {
            'iat': now,
            'exp': now + 600,  # 10 minutes
            'iss': self.app_id
        }

        jwt_token = jwt.encode(payload, private_key, algorithm='RS256')

        # If installation_id provided, get installation access token
        if self.installation_id:
            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            try:
                response = requests.post(
                    f'https://api.github.com/app/installations/{self.installation_id}/access_tokens',
                    headers=headers,
                    timeout=10
                )
                response.raise_for_status()
                installation_token = response.json()['token']
                self.session.headers['Authorization'] = f'token {installation_token}'
            except requests.exceptions.RequestException as e:
                print(f"Warning: Failed to get installation token: {e}. Using JWT.", file=sys.stderr)
                self.session.headers['Authorization'] = f'Bearer {jwt_token}'
        else:
            # Use JWT directly (useful for checking app-level rate limits)
            self.session.headers['Authorization'] = f'Bearer {jwt_token}'

        self.session.headers['Accept'] = 'application/vnd.github.v3+json'

    def check_rate_limit(self) -> Dict[str, Any]:
        """
        Check current GitHub API rate limits.

        Returns:
            Dict with rate limit information for all API categories
        """
        try:
            response = self.session.get('https://api.github.com/rate_limit', timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {'error': str(e)}

    def check_graphql_rate_limit(self) -> Dict[str, Any]:
        """
        Check GraphQL API rate limits.

        Returns:
            Dict with GraphQL rate limit information
        """
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
            response = self.session.post(
                'https://api.github.com/graphql',
                json={'query': query},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if 'errors' in data:
                return {'error': data['errors']}

            return data.get('data', {}).get('rateLimit', {})
        except requests.exceptions.RequestException as e:
            return {'error': str(e)}

    def format_reset_time(self, reset_timestamp: int) -> str:
        """Convert Unix timestamp to human-readable time."""
        reset_time = datetime.fromtimestamp(reset_timestamp, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = reset_time - now

        minutes = int(delta.total_seconds() / 60)
        seconds = int(delta.total_seconds() % 60)

        return f"{reset_time.strftime('%Y-%m-%d %H:%M:%S UTC')} (in {minutes}m {seconds}s)"

    def print_rate_limit_status(self, data: Dict[str, Any]):
        """Print formatted rate limit status."""
        if 'error' in data:
            print(f"❌ Error checking rate limits: {data['error']}", file=sys.stderr)
            return

        print(f"\n{'=' * 80}")
        print(f"GitHub API Rate Limit Status - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
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

    def print_graphql_status(self, data: Dict[str, Any]):
        """Print formatted GraphQL rate limit status."""
        if 'error' in data:
            print(f"❌ Error checking GraphQL rate limits: {data['error']}", file=sys.stderr)
            return

        print(f"{'=' * 80}")
        print("GraphQL API Rate Limit")
        print(f"{'=' * 80}\n")

        limit = data.get('limit', 0)
        remaining = data.get('remaining', 0)
        used = data.get('used', 0)
        cost = data.get('cost', 0)
        node_count = data.get('nodeCount', 0)
        reset_at = data.get('resetAt', '')

        percentage_remaining = (remaining / limit * 100) if limit > 0 else 0

        if percentage_remaining > 50:
            status = "✅ HEALTHY"
        elif percentage_remaining > 20:
            status = "⚠️  WARNING"
        else:
            status = "🚨 CRITICAL"

        print(f"Status:         {status}")
        print(f"Limit:          {limit:>6}")
        print(f"Used:           {used:>6}")
        print(f"Remaining:      {remaining:>6} ({percentage_remaining:>5.1f}%)")
        print(f"Last Query Cost: {cost}")
        print(f"Node Count:     {node_count}")
        print(f"Resets at:      {reset_at}")
        print()

    def export_prometheus_metrics(self, port: int = 9090):
        """
        Export rate limit metrics in Prometheus format via HTTP endpoint.

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

                rate_data = checker.check_rate_limit()
                graphql_data = checker.check_graphql_rate_limit()

                metrics = []

                # REST API metrics
                if 'resources' in rate_data:
                    for resource_name, limits in rate_data['resources'].items():
                        metrics.append(f'github_rate_limit_limit{{resource="{resource_name}"}} {limits.get("limit", 0)}')
                        metrics.append(f'github_rate_limit_remaining{{resource="{resource_name}"}} {limits.get("remaining", 0)}')
                        metrics.append(f'github_rate_limit_used{{resource="{resource_name}"}} {limits.get("used", 0)}')
                        metrics.append(f'github_rate_limit_reset{{resource="{resource_name}"}} {limits.get("reset", 0)}')

                # GraphQL metrics
                if 'error' not in graphql_data:
                    metrics.append(f'github_graphql_rate_limit_limit {graphql_data.get("limit", 0)}')
                    metrics.append(f'github_graphql_rate_limit_remaining {graphql_data.get("remaining", 0)}')
                    metrics.append(f'github_graphql_rate_limit_used {graphql_data.get("used", 0)}')

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
        print("Press Ctrl+C to stop\n")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n\n✅ Metrics server stopped")
            sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description='Monitor GitHub API rate limits',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument('--token', help='GitHub Personal Access Token (or use GITHUB_TOKEN env var)')
    parser.add_argument('--app-id', help='GitHub App ID (or use GITHUB_APP_ID env var)')
    parser.add_argument('--installation-id', help='GitHub App Installation ID (or use GITHUB_APP_INSTALLATION_ID env var)')
    parser.add_argument('--private-key', help='Path to GitHub App private key (or use GITHUB_APP_PRIVATE_KEY_PATH env var)')
    parser.add_argument('--watch', action='store_true', help='Continuously monitor rate limits')
    parser.add_argument('--interval', type=int, default=60, help='Interval in seconds for watch mode (default: 60)')
    parser.add_argument('--prometheus-port', type=int, help='Export Prometheus metrics on specified port')
    parser.add_argument('--json', action='store_true', help='Output in JSON format')

    args = parser.parse_args()

    # Get credentials from args or environment
    token = args.token or os.getenv('GITHUB_TOKEN')
    app_id = args.app_id or os.getenv('GITHUB_APP_ID')
    installation_id = args.installation_id or os.getenv('GITHUB_APP_INSTALLATION_ID')
    private_key_path = args.private_key or os.getenv('GITHUB_APP_PRIVATE_KEY_PATH')

    if not token and not (app_id and private_key_path):
        print("Error: Provide either GITHUB_TOKEN or both GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH", file=sys.stderr)
        print("\nExamples:", file=sys.stderr)
        print("  export GITHUB_TOKEN=ghp_xxxxx", file=sys.stderr)
        print("  python github_rate_limit_checker.py", file=sys.stderr)
        print("\n  OR", file=sys.stderr)
        print("\n  export GITHUB_APP_ID=123456", file=sys.stderr)
        print("  export GITHUB_APP_INSTALLATION_ID=987654", file=sys.stderr)
        print("  export GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem", file=sys.stderr)
        print("  python github_rate_limit_checker.py", file=sys.stderr)
        sys.exit(1)

    try:
        checker = GitHubRateLimitChecker(token=token, app_id=app_id, private_key_path=private_key_path, installation_id=installation_id)
    except Exception as e:
        print(f"Error initializing checker: {e}", file=sys.stderr)
        sys.exit(1)

    # Prometheus export mode
    if args.prometheus_port:
        checker.export_prometheus_metrics(port=args.prometheus_port)
        return

    # Watch mode
    if args.watch:
        print(f"🔄 Monitoring GitHub API rate limits every {args.interval} seconds...")
        print("Press Ctrl+C to stop\n")

        try:
            while True:
                rate_data = checker.check_rate_limit()
                graphql_data = checker.check_graphql_rate_limit()

                if args.json:
                    print(json.dumps({
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'rest_api': rate_data,
                        'graphql': graphql_data
                    }, indent=2))
                else:
                    checker.print_rate_limit_status(rate_data)
                    checker.print_graphql_status(graphql_data)

                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\n✅ Monitoring stopped")
            sys.exit(0)

    # Single check mode
    else:
        rate_data = checker.check_rate_limit()
        graphql_data = checker.check_graphql_rate_limit()

        if args.json:
            print(json.dumps({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'rest_api': rate_data,
                'graphql': graphql_data
            }, indent=2))
        else:
            checker.print_rate_limit_status(rate_data)
            checker.print_graphql_status(graphql_data)


if __name__ == '__main__':
    main()
