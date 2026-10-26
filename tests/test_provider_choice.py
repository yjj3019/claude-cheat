"""Provider choice: one configured service is selected, two require an explicit choice."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import openrouterAdvisor as route


class ProviderChoiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))

    def write_openrouter(self, model="vendor/model-a", key="sk-or-synthetic-key"):
        (self.root / ".env.openrouter").write_text(
            f"ANTHROPIC_AUTH_TOKEN={key}\nOPENROUTER_MODEL={model}\n", encoding="utf-8")

    def write_local(self, model="fixture/local-model", url="http://127.0.0.1:11434/v1/messages"):
        (self.root / ".env.local-llm").write_text(
            f"LOCAL_LLM_URL={url}\nLOCAL_LLM_MODEL={model}\n", encoding="utf-8")

    def test_only_openrouter_is_selected(self):
        self.write_openrouter()
        self.assertEqual(route.configured_provider(), "openrouter")

    def test_only_local_is_selected(self):
        self.write_local()
        self.assertEqual(route.configured_provider(), "local")

    def test_both_configured_requires_an_explicit_choice(self):
        self.write_openrouter()
        self.write_local()
        with self.assertRaises(route.ProviderChoiceError):
            route.configured_provider()

    def test_nothing_configured_keeps_the_openrouter_setup_guidance(self):
        with self.assertRaises(route.OpenRouterConfigError):
            route.configured_provider()

    def test_a_broken_local_section_points_at_local_not_openrouter(self):
        (self.root / "openclaude.config.yaml").write_text(
            'local:\n  url: "http://127.0.0.1:11434/v1/messages"\n  model: ""\n', encoding="utf-8")
        with self.assertRaises(route.LocalStartupError) as caught:
            route.configured_provider()
        self.assertEqual(caught.exception.code, "local_configuration_invalid")

    def test_launch_auto_uses_the_single_configured_provider(self):
        self.write_local()
        session = mock.MagicMock()
        session.__enter__ = mock.Mock(return_value={})
        session.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(route.shutil, "which", return_value="claude"), \
             mock.patch.object(route, "routed_session", return_value=session) as routed, \
             mock.patch.object(route.subprocess, "call", return_value=0):
            self.assertEqual(route.launch("auto"), 0)
        self.assertEqual(routed.call_args.args[0], "local")

    def test_launch_auto_refuses_an_ambiguous_install(self):
        self.write_openrouter()
        self.write_local()
        with self.assertRaises(route.ProviderChoiceError):
            route.launch("auto")

    def test_the_cli_prints_choice_guidance_and_exits_two(self):
        self.write_openrouter()
        self.write_local()
        with mock.patch.object(route, "read_handoff", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = route.main([])
        self.assertEqual(code, 2)
        text = stderr.getvalue()
        self.assertIn("Choose one explicitly", text)
        self.assertIn("OpenClaude.ps1 -Provider OpenRouter", text)
        self.assertIn("OpenClaude.ps1 -Provider Local", text)
        payload = json.loads([line for line in text.splitlines() if line.startswith("{")][-1])
        self.assertEqual(payload["event"], "provider_choice_required")
        self.assertEqual(payload["providers"], ["openrouter", "local"])
        self.assertEqual(payload["action"], "choose_provider")
        self.assertIn("OpenClaude.ps1 -Provider Local", payload["commands"])

    def test_takeover_auto_follows_the_live_sessions_provider(self):
        record = {"port": 48432, "nonce": "nonce", "provider": "local", "proxy_pid": 1}
        session = mock.MagicMock()
        session.__enter__ = mock.Mock(return_value={})
        session.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(route, "read_handoff", return_value=record), \
             mock.patch.object(route, "port_open", return_value=True), \
             mock.patch.object(route, "request_handover", return_value={"drained": True}), \
             mock.patch.object(route, "wait_for_port_release", return_value=True), \
             mock.patch.object(route, "routed_session", return_value=session) as routed, \
             mock.patch.object(route.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(route.takeover("auto"), 0)
        self.assertEqual(routed.call_args.args[0], "local")


if __name__ == "__main__":
    unittest.main()
