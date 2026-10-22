"""Local metadata checks use disposable loopback HTTP and synthetic credentials only."""

import contextlib
import importlib.util
import io
import json
import os
import runpy
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest import mock
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SPEC = importlib.util.spec_from_file_location("local_config_route", Path(__file__).resolve().parents[1] / "openrouterAdvisor.py")
route = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route)


class LocalTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.enterContext(mock.patch.object(route, "ROOT", self.root))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(route, "read_key", side_effect=AssertionError("OpenRouter key access")))
        self.enterContext(mock.patch.object(route, "run_owned", side_effect=AssertionError("Claude invocation")))

    def configure(self, **values):
        os.environ.update({"LOCAL_LLM_MODEL": "synthetic-model", **values})


class LocalConfigTests(LocalTestCase):
    def test_default_loopback_and_file_environment_precedence(self):
        (self.root / ".env.local-llm").write_text("LOCAL_LLM_MODEL=file-model\nLOCAL_LLM_API_KEY=file-key\n", encoding="utf-8")
        config = route.provider_config("local")
        self.assertEqual(config["LOCAL_LLM_URL"], "http://127.0.0.1:11434/v1/messages")
        self.assertEqual(config["LOCAL_LLM_MODELS_URL"], "http://127.0.0.1:11434/v1/models")
        self.assertEqual(config["LOCAL_LLM_MODEL"], "file-model")
        self.configure(LOCAL_LLM_API_KEY="process-key")
        self.assertEqual(route.provider_config("local")["LOCAL_LLM_API_KEY"], "process-key")

    def test_remote_https_requires_explicit_opt_in(self):
        self.configure(LOCAL_LLM_URL="https://llm.invalid/v1/messages")
        with self.assertRaises(ValueError):
            route.provider_config("local")
        os.environ["LOCAL_LLM_ALLOW_REMOTE"] = "true"
        self.assertEqual(route.provider_config("local")["LOCAL_LLM_ALLOW_REMOTE"], "true")
        os.environ["LOCAL_LLM_URL"] = "http://llm.invalid/v1/messages"
        with self.assertRaises(ValueError):
            route.provider_config("local")

    def test_invalid_endpoint_components(self):
        for url in ("http://127.0.0.1:bad/v1/messages", "http://user:pass@127.0.0.1/v1/messages",
                    "http://127.0.0.1:0/v1/messages", "http://@127.0.0.1/v1/messages",
                    " http://127.0.0.1/v1/messages", "http://127.0.0.1/v1/messages\n",
                    "http://127.0.0.1/\tv1/messages", "http://127.0.0.1/v1/mess ages",
                    "http://127.0.0.1/v1/messages?x=1", "http://127.0.0.1/v1/messages#fragment",
                    "http://127.0.0.1/other", "ftp://127.0.0.1/v1/messages"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                route.local_endpoint(url, False)

    def test_internal_http_exception_is_limited_to_approved_origin(self):
        url = "http://192.0.2.10:4000/v1/messages"
        self.configure(LOCAL_LLM_URL=url)
        with self.assertRaises(ValueError):
            route.provider_config("local")
        self.configure(LOCAL_LLM_HTTP_URL=url)
        config = route.provider_config("local")
        self.assertEqual(config["LOCAL_LLM_URL"], url)
        self.assertEqual(config["LOCAL_LLM_MODELS_URL"], "http://192.0.2.10:4000/v1/models")
        self.assertEqual(config["LOCAL_LLM_ALLOW_REMOTE"], "false")
        for invalid in (url.replace(".10", ".11"), url.replace(":4000", ":4001"),
                        url.replace(":4000", ""), url.replace("http://", "http://user:password@"),
                        url + "?q=1", url + "#fragment", url.replace("/v1/messages", "/other"),
                        url.replace("192.0.2.10", "0xc000020a"),
                        url.replace("/v1/messages", "/other/../v1/messages")):
            for allow_remote in (False, True):
                with self.subTest(url=invalid, allow_remote=allow_remote), self.assertRaises(ValueError):
                    route.local_endpoint(invalid, allow_remote, url)
        self.configure(LOCAL_LLM_MODELS_URL="http://192.0.2.11:4000/v1/models")
        with self.assertRaises(ValueError):
            route.provider_config("local")

    def test_http_pin_cannot_bypass_other_origins_or_https_opt_in(self):
        url = "http://192.0.2.10:4000/v1/messages"
        (self.root / ".env.local-llm").write_text(
            "LOCAL_LLM_MODEL=synthetic-model\nLOCAL_LLM_URL=" + url + "\nLOCAL_LLM_HTTP_URL=" + url + "\n")
        self.assertEqual(route.provider_config("local")["LOCAL_LLM_HTTP_URL"], url)
        self.configure(LOCAL_LLM_URL=url.replace(":4000", ":4001"))
        with self.assertRaises(ValueError):
            route.provider_config("local")
        https = "https://llm.invalid/v1/messages"
        self.configure(LOCAL_LLM_URL=https, LOCAL_LLM_HTTP_URL=https)
        with self.assertRaises(ValueError):
            route.provider_config("local")

    def test_key_control_characters_rejected(self):
        for key in ("synthetic\rheader", "synthetic\nheader", "synthetic\x7f"):
            self.configure(LOCAL_LLM_API_KEY=key)
            with self.subTest(key=repr(key)), self.assertRaisesRegex(ValueError, "value hidden"):
                route.provider_config("local")

    def test_metadata_url_same_origin_effective_port(self):
        self.configure(LOCAL_LLM_URL="http://127.0.0.1:80/v1/messages", LOCAL_LLM_MODELS_URL="http://127.0.0.1/custom")
        self.assertEqual(route.provider_config("local")["LOCAL_LLM_MODELS_URL"], "http://127.0.0.1/custom")
        for url in ("http://127.0.0.1:81/models", "https://127.0.0.1/models", "http://localhost/models",
                    "http://127.0.0.1:0/models", "http://@127.0.0.1/models", " http://127.0.0.1/models",
                    "http://127.0.0.1/mo dels", "http://127.0.0.1/models\n", "http://127.0.0.1/mo\tdels",
                    "http://user@127.0.0.1/models", "http://127.0.0.1/models?q=1", "http://127.0.0.1/models#x"):
            os.environ["LOCAL_LLM_MODELS_URL"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                route.provider_config("local")
        self.configure(LOCAL_LLM_URL="https://llm.invalid/v1/messages", LOCAL_LLM_ALLOW_REMOTE="true",
                       LOCAL_LLM_MODELS_URL="https://llm.invalid:443/models")
        route.provider_config("local")


class LocalDiagnosticTests(LocalTestCase):
    def test_diagnostic_launches_only_node_with_isolated_provider_environment(self):
        self.configure(LOCAL_LLM_API_KEY="synthetic-local-key")
        os.environ.update({"ANTHROPIC_AUTH_TOKEN": "private-marker", "CLAUDE_CODE_OAUTH_TOKEN": "private-marker", "OPENROUTER_API_KEY": "private-marker"})
        with mock.patch.object(route.shutil, "which", return_value="synthetic-node") as which, mock.patch.object(route.subprocess, "call", return_value=0) as child:
            self.assertEqual(route.diagnose_local(), 0)
        which.assert_called_once_with("node")
        self.assertEqual(child.call_args.args[0], ["synthetic-node", str(self.root / "claudeRouteProxy.mjs"), "--diagnose-local"])
        env = child.call_args.kwargs["env"]
        self.assertEqual(env["SONNET_PROVIDER"], "local")
        self.assertEqual(env["LOCAL_LLM_API_KEY"], "synthetic-local-key")
        for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY"):
            self.assertNotIn(name, env)

    def test_diagnostic_cli_rejects_conflicting_modes_before_network(self):
        for args in (["--diagnose-local"], ["--provider", "local", "--check-local", "--diagnose-local"]):
            result = route.subprocess.run([route.sys.executable, SPEC.origin, *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--diagnose-local requires --provider local", result.stderr)


class LocalCliModeTests(LocalTestCase):
    def test_missing_cli_keeps_ordinary_errors_and_classifies_only_local_mode(self):
        with mock.patch.object(route.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                route.pro_command()
            with self.assertRaises(FileNotFoundError):
                with route.routed_session("local"):
                    self.fail("Missing CLI must fail before session")
            with self.assertRaises(route.LocalStartupError) as result:
                with route.routed_session("local", local_only=True):
                    self.fail("Missing CLI must fail before Local session")
            self.assertEqual(result.exception.code, "local_cli_not_found")

    def test_local_mode_preserves_only_proxy_credentials_and_never_queries_plan_auth(self):
        self.configure(LOCAL_LLM_API_KEY="private-local-key")
        os.environ.update({"ANTHROPIC_AUTH_TOKEN": "private-plan-key", "ANTHROPIC_API_KEY": "private-api-key",
                           "CLAUDE_CODE_OAUTH_TOKEN": "private-oauth", "OPENROUTER_API_KEY": "private-openrouter"})
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}\n'
        with mock.patch.object(route.Path, "home", return_value=self.root), mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), mock.patch.object(route.subprocess, "Popen", return_value=proxy) as start:
            with route.routed_session("local", local_only=True) as env:
                self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12345")
                self.assertNotEqual(env["ANTHROPIC_API_KEY"], "private-api-key")
                self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")
                self.assertEqual(env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
                for name in ("LOCAL_LLM_API_KEY", "LOCAL_LLM_URL", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODEL",
                             "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY"):
                    self.assertNotIn(name, env)
                proxy_env = start.call_args.kwargs["env"]
                self.assertEqual(proxy_env["LOCAL_ROUTE_ONLY"], "1")
                self.assertEqual(proxy_env["LOCAL_LLM_API_KEY"], "private-local-key")
                self.assertNotIn("ANTHROPIC_API_KEY", proxy_env)
        proxy.terminate.assert_called_once()

    def test_local_cli_command_requires_supported_bare_version_and_retains_permission_checks(self):
        for version in ("2.1.304", "2.1.305"):
            with self.subTest(version=version), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout=version + " (Claude Code)")):
                command = route.local_cli_command("synthetic-cli")
                self.assertIn("--bare", command)
                self.assertIn("default", command)
                self.assertNotIn("--dangerously-skip-permissions", command)
                self.assertIn("Read", command[command.index("--tools") + 1])
        for version in ("2.1.183", "2.1.286", "2.1.303", "invalid-version"):
            with self.subTest(version=version), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout=version)):
                with self.assertRaisesRegex(RuntimeError, "requires Claude Code 2.1.304 or later"):
                    route.local_cli_command("synthetic-cli")

    def test_unsupported_local_cli_is_rejected_before_starting_proxy_or_session(self):
        with mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.303 (Claude Code)")), mock.patch.object(route, "routed_session") as session, mock.patch.object(route.subprocess, "call") as child:
            with self.assertRaisesRegex(RuntimeError, "requires Claude Code 2.1.304 or later"):
                route.launch("local", local_only=True)
        session.assert_not_called()
        child.assert_not_called()

    def test_local_output_budget_is_applied_before_cli_context_calculation(self):
        for requested, expected in ((None, "8192"), ("", "8192"), ("2048", "8192"),
                                    ("8192", "8192"), ("16384", "16384"), ("32_768", "32768"), ("3.2e4", "32000")):
            with self.subTest(requested=requested), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304 (Claude Code)")):
                if requested is not None:
                    os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = requested
                command = route.local_cli_command("synthetic-cli")
                settings = json.loads(command[command.index("--settings") + 1])
                self.assertEqual(settings["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], expected)
                self.assertFalse(settings["alwaysThinkingEnabled"])
                self.assertEqual(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), requested)

    def test_local_output_budget_rejects_invalid_values(self):
        for value in ("invalid", "0", "-1", "1.5", "nan", "inf", "1e999"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": value}), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304 (Claude Code)")):
                with self.assertRaisesRegex(route.LocalStartupError, "Local output budget must be a positive integer"):
                    route.local_cli_command("synthetic-cli")

    def test_local_startup_failures_report_fixed_codes_without_environment_or_exception_values(self):
        for label, version, which, config_error, spawn_error, ready, expected in [
            ("missing CLI", "2.1.304", lambda _: None, None, None, "", "local_cli_not_found"),
            ("missing Node", "2.1.304", lambda n: "synthetic-cli" if n == "claude" else None, None, None, "", "local_node_not_found"),
            ("old CLI", "2.1.303", lambda _: "synthetic-cli", None, None, "", "local_cli_unsupported"),
            ("bad version", "private-exception-marker", lambda _: "synthetic-cli", None, None, "", "local_cli_unsupported"),
            ("bad config", "2.1.304", lambda _: "synthetic-cli", ValueError("private-exception-marker"), None, "", "local_configuration_invalid"),
            ("proxy spawn", "2.1.304", lambda _: "synthetic-cli", None, OSError("private-exception-marker"), "", "local_proxy_start_failed"),
            ("proxy EOF", "2.1.304", lambda _: "synthetic-cli", None, None, "", "local_proxy_start_failed"),
            ("proxy unreadable output", "2.1.304", lambda _: "synthetic-cli", None, None, UnicodeError("private-exception-marker"), "local_proxy_start_failed"),
            ("proxy invalid JSON", "2.1.304", lambda _: "synthetic-cli", None, None, "private-exception-marker", "local_proxy_start_failed"),
            ("proxy invalid port", "2.1.304", lambda _: "synthetic-cli", None, None, '{"port":0}', "local_proxy_start_failed"),
            ("CLI spawn", "2.1.304", lambda _: "synthetic-cli", None, None, '{"port":12345}', "local_cli_start_failed"),
        ]:
            with self.subTest(label=label):
                proxy = mock.Mock()
                if isinstance(ready, Exception):
                    proxy.stdout.readline.side_effect = ready
                else:
                    proxy.stdout.readline.return_value = ready
                out, err = io.StringIO(), io.StringIO()
                config = {"SONNET_PROVIDER": "local", "LOCAL_LLM_URL": "http://127.0.0.1:9/v1/messages",
                          "LOCAL_LLM_MODEL": "private-model-marker", "LOCAL_LLM_API_KEY": "private-key-marker"}
                is_file = Path.is_file
                # run_path exercises the actual CLI error handler, with every external action replaced.
                with mock.patch.object(sys, "argv", [str(SPEC.origin), "--provider", "local", "--local-mode"]), \
                     mock.patch("shutil.which", side_effect=which), \
                     mock.patch("subprocess.run", return_value=mock.Mock(stdout=version)), \
                     mock.patch("subprocess.Popen", side_effect=spawn_error, return_value=proxy), \
                     mock.patch("subprocess.call", side_effect=OSError("private-exception-marker")), \
                     mock.patch.object(Path, "home", return_value=self.root), \
                     mock.patch.object(Path, "is_file", lambda p: False if p.name == ".env.local-llm" else is_file(p)), \
                     mock.patch.dict(os.environ, config), \
                     contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    # Invalid configuration is supplied by the existing boundary validator.
                    if config_error:
                        os.environ["LOCAL_LLM_URL"] = "private-exception-marker"
                    with self.assertRaises(SystemExit) as exit_result:
                        runpy.run_path(str(SPEC.origin), run_name="__main__")
                self.assertEqual(exit_result.exception.code, 1)
                diagnostic = json.loads(err.getvalue())
                self.assertEqual(diagnostic["event"], "openrouter_start_failed")
                self.assertEqual(diagnostic["kind"], "LocalStartupError")
                self.assertEqual(diagnostic["code"], expected)
                self.assertEqual(diagnostic["message"], route.LOCAL_STARTUP_MESSAGES[expected])
                for hidden in ("private-exception-marker", "private-key-marker", "private-model-marker", config["LOCAL_LLM_URL"]):
                    self.assertNotIn(hidden, out.getvalue() + err.getvalue())

    def test_local_version_check_errors_and_budget_have_safe_startup_codes(self):
        for failure in (OSError("private-marker"), route.subprocess.TimeoutExpired("private-marker", 10),
                        route.subprocess.CalledProcessError(1, "private-marker")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(route.subprocess, "run", side_effect=failure):
                with self.assertRaises(route.LocalStartupError) as result:
                    route.local_cli_command("synthetic-cli")
                self.assertEqual(result.exception.code, "local_cli_version_check_failed")
                self.assertNotIn("private-marker", str(result.exception))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "private-marker"}), mock.patch.object(route.subprocess, "run", return_value=mock.Mock(stdout="2.1.304")):
            with self.assertRaises(route.LocalStartupError) as result:
                route.local_cli_command("synthetic-cli")
            self.assertEqual(result.exception.code, "local_output_budget_invalid")

    def test_local_parent_pipe_is_opt_in_and_closed_during_cleanup(self):
        self.configure()
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = '{"port":12345}'
        with mock.patch.object(route.Path, "home", return_value=self.root), mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), mock.patch.object(route.subprocess, "Popen", return_value=proxy) as spawn:
            with route.routed_session("local", local_only=True):
                self.assertEqual(spawn.call_args.args[0][-1], "--parent-pipe")
                self.assertEqual(spawn.call_args.kwargs["stdin"], route.subprocess.PIPE)
        proxy.stdin.close.assert_called_once()
        proxy.terminate.assert_called_once()

    def test_local_proxy_startup_deadline_cleans_up_without_exposing_output(self):
        self.configure()
        proxy = mock.Mock()
        proxy.stdout.readline.return_value = "private-output-marker"
        ready = mock.Mock()
        ready.get.side_effect = route.queue.Empty
        with mock.patch.object(route.Path, "home", return_value=self.root), mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), mock.patch.object(route.subprocess, "Popen", return_value=proxy), mock.patch.object(route.queue, "Queue", return_value=ready):
            with self.assertRaises(route.LocalStartupError) as result:
                with route.routed_session("local", local_only=True):
                    self.fail("Unready proxy must not start CLI")
        self.assertEqual(result.exception.code, "local_proxy_start_failed")
        self.assertNotIn("private-output-marker", str(result.exception))
        ready.get.assert_called_once_with(timeout=15)
        proxy.terminate.assert_called_once()
        proxy.stdin.close.assert_called_once()

    def test_ordinary_launch_preserves_existing_output_budget(self):
        env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2048"}
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(route.shutil, "which", return_value="synthetic-cli"), mock.patch.object(route, "routed_session", return_value=contextlib.nullcontext(env)) as session, mock.patch.object(route.subprocess, "call", return_value=0) as child:
            self.assertEqual(route.launch("local"), 0)
        session.assert_called_once_with("local")
        self.assertEqual(child.call_args.args[0], ["synthetic-cli", "--model", "sonnet"])
        self.assertEqual(child.call_args.kwargs["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "2048")

    def test_local_mode_cli_rejects_other_modes_before_network(self):
        for args in (["--local-mode"], ["--provider", "local", "--local-mode", "--check-local"],
                     ["--provider", "local", "--local-mode", "--diagnose-local"]):
            result = route.subprocess.run([route.sys.executable, SPEC.origin, *args], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--local-mode requires --provider local", result.stderr)

    def test_local_mode_is_rejected_for_other_providers(self):
        with self.assertRaises(ValueError):
            with route.routed_session("openrouter", local_only=True):
                self.fail("Invalid Local mode entered")


class LocalMetadataTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.payload = json.dumps({"data": [{"id": "synthetic-model"}, {"id": "private-name-marker"}]}).encode()
        self.status = 200
        self.location = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.command, self.path, dict(self.headers)))
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                if owner.location:
                    self.send_header("Location", owner.location)
                self.end_headers()
                self.wfile.write(owner.payload)

            def do_POST(self):
                owner.requests.append((self.command, self.path, dict(self.headers)))
                self.send_error(405)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.configure(LOCAL_LLM_URL=self.base + "/v1/messages", LOCAL_LLM_API_KEY="synthetic-local-key")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def check(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = route.check_local()
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        serialized = json.dumps(result)
        for hidden in ("synthetic-local-key", "private-name-marker", self.base, "sensitive-error-marker"):
            self.assertNotIn(hidden, serialized)
        self.assertIs(result["inference_checked"], False)
        return result

    def test_openai_metadata_uses_only_fresh_local_bearer_and_get(self):
        os.environ.update({"ANTHROPIC_AUTH_TOKEN": "stale-provider-marker", "OPENROUTER_API_KEY": "stale-router-marker"})
        self.assertEqual(self.check(), {"local_check": True, "inference_checked": False, "model_count": 2,
                                      "configured_model_found": True, "reason": "ready_metadata_only"})
        self.assertEqual(len(self.requests), 1)
        method, path, headers = self.requests[0]
        self.assertEqual((method, path), ("GET", "/v1/models"))
        self.assertEqual(headers["Authorization"], "Bearer synthetic-local-key")
        self.assertNotIn("stale-", json.dumps(headers))
        os.environ["LOCAL_LLM_API_KEY"] = ""
        self.check()
        self.assertNotIn("Authorization", self.requests[-1][2])

    def test_ollama_names_and_model_fields(self):
        for field in ("name", "model"):
            self.payload = json.dumps({"models": [{field: "synthetic-model"}]}).encode()
            with self.subTest(field=field):
                result = self.check()
                self.assertTrue(result["local_check"])
                self.assertEqual(result["model_count"], 1)

    def test_missing_and_not_found_model(self):
        for model, reason in (("", "model_not_configured"), ("absent-model", "model_not_found")):
            os.environ["LOCAL_LLM_MODEL"] = model
            with self.subTest(model=model):
                result = self.check()
                self.assertFalse(result["local_check"])
                self.assertFalse(result["configured_model_found"])
                self.assertEqual(result["reason"], reason)

    def test_malformed_oversized_and_invalid_schema(self):
        cases = [(b"{invalid", "invalid_configuration_or_response"),
                 (b" " * (1024 * 1024 + 1), "response_too_large")]
        cases += [(json.dumps(value).encode(), "invalid_model_list") for value in
                  ([], {}, {"data": {}}, {"data": ["x"]}, {"data": [{"id": 7}]}, {"models": [{"name": ""}]})]
        for payload, reason in cases:
            self.payload = payload
            with self.subTest(reason=reason, size=len(payload)):
                self.assertEqual(self.check()["reason"], reason)

    def test_redirect_rejected_without_contacting_target(self):
        self.status, self.location = 302, self.base + "/redirect-target"
        result = self.check()
        self.assertEqual((result["reason"], result["status"]), ("redirect_rejected", 302))
        self.assertEqual([item[1] for item in self.requests], ["/v1/models"])

    def test_http_401_does_not_expose_response_body(self):
        self.status, self.payload = 401, b"sensitive-error-marker"
        result = self.check()
        self.assertEqual((result["reason"], result["status"]), ("http_error", 401))

    def test_url_error_is_classified_and_open_has_five_second_socket_timeout(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("sensitive-error-marker " + self.base)
        with mock.patch.object(route.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.check()["reason"], "connection_failed")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 5})
        self.assertEqual(self.requests, [])

    def test_environment_proxy_is_explicitly_disabled(self):
        os.environ.update({"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""})
        original = route.urllib.request.build_opener
        captured = []

        def build(*handlers):
            captured.extend(handlers)
            return original(*handlers)

        with mock.patch.object(route.urllib.request, "build_opener", side_effect=build):
            self.assertTrue(self.check()["local_check"])
        proxies = [handler for handler in captured if isinstance(handler, route.urllib.request.ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
