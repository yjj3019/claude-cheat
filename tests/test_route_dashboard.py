"""Dashboard aggregation runs on synthetic logs and transcripts only."""
import contextlib
import importlib.util
import inspect
import io
import http.client
import json
import os
import sys
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


class ResponseDisconnectTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(dash.Handler)
        handler.headers = {"host": "127.0.0.1:8787"}
        handler.path = "/api/snapshot"
        handler.close_connection = False
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        return handler

    def test_snapshot_disconnect_does_not_attempt_a_second_response(self):
        for error in (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            for stage in ("headers", "body"):
                with self.subTest(error=error.__name__, stage=stage):
                    handler = self.handler()
                    target = handler.end_headers if stage == "headers" else handler.wfile.write
                    target.side_effect = error("client disconnected")
                    with mock.patch.object(dash, "cached_snapshot", return_value={}):
                        handler.do_GET()
                    handler.send_response.assert_called_once_with(200)
                    self.assertTrue(handler.close_connection)
                    if stage == "headers":
                        handler.wfile.write.assert_not_called()

    def test_snapshot_failure_still_returns_500(self):
        handler = self.handler()
        with mock.patch.object(dash, "cached_snapshot", side_effect=ValueError("invalid snapshot")):
            handler.do_GET()
        handler.send_response.assert_called_once_with(500)
        self.assertEqual(json.loads(handler.wfile.write.call_args.args[0]),
                         {"error": "snapshot_failed"})

    def test_unrelated_io_error_is_not_suppressed(self):
        handler = self.handler()
        handler.wfile.write.side_effect = OSError("unexpected I/O failure")
        with self.assertRaises(OSError):
            handler._send(200, "application/json", "{}")


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

    def test_restart_does_not_reuse_previous_route_as_current_evidence(self):
        now = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)
        old = {"event": "route_selected", "provider": "openrouter", "model": "vendor/old",
               "ts": (now - timedelta(seconds=2)).isoformat()}
        start = {"event": "proxy_start", "ts": now.isoformat()}
        state = dash.routing_state([old, start], now=now)
        self.assertEqual(state["state"], "unknown")
        self.assertIsNone(state["last"])
        self.assertEqual(state["proxy_started_at"], start["ts"])
        self.assertEqual(state["counts"]["external"], 1)  # Historical window remains intact.
        current = {"event": "route_selected", "provider": "local", "model": "new-local",
                   "ts": now.isoformat()}
        state = dash.routing_state([old, start, current], now=now)
        self.assertEqual(state["last"]["provider"], "local")
        self.assertEqual(state["last"]["model"], "new-local")

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


class CurrentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 6, 10, tzinfo=timezone.utc)
        self.started = self.now - timedelta(minutes=5)
        self.session = {"provider": "local", "started": self.started.isoformat()}
        self.metrics = {"startedAt": self.started.isoformat(), "configuration": {
            "provider": "local", "model": "fixture-loaded", "alternate": None},
            "requests": 7, "routes": {"external": 0, "plan": 7},
            "last": {"ts": (self.now - timedelta(seconds=2)).isoformat(), "route": "plan",
                     "reason": "native_feature", "status": 200, "outcome": "ok"}}

    def event(self, when, provider="plan", model="historical-model"):
        return {"ts": when.isoformat(), "event": "route_selected", "provider": provider,
                "reason": "replacement" if provider == "local" else "native_feature", "model": model}

    def test_timestamp_order_does_not_use_clamped_durations(self):
        older = (self.started - timedelta(days=2)).isoformat()
        self.assertEqual(dash._seconds_between(self.started.isoformat(), older), 0)
        self.assertFalse(dash._in_session(older, self.started.isoformat(), self.now))
        self.assertTrue(dash._in_session(self.started.isoformat(), self.started.isoformat(), self.now))
        self.assertFalse(dash._in_session((self.now + timedelta(seconds=1)).isoformat(), self.started.isoformat(), self.now))

    def test_live_plan_route_replaces_a_previous_sessions_log_and_hash(self):
        events = [{"ts": (self.started - timedelta(days=2)).isoformat(), "event": "proxy_start", "hash": "old-hash"},
                  self.event(self.started - timedelta(minutes=1))]
        routing = dash._current_routing(events, self.session, self.metrics, now=self.now)
        self.assertEqual(routing["state"], "plan")
        self.assertEqual(routing["last"]["ts"], self.metrics["last"]["ts"])
        self.assertEqual(routing["last"]["provider"], "plan")
        self.assertIsNone(routing["last"]["model"], "metrics do not report the actual Plan model")
        self.assertIsNone(routing["proxy_hash"])
        self.assertEqual(routing["proxy_started_at"], self.metrics["startedAt"])
        with mock.patch.object(dash, "code_modified_at", return_value=self.started - timedelta(hours=1)):
            self.assertFalse(dash.proxy_stale(routing))

    def test_cumulative_counters_are_not_fabricated_as_windowed_log_counts(self):
        events = [self.event(self.started - timedelta(minutes=1)),
                  self.event(self.now - timedelta(seconds=4))]
        routing = dash._current_routing(events, self.session, self.metrics, now=self.now)
        self.assertEqual(routing["counts"], {"external": 0, "plan": 1})
        self.assertEqual(routing["short"]["counts"], {"external": 0, "plan": 1})
        self.assertEqual(len(routing["recent"]), 1)
        self.assertEqual(self.metrics["requests"], 7)

    def test_newer_log_evidence_is_not_overwritten_by_older_completed_metrics(self):
        event = self.event(self.now - timedelta(seconds=1), provider="local", model="fixture-loaded")
        routing = dash._current_routing([event], self.session, self.metrics, now=self.now)
        self.assertEqual(routing["last"]["model"], "fixture-loaded")
        self.assertEqual(routing["state"], "external")

    def test_unknown_or_future_live_routes_do_not_become_plan(self):
        for route, stamp in (("none", self.now.isoformat()),
                             ("plan", (self.now + timedelta(seconds=1)).isoformat())):
            with self.subTest(route=route):
                self.metrics["last"].update(route=route, ts=stamp)
                routing = dash._current_routing([self.event(self.started - timedelta(minutes=1))],
                                                self.session, self.metrics, now=self.now)
                self.assertEqual(routing["state"], "unknown")
                self.assertIsNone(routing["last"])

    def test_single_external_model_is_reported_but_dual_model_is_not_guessed(self):
        self.metrics["last"].update(route="external", reason="replacement")
        routing = dash._current_routing([], self.session, self.metrics, now=self.now)
        self.assertEqual(routing["last"]["model"], "fixture-loaded")
        self.metrics["configuration"]["alternate"] = {"provider": "openrouter", "model": "other-model"}
        routing = dash._current_routing([], self.session, self.metrics, now=self.now)
        self.assertIsNone(routing["last"]["model"])
        self.assertIsNone(routing["last"]["provider"])

    def test_failed_external_request_is_not_described_as_success(self):
        self.metrics["last"].update(route="external", reason="replacement", status=503, outcome="failed")
        routing = dash._current_routing([], self.session, self.metrics, now=self.now)
        summary = dash.summary_line(routing, dash._empty_usage(), {"ok": False}, [], live=True)
        self.assertIn("최근 요청 실패", summary)
        self.assertNotIn("정상 처리", summary)

    def test_current_openrouter_startup_hash_is_retained(self):
        self.session["provider"] = "openrouter"
        event = {"ts": (self.started - timedelta(milliseconds=20)).isoformat(),
                 "event": "proxy_start", "hash": "current-hash"}
        routing = dash._current_routing([event], self.session, self.metrics, now=self.now)
        self.assertEqual(routing["proxy_hash"], "current-hash")


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


class CostProvenanceTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 11, 0)
    RATES = {"ok": True, "prompt": 0.000003, "completion": 0.000015,
             "cache_read": 0.0000003, "cache_write": 0.00000375,
             "cache_write_1h": 0.000006,
             "pricing_version": "abc123def456",
             "fetched_at": "2026-10-25T02:00:00+00:00",
             "normalized_model": "claude-sonnet-4-5"}

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.projects = self.root / "projects"
        self.session = self.projects / "D--project" / "cost.jsonl"
        self.session.parent.mkdir(parents=True)
        self.enterContext(mock.patch.object(dash, "_SESSIONS", {}))
        self.enterContext(mock.patch.object(dash, "_CLAIM_OWNER", {}, create=True))
        self.enterContext(mock.patch.object(
            dash, "USAGE_CACHE_FILE", self.root / ".usage-cache.json"))
        dash._USAGE_CACHE["key"] = None

    def append(self, records):
        with self.session.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        dash._USAGE_CACHE["key"] = None

    def assistant(self, message_id="msg_other", model="claude-sonnet-4-5-20250929",
                  tokens=(10, 2, 0, 0), cache_1h=None, stamp="2026-10-25T02:10:00.000Z"):
        usage = {"input_tokens": tokens[0], "output_tokens": tokens[1],
                 "cache_read_input_tokens": tokens[2],
                 "cache_creation_input_tokens": tokens[3]}
        if cache_1h is not None:
            usage["cache_creation"] = {"ephemeral_1h_input_tokens": cache_1h,
                                       "ephemeral_5m_input_tokens": tokens[3] - cache_1h}
        return {"type": "assistant", "timestamp": stamp,
                "message": {"id": message_id, "model": model, "usage": usage}}

    def scan(self, pricing=None):
        return dash.scan_sessions(projects=self.projects, now=self.NOW, pricing=pricing)

    def opener(self, payload):
        body = io.BytesIO(json.dumps(payload).encode())
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=mock.Mock(read=lambda *_: body.read()))
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        return opener

    def test_model_ids_are_normalized_like_orca(self):
        cases = {
            "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
            "claude-3-5-sonnet-20241022": "claude-sonnet-3-5",
            "anthropic/claude-sonnet-4.5": "claude-sonnet-4-5",
            "CLAUDE-HAIKU-3-5": "claude-haiku-3-5",
            "claude-sonnet-4-5-1m": "claude-sonnet-4-5",
            "model_placeholder_m26": "claude-opus-4-6",
            "vendor/some-model-2025-09-29": "vendor/some-model",
            "vendor/model": "vendor/model",
        }
        for raw, expected in cases.items():
            self.assertEqual(dash.normalize_model_id(raw), expected, raw)
        for empty in (None, "", "   ", 7):
            self.assertIsNone(dash.normalize_model_id(empty))

    def test_pricing_lookup_normalizes_the_catalog_row(self):
        payload = {"data": [{"id": "anthropic/claude-sonnet-4.5",
                             "pricing": {"prompt": "0.000003", "completion": "0.000015",
                                         "input_cache_read": "0.0000003",
                                         "input_cache_write": "0.00000375",
                                         "input_cache_write_1h": "0.000006"}}]}
        cache = {"at": 0.0, "data": None}
        with mock.patch.object(dash.urllib.request, "build_opener",
                               return_value=self.opener(payload)):
            result = dash.model_pricing("sk-or-synthetic", "anthropic/claude-sonnet-4.5-20250929",
                                        cache=cache, now=10.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["normalized_model"], "claude-sonnet-4-5")
        self.assertAlmostEqual(result["cache_write_1h"], 0.000006)
        self.assertRegex(result["pricing_version"], r"^[0-9a-f]{12}$")
        self.assertTrue(result["fetched_at"].endswith("+00:00"))

    def test_an_unpriced_model_is_unknown_not_zero(self):
        self.append([self.assistant(tokens=(100, 10, 0, 0))])
        usage = self.scan(pricing={"ok": False, "error": "model_not_found"})
        self.assertIsNone(usage["cost_usd"])
        self.assertIsNone(usage["today_cost_usd"])
        cost = usage["cost"]
        self.assertEqual(cost["status"], "unknown")
        self.assertIsNone(cost["estimated_usd"])
        self.assertEqual(cost["buckets"]["estimated"]["tokens"], [0, 0, 0, 0])
        self.assertEqual(cost["buckets"]["unknown"]["tokens"], [100, 10, 0, 0])
        self.assertEqual(cost["buckets"]["included"]["tokens"], [0, 0, 0, 0])
        self.assertEqual(usage["sessions"][0]["cost_status"], "unknown")
        self.assertIsNone(usage["sessions"][0]["cost_usd"])

    def test_plan_usage_is_included_not_estimated(self):
        self.append([self.assistant(message_id="msg_011ABC", tokens=(10, 2, 0, 4))])
        usage = self.scan(pricing=self.RATES)
        self.assertEqual(usage["totals"]["plan"], [10, 2, 0, 4])
        self.assertEqual(usage["cost_usd"], 0.0)
        self.assertEqual(usage["cost"]["status"], "included")
        self.assertEqual(usage["cost"]["buckets"]["included"]["tokens"], [10, 2, 0, 4])
        self.assertEqual(usage["cost"]["buckets"]["estimated"]["tokens"], [0, 0, 0, 0])
        self.assertEqual(usage["sessions"][0]["cost_status"], "included")
        self.assertEqual(usage["sessions"][0]["cost_usd"], 0.0)

    def test_cache_write_ttl_split_is_separated_and_priced(self):
        self.append([self.assistant(tokens=(10, 2, 0, 100), cache_1h=60)])
        usage = self.scan(pricing=self.RATES)
        # The four compatible token axes stay untouched; the 1h share is a new axis.
        self.assertEqual(usage["totals"]["external"], [10, 2, 0, 100])
        self.assertEqual(usage["cache_write_1h"]["external"], 60)
        self.assertEqual(usage["today_cache_write_1h"]["external"], 60)
        row = usage["sessions"][0]
        self.assertEqual(row["cache_write_1h"], {"external": 60, "plan": 0})
        expected = 10 * 0.000003 + 2 * 0.000015 + 40 * 0.00000375 + 60 * 0.000006
        self.assertAlmostEqual(usage["cost_usd"], expected, places=9)
        self.assertAlmostEqual(row["cost_usd"], expected, places=9)
        self.assertEqual(usage["cost"]["cache_write_1h_rate"], "explicit")

    def test_cache_write_1h_falls_back_to_the_standard_rate(self):
        self.append([self.assistant(tokens=(0, 0, 0, 100), cache_1h=60)])
        pricing = dict(self.RATES)
        pricing.pop("cache_write_1h")
        usage = self.scan(pricing=pricing)
        self.assertAlmostEqual(usage["cost_usd"], 100 * pricing["cache_write"], places=9)
        self.assertEqual(usage["cost"]["cache_write_1h_rate"], "fallback")

    def test_cost_buckets_partition_the_totals(self):
        self.append([self.assistant(message_id="msg_other", tokens=(10, 2, 0, 4), cache_1h=4),
                     self.assistant(message_id="msg_011ABC", tokens=(5, 1, 2, 3), cache_1h=1)])
        usage = self.scan(pricing=self.RATES)
        cost = usage["cost"]
        self.assertEqual(cost["status"], "estimated")
        for index in range(4):
            self.assertEqual(cost["buckets"]["estimated"]["tokens"][index]
                             + cost["buckets"]["unknown"]["tokens"][index],
                             usage["totals"]["external"][index])
            self.assertEqual(cost["buckets"]["included"]["tokens"][index],
                             usage["totals"]["plan"][index])
        self.assertEqual(cost["buckets"]["estimated"]["cache_write_1h"]
                         + cost["buckets"]["included"]["cache_write_1h"],
                         usage["cache_write_1h"]["external"] + usage["cache_write_1h"]["plan"])
        self.assertEqual(cost["buckets"]["estimated"]["turns"]
                         + cost["buckets"]["included"]["turns"], 2)

    def test_a_malformed_ttl_split_is_ignored_safely(self):
        broken_text = self.assistant(message_id="msg_1", tokens=(1, 1, 0, 10))
        broken_text["message"]["usage"]["cache_creation"] = "nope"
        broken_value = self.assistant(message_id="msg_2", tokens=(1, 1, 0, 10))
        broken_value["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": "many"}
        over_limit = self.assistant(message_id="msg_3", tokens=(1, 1, 0, 10), cache_1h=999)
        self.append([broken_text, broken_value, over_limit])
        usage = self.scan(pricing=self.RATES)
        self.assertEqual(usage["totals"]["external"], [3, 3, 0, 30])
        # A split above the cache-write total is clamped to it, never exceeding it.
        self.assertEqual(usage["cache_write_1h"]["external"], 10)

    def test_same_rates_do_not_reuse_another_models_pricing_metadata(self):
        self.append([self.assistant()])
        first = {**self.RATES, "normalized_model": "vendor/first", "pricing_version": "version-1",
                 "fetched_at": "2026-10-25T01:00:00Z"}
        self.assertEqual(self.scan(pricing=first)["cost"]["normalized_model"], "vendor/first")
        second = {**first, "normalized_model": "vendor/second"}
        self.assertEqual(self.scan(pricing=second)["cost"]["normalized_model"], "vendor/second")
        refreshed = {**second, "fetched_at": "2026-10-25T02:00:00Z"}
        self.assertEqual(self.scan(pricing=refreshed)["cost"]["fetched_at"], refreshed["fetched_at"])

    def test_existing_payload_fields_are_unchanged_for_normal_data(self):
        self.append([self.assistant(message_id="msg_other",
                                    tokens=(1_000_000, 1_000_000, 1_000_000, 0)),
                     self.assistant(message_id="msg_011ABC", tokens=(5, 5, 5, 5))])
        pricing = {"ok": True, "prompt": 0.00000015, "completion": 0.0000006,
                   "cache_read": 0.000000003, "cache_write": 0.0000009}
        usage = self.scan(pricing=pricing)
        self.assertEqual(usage["totals"]["external"], [1_000_000, 1_000_000, 1_000_000, 0])
        self.assertEqual(len(usage["sessions"][0]["tokens"]["external"]), 4)
        self.assertEqual(set(usage["timeline"][-1]["external"]), {"turns", "tokens"})
        self.assertAlmostEqual(usage["cost_usd"], 0.15 + 0.6 + 0.003, places=6)
        self.assertAlmostEqual(usage["sessions"][0]["cost_usd"], 0.15 + 0.6 + 0.003, places=6)
        self.assertEqual(usage["sessions"][0]["cost_status"], "estimated")
        self.assertEqual(usage["cost"]["cache_write_1h_rate"], "fallback")

    def test_status_report_exposes_cost_provenance(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "parse_route_log", return_value=[]):
            report = dash.status_report(include_usage=True, offline=True)
        self.assertEqual(report["usage"]["cost"]["status"], "included")
        self.assertIn("cache_write_1h", report["usage"])


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


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "openclaude.config.yaml"
        self.store = dash.routeConfig.Store(self.path)
        self.store.write_provider("local", {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages",
                                            "LOCAL_LLM_MODEL": "fixture-local", "LOCAL_LLM_API_KEY": "private-api-key-fixture"})
        self.store.write_provider("routing", {"ROUTE_DEFAULT_PROVIDER": "local"})
        files = {name: {**spec, "path": self.path} for name, spec in dash.CONFIG_FILES.items()}
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", files))
        self.live = self.enterContext(mock.patch.object(dash, "_live_session", return_value=(None, None)))
        self.enterContext(mock.patch.object(dash, "parse_route_log", return_value=[]))
        self.cache = {"at": time.monotonic(), "data": {}, "error": None}
        self.enterContext(mock.patch.object(dash, "_SNAPSHOT", self.cache))
        for name in ("_http_json", "account_state", "model_pricing", "scan_sessions", "snapshot", "status_report",
                     "save_config", "save_default_provider"):
            self.enterContext(mock.patch.object(dash, name, side_effect=AssertionError("diagnostics must not call " + name)))

    def live_proxy(self, model="fixture-local", configuration=True):
        metrics = {"startedAt": "2026-10-28T00:00:00Z", "requests": 3, "failed": 0,
                   "retries": 0, "circuitOpens": 0}
        if configuration:
            metrics["configuration"] = {"provider": "local", "model": model, "planFallback": True}
        self.live.return_value = ({"provider": "local", "started": "2026-10-28T00:00:00Z"}, metrics)
        return metrics

    def test_read_only_report_distinguishes_settings_from_a_missing_session(self):
        before = self.path.read_bytes()
        report = dash.diagnostics_report()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["status"], "degraded")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["inference_performed"])
        self.assertEqual(report["configuration"]["next_provider"], "local")
        self.assertEqual(report["configuration"]["configured_model"], "fixture-local")
        self.assertFalse(report["session"]["active"])
        self.assertIsNone(report["session"]["loaded_model"])
        self.assertIn("session_not_running", [item["code"] for item in report["checks"]])
        self.assertIn("upstream_not_checked", [item["code"] for item in report["checks"]])
        self.assertEqual(self.path.read_bytes(), before)
        self.live.assert_called_once()
        public = json.dumps(report)
        self.assertNotIn("private-api-key-fixture", public)
        self.assertNotIn("127.0.0.1:11434", public)
        self.assertNotIn(str(self.root), public)

    def test_authenticated_loaded_model_and_unchecked_upstream_are_separate(self):
        self.live_proxy()
        report = dash.diagnostics_report()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["session"]["loaded_model"], "fixture-local")
        self.assertEqual(report["session"]["model_status"], "reported")
        self.assertTrue(report["session"]["plan_fallback"])
        upstream = next(item for item in report["checks"] if item["component"] == "upstream")
        self.assertEqual(upstream["status"], "unknown")
        self.assertFalse(report["inference_performed"])
        self.live.assert_called_once()

    def test_changed_settings_do_not_overwrite_the_loaded_model(self):
        self.live_proxy(model="previous-local-model")
        report = dash.diagnostics_report()
        self.assertEqual(report["configuration"]["configured_model"], "fixture-local")
        self.assertEqual(report["session"]["loaded_model"], "previous-local-model")
        self.assertEqual(report["status"], "degraded")
        self.assertIn("model_settings_changed", [item["code"] for item in report["checks"]])

    def test_old_proxy_metadata_is_unreported_instead_of_inferred_from_settings(self):
        self.live_proxy(configuration=False)
        report = dash.diagnostics_report()
        self.assertIsNone(report["session"]["loaded_model"])
        self.assertIsNone(report["session"]["plan_fallback"])
        self.assertEqual(report["session"]["model_status"], "unreported")
        self.assertIn("proxy_model_unreported", [item["code"] for item in report["checks"]])

    def test_model_paths_urls_tokens_and_unexpected_nested_fields_are_redacted(self):
        for value in ("https://user:pw@private.example/model", "C:/private/model.gguf", "/private/model",
                      "\\\\host\\models", "private-api-key-fixture", "sk-or-private", "bad\nmodel", "m" * 257):
            with self.subTest(value=value):
                model, state = dash._diagnostic_model(value, ["private-api-key-fixture"])
                self.assertIsNone(model)
                self.assertEqual(state, "redacted")
        metrics = self.live_proxy(model="C:/private/model.gguf")
        metrics["configuration"]["alternate"] = {"provider": "openrouter", "model": "https://private.example/model",
                                                   "key": "private-nested-token", "url": "https://private.example"}
        metrics["secret"] = "private-metrics-token"
        self.cache["error"] = {"message": "private-refresh-token C:/private/config.yaml"}
        report = dash.diagnostics_report()
        self.assertIsNone(report["session"]["loaded_model"])
        self.assertEqual(report["session"]["model_status"], "redacted")
        self.assertIsNone(report["session"]["alternate"]["model"])
        self.assertNotIn("private", json.dumps(report))
        self.assertTrue(report["snapshot"]["refresh_failed"])

    def test_invalid_config_returns_a_code_without_source_values(self):
        self.path.write_text('routing:\n  default_provider: "private-invalid-value"\n', encoding="utf-8")
        report = dash.diagnostics_report()
        self.assertEqual(report["status"], "error")
        self.assertIsNone(report["configuration"]["default_provider"])
        self.assertIn("routing_configuration_invalid", [item["code"] for item in report["checks"]])
        self.assertNotIn("private-invalid-value", json.dumps(report))

    def test_stale_snapshot_is_distinct_from_proxy_health(self):
        self.live_proxy()
        self.cache["at"] = time.monotonic() - 90
        report = dash.diagnostics_report()
        self.assertGreaterEqual(report["snapshot"]["age_seconds"], 90)
        self.assertIn("snapshot_stale", [item["code"] for item in report["checks"]])
        self.assertTrue(report["session"]["active"])

    def test_previous_session_route_is_not_marked_current_and_live_evidence_is_used(self):
        metrics = self.live_proxy()
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=5)
        self.live.return_value[0]["started"] = started.isoformat()
        metrics["startedAt"] = started.isoformat()
        old = {"ts": (started - timedelta(days=2)).isoformat(), "event": "route_selected",
               "provider": "plan", "model": "historical-model", "reason": "fallback"}
        with mock.patch.object(dash, "parse_route_log", return_value=[old]):
            report = dash.diagnostics_report()
            self.assertFalse(report["last_route"]["current_session"])
            self.assertIsNone(report["last_route"]["model"])
            metrics["last"] = {"ts": (now - timedelta(seconds=1)).isoformat(),
                               "route": "plan", "reason": "native_feature", "status": 200}
            report = dash.diagnostics_report()
            self.assertTrue(report["last_route"]["current_session"])
            self.assertEqual(report["last_route"]["timestamp"], metrics["last"]["ts"])
            self.assertIsNone(report["last_route"]["model"])

    def test_http_contract_is_read_only_redacted_and_rejects_foreign_hosts(self):
        import threading

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method="GET", headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                connection.request(method, "/api/diagnostics", headers=headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        status, report = request()
        self.assertEqual(status, 200)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(request(headers={"host": "foreign.invalid"}), (403, {"error": "host_rejected"}))
        self.assertEqual(request(method="POST"), (404, {"error": "not_found"}))
        with mock.patch.object(dash, "diagnostics_report", side_effect=ValueError("private-handler-error")):
            self.assertEqual(request(), (500, {"error": "diagnostics_failed"}))


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash, "_live_session", return_value=(None, None)))
        self.enterContext(mock.patch.object(dash, "read_settings", return_value={
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
        names = " ".join(endpoint for endpoint, _ in dash.HELP_SECTION["endpoints"])
        self.assertIn("/api/config", names)
        self.assertIn("/api/default-provider", names)
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
        self.assertIn("data-copy", dashboard)
        self.assertIn("insightsCard", dashboard)
        self.assertIn("function poll()", dashboard)
        self.assertNotIn("saveSettings", dashboard)
        self.assertIn("saveSettings", settings)
        self.assertNotIn("renderGrid", settings)
        for page in (dash.PAGE, dash.SETTINGS_PAGE):
            self.assertIn('const TOKEN = "__DASHBOARD_TOKEN__"', page)
            self.assertIn('href="/"', page)
            self.assertIn('href="/settings"', page)

    def test_settings_page_offers_one_service_select_and_dashboard_does_not(self):
        self.assertIn('id="provider-select"', dash.SETTINGS_PAGE)
        self.assertEqual(dash.SETTINGS_PAGE.count('id="provider-select"'), 1)
        self.assertNotIn('id="provider-select"', dash.PAGE)
        # one select, not a card per service side by side
        self.assertNotIn('class="card col-6" id="card-', self.scripts()["settings"])
        self.assertIn(".card[hidden]{display:none}", dash.SETTINGS_PAGE)

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

    def test_staleness_compares_content_not_timestamp(self):
        # A checkout that only touched the file (same bytes) is not stale.
        self.code.write_text("// fixture", encoding="utf-8")
        digest = dash.code_digest(self.code)
        older_mtime = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.code, (older_mtime, older_mtime))
        self.assertFalse(dash.proxy_stale({"proxy_hash": digest}, path=self.code))
        self.assertTrue(dash.proxy_stale({"proxy_hash": "000000000000"}, path=self.code))
        self.assertIsNone(dash.proxy_stale({"proxy_hash": digest}, path=self.root / "absent.mjs"))

    def test_start_marker_hash_is_parsed(self):
        line = ("2026-10-25T01:00:00Z --- proxy start code=2026-10-24T23:00:00.000Z hash=abcdef123456 ---\n")
        events = dash.parse_route_log(self.write_log(line), incremental=False)
        self.assertEqual(events[0]["event"], "proxy_start")
        self.assertEqual(events[0]["hash"], "abcdef123456")

    def write_log(self, line):
        path = self.root / ".route.log"
        path.write_text(line, encoding="utf-8")
        return path

    def test_stale_alert_carries_a_copyable_restart_command(self):
        routing = {"proxy_started_at": "2026-10-25T02:40:00Z", "proxy_code": None, "state": "plan",
                   "last": None, "short": {}, "counts": {}}
        with mock.patch.object(dash, "PROXY_FILE", self.code):
            alerts = dash.build_alerts(routing, dash._empty_usage(), {"ok": False}, {"ok": False})
        stale = [alert for alert in alerts if alert["title"] == "실행기 재시작 필요"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["command"], ".\\OpenClaude.ps1")

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
        # The record carries the pid's creation time, so a stop is only sent when the live
        # process still matches it; the mocked port keeps this independent of a real dashboard.
        self.pid_file.write_text(json.dumps({"pid": 4321, "port": 8787, "process_created": 111}),
                                 encoding="utf-8")
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)), \
             mock.patch.object(dash, "own_tree_pids", return_value=set()), \
             mock.patch.object(dash, "_port_open", return_value=True), \
             mock.patch.object(dash.os, "kill") as killer, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dash.main(["--stop"]), 0)
        killer.assert_called_once()
        self.assertEqual(killer.call_args.args, (4321, dash.signal.SIGTERM))
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
        self.path = self.root / "openclaude.config.yaml"
        self.files = {
            "openrouter": {"path": self.path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                           "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                           "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")},
            "local": {"path": self.path, "secret": {"LOCAL_LLM_API_KEY"},
                      "required": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL"),
                      "fields": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                                 "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL")},
        }
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", self.files))

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def put(self, provider, **values):
        """Store environment-style values for one service in the unified file."""
        keys = {name: key for key, name in dash.routeConfig.SECTIONS[provider]["keys"].items()}
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        self.write(dash.routeConfig.update_text(text, provider, {keys[name]: value for name, value in values.items()}))

    def put_openrouter(self, model="vendor/a", key="sk-or-v1-abcdefghijkl"):
        self.put("openrouter", OPENROUTER_MODEL=model, ANTHROPIC_AUTH_TOKEN=key)

    def test_default_selection_is_saved_without_changing_service_settings(self):
        self.put_openrouter()
        values = dash._store("openrouter").read_provider("openrouter")
        self.assertEqual(dash.save_default_provider("openrouter"), "openrouter")
        self.assertEqual(dash.config_snapshot()["default_provider"], "openrouter")
        self.assertEqual(dash._store("openrouter").read_provider("openrouter"), values)
        before = self.path.read_bytes()
        self.assertEqual(dash.save_default_provider("openrouter"), "openrouter")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(dash._store("openrouter").read_provider("openrouter"), values)

    def test_dashboard_saved_default_is_used_by_the_launcher(self):
        import openrouterAdvisor as route

        self.put_openrouter()
        self.put("local", LOCAL_LLM_URL="http://127.0.0.1:11434/v1/messages", LOCAL_LLM_MODEL="fixture")
        dash.save_default_provider("local")
        with mock.patch.object(route, "ROOT", self.root), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(route.configured_provider(), "local")

    def test_local_snapshots_skip_openrouter_and_do_not_reuse_its_priced_cache(self):
        self.put_openrouter()
        self.put("local", LOCAL_LLM_URL="http://127.0.0.1:11434/v1/messages", LOCAL_LLM_MODEL="fixture-local")
        with mock.patch.object(dash, "_live_session", return_value=(None, None)), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "proxy_metrics", return_value={"requests": 4, "failed": 4, "stale": False}), \
             mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()) as scan, \
             mock.patch.object(dash, "_USAGE_SNAP", {"at": 0, "data": None, "signature": None}), \
             mock.patch.object(dash, "model_pricing", return_value={"ok": True, "pricing_version": "fixture"}) as pricing, \
             mock.patch.object(dash, "account_state", return_value={"ok": True}) as account:
            dash.save_default_provider("openrouter")
            dash.snapshot()
            account.reset_mock()
            pricing.reset_mock()
            dash.save_default_provider("local")
            data = dash.snapshot()
            self.assertEqual(scan.call_count, 2, "changing services invalidates priced usage")
            self.assertIsNone(scan.call_args.kwargs["pricing"])
            self.assertEqual(data["model"], "fixture-local")
            self.assertEqual(data["account"]["error"], "not_applicable")
            self.assertTrue(data["proxy_metrics"]["stale"], "orphaned counters are historical even if recently written")
            self.assertNotIn("실패 응답 누적", [item["title"] for item in data["insights"]])
            self.assertNotIn("account_error", [item.get("code") for item in data["alerts"]])
            account.assert_not_called()
            pricing.assert_not_called()

    def test_real_proxy_handoff_settings_and_dashboard_follow_the_same_service(self):
        import queue
        import shutil
        import subprocess
        import threading
        import openrouterAdvisor as route

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        module = Path(dash.__file__).with_name("claudeRouteProxy.mjs").as_uri()
        script = ("import {createProxy} from " + json.dumps(module) + ";"
                  "const proxy=createProxy({provider:'local',localUrl:'http://127.0.0.1:1/v1/messages',"
                  "localModel:'fixture-local',nonce:'fixture-session',metricsPath:null,"
                  "fetchImpl:async()=>{throw new Error('Unexpected upstream request')}});"
                  "proxy.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({port:proxy.address().port})));"
                  "process.stdin.resume();")
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC"}}
        proxy = subprocess.Popen([node, "--input-type=module", "-e", script], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8")
        def stop_proxy():
            if proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)
            for stream in (proxy.stdin, proxy.stdout, proxy.stderr):
                stream.close()
        self.addCleanup(stop_proxy)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(proxy.stdout.readline()), daemon=True).start()
        port = json.loads(ready.get(timeout=10))["port"]
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        handoff = self.root / ".session-handoff.json"
        log = self.root / ".route.log"
        log.write_text(route_line(datetime.now(timezone.utc).isoformat(),
                                  {"event": "route_selected", "provider": "local", "reason": "replacement",
                                   "model": "fixture-local"}), encoding="utf-8")
        self.put_openrouter()
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.object(route, "HANDOFF_FILE", handoff))
        self.enterContext(mock.patch.object(dash, "SESSION_HANDOFF_FILE", handoff))
        self.enterContext(mock.patch.object(dash, "ROUTE_LOG", log))
        self.enterContext(mock.patch.object(dash, "usage_snapshot", return_value=dash._empty_usage()))
        self.enterContext(mock.patch.object(dash, "account_state", side_effect=AssertionError("Local must not call OpenRouter")))
        self.enterContext(mock.patch.object(dash, "model_pricing", side_effect=AssertionError("Local must not use OpenRouter pricing")))
        route.write_handoff(port=port, nonce="fixture-session", proxy_pid=proxy.pid,
                            provider="local", cli_pid=os.getpid())
        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, payload=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                connection.request(method, path, None if payload is None else json.dumps(payload),
                                   {"x-dashboard-token": dash.TOKEN, "content-type": "application/json"})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, 200, data)
                return data
            finally:
                connection.close()
        request("POST", "/api/config", {"provider": "local", "values": {
            "LOCAL_LLM_URL": "http://127.0.0.1:1/v1/messages", "LOCAL_LLM_MODEL": "fixture-local"}})
        request("POST", "/api/default-provider", {"provider": "local"})
        self.assertEqual(route.configured_provider(), "local")
        saved = request("GET", "/api/config")
        self.assertEqual(saved["default_provider"], "local")
        self.assertEqual(saved["active_session"]["provider"], "local")
        snapshot = dash.snapshot()
        self.assertEqual(snapshot["model"], "fixture-local")
        self.assertEqual(snapshot["proxy_metrics"]["requests"], 0)
        self.assertFalse(snapshot["proxy_metrics"]["stale"])
        self.assertIn("Local LLM", snapshot["summary"])
        diagnostics = request("GET", "/api/diagnostics")
        self.assertEqual(diagnostics["schema_version"], 1)
        self.assertEqual(diagnostics["session"]["loaded_model"], "fixture-local")
        self.assertEqual(diagnostics["configuration"]["next_provider"], "local")
        self.assertFalse(diagnostics["inference_performed"])
        self.assertEqual(diagnostics["metrics"]["requests"], 0)
        request("POST", "/api/default-provider", {"provider": "openrouter"})
        self.assertEqual(route.configured_provider(), "openrouter")
        report = request("GET", "/api/status")
        self.assertEqual(report["active_session"]["provider"], "local")
        self.assertEqual(report["config"]["model"], "fixture-local", "changing the next launch keeps the live session's context")
        self.assertNotIn("fixture-session", json.dumps(report))
        diagnostics = request("GET", "/api/diagnostics")
        self.assertEqual(diagnostics["configuration"]["next_provider"], "openrouter")
        self.assertEqual(diagnostics["session"]["provider"], "local")
        self.assertEqual(diagnostics["session"]["loaded_model"], "fixture-local")
        self.assertNotIn("fixture-session", json.dumps(diagnostics))
        stop_proxy()
        self.assertIsNone(dash.active_session(), "a leftover handoff file does not prove liveness")

    def test_rejected_default_does_not_change_the_file_or_backup(self):
        self.put_openrouter()
        dash.save_default_provider("openrouter")
        before = self.path.read_bytes()
        backup = self.path.with_name(self.path.name + ".bak").read_bytes()
        for provider in (None, [], {}, "cli", "unknown", "local"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                dash.save_default_provider(provider)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.path.with_name(self.path.name + ".bak").read_bytes(), backup)

    def test_default_requires_the_selected_services_key(self):
        self.put("openrouter", OPENROUTER_MODEL="vendor/a")
        with self.assertRaisesRegex(ValueError, "default_provider_not_configured"):
            dash.save_default_provider("openrouter")

    def test_auto_can_recover_an_invalid_preference_without_a_ready_service(self):
        self.write('routing:\n  default_provider: "invalid"\n')
        snapshot = dash.config_snapshot()
        self.assertIsNone(snapshot["default_provider"])
        self.assertEqual(snapshot["default_provider_error"], "invalid_default_provider")
        dash.save_default_provider("auto")
        self.assertEqual(dash.config_snapshot()["default_provider"], "auto")
        self.assertIsNone(dash.config_snapshot()["default_provider_error"])

    def test_default_write_failure_keeps_the_current_file(self):
        self.put_openrouter()
        before = self.path.read_bytes()
        with mock.patch.object(dash.routeConfig.Store, "_atomic_write", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                dash.save_default_provider("openrouter")
        self.assertEqual(self.path.read_bytes(), before)

    def test_default_endpoint_checks_authorization_origin_and_payload_before_saving(self):
        import threading

        self.put_openrouter()
        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cases = [
                ({}, {"provider": "openrouter"}, 403, "token_required"),
                ({"x-dashboard-token": dash.TOKEN, "origin": "https://foreign.invalid"},
                 {"provider": "openrouter"}, 403, "origin_rejected"),
                ({"x-dashboard-token": dash.TOKEN}, {"provider": []}, 400, "invalid_default_provider"),
                ({"x-dashboard-token": dash.TOKEN}, {"provider": "cli"}, 400, "default_provider_not_configured"),
                ({"x-dashboard-token": dash.TOKEN}, {"provider": "openrouter"}, 200, None),
            ]
            for headers, payload, status, error in cases:
                with self.subTest(payload=payload, status=status):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    try:
                        connection.request("POST", "/api/default-provider", json.dumps(payload), headers)
                        response = connection.getresponse()
                        data = json.loads(response.read())
                        self.assertEqual(response.status, status)
                        self.assertEqual(data.get("error"), error)
                    finally:
                        connection.close()
            self.assertEqual(dash.config_snapshot()["default_provider"], "openrouter")
        finally:
            server.shutdown()
            server.server_close()

    def test_save_validates_latest_candidate_inside_store_transaction(self):
        self.put("local", LOCAL_LLM_URL="http://127.0.0.1:1111/v1/messages",
                 LOCAL_LLM_MODEL="fixture", LOCAL_LLM_API_KEY="fixture-secret")
        original_write = dash.routeConfig.Store.write_provider
        interleaved = False
        committed = {}

        def concurrent_write(store, provider, updates, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                # Another valid save commits after A's preflight but before A's transaction.
                original_write(store, "local", {"LOCAL_LLM_MODELS_URL": "http://127.0.0.1:1111/v1/models"})
                committed["file"] = self.path.read_bytes()
                committed["backup"] = self.path.with_name(self.path.name + ".bak").read_bytes()
            return original_write(store, provider, updates, **kwargs)

        with mock.patch.object(dash.routeConfig.Store, "write_provider", concurrent_write):
            with self.assertRaisesRegex(ValueError, "invalid_models_url"):
                dash.save_config("local", {"LOCAL_LLM_URL": "http://127.0.0.1:2222/v1/messages",
                                           "LOCAL_LLM_API_KEY": ""})
        stored = dash._store("local").read_provider("local")
        self.assertEqual(stored["LOCAL_LLM_URL"], "http://127.0.0.1:1111/v1/messages")
        self.assertEqual(stored["LOCAL_LLM_API_KEY"], "fixture-secret")
        dash._validate_local(stored)
        backup = self.path.with_name(self.path.name + ".bak")
        # Rejected validation changes neither the latest config nor its existing backup.
        self.assertEqual(self.path.read_bytes(), committed["file"])
        self.assertEqual(backup.read_bytes(), committed["backup"])

    def test_snapshot_masks_secrets_and_reports_which_are_set(self):
        self.put_openrouter("vendor/model")
        snapshot = dash.config_snapshot()
        entries = snapshot["openrouter"]["fields"]
        self.assertTrue(entries["ANTHROPIC_AUTH_TOKEN"]["secret"])
        self.assertTrue(entries["ANTHROPIC_AUTH_TOKEN"]["set"])
        self.assertEqual(entries["ANTHROPIC_AUTH_TOKEN"]["value"], "")
        self.assertIn("…", entries["ANTHROPIC_AUTH_TOKEN"]["preview"])
        self.assertNotIn("abcdefghijkl", json.dumps(snapshot))
        self.assertEqual(entries["OPENROUTER_MODEL"]["value"], "vendor/model")
        self.assertEqual((snapshot["openrouter"]["source"], snapshot["openrouter"]["error"]), ("yaml", None))
        self.assertTrue(snapshot["openrouter"]["exists"])

    def test_save_keeps_comments_other_sections_and_the_stored_secret(self):
        self.write('# keep me\nopenrouter:\n  api_key: "sk-or-v1-abcdefghijkl"\n  model: "vendor/old"\n'
                   '# about local\nlocal:\n  model: "llama"\nfuture:\n  kind: codex\n')
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/new", "ANTHROPIC_AUTH_TOKEN": ""})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# keep me", text)
        self.assertIn("# about local", text)
        self.assertIn('local:\n  model: "llama"', text.replace("\r\n", "\n"))
        self.assertIn("future:\n  kind: codex", text.replace("\r\n", "\n"))
        self.assertEqual(dash.read_settings("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/new", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        self.assertTrue((self.root / "openclaude.config.yaml.bak").is_file())

    def test_save_writes_a_new_secret_when_provided(self):
        self.put_openrouter("vendor/a")
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/a", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-zzzzzzzzzzzz"})
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(dash.read_settings("openrouter")["ANTHROPIC_AUTH_TOKEN"], "sk-or-v1-zzzzzzzzzzzz")
        self.assertEqual(text.count("api_key:"), 1)

    def test_saving_one_service_leaves_the_other_untouched(self):
        self.put("local", LOCAL_LLM_URL="http://127.0.0.1:11434/v1/messages", LOCAL_LLM_MODEL="llama")
        self.put_openrouter()
        before = dash.read_settings("local")
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/b"})
        self.assertEqual(dash.read_settings("local"), before)
        self.assertEqual(dash.read_settings("openrouter")["OPENROUTER_MODEL"], "vendor/b")

    def test_invalid_values_are_rejected_before_writing(self):
        self.put_openrouter("vendor/a")
        before = self.path.read_bytes()
        for values, reason in [
            ({"OPENROUTER_MODEL": "REPLACE_WITH_MODEL", "ANTHROPIC_AUTH_TOKEN": ""}, "invalid_model"),
            ({"OPENROUTER_MODEL": "vendor/a", "ANTHROPIC_AUTH_TOKEN": "nope"}, "invalid_key"),
            ({"OPENROUTER_MODEL": "", "ANTHROPIC_AUTH_TOKEN": ""}, "missing_openrouter_model"),
        ]:
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError) as caught:
                    dash.save_config("openrouter", values)
                self.assertEqual(str(caught.exception), reason)
        self.assertEqual(self.path.read_bytes(), before)

    def test_control_and_line_separator_characters_are_refused_before_writing(self):
        self.put("local", LOCAL_LLM_URL="http://127.0.0.1:11434/v1/messages", LOCAL_LLM_MODEL="llama")
        before = self.path.read_bytes()
        for bad in ("a b", "a b", "a\u0085b", "a\x00b", "a\nb"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError) as caught:
                    dash.save_config("local", {"LOCAL_LLM_MODEL": bad})
                self.assertEqual(str(caught.exception), "invalid_value")
        self.assertEqual(self.path.read_bytes(), before)

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
        self.put_openrouter("vendor/a")
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

    def test_local_inference_requires_selected_model_complete_fixed_reply_and_enough_budget(self):
        values = {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODEL": "fixture-local"}
        reply = {"model": "fixture-local", "stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}]}
        cases = [(reply, True, "ready"), ({**reply, "model": "other"}, False, "wrong_model"),
                 ({**reply, "stop_reason": "max_tokens"}, False, "invalid_response"),
                 ({**reply, "content": []}, False, "empty_reply"),
                 ({**reply, "content": [{"type": "text", "text": "O"}]}, False, "invalid_response")]
        for response, ok, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(dash, "_http_json", return_value=(200, response)) as request:
                result = dash.run_connection_test("local", "inference", values)
            self.assertEqual((result["ok"], result["reason"]), (ok, reason))
            self.assertEqual(request.call_args.args[2]["max_tokens"], 512)

    def test_connection_test_accepts_unsaved_values(self):
        with mock.patch.object(dash, "_http_json", return_value=(200, {"data": [{"id": "vendor/b"}]})):
            result = dash.run_connection_test(
                "openrouter", "metadata",
                {"OPENROUTER_MODEL": "vendor/b", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-unsavedkey1234"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "model_found")
        self.assertNotIn("unsavedkey", json.dumps(result))
        # A blank secret falls back to the stored value, so a saved key still works.
        self.put_openrouter("vendor/old", "sk-or-v1-storedkey1234")
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
        example = self.root / "openclaude.config.example.yaml"
        example.write_text('# shipped comment\nopenrouter:\n  model: ""\n  api_key: ""\n', encoding="utf-8")
        self.assertFalse(self.path.exists())
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/a",
                                        "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# shipped comment", text)
        self.assertEqual(dash.read_settings("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/a", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})

    def test_connection_test_missing_configuration_and_http_errors(self):
        self.put("openrouter", OPENROUTER_MODEL="vendor/a")
        self.assertEqual(dash.run_connection_test("openrouter")["reason"], "configuration_missing")
        self.put_openrouter("vendor/a")
        import urllib.error
        with mock.patch.object(dash, "_http_json", side_effect=urllib.error.HTTPError(
                "u", 401, "denied", {}, None)):
            self.assertEqual(dash.run_connection_test("openrouter")["status"], 401)
        with mock.patch.object(dash, "_http_json", side_effect=dash.urllib.error.URLError("down")):
            self.assertEqual(dash.run_connection_test("openrouter")["reason"], "connection_failed")

    def test_legacy_env_files_are_shown_and_migrated_on_the_first_save(self):
        legacy = self.root / ".env.openrouter"
        legacy.write_text("ANTHROPIC_AUTH_TOKEN=sk-or-v1-legacykey123\nOPENROUTER_MODEL=vendor/legacy\n", encoding="utf-8")
        (self.root / ".env.local-llm").write_text("LOCAL_LLM_MODEL=llama\n", encoding="utf-8")
        before = legacy.read_bytes()
        snapshot = dash.config_snapshot()
        self.assertEqual(snapshot["openrouter"]["source"], "legacy")
        self.assertEqual(snapshot["openrouter"]["fields"]["OPENROUTER_MODEL"]["value"], "vendor/legacy")
        self.assertNotIn("legacykey", json.dumps(snapshot))
        self.assertFalse(self.path.exists())
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/migrated"})
        self.assertEqual(dash.config_snapshot()["openrouter"]["source"], "yaml")
        self.assertEqual(dash.read_settings("openrouter"),
                         {"OPENROUTER_MODEL": "vendor/migrated", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-legacykey123"})
        self.assertEqual(dash.read_settings("local")["LOCAL_LLM_MODEL"], "llama")  # moved over too
        self.assertEqual(legacy.read_bytes(), before)  # the deprecated file is never touched

    def test_a_broken_settings_file_is_reported_and_never_overwritten(self):
        broken = 'openrouter:\n  model: "vendor/a"\n  bogus: 1\nlocal:\n  model: "llama"\n'
        self.write(broken)
        snapshot = dash.config_snapshot()  # must not raise
        self.assertEqual(snapshot["openrouter"]["error"], {"code": "unknown_key", "line": 3})
        self.assertIsNone(snapshot["local"]["error"])
        self.assertNotIn("vendor/a", json.dumps(snapshot["openrouter"]["error"]))
        with self.assertRaises(dash.routeConfig.ConfigError) as caught:
            dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/b", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        self.assertEqual(caught.exception.code, "unknown_key")
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)
        # the other service is not blocked by it
        dash.save_config("local", {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODEL": "llama2"})
        self.assertEqual(dash.read_settings("local")["LOCAL_LLM_MODEL"], "llama2")

    def test_an_undecodable_settings_file_is_reported_not_rewritten(self):
        self.path.write_bytes(b'openrouter:\n  model: "\xff"\n')
        before = self.path.read_bytes()
        snapshot = dash.config_snapshot()
        self.assertEqual(snapshot["openrouter"]["error"]["code"], "config_not_utf8")
        with self.assertRaises(dash.routeConfig.ConfigError):
            dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/b", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abcdefghijkl"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "openclaude.config.yaml.bak").exists())


class RouteTailTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / ".route.log"
        self.enterContext(mock.patch.object(dash, "_ROUTE_TAIL",
                                            {"path": None, "events": [], "offset": 0}))

    def write(self, lines, mode="w"):
        with self.path.open(mode, encoding="utf-8") as handle:
            handle.write("".join(lines))

    def test_only_appended_lines_are_parsed(self):
        self.write([route_line("2026-01-01T00:00:00+00:00",
                              {"event": "route_selected", "provider": "openrouter", "reason": "replacement"}),
                    route_line("2026-01-01T00:00:01+00:00",
                               {"event": "route_selected", "provider": "plan", "reason": "native_feature"})])
        first = dash.parse_route_log(self.path)
        self.assertEqual(len(first), 2)
        self.write([route_line("2026-01-01T00:00:02+00:00",
                               {"event": "route_selected", "provider": "openrouter", "reason": "replacement"})],
                   mode="a")
        second = dash.parse_route_log(self.path)
        self.assertEqual(len(second), 3)
        self.assertEqual(second[-1]["ts"], "2026-01-01T00:00:02+00:00")
        self.assertEqual(dash.parse_route_log(self.path), second)  # unchanged file: no growth

    def test_truncation_starts_over(self):
        self.write([route_line("2026-01-01T00:00:00+00:00", {"event": "route_selected", "provider": "plan"})])
        self.assertEqual(len(dash.parse_route_log(self.path)), 1)
        self.write([route_line("2026-01-01T00:00:05+00:00", {"event": "route_selected", "provider": "openrouter"})])
        events = dash.parse_route_log(self.path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["provider"], "openrouter")

    def test_partial_trailing_line_waits_for_its_newline(self):
        self.write([route_line("2026-01-01T00:00:00+00:00", {"event": "route_selected", "provider": "plan"})])
        dash.parse_route_log(self.path)
        self.write(['2026-01-01T00:00:01+00:00 {"event": "route_sel'], mode="a")
        self.assertEqual(len(dash.parse_route_log(self.path)), 1)
        self.write(['ected", "provider": "openrouter"}\n'], mode="a")
        events = dash.parse_route_log(self.path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["provider"], "openrouter")

    def test_full_read_ignores_the_parked_offset(self):
        self.write([route_line("2026-01-01T00:00:00+00:00", {"event": "route_selected", "provider": "plan"})])
        dash.parse_route_log(self.path)
        self.assertEqual(len(dash.parse_route_log(self.path, incremental=False)), 1)

    def test_limit_keeps_the_tail(self):
        lines = [route_line(f"2026-01-01T00:00:{index:02d}+00:00",
                            {"event": "route_selected", "provider": "openrouter"}) for index in range(30)]
        self.write(lines)
        events = dash.parse_route_log(self.path, limit=5)
        self.assertEqual(len(events), 5)
        self.assertEqual(events[-1]["ts"], "2026-01-01T00:00:29+00:00")


class SnapshotReuseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash, "_USAGE_SNAP",
                                            {"signature": None, "at": 0.0, "data": None}))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        usage = dash._empty_usage()
        usage["totals"]["external"] = [10, 2, 0, 0]
        self.usage = usage

    def test_unchanged_signature_skips_the_transcript_walk(self):
        with mock.patch.object(dash, "scan_sessions", return_value=self.usage) as scan:
            first = dash.usage_snapshot(signature=("2026-10-25T01:00:00Z", 3, 200))
            second = dash.usage_snapshot(signature=("2026-10-25T01:00:00Z", 3, 200))
        scan.assert_called_once()
        self.assertIs(first, second)

    def test_new_routing_event_forces_a_rescan(self):
        with mock.patch.object(dash, "scan_sessions", return_value=self.usage) as scan:
            dash.usage_snapshot(signature=("2026-10-25T01:00:00Z", 3, 200))
            dash.usage_snapshot(signature=("2026-10-25T01:05:00Z", 4, 200))
        self.assertEqual(scan.call_count, 2)

    def test_missing_signature_always_scans(self):
        with mock.patch.object(dash, "scan_sessions", return_value=self.usage) as scan:
            dash.usage_snapshot(signature=None)
            dash.usage_snapshot(signature=None)
        self.assertEqual(scan.call_count, 2)

    def test_expired_cache_rescans(self):
        with mock.patch.object(dash, "scan_sessions", return_value=self.usage) as scan:
            dash.usage_snapshot(signature=("t", 1, 200), ttl=0)
            dash.usage_snapshot(signature=("t", 1, 200), ttl=0)
        self.assertEqual(scan.call_count, 2)

    def test_routing_signature_tracks_the_last_event(self):
        routing = {"last": {"ts": "2026-10-25T02:00:00Z", "status": 200}, "recent": [{"ts": "x"}] * 4}
        self.assertEqual(dash.routing_signature(routing)[:2], ("2026-10-25T02:00:00Z", 4))
        self.assertIsNone(dash.routing_signature({})[0])

    def test_transcript_walk_finds_nested_files(self):
        projects = self.root / "projects"
        (projects / "a" / "b").mkdir(parents=True)
        (projects / "top.jsonl").write_text("{}\n", encoding="utf-8")
        (projects / "a" / "b" / "deep.jsonl").write_text("{}\n", encoding="utf-8")
        (projects / "a" / "notes.txt").write_text("x", encoding="utf-8")
        found = {path.name for path, _mtime, _size in dash._transcript_files(projects)}
        self.assertEqual(found, {"top.jsonl", "deep.jsonl"})


class ProxyMetricsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash, "_live_session", return_value=(None, None)))
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


