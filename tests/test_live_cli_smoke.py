"""Safety checks for the explicitly opted-in live CLI smoke entrypoint."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

from tests import live_cli_smoke as smoke


class LiveCliSmokeTest(unittest.TestCase):
    def test_missing_run_flag_never_constructs_a_backend(self):
        with patch.object(sys, "argv", ["live_cli_smoke.py"]), \
                patch.object(smoke.cliBackend, "Backend") as backend, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                smoke.main()
        self.assertEqual(result.exception.code, 2)
        backend.assert_not_called()

    def test_synthetic_tool_proposal_reports_no_raw_response(self):
        class FakeStore:
            def read_provider(self, provider):
                return {"CLI_KIND": "codex"}

        class FakeBackend:
            def __init__(self, values, runner=None):
                self.runner = runner
                self.protocol_model = "cli/codex"

            def generate(self, request, cancelled):
                self.asserted_request = request
                return {"content": [{"type": "tool_use", "name": "Echo", "input": {"value": "ok"}}]}, {
                    "reported_model": "PRIVATE-MODEL-STRING", "usage_measured": True, "process_ms": 10.5}

        out = io.StringIO()
        with patch.object(sys, "argv", ["live_cli_smoke.py", "--run"]), \
                patch.object(smoke.routeConfig, "Store", FakeStore), \
                patch.object(smoke.cliBackend, "Backend", FakeBackend), contextlib.redirect_stdout(out):
            self.assertEqual(smoke.main(), 0)
        self.assertTrue(json.loads(out.getvalue())["model_reported"])
        self.assertTrue(json.loads(out.getvalue())["process_timing_reported"])
        self.assertNotIn("PRIVATE-MODEL-STRING", out.getvalue())

    def test_default_runner_identity_passes_process_timing_to_adapter(self):
        class FakeStore:
            def read_provider(self, provider):
                return {"CLI_KIND": "codex"}

        def fake_run(argv, prompt, env, cwd, timeout, cancelled, process_timing=None):
            self.assertIsNotNone(process_timing)
            process_timing["process_ms"] = 24.5
            value = {"text": "", "tool_calls": [{"name": "Echo", "arguments_json": '{"value":"ok"}'}]}
            return "\n".join(json.dumps(event) for event in [
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}},
                {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}}])

        out = io.StringIO()
        with patch.object(sys, "argv", ["live_cli_smoke.py", "--run"]), \
                patch.object(smoke.routeConfig, "Store", FakeStore), \
                patch.object(smoke.cliBackend, "_run", fake_run), \
                patch.multiple(smoke.cliBackend.routeCli,
                               find_binary=lambda *_: {"path": "fixture"},
                               resolve_command=lambda _: ["fixture"],
                               child_env=lambda _: {},
                               probe_version=lambda *_: {"ok": True, "version": "0.165.0"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(smoke.main(), 0)
        self.assertTrue(json.loads(out.getvalue())["process_timing_reported"])


if __name__ == "__main__":
    unittest.main()
