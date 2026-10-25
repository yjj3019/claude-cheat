"""Regression tests for the defects found in the source audit."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import routeDashboard as dash


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    # --- settings file safety -------------------------------------------------
    def test_a_read_error_never_rewrites_the_settings_file(self):
        path = self.root / ".env.openrouter"
        original = "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=v/a\n# comment\n"
        path.write_text(original, encoding="utf-8")
        with mock.patch.object(dash.Path, "read_text", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                dash._write_env(path, {"OPENROUTER_MODEL": "v/b"})
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / ".env.openrouter.bak").exists())

    def test_a_non_utf8_settings_file_is_read_without_raising(self):
        path = self.root / ".env.openrouter"
        path.write_bytes(b"OPENROUTER_MODEL=v/a\n# \xff\xfe broken comment\n")
        values = dash.read_env(path)
        self.assertEqual(values.get("OPENROUTER_MODEL"), "v/a")
        with mock.patch.object(dash, "CONFIG_FILES", {
                "openrouter": {"path": path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                               "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                               "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}}):
            snapshot = dash.config_snapshot()  # must not raise
        self.assertEqual(snapshot["openrouter"]["fields"]["OPENROUTER_MODEL"]["value"], "v/a")

    def test_saving_only_a_new_key_keeps_the_stored_model(self):
        path = self.root / ".env.openrouter"
        path.write_text("ANTHROPIC_AUTH_TOKEN=sk-or-v1-aaaaaaaaaaaa\nOPENROUTER_MODEL=v/a\n",
                        encoding="utf-8")
        with mock.patch.object(dash, "CONFIG_FILES", {
                "openrouter": {"path": path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                               "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                               "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}}):
            saved = dash.save_config("openrouter", {"ANTHROPIC_AUTH_TOKEN": "sk-or-v1-bbbbbbbbbbbb"})
        text = path.read_text(encoding="utf-8")
        self.assertIn("sk-or-v1-bbbbbbbbbbbb", text)
        self.assertIn("OPENROUTER_MODEL=v/a", text)
        self.assertTrue(saved["openrouter"]["fields"]["ANTHROPIC_AUTH_TOKEN"]["set"])

    # --- transcript scanning --------------------------------------------------
    def test_a_malformed_record_does_not_swallow_the_next_one(self):
        projects = self.root / "projects"
        (projects / "p").mkdir(parents=True)
        transcript = projects / "p" / "s.jsonl"
        good = {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"model": "vendor/model", "usage": {"input_tokens": 7, "output_tokens": 3}}}
        transcript.write_text(
            json.dumps({"type": "assistant", "message": "oops"}) + "\n" + json.dumps(good) + "\n",
            encoding="utf-8")
        dash._SESSIONS.clear()
        usage = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(usage["totals"]["external"][0], 7)
        self.assertEqual(usage["totals"]["external"][1], 3)

    def test_a_bad_token_count_is_skipped_without_double_counting(self):
        projects = self.root / "projects"
        (projects / "p").mkdir(parents=True)
        transcript = projects / "p" / "s.jsonl"
        good = {"type": "assistant", "timestamp": "2026-10-25T01:00:00+00:00",
                "message": {"model": "vendor/model", "usage": {"input_tokens": 5, "output_tokens": 2}}}
        bad = {"type": "assistant", "timestamp": "2026-10-25T01:00:01+00:00",
               "message": {"model": "vendor/model", "usage": {"input_tokens": "many"}}}
        transcript.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n" + json.dumps(good) + "\n",
                              encoding="utf-8")
        dash._SESSIONS.clear()
        first = dash.scan_sessions(projects=projects, now=datetime.now())
        second = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(first["totals"]["external"][0], 10)
        self.assertEqual(second["totals"]["external"][0], 10, "a rescan must not double count")

    # --- route log concurrency -----------------------------------------------
    def test_concurrent_reads_do_not_double_count(self):
        log = self.root / ".route.log"
        lines = [f"2026-10-25T0{index % 9}:00:00+00:00 "
                 + json.dumps({"event": "route_selected", "provider": "openrouter",
                               "reason": "replacement"}) + "\n" for index in range(50)]
        log.write_text("".join(lines), encoding="utf-8")
        with mock.patch.object(dash, "_ROUTE_TAIL",
                               {"path": None, "events": [], "offset": 0, "digest": None}):
            results = []

            def worker():
                for _ in range(10):
                    results.append(len(dash.parse_route_log(log)))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertTrue(results)
        self.assertEqual(max(results), 50, "duplicate lines were appended by concurrent readers")

    # --- caches ---------------------------------------------------------------
    def test_pricing_cache_is_keyed_by_model(self):
        payload = {"data": [{"id": "a/a", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                            {"id": "b/b", "pricing": {"prompt": "0.00001", "completion": "0.00002"}}]}
        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def read(self, size=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout=8):
                self.calls += 1
                return FakeResponse(json.dumps(payload).encode("utf-8"))

        opener = FakeOpener()
        cache = {}
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=opener):
            first = dash.model_pricing("sk-or-v1-x", "a/a", cache=cache, now=100.0)
            second = dash.model_pricing("sk-or-v1-x", "b/b", cache=cache, now=101.0)
            third = dash.model_pricing("sk-or-v1-x", "b/b", cache=cache, now=102.0)
        self.assertEqual(first["prompt"], 1e-06)
        self.assertEqual(second["prompt"], 1e-05)
        self.assertEqual(third["prompt"], 1e-05)
        self.assertEqual(opener.calls, 2, "the second model must not reuse the first model's cache")

    # --- snapshot failure visibility -----------------------------------------
    def test_a_broken_snapshot_is_reported_instead_of_frozen(self):
        data = {"generated_at": "2026-10-25T00:00:00+00:00", "routing": {}, "usage": {}, "alerts": []}
        with mock.patch.dict(dash._SNAPSHOT, {"data": data, "error": {"message": "boom"}}, clear=True):
            payload = dash.cached_snapshot()
        self.assertEqual(payload["snapshot_error"], {"message": "boom"})
        self.assertIn("generated_at", payload)

    def test_scalar_metrics_do_not_break_the_insights(self):
        routing = {"state": "external", "last": {"age_seconds": 1},
                   "short": {"minutes": 10, "counts": {"external": 1, "plan": 0},
                             "plan_reasons": [], "failures": [], "circuit_opens": 0}}
        insights = dash.build_insights(routing, {"today": {"external": [0, 0, 0, 0]}, "cost_usd": None},
                                       {"ok": False}, None, metrics={"latencyMs": 5})
        self.assertIsInstance(insights, list)

    # --- stop safety ----------------------------------------------------------
    def test_stop_ignores_a_record_whose_port_is_closed(self):
        pid_file = self.root / ".dashboard.pid"
        pid_file.write_text(json.dumps({"pid": os.getpid(), "port": 1,
                                        "started": "2026-10-25T00:00:00+00:00"}), encoding="utf-8")
        with mock.patch.object(dash, "PID_FILE", pid_file), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = dash.main(["--stop"])
        self.assertEqual(code, 1)
        self.assertIn("stale_record", output.getvalue())
        self.assertFalse(pid_file.exists(), "a stale record must be cleared, not signalled")

    # --- rate limiting --------------------------------------------------------
    def test_write_requests_are_rate_limited_per_minute(self):
        with mock.patch.dict(dash._RATE, {"window": -1, "counts": {}}, clear=True):
            for index in range(dash.RATE_LIMIT_PER_MINUTE):
                self.assertTrue(dash._rate_ok("127.0.0.1", now=100.0), f"request {index + 1} should pass")
            self.assertFalse(dash._rate_ok("127.0.0.1", now=100.0), "the burst limit must stop here")
            self.assertTrue(dash._rate_ok("127.0.0.1", now=160.0), "a new minute starts fresh")
            self.assertTrue(dash._rate_ok("127.0.0.2", now=160.0), "clients are counted separately")

    # --- IPv6 ----------------------------------------------------------------
    def test_ipv6_hosts_are_bracketed(self):
        self.assertEqual(dash._display_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(dash._display_host("::1"), "[::1]")
        self.assertEqual(dash._display_host("[::1]"), "[::1]")


if __name__ == "__main__":
    unittest.main()