class HttpRedirectTests(unittest.TestCase):
    def test_authenticated_requests_never_follow_redirects(self):
        import urllib.error
        import urllib.request

        for status, method in ((302, "GET"), (303, "POST"), (307, "POST"), (308, "GET")):
            with self.subTest(status=status, method=method):
                build_opener = urllib.request.build_opener
                seen = []

                class RedirectSource(urllib.request.BaseHandler):
                    handler_order = 100

                    def https_open(self, request):
                        seen.append(request.full_url)
                        if len(seen) > 1:
                            raise AssertionError("redirect target received credentials")
                        headers = {"location": "https://other.invalid/collect"}
                        return self.parent.error("https", request, io.BytesIO(b""),
                                                 status, "redirect", headers)

                with mock.patch.object(urllib.request, "build_opener",
                                       side_effect=lambda *handlers: build_opener(*handlers, RedirectSource())):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        dash._http_json("https://provider.invalid/v1/models",
                                        {"authorization": "Bearer fixture-secret"},
                                        body={} if method == "POST" else None)
                    self.assertEqual(raised.exception.code, status)
                    raised.exception.close()
                self.assertEqual(seen, ["https://provider.invalid/v1/models"])

class ModelCatalogTests(unittest.TestCase):
    def test_same_model_id_is_listed_once_with_the_first_rows_metadata(self):
        rows = dash._parse_models({"data": [
            {"id": " vendor/model-a ", "name": "First model", "context_length": 1000},
            {"id": "vendor/model-a", "name": "Repeated model", "context_length": 2000},
            {"id": "vendor/model-b", "name": "Other model"},
        ]})
        self.assertEqual([row["id"] for row in rows], ["vendor/model-a", "vendor/model-b"])
        self.assertEqual(rows[0]["name"], "First model")
        self.assertEqual(rows[0]["context_length"], 1000)

    def test_local_model_name_duplicates_are_removed_without_merging_different_ids(self):
        rows = dash._parse_models({"models": [
            {"name": "local-model"}, {"model": " local-model "},
            {"name": "Local-Model"}, {"name": "local-model:latest"},
        ]})
        self.assertEqual([row["id"] for row in rows], ["Local-Model", "local-model", "local-model:latest"])

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = {
            "openrouter": {"path": self.root / "openclaude.config.yaml", "secret": {"ANTHROPIC_AUTH_TOKEN"},
                           "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                           "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")},
            "local": {"path": self.root / "openclaude.config.yaml", "secret": {"LOCAL_LLM_API_KEY"},
                      "required": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL"),
                      "fields": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                                 "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL")},
        }
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", self.files))
        self.enterContext(mock.patch.object(dash, "_MODELS_CACHE", {}))
        (self.root / "openclaude.config.yaml").write_text(
            'openrouter:\n  api_key: "sk-or-v1-abcdefghijkl"\n  model: "vendor/a"\n', encoding="utf-8")

    def test_openrouter_list_is_parsed_with_pricing_and_context(self):
        payload = {"data": [
            {"id": "vendor/b", "name": "Vendor B", "context_length": 131072,
             "pricing": {"prompt": "0.0000005", "completion": "0.0000015"}},
            {"id": "vendor/a", "name": "Vendor A"},
        ]}
        with mock.patch.object(dash, "_http_json", return_value=(200, payload)) as request:
            catalog = dash.model_catalog("openrouter")
        self.assertTrue(catalog["ok"])
        self.assertEqual([row["id"] for row in catalog["models"]], ["vendor/a", "vendor/b"])
        self.assertEqual(catalog["models"][1]["context_length"], 131072)
        self.assertEqual(catalog["models"][1]["prompt_usd_per_m"], 0.5)
        self.assertEqual(catalog["models"][1]["completion_usd_per_m"], 1.5)
        self.assertNotIn("sk-or", json.dumps(catalog))
        self.assertTrue(request.call_args.args[1]["authorization"].startswith("Bearer sk-or-"))

    def test_local_endpoint_is_derived_from_the_messages_url(self):
        url, _ = dash._models_endpoint("local", {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages"})
        self.assertEqual(url, "http://127.0.0.1:11434/v1/models")
        url, _ = dash._models_endpoint("local", {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages",
                                                 "LOCAL_LLM_MODELS_URL": "http://127.0.0.1:11434/api/tags"})
        self.assertEqual(url, "http://127.0.0.1:11434/api/tags")
        with self.assertRaises(ValueError):
            dash._models_endpoint("local", {"LOCAL_LLM_URL": "not a url"})

    def test_ollama_style_list_is_accepted(self):
        settings = self.root / "openclaude.config.yaml"
        settings.write_text(settings.read_text(encoding="utf-8")
                            + 'local:\n  url: "http://127.0.0.1:11434/v1/messages"\n  model: "llama3:8b"\n',
                            encoding="utf-8")
        with mock.patch.object(dash, "_http_json", return_value=(200, {"models": [{"name": "llama3:8b"}]})):
            catalog = dash.model_catalog("local")
        self.assertEqual(catalog["models"][0]["id"], "llama3:8b")

    def test_results_are_cached_then_refreshed(self):
        payload = {"data": [{"id": "vendor/a"}]}
        with mock.patch.object(dash, "_http_json", return_value=(200, payload)) as request:
            first = dash.model_catalog("openrouter", now=1000.0)
            second = dash.model_catalog("openrouter", now=1010.0)
            third = dash.model_catalog("openrouter", refresh=True, now=1011.0)
        self.assertFalse(first.get("cached"))
        self.assertTrue(second["cached"])
        self.assertFalse(third.get("cached"))
        self.assertEqual(request.call_count, 2)

    def test_http_errors_become_reasons_without_leaking_the_key(self):
        import urllib.error
        with mock.patch.object(dash, "_http_json",
                               side_effect=urllib.error.HTTPError("u", 401, "denied", {}, None)):
            catalog = dash.model_catalog("openrouter")
        self.assertFalse(catalog["ok"])
        self.assertEqual(catalog["error"], "authentication_failed")
        self.assertEqual(catalog["status"], 401)
        self.assertNotIn("sk-or", json.dumps(catalog))

    def test_connection_check_closes_redirect_response(self):
        import urllib.error
        body = io.BytesIO(b"credential must not be returned")
        error = urllib.error.HTTPError("https://provider.invalid", 303, "redirect", {}, body)
        values = {"ANTHROPIC_AUTH_TOKEN": "sk-or-fixture", "OPENROUTER_MODEL": "vendor/a"}
        with mock.patch.object(dash, "_http_json", side_effect=error):
            result = dash.run_connection_test("openrouter", mode="inference", values=values)
        self.assertTrue(body.closed)
        self.assertEqual((result["reason"], result["status"]), ("http_error", 303))
        self.assertNotIn("credential", json.dumps(result))

    def test_catalog_closes_redirect_response_and_returns_sanitized_error(self):
        import urllib.error
        body = io.BytesIO(b"credential must not be returned")
        error = urllib.error.HTTPError("https://provider.invalid", 302, "redirect", {}, body)
        with mock.patch.object(dash, "_http_json", side_effect=error):
            catalog = dash.model_catalog("openrouter")
        self.assertTrue(body.closed)
        self.assertEqual((catalog["error"], catalog["status"]), ("http_error", 302))
        self.assertNotIn("credential", json.dumps(catalog))

    def test_empty_catalog_is_reported(self):
        with mock.patch.object(dash, "_http_json", return_value=(200, {"data": []})):
            catalog = dash.model_catalog("openrouter")
        self.assertFalse(catalog["ok"])
        self.assertEqual(catalog["error"], "empty_catalog")

    def test_unsupported_methods_answer_json_instead_of_an_html_page(self):
        import threading

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    connection.request(method, "/api/status")
                    response = connection.getresponse()
                    body = response.read()
                finally:
                    connection.close()
                self.assertEqual(response.status, 405, method)
                self.assertEqual(json.loads(body), {"error": "method_not_allowed"})
        finally:
            server.shutdown()
            server.server_close()

    def test_a_foreign_host_header_is_rejected(self):
        import socket as socket_module
        import threading

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with socket_module.create_connection(("127.0.0.1", port), timeout=10) as connection:
                connection.sendall(b"GET /api/status HTTP/1.1\r\nHost: evil.example:8787\r\n"
                                   b"Connection: close\r\n\r\n")
                chunks = []
                while True:
                    piece = connection.recv(4096)
                    if not piece:
                        break
                    chunks.append(piece)
            reply = b"".join(chunks).decode("utf-8", "replace")
            self.assertIn("403", reply.splitlines()[0])
            self.assertIn("host_rejected", reply)
        finally:
            server.shutdown()
            server.server_close()

    def test_security_headers_are_present(self):
        import threading
        import urllib.request

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            # This class stores a fake key; the status report must not call out to OpenRouter with it.
            with mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "offline"}), \
                    opener.open(f"http://127.0.0.1:{port}/api/status", timeout=15) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
                self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")
                self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        finally:
            server.shutdown()
            server.server_close()

    def test_negative_content_length_is_rejected(self):
        import socket as socket_module
        import threading

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with socket_module.create_connection(("127.0.0.1", port), timeout=10) as connection:
                connection.sendall(
                    b"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    + b"x-dashboard-token: " + dash.TOKEN.encode() + b"\r\n"
                    + b"content-type: application/json\r\nContent-Length: -1\r\nConnection: close\r\n\r\n")
                chunks = []
                while True:
                    piece = connection.recv(2048)
                    if not piece:
                        break
                    chunks.append(piece)
                reply = b"".join(chunks).decode("utf-8", "replace")
            self.assertIn("400", reply.splitlines()[0])
            self.assertIn("invalid_length", reply)
        finally:
            server.shutdown()
            server.server_close()

    def test_models_endpoint_requires_the_token(self):
        import threading
        import urllib.error
        import urllib.request

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            base = f"http://127.0.0.1:{port}/api/models?provider=openrouter"
            with self.assertRaises(urllib.error.HTTPError) as denied:
                opener.open(base, timeout=10)
            denied.exception.close()
            self.assertEqual(denied.exception.code, 403)
            with mock.patch.object(dash, "model_catalog", return_value={"ok": True, "models": []}):
                request = urllib.request.Request(base, headers={"x-dashboard-token": dash.TOKEN})
                with opener.open(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["ok"], True)
        finally:
            server.shutdown()
            server.server_close()


class InsightTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 12, 0)

    def routing(self, **overrides):
        routing = {"state": "external", "last": {"age_seconds": 10},
                   "short": {"minutes": 10, "counts": {"external": 10, "plan": 0},
                             "plan_reasons": [], "failures": [], "circuit_opens": 0},
                   "counts": {"external": 10, "plan": 0}, "proxy_started_at": None}
        routing.update(overrides)
        return routing

    def usage(self, cost=1.0, external=None):
        return {"today": {"external": external or [1000, 100, 5000, 0], "plan": [0, 0, 0, 0]},
                "cost_usd": 21.0, "today_cost_usd": cost}

    def test_circuit_open_ranks_first(self):
        routing = self.routing(short={"minutes": 10, "counts": {"external": 2, "plan": 8},
                                      "plan_reasons": [["native_feature:unsupported_beta", 8]],
                                      "failures": [], "circuit_opens": 2})
        insights = dash.build_insights(routing, self.usage(), {"ok": True}, None, now=self.NOW)
        self.assertEqual(insights[0]["title"], "외부 회로 차단")
        self.assertEqual(insights[0]["level"], "critical")
        self.assertIn("2회", insights[0]["detail"])

    def test_plan_share_explains_the_top_reason(self):
        routing = self.routing(short={"minutes": 10, "counts": {"external": 3, "plan": 9},
                                      "plan_reasons": [["native_feature:unsupported_beta", 9]],
                                      "failures": [], "circuit_opens": 0})
        insights = dash.build_insights(routing, self.usage(), {"ok": False}, None, now=self.NOW)
        surge = [item for item in insights if item["title"] == "Plan 우회 급증"][0]
        self.assertEqual(surge["level"], "warn")
        self.assertIn("unsupported_beta", surge["detail"])
        self.assertIn(dash.REASON_HELP["native_feature:unsupported_beta"][1], surge["detail"])
        self.assertEqual(surge["evidence"], ["native_feature:unsupported_beta: 9"])

    def test_mild_plan_share_stays_informational(self):
        routing = self.routing(short={"minutes": 10, "counts": {"external": 7, "plan": 3},
                                      "plan_reasons": [["native_feature:token_count", 3]],
                                      "failures": [], "circuit_opens": 0})
        insights = dash.build_insights(routing, self.usage(), {"ok": False}, None, now=self.NOW)
        titles = [item["title"] for item in insights]
        self.assertIn("Plan 비중 상승", titles)
        self.assertNotIn("Plan 우회 급증", titles)

    def test_cost_projection_warns_before_the_limit_runs_out(self):
        insights = dash.build_insights(self.routing(), self.usage(cost=2.4),
                                       {"ok": True, "remaining_usd": 4.0, "limit_usd": 50}, None,
                                       now=self.NOW)
        burn = [item for item in insights if item["title"] == "한도 소진 예상"][0]
        self.assertEqual(burn["level"], "critical")
        self.assertIn("0.8일", burn["detail"])
        self.assertIn("$4.80", burn["detail"])

    def test_low_cache_reuse_is_flagged(self):
        usage = self.usage(cost=1.0, external=[500_000, 20_000, 1_000, 0])
        insights = dash.build_insights(self.routing(), usage, {"ok": False}, None, now=self.NOW)
        cache = [item for item in insights if item["title"] == "캐시 재사용 저하"]
        self.assertEqual(len(cache), 1)
        self.assertIn("0%", cache[0]["detail"])

    def test_latency_insight_separates_first_byte_from_generation(self):
        metrics = {"requests": 9, "failed": 0, "latencyMs": {
            "external": {"p50": 9000, "p95": 24_000}, "ttfb": {"p50": 1800, "p95": 2400}}}
        insights = dash.build_insights(self.routing(), self.usage(), {"ok": False}, None,
                                       metrics=metrics, now=self.NOW)
        latency = [item for item in insights if item["title"] == "응답 지연"][0]
        self.assertIn("첫 바이트까지 2.4초", latency["detail"])
        self.assertIn("21.6초는 생성 대기", latency["detail"])
        self.assertIn("-Stream", latency["detail"])
        self.assertEqual(latency["evidence"], ["p95 24.0s", "첫 바이트 2.4s"])

    def test_latency_and_failures_come_from_proxy_metrics(self):
        metrics = {"requests": 40, "failed": 2, "latencyMs": {"external": {"p50": 900, "p95": 25_000}}}
        insights = dash.build_insights(self.routing(), self.usage(), {"ok": False}, None,
                                       metrics=metrics, now=self.NOW)
        titles = [item["title"] for item in insights]
        self.assertIn("응답 지연", titles)
        self.assertIn("실패 응답 누적", titles)
        latency = [item for item in insights if item["title"] == "응답 지연"][0]
        self.assertIn("25.0초", latency["detail"])

    def test_idle_session_is_reported_but_not_alarming(self):
        routing = self.routing(state="idle", last={"age_seconds": 3600})
        insights = dash.build_insights(routing, self.usage(), {"ok": False}, None, now=self.NOW)
        idle = [item for item in insights if item["title"] == "세션 유휴"]
        self.assertEqual(idle[0]["level"], "info")

    def test_insights_are_capped_and_sorted(self):
        routing = self.routing(state="plan", last={"age_seconds": 3600},
                               short={"minutes": 10, "counts": {"external": 1, "plan": 9},
                                      "plan_reasons": [["native_feature:unsupported_beta", 9]],
                                      "failures": [["400", 2]], "circuit_opens": 3})
        usage = self.usage(cost=2.4, external=[500_000, 20_000, 1_000, 0])
        metrics = {"requests": 10, "failed": 1, "latencyMs": {"external": {"p95": 30_000}}}
        with mock.patch.object(dash, "proxy_stale", return_value=True):
            insights = dash.build_insights(routing, usage, {"ok": True, "remaining_usd": 2.0}, None,
                                           metrics=metrics, now=self.NOW)
        self.assertLessEqual(len(insights), 5)
        scores = [item["score"] for item in insights]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(insights[0]["title"], "실행기 코드가 오래됨")

    def test_summary_line_mentions_state_cost_and_next_step(self):
        insights = dash.build_insights(self.routing(), self.usage(cost=1.5), {"ok": False}, None,
                                       now=self.NOW)
        line = dash.summary_line(self.routing(), self.usage(cost=1.5), {"ok": False}, insights)
        self.assertIn("외부 서비스로 정상 처리 중", line)
        self.assertIn("$1.500", line)

    def test_refresh_interval_follows_activity(self):
        self.assertEqual(dash.refresh_interval_ms({"state": "external"}), 2000)
        self.assertEqual(dash.refresh_interval_ms({"state": "plan"}), 2000)
        self.assertEqual(dash.refresh_interval_ms({"state": "idle"}), 10000)
        self.assertEqual(dash.refresh_interval_ms({}), 10000)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(dash, "_live_session", return_value=(None, None)))

    def test_snapshot_shape_and_loopback_only_binding(self):
        with mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "model_pricing", return_value={"ok": False, "error": "configuration_missing"}), \
             mock.patch.object(dash, "read_settings", return_value={"OPENROUTER_MODEL": "vendor/model", "ROUTE_DEFAULT_PROVIDER": "openrouter"}):
            data = dash.snapshot()
        self.assertEqual(sorted(data),
                         ["account", "active_session", "alerts", "configured", "generated_at", "help",
                          "insights", "model", "pricing", "providers", "proxy_metrics",
                          "proxy_modified_at", "proxy_stale", "refresh_ms", "routing", "summary", "usage"])
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


