"""Requirement R1: an ordinary OpenRouter session routes externally without an extra option.

This file is a regression fence, not a general launcher test. If it fails, the change that
broke it must be reverted (see docs/USER-REQUIREMENTS.ko.md); weakening the requirement needs
explicit user approval.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("user_requirements_route", ROOT / "openrouterAdvisor.py")
route = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route)

AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
OPENROUTER = {"SONNET_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-synthetic",
              "OPENROUTER_MODEL": "vendor/model"}
LOCAL = {"SONNET_PROVIDER": "local", "LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages",
         "LOCAL_LLM_MODEL": "local-model"}
DUAL_OPENROUTER = {"ROUTE_ALT_PROVIDER": "openrouter", "ROUTE_ALT_ALIAS": "claude-route-openrouter",
                   "ROUTE_ALT_MODEL": "vendor/model", "ROUTE_ALT_KEY": "sk-or-synthetic"}


class RequirementR1Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.object(route.Path, "home", return_value=self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))

    def start(self, provider="openrouter", dual=None, **flags):
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port": 12345}\n'
        configs = {"openrouter": OPENROUTER, "local": LOCAL, "cli": {}}
        with mock.patch.object(route, "run_owned", return_value=AUTH), \
             mock.patch.object(route, "check_connection", return_value=0), \
             mock.patch.object(route.shutil, "which", return_value="fixture"), \
             mock.patch.object(route, "provider_config", side_effect=lambda name: configs[name]), \
             mock.patch.object(route, "dual_provider_setup", return_value=dual), \
             mock.patch.object(route.subprocess, "Popen", return_value=proxy):
            with route.routed_session(provider, **flags) as env:
                return dict(env)

    def test_ordinary_openrouter_session_uses_the_stable_protocol(self):
        env = self.start()
        self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")

    def test_dual_session_with_an_openrouter_alternate_uses_the_stable_protocol(self):
        env = self.start("local", dual=DUAL_OPENROUTER)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")

    def test_compat_mode_remains_available_as_an_extra_option(self):
        env = self.start(openrouter_compat=True)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")

    def test_the_requirement_ledger_and_regression_comment_stay_in_place(self):
        ledger = (ROOT / "docs" / "USER-REQUIREMENTS.ko.md").read_text(encoding="utf-8")
        self.assertIn("# 사용자 요구사항 원장", ledger)
        self.assertIn("R1. 옵션 없이 지정 모델(OpenRouter)로 라우팅", ledger)
        self.assertIn("e075542", ledger)
        source = (ROOT / "openrouterAdvisor.py").read_text(encoding="utf-8")
        self.assertIn("User requirement R1", source)


if __name__ == "__main__":
    unittest.main()
