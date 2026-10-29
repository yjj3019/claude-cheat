"""Opt-in, one-request Codex backend smoke test; no project tools are executed.

Run explicitly: python tests/live_cli_smoke.py --run
This consumes real CLI model usage. Output contains only bounded status metadata.
"""
import argparse
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliBackend
import routeConfig


def _request_adapter(instance, values, body):
    # One authenticated loopback request; the token and provider output stay in memory.
    with patch.object(cliBackend, "Backend", return_value=instance), cliBackend.serve(values) as (_, url, token):
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=130) as response:
            raw = response.read(cliBackend.MAX_BYTES + 1)
            if len(raw) > cliBackend.MAX_BYTES:
                raise ValueError("adapter response too large")
            message = json.loads(raw)
            metadata = {"reported_model": response.headers.get("X-CLI-Reported-Model"),
                        "usage_measured": response.headers.get("X-CLI-Usage-Measured") == "true"}
            try:
                process_ms = float(response.headers.get("X-CLI-Process-Ms", ""))
                metadata["process_timing_reported"] = math.isfinite(process_ms) and 0 <= process_ms <= 180000
            except (TypeError, ValueError):
                metadata["process_timing_reported"] = False
    return message, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="make one real Codex inference request")
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; this test invokes a real model")

    try:
        values = routeConfig.Store().read_provider("cli")
        if values.get("CLI_KIND") != "codex":
            print(json.dumps({"ok": False, "reason": "configured_cli_not_codex"}))
            return 1
        observed = {}
        original_run = cliBackend._run
        def runner(argv, prompt, env, cwd, timeout, cancelled, process_timing=None):
            output = original_run(argv, prompt, env, cwd, timeout, cancelled, process_timing)
            try:
                events = [json.loads(line) for line in output.splitlines() if line.strip()]
                completed = [event for event in events if isinstance(event, dict) and event.get("type") == "turn.completed"]
                observed["turn_completed"] = len(completed) == 1
                observed["model_field_present"] = bool(completed and "model" in completed[0])
                observed["model_usage_field_present"] = bool(completed and "modelUsage" in completed[0])
            except (ValueError, TypeError):
                observed["output_json_valid"] = False
            return output

        body = {"model": "cli/codex", "max_tokens": 64,
                "messages": [{"role": "user", "content": "Call the Echo tool once with value 'ok'."}],
                "tools": [{"name": "Echo", "description": "A synthetic tool; request its invocation.",
                           "input_schema": {"type": "object", "properties": {"value": {"type": "string"}},
                                            "required": ["value"]}}],
                "tool_choice": {"type": "tool", "name": "Echo"}}
        # Patch the default runner before constructing Backend so the production identity
        # path supplies its timing hook; the wrapped process is still the real runner.
        with patch.object(cliBackend, "_run", runner):
            instance = cliBackend.Backend(values)
            message, metadata = _request_adapter(instance, values, body)
        calls = [block for block in message.get("content", []) if block.get("type") == "tool_use"]
        valid = len(calls) == 1 and calls[0].get("name") == "Echo" and calls[0].get("input") == {"value": "ok"}
        # Only the proposal is checked. No tool, shell, MCP server, or project is executed.
        print(json.dumps({"ok": valid, "authenticated_adapter": True, "tool_proposal_valid": valid,
                          "process_timing_reported": metadata.get("process_timing_reported") is True,
                          "model_reported": bool(metadata.get("reported_model")),
                          "usage_measured": metadata.get("usage_measured") is True, **observed}))
        return 0 if valid else 1
    except cliBackend.BackendError as error:
        safe = {"not_found", "backend_version_unverified", "not_logged_in", "cli_failed", "incomplete_response",
                "invalid_response", "invalid_usage", "invalid_tool_call", "invalid_tool_choice",
                "native_tool_execution_detected", "timeout", "wrong_model"}
        print(json.dumps({"ok": False, "reason": error.code if error.code in safe else "backend_error"}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "reason": "configuration_or_io_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
