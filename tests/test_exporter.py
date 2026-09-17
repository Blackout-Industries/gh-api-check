"""Tests for the cached Prometheus exporter."""

import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from github_rate_limit_checker import (
    AppCredentials,
    MetricsStore,
    SnapshotCollector,
    create_metrics_server,
    parse_expires_at,
)

STUB_APP_NAME = 'stub-app'
STUB_APP_ID = '1187645'
STUB_INSTALLATION_ID = '63220290'


def stub_results() -> Dict[str, Dict[str, Any]]:
    """One successful app, shaped like check_all_apps() output."""
    metadata = {
        'name': STUB_APP_NAME,
        'app_id': STUB_APP_ID,
        'installation_id': STUB_INSTALLATION_ID,
    }
    return {
        STUB_APP_NAME: {
            'rest_api': {
                'resources': {
                    'core': {'limit': 15000, 'remaining': 14875, 'used': 125, 'reset': 1726500000},
                    'search': {'limit': 30, 'remaining': 30, 'used': 0, 'reset': 1726499000},
                },
                'app_metadata': metadata,
            },
            'graphql': {
                'limit': 12500,
                'remaining': 12490,
                'used': 10,
                'app_metadata': metadata,
            },
        }
    }


def build_store() -> MetricsStore:
    return MetricsStore([
        AppCredentials(
            name=STUB_APP_NAME,
            app_id=STUB_APP_ID,
            installation_id=STUB_INSTALLATION_ID,
            private_key_path='/nonexistent.pem',
        )
    ])


def render(store: MetricsStore) -> str:
    registry = CollectorRegistry()
    registry.register(SnapshotCollector(store))
    return generate_latest(registry).decode('utf-8')


def sample_value(output: str, selector: str) -> float:
    """Look up one exposition line, so tests do not depend on float formatting."""
    for line in output.splitlines():
        if line.startswith(f'{selector} '):
            return float(line.split(' ', 1)[1])
    raise AssertionError(f'{selector} missing from exposition')


# the client library renders label names alphabetically
APP_LABELS = f'app_id="{STUB_APP_ID}",app_name="{STUB_APP_NAME}",installation_id="{STUB_INSTALLATION_ID}"'
NAME_LABEL = f'app_name="{STUB_APP_NAME}"'


class SnapshotRenderingTest(unittest.TestCase):
    def test_renders_expected_metric_families(self):
        store = build_store()
        store.record(stub_results(), {STUB_APP_NAME: 0.25}, collected_at=1726500123.0)
        output = render(store)

        app_labels = APP_LABELS
        core = f'{APP_LABELS},resource="core"'
        name_label = NAME_LABEL

        self.assertEqual(1.0, sample_value(output, f'github_app_status{{{app_labels}}}'))
        self.assertEqual(15000.0, sample_value(output, f'github_rate_limit_limit{{{core}}}'))
        self.assertEqual(14875.0, sample_value(output, f'github_rate_limit_remaining{{{core}}}'))
        self.assertEqual(125.0, sample_value(output, f'github_rate_limit_used{{{core}}}'))
        self.assertEqual(1726500000.0, sample_value(output, f'github_rate_limit_reset{{{core}}}'))
        self.assertEqual(30.0, sample_value(output, f'github_rate_limit_remaining{{{app_labels},resource="search"}}'))
        self.assertEqual(12500.0, sample_value(output, f'github_graphql_rate_limit_limit{{{app_labels}}}'))
        self.assertEqual(12490.0, sample_value(output, f'github_graphql_rate_limit_remaining{{{app_labels}}}'))
        self.assertEqual(10.0, sample_value(output, f'github_graphql_rate_limit_used{{{app_labels}}}'))
        self.assertEqual(0.25, sample_value(output, f'github_exporter_scrape_duration_seconds{{{name_label}}}'))
        self.assertEqual(
            1726500123.0,
            sample_value(output, f'github_exporter_last_success_timestamp_seconds{{{name_label}}}'))
        self.assertEqual(0.0, sample_value(output, f'github_exporter_scrape_errors_total{{{name_label}}}'))

    def test_empty_snapshot_reports_no_success(self):
        output = render(build_store())

        self.assertEqual(0.0, sample_value(output, f'github_app_status{{{APP_LABELS}}}'))
        self.assertEqual(0.0, sample_value(
            output, f'github_exporter_last_success_timestamp_seconds{{{NAME_LABEL}}}'))
        self.assertNotIn('github_rate_limit_remaining{', output)

    def test_failed_result_counts_an_error(self):
        store = build_store()
        store.record({STUB_APP_NAME: {'rest_api': {'error': 'boom', 'app_metadata': {}}, 'graphql': {}}}, {})
        output = render(store)

        self.assertEqual(0.0, sample_value(output, f'github_app_status{{{APP_LABELS}}}'))
        self.assertEqual(1.0, sample_value(output, f'github_exporter_scrape_errors_total{{{NAME_LABEL}}}'))
        self.assertEqual(0.0, store.last_success)


class ExpiryParsingTest(unittest.TestCase):
    def test_parses_iso8601_z(self):
        self.assertEqual(1726500000, parse_expires_at('2024-09-16T15:20:00Z', 0))

    def test_falls_back_on_garbage(self):
        self.assertEqual(42, parse_expires_at('not-a-timestamp', 42))
        self.assertEqual(42, parse_expires_at(None, 42))


class EndpointTest(unittest.TestCase):
    interval = 10

    def setUp(self):
        self.store = build_store()
        self.server = create_metrics_server(self.store, 0, self.interval)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str) -> Tuple[int, str, str]:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5) as response:
                return response.status, response.headers.get('Content-Type', ''), response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get('Content-Type', ''), error.read().decode('utf-8')

    def test_healthz_is_up_before_any_collection(self):
        status, _, body = self.get('/healthz')

        self.assertEqual(200, status)
        self.assertEqual('ok\n', body)

    def test_ready_is_503_before_first_success_and_200_after(self):
        status, _, body = self.get('/ready')
        self.assertEqual(503, status)
        self.assertIn('no successful collection', body)

        self.store.record(stub_results(), {STUB_APP_NAME: 0.1}, collected_at=time.time())

        status, _, body = self.get('/ready')
        self.assertEqual(200, status)
        self.assertEqual('ready\n', body)

    def test_ready_is_503_when_the_snapshot_is_stale(self):
        stale = time.time() - (self.interval * 4)
        self.store.record(stub_results(), {STUB_APP_NAME: 0.1}, collected_at=stale)

        status, _, body = self.get('/ready')

        self.assertEqual(503, status)
        self.assertIn('last successful collection', body)

    def test_metrics_serves_the_snapshot_with_the_library_content_type(self):
        self.store.record(stub_results(), {STUB_APP_NAME: 0.1}, collected_at=time.time())

        status, content_type, body = self.get('/metrics')

        self.assertEqual(200, status)
        self.assertEqual(CONTENT_TYPE_LATEST, content_type)
        self.assertIn(f'github_rate_limit_remaining{{{APP_LABELS},resource="core"}} 14875.0', body)

    def test_unknown_path_is_404(self):
        status, _, _ = self.get('/nope')

        self.assertEqual(404, status)


if __name__ == '__main__':
    unittest.main()
