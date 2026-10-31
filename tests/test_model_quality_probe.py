import contextlib
import io
import json
import unittest
from unittest.mock import patch

import cliBackend
import modelQualityProbe as probe
import routeConfig
import routeDashboard as dashboard
from tests.live_cli_smoke import _request_adapter


def tool_call(key="code", tool_id="synthetic-id"):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": tool_id, "name": "Lookup", "input": {"key": key}}]}


class ModelQualityProbeTest(unittest.TestCase):
    def test_requires_explicit_opt_in_before_backend_initialization(self):
        with patch.object(probe, "configured_call") as configured, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                probe.main(["--provider", "local"])
        self.assertEqual(error.exception.code, 2)
        configured.assert_not_called()

    def test_tool_round_trip_checks_result_and_reports_only_metadata(self):
        requests = []
        def call(body):
            requests.append(body)
            if len(requests) == 1:
                return tool_call(), {"usage_measured": True, "model_reported": False}
            self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "synthetic-id")
            value = body["messages"][2]["content"][0]["content"]
            self.assertRegex(value, r"^probe-[0-9a-f]{16}$")
            self.assertNotIn(value, json.dumps(requests[0]))
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": value}]}, {"usage_measured": False, "model_reported": True}
        result = probe.evaluate(call, "private/model")
        self.assertTrue(result["ok"])
        self.assertTrue(result["tool_result_understood"])
        self.assertFalse(result["first_model_reported"])
        self.assertFalse(result["second_usage_reported"])
        self.assertNotIn("private/model", json.dumps(result))
        self.assertEqual(len(requests), 2)

    def test_wrong_tool_arguments_stop_before_second_request(self):
        calls = []
        def call(body):
            calls.append(body)
            return tool_call("wrong"), {}
        result = probe.evaluate(call, "model")
        self.assertEqual(result["reason"], "invalid_tool_proposal")
        self.assertEqual(len(calls), 1)

    def test_fabricated_continuation_is_failure(self):
        results = iter([(tool_call(), {}), ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "not the tool result"}]}, {})])
        result = probe.evaluate(lambda _: next(results), "model")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "continuation_failed")

    def test_backend_exception_never_prints_sensitive_details(self):
        out = io.StringIO()
        with patch.object(probe, "configured_call", side_effect=RuntimeError("private-token")), contextlib.redirect_stdout(out):
            self.assertEqual(probe.main(["--provider", "cli", "--run"]), 1)
        self.assertEqual(json.loads(out.getvalue())["reason"], "backend_failed")
        self.assertNotIn("private-token", out.getvalue())

    def test_local_probe_preserves_unknown_and_mismatched_model_metadata(self):
        values = {"LOCAL_LLM_URL": "http://127.0.0.1:8080/v1/messages", "LOCAL_LLM_MODEL": "model-a"}
        replies = iter([(200, {"model": "model-b", "usage": {"input_tokens": 3}}),
                        (200, {"usage": {"input_tokens": 3, "output_tokens": 2}})])
        with patch.object(dashboard, "read_settings", return_value=values), \
                patch.object(dashboard, "_http_json", side_effect=lambda *a, **k: next(replies)):
            call, model = probe.configured_call("local", dashboard=dashboard)
            _, first = call({"model": model})
            _, second = call({"model": model})
        self.assertIs(first["model_matches_requested"], False)
        self.assertFalse(first["usage_measured"])
        self.assertIsNone(second["model_matches_requested"])
        self.assertTrue(second["usage_measured"])

    def test_captured_config_is_used_for_http_and_cli_adapters(self):
        values = {"LOCAL_LLM_URL": "http://127.0.0.1:8080/v1/messages", "LOCAL_LLM_MODEL": "model-a",
                  "LOCAL_LLM_API_KEY": "synthetic-key"}
        with patch.object(dashboard, "read_settings", side_effect=AssertionError("unexpected settings read")), \
                patch.object(dashboard, "_http_json", return_value=(200, {"model": "model-a"})) as request:
            call, model = probe.configured_call("local", dashboard=dashboard, values=values)
            values["LOCAL_LLM_MODEL"] = "changed"
            call({"model": model})
        self.assertEqual(model, "model-a")
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:8080/v1/messages")
        self.assertEqual(request.call_args.args[1]["authorization"], "Bearer synthetic-key")
        cli_values = {"CLI_KIND": "codex", "CLI_MODEL": "original"}
        with patch.object(cliBackend, "Backend") as backend, patch.object(routeConfig, "Store", side_effect=AssertionError("unexpected read")):
            backend.return_value.protocol_model = "cli/codex"
            probe.configured_call("cli", values=cli_values)
        self.assertEqual(backend.call_args.args[0], cli_values)
        self.assertIsNot(backend.call_args.args[0], cli_values)

    def test_cli_adapter_accepts_both_probe_turns(self):
        class FakeBackend:
            protocol_model = "cli/codex"

            def generate(self, request, cancelled):
                cliBackend.request_prompt(request)
                value = request["messages"][-1]["content"][0]["content"] if len(request["messages"]) != 1 else None
                content = [tool_call()["content"][0]] if value is None else [{"type": "text", "text": value}]
                return {"stop_reason": "tool_use" if value is None else "end_turn", "content": content}, {"reported_model": "model-a", "usage_measured": True}
        backend = FakeBackend()
        def call(body):
            reply, metadata = _request_adapter(backend, {}, body)
            return reply, {"model_reported": bool(metadata["reported_model"]), "usage_measured": metadata["usage_measured"]}
        self.assertTrue(probe.evaluate(call, "cli/codex")["ok"])

    def test_incomplete_stop_reason_is_not_a_success(self):
        message = tool_call()
        message["stop_reason"] = "max_tokens"
        result = probe.evaluate(lambda _: (message, {}), "model")
        self.assertEqual(result["reason"], "invalid_tool_proposal")

    def test_deadline_prevents_second_call(self):
        calls = []
        def call(body):
            calls.append(body)
            return tool_call(), {}
        with patch.object(probe.time, "monotonic", side_effect=[0, 0, 181]):
            with self.assertRaises(TimeoutError):
                probe.evaluate(call, "model", deadline=180)
        self.assertEqual(len(calls), 1)

    def test_cli_uses_backend_directly_without_global_factory_patch(self):
        class FakeBackend:
            model = "requested-model"
            protocol_model = "cli/codex"
            def __init__(self, values):
                self.values = values
            def generate(self, body, cancelled):
                self.cancelled = cancelled()
                return tool_call(), {"reported_model": "other-model", "usage_measured": True}
        with patch.object(routeConfig, "Store") as store, patch.object(cliBackend, "Backend", FakeBackend):
            store.return_value.read_provider.return_value = {"CLI_KIND": "codex"}
            call, model = probe.configured_call("cli", deadline=float("inf"))
            _, metadata = call({"model": model})
        self.assertEqual(model, "cli/codex")
        self.assertIs(metadata["model_matches_requested"], False)


if __name__ == "__main__":
    unittest.main()
