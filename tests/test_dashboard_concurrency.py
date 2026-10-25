"""Parallel dashboard reads must agree: the shared route-log tail is the risky part."""
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import routeDashboard as dash


@unittest.skipUnless(__import__("os").environ.get("CLAUDE_CHEAT_SLOW") == "1",
                     "hammers the server with 12 threads; set CLAUDE_CHEAT_SLOW=1 to run")
class DashboardConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / ".route.log"
        stamp = datetime.now(timezone.utc)
        lines = []
        for index in range(120):
            when = (stamp - timedelta(seconds=120 - index)).isoformat()
            provider = "openrouter" if index % 5 else "plan"
            lines.append(f"{when} " + json.dumps({
                "event": "route_selected", "provider": provider,
                "reason": "replacement" if provider == "openrouter" else "native_feature",
                "model": "vendor/model" if provider == "openrouter" else "claude-sonnet-5-5"}) + "\n")
        self.log.write_text("".join(lines), encoding="utf-8")
        self.enterContext(mock.patch.object(dash, "ROUTE_LOG", self.log))
        self.enterContext(mock.patch.object(dash, "_ROUTE_TAIL",
                                            {"path": None, "events": [], "offset": 0, "digest": None}))
        self.enterContext(mock.patch.object(dash, "read_env",
                                            return_value={"OPENROUTER_MODEL": "vendor/model"}))
        self.enterContext(mock.patch.object(dash, "account_state",
                                            return_value={"ok": False, "error": "offline"}))
        self.enterContext(mock.patch.object(dash, "model_pricing", return_value={"ok": False}))
        self.enterContext(mock.patch.object(dash, "scan_sessions",
                                            return_value=dash._empty_usage()))
        self.server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fetch(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=60) as response:
            return response.status, json.loads(response.read())

    def test_parallel_status_reads_report_the_same_counts(self):
        results, errors = [], []

        def worker():
            for _ in range(8):
                try:
                    status, payload = self.fetch("/api/status")
                    results.append((status, payload["window_counts"], payload["plan_reasons"]))
                except Exception as error:  # noqa: BLE001 - the test reports the failure
                    errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 12 * 8)
        self.assertTrue(all(status == 200 for status, _counts, _reasons in results))
        counts = {json.dumps(item[1], sort_keys=True) for item in results}
        self.assertEqual(len(counts), 1, f"inconsistent counts under load: {counts}")
        self.assertEqual(json.loads(counts.pop()), {"external": 96, "plan": 24})

    def test_parallel_snapshots_stay_consistent(self):
        statuses = []
        errors = []

        def worker():
            for _ in range(6):
                try:
                    statuses.append(self.fetch("/api/snapshot")[0])
                except Exception as error:  # noqa: BLE001
                    errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(status == 200 for status in statuses))


if __name__ == "__main__":
    unittest.main()
