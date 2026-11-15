# SPDX-License-Identifier: MIT
# Real HTTP upstream stub for the live scenario suite (tests/scenarios_real.ps1).
#
# This is a REAL listening HTTP server used by the REAL claudeRouteProxy.mjs process so the
# end-to-end scenario exercises the actual network path (node proxy -> real socket -> this
# server) without spending money on a provider. It is not part of the unit suites.
#
#   python scenario_stub_server.py <port> <port-file> <body-text>
#   Serves: GET  /v1/models        -> {data:[{id:stub-model}]}
#           POST /v1/messages      -> Anthropic-style SSE stream carrying <body-text>
import http.server
import json
import sys


def main() -> int:
    port = int(sys.argv[1])
    port_file = sys.argv[2]
    body_text = sys.argv[3]

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/").endswith("/v1/models"):
                self._json(200, {"data": [{"id": "stub-model", "type": "model"}]})
                return
            self._json(404, {"error": {"type": "not_found", "message": "no such stub route"}})

        def do_POST(self):
            if not self.path.endswith("/v1/messages"):
                self._json(404, {"error": {"type": "not_found", "message": "no such stub route"}})
                return
            length = int(self.headers.get("content-length", "0"))
            if length:
                self.rfile.read(length)
            frames = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_stub", "type": "message", "role": "assistant",
                    "model": "stub-model", "content": [],
                    "usage": {"input_tokens": 4, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": body_text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                  "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                  "usage": {"output_tokens": 5, "input_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            payload = "".join("event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))
                              for name, data in frames).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_DELETE(self):
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        with open(port_file, "w", encoding="utf-8") as handle:
            handle.write("ERROR: %s\n" % exc)
        return 1
    with open(port_file, "w", encoding="utf-8") as handle:
        handle.write("PORT=%d\n" % server.server_address[1])
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
