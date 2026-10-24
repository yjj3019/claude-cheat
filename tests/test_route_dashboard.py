"""Dashboard aggregation runs on synthetic logs and transcripts only."""
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "route_dashboard", Path(__file__).resolve().parents[1] / "routeDashboard.py")
dash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dash)


def route_line(stamp, payload):
    return f"{stamp} {json.dumps(payload)}\n"


class RouteLogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / ".route.log"
        self.enterContext(mock.patch.object(dash, "ROUTE_LOG", self.log))

    def test_state_tracks_the_newest_decision_and_idles_out(self):
        now = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)
        self.log.write_text(
            f"{now.isoformat().replace('+00:00', 'Z')} --- proxy start ---\n"
            + route_line((now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                         {"event": "route_selected", "provider": "openrouter", "reason": "replacement"})
            + route_line((now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                         {"event": "route_selected", "provider": "plan", "reason": "native_feature",
                          "detail": "plan_model"}),
            encoding="utf-8")
        state = dash.routing_state(dash.parse_route_log(), now=now)
        self.assertEqual(state["state"], "plan")
        self.assertEqual(state["counts"], {"external": 1, "plan": 1})
        self.assertEqual(state["plan_reasons"], [("native_feature:plan_model", 1)])
        self.assertEqual(state["proxy_started_at"], now.isoformat().replace("+00:00", "Z"))
        self.assertIsNotNone(state["last"]["age_seconds"])

        later = now + timedelta(minutes=6)
        self.assertEqual(dash.routing_state(dash.parse_route_log(), now=later)["state"], "idle")

    def test_failures_and_circuit_opens_are_counted_inside_the_window_only(self):
        now = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)
        stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        self.log.write_text(
            route_line(stamp(2), {"event": "external_attempt_failed", "provider": "openrouter",
                                  "status": 400, "validation": "invalid_message"})
            + route_line(stamp(1), {"event": "circuit_open", "provider": "openrouter"})
            + route_line(stamp(120), {"event": "external_attempt_failed", "provider": "openrouter",
                                      "status": 500}),
            encoding="utf-8")
        state = dash.routing_state(dash.parse_route_log(), now=now)
        self.assertEqual(state["failures"], [("400:invalid_message", 1)])
        self.assertEqual(state["circuit_opens"], 1)

    def test_malformed_lines_are_ignored(self):
        self.log.write_text(
            "not-a-timestamp {\"event\": \"route_selected\", \"provider\": \"plan\"}\n"
            "2026-10-25T02:00:00Z {broken json\n"
            "2026-10-25T02:00:00Z --- proxy start ---\n",
            encoding="utf-8")
        events = dash.parse_route_log()
        self.assertEqual([event["event"] for event in events],
                         ["route_selected", "proxy_start"])

    def test_missing_log_reports_unknown_state(self):
        self.assertEqual(dash.parse_route_log(), [])
        state = dash.routing_state([])
        self.assertEqual(state["state"], "unknown")
        self.assertIsNone(state["last"])


class SessionScanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.projects = self.root / "projects"
        self.session = self.projects / "D--project" / "abc12345-0000.jsonl"
        self.session.parent.mkdir(parents=True)
        self.enterContext(mock.patch.object(dash, "_SESSIONS", {}))

    def append(self, records):
        with self.session.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def assistant(self, message_id, model="claude-sonnet-5-5", stamp="2026-10-25T02:10:00.000Z",
                  tokens=(10, 2, 0, 4)):
        return {"type": "assistant", "timestamp": stamp,
                "message": {"id": message_id, "model": model,
                            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                                      "cache_read_input_tokens": tokens[2],
                                      "cache_creation_input_tokens": tokens[3]}}}

    def test_provider_split_and_token_totals(self):
        self.append([self.assistant("msg_011ABC"), self.assistant("msg_other"),
                     {"type": "user", "timestamp": "2026-10-25T02:10:01.000Z", "message": {}}])
        usage = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(usage["totals"]["plan"], [10, 2, 0, 4])
        self.assertEqual(usage["totals"]["external"], [10, 2, 0, 4])
        self.assertEqual(usage["session_count"], 1)
        row = usage["sessions"][0]
        self.assertEqual((row["turns"], row["plan"], row["external"]), (2, 1, 1))
        self.assertEqual(row["models"], [("claude-sonnet-5-5", 2)])
        # 02:10Z is 11:10 in KST, so it counts toward the local day and hour.
        self.assertEqual(usage["today"]["external"], [10, 2, 0, 4])
        self.assertEqual(usage["current_hour"], "2026-10-25T11")

    def test_appended_lines_are_counted_once_and_other_days_excluded(self):
        self.append([self.assistant("msg_other")])
        first = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(first["totals"]["external"], [10, 2, 0, 4])
        again = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(again["totals"]["external"], [10, 2, 0, 4], "re-scan must not double count")
        self.append([self.assistant("msg_other2", tokens=(1, 1, 0, 0))])
        third = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(third["totals"]["external"], [11, 3, 0, 4])
        # A transcript untouched for a week drops out of the scan.
        older = dash.scan_sessions(projects=self.projects, now=datetime(2026, 11, 25, 11, 0))
        self.assertEqual(older["session_count"], 0)

    def test_partial_trailing_line_waits_for_completion(self):
        with self.session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.assistant("msg_other")) [:40])
        usage = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(usage["session_count"], 0)
        with self.session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.assistant("msg_other"))[40:] + "\n")
        usage = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(usage["totals"]["external"], [10, 2, 0, 4])

    def test_missing_projects_directory_yields_empty_usage(self):
        usage = dash.scan_sessions(projects=self.root / "absent", now=datetime(2026, 10, 25, 11, 0))
        self.assertEqual(usage["session_count"], 0)
        self.assertEqual(usage["totals"]["external"], [0, 0, 0, 0])


