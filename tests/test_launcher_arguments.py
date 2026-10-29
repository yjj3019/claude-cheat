"""Native host arguments and CLI backend boundaries, without using real credentials."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import openrouterAdvisor as route


class LauncherArgumentsTests(unittest.TestCase):
    def test_utf8_encoding_preserves_argument_boundaries(self):
        values = ["--resume", "session id", "--plugin-dir", "C:\\fixture plugin\\", "--mcp-config",
                  json.dumps({"mcpServers": {"fixture": {"args": ["a b", 'quoted"']}}}), "", "??"]
        encoded = base64.b64encode(json.dumps(values, ensure_ascii=False).encode("utf-8")).decode("ascii")
        self.assertEqual(route.decode_claude_args(encoded), values)
        for invalid in ("%%%", base64.b64encode(b'{}').decode(), base64.b64encode(b'[1]').decode()):
            with self.subTest(invalid=invalid), self.assertRaises(route.LauncherArgumentsError):
                route.decode_claude_args(invalid)

    def test_conflicting_wrapper_modes_fail_before_startup(self):
        for values, kwargs in ((["--resume", "id"], {"local_only": True}),
                               (["--settings=private"], {"compat": True}),
                               (["--output-format", "json"], {"print_prompt": "OK"}),
                               (["auth", "status"], {"print_prompt": "OK"})):
            with self.subTest(values=values), self.assertRaises(route.LauncherArgumentsError):
                route.validate_claude_args(values, **kwargs)

    def test_management_bypasses_provider_auth_and_proxy(self):
        with mock.patch.object(route.shutil, "which", return_value="fixture.cmd"), \
             mock.patch.object(route.routeCli, "resolve_command", return_value=["node", "fixture.js"]), \
             mock.patch.object(route, "configured_provider", side_effect=AssertionError("provider not needed")), \
             mock.patch.object(route, "routed_session", side_effect=AssertionError("proxy not needed")), \
             mock.patch.object(route.subprocess, "call", return_value=0) as called:
            for args in (["mcp", "list"], ["attach", "session id"], ["logs", "session id"],
                         ["respawn", "session id"], ["stop", "session id"], ["kill", "session id"],
                         ["rm", "session id"], ["purge"], ["gateway", "--help"],
                         ["setup-token"], ["upgrade"]):
                with self.subTest(args=args):
                    self.assertEqual(route.launch(cli_args=args), 0)
                    self.assertEqual(called.call_args.args[0], ["node", "fixture.js"] + args)
                    self.assertEqual(called.call_args.kwargs["env"], dict(os.environ))

    def test_frontend_preferences_survive_ordinary_and_compat_only(self):
        preferences = {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "CLAUDE_CODE_FORK_SUBAGENT": "0",
            "CLAUDE_CODE_FORWARD_SUBAGENT_TEXT": "1", "CLAUDE_CODE_ENABLE_TASKS": "0",
            "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1", "CLAUDE_CODE_TASK_LIST_ID": "shared-task-list",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0", "CLAUDE_CODE_DISABLE_AGENT_VIEW": "0",
            "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND": "1", "CLAUDE_CODE_AUTO_CONNECT_IDE": "true",
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": "C:/fixture cache", "CLAUDE_CODE_PLUGIN_DIRS": "C:/one;C:/two",
            "CLAUDE_CODE_PLUGIN_DIR_WATCH": "0", "CLAUDE_CODE_PLUGIN_SEED_DIR": "C:/seed",
            "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1", "CLAUDE_CODE_SYNC_PLUGIN_INSTALL_TIMEOUT_MS": "25000",
            "CLAUDE_CODE_SYNC_SKILLS": "1", "CLAUDE_CODE_SYNC_SKILLS_INSTALL_TIMEOUT_MS": "12000",
            "CLAUDE_CODE_SYNC_SKILLS_WAIT_TIMEOUT_MS": "15000",
        }
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        modes = [("local", {}), ("openrouter", {}), ("cli", {}),
                 ("local", {"local_only": True, "local_compat": True}),
                 ("openrouter", {"openrouter_compat": True}), ("local", {"local_only": True})]
        for provider, flags in modes:
            for explicit in (False, True):
                with self.subTest(provider=provider, flags=flags, explicit=explicit):
                    proxy = mock.Mock()
                    proxy.stdout.readline.return_value = '{"port":12345}\n'
                    inherited = dict(preferences) if explicit else {}
                    inherited.update(ANTHROPIC_API_KEY="fixture-secret", CLAUDE_CODE_OAUTH_TOKEN="fixture-oauth",
                                     CLAUDE_CODE_USE_BEDROCK="1")
                    with tempfile.TemporaryDirectory() as directory, mock.patch.object(route, "ROOT", Path(directory)), \
                         mock.patch.object(route.Path, "home", return_value=Path(directory)), \
                         mock.patch.dict(os.environ, inherited, clear=True), \
                         mock.patch.object(route.shutil, "which", return_value="fixture"), \
                         mock.patch.object(route, "run_owned", return_value=auth), \
                         mock.patch.object(route, "dual_provider_setup", return_value=None), \
                         mock.patch.object(route, "check_connection", return_value=0), \
                         mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
                        with route._routed_session(provider, _config={"OPENROUTER_MODEL": "fixture/model"}, **flags) as env:
                            restricted = flags.get("local_only") and not flags.get("local_compat")
                            for name, value in preferences.items():
                                if restricted and name == "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS":
                                    self.assertEqual(env[name], "1")
                                elif restricted and name == "CLAUDE_CODE_AUTO_CONNECT_IDE":
                                    self.assertEqual(env[name], "false")
                                elif explicit and not restricted:
                                    self.assertEqual(env[name], value)
                                else:
                                    self.assertNotIn(name, env)
                                self.assertNotIn(name, spawn.call_args.kwargs["env"])
                            self.assertNotIn("ANTHROPIC_API_KEY", env)
                            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                            if flags:
                                self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    def test_native_agent_plugin_and_mcp_arguments_reach_compat_child(self):
        args = ["--agent", "reviewer", "--plugin-dir", "C:/fixture plugins", "--mcp-config", "fixture-mcp.json"]
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:12345", "ANTHROPIC_AUTH_TOKEN": "fixture-nonce",
               "ANTHROPIC_CUSTOM_HEADERS": "X-Local-Route-Token: fixture-nonce"}
        for provider, flags in (("local", {"local_only": True, "local_compat": True}),
                                ("openrouter", {"openrouter_compat": True})):
            with self.subTest(provider=provider), contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(route.shutil, "which", return_value="fixture"), \
                 mock.patch.object(route.routeCli, "resolve_command", return_value=["fixture"]), \
                 mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304")), \
                 mock.patch.object(route, "routed_session", return_value=contextlib.nullcontext(env)), \
                 mock.patch.object(route.subprocess, "call", return_value=0) as called:
                self.assertEqual(route.launch(provider, cli_args=args, **flags), 0)
                command = called.call_args.args[0]
                self.assertEqual(command[-len(args):], args)
                settings = json.loads(command[command.index("--settings") + 1])
                for key in ("disableAllHooks", "autoMemoryEnabled", "enabledPlugins"):
                    self.assertNotIn(key, settings)
                self.assertNotIn("--safe-mode", command)
                self.assertNotIn("--strict-mcp-config", command)

    def test_native_suffix_reaches_launcher_unchanged(self):
        values = ["--resume", "a b", "", '{"quoted":"value"}']
        with mock.patch.object(route, "launch", return_value=0) as launch:
            self.assertEqual(route.main(["--provider", "cli", "--"] + values), 0)
        self.assertEqual(launch.call_args.kwargs["cli_args"], values)

    def test_real_child_argv_preserves_spaces_empty_json_and_backslashes(self):
        values = ["--resume", "session id", "", '{"mcpServers":{"fixture":{"args":["a b"]}}}', "C:\\space here\\", "??"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "argv.json"
            receiver = root / "receiver.py"
            receiver.write_text("import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False),encoding='utf-8')", encoding="utf-8")
            with mock.patch.object(route.shutil, "which", return_value="fixture.cmd"), \
                 mock.patch.object(route.routeCli, "resolve_command", return_value=[sys.executable, str(receiver), str(output)]), \
                 mock.patch.object(route, "routed_session", return_value=contextlib.nullcontext(dict(os.environ))), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(route.launch("openrouter", cli_args=values), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), ["--model", "sonnet"] + values)

    def test_cli_bridge_config_stays_out_of_frontend(self):
        values = {"SONNET_PROVIDER": "cli", "CLI_KIND": "codex", "CLI_MODEL": "requested", "CLI_BINARY": "", "CLI_TIMEOUT_S": "60"}
        backend = mock.Mock(protocol_model="cli/codex")
        with mock.patch.object(route, "provider_config", return_value=values), \
             mock.patch.object(route.cliBackend, "serve", return_value=contextlib.nullcontext((backend, "http://127.0.0.1:1234/v1/messages", "synthetic-bridge-token"))), \
             mock.patch.object(route, "_routed_session", return_value=contextlib.nullcontext({"front": "only"})) as front:
            with route.routed_session("cli") as env:
                self.assertEqual(env, {"front": "only"})
        config = front.call_args.kwargs["_config"]
        self.assertEqual(config["LOCAL_LLM_MODEL"], "cli/codex")
        self.assertEqual(config["LOCAL_LLM_API_KEY"], "synthetic-bridge-token")
        self.assertIs(front.call_args.kwargs["_backend"], backend)

    def test_cli_proxy_environment_separates_backend_and_frontend(self):
        config = {"SONNET_PROVIDER": "cli", "CLI_KIND": "codex", "CLI_MODEL": "requested", "CLI_BINARY": "", "CLI_TIMEOUT_S": "60",
                  "LOCAL_LLM_URL": "http://127.0.0.1:1234/v1/messages", "LOCAL_LLM_MODEL": "cli/codex", "LOCAL_LLM_API_KEY": "synthetic-bridge-token"}
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}\n'
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(route, "ROOT", Path(directory)), \
             mock.patch.dict(os.environ, {"USERPROFILE": directory, "CLI_KIND": "codex", "CLI_MODEL": "requested", "CLI_BINARY": "fixture", "CLI_TIMEOUT_S": "60"}, clear=True), \
             mock.patch.object(route.shutil, "which", return_value="fixture"), \
             mock.patch.object(route, "run_owned", return_value=auth), \
             mock.patch.object(route, "check_cli_backend", return_value=0), \
             mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
            with route._routed_session("cli", _config=config, _backend=mock.Mock()) as front:
                for key in config:
                    self.assertNotIn(key, front)
                self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", front)
                self.assertNotIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", front)
                self.assertNotIn("CLAUDE_CONFIG_DIR", front)
                self.assertEqual(spawn.call_args.kwargs["env"]["LOCAL_LLM_API_KEY"], "synthetic-bridge-token")
                self.assertEqual(spawn.call_args.kwargs["env"]["SONNET_PROVIDER"], "cli")
        proxy.terminate.assert_called_once()

    def test_cli_saved_default_resolves_three_configured_services(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(route, "ROOT", Path(directory)), mock.patch.dict(os.environ, {}, clear=True):
            store = route.routeConfig.Store(Path(directory) / "openclaude.config.yaml")
            store.write_provider("openrouter", {"ANTHROPIC_AUTH_TOKEN": "sk-or-synthetic-key", "OPENROUTER_MODEL": "fixture/model"})
            store.write_provider("local", {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODEL": "fixture/local"})
            store.write_provider("cli", {"CLI_KIND": "codex"})
            with self.assertRaises(route.ProviderChoiceError) as caught:
                route.configured_provider()
            self.assertEqual(caught.exception.providers, ["openrouter", "local", "cli"])
            store.write_provider("routing", {"ROUTE_DEFAULT_PROVIDER": "cli"})
            self.assertEqual(route.configured_provider(), "cli")
            self.assertEqual(route.provider_config("cli")["CLI_KIND"], "codex")

    def test_login_errors_are_actionable_and_sanitized(self):
        with mock.patch.object(route, "launch", side_effect=route.PlanLoginError()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(route.main(["--provider", "cli"]), 1)
        result = json.loads(stderr.getvalue().splitlines()[0])
        self.assertEqual(result["event"], "claude_login_required")
        self.assertEqual(result["command"], "claude auth login")
        self.assertIn("claude auth status", stderr.getvalue())
        self.assertIn("OpenClaude.ps1", stderr.getvalue())
        self.assertIn("로그인이 필요", stderr.getvalue())
        with mock.patch.object(route, "launch", side_effect=route.PlanLoginError()), \
             mock.patch.dict(os.environ, {"CLAUDE_CHEAT_HUMAN_GUIDANCE": "1"}), \
             contextlib.redirect_stderr(io.StringIO()) as human_output:
            self.assertEqual(route.main(["--provider", "cli"]), 1)
        self.assertNotIn('"event"', human_output.getvalue())
        self.assertIn("claude auth login", human_output.getvalue())


if __name__ == "__main__":
    unittest.main()
