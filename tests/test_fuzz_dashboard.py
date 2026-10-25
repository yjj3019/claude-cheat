"""Fuzz the dashboard: hostile paths, bodies and framing must never break it.

    python -m unittest tests.test_fuzz_dashboard

Deterministic (seeded) so a failure reproduces. The dashboard's slow dependencies are
patched out, so this measures request handling only.
"""
import http.client
import json
import random
import socket
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import routeDashboard as dash

SEED = 7
ITERATIONS = 250


def status_code(text):
    try:
        return int(text.split()[1])
    except (IndexError, ValueError):
        return None


class DashboardFuzzTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        log = self.root / ".route.log"
        log.write_text("\n".join(
            f"{datetime.now(timezone.utc).isoformat()} " + json.dumps(
                {"event": "route_selected", "provider": "openrouter", "reason": "replacement",
                 "model": "vendor/model"}) for _ in range(20)) + "\n", encoding="utf-8")
        self.enterContext(mock.patch.object(dash, "ROUTE_LOG", log))
        self.enterContext(mock.patch.object(dash, "_ROUTE_TAIL",
                                            {"path": None, "events": [], "offset": 0, "digest": None}))
        self.enterContext(mock.patch.object(dash, "read_env", return_value={"OPENROUTER_MODEL": "vendor/model"}))
        self.enterContext(mock.patch.object(dash, "account_state", return_value={"ok": False}))
        self.enterContext(mock.patch.object(dash, "model_pricing", return_value={"ok": False}))
        self.enterContext(mock.patch.object(dash, "scan_sessions", return_value=dash._empty_usage()))
        self.server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_random_requests_never_break_the_dashboard(self):
        random.seed(SEED)
        paths = ["/", "/settings", "/api/status", "/api/snapshot", "/api/models", "/api/config",
                 "/api/test", "/../etc/passwd", "//", "/api/status?provider=" + "x" * 2000,
                 "/api/models?provider=%00", "/api/status#frag", "/" + "a" * 3000,
                 "/api/status?", "/.\u0000/", "/%2e%2e/"]
        methods = ["GET", "GET", "GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"]
        headers = [{}, {"content-type": "application/json"}, {"content-type": "text/plain"},
                   {"origin": "http://evil.example"}, {"host": "evil.example"},
                   {"x-dashboard-token": "wrong"}, {"x-dashboard-token": ""}]
        bodies = [None, "", "{}", "[]", '{"provider":', "x" * 9000, '{"provider":"nope"}',
                  '{"provider":"openrouter","fields":{"OPENROUTER_MODEL":"$$$"}}']
        bad = []
        aborts = []
        for index in range(ITERATIONS):
            method = random.choice(methods)
            path = random.choice(paths)
            mixed = dict(random.choice(headers))
            body = random.choice(bodies)
            try:
                status, payload = self.request(method, path, mixed, body)
            except (http.client.HTTPException, OSError) as error:
                message = str(error)
                # A control character in the path is refused by the client itself.
                if "control characters" in message:
                    continue
                # An early error response may be followed by a reset; that is a transport
                # detail, not a crash, so count them and fail only if they are systematic.
                if "10053" in message or "10054" in message or "reset" in message.lower():
                    aborts.append(index)
                    continue
                bad.append({"i": index, "method": method, "path": path, "reason": "connection", "error": message})
                continue
            if status is None or not 100 <= status < 600:
                bad.append({"i": index, "method": method, "path": path, "reason": "status", "status": status})
            if payload and path.startswith("/api/") and method != "HEAD":
                try:
                    json.loads(payload)
                except ValueError:
                    bad.append({"i": index, "method": method, "path": path, "reason": "invalid json",
                                "body": payload[:120].decode("utf-8", "replace")})
        self.assertEqual(bad, [], f"fuzz failures: {bad[:5]}")
        self.assertLessEqual(len(aborts), max(2, ITERATIONS // 20),
                             f"{len(aborts)} of {ITERATIONS} requests were reset")

    def test_raw_framing_abuse_does_not_hang_the_server(self):
        frames = [b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n",
                  b"POST /api/config HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n{}",
                  b"POST /api/config HTTP/1.1\r\nHost: x\r\nBadHeader\r\n\r\n",
                  b"GET / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
                  b"GET / HTTP/9.9\r\n\r\n"]
        for frame in frames:
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
                connection.sendall(frame)
                connection.settimeout(10)
                try:
                    connection.recv(4096)  # any answer (or a clean close) is acceptable
                except (socket.timeout, ConnectionResetError):
                    pass
        with mock.patch.object(dash, "read_env", return_value={"OPENROUTER_MODEL": "vendor/model"}):
            status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200, "the dashboard stopped serving after framing abuse")
        self.assertIn("state", json.loads(payload))


if __name__ == "__main__":
    unittest.main()
