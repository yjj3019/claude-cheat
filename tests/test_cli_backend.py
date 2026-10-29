"""Synthetic protocol and owned-process tests; never call an authenticated CLI."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import cliBackend as backend


def request(kind="codex"):
    return {"model": "cli/" + kind, "messages": [{"role": "user", "content": "synthetic prompt"}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}


def codex_output(value=None, **reported):
    value = value if value is not None else {"text": "synthetic answer", "tool_calls": []}
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "synthetic"},
        {"type": "item.completed", "item": {"type": "error", "message": "nonfatal startup warning"}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}},
        {"type": "turn.completed", **reported}])


def claude_output(value=None, **reported):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "structured_output": value or {"text": "synthetic answer", "tool_calls": []}, **reported})


class BackendTest(unittest.TestCase):
    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(backend.BackendError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def construct(self, kind="codex", runner=None, **values):
        with patch.multiple(backend.routeCli,
                            find_binary=lambda *_: {"path": "synthetic"},
                            resolve_command=lambda _: ["synthetic"],
                            child_env=lambda _: {"SYNTHETIC": "yes"},
                            probe_version=lambda *_: {"ok": True, "version": {"codex": "0.165.0", "claude": "2.1.312", "grok": "1.0.70"}[kind]}):
            return backend.Backend({"CLI_KIND": kind, **values}, runner)

    def test_strict_json_rejects_ambiguous_and_nonfinite_values(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', 'prefix {}'):
            with self.subTest(raw=raw):
                self.error("invalid_response", backend._json, raw)

    def test_history_preserves_roles_tools_and_ids_without_mutation(self):
        body = request()
        body["system"] = "host system"
        body["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "host1", "name": "Read", "input": {"path": "sample"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "host1", "content": [{"type": "text", "text": "data"}]}]}]
        original = copy.deepcopy(body)
        self.assertEqual(json.loads(backend.request_prompt(body).split("REQUEST_JSON:\n", 1)[1]), original)
        self.assertEqual(body, original)

    def test_backend_sandbox_guidance_preserves_host_tool_results(self):
        body = request()
        body["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "host1", "name": "Read", "input": {"path": "sample"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "host1", "is_error": True, "content": "Permission denied"}]},
        ]
        prompt = backend.request_prompt(body)
        guidance, serialized = prompt.split("REQUEST_JSON:\n", 1)
        self.assertIn("do not infer that the host is read-only", guidance)
        self.assertIn("respect the host's permission decisions", guidance)
        self.assertEqual(json.loads(serialized), body)

    def test_unsupported_request_cannot_silently_drop_content(self):
        variants = [[], {"messages": []}]
        for block in ({"type": "image", "source": {}}, {"type": "thinking", "thinking": "private"},
                      {"type": "tool_result", "tool_use_id": "unknown", "content": "result"}):
            body = request()
            body["messages"][0]["content"] = [block]
            variants.append(body)
        for choice in ({"type": "tool", "name": []}, {"type": "any", "disable_parallel_tool_use": 1}):
            variants.append({**request(), "tool_choice": choice})
        variants.append({**request(), "tools": [{"name": "Native", "type": "computer_20250124", "input_schema": {}}]})
        for body in variants:
            self.error("unsupported_request", backend.request_prompt, body)

    def test_unresolved_or_duplicate_tool_ids_rejected(self):
        body = request()
        body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "host1", "name": "Read", "input": {}}]}]
        self.error("unsupported_request", backend.request_prompt, body)
        body["messages"] += [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "host1", "content": "ok"}]}, body["messages"][-1]]
        self.error("unsupported_request", backend.request_prompt, body)

    def test_only_advertised_tool_plans_accepted_and_ids_are_unique(self):
        value = {"text": "", "tool_calls": [{"name": "Read", "arguments_json": '{"path":"sample"}'}]}
        first = backend.validate_reply(value, request())[0]
        second = backend.validate_reply(value, request())[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["input"], {"path": "sample"})
        for call in ({"name": [], "arguments_json": "{}"}, {"name": "Shell", "arguments_json": "{}"},
                     {"name": "Read", "arguments_json": "[]"}):
            self.error("invalid_tool_call", backend.validate_reply, {"text": "", "tool_calls": [call]}, request())
        self.error("invalid_tool_choice", backend.validate_reply, value, {**request(), "tool_choice": {"type": "none"}})
        self.error("invalid_tool_choice", backend.validate_reply, {"text": "done", "tool_calls": []}, {**request(), "tool_choice": {"type": "any"}})
        self.error("invalid_tool_choice", backend.validate_reply, {"text": "", "tool_calls": value["tool_calls"] * 2},
                   {**request(), "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}})

    def test_codex_requires_terminal_success_and_rejects_native_execution(self):
        value, model, usage = backend.parse_output("codex", codex_output(model="gpt-test", usage={"input_tokens": 2, "output_tokens": 3}))
        self.assertEqual((value["text"], model, usage["output_tokens"]), ("synthetic answer", "gpt-test", 3))
        self.error("incomplete_response", backend.parse_output, "codex", codex_output().rsplit("\n", 1)[0])
        self.error("cli_failed", backend.parse_output, "codex", codex_output() + '\n{"type":"turn.failed"}')
        native = '{"type":"item.started","item":{"type":"command_execution","command":"forbidden"}}\n'
        self.error("native_tool_execution_detected", backend.parse_output, "codex", native + codex_output())

    def test_claude_wrapper_and_usage_are_validated(self):
        self.assertEqual(backend.parse_output("claude", claude_output(modelUsage={"claude-test": {}}))[1], "claude-test")
        for value in (True, -1, 2.5):
            self.error("invalid_usage", backend.parse_output, "claude", claude_output(usage={"input_tokens": value}))
        self.error("incomplete_response", backend.parse_output, "claude", claude_output(stop_reason="max_tokens"))
        self.error("incomplete_response", backend.parse_output, "claude", '{"structured_output":{"text":"x","tool_calls":[]}}')
        self.error("not_logged_in", backend.parse_output, "claude", '{"is_error":true,"result":"authentication required"}')

    def test_reported_model_does_not_expose_credentials_or_paths(self):
        for model in ("sk-synthetic", "xai-synthetic", "BearerSynthetic", "C:/private", "unknown", "model\r\nheader"):
            self.assertIsNone(backend._reported({"model": model})[0])
        self.assertEqual(backend._reported({"model": "vendor/model-v1"})[0], "vendor/model-v1")

    def test_verified_profiles_fail_closed(self):
        self.error("backend_version_unverified", self.construct, "grok")
        for timeout in (False, 1, 121, "invalid"):
            self.error("invalid_cli_timeout", self.construct, "codex", None, CLI_TIMEOUT_S=timeout)

    def test_codex_generation_is_memory_only_and_reports_model_truthfully(self):
        body = request()
        calls = []
        def runner(argv, prompt, env, directory, timeout, cancelled):
            calls.append(argv)
            self.assertNotIn(body["messages"][0]["content"], argv)
            self.assertIn("synthetic prompt", prompt)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["response-schema.json"])
            self.assertEqual(json.loads((Path(directory) / "response-schema.json").read_text()), backend.SCHEMA)
            self.assertIn("--ignore-user-config", argv)
            self.assertIn("--ignore-rules", argv)
            for feature in backend.CODEX_DISABLED:
                self.assertIn(feature, argv)
            return codex_output()
        instance = self.construct(runner=runner)
        message, metadata = instance.generate(body)
        self.assertEqual(message["model"], "cli/codex")
        self.assertEqual(metadata, {"reported_model": None, "usage_measured": False})
        self.error("wrong_model", instance.generate, {**body, "model": "actual-model"})
        self.assertEqual(len(calls), 1)

    def test_claude_generation_disables_backend_tools_hooks_mcp_and_plugins(self):
        def runner(argv, *_):
            for flag in ("--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
            self.assertTrue(json.loads(argv[argv.index("--settings") + 1])["disableAllHooks"])
            return claude_output(model="claude-test", usage={"input_tokens": 2, "output_tokens": 1})
        message, metadata = self.construct("claude", runner).generate(request("claude"))
        self.assertEqual(message["model"], "cli/claude")
        self.assertEqual(metadata, {"reported_model": "claude-test", "usage_measured": True})

    def test_loopback_auth_input_and_native_metadata(self):
        instance = self.construct(runner=lambda *_: codex_output(model="native-test", usage={"input_tokens": 2, "output_tokens": 1}))
        with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
            parsed = urlsplit(url)
            def post(body, auth):
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                connection.request("POST", parsed.path, json.dumps(body), {"Authorization": auth})
                response = connection.getresponse()
                result = response.status, dict(response.getheaders()), json.loads(response.read())
                connection.close()
                return result
            self.assertEqual(post(request(), "invalid")[0], 403)
            self.assertEqual(post([], "Bearer " + token)[2], {"error": "invalid_request"})
            status, headers, body = post(request(), "Bearer " + token)
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-CLI-Reported-Model"], "native-test")
            self.assertEqual(headers["X-CLI-Usage-Measured"], "true")
            self.assertEqual(body["model"], "cli/codex")

    def test_loopback_emits_only_bounded_process_duration_metadata(self):
        instance = self.construct(runner=lambda *_: codex_output())
        message, metadata = instance.generate(request())
        metadata["process_ms"] = 123.45
        with patch.object(instance, "generate", return_value=(message, metadata)), \
                patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
            parsed = urlsplit(url)
            def header():
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
                connection.request("POST", parsed.path, json.dumps(request()), {"Authorization": "Bearer " + token})
                response = connection.getresponse()
                value = response.getheader("X-CLI-Process-Ms")
                response.read()
                connection.close()
                return value
            self.assertEqual(header(), "123.45")
            metadata["process_ms"] = 180001
            self.assertIsNone(header())
            metadata["process_ms"] = True
            self.assertIsNone(header())

    def test_common_effort_levels_use_native_flags_without_guessing_other_levels(self):
        for kind in ("codex", "claude"):
            for effort in ("low", "medium", "high", "max", "xhigh"):
                captured = []
                def runner(argv, *_):
                    captured.extend(argv)
                    return codex_output() if kind == "codex" else claude_output()
                instance = self.construct(kind, runner)
                instance.generate({**request(kind), "output_config": {"effort": effort}})
                if effort in ("low", "medium", "high"):
                    if kind == "codex":
                        self.assertIn('model_reasoning_effort="' + effort + '"', captured)
                    else:
                        self.assertEqual(captured[captured.index("--effort") + 1], effort)
                else:
                    self.assertFalse(any(arg.startswith("model_reasoning_effort=") or arg == "--effort" for arg in captured))

    def test_loopback_disconnect_cancels_owned_work(self):
        started, stopped = threading.Event(), threading.Event()
        def runner(_argv, _prompt, _env, _cwd, _timeout, cancelled):
            started.set()
            until = time.monotonic() + 5
            while not cancelled() and time.monotonic() < until:
                time.sleep(.01)
            stopped.set()
            raise backend.BackendError("cancelled")
        instance = self.construct(runner=runner)
        with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
            parsed = urlsplit(url)
            wire = json.dumps(request()).encode()
            connection = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
            connection.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(wire)}\r\n\r\n").encode() + wire)
            self.assertTrue(started.wait(3))
            connection.close()
            self.assertTrue(stopped.wait(3))

    def test_shutdown_closes_incomplete_request_headers(self):
        instance = self.construct()
        manager = backend.serve({})
        self.enterContext(patch.object(backend, "HEADER_TIMEOUT_SECONDS", .5))
        accepted = threading.Event()
        original_setup = backend.BaseHTTPRequestHandler.setup
        def mark_setup(handler):
            original_setup(handler)
            accepted.set()
        self.enterContext(patch.object(backend.BaseHTTPRequestHandler, "setup", mark_setup))
        with patch.object(backend, "Backend", return_value=instance):
            _, url, _ = manager.__enter__()
        parsed = urlsplit(url)
        client = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
        client.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n")
        # Ensure the handler has accepted the connection before measuring shutdown.
        self.assertTrue(accepted.wait(3))
        done = threading.Event()
        errors = []
        def close():
            try:
                manager.__exit__(None, None, None)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        thread = threading.Thread(target=close, daemon=True)
        thread.start()
        try:
            self.assertTrue(done.wait(3), "incomplete headers prevented server shutdown")
        finally:
            client.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_shutdown_cancels_slow_drip_headers_and_body(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                instance = self.construct()
                manager = backend.serve({})
                with patch.object(backend, "Backend", return_value=instance):
                    _, url, token = manager.__enter__()
                parsed = urlsplit(url)
                client = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
                stop, done = threading.Event(), threading.Event()
                errors = []
                if phase == "headers":
                    initial = b"POST /v1/messages HTTP/1.1\r\nHost: local"
                else:
                    initial = (f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: 10000\r\n\r\n{{").encode()
                client.sendall(initial)
                def drip():
                    while not stop.wait(.05):
                        try:
                            client.sendall(b" ")
                        except OSError:
                            return
                sender = threading.Thread(target=drip, daemon=True)
                sender.start()
                time.sleep(.3)
                def close():
                    try:
                        manager.__exit__(None, None, None)
                    except Exception as error:
                        errors.append(error)
                    finally:
                        done.set()
                closer = threading.Thread(target=close, daemon=True)
                closer.start()
                try:
                    self.assertTrue(done.wait(3), "slow client prevented shutdown")
                finally:
                    stop.set()
                    client.close()
                    sender.join(3)
                    closer.join(3)
                self.assertEqual(errors, [])
                self.assertFalse(closer.is_alive())

    def test_shutdown_interrupts_backpressured_response(self):
        ready = threading.Event()
        instance = mock_instance = self.construct()
        def generate(*_):
            ready.set()
            return ({"text": "x" * (16 * 1024 * 1024)}, {})
        instance.generate = generate
        manager = backend.serve({})
        with patch.object(backend, "Backend", return_value=mock_instance):
            _, url, token = manager.__enter__()
        parsed = urlsplit(url)
        client = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        wire = json.dumps(request()).encode()
        client.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(wire)}\r\n\r\n").encode() + wire)
        self.assertTrue(ready.wait(3))
        time.sleep(.3)
        done, errors = threading.Event(), []
        def close():
            try:
                manager.__exit__(None, None, None)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        try:
            self.assertTrue(done.wait(3), "backpressured writer prevented shutdown")
        finally:
            client.close()
            closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])

    def test_ambiguous_and_truncated_request_framing_is_rejected(self):
        instance = self.construct()
        with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
            parsed = urlsplit(url)
            body = json.dumps(request()).encode()
            for headers, wire in ((f"Content-Length: {len(body)}\r\nContent-Length: {len(body)}\r\n", body),
                                  (f"Content-Length: {len(body)}\r\nTransfer-Encoding: chunked\r\n", body),
                                  (f"Content-Length: +{len(body)}\r\n", body),
                                  ("Content-Length: 1_0\r\n", body),
                                  (f"Content-Length: {len(body) + 5}\r\n", body)):
                with self.subTest(headers=headers):
                    connection = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
                    try:
                        connection.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n{headers}\r\n").encode() + wire)
                        connection.shutdown(socket.SHUT_WR)
                        response = b""
                        while chunk := connection.recv(4096):
                            response += chunk
                        self.assertIn(b" 400 ", response)
                    finally:
                        connection.close()

    def test_slow_drip_request_has_absolute_deadline(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase), patch.object(backend, "HEADER_TIMEOUT_SECONDS", .6), \
                    patch.object(backend, "BODY_TIMEOUT_SECONDS", .6):
                instance = self.construct()
                with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
                    parsed = urlsplit(url)
                    client = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
                    client.settimeout(3)
                    if phase == "headers":
                        initial = b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nX-Drip: "
                    else:
                        initial = (f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\nContent-Length: 10000\r\n\r\n{{").encode()
                    client.sendall(initial)
                    stop = threading.Event()
                    def drip():
                        while not stop.wait(.05):
                            try:
                                client.sendall(b"x")
                            except OSError:
                                return
                    sender = threading.Thread(target=drip, daemon=True)
                    sender.start()
                    try:
                        try:
                            result = client.recv(1024)
                        except (ConnectionAbortedError, ConnectionResetError):
                            result = b""
                        self.assertEqual(result, b"")
                    finally:
                        stop.set()
                        client.close()
                        sender.join(3)

    def test_incomplete_headers_cannot_spawn_unbounded_connections(self):
        instance = self.construct()
        self.enterContext(patch.object(backend, "MAX_CONNECTIONS", 1))
        self.enterContext(patch.object(backend, "HEADER_TIMEOUT_SECONDS", 2))
        with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, _):
            parsed = urlsplit(url)
            first = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
            second = None
            try:
                first.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n")
                time.sleep(.2)
                second = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
                second.settimeout(3)
                second.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                try:
                    response = second.recv(1024)
                except (ConnectionResetError, ConnectionAbortedError):
                    response = b""
                self.assertEqual(response, b"", "excess connection should be closed")
            finally:
                first.close()
                if second:
                    second.close()

    def test_loopback_accepts_only_four_concurrent_generations(self):
        release = threading.Event()
        entered = threading.Condition()
        count = [0]
        errors = []
        def runner(*_):
            with entered:
                count[0] += 1
                entered.notify_all()
            release.wait(5)
            return codex_output()
        instance = self.construct(runner=runner)
        with patch.object(backend, "Backend", return_value=instance), backend.serve({}) as (_, url, token):
            parsed = urlsplit(url)
            def post():
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=8)
                try:
                    connection.request("POST", parsed.path, json.dumps(request()), {"Authorization": "Bearer " + token})
                    response = connection.getresponse()
                    response.read()
                    return response.status
                except Exception as error:
                    errors.append(type(error).__name__)
                finally:
                    connection.close()
            threads = [threading.Thread(target=post) for _ in range(4)]
            try:
                for thread in threads:
                    thread.start()
                with entered:
                    self.assertTrue(entered.wait_for(lambda: count[0] == 4, timeout=3))
                self.assertEqual(post(), 429)
                self.assertEqual(count[0], 4)
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
            self.assertEqual(errors, [])

    def test_runner_memory_io_and_bounded_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            def run(script, timeout=15, cancelled=lambda: False):
                return backend._run([sys.executable, "-c", script], "synthetic", os.environ.copy(), directory, timeout, cancelled)
            self.assertEqual(run("import sys; print(sys.stdin.read(), end='')"), "synthetic")
            timing = {}
            self.assertEqual(backend._run([sys.executable, "-c", "print('done')"], "", os.environ.copy(),
                                          directory, 15, lambda: False, timing).strip(), "done")
            self.assertTrue(0 <= timing["process_ms"] <= 180000)
            timing = {}
            with self.assertRaises(backend.BackendError):
                backend._run([sys.executable, "-c", "import time; time.sleep(10)"], "", os.environ.copy(),
                             directory, .15, lambda: False, timing)
            self.assertEqual(timing, {})
            self.error("not_logged_in", run, "import sys; sys.stderr.write('authentication required'); sys.exit(1)")
            self.error("timeout", run, "import time; time.sleep(10)", .15)
            start = time.monotonic()
            self.error("cancelled", run, "import time; time.sleep(10)", 3, lambda: time.monotonic() - start > .15)
            with patch.object(backend, "MAX_BYTES", 1024):
                self.error("output_too_large", run, "import sys; sys.stdout.write('x'*4096)")
            with patch.object(backend.subprocess, "Popen") as spawn:
                self.error("cancelled", run, "pass", 3, lambda: True)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
