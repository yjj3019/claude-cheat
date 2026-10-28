"""OpenRouter configuration uses synthetic keys and disposable files only."""
import contextlib
import io
import json
import importlib.util
import os
import runpy
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

    def test_config_error_message_points_at_the_web_console(self):
        with self.assertRaises(route.OpenRouterConfigError) as caught:
            route.provider_config("openrouter")
        message = str(caught.exception)
        self.assertIn("OpenClaudeDashboard.ps1", message)
        self.assertIn("127.0.0.1:8787", message)

    def write_yaml(self, text):
        (self.root / "openclaude.config.yaml").write_text(text, encoding="utf-8")

    def test_settings_are_read_from_the_unified_file(self):
        self.write_yaml('openrouter:\n  model: "vendor/yaml-model"\n  api_key: "sk-or-synthetic-key"\n')
        self.assertEqual(route.provider_config("openrouter"), {"SONNET_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or-synthetic-key", "OPENROUTER_MODEL": "vendor/yaml-model"})

    def test_the_unified_file_wins_over_a_legacy_file_for_the_same_service(self):
        self.write("vendor/legacy-model")
        self.write_yaml('openrouter:\n  model: "vendor/yaml-model"\n  api_key: "sk-or-synthetic-key"\n')
        self.assertEqual(route.provider_config("openrouter")["OPENROUTER_MODEL"], "vendor/yaml-model")

    def test_a_file_with_only_another_service_still_reads_the_legacy_openrouter_file(self):
        self.write("vendor/legacy-model")
        self.write_yaml('local:\n  model: "m"\n')
        self.assertEqual(route.provider_config("openrouter")["OPENROUTER_MODEL"], "vendor/legacy-model")

    def test_a_broken_unified_file_is_a_sanitized_config_error(self):
        self.write_yaml('openrouter:\n  model: "vendor/m"\n  api_key: sk-or-synthetic-key\n  extra: &anchor 1\n')
        with self.assertRaises(route.OpenRouterConfigError) as caught:
            route.provider_config("openrouter")
        self.assertNotIn("sk-or-synthetic-key", str(caught.exception))
        self.assertIn("openclaude.config.yaml", str(caught.exception))

    def test_an_undecodable_unified_file_is_a_config_error_not_a_crash(self):
        (self.root / "openclaude.config.yaml").write_bytes(b'openrouter:\n  model: "\xff"\n')
        with self.assertRaises(route.OpenRouterConfigError):
            route.provider_config("openrouter")

    def test_launcher_config_failure_prints_setup_guidance(self):
        with mock.patch.object(route, "launch", side_effect=route.OpenRouterConfigError()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = route.main([])
        self.assertEqual(code, 1)
        text = stderr.getvalue()
        self.assertIn("[setup required]", text)
        self.assertIn("OpenClaudeDashboard.ps1", text)
        payload = json.loads([line for line in text.splitlines() if line.startswith("{")][-1])
        self.assertEqual(payload["action"], "open_dashboard")
        self.assertEqual(payload["dashboard"], "http://127.0.0.1:8787/")
        self.assertEqual(payload["code"], "openrouter_configuration_invalid")
        self.assertEqual(payload["kind"], "OpenRouterConfigError")

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
        with mock.patch.object(route, "run_owned", return_value=auth), mock.patch.object(route, "check_connection", return_value=0), mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
            with route.routed_session("openrouter") as env:
                self.assertNotIn("OPENROUTER_MODEL", env)
                self.assertNotIn("OPENROUTER_API_KEY", env)
                self.assertNotIn("ANTHROPIC_DEFAULT_SONNET_MODEL", env)
                self.assertEqual(spawn.call_args.kwargs["env"]["OPENROUTER_MODEL"], "fixture/model-a")
                self.assertEqual(spawn.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "sk-or-synthetic-key")
        proxy.terminate.assert_called_once()

    def test_launcher_lifecycle_marker_is_written_beside_the_route_log(self):
        (self.root / ".route.log").write_text("", encoding="utf-8")
        route.log_route_marker("launcher start pid=1")
        text = (self.root / ".route.log").read_text(encoding="utf-8")
        self.assertRegex(text.strip(), r"^\S+Z --- launcher start pid=1 ---$")

    def test_only_explicit_openrouter_compat_pins_stable_protocol(self):
        self.write()
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}\n'
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        for flags, mode in (({}, "ordinary"), ({"openrouter_compat": True}, "compat")):
            with self.subTest(mode=mode):
                with mock.patch.object(route, "run_owned", return_value=auth), \
                     mock.patch.object(route, "check_connection", return_value=0), \
                     mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), \
                     mock.patch.object(route.subprocess, "Popen", return_value=proxy):
                    with route.routed_session("openrouter", **flags) as env:
                        if flags:
                            self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
                            self.assertEqual(env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
                        else:
                            self.assertNotIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", env)
                            self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", env)
                        # Thinking stays a per-request decision, so Plan fallback keeps its own settings.
                        self.assertNotIn("MAX_THINKING_TOKENS", env)

    def test_missing_model_never_starts_proxy_or_falls_back_to_plan(self):
        self.write(model="")
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with mock.patch.object(route, "run_owned", return_value=auth), mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), mock.patch.object(route.subprocess, "Popen") as spawn:
            with self.assertRaises(route.OpenRouterConfigError):
                with route.routed_session("openrouter"):
                    self.fail("Invalid configuration started a session")
            spawn.assert_not_called()


    def test_openrouter_compat_isolates_credentials_skips_plan_and_reuses_check_snapshot(self):
        self.write()
        os.environ.update(ANTHROPIC_API_KEY="private-api-marker", CLAUDE_CODE_OAUTH_TOKEN="private-oauth-marker",
                          OPENROUTER_ROUTE_COMPAT="inherited", LOCAL_ROUTE_COMPAT="inherited")
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}\n'
        captured = {}
        def check(provider, config):
            captured.update(config)
            self.write(model="fixture/changed-after-check")
            return 0
        with mock.patch.object(route, "run_owned", side_effect=AssertionError("Plan auth forbidden")), \
             mock.patch.object(route, "provider_config", wraps=route.provider_config) as config_reader, \
             mock.patch.object(route, "check_connection", side_effect=check) as checked, \
             mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), \
             mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
            with route.routed_session("openrouter", openrouter_compat=True) as env:
                self.assertNotIn("OPENROUTER_API_KEY", env)
                self.assertNotIn("OPENROUTER_MODEL", env)
                self.assertNotIn("OPENROUTER_ROUTE_COMPAT", env)
                self.assertNotIn("LOCAL_ROUTE_COMPAT", env)
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Local-Route-Token: " + env["ANTHROPIC_AUTH_TOKEN"])
                self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-5-5")
                self.assertNotIn("MAX_THINKING_TOKENS", env)
                child_env = spawn.call_args.kwargs["env"]
                self.assertEqual(child_env["OPENROUTER_MODEL"], "fixture/model-a")
                self.assertEqual(child_env["OPENROUTER_API_KEY"], "sk-or-synthetic-key")
                self.assertEqual(child_env["OPENROUTER_ROUTE_COMPAT"], "1")
                self.assertEqual(child_env["LOCAL_ROUTE_ONLY"], "0")
                self.assertNotIn("LOCAL_ROUTE_COMPAT", child_env)
                self.assertNotIn("ANTHROPIC_API_KEY", child_env)
                self.assertEqual(spawn.call_args.args[0][-1], "--parent-pipe")
            config_reader.assert_called_once_with("openrouter")
            checked.assert_called_once_with("openrouter", config=captured)
        proxy.terminate.assert_called_once()
        proxy.stdin.close.assert_called_once()

    def test_preflight_failure_continues_all_modes_with_original_plan_policy(self):
        modes = [("openrouter", {}), ("local", {}), ("local", {"local_only": True}),
                 ("local", {"local_only": True, "local_compat": True}), ("openrouter", {"openrouter_compat": True})]
        for provider, flags in modes:
            with self.subTest(provider=provider, flags=flags):
                proxy = mock.Mock()
                proxy.stdout.readline.return_value = '{"port":12345}\n'
                auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
                config = {"SONNET_PROVIDER": provider, "OPENROUTER_MODEL": "fixture/model-a", "OPENROUTER_API_KEY": "sk-or-synthetic-key"}
                order = []
                def checked(*args, **kwargs):
                    order.append("check")
                    return 1
                def spawned(*args, **kwargs):
                    order.append("proxy")
                    return proxy
                output = io.StringIO()
                with mock.patch.object(route, "provider_config", return_value=config) as config_reader, \
                     mock.patch.object(route, "run_owned", return_value=auth) as plan_auth, \
                     mock.patch.object(route, "check_connection", side_effect=checked), \
                     mock.patch.object(route.shutil, "which", return_value="synthetic-executable"), \
                     mock.patch.object(route.subprocess, "Popen", side_effect=spawned), contextlib.redirect_stdout(output):
                    with route.routed_session(provider, **flags):
                        order.append("session")
                self.assertEqual(order, ["check", "proxy", "session"])
                config_reader.assert_called_once_with(provider)
                diagnostic = json.loads(output.getvalue())
                self.assertEqual(diagnostic["event"], "provider_connection_unavailable")
                self.assertEqual(diagnostic["action"], "continue")
                isolated = bool(flags.get("local_only") or flags.get("openrouter_compat"))
                self.assertEqual(diagnostic["plan_fallback"], not isolated)
                self.assertEqual(plan_auth.call_count, 0 if isolated else 1)

    def test_connection_check_is_provider_only_and_does_not_reread_snapshot(self):
        config = {"SONNET_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-synthetic-key", "OPENROUTER_MODEL": "fixture/model-a"}
        os.environ.update(ANTHROPIC_API_KEY="private-api-marker", CLAUDE_CODE_OAUTH_TOKEN="private-oauth-marker", LOCAL_LLM_API_KEY="private-local-marker")
        with mock.patch.object(route, "provider_config", side_effect=AssertionError("Snapshot must not be reread")), \
             mock.patch.object(route.shutil, "which", return_value="synthetic-node"), \
             mock.patch.object(route.subprocess, "call", return_value=1) as child:
            self.assertEqual(route.check_connection("openrouter", config=config), 1)
        self.assertEqual(child.call_args.args[0][-1], "--check-connection")
        self.assertEqual(child.call_args.kwargs["env"]["OPENROUTER_MODEL"], "fixture/model-a")
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "LOCAL_LLM_API_KEY"):
            self.assertNotIn(name, child.call_args.kwargs["env"])

    def test_connection_process_failure_is_safe_and_nonfatal(self):
        self.write()
        output = io.StringIO()
        with mock.patch.object(route.shutil, "which", return_value="synthetic-node"), \
             mock.patch.object(route.subprocess, "call", side_effect=OSError("private-error-marker")), contextlib.redirect_stdout(output):
            self.assertEqual(route.check_connection("openrouter"), 1)
        diagnostic = json.loads(output.getvalue())
        self.assertFalse(diagnostic["connection_check"])
        self.assertEqual(diagnostic["reason"], "connection_check_failed")
        self.assertNotIn("private-error-marker", output.getvalue())
        self.assertNotIn("sk-or-synthetic-key", output.getvalue())

    def test_openrouter_compat_invalid_combinations_fail_before_startup(self):
        for args in (["--provider", "local", "--openrouter-compat"], ["--openrouter-compat", "--local-mode"],
                     ["--openrouter-compat", "--local-compat"], ["--openrouter-compat", "--check-local"],
                     ["--openrouter-compat", "--diagnose-local"]):
            result = route.subprocess.run([route.sys.executable, SPEC.origin, *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--openrouter-compat requires --provider openrouter", result.stderr)
        for provider, flags in (("local", {}), ("openrouter", {"local_only": True}), ("openrouter", {"local_compat": True})):
            with self.assertRaises(ValueError):
                with route.routed_session(provider, openrouter_compat=True, **flags):
                    self.fail("Invalid session started")
            with self.assertRaises(ValueError):
                route.launch(provider, openrouter_compat=True, **flags)

    def test_openrouter_compat_launch_keeps_default_permissions_and_pins_route(self):
        pinned = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:12345", "ANTHROPIC_AUTH_TOKEN": "synthetic-proxy-token",
                  "ANTHROPIC_CUSTOM_HEADERS": "X-Local-Route-Token: synthetic-proxy-token",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5", "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5-1",
                  "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-5-5", "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-5-5"}
        output = io.StringIO()
        with mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), \
             mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304")), \
             mock.patch.object(route, "routed_session", return_value=contextlib.nullcontext(pinned)) as session, \
             mock.patch.object(route.subprocess, "call", return_value=0) as child, contextlib.redirect_stdout(output):
            self.assertEqual(route.launch("openrouter", openrouter_compat=True), 0)
        session.assert_called_once_with("openrouter", openrouter_compat=True, debug=False)
        command = child.call_args.args[0]
        self.assertNotIn("--safe-mode", command)
        self.assertNotIn("--disallowedTools", command)
        self.assertNotIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["apiKeyHelper"], "")
        self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "8192")
        self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], "synthetic-proxy-token")
        self.assertEqual(settings["env"]["OPENROUTER_API_KEY"], "")
        self.assertEqual(settings["env"]["OPENROUTER_MODEL"], "")
        self.assertEqual(settings["env"][route.PRO_SESSION_GATE], "")
        self.assertIn("No Plan fallback", output.getvalue())
        self.assertIn("OpenRouter compatibility mode", output.getvalue())

    def test_openrouter_startup_error_uses_provider_label_without_private_values(self):
        with mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.303")), \
             mock.patch.object(route.shutil, "which", return_value="synthetic-cli"):
            with self.assertRaises(route.OpenRouterStartupError) as caught:
                route.launch("openrouter", openrouter_compat=True)
        self.assertEqual(caught.exception.code, "openrouter_cli_unsupported")
        self.assertIn("OpenRouter", str(caught.exception))
        self.assertNotIn("Local", str(caught.exception))


    def test_failed_check_still_enters_cli_after_proxy_in_every_launch_mode(self):
        modes = [("openrouter", {}), ("local", {}), ("local", {"local_only": True}),
                 ("local", {"local_only": True, "local_compat": True}), ("openrouter", {"openrouter_compat": True})]
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        for provider, flags in modes:
            with self.subTest(provider=provider, flags=flags):
                proxy = mock.Mock()
                proxy.stdout.readline.return_value = '{"port":12345}\n'
                config = {"SONNET_PROVIDER": provider, "OPENROUTER_MODEL": "fixture/model-a", "OPENROUTER_API_KEY": "sk-or-synthetic-key"}
                order = []
                def checked(*args, **kwargs):
                    order.append("check")
                    return 1
                def spawned(*args, **kwargs):
                    order.append("proxy")
                    return proxy
                def entered(*args, **kwargs):
                    order.append("cli")
                    return 0
                with mock.patch.object(route, "provider_config", return_value=config), \
                     mock.patch.object(route, "run_owned", return_value=auth), \
                     mock.patch.object(route, "check_connection", side_effect=checked), \
                     mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), \
                     mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304")), \
                     mock.patch.object(route.subprocess, "Popen", side_effect=spawned), \
                     mock.patch.object(route.subprocess, "call", side_effect=entered), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(route.launch(provider, **flags), 0)
                self.assertEqual(order, ["check", "proxy", "cli"])

    def test_openrouter_compat_cleanup_preparation_failure_stops_before_cli(self):
        err = io.StringIO()
        with mock.patch.object(route.sys, "argv", [str(SPEC.origin), "--openrouter-compat"]), \
             mock.patch.object(route.localProcessTree, "ensure_windows_local_tree", side_effect=RuntimeError("private-error-marker")), \
             mock.patch("shutil.which", side_effect=AssertionError("CLI must not start before cleanup ownership")), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(SPEC.origin), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)
        diagnostic = json.loads(err.getvalue())
        self.assertEqual(diagnostic["code"], "openrouter_process_cleanup_failed")
        self.assertNotIn("private-error-marker", err.getvalue())
        self.assertNotIn("Local", diagnostic["message"])


if __name__ == "__main__":
    unittest.main()
