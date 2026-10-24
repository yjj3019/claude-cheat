"""Dashboard aggregation runs on synthetic logs and transcripts only."""
import contextlib
import importlib.util
import inspect
import io
import json
import os
import tempfile
import time
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
            "2026-10-25T02:00:00Z --- proxy start ---\n"
            "2026-10-25T03:00:00Z --- proxy start code=2026-10-25T02:30:00.000Z ---\n",
            encoding="utf-8")
        events = dash.parse_route_log()
        self.assertEqual([event["event"] for event in events],
                         ["route_selected", "proxy_start", "proxy_start"])
        self.assertNotIn("code", events[1])
        self.assertEqual(events[2]["code"], "2026-10-25T02:30:00.000Z")
        state = dash.routing_state(events)
        self.assertEqual(state["proxy_code"], "2026-10-25T02:30:00.000Z")

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


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / ".route.log"
        self.enterContext(mock.patch.object(dash, "ROUTE_LOG", self.log))

    def test_consecutive_decisions_collapse_with_durations(self):
        start = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)

        def stamp(minutes):
            return (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

        self.log.write_text(
            route_line(stamp(0), {"event": "route_selected", "provider": "openrouter",
                                  "reason": "replacement", "model": "vendor/a"})
            + route_line(stamp(1), {"event": "route_selected", "provider": "openrouter",
                                    "reason": "replacement", "model": "vendor/a"})
            + route_line(stamp(4), {"event": "route_selected", "provider": "plan",
                                    "reason": "native_feature", "detail": "plan_model"})
            + route_line(stamp(5), {"event": "route_selected", "provider": "plan",
                                    "reason": "native_feature", "detail": "plan_model"}),
            encoding="utf-8")
        entries = dash.route_transitions(dash.parse_route_log())
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["state"], "external")
        self.assertEqual(entries[0]["count"], 2)
        self.assertEqual(entries[0]["model"], "vendor/a")
        self.assertEqual(entries[0]["seconds"], 240.0)
        self.assertFalse(entries[0]["ongoing"])
        self.assertEqual(entries[1]["state"], "plan")
        self.assertEqual(entries[1]["count"], 2)
        self.assertTrue(entries[1]["ongoing"])
        self.assertIsNone(entries[1]["seconds"])
        self.assertEqual(entries[1]["detail"], "plan_model")

    def test_transition_limit_keeps_the_newest_entries(self):
        start = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)
        lines = []
        for index in range(30):
            provider = "openrouter" if index % 2 else "plan"
            lines.append(route_line((start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
                                    {"event": "route_selected", "provider": provider, "reason": "replacement"}))
        self.log.write_text("".join(lines), encoding="utf-8")
        entries = dash.route_transitions(dash.parse_route_log(), limit=5)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[-1]["first_ts"],
                         (start + timedelta(minutes=29)).isoformat().replace("+00:00", "Z"))

    def test_help_covers_every_reason_the_proxy_emits(self):
        expected = {"native_feature:plan_model", "native_feature:token_count",
                    "native_feature:unsupported_beta", "native_feature:unsupported_content_field",
                    "native_feature:unsupported_content_block", "fallback", "circuit_open"}
        self.assertLessEqual(expected, set(dash.REASON_HELP))
        for code, values in dash.REASON_HELP.items():
            self.assertEqual(len(values), 2, code)
            self.assertTrue(all(isinstance(value, str) and value for value in values), code)
        for row in dash.HELP_SECTION["states"] + dash.HELP_SECTION["exit_codes"]:
            self.assertEqual(len(row), 2)
        commands = dash.HELP_SECTION["commands"]
        self.assertEqual(len(commands), 6)
        self.assertTrue(all(len(row) == 2 and all(row) for row in commands))
        joined = " ".join(row[0] for row in commands)
        for flag in ("--status", "--pretty", "--usage", "--offline"):
            self.assertIn(flag, joined)
        self.assertEqual(len(dash.HELP_SECTION["endpoints"]), 4)
        names = " ".join(endpoint for endpoint, _ in dash.HELP_SECTION["endpoints"])
        self.assertIn("/api/config", names)
        self.assertIn("/api/metrics", names)