class CliSettingsTests(unittest.TestCase):
    """The CLI card detects installed clients and runs one fixed connection test; it never routes."""

    def setUp(self):
        import types

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "openclaude.config.yaml"
        files = {name: {**spec, "path": self.path} for name, spec in dash.CONFIG_FILES.items()}
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", files))

        class CliError(ValueError):
            def __init__(self, code):
                self.code = code
                super().__init__(code)

        self.calls = []
        fake = types.ModuleType("routeCli")
        fake.KINDS = ("codex", "grok", "claude")
        fake.CliError = CliError

        def validate_binary(kind, path):
            if not str(path).startswith("C:\\tools\\" + kind):
                raise CliError("binary_name_mismatch")
            return path

        def detect_all(pinned=None):
            self.calls.append(("detect_all", pinned))
            return [{"kind": "codex", "found": True, "name": "codex.cmd", "dir": "C:\\npm", "via": "PATH",
                     "version": "0.164.0", "reason": None},
                    {"kind": "grok", "found": False, "name": None, "dir": None, "via": None,
                     "version": None, "reason": "not_found"},
                    {"kind": "claude", "found": True, "name": "claude.cmd", "dir": "C:\\npm", "via": "known-dir",
                     "version": "2.1.310", "reason": None}]

        def connection_test(kind, **options):
            self.calls.append(("connection_test", kind, options))
            return {"ok": True, "reason": "ready", "kind": kind, "version": "0.164.0", "exit_code": 0,
                    "elapsed_ms": 1200, "stdout": "SHOULD NOT LEAK"}

        fake.validate_binary, fake.detect_all, fake.connection_test = validate_binary, detect_all, connection_test
        self.fake = fake
        self.enterContext(mock.patch.dict(sys.modules, {"routeCli": fake}))

    def valid(self, **extra):
        return {"CLI_KIND": "codex", "CLI_MODEL": "gpt-x", "CLI_BINARY": "", "CLI_TIMEOUT_S": "45", **extra}

    def test_the_cli_section_is_saved_validated_and_counts_as_a_routing_service(self):
        self.assertFalse(dash.any_provider_configured())
        dash.save_config("cli", self.valid())
        self.assertEqual(dash.read_settings("cli"),
                         {"CLI_KIND": "codex", "CLI_MODEL": "gpt-x", "CLI_TIMEOUT_S": "45"})
        snapshot = dash.config_snapshot()["cli"]
        self.assertEqual(snapshot["fields"]["CLI_KIND"]["value"], "codex")
        self.assertFalse(snapshot["fields"]["CLI_KIND"]["secret"])
        self.assertTrue(dash.any_provider_configured())
        dash.save_default_provider("cli")
        self.assertEqual(dash.config_snapshot()["default_provider"], "cli")

    def test_cli_diagnostics_separate_requested_and_reported_models_without_spawning(self):
        dash.save_config("cli", self.valid(CLI_MODEL="requested-model"))
        dash.save_default_provider("cli")
        started = datetime.now(timezone.utc).isoformat()
        session = {"provider": "cli", "started": started}
        metrics = {"startedAt": started, "configuration": {"provider": "cli", "cliKind": "codex",
                   "requestedModel": "requested-model", "model": None, "usageMeasured": False}}
        with mock.patch.object(dash, "_live_session", return_value=(session, metrics)), \
                mock.patch.object(dash, "parse_route_log", return_value=[]), \
                mock.patch.object(dash, "_cli_detection", side_effect=AssertionError("read-only must not spawn")), \
                mock.patch.object(dash, "_cli_test", side_effect=AssertionError("read-only must not infer")):
            report = dash.diagnostics_report()
        self.assertEqual(report["configuration"]["next_provider"], "cli")
        self.assertEqual(report["configuration"]["configured_model"], "requested-model")
        self.assertEqual(report["session"]["requested_model"], "requested-model")
        self.assertIsNone(report["session"]["loaded_model"])
        self.assertEqual(report["session"]["model_status"], "unreported")
        self.assertFalse(report["session"]["usage_measured"])
        self.assertFalse(report["inference_performed"])
        self.assertIn("cli_model_unreported", [entry["code"] for entry in report["checks"]])

    def test_invalid_values_are_rejected_before_writing(self):
        dash.save_config("cli", self.valid())
        before = self.path.read_bytes()
        cases = [({"CLI_KIND": "gemini"}, "invalid_cli_kind"), ({"CLI_KIND": ""}, "missing_cli_kind"),
                 ({"CLI_MODEL": "bad model!"}, "invalid_cli_model"), ({"CLI_MODEL": "-x"}, "invalid_cli_model"),
                 ({"CLI_BINARY": "C:\\evil\\codex.exe"}, "invalid_cli_binary"),
                 ({"CLI_TIMEOUT_S": "5"}, "invalid_cli_timeout"), ({"CLI_TIMEOUT_S": "121"}, "invalid_cli_timeout"),
                 ({"CLI_TIMEOUT_S": "abc"}, "invalid_cli_timeout"), ({"CLI_TIMEOUT_S": "\u0663\u0660"}, "invalid_cli_timeout")]
        for override, reason in cases:
            with self.subTest(reason=reason, override=override):
                with self.assertRaises(ValueError) as caught:
                    dash.save_config("cli", {**self.valid(), **override})
                self.assertEqual(str(caught.exception), reason)
        self.assertEqual(self.path.read_bytes(), before)
        dash.save_config("cli", self.valid(CLI_BINARY="C:\\tools\\codex.exe"))  # a matching pin is accepted
        self.assertEqual(dash.read_settings("cli")["CLI_BINARY"], "C:\\tools\\codex.exe")

    def test_saving_the_cli_section_leaves_the_routing_services_alone(self):
        self.path.write_text('openrouter:\n  model: "vendor/a"\n  api_key: "sk-or-v1-abcdefghijkl"\n', encoding="utf-8")
        before = dash.read_settings("openrouter")
        dash.save_config("cli", self.valid())
        self.assertEqual(dash.read_settings("openrouter"), before)

    def test_detection_test_returns_only_safe_fields(self):
        result = dash.run_connection_test("cli", "metadata", self.valid(CLI_KIND="grok"))
        self.assertEqual((result["ok"], result["reason"], result["kind"]), (False, "not_found", "grok"))
        result = dash.run_connection_test("cli", "metadata", self.valid(CLI_KIND="codex"))
        self.assertEqual((result["ok"], result["reason"], result["version"]), (True, "detected", "0.164.0"))
        self.assertEqual(dash.run_connection_test("cli", "metadata", {"CLI_KIND": "nope"})["reason"],
                         "invalid_configuration")

    def test_the_connection_test_uses_the_model_backend_and_keeps_actual_model_unknown(self):
        import types
        backend = mock.Mock(protocol_model="cli/codex", version="0.165.0")
        backend.generate.return_value = ({"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"},
                                         {"reported_model": None, "usage_measured": False})
        constructor = mock.Mock(return_value=backend)
        with mock.patch.dict(sys.modules, {"cliBackend": types.SimpleNamespace(Backend=constructor)}):
            result = dash.run_connection_test("cli", "inference", self.valid(CLI_MODEL="m-1"))
        self.assertEqual(result["reason"], "ok")
        self.assertIsNone(result["model"])
        self.assertEqual(result["model_status"], "unreported")
        self.assertFalse(result["usage_measured"])
        self.assertEqual(constructor.call_args.args[0]["CLI_MODEL"], "m-1")
        self.assertEqual(backend.generate.call_args.args[0]["model"], "cli/codex")
        self.assertFalse([call for call in self.calls if call[0] == "connection_test"])

    def test_backend_errors_are_sanitized_and_unverified_versions_fail_closed(self):
        import types
        for code, expected in (("backend_version_unverified", "backend_version_unverified"),
                               ("secret C:/private/path", "cli_backend_failed")):
            error = ValueError("SHOULD NOT LEAK")
            error.code = code
            with mock.patch.dict(sys.modules, {"cliBackend": types.SimpleNamespace(Backend=mock.Mock(side_effect=error))}):
                result = dash.run_connection_test("cli", "inference", self.valid())
            self.assertEqual(result["reason"], expected)
            self.assertNotIn("SHOULD NOT LEAK", json.dumps(result))
            self.assertNotIn("private", json.dumps(result))

    def test_unsaved_values_are_validated_before_anything_runs(self):
        result = dash.run_connection_test("cli", "inference", self.valid(CLI_TIMEOUT_S="999"))
        self.assertEqual(result["reason"], "invalid_configuration")
        self.assertFalse([call for call in self.calls if call[0] == "connection_test"])

    def test_a_missing_helper_module_is_reported_instead_of_crashing(self):
        with mock.patch.dict(sys.modules, {"routeCli": None}):
            self.assertEqual(dash.run_connection_test("cli", "metadata", self.valid())["reason"], "cli_module_missing")
            with self.assertRaises(ImportError):
                dash._cli_detection({})

    def test_a_cli_has_no_model_catalog(self):
        catalog = dash.model_catalog("cli")
        self.assertEqual((catalog["ok"], catalog["error"], catalog["models"]), (False, "not_supported", []))

    def test_the_detection_endpoint_needs_the_session_token(self):
        import threading
        import urllib.error
        import urllib.request

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            url = f"http://127.0.0.1:{port}/api/cli"
            with self.assertRaises(urllib.error.HTTPError) as denied:
                opener.open(url, timeout=10)
            denied.exception.close()
            self.assertEqual(denied.exception.code, 403)
            request = urllib.request.Request(url, headers={"x-dashboard-token": dash.TOKEN})
            with opener.open(request, timeout=10) as response:
                kinds = json.loads(response.read())["kinds"]
            self.assertEqual([row["kind"] for row in kinds], ["codex", "grok", "claude"])
        finally:
            server.shutdown()
            server.server_close()


