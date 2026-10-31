"""Explicitly run two synthetic model calls; no project tools are executed.

This probes a backend directly, not Claude Code or its proxy. It consumes model
usage and cannot establish general coding quality or native feature parity.
"""
import argparse
import json
import secrets
import time
import urllib.error


TOOL = {"name": "Lookup", "description": "Look up the requested key.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}},
                         "required": ["key"], "additionalProperties": False}}


def evaluate(call, model, deadline=None):
    """Check a single proposed tool call and a synthetic result continuation."""
    started = time.monotonic()
    base = {"model": model, "max_tokens": 128, "stream": False, "tools": [TOOL]}
    first = {**base, "tool_choice": {"type": "tool", "name": "Lookup"},
             "messages": [{"role": "user", "content": "Call Lookup with key code."}]}
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("probe_deadline")
    message, metadata = call(first)
    blocks = message.get("content") if isinstance(message, dict) else None
    calls = [block for block in blocks or [] if isinstance(block, dict) and block.get("type") == "tool_use"] if isinstance(blocks, list) else []
    usage_first = metadata.get("usage_measured") is True
    model_first = metadata.get("model_reported") is True
    if (not isinstance(message, dict) or message.get("stop_reason") != "tool_use"
            or len(calls) != 1 or calls[0].get("name") != "Lookup" or calls[0].get("input") != {"key": "code"}
            or not isinstance(calls[0].get("id"), str) or not 0 < len(calls[0]["id"]) <= 200):
        return {"ok": False, "reason": "invalid_tool_proposal", "tool_proposal_valid": False,
                "first_usage_reported": usage_first, "first_model_reported": model_first,
                "first_model_matches_requested": metadata.get("model_matches_requested")}
    value = "probe-" + secrets.token_hex(8)
    safe_call = {"type": "tool_use", "id": calls[0]["id"], "name": "Lookup", "input": {"key": "code"}}
    second = {**base, "tool_choice": {"type": "none"}, "messages": [first["messages"][0],
              {"role": "assistant", "content": [safe_call]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"],
                                               "content": value},
                                              {"type": "text", "text": "Reply with only the exact value returned by Lookup."}]}]}
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("probe_deadline")
    reply, next_metadata = call(second)
    content = reply.get("content") if isinstance(reply, dict) else None
    valid = (isinstance(reply, dict) and reply.get("stop_reason") == "end_turn"
             and isinstance(content, list) and all(isinstance(block, dict) for block in content)
             and not any(block.get("type") == "tool_use" for block in content)
             and "".join(block.get("text", "") for block in content
                         if block.get("type") == "text" and isinstance(block.get("text"), str)).strip() == value)
    return {"ok": valid, "reason": "passed" if valid else "continuation_failed", "tool_proposal_valid": True,
            "tool_result_understood": valid, "first_usage_reported": usage_first,
            "second_usage_reported": next_metadata.get("usage_measured") is True,
            "first_model_reported": model_first, "second_model_reported": next_metadata.get("model_reported") is True,
            "first_model_matches_requested": metadata.get("model_matches_requested"),
            "second_model_matches_requested": next_metadata.get("model_matches_requested"),
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def configured_call(provider, dashboard=None, deadline=None, values=None):
    if provider == "cli":
        import cliBackend
        import routeConfig
        values = dict(values) if values is not None else routeConfig.Store().read_provider("cli")
        backend = cliBackend.Backend(values)
        def call(body):
            message, metadata = backend.generate(body, lambda: deadline is not None and time.monotonic() >= deadline)
            reported = metadata.get("reported_model")
            return message, {"usage_measured": metadata.get("usage_measured"),
                             "model_reported": isinstance(reported, str) and bool(reported),
                             "model_matches_requested": reported == backend.model if reported and backend.model else None}
        return call, backend.protocol_model
    if dashboard is None:
        import routeDashboard as dashboard
    values = dict(values) if values is not None else dashboard.read_settings(provider)
    dashboard._validate_config(provider, values)
    if provider == "local":
        url, model, key = values["LOCAL_LLM_URL"], values["LOCAL_LLM_MODEL"], values.get("LOCAL_LLM_API_KEY", "")
    else:
        url, model, key = "https://openrouter.ai/api/v1/messages", values["OPENROUTER_MODEL"], values["ANTHROPIC_AUTH_TOKEN"]
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if key:
        headers["authorization"] = "Bearer " + key
    def call(body):
        status, message = dashboard._http_json(url, headers, body, timeout=60)
        if status != 200 or not isinstance(message, dict):
            raise ValueError("invalid_response")
        usage = message.get("usage")
        measured = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                                                    for key in ("input_tokens", "output_tokens"))
        reported = message.get("model")
        reported = reported if isinstance(reported, str) and reported else None
        return message, {"usage_measured": measured, "model_reported": reported is not None,
                         "model_matches_requested": reported == model if reported else None}
    return call, model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("local", "openrouter", "cli"))
    parser.add_argument("--run", action="store_true", help="perform up to two real inference calls")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("--run is required; real model inference consumes usage")
    try:
        deadline = time.monotonic() + 180
        call, model = configured_call(args.provider, deadline=deadline)
        result = evaluate(call, model, deadline=deadline)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        result = {"ok": False, "reason": "configuration_or_request_failed"}
    except Exception:
        # Provider-specific errors can contain credentials or response text.
        result = {"ok": False, "reason": "backend_failed"}
    print(json.dumps({"provider": args.provider, "scope": "backend_only", **result}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
