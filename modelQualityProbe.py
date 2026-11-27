# SPDX-License-Identifier: MIT
"""Explicitly run two synthetic model calls; no project tools are executed.

This probes a backend directly, not Claude Code or its proxy. It consumes model
usage and cannot establish general coding quality or native feature parity.
"""
import argparse
import json
import secrets
import time
import urllib.error


PROBE_TIMEOUT_SECONDS = 180
TOOL = {"name": "Lookup", "description": "Look up the requested key.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}},
                         "required": ["key"], "additionalProperties": False}}


def evaluate_token_counter(call, model, measure=None):
    """Probe whether a counter notices synthetic text and tool definitions.

    Positive results are only a sanity check, never proof of exact accounting.
    The caller supplies the transport and receives no real conversation content.
    """
    base = {"model": model, "messages": [{"role": "user", "content": "short"},
            {"role": "assistant", "content": "acknowledged"}, {"role": "user", "content": "hello"}]}
    long_text = "synthetic probe description " * 500
    tool = {"name": "counter_probe", "description": long_text,
            "input_schema": {"type": "object", "properties": {}}}
    cases = {
        "base": base,
        "tool": {**base, "tools": [tool]},
        "text": {**base, "messages": [*base["messages"][:-1], {"role": "user", "content": long_text}]},
        "system": {**base, "system": long_text},
        "history": {**base, "messages": [{"role": "user", "content": long_text},
                    *base["messages"][1:]]},
    }
    values = [call(body) for body in cases.values()]
    if any(type(value) is not int or value < 0 for value in values):
        return {"status": "invalid_response", "usable_for_budget": False}
    base_count, tool_count, text_count, system_count, history_count = values
    text_observed = text_count > base_count
    tool_observed = tool_count > base_count
    system_observed = system_count > base_count
    history_observed = history_count > base_count
    result = {"status": "sanity_passed" if all((text_observed, tool_observed, system_observed, history_observed)) else "incomplete",
              "text_observed": text_observed, "tool_observed": tool_observed,
              "system_observed": system_observed, "history_observed": history_observed,
              "usable_for_budget": False}
    if measure is not None:
        measured = [measure(body) for body in cases.values()]
        valid = all(type(value) is int and value >= 0 for value in measured)
        result["usage_comparison"] = ("matches" if measured == values else "mismatch") if valid else "unreported"
        if result["usage_comparison"] == "mismatch":
            result["status"] = "incomplete"
        elif result["usage_comparison"] == "unreported" and result["status"] == "sanity_passed":
            result["status"] = "unverified"
    return result


class ProbeDeadline(TimeoutError):
    """The shared deadline for the entire probe has expired."""


def _remaining(deadline):
    if deadline is None:
        return float("inf")
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ProbeDeadline("probe_deadline")
    return seconds


def _timed_call(call, body, deadline):
    _remaining(deadline)
    try:
        return call(body)
    finally:
        # A backend may return a valid reply, or report cancellation, after expiry.
        _remaining(deadline)


def evaluate(call, model, deadline=None):
    """Check a single proposed tool call and a synthetic result continuation."""
    started = time.monotonic()
    base = {"model": model, "max_tokens": 128, "stream": False, "tools": [TOOL]}
    first = {**base, "tool_choice": {"type": "tool", "name": "Lookup"},
             "messages": [{"role": "user", "content": "Call Lookup with key code."}]}
    message, metadata = _timed_call(call, first, deadline)
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
    reply, next_metadata = _timed_call(call, second, deadline)
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


def evaluate_thinking(call, model, deadline=None):
    """Run one extra synthetic call that requests extended thinking.

    The backend either emits Anthropic-style thinking blocks (it can serve
    --local-compat sessions with reasoning visible), returns plain text only
    (thinking stays disabled for this model), or rejects the thinking parameter
    (reported as 'error' without failing the primary tool round-trip).
    """
    started = time.monotonic()
    # Manual thinking needs at least 1024 tokens and room for a final answer.
    body = {"model": model, "max_tokens": 1280, "stream": False,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "Think briefly, then reply with only: probe"}]}
    try:
        message, metadata = _timed_call(call, body, deadline)
    except ProbeDeadline:
        raise
    except Exception:
        # Provider errors can contain credentials or raw model output; keep the reason coarse.
        return {"thinking_status": "error", "thinking_blocks": 0, "signed": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    if metadata.get("model_matches_requested") is not True:
        return {"thinking_status": "unknown", "thinking_blocks": 0, "signed": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return {"thinking_status": "error", "thinking_blocks": 0, "signed": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    thinking_blocks = [block for block in blocks if isinstance(block, dict) and block.get("type") == "thinking"]
    if thinking_blocks:
        valid = all(isinstance(block.get("thinking"), str) and block["thinking"].strip()
                    for block in thinking_blocks)
        status = "present" if valid else "invalid"
        signed = all(isinstance(block.get("signature"), str) and block["signature"]
                     for block in thinking_blocks)
    elif (message.get("stop_reason") == "end_turn" and blocks
          and all(isinstance(block, dict) and block.get("type") == "text"
                  and isinstance(block.get("text"), str) for block in blocks)
          and any(block["text"].strip() for block in blocks)):
        status, signed = "absent", False
    else:
        status, signed = "invalid", None
    return {"thinking_status": status, "thinking_blocks": len(thinking_blocks), "signed": signed,
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
        if provider == "openrouter":
            upstream = values.get("OPENROUTER_UPSTREAM_PROVIDER") or ""
            policy = {"only": [upstream], "allow_fallbacks": False} if upstream else {"allow_fallbacks": False}
            body = {**body, "provider": policy}
        options = {"timeout": min(60, _remaining(deadline))}
        if deadline is not None:
            options["deadline"] = deadline
        status, message = dashboard._http_json(url, headers, body, **options)
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
    parser.add_argument("--thinking", action="store_true",
                        help="after a passed round-trip, spend one more call probing thinking blocks")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("--run is required; real model inference consumes usage")
    try:
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        call, model = configured_call(args.provider, deadline=deadline)
        result = evaluate(call, model, deadline=deadline)
        if args.thinking and isinstance(result, dict) and result.get("reason") == "passed":
            try:
                result["thinking"] = evaluate_thinking(call, model, deadline=deadline)
            except ProbeDeadline:
                raise
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError):
                result["thinking"] = {"thinking_status": "error", "thinking_blocks": 0, "signed": None,
                                      "elapsed_ms": 0}
    except TimeoutError:
        # Same label as routeDashboard.run_tool_roundtrip: a deadline has to surface as
        # probe_timeout, not as a configuration failure.
        result = {"ok": False, "reason": "probe_timeout"}
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        result = {"ok": False, "reason": "configuration_or_request_failed"}
    except Exception:
        # Provider-specific errors can contain credentials or response text.
        result = {"ok": False, "reason": "backend_failed"}
    print(json.dumps({"provider": args.provider, "scope": "backend_only", **result}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