class RequestShapeTests(unittest.TestCase):
    """request_shape events say what a request looks like, never what it says."""

    def event(self, now, minutes_ago=1, **fields):
        stamp = (now - timedelta(minutes=minutes_ago)).isoformat()
        return {"ts": stamp, "event": "request_shape", **fields}

    def test_counts_by_family_with_text_only_candidates(self):
        now = datetime.now(timezone.utc)
        events = [
            self.event(now, family="sonnet", tools=True, tool_blocks=False, media=False, stream=True),
            self.event(now, family="sonnet", tools=False, tool_blocks=False, media=False, stream=True),
            self.event(now, family="sonnet", tools=False, tool_blocks=True, media=False, stream=True),
            self.event(now, family="haiku", tools=False, tool_blocks=False, media=False, stream=False),
            self.event(now, family="haiku", tools=False, tool_blocks=False, media=True, stream=False),
            self.event(now, family="opus", tools=True, tool_blocks=True, media=False, stream=True),
        ]
        shapes = dash.routing_state(events, now=now)["shapes"]
        self.assertEqual(shapes["total"], 6)
        self.assertEqual(shapes["by_family"]["sonnet"], {"no_tools": 2, "tools": 1})
        self.assertEqual(shapes["by_family"]["haiku"], {"no_tools": 2, "tools": 0})
        self.assertEqual(shapes["by_family"]["opus"], {"no_tools": 0, "tools": 1})
        # a tool_result block or an image makes a request unfit for a text-only backend
        self.assertEqual(shapes["text_only"], {"sonnet": 1, "haiku": 1})

    def test_old_events_unknown_families_and_non_boolean_flags(self):
        now = datetime.now(timezone.utc)
        events = [
            self.event(now, minutes_ago=dash.ROUTE_WINDOW + 5, family="sonnet", tools=False),
            self.event(now, family="gemini", tools=False, tool_blocks=False, media=False),
            self.event(now, family="sonnet", tools="true", tool_blocks="yes", media=1),
            self.event(now),  # fields missing entirely
        ]
        shapes = dash.routing_state(events, now=now)["shapes"]
        self.assertEqual(shapes["total"], 3)  # the old one is outside the window
        self.assertEqual(shapes["by_family"]["other"], {"no_tools": 2, "tools": 0})
        # only literal booleans count as true: the string and integer flags do not
        self.assertEqual(shapes["by_family"]["sonnet"], {"no_tools": 1, "tools": 0})
        self.assertEqual(shapes["text_only"], {"other": 2, "sonnet": 1})

    def test_without_the_events_the_totals_are_zero_and_routing_is_unaffected(self):
        now = datetime.now(timezone.utc)
        selected = {"ts": (now - timedelta(seconds=5)).isoformat(), "event": "route_selected",
                    "provider": "openrouter", "reason": "replacement", "model": "vendor/model"}
        state = dash.routing_state([selected], now=now)
        self.assertEqual(state["shapes"], {"total": 0, "by_family": {}, "text_only": {}})
        self.assertEqual(state["state"], "external")
        with_shape = dash.routing_state([selected, self.event(now, family="sonnet", tools=True)], now=now)
        self.assertEqual(with_shape["state"], "external")
        self.assertEqual(with_shape["counts"], state["counts"])

    def test_they_do_not_flood_the_recent_event_list(self):
        now = datetime.now(timezone.utc)
        selected = {"ts": (now - timedelta(seconds=30)).isoformat(), "event": "route_selected",
                    "provider": "plan", "reason": "native_feature"}
        events = [selected] + [self.event(now, family="sonnet", tools=True) for _ in range(40)]
        recent = dash.routing_state(events, now=now)["recent"]
        self.assertEqual([row["event"] for row in recent], ["route_selected"])

    def test_the_status_report_exposes_the_shapes(self):
        now = datetime.now(timezone.utc)
        events = [self.event(now, family="sonnet", tools=False, tool_blocks=False, media=False)]
        with mock.patch.object(dash, "parse_route_log", return_value=events), \
                mock.patch.object(dash, "read_settings", return_value={"OPENROUTER_MODEL": "vendor/model"}):
            report = dash.status_report(offline=True)
        self.assertEqual(report["request_shapes"]["text_only"], {"sonnet": 1})


