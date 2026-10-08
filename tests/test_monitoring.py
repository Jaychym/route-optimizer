#!/usr/bin/env python3
"""Offline smoke tests; no real nfdump, ExaBGP or WAN access required."""
import importlib.util
import json
import math
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "src"


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = module(ROOT / 'route-optimizer.py', 'test_optimizer_engine_v33')
web = module(ROOT / 'route-optimizer-web.py', 'test_optimizer_dashboard_v33')


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        engine.configure_site({
            "ucg_exporter": "192.168.10.1", "bgp_peer": "192.168.10.1",
            "own_ips": ["192.168.10.50", "10.250.1.2", "10.250.2.2"],
            "internal_networks": ["192.168.10.0/24"],
            "wan_connected_networks": ["198.51.100.0/24", "203.0.113.0/24"],
            "isps": {"FRONTIER": {"source": "10.250.1.2", "next_hop": "198.51.100.1"},
                     "SPECTRUM": {"source": "10.250.2.2", "next_hop": "203.0.113.1"}},
        })
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.patchers = [
            patch.object(engine, 'HISTORY_FILE', self.tmp / 'quality-history-v3.json'),
            patch.object(engine, 'ROUTE_EVENTS_FILE', self.tmp / 'route-events-v3.json'),
            patch.object(engine, 'ACTIVE_STATE_PATH', self.tmp / 'state-v3.json'),
            patch.object(engine, 'STATUS_FILE', self.tmp / 'dashboard-v3.json'),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_history_uses_comparable_prefixes_and_bounded_samples(self):
        rows = [
            {'frontier': {'latency': 10., 'jitter': 1., 'loss': 0., 'score': 12.},
             'spectrum': {'latency': 20., 'jitter': 2., 'loss': 20., 'score': 524.}},
            {'frontier': {'latency': 12., 'jitter': 2., 'loss': 0., 'score': 16.},
             'spectrum': {'latency': 22., 'jitter': 3., 'loss': 0., 'score': 28.}},
            {'frontier': {'latency': None, 'jitter': None, 'loss': None, 'score': None},
             'spectrum': {'latency': 80., 'jitter': 10., 'loss': 0., 'score': 90.}},
        ]
        sample = engine.record_quality_sample(rows, timestamp=123)
        self.assertEqual(sample['paired_prefixes'], 2)
        self.assertEqual(sample['frontier']['rtt_ms'], 11)
        self.assertEqual(sample['spectrum']['rtt_ms'], 21)
        self.assertEqual(sample['spectrum']['loss_percent'], 10)
        self.assertEqual(json.loads(engine.HISTORY_FILE.read_text())[0], sample)
        with patch.object(engine, 'HISTORY_MAX_SAMPLES', 2):
            engine.record_quality_sample(rows, timestamp=124)
            engine.record_quality_sample(rows, timestamp=125)
        self.assertEqual([x['epoch'] for x in json.loads(engine.HISTORY_FILE.read_text())], [124, 125])

    def test_route_events_only_when_apply_and_route_state_update(self):
        state = engine.new_state()
        prefix = '104.20.27.0/24'
        with patch.object(engine, 'bgp', return_value=(True, 'accepted')) as mocked:
            ok, _ = engine.announce(prefix, 'FRONTIER', state, False, 50, 'hypothetical')
            self.assertTrue(ok)
            self.assertFalse(engine.ROUTE_EVENTS_FILE.exists())
            self.assertEqual(state['routes'], {})
            ok, _ = engine.announce(prefix, 'FRONTIER', state, True, 50, 'latency advantage')
            self.assertTrue(ok)
            self.assertEqual(state['routes'][prefix]['isp'], 'FRONTIER')
            ok, _ = engine.announce(prefix, 'SPECTRUM', state, True, 50, 'severe loss')
            self.assertTrue(ok)
            ok, _ = engine.withdraw(prefix, state, True)
            self.assertTrue(ok)
            self.assertEqual(len(mocked.call_args_list), 4)
        self.assertEqual(state['routes'], {})
        events = json.loads(engine.ROUTE_EVENTS_FILE.read_text())
        self.assertEqual([x['event'] for x in events], ['withdraw', 'move', 'announce'])
        self.assertEqual(events[0]['from'], 'SPECTRUM')
        self.assertEqual(events[1]['to'], 'SPECTRUM')

    def test_bgp_health_unknown_vs_established(self):
        from subprocess import CompletedProcess
        with patch.object(engine, 'command', return_value=CompletedProcess([], 0, '192.168.10.1 65050 ... established 3 0', '')):
            self.assertEqual(engine.bgp_health()['status'], 'established')
        with patch.object(engine, 'command', return_value=CompletedProcess([], 0, '192.168.10.1 65050 ... active 3 0', '')):
            self.assertEqual(engine.bgp_health()['status'], 'down')
        with patch.object(engine, 'command', return_value=CompletedProcess([], 1, '', 'CLI unavailable')):
            self.assertEqual(engine.bgp_health()['status'], 'unknown')

    def test_site_configuration_rejects_missing_wan_protections(self):
        good = {
            "ucg_exporter": "192.168.10.1",
            "own_ips": ["192.168.10.50", "10.250.1.2", "10.250.2.2"],
            "internal_networks": ["192.168.10.0/24"],
            "wan_connected_networks": ["198.51.100.0/24", "203.0.113.0/24"],
            "isps": {"FRONTIER": {"source": "10.250.1.2", "next_hop": "198.51.100.1"},
                     "SPECTRUM": {"source": "10.250.2.2", "next_hop": "203.0.113.1"}},
        }
        import copy
        wrong = copy.deepcopy(good)
        wrong["wan_connected_networks"] = ["198.51.100.0/24", "192.0.2.0/24"]
        with self.assertRaisesRegex(ValueError, "outside wan_connected_networks"):
            engine.configure_site(wrong)
        wrong = copy.deepcopy(good)
        wrong["own_ips"].remove("10.250.2.2")
        with self.assertRaisesRegex(ValueError, "must also be listed"):
            engine.configure_site(wrong)
        engine.configure_site(good)
        self.assertIsNone(engine.eligible_prefix("192.168.10.20"))
        self.assertIsNone(engine.eligible_prefix("198.51.100.1"))
        self.assertEqual(engine.eligible_prefix("1.1.1.1"), "1.1.1.0/24")

    def test_public_config_example_is_syntactically_valid(self):
        path = ROOT.parent / "examples" / "config.example.json"
        engine.configure_site(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(set(engine.ISPS), {"FRONTIER", "SPECTRUM"})

    def test_http_get_and_no_route_write_api(self):
        status = {'epoch': 1, 'rows': [], 'mode': 'DRY-RUN'}
        for name, data in [('dashboard-v3.json', status), ('quality-history-v3.json', []), ('route-events-v3.json', [])]:
            (self.tmp / name).write_text(json.dumps(data))
        original = web.FILES
        web.FILES = {endpoint: self.tmp / path.name for endpoint, path in original.items()}
        self.addCleanup(setattr, web, 'FILES', original)
        server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        server.timeout = .5
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = f'http://127.0.0.1:{server.server_address[1]}'
        for endpoint in ('/', '/api/status', '/api/history', '/api/route-events', '/healthz'):
            with urllib.request.urlopen(host + endpoint, timeout=4) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(host + '/api/routes', data=b'change', method='POST'), timeout=4)
        self.assertEqual(err.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(host + '/var/lib/route-optimizer/state-v3.json', timeout=4)
        self.assertEqual(err.exception.code, 404)
        with urllib.request.urlopen(host + '/api/status', timeout=4) as response:
            self.assertEqual(json.loads(response.read()), status)


if __name__ == '__main__':
    unittest.main(verbosity=2)