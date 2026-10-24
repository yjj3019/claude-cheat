"""Dashboard aggregation runs on synthetic logs and transcripts only."""
import contextlib
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
                          "detail": "plan_model", "model": "claude-opus-5-5"}),
            encoding="utf-8")
        state = dash.routing_state(dash.parse_route_log(), now=now)
        self.assertEqual(state["state"], "plan")
        self.assertEqual(state["counts"], {"external": 1, "plan": 1})
        self.assertEqual(state["plan_reasons"], [("native_feature:plan_model", 1)])
        self.assertEqual(state["last"]["model"], "claude-opus-5-5")
        self.assertEqual(state["models"], [("plan:claude-opus-5-5", 1)])
        self.assertEqual(state["short"]["models"], [("plan:claude-opus-5-5", 1)])
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
        self.assertEqual(state["models"], [])

    def test_events_without_a_model_keep_the_totals(self):
        now = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)
        self.log.write_text(
            route_line(now.isoformat().replace("+00:00", "Z"),
                       {"event": "route_selected", "provider": "openrouter", "reason": "replacement"})
            + route_line(now.isoformat().replace("+00:00", "Z"),
                         {"event": "route_selected", "provider": "openrouter", "reason": "replacement",
                          "model": "x" * 500}),
            encoding="utf-8")
        state = dash.routing_state(dash.parse_route_log(), now=now)
        self.assertEqual(state["counts"]["external"], 2)
        # Legacy lines have no model, and an oversized identifier is ignored rather than shown.
        self.assertEqual(state["models"], [])
        self.assertIsNone(state["last"]["model"])


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
        self.assertEqual(len(usage["timeline"]), 24)
        self.assertEqual(usage["timeline"][-1]["hour"], "2026-10-25T11")
        self.assertEqual(usage["timeline"][-1]["external"], {"turns": 1, "tokens": [10, 2, 0, 4]})
        self.assertEqual(usage["timeline"][-1]["plan"], {"turns": 1, "tokens": [10, 2, 0, 4]})
        self.assertIsNone(usage["cost_usd"])

    def test_estimated_cost_uses_external_tokens_and_model_rates(self):
        self.append([self.assistant("msg_other", tokens=(1_000_000, 1_000_000, 1_000_000, 0)),
                     self.assistant("msg_011ABC", tokens=(5, 5, 5, 5))])
        pricing = {"ok": True, "prompt": 0.00000015, "completion": 0.0000006,
                   "cache_read": 0.000000003, "cache_write": 0.0000009}
        usage = dash.scan_sessions(projects=self.projects, now=datetime(2026, 10, 25, 11, 0), pricing=pricing)
        # Only replacement tokens cost money on OpenRouter.
        self.assertAlmostEqual(usage["cost_usd"], 0.15 + 0.6 + 0.003, places=6)
        self.assertAlmostEqual(usage["sessions"][0]["cost_usd"], 0.15 + 0.6 + 0.003, places=6)
        # A plan-only session has no OpenRouter cost.
        self.assertEqual(dash._session_cost(usage["today"]["plan"], pricing),
                         5 * 0.00000015 + 5 * 0.0000006 + 5 * 0.000000003 + 5 * 0.0000009)

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


