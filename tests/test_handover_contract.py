# SPDX-License-Identifier: MIT
"""Contract tests for the launcher's side of the proxy handover (no real proxy needed).

Covers the three defects that let a restart fail quietly: the client timeout drifting
from the server cap, a 409 revert surfacing as a bare HTTP error, and a takeover
starting a replacement proxy while the previous one is still serving.
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import openrouterAdvisor as advisor


class _Canned(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"

    def do_POST(self):  # noqa: N802 - stdlib handler naming
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class StubServer:
    """A one-route HTTP server that answers every handover POST with a canned reply."""

    def __init__(self, status, report):
        handler = type("_Handler", (_Canned,), {"status": status,
                                                "body": json.dumps(report).encode()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class HandoverTimeoutTests(unittest.TestCase):
    def test_the_client_wait_covers_the_proxy_drain_cap(self):
        with mock.patch.dict(os.environ, {"ROUTE_HANDOVER_TIMEOUT_MS": "1000"}):
            # The proxy may legitimately spend its whole cap draining; +5s is the margin
            # for the response to arrive after that, so the launcher must not fail first.
            self.assertEqual(advisor.handover_timeout_seconds(), 6.0)
        with mock.patch.dict(os.environ, {"ROUTE_HANDOVER_TIMEOUT_MS": "120000"}):
            self.assertEqual(advisor.handover_timeout_seconds(), 125.0)

    def test_a_missing_or_broken_cap_falls_back_to_the_proxy_default(self):
        for value in (None, "", "not-a-number"):
            environment = {k: v for k, v in os.environ.items()
                           if k != "ROUTE_HANDOVER_TIMEOUT_MS"}
            if value is not None:
                environment["ROUTE_HANDOVER_TIMEOUT_MS"] = value
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(advisor.handover_timeout_seconds(), 125.0,
                                 f"cap={value!r} must fall back to 120s")

    def test_an_absurdly_small_cap_is_still_a_usable_wait(self):
        with mock.patch.dict(os.environ, {"ROUTE_HANDOVER_TIMEOUT_MS": "10"}):
            # A sub-second cap would make the socket timeout win the race, turning a
            # normal slow drain into a launch failure.
            self.assertGreaterEqual(advisor.handover_timeout_seconds(), 1.0)

    def test_request_handover_uses_the_computed_timeout(self):
        captured = {}
        opener = mock.Mock()

        def open_(request, timeout=None):
            captured["timeout"] = timeout
            response = mock.Mock()
            response.read.return_value = b'{"drained": true}'
            response.__enter__ = lambda self: self
            response.__exit__ = lambda *exc: None
            return response

        opener.open.side_effect = open_
        with mock.patch.dict(os.environ, {"ROUTE_HANDOVER_TIMEOUT_MS": "2000"}), \
                mock.patch.object(urllib.request, "build_opener", return_value=opener):
            self.assertEqual(advisor.request_handover(48432, "nonce"), {"drained": True})
        self.assertEqual(captured["timeout"], 7.0)


class HandoverErrorTests(unittest.TestCase):
    def test_a_reverted_drain_is_reported_as_a_retry_not_a_refusal(self):
        with StubServer(409, {"drained": False, "reverted": True, "inflight": 1,
                              "reason": "drain_timeout"}) as port:
            with self.assertRaises(RuntimeError) as caught:
                advisor.request_handover(port, "nonce", timeout=5)
        message = str(caught.exception)
        self.assertIn("reverted", message)
        self.assertIn("retry", message.lower())

    def test_any_other_http_error_keeps_its_status(self):
        with StubServer(503, {"type": "error"}) as port:
            with self.assertRaises(RuntimeError) as caught:
                advisor.request_handover(port, "nonce", timeout=5)
        self.assertIn("503", str(caught.exception))

    def test_a_successful_handover_returns_the_report(self):
        with StubServer(200, {"drained": True, "inflight": 0, "waitedMs": 12}) as port:
            report = advisor.request_handover(port, "nonce", timeout=5)
        self.assertTrue(report["drained"])
        self.assertEqual(report["waitedMs"], 12)


class TakeoverGuardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(advisor.shutil, "which", return_value="fixture"))
        self.enterContext(mock.patch.object(advisor, "provider_config", return_value={}))

    def test_invalid_replacement_config_never_drains_existing_session(self):
        for failure in (ValueError("private config"), OSError("private path")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(advisor, "read_handoff", return_value={"port": 48432, "nonce": "n", "mode": "ordinary"}), \
                    mock.patch.object(advisor, "port_open", return_value=True), \
                    mock.patch.object(advisor, "provider_config", side_effect=failure), \
                    mock.patch.object(advisor, "request_handover") as handover:
                with self.assertRaisesRegex(RuntimeError, "existing session was not changed") as caught:
                    advisor.takeover("local")
                self.assertNotIn("private", str(caught.exception))
                handover.assert_not_called()

    def _with_running_session(self):
        record = {"port": 48432, "nonce": "nonce", "mode": "ordinary"}
        patches = [mock.patch.object(advisor, "read_handoff", return_value=record),
                   mock.patch.object(advisor, "port_open", return_value=True)]
        return patches

    def test_takeover_refuses_isolated_or_unknown_modes_before_draining(self):
        for mode in (None, "local_only", "local_compat", "openrouter_compat", "unknown"):
            with self.subTest(mode=mode), \
                    mock.patch.object(advisor, "read_handoff", return_value={"port": 48432, "nonce": "n", "mode": mode}), \
                    mock.patch.object(advisor, "request_handover") as handover:
                with self.assertRaisesRegex(RuntimeError, "restart"):
                    advisor.takeover("local")
                handover.assert_not_called()

    def test_takeover_rejects_incompatible_command_line_flags(self):
        for flags in (["--local-mode", "--provider", "local"], ["--openrouter-compat"],
                      ["--local-compat", "--provider", "local"], ["--print", "x"],
                      ["--check-local", "--provider", "local"]):
            with self.subTest(flags=flags), mock.patch.object(advisor, "takeover") as takeover:
                with self.assertRaises(SystemExit) as caught:
                    advisor.main(["--takeover", *flags])
                self.assertEqual(caught.exception.code, 2)
                takeover.assert_not_called()

    def test_takeover_stops_when_the_previous_proxy_kept_serving(self):
        patches = self._with_running_session()
        patches.append(mock.patch.object(advisor, "request_handover",
                                         return_value={"drained": False, "reverted": True,
                                                       "inflight": 2}))
        for patch in patches:
            self.enterContext(patch)
        routed = self.enterContext(mock.patch.object(advisor, "routed_session"))
        marker = self.enterContext(mock.patch.object(advisor, "log_route_marker"))
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("still serving", str(caught.exception))
        routed.assert_not_called()
        marker.assert_not_called()

    def test_takeover_exits_and_cleans_up_when_the_recorded_launcher_is_gone(self):
        # The dashboard's auto-takeover serves a CLI it never launched; when that CLI's
        # launcher pid disappears, the loop must end (the routed_session finally block
        # clears the handoff) instead of keeping a dead session visible forever.
        record = {"port": 48432, "nonce": "n", "mode": "ordinary", "launcher_pid": 999999}
        sleeps = []
        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt  # the client-watch break exits the loop first
        patches = [mock.patch.object(advisor, "read_handoff", return_value=record),
                   mock.patch.object(advisor, "port_open", return_value=True),
                   mock.patch("shutil.which", return_value="exe"),
                   mock.patch.object(advisor, "provider_config", return_value={}),
                   mock.patch.object(advisor, "request_handover",
                                     return_value={"drained": True, "waitedMs": 0, "forced": False}),
                   mock.patch.object(advisor, "wait_for_port_release", return_value=True),
                   mock.patch.object(advisor, "_process_start_time", side_effect=[123.0, None]),
                   mock.patch.object(advisor, "routed_session", mock.MagicMock()),
                   mock.patch.object(advisor.time, "sleep", fake_sleep)]
        for patch in patches:
            self.enterContext(patch)
        log = self.enterContext(mock.patch.object(advisor, "log_route_marker"))
        self.assertEqual(advisor.takeover("openrouter"), 0)
        self.assertTrue(any("gone" in str(call.args) for call in log.call_args_list))

    def test_takeover_refuses_a_session_without_a_token(self):
        self.enterContext(mock.patch.object(advisor, "read_handoff",
                                            return_value={"port": 48432, "nonce": "", "mode": "ordinary"}))
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("token", str(caught.exception))

    def test_takeover_refuses_when_nothing_listens(self):
        self.enterContext(mock.patch.object(advisor, "read_handoff",
                                            return_value={"port": 48432, "nonce": "n", "mode": "ordinary"}))
        self.enterContext(mock.patch.object(advisor, "port_open", return_value=False))
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("Nothing is listening", str(caught.exception))

    DUAL_RECORD = {"port": 48432, "nonce": "n", "provider": "openrouter", "mode": "ordinary",
                   "dual": {"provider": "local", "alias": "claude-route-local", "model": "local-model"}}
    DUAL_WANTED = {"ROUTE_ALT_PROVIDER": "local", "ROUTE_ALT_ALIAS": "claude-route-local",
                   "ROUTE_ALT_MODEL": "local-model", "ROUTE_ALT_URL": "http://127.0.0.1:9/v1/messages",
                   "ROUTE_ALT_KEY": "k", "ROUTE_ALT_ALLOW_REMOTE": "false", "ROUTE_ALT_HTTP_URL": ""}

    def _guards(self, record, wanted):
        self.enterContext(mock.patch.object(advisor, "read_handoff", return_value=record))
        self.enterContext(mock.patch.object(advisor, "port_open", return_value=True))
        self.enterContext(mock.patch.object(advisor, "dual_provider_setup", return_value=wanted))
        return self.enterContext(mock.patch.object(advisor, "request_handover"))

    def test_takeover_refuses_before_draining_when_the_recorded_dual_topology_is_gone(self):
        handover = self._guards(dict(self.DUAL_RECORD), None)
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("dual_provider", str(caught.exception))
        self.assertIn("restart", str(caught.exception))
        handover.assert_not_called()

    def test_takeover_refuses_a_malformed_dual_record(self):
        handover = self._guards({**self.DUAL_RECORD, "dual": "claude-route-local"}, dict(self.DUAL_WANTED))
        with self.assertRaises(RuntimeError):
            advisor.takeover("openrouter")
        handover.assert_not_called()

    def test_takeover_refuses_when_the_alternate_service_changed(self):
        wanted = {**self.DUAL_WANTED, "ROUTE_ALT_MODEL": "new-model"}
        handover = self._guards(dict(self.DUAL_RECORD), wanted)
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("same alternate", str(caught.exception))
        handover.assert_not_called()

    def test_takeover_refuses_an_explicit_provider_flip_that_orphans_the_alias(self):
        wanted = {**self.DUAL_WANTED, "ROUTE_ALT_PROVIDER": "openrouter", "ROUTE_ALT_ALIAS": "claude-route-openrouter"}
        handover = self._guards(dict(self.DUAL_RECORD), wanted)
        with self.assertRaises(RuntimeError):
            advisor.takeover("local")
        handover.assert_not_called()

    def test_takeover_accepts_the_same_dual_topology(self):
        handover = self._guards(dict(self.DUAL_RECORD), dict(self.DUAL_WANTED))
        handover.return_value = {"drained": True, "waitedMs": 1}
        self.enterContext(mock.patch.object(advisor, "wait_for_port_release", return_value=True))
        marker = self.enterContext(mock.patch.object(advisor, "log_route_marker"))
        self.enterContext(mock.patch.object(advisor, "routed_session"))
        self.enterContext(mock.patch.object(advisor.time, "sleep", side_effect=KeyboardInterrupt))
        self.assertEqual(advisor.takeover("openrouter"), 0)
        handover.assert_called_once()
        marker.assert_called_once()


class OwnedPortVerificationTests(unittest.TestCase):
    """Audit L5: never start the CLI against a loopback port this launcher cannot prove."""

    def _serve(self, status):
        handler = type("_Hello", (_Canned,), {"status": status, "body": b""})
        handler.do_HEAD = _Canned.do_POST
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_proxy_that_accepts_the_launch_token_is_ours(self):
        port = self._serve(200)
        self.assertTrue(advisor.verify_owned_proxy(port, "fixture-nonce", timeout=2))

    def test_a_foreign_listener_on_the_port_is_rejected(self):
        port = self._serve(403)
        self.assertFalse(advisor.verify_owned_proxy(port, "fixture-nonce", timeout=2),
                         "a 403 answer means another program holds the port")

    def test_an_unreachable_port_is_rejected_after_the_wait(self):
        def refused(*_args, **_kwargs):
            raise OSError("connection refused")

        with mock.patch.object(advisor.urllib.request, "urlopen", side_effect=refused):
            self.assertFalse(advisor.verify_owned_proxy(49999, "fixture-nonce", timeout=0.2))

    def test_the_launcher_aborts_before_handing_an_unverified_port_to_the_cli(self):
        class LiveProxy(advisor.subprocess.Popen):
            """A real Popen subclass: `_verify_port_owned` only trusts real children."""

            def __init__(self, *args, **kwargs):
                self.pid = 4242
                self.stdout = mock.MagicMock()
                self.stdout.readline.return_value = '{"port":49999}\n'
                self.stdin = mock.MagicMock()

            def poll(self, *_args):
                return None

            def terminate(self):
                pass

            def wait(self, *_args, **_kwargs):
                return 0

            def kill(self):
                pass

        proxy = LiveProxy()
        auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
        with mock.patch.object(advisor.subprocess, "Popen", return_value=proxy), \
             mock.patch.object(advisor, "verify_owned_proxy", return_value=False) as owned, \
             mock.patch.object(advisor.shutil, "which", return_value="node"), \
             mock.patch.object(advisor, "run_owned", return_value=auth), \
             mock.patch.object(advisor, "provider_config", return_value={"OPENROUTER_MODEL": "fixture/model"}), \
             mock.patch.object(advisor, "check_connection", return_value=0), \
             mock.patch.object(advisor, "log_route_marker"), \
             mock.patch.object(advisor, "write_handoff") as handoff:
            with self.assertRaises(RuntimeError) as caught:
                with advisor._routed_session("openrouter", _config={"OPENROUTER_MODEL": "fixture/model"}):
                    self.fail("an unverified port must never reach the session body")
        self.assertIn("another process", str(caught.exception))
        owned.assert_called_once()          # a live child is always verified
        handoff.assert_not_called()

    def test_a_stubbed_spawner_keeps_its_previous_contract(self):
        # Tests and embedders replace Popen to model other failures; they never bound a port,
        # so the ownership probe must not be forced onto them (it would fail on every stub).
        stub = mock.Mock()
        self.assertTrue(advisor._verify_port_owned(49999, "nonce", stub))
        with mock.patch.object(advisor, "verify_owned_proxy") as probe:
            advisor._verify_port_owned(49999, "nonce", stub)
        probe.assert_not_called()

    def _popen_shaped(self, poll_status):
        """An object that is a real Popen subclass instance without starting a process."""
        proxy = advisor.subprocess.Popen.__new__(advisor.subprocess.Popen)
        proxy.pid = 4242
        proxy.poll = lambda *_a: poll_status
        return proxy

    def test_a_dead_child_never_proves_ownership(self):
        with mock.patch.object(advisor, "verify_owned_proxy") as probe:
            self.assertFalse(advisor._verify_port_owned(49999, "nonce", self._popen_shaped(1)))
        probe.assert_not_called()

    def test_a_live_child_is_verified_against_the_proxy(self):
        with mock.patch.object(advisor, "verify_owned_proxy", return_value=True) as probe:
            self.assertTrue(advisor._verify_port_owned(49999, "nonce", self._popen_shaped(None)))
        probe.assert_called_once_with(49999, "nonce")

if __name__ == "__main__":
    unittest.main()
