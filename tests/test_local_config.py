"""Local metadata checks use disposable loopback HTTP and synthetic credentials only."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
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