class PricingTests(unittest.TestCase):
    def opener(self, payload):
        body = io.BytesIO(json.dumps(payload).encode())
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=mock.Mock(read=lambda *_: body.read()))
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        return opener

    def test_rates_are_parsed_and_cached(self):
        payload = {"data": [{"id": "vendor/model",
                             "pricing": {"prompt": "0.000001", "completion": "0.000002",
                                         "input_cache_read": "0.0000001"}}]}
        cache = {"at": 0.0, "data": None}
        with mock.patch.object(dash.urllib.request, "build_opener",
                               return_value=self.opener(payload)) as factory:
            first = dash.model_pricing("sk-or-synthetic", "vendor/model", cache=cache, now=10.0)
            self.assertTrue(first["ok"])
            self.assertAlmostEqual(first["prompt"], 0.000001)
            self.assertAlmostEqual(first["cache_read"], 0.0000001)
            self.assertEqual(first["cache_write"], 0.0)
            again = dash.model_pricing("sk-or-synthetic", "vendor/model", cache=cache, now=11.0)
            self.assertEqual(again, first)
            factory.assert_called_once()
            dash.model_pricing("sk-or-synthetic", "vendor/model", cache=cache, now=10.0 + dash.PRICING_TTL + 1)
            self.assertEqual(factory.call_count, 2)

    def test_unknown_model_and_network_failures_stay_safe(self):
        cache = {"at": 0.0, "data": None}
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=self.opener({"data": []})):
            result = dash.model_pricing("sk-or-synthetic", "vendor/missing", cache=cache, now=1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "model_not_found")
        opener = mock.Mock()
        opener.open.side_effect = dash.urllib.error.URLError("boom")
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=opener):
            failed = dash.model_pricing("sk-or-synthetic", "vendor/model",
                                        cache={"at": 0.0, "data": None}, now=1.0)
        self.assertEqual(failed["error"], "connection_failed")
        self.assertNotIn("sk-or-synthetic", json.dumps(failed))


