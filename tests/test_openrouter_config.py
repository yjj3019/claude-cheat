"""OpenRouter configuration uses synthetic keys and disposable files only."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("openrouter_config_route", Path(__file__).resolve().parents[1] / "openrouterAdvisor.py")
route = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route)


class OpenRouterConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.object(route.Path, "home", return_value=self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))

    def write(self, model="fixture/model-a", key="sk-or-synthetic-key"):
        (self.root / ".env.openrouter").write_text(
            f"ANTHROPIC_AUTH_TOKEN={key}\nOPENROUTER_MODEL={model}\n", encoding="utf-8")

    def test_explicit_provider_models_and_suffixes_are_not_deepseek_specific(self):
        for model in ("vendor-a/model-a", "vendor-b/model-b:free", "~vendor-c/latest", "@preset/code"):
            with self.subTest(model=model):
                self.write(model)
                self.assertEqual(route.provider_config("openrouter"), {"SONNET_PROVIDER": "openrouter",
                    "OPENROUTER_API_KEY": "sk-or-synthetic-key", "OPENROUTER_MODEL": model})

    def test_missing_empty_placeholder_and_malformed_models_have_no_default(self):
        for model in ("", "REPLACE_WITH_OPENROUTER_MODEL_ID", "YOUR_MODEL", "vendor/model with spaces", "vendor/model\x00", "x" * 257):
            with self.subTest(model=model):
                self.write(model)
                with self.assertRaises(route.OpenRouterConfigError) as caught:
                    route.provider_config("openrouter")
                self.assertIn("OPENROUTER_MODEL", str(caught.exception))
                self.assertNotIn("sk-or-synthetic-key", str(caught.exception))
        (self.root / ".env.openrouter").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-synthetic-key\n", encoding="utf-8")
        with self.assertRaises(route.OpenRouterConfigError):
            route.provider_config("openrouter")

    def test_missing_file_and_invalid_key_are_sanitized(self):
        with self.assertRaises(route.OpenRouterConfigError):
            route.provider_config("openrouter")
        self.write(key="private-key-marker")
        with self.assertRaises(route.OpenRouterConfigError) as caught:
            route.provider_config("openrouter")
        self.assertNotIn("private-key-marker", str(caught.exception))

    def test_example_exposes_both_required_settings(self):
        example = Path(SPEC.origin).parent / ".env.openrouter.example"
        values = route.read_env(example)
        self.assertEqual(set(values), {"ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"})
        sample = example.read_text(encoding="utf-8").replace("YOUR_OPENROUTER_API_KEY", "sk-or-synthetic-key").replace("REPLACE_WITH_OPENROUTER_MODEL_ID", "fixture/model-a")
        (self.root / ".env.openrouter").write_text(sample, encoding="utf-8")
        self.assertEqual(route.provider_config("openrouter")["OPENROUTER_MODEL"], "fixture/model-a")

    def test_model_and_key_are_proxy_only_and_saved_model_override_is_removed(self):
        self.write()
        os.environ.update(OPENROUTER_MODEL="ignored/environment-model", ANTHROPIC_DEFAULT_SONNET_MODEL="fixture/model-a")
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}\n'
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with mock.patch.object(route, "run_owned", return_value=auth), mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
            with route.routed_session("openrouter") as env:
                self.assertNotIn("OPENROUTER_MODEL", env)
                self.assertNotIn("OPENROUTER_API_KEY", env)
                self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", env)
                self.assertEqual(spawn.call_args.kwargs["env"]["OPENROUTER_MODEL"], "fixture/model-a")
                self.assertEqual(spawn.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "sk-or-synthetic-key")
        proxy.terminate.assert_called_once()

    def test_missing_model_never_starts_proxy_or_falls_back_to_plan(self):
        self.write(model="")
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with mock.patch.object(route, "run_owned", return_value=auth), mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), mock.patch.object(route.subprocess, "Popen") as spawn:
            with self.assertRaises(route.OpenRouterConfigError):
                with route.routed_session("openrouter"):
                    self.fail("Invalid configuration started a session")
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