class UrlMaskingTests(unittest.TestCase):
    """Server addresses are masked like keys; optional ones can still be cleared on purpose."""

    URL = "http://127.0.0.1:11434/v1/messages"
    MODELS = "http://127.0.0.1:11434/v1/models"

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "openclaude.config.yaml"
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", {"local": {
            "path": self.path,
            "secret": {"LOCAL_LLM_API_KEY", "LOCAL_LLM_URL", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL"},
            "clearable": {"LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL"},
            "required": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL"),
            "fields": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                       "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL")}}))
        self.path.write_text(f'local:\n  url: "{self.URL}"\n  model: "llama"\n  models_url: "{self.MODELS}"\n',
                             encoding="utf-8")

    def stored(self):
        return dash.read_settings("local")

    def test_mask_url_hides_host_credentials_and_query(self):
        self.assertEqual(dash._mask_url(""), "")
        self.assertEqual(dash._mask_url(self.URL), "http://12•••••.1:11434/v1/messages")
        self.assertEqual(dash._mask_url("http://gpu/v1/messages"), "http://g•••/v1/messages")
        self.assertEqual(dash._mask_url("http://[::1]:8080/v1/messages"), "http://[:•••]:8080/v1/messages")
        hidden = dash._mask_url("https://user:pw@llm.example.com/v1/messages?token=abc#frag")
        self.assertEqual(hidden, "https://•••@ll•••••••••••om/v1/messages?•••")
        custom = dash._mask_url("http://127.0.0.1:11434/v1/models/sk-live-SECRET")
        self.assertNotIn("SECRET", custom, "a custom path can carry a credential")
        self.assertTrue(custom.endswith("/•••"), custom)
        for leaked in ("user", "pw", "token", "abc", "frag", "example"):
            self.assertNotIn(leaked, hidden)
        for bad in ("not a url", "ftp://host/x", "http://host:99999/x", "http://"):
            self.assertEqual(dash._mask_url(bad), "•" * 8)

    def test_snapshot_never_returns_a_stored_address(self):
        snapshot = dash.config_snapshot()
        fields = snapshot["local"]["fields"]
        for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODELS_URL"):
            self.assertTrue(fields[name]["secret"])
            self.assertTrue(fields[name]["set"])
            self.assertEqual(fields[name]["value"], "")
        self.assertEqual(fields["LOCAL_LLM_URL"]["preview"], "http://12•••••.1:11434/v1/messages")
        self.assertNotIn("127.0.0.1", json.dumps(snapshot))
        self.assertFalse(fields["LOCAL_LLM_URL"]["clearable"])
        self.assertTrue(fields["LOCAL_LLM_MODELS_URL"]["clearable"])
        self.assertFalse(fields["LOCAL_LLM_HTTP_URL"]["set"])
        self.assertEqual(fields["LOCAL_LLM_MODEL"]["value"], "llama")

    def test_an_empty_address_keeps_the_stored_one(self):
        dash.save_config("local", {"LOCAL_LLM_URL": "", "LOCAL_LLM_MODEL": "llama2",
                                   "LOCAL_LLM_MODELS_URL": ""})
        stored = self.stored()
        self.assertEqual(stored["LOCAL_LLM_URL"], self.URL)
        self.assertEqual(stored["LOCAL_LLM_MODELS_URL"], self.MODELS)
        self.assertEqual(stored["LOCAL_LLM_MODEL"], "llama2")

    def test_a_typed_address_replaces_the_stored_one(self):
        other = "http://127.0.0.1:9000/v1/messages"
        dash.save_config("local", {"LOCAL_LLM_URL": other, "LOCAL_LLM_MODEL": "llama",
                                   "LOCAL_LLM_MODELS_URL": "http://127.0.0.1:9000/v1/models"})
        self.assertEqual(self.stored()["LOCAL_LLM_URL"], other)

    def test_clear_erases_an_optional_address(self):
        dash.save_config("local", {"LOCAL_LLM_MODEL": "llama"}, clear=["LOCAL_LLM_MODELS_URL"])
        self.assertEqual(self.stored().get("LOCAL_LLM_MODELS_URL", ""), "")
        self.assertFalse(dash.config_snapshot()["local"]["fields"]["LOCAL_LLM_MODELS_URL"]["set"])
        self.assertEqual(self.stored()["LOCAL_LLM_URL"], self.URL)

    def test_clear_wins_over_a_posted_value(self):
        dash.save_config("local", {"LOCAL_LLM_MODEL": "llama", "LOCAL_LLM_MODELS_URL": self.MODELS},
                         clear=["LOCAL_LLM_MODELS_URL"])
        self.assertEqual(self.stored().get("LOCAL_LLM_MODELS_URL", ""), "")

    def test_clear_rejects_required_keys_and_unknown_names_without_writing(self):
        before = self.path.read_text(encoding="utf-8")
        for bad in (["LOCAL_LLM_URL"], ["LOCAL_LLM_API_KEY"], ["NOPE"], [1], "LOCAL_LLM_MODELS_URL", {"a": 1},
                    ["LOCAL_LLM_MODELS_URL"] * 17):
            with self.assertRaises(ValueError) as caught:
                dash.save_config("local", {"LOCAL_LLM_MODEL": "llama"}, clear=bad)
            self.assertEqual(str(caught.exception), "invalid_clear")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / "openclaude.config.yaml.bak").exists())

    def test_clearing_the_exception_a_url_depends_on_is_rejected(self):
        internal = "http://llm.corp.example/v1/messages"
        self.path.write_text(f'local:\n  url: "{internal}"\n  model: "llama"\n  http_url: "{internal}"\n',
                             encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            dash.save_config("local", {"LOCAL_LLM_MODEL": "llama"}, clear=["LOCAL_LLM_HTTP_URL"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_connection_results_and_model_lists_do_not_echo_the_address(self):
        with mock.patch.object(dash, "_http_json", side_effect=OSError("boom")):
            result = dash.run_connection_test("local", "metadata")
            catalog = dash.model_catalog("local", refresh=True)
        self.assertNotIn("127.0.0.1", json.dumps(result) + json.dumps(catalog))


class OpenPageTests(unittest.TestCase):
    """--open sends the browser to a dashboard page without touching any settings file."""

    def run_serve(self, **kwargs):
        class Stub:
            server_address = ("127.0.0.1", 8787)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with mock.patch.object(dash, "DashboardServer", return_value=Stub()),                 mock.patch.object(dash, "_refresh_loop"), mock.patch.object(dash, "_stop_previous"),                 mock.patch.object(dash, "_write_pid_record"), mock.patch.object(dash, "_read_pid_record",
                                                                                 return_value=None),                 mock.patch.object(dash.webbrowser, "open") as opener,                 contextlib.redirect_stdout(io.StringIO()):
            dash.serve(**kwargs)
        return opener

    def test_open_page_launches_the_browser_on_that_page(self):
        opener = self.run_serve(open_page="settings")
        opener.assert_called_once_with("http://127.0.0.1:8787/settings")

    def test_default_serve_does_not_open_a_browser(self):
        self.run_serve().assert_not_called()

    def test_open_flag_defaults_to_the_settings_page(self):
        with mock.patch.object(dash, "serve") as serve:
            dash.main(["--open"])
            dash.main([])
        self.assertEqual(serve.call_args_list[0].kwargs["open_page"], "settings")
        self.assertIsNone(serve.call_args_list[1].kwargs["open_page"])


class LegacyMigrationSaveTests(unittest.TestCase):
    """Saving one service must not shadow another value still read from a legacy .env file."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "openclaude.config.yaml"
        self.enterContext(mock.patch.object(dash, "CONFIG_FILES", {
            "openrouter": {"path": self.config,
                           "secret": {"ANTHROPIC_AUTH_TOKEN"},
                           "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                           "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}}))

    def test_saving_a_model_keeps_the_legacy_key(self):
        self.config.write_text('local:\n  model: "keep"\n', encoding="utf-8")
        (self.root / ".env.openrouter").write_text(
            "ANTHROPIC_AUTH_TOKEN=sk-or-legacy\nOPENROUTER_MODEL=vendor/old\n", encoding="utf-8")
        dash.save_config("openrouter", {"OPENROUTER_MODEL": "vendor/new"})
        values = dash.read_settings("openrouter")
        self.assertEqual(values["OPENROUTER_MODEL"], "vendor/new")
        self.assertEqual(values["ANTHROPIC_AUTH_TOKEN"], "sk-or-legacy")
        self.assertEqual(dash.config_snapshot()["openrouter"]["fields"]["ANTHROPIC_AUTH_TOKEN"]["set"], True)


class ConfigEndpointAuthTests(unittest.TestCase):
    """Settings are session-scoped even when values are masked: no token, no config."""

    def setUp(self):
        import threading

        self.server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, headers):
        import socket

        request = ["GET /api/config HTTP/1.1", f"Host: 127.0.0.1:{self.port}", "Connection: close"]
        request.extend(f"{name}: {value}" for name, value in headers)
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
            chunks = []
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                chunks.append(part)
        return b"".join(chunks)

    def test_config_needs_the_session_token(self):
        denied = self.get([])
        self.assertIn(b"403", denied.split(b"\r\n", 1)[0])
        self.assertIn(b"token_required", denied)
        allowed = self.get([("x-dashboard-token", dash.TOKEN)])
        self.assertIn(b"200", allowed.split(b"\r\n", 1)[0])
        self.assertIn(b"active_session", allowed)

    def test_a_non_ascii_token_is_rejected_as_json(self):
        response = self.get([("x-dashboard-token", "\xff")])
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])
        self.assertIn(b"token_required", response)


class ProviderSelectionDisplayTests(unittest.TestCase):
    """A dual install must be visible: configured services and the live session's provider."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(dash, "ROOT", self.root))
        self.enterContext(mock.patch.object(dash, "SESSION_HANDOFF_FILE",
                                            self.root / ".session-handoff.json"))

    def handoff(self, payload):
        (self.root / ".session-handoff.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_provider_states_reports_each_routing_service(self):
        with mock.patch.object(dash, "provider_configured",
                               side_effect=lambda name: name == "local"):
            self.assertEqual(dash.provider_states(), {"openrouter": False, "local": True, "cli": False})

    def test_active_session_exposes_only_provider_and_started(self):
        self.handoff({"provider": "local", "started": "2026-10-26T00:00:00+00:00",
                      "nonce": "private-token", "port": 48432, "proxy_pid": 1})
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"startedAt":"2026-10-26T00:00:00Z","requests":4}'
        with mock.patch.object(dash.http.client, "HTTPConnection", return_value=connection) as connect:
            self.assertEqual(dash.active_session(),
                             {"provider": "local", "started": "2026-10-26T00:00:00+00:00"})
        connect.assert_called_once_with("127.0.0.1", 48432, timeout=0.5)
        connection.request.assert_called_once_with("GET", "/api/metrics",
                                                   headers={"x-local-route-token": "private-token"})
        connection.close.assert_called_once()

    def test_active_session_ignores_missing_malformed_or_foreign_records(self):
        self.assertIsNone(dash.active_session())
        (self.root / ".session-handoff.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(dash.active_session())
        self.handoff(["not", "an", "object"])
        self.assertIsNone(dash.active_session())
        self.handoff({"provider": "plan", "nonce": "private-token"})
        self.assertIsNone(dash.active_session(), "only routing services count as a session")
        self.handoff({"provider": "local", "nonce": "private-token"})
        self.assertIsNone(dash.active_session(), "a record without a listening authenticated proxy is not live")

    def test_live_session_refuses_redirects_and_unauthorized_proxies(self):
        self.handoff({"provider": "local", "nonce": "private-token", "port": 48432})
        for status in (301, 403, 500):
            with self.subTest(status=status):
                connection = mock.Mock()
                connection.getresponse.return_value.status = status
                with mock.patch.object(dash.http.client, "HTTPConnection", return_value=connection):
                    self.assertIsNone(dash.active_session())
                connection.getresponse.return_value.read.assert_not_called()
                connection.close.assert_called_once()

    def test_offline_and_malformed_session_records_never_connect(self):
        with mock.patch.object(dash.http.client, "HTTPConnection") as connect:
            self.handoff({"provider": "local", "nonce": "private-token", "port": 48432})
            self.assertEqual(dash._live_session(offline=True), (None, None))
            for port in (True, 0, 65536, "48432", None):
                self.handoff({"provider": "local", "nonce": "private-token", "port": port})
                self.assertIsNone(dash.active_session())
            self.handoff({"provider": "local", "nonce": "invalid\nheader", "port": 48432})
            self.assertIsNone(dash.active_session())
        connect.assert_not_called()

    def test_status_and_snapshot_expose_the_dual_provider_state(self):
        self.handoff({"provider": "openrouter", "started": "2026-10-26T00:00:00+00:00",
                      "nonce": "private-token"})
        with mock.patch.object(dash, "provider_configured", return_value=True), \
             mock.patch.object(dash, "parse_route_log", return_value=[]), \
             mock.patch.object(dash, "account_state", return_value={"ok": False, "error": "offline"}), \
             mock.patch.object(dash, "usage_snapshot", return_value=dash._empty_usage()), \
             mock.patch.object(dash, "model_pricing", return_value=None), \
             mock.patch.object(dash, "_live_session", side_effect=lambda offline=False:
                               (None, None) if offline else ({"provider": "openrouter", "started": None}, None)), \
             mock.patch.object(dash, "proxy_metrics", return_value=None):
            report = dash.status_report(offline=True)
            data = dash.snapshot()
        self.assertEqual(report["providers"], {"openrouter": True, "local": True, "cli": True})
        self.assertIsNone(report["active_session"], "offline mode does not authenticate a live proxy")
        self.assertEqual(data["providers"], {"openrouter": True, "local": True, "cli": True})
        self.assertEqual(data["active_session"]["provider"], "openrouter")
        self.assertNotIn("private-token", json.dumps(report))
        self.assertNotIn("private-token", json.dumps(data))


class RequirementAuditRegressionTests(unittest.TestCase):
    def test_nonobject_and_malformed_inference_replies_are_sanitized_errors(self):
        replies = [None, [], 3, "fixture", {"content": 7}, {"content": {}},
                   {"content": [None]}, {"content": [{"type": "text", "text": 3}]}]
        for provider in ("openrouter", "local"):
            values = {"ANTHROPIC_AUTH_TOKEN": "sk-or-synthetic", "OPENROUTER_MODEL": "fixture/model"} if provider == "openrouter" else {
                "LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODEL": "fixture-model"}
            for reply in replies:
                with self.subTest(provider=provider, reply=reply), mock.patch.object(dash, "_effective_values", return_value=values), mock.patch.object(dash, "_http_json", return_value=(200, reply)):
                    result = dash.run_connection_test(provider, "inference")
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "invalid_response")
                self.assertNotIn("sk-or-synthetic", json.dumps(result))

    def test_stale_proxy_never_has_a_healthy_exit_code(self):
        for state in ("external", "idle"):
            report = {"ok": False, "state": state, "config": {"proxy_stale": True}, "alerts": []}
            self.assertEqual(dash.status_exit_code(report), 3)

    def test_ambiguous_auto_provider_has_no_guessed_account_or_model(self):
        with mock.patch.object(dash, "read_settings", return_value={"ROUTE_DEFAULT_PROVIDER": "auto"}), mock.patch.object(dash, "provider_configured", return_value=True):
            provider, values, model = dash._display_service({"last": {}}, None)
        self.assertEqual((provider, values, model), (None, {}, None))
        with mock.patch.object(dash, "account_state", side_effect=AssertionError("no selected account")), mock.patch.object(dash, "model_pricing", side_effect=AssertionError("no selected model")):
            account, pricing = dash._service_finances(provider, values, model)
        self.assertEqual(account["error"], "not_applicable")
        self.assertIsNone(pricing)


if __name__ == "__main__":
    unittest.main()