class AlertTests(unittest.TestCase):
    def routing(self, external=0, plan=0, reasons=None, failures=None, circuits=0, state="external"):
        return {"state": state,
                "last": {"reason": "native_feature", "detail": "plan_model"},
                "short": {"minutes": 10,
                          "counts": {"external": external, "plan": plan},
                          "plan_reasons": reasons or [],
                          "failures": failures or [],
                          "circuit_opens": circuits}}

    def test_quiet_replacement_traffic_has_no_alerts(self):
        alerts = dash.build_alerts(self.routing(external=8), {}, {"ok": True, "limit_usd": 50,
                                                                  "remaining_usd": 40}, {"ok": True})
        self.assertEqual(alerts, [])

    def test_spike_circuit_rejection_and_plan_state_are_ordered(self):
        alerts = dash.build_alerts(
            self.routing(external=1, plan=5, reasons=[["native_feature:plan_model", 5]],
                         failures=[["400:invalid_message", 2]], circuits=1, state="plan"),
            {}, {"ok": True, "limit_usd": 50, "remaining_usd": 40}, {"ok": True})
        self.assertEqual([alert["level"] for alert in alerts], ["critical", "warn", "warn", "info"])
        self.assertEqual([alert["title"] for alert in alerts],
                         ["외부 회로 차단", "Plan 우회 급증", "외부 요청 거부", "현재 Plan 경로"])
        self.assertIn("native_feature:plan_model", alerts[1]["detail"])

    def test_low_balance_and_pricing_failure_are_surfaced(self):
        alerts = dash.build_alerts(self.routing(external=3), {},
                                   {"ok": True, "limit_usd": 50, "remaining_usd": 2.0},
                                   {"ok": False, "error": "connection_failed"})
        titles = [alert["title"] for alert in alerts]
        self.assertEqual(titles[0], "OpenRouter 잔액 부족")
        self.assertIn("단가 조회 실패", titles)


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash, "read_env", return_value={
            "ANTHROPIC_AUTH_TOKEN": "sk-or-synthetic", "OPENROUTER_MODEL": "vendor/model"}))

    def event(self, provider="openrouter", reason="replacement", model="vendor/model", minutes=0):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        return {"ts": stamp, "event": "route_selected", "provider": provider, "reason": reason,
                "model": model}

    def test_external_state_is_healthy_and_does_not_scan_transcripts(self):
        with mock.patch.object(dash, "parse_route_log", return_value=[self.event()]), \
             mock.patch.object(dash, "account_state",
                               return_value={"ok": True, "limit_usd": 50, "remaining_usd": 40}) as account, \
             mock.patch.object(dash, "scan_sessions",
                               side_effect=AssertionError("status must stay cheap")) as scan:
            report = dash.status_report()
        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "external")
        self.assertEqual(report["provider"], "openrouter")
        self.assertEqual(report["model"], "vendor/model")
        self.assertEqual(report["alerts"], [])
        self.assertNotIn("usage", report)
        self.assertEqual(report["config"]["model"], "vendor/model")
        account.assert_called_once()
        scan.assert_not_called()
        self.assertEqual(dash.status_exit_code(report), 0)
        self.assertNotIn("sk-or-synthetic", json.dumps(report))

    def test_plan_state_reports_exit_code_two(self):
        with mock.patch.object(dash, "parse_route_log",
                               return_value=[self.event(provider="plan", reason="native_feature")]), \
             mock.patch.object(dash, "account_state", return_value={"ok": True, "limit_usd": 50,
                                                                   "remaining_usd": 40}):
            report = dash.status_report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "plan")
        self.assertIn("현재 Plan 경로", [alert["title"] for alert in report["alerts"]])
        self.assertEqual(dash.status_exit_code(report), 2)

    def test_critical_alert_wins_over_plan(self):
        with mock.patch.object(dash, "parse_route_log", return_value=[
                self.event(provider="plan", reason="fallback", minutes=1),
                {"ts": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                 "event": "circuit_open", "provider": "openrouter"}]), \
             mock.patch.object(dash, "account_state", return_value={"ok": True, "limit_usd": 50,
                                                                   "remaining_usd": 40}):
            report = dash.status_report()
        self.assertEqual(report["alerts"][0]["title"], "외부 회로 차단")
        self.assertEqual(dash.status_exit_code(report), 3)

    def test_offline_skips_account_and_usage_opt_in_is_explicit(self):
        usage = dash._empty_usage()
        usage["cost_usd"] = 0.5
        with mock.patch.object(dash, "parse_route_log", return_value=[self.event()]), \
             mock.patch.object(dash, "account_state",
                               side_effect=AssertionError("offline must not call the API")), \
             mock.patch.object(dash, "model_pricing", side_effect=AssertionError("offline pricing")), \
             mock.patch.object(dash, "scan_sessions", return_value=usage) as scan:
            report = dash.status_report(include_usage=True, offline=True)
        self.assertEqual(report["account"]["error"], "offline")
        self.assertEqual(report["usage"]["cost_usd"], 0.5)
        scan.assert_called_once()

    def test_no_route_record_returns_exit_code_four(self):
        with mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}):
            report = dash.status_report()
        self.assertEqual(report["state"], "unknown")
        self.assertFalse(report["ok"])
        self.assertEqual(dash.status_exit_code(report), 4)

    def test_cli_prints_json_and_returns_the_exit_code(self):
        payload = {"ok": True, "state": "external", "alerts": [], "model": "vendor/model"}
        with mock.patch.object(dash, "status_report", return_value=payload) as builder, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = dash.main(["--status", "--offline"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)
        builder.assert_called_once_with(include_usage=False, offline=True)

    def test_cli_pretty_and_usage_flags(self):
        payload = {"ok": False, "state": "plan", "alerts": [{"level": "warn", "title": "t", "detail": "d"}]}
        with mock.patch.object(dash, "status_report", return_value=payload) as builder, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = dash.main(["--status", "--usage", "--pretty"])
        self.assertEqual(code, 2)
        self.assertIn("\n", output.getvalue())
        builder.assert_called_once_with(include_usage=True, offline=False)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_and_loopback_only_binding(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "model_pricing", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "read_env", return_value={"OPENROUTER_MODEL": "vendor/model"}):
            data = dash.snapshot()
        self.assertEqual(sorted(data), ["account", "alerts", "generated_at", "model", "pricing", "routing", "usage"])
        self.assertEqual(data["model"], "vendor/model")
        self.assertEqual(data["routing"]["state"], "unknown")
        self.assertEqual(data["alerts"], [])
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
        self.assertEqual(usage["timeline"], [])
        self.assertIsNone(usage["cost_usd"])
        self.assertRegex(usage["current_hour"], r"^\d{4}-\d{2}-\d{2}T\d{2}$")


if __name__ == "__main__":
    unittest.main()
