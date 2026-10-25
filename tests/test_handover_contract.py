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
    def _with_running_session(self):
        record = {"port": 48432, "nonce": "nonce"}
        patches = [mock.patch.object(advisor, "read_handoff", return_value=record),
                   mock.patch.object(advisor, "port_open", return_value=True)]
        return patches

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

    def test_takeover_refuses_a_session_without_a_token(self):
        self.enterContext(mock.patch.object(advisor, "read_handoff",
                                            return_value={"port": 48432, "nonce": ""}))
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("token", str(caught.exception))

    def test_takeover_refuses_when_nothing_listens(self):
        self.enterContext(mock.patch.object(advisor, "read_handoff",
                                            return_value={"port": 48432, "nonce": "n"}))
        self.enterContext(mock.patch.object(advisor, "port_open", return_value=False))
        with self.assertRaises(RuntimeError) as caught:
            advisor.takeover("openrouter")
        self.assertIn("Nothing is listening", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