class AccountTests(unittest.TestCase):
    def payload(self):
        body = {"data": {"limit": 50, "limit_remaining": 49.5, "limit_reset": "monthly",
                         "is_free_tier": False,
                         "budgets": [{"scope": "key_limit", "spend_usd": 0.5,
                                      "resets_at": "2026-11-01T00:00:00.000Z"}]}}
        return io.BytesIO(json.dumps(body).encode())

    def test_limit_and_spend_are_reported_and_cached(self):
        cache = {"at": 0.0, "data": None}
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=mock.Mock(read=lambda *_: self.payload().read()))
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=opener) as factory:
            first = dash.account_state("sk-or-synthetic", cache=cache, now=1000.0)
            self.assertTrue(first["ok"])
            self.assertEqual(first["limit_usd"], 50)
            self.assertEqual(first["spent_usd"], 0.5)
            self.assertEqual(first["remaining_usd"], 49.5)
            second = dash.account_state("sk-or-synthetic", cache=cache, now=1030.0)
            self.assertEqual(second, first)
            factory.assert_called_once()
            dash.account_state("sk-or-synthetic", cache=cache, now=1100.0)
            self.assertEqual(factory.call_count, 2)

    def test_missing_key_and_network_errors_stay_safe(self):
        self.assertEqual(dash.account_state("", cache={"at": 0.0, "data": None}, now=1.0)["error"],
                         "configuration_missing")
        opener = mock.Mock()
        opener.open.side_effect = dash.urllib.error.URLError("boom")
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=opener):
            result = dash.account_state("sk-or-synthetic", cache={"at": 0.0, "data": None}, now=1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "connection_failed")
        self.assertNotIn("sk-or-synthetic", json.dumps(result))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_and_loopback_only_binding(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "read_env", return_value={"OPENROUTER_MODEL": "vendor/model"}):
            data = dash.snapshot()
        self.assertEqual(sorted(data), ["account", "generated_at", "model", "routing", "usage"])
        self.assertEqual(data["model"], "vendor/model")
        self.assertEqual(data["routing"]["state"], "unknown")
        self.assertNotIn("sk-or", json.dumps(data))
        with self.assertRaises(SystemExit):
            dash.main(["--host", "0.0.0.0"])
        with self.assertRaises(SystemExit):
            dash.main(["--host", "example.invalid"])

    def test_cached_snapshot_serves_without_rebuilding(self):
        built = {"generated_at": "2026-10-25T03:00:00+00:00", "routing": {}, "usage": {}, "account": {},
                 "model": None}
        with mock.patch.object(dash, "_SNAPSHOT", {"at": 1.0, "data": built}), \
             mock.patch.object(dash, "snapshot", side_effect=AssertionError("must not rebuild")):
            self.assertIs(dash.cached_snapshot(), built)

    def test_empty_usage_has_stable_shape(self):
        usage = dash._empty_usage()
        self.assertEqual(usage["totals"]["external"], [0, 0, 0, 0])
        self.assertEqual(usage["session_count"], 0)
        self.assertRegex(usage["current_hour"], r"^\d{4}-\d{2}-\d{2}T\d{2}$")


if __name__ == "__main__":
    unittest.main()
