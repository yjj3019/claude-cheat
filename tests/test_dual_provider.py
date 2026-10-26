"""Dual provider routing: the opt-in routing section, the launcher env it produces, and the
guarantee that the alternate provider's credential reaches the proxy only, never the CLI."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import routeConfig


SPEC = importlib.util.spec_from_file_location("dual_provider_route", Path(__file__).resolve().parents[1] / "openrouterAdvisor.py")
route = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route)

DUAL = "routing:\n  dual_provider: true\n"
BOTH = ('openrouter:\n  model: "vendor/new"\n  api_key: "sk-or-dual0000000000000000000000000000000000000000"\n'
        'local:\n  model: "local-model"\n  api_key: "local-key"\n')


def write_config(root, text):
    (root / "openclaude.config.yaml").write_text(text, encoding="utf-8")


class DualProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(route, "check_connection", return_value=0))
        self.enterContext(mock.patch.object(route.localProcessTree, "ensure_windows_local_tree"))
        self.proxy = mock.Mock()
        self.proxy.stdout.readline.return_value = '{"port":12345}'

    def spawn(self):
        return contextlib.ExitStack()

    def run_session(self, provider, **kwargs):
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}'
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with mock.patch.object(route, "run_owned", return_value=auth), \
             mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), \
             mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn, \
             mock.patch.object(route.Path, "home", return_value=self.root):
            with route.routed_session(provider, **kwargs) as env:
                return env, spawn.call_args.kwargs["env"]


class RoutingSectionTests(DualProviderTestCase):
    def test_routing_section_parses_and_rejects_unknown_keys(self):
        doc = routeConfig.parse(DUAL)
        self.assertEqual(doc.sections["routing"]["values"], {"dual_provider": "true"})
        broken = routeConfig.parse("routing:\n  auto: true\n")
        self.assertIsNotNone(broken.first_error("routing"))

    def test_dashboard_style_save_leaves_the_routing_section_untouched(self):
        text = DUAL + BOTH
        updated = routeConfig.update_text(text, "openrouter", {"model": "vendor/next"})
        doc = routeConfig.parse(updated)
        self.assertEqual(doc.sections["routing"]["values"], {"dual_provider": "true"})
        self.assertEqual(doc.sections["openrouter"]["values"]["model"], "vendor/next")

    def test_the_shipped_example_documents_the_new_section(self):
        example = routeConfig.parse((route.routeConfig.ROOT / route.routeConfig.EXAMPLE_NAME).read_text(encoding="utf-8"))
        self.assertIn("routing", example.sections)
        self.assertEqual(example.sections["routing"]["values"].get("dual_provider"), "false")


class DualSetupTests(DualProviderTestCase):
    def test_off_or_absent_stays_single_provider(self):
        write_config(self.root, BOTH)
        self.assertIsNone(route.dual_provider_setup("openrouter"))
        write_config(self.root, BOTH + DUAL.replace("true", "false"))
        self.assertIsNone(route.dual_provider_setup("openrouter"))

    def test_enabled_with_both_configured_exposes_the_other_provider_without_its_primary_marker(self):
        write_config(self.root, BOTH + DUAL)
        fragment = route.dual_provider_setup("openrouter")
        self.assertEqual(fragment["ROUTE_ALT_PROVIDER"], "local")
        self.assertEqual(fragment["ROUTE_ALT_ALIAS"], "claude-route-local")
        self.assertEqual(fragment["ROUTE_ALT_URL"], "http://127.0.0.1:11434/v1/messages")
        self.assertEqual(fragment["ROUTE_ALT_MODEL"], "local-model")
        self.assertEqual(fragment["ROUTE_ALT_KEY"], "local-key")
        self.assertEqual(fragment["ROUTE_ALT_ALLOW_REMOTE"], "false")
        self.assertNotIn("SONNET_PROVIDER", fragment)
        fragment = route.dual_provider_setup("local")
        self.assertEqual(fragment["ROUTE_ALT_PROVIDER"], "openrouter")
        self.assertEqual(fragment["ROUTE_ALT_ALIAS"], "claude-route-openrouter")
        self.assertEqual(fragment["ROUTE_ALT_MODEL"], "vendor/new")
        self.assertTrue(fragment["ROUTE_ALT_KEY"].startswith("sk-or-"))

    def test_a_missing_or_broken_other_provider_keeps_the_session_single_provider(self):
        write_config(self.root, DUAL + 'openrouter:\n  model: "vendor/new"\n  api_key: "sk-or-dual0000000000000000000000000000000000000000"\n')
        self.assertIsNone(route.dual_provider_setup("openrouter"), "no local section means no alternate")
        write_config(self.root, DUAL + BOTH.replace('  model: "local-model"', '  model: ""'))
        self.assertIsNone(route.dual_provider_setup("openrouter"), "an unusable local service is not exposed")


class DualSessionTests(DualProviderTestCase):
    def test_ordinary_session_carries_the_picker_entry_and_the_proxy_gets_the_alternate(self):
        write_config(self.root, BOTH + DUAL)
        env, proxy_env = self.run_session("openrouter")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], "claude-route-local")
        self.assertIn("Local (local-model)", env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"])
        self.assertEqual(proxy_env["ROUTE_ALT_PROVIDER"], "local")
        self.assertEqual(proxy_env["ROUTE_ALT_ALIAS"], "claude-route-local")
        self.assertEqual(proxy_env["ROUTE_ALT_KEY"], "local-key")
        # The alternate credential is a proxy secret: the CLI never sees it.
        for name in ("ROUTE_ALT_PROVIDER", "ROUTE_ALT_KEY", "ROUTE_ALT_URL"):
            self.assertNotIn(name, env)
        for name in ("ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"):
            self.assertNotIn(name, proxy_env)

    def test_special_modes_never_get_a_second_provider(self):
        write_config(self.root, BOTH + DUAL)
        env, proxy_env = self.run_session("local", local_only=True)
        for name in ("ANTHROPIC_CUSTOM_MODEL_OPTION", "ROUTE_ALT_PROVIDER"):
            self.assertNotIn(name, env)
            self.assertNotIn(name, proxy_env)
        with self.assertRaises(ValueError):
            self.run_session("local", openrouter_compat=True)

    def test_a_user_exported_stale_hook_is_cleared_for_single_provider_sessions(self):
        write_config(self.root, BOTH)
        os.environ.update({"ANTHROPIC_CUSTOM_MODEL_OPTION": "stale-alias",
                           "ROUTE_ALT_KEY": "stale-secret"})
        env, proxy_env = self.run_session("openrouter")
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION", env)
        self.assertNotIn("ROUTE_ALT_KEY", env)
        self.assertNotIn("ROUTE_ALT_KEY", proxy_env)


if __name__ == "__main__":
    unittest.main()