class PageTests(unittest.TestCase):
    def scripts(self):
        import re

        return {
            "dashboard": re.search(r"<script>(.*)</script>", dash.PAGE, re.S).group(1),
            "settings": re.search(r"<script>(.*)</script>", dash.SETTINGS_PAGE, re.S).group(1),
        }

    def test_both_pages_are_valid_javascript(self):
        """A stray escape in the Python string can break a page without failing any API test."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        for name, script in self.scripts().items():
            with self.subTest(page=name):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "page.js"
                    path.write_text(script, encoding="utf-8")
                    result = subprocess.run([node, "--check", str(path)],
                                            capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[:400])

    def test_pages_are_separated(self):
        dashboard, settings = self.scripts()["dashboard"], self.scripts()["settings"]
        self.assertIn("help.commands", dashboard)
        self.assertIn("renderGrid", dashboard)
        self.assertNotIn("saveSettings", dashboard)
        self.assertIn("saveSettings", settings)
        self.assertNotIn("renderGrid", settings)
        for page in (dash.PAGE, dash.SETTINGS_PAGE):
            self.assertIn('const TOKEN = "__DASHBOARD_TOKEN__"', page)
            self.assertIn('href="/"', page)
            self.assertIn('href="/settings"', page)

    def test_routes_serve_the_matching_page(self):
        import threading
        import urllib.error
        import urllib.request

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            base = f"http://127.0.0.1:{port}"
            dashboard = opener.open(base + "/", timeout=10).read().decode("utf-8")
            settings = opener.open(base + "/settings", timeout=10).read().decode("utf-8")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                opener.open(base + "/nope", timeout=10)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(missing.exception.code, 404)
        missing.exception.close()
        self.assertIn("renderGrid", dashboard)
        self.assertIn("saveSettings", settings)
        self.assertNotIn("__DASHBOARD_TOKEN__", dashboard)
        self.assertNotIn("__DASHBOARD_TOKEN__", settings)


class ProxyStaleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.code = self.root / "claudeRouteProxy.mjs"
        self.code.write_text("// fixture", encoding="utf-8")
        modified = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.code, (modified, modified))

    def routing(self, started):
        return {"proxy_started_at": started, "state": "plan", "last": None}

    def test_proxy_started_before_the_code_is_stale(self):
        stale = dash.proxy_stale(self.routing("2026-10-25T02:40:00Z"), path=self.code)
        self.assertTrue(stale)

    def test_proxy_started_after_the_code_is_current(self):
        self.assertFalse(dash.proxy_stale(self.routing("2026-10-25T03:10:00Z"), path=self.code))

    def test_missing_start_or_missing_file_is_unknown(self):
        self.assertIsNone(dash.proxy_stale(self.routing(None), path=self.code))
        self.assertIsNone(dash.proxy_stale(self.routing("2026-10-25T03:10:00Z"), path=self.root / "absent.mjs"))

    def test_recorded_code_stamp_is_authoritative(self):
        older = {"proxy_started_at": "2026-10-25T03:20:00Z", "proxy_code": "2026-10-25T02:30:00.000Z"}
        current = {"proxy_started_at": "2026-10-25T02:00:00Z", "proxy_code": "2026-10-25T03:00:00.000Z"}
        self.assertTrue(dash.proxy_stale(older, path=self.code))
        self.assertFalse(dash.proxy_stale(current, path=self.code))

    def test_stale_proxy_raises_a_restart_alert(self):
        routing = self.routing("2026-10-25T02:40:00Z")
        routing["short"] = {"minutes": 10, "counts": {"external": 1, "plan": 0}, "plan_reasons": [],
                            "failures": [], "circuit_opens": 0}
        with mock.patch.object(dash, "PROXY_FILE", self.code):
            alerts = dash.build_alerts(routing, {}, {"ok": True, "limit_usd": 50, "remaining_usd": 40},
                                       {"ok": True})
        self.assertIn("실행기 재시작 필요", [alert["title"] for alert in alerts])


class PortTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pid_file = self.root / ".dashboard.pid"
        self.enterContext(mock.patch.object(dash, "PID_FILE", self.pid_file))

    def test_pid_record_roundtrip_and_malformed_input(self):
        self.assertIsNone(dash._read_pid_record())
        self.pid_file.write_text("{broken", encoding="utf-8")
        self.assertIsNone(dash._read_pid_record())
        self.pid_file.write_text('{"pid": "x", "port": 1}', encoding="utf-8")
        self.assertIsNone(dash._read_pid_record())
        record = dash._write_pid_record(8787)
        self.assertEqual(record["port"], 8787)
        self.assertEqual(dash._read_pid_record()["pid"], record["pid"])
        self.assertIn("started", record)

    def test_busy_port_fails_instead_of_moving(self):
        import socket as socket_module

        holder = socket_module.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with mock.patch.object(dash, "_refresh_loop", lambda *args: None):
                with self.assertRaises(SystemExit):
                    dash.serve(port=port)
        finally:
            holder.close()

    def test_previous_instance_is_only_stopped_for_its_own_port(self):
        self.pid_file.write_text(json.dumps({"pid": os.getpid(), "port": 1}), encoding="utf-8")
        self.assertFalse(dash._stop_previous(8787))
        self.assertFalse(dash._stop_previous(1))

    def test_stop_only_targets_the_recorded_dashboard(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dash.main(["--stop"]), 1)
        self.assertIn("no_record", output.getvalue())
        self.pid_file.write_text(json.dumps({"pid": 4321, "port": 8787}), encoding="utf-8")
        with mock.patch.object(dash.os, "kill") as killer, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dash.main(["--stop"]), 0)
        killer.assert_called_once()
        self.assertEqual(killer.call_args.args[0], 4321)
        self.assertIn('"stopped": true', output.getvalue())

    def test_default_port_is_stable(self):
        self.assertEqual(dash.DEFAULT_PORT, 8787)
        parser_default = inspect.signature(dash.serve).parameters["port"].default
        self.assertEqual(parser_default, dash.DEFAULT_PORT)


class ServerTests(unittest.TestCase):
    def test_dashboard_server_binds_exclusively(self):
        self.assertFalse(dash.DashboardServer.allow_reuse_address)
        first = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        try:
            port = first.server_address[1]
            with self.assertRaises(OSError):
                # A stale instance must not share the port and answer with older code.
                dash.DashboardServer(("127.0.0.1", port), dash.Handler)
        finally:
            first.server_close()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = {
            "openrouter": {"path": self.root / ".env.openrouter", "secret": {"ANTHROPIC_AUTH_TOKEN"},
                           "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                           "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")},
            "local": {"path": self.root / ".env.local-llm", "secret": {"LOCAL_LLM_API_KEY"},
                      "required": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL"),
                      "fields": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                                 "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL")},
        }
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", self.files))

    def write(self, provider, text):
        self.files[provider]["path"].write_text(text, encoding="utf-8")

    def test_snapshot_masks_secrets_and_reports_which_are_set(self):
        self.write("openrouter", "# comment\nANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=vendor/model\n")
        snapshot = dash.config_snapshot()
        entries = snapshot["openrouter"]["fields"]
        self.assertTrue(entries["ANTHROPIC_AUTH_TOKEN"]["secret"])
        self.assertTrue(entries["ANTHROPIC_AUTH_TOKEN"]["set"])
        self.assertEqual(entries["ANTHROPIC_AUTH_TOKEN"]["value"], "")
        self.assertIn("…", entries["ANTHROPIC_AUTH_TOKEN"]["preview"])
        self.assertNotIn("abcdefghijkl", json.dumps(snapshot))
        self.assertEqual(entries["OPENROUTER_MODEL"]["value"], "vendor/model")

    def test_save_keeps_comments_unrelated_keys_and_the_stored_secret(self):
        self.write("openrouter", "# keep me\nANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\n"
                                 "OPENROUTER_MODEL=vendor/old\nUNRELATED=1\n")
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/new", "ANTHROPIC_AUTH_TOKEN": ""})
        text = self.files["openrouter"]["path"].read_text(encoding="utf-8")
        self.assertIn("# keep me", text)
        self.assertIn("UNRELATED=1", text)
        self.assertIn("OPENROUTER_MODEL=vendor/new", text)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl", text)
        self.assertTrue((self.root / ".env.openrouter.bak").is_file())

    def test_save_writes_a_new_secret_when_provided(self):
        self.write("openrouter", "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=vendor/a\n")
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/a", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-zzzzzzzzzzzz"})
        text = self.files["openrouter"]["path"].read_text(encoding="utf-8")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-or-v1-zzzzzzzzzzzz", text)
        self.assertEqual(text.count("ANTHROPIC_AUTH_TOKEN="), 1)

    def test_invalid_values_are_rejected_before_writing(self):
        self.write("openrouter", "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=vendor/a\n")
        before = self.files["openrouter"]["path"].read_text(encoding="utf-8")
        for values, reason in [
            ({"OPENROUTER_MODEL": "REPLACE_WITH_MODEL", "ANTHROPIC_AUTH_TOKEN": ""}, "invalid_model"),
            ({"OPENROUTER_MODEL": "vendor/a", "ANTHROPIC_AUTH_TOKEN": "nope"}, "invalid_key"),
            ({"OPENROUTER_MODEL": "", "ANTHROPIC_AUTH_TOKEN": ""}, "missing_openrouter_model"),
        ]:
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError) as caught:
                    dash.save_config("openrouter", values)
                self.assertEqual(str(caught.exception), reason)
        self.assertEqual(self.files["openrouter"]["path"].read_text(encoding="utf-8"), before)

    def test_local_validation_covers_url_mode_and_origin(self):
        base = {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODEL": "fixture"}
        dash._validate_local(base)
        self.assertRaises(ValueError, dash._validate_local, {**base, "LOCAL_LLM_URL": "http://example.com/v1/messages"})
        self.assertRaises(ValueError, dash._validate_local, {**base, "LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/chat"})
        self.assertRaises(ValueError, dash._validate_local, {**base, "LOCAL_LLM_ALLOW_REMOTE": "yes"})
        self.assertRaises(ValueError, dash._validate_local, {**base, "LOCAL_LLM_MODEL": ""})
        self.assertRaises(ValueError, dash._validate_local,
                          {**base, "LOCAL_LLM_MODELS_URL": "https://other.example/v1/models"})

    def test_connection_test_reports_metadata_and_inference_safely(self):
        self.write("openrouter", "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=vendor/a\n")
        with mock.patch.object(dash, "_http_json", return_value=(200, {"data": [{"id": "vendor/a"}]})):
            metadata = dash.run_connection_test("openrouter", "metadata")
        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["reason"], "model_found")
        self.assertNotIn("sk-or", json.dumps(metadata))

        reply = {"model": "vendor/a", "content": [{"type": "text", "text": "OK"}]}
        with mock.patch.object(dash, "_http_json", return_value=(200, reply)):
            inference = dash.run_connection_test("openrouter", "inference")
        self.assertTrue(inference["ok"])
        self.assertEqual(inference["reason"], "ready")

        with mock.patch.object(dash, "_http_json", return_value=(200, {**reply, "model": "other/model"})):
            wrong = dash.run_connection_test("openrouter", "inference")
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["reason"], "wrong_model")

    def test_connection_test_accepts_unsaved_values(self):
        with mock.patch.object(dash, "_http_json", return_value=(200, {"data": [{"id": "vendor/b"}]})):
            result = dash.run_connection_test(
                "openrouter", "metadata",
                {"OPENROUTER_MODEL": "vendor/b", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-unsavedkey1234"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "model_found")
        self.assertNotIn("unsavedkey", json.dumps(result))
        # A blank secret falls back to the stored value, so a saved key still works.
        self.write("openrouter", "ANTHROPIC_AUTH_TOKEN=sk-or-v1-storedkey1234\nOPENROUTER_MODEL=vendor/old\n")
        with mock.patch.object(dash, "_http_json", return_value=(200, {"data": [{"id": "vendor/c"}]})):
            fallback = dash.run_connection_test("openrouter", "metadata",
                                                {"OPENROUTER_MODEL": "vendor/c", "ANTHROPIC_AUTH_TOKEN": ""})
        self.assertTrue(fallback["ok"])

    def test_unsaved_values_are_validated_before_any_request(self):
        with mock.patch.object(dash, "_http_json") as request:
            result = dash.run_connection_test("openrouter", "metadata",
                                              {"OPENROUTER_MODEL": "vendor/b", "ANTHROPIC_AUTH_TOKEN": "bad"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_configuration")
        request.assert_not_called()

    def test_first_save_seeds_comments_from_the_example_file(self):
        example = self.root / ".env.openrouter.example"
        example.write_text("# shipped comment\nOPENROUTER_MODEL=REPLACE_WITH_OPENROUTER_MODEL_ID\n"
                           "ANTHROPIC_AUTH_TOKEN=YOUR_OPENROUTER_API_KEY\n", encoding="utf-8")
        self.assertFalse((self.root / ".env.openrouter").exists())
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/a",
                                        "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        text = (self.root / ".env.openrouter").read_text(encoding="utf-8")
        self.assertIn("# shipped comment", text)
        self.assertIn("OPENROUTER_MODEL=vendor/a", text)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl", text)
        self.assertNotIn("REPLACE_WITH_OPENROUTER_MODEL_ID", text)

    def test_connection_test_missing_configuration_and_http_errors(self):
        self.write("openrouter", "OPENROUTER_MODEL=vendor/a\n")
        self.assertEqual(dash.run_connection_test("openrouter")["reason"], "configuration_missing")
        self.write("openrouter", "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=vendor/a\n")
        import urllib.error
        with mock.patch.object(dash, "_http_json", side_effect=urllib.error.HTTPError(
                "u", 401, "denied", {}, None)):
            self.assertEqual(dash.run_connection_test("openrouter")["status"], 401)
        with mock.patch.object(dash, "_http_json", side_effect=dash.urllib.error.URLError("down")):
            self.assertEqual(dash.run_connection_test("openrouter")["reason"], "connection_failed")


class ProxyMetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / ".proxy-metrics.json"
        self.enterContext(mock.patch.object(dash, "METRICS_FILE", self.path))

    def test_missing_file_reports_none(self):
        self.assertIsNone(dash.proxy_metrics())

    def test_counters_are_exposed_with_age(self):
        self.path.write_text(json.dumps({"requests": 4, "routes": {"external": 3, "plan": 1}}),
                             encoding="utf-8")
        data = dash.proxy_metrics()
        self.assertEqual(data["requests"], 4)
        self.assertEqual(data["routes"]["external"], 3)
        self.assertFalse(data["stale"])
        self.assertGreaterEqual(data["age_seconds"], 0)

    def test_old_file_is_marked_stale(self):
        self.path.write_text('{"requests": 1}', encoding="utf-8")
        old = time.time() - 120
        os.utime(self.path, (old, old))
        data = dash.proxy_metrics()
        self.assertTrue(data["stale"])
        self.assertGreater(data["age_seconds"], 60)

    def test_broken_json_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(dash.proxy_metrics())

    def test_snapshot_carries_the_metrics(self):
        self.path.write_text(json.dumps({"requests": 7, "latencyMs": {"external": {"p50": 3.5}}}),
                             encoding="utf-8")
        with mock.patch.object(dash, "model_pricing", return_value={"ok": False}), \
             mock.patch.object(dash, "account_state", return_value={"ok": False}), \
             mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()):
            data = dash.snapshot()
        self.assertEqual(data["proxy_metrics"]["requests"], 7)
        self.assertEqual(data["proxy_metrics"]["latencyMs"]["external"]["p50"], 3.5)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_and_loopback_only_binding(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "model_pricing", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "read_env", return_value={"OPENROUTER_MODEL": "vendor/model"}):
            data = dash.snapshot()
        self.assertEqual(sorted(data),
                         ["account", "alerts", "generated_at", "help", "model", "pricing", "proxy_metrics",
                          "proxy_stale", "routing", "usage"])
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
