# SPDX-License-Identifier: MIT
"""Opt-in live check against the configured provider.

The suite never calls a real provider. This script does one small request so a real
key, model id and network path can be confirmed after a configuration change.

    python tests/live_smoke.py                # OpenRouter: key limit + one 16-token reply
    python tests/live_smoke.py --provider local  # Local: one 512-token reply
    python tests/live_smoke.py --dry-run      # only report which settings are present
    python tests/live_smoke.py --json

Exit codes: 0 pass, 1 fail, 2 missing configuration. A run costs roughly one cent
per thousand calls (16 output tokens on a small model).
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routeDashboard as dash  # noqa: E402  (local import: shares the config parser)
import modelQualityProbe  # noqa: E402


def total_input_usage(usage):
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    if any(key in usage for key in ("prompt_tokens", "prompt_tokens_details", "input_tokens_details")):
        return None
    counts = [usage.get(key, 0) for key in
              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    return sum(counts) if all(type(n) is int and n >= 0 for n in counts) else None


def load(provider):
    return dash.read_settings(provider)


def call(url, key, payload=None, timeout=30):
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if key:
        headers["authorization"] = "Bearer " + key
    started = time.perf_counter()
    status, data = dash._http_json(url, headers, payload, timeout=timeout)
    return status, data, round((time.perf_counter() - started) * 1000, 1)


def check_openrouter(values):
    key = values.get("ANTHROPIC_AUTH_TOKEN", "")
    model = values.get("OPENROUTER_MODEL", "")
    if not key or not model:
        return {"ok": False, "reason": "configuration_missing", "provider": "openrouter"}
    try:
        dash._validate_config("openrouter", values)
    except ValueError:
        return {"ok": False, "reason": "invalid_configuration", "provider": "openrouter"}
    result = {"provider": "openrouter", "model": model}
    try:
        status, data, ms = call("https://openrouter.ai/api/v1/key", key)
        row = data.get("data") if isinstance(data, dict) else None
        result["key"] = {"status": status, "ms": ms,
                         "limit_usd": (row or {}).get("limit"), "remaining_usd": (row or {}).get("limit_remaining")}
    except (urllib.error.URLError, OSError, ValueError) as error:
        result["key"] = {"status": None, "error": type(error).__name__}
    started = time.perf_counter()
    try:
        status, data, _ = call("https://openrouter.ai/api/v1/messages", key, {
            "model": model, "max_tokens": 16, "stream": False,
            "provider": dash._openrouter_provider_policy(values),
            "messages": [{"role": "user", "content": "Reply with exactly OK"}]})
        text = "".join(block.get("text", "") for block in (data.get("content") or [])
                       if isinstance(block, dict) and block.get("type") == "text").strip()
        result["inference"] = {"status": status, "ms": round((time.perf_counter() - started) * 1000, 1),
                               "model": data.get("model"), "reply": text[:40],
                               "ok": status == 200 and data.get("model") == model and bool(text)}
    except (urllib.error.URLError, OSError, ValueError) as error:
        result["inference"] = {"ok": False, "error": type(error).__name__}
    result["ok"] = bool(result.get("inference", {}).get("ok"))
    if not result["ok"]:
        result["reason"] = "inference_failed"
    return result


def check_local(values):
    url = (values.get("LOCAL_LLM_URL") or "").strip()
    model = (values.get("LOCAL_LLM_MODEL") or "").strip()
    if not url or not model:
        return {"ok": False, "reason": "configuration_missing", "provider": "local"}
    key = values.get("LOCAL_LLM_API_KEY", "")
    result = {"provider": "local", "model": model}
    started = time.perf_counter()
    try:
        status, data, _ = call(url, key, {"model": model, "max_tokens": 512, "stream": False,
                                          "messages": [{"role": "user", "content": "Reply with exactly OK"}]}, timeout=60)
        reason = None
        try:
            text = dash._connection_reply_text(data)
        except ValueError:
            text, reason = "", "invalid_response"
        if not reason:
            if status != 200:
                reason = "upstream_error"
            elif data.get("model") != model:
                reason = "wrong_model"
            elif text != "OK":
                reason = "unexpected_reply"
        result["inference"] = {"status": status, "ms": round((time.perf_counter() - started) * 1000, 1),
                               "ok": reason is None, "reason": reason, "reply": text[:40]}
    except (urllib.error.URLError, OSError, ValueError) as error:
        result["inference"] = {"ok": False, "error": type(error).__name__}
    result["ok"] = bool(result.get("inference", {}).get("ok"))
    if not result["ok"]:
        result["reason"] = "inference_failed"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("openrouter", "local"), default="openrouter")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--token-counter", action="store_true", help="also check whether Local token counts include a synthetic tool definition")
    options = parser.parse_args()
    if options.token_counter and (options.provider != "local" or options.dry_run):
        parser.error("--token-counter requires --provider local without --dry-run")

    values = load(options.provider)
    spec = dash.CONFIG_FILES[options.provider]
    present = {name: bool(values.get(name)) for name in spec["fields"] if name not in spec["secret"]}
    present["secret_present"] = any(bool(values.get(name)) for name in spec["secret"])
    if options.dry_run:
        report = {"provider": options.provider, "dry_run": True, "configured": present,
                  "file": str(spec["path"])}
    else:
        report = check_openrouter(values) if options.provider == "openrouter" else check_local(values)
        if options.token_counter and report.get("ok"):
            def count(payload):
                status, data, _ = call(values["LOCAL_LLM_URL"].rstrip("/") + "/count_tokens",
                                       values.get("LOCAL_LLM_API_KEY", ""), payload, timeout=30)
                return data.get("input_tokens") if status == 200 and isinstance(data, dict) else None
            def measure(payload):
                status, data, _ = call(values["LOCAL_LLM_URL"], values.get("LOCAL_LLM_API_KEY", ""),
                                       {**payload, "max_tokens": 32, "stream": False}, timeout=60)
                usage = data.get("usage") if status == 200 and isinstance(data, dict) else None
                return total_input_usage(usage)
            try:
                report["token_counter"] = modelQualityProbe.evaluate_token_counter(count, values["LOCAL_LLM_MODEL"], measure)
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                report["token_counter"] = {"status": "unavailable", "usable_for_budget": False}

    if options.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        if options.dry_run:
            ready = all(values.get(name) for name in spec["required"])
            print(f"[{options.provider}] dry-run · required settings {'present' if ready else 'missing'}")
        else:
            print(f"[{options.provider}] {'ok' if report.get('ok') else (report.get('reason') or 'failed')}")
        for name, value in (report.get("key") or {}).items():
            print(f"  key.{name}: {value}")
        for name, value in (report.get("inference") or {}).items():
            print(f"  inference.{name}: {value}")
        for name, value in (report.get("token_counter") or {}).items():
            print(f"  token_counter.{name}: {value}")

    if options.dry_run:
        return 0 if all(values.get(name) for name in spec["required"]) else 2
    if report.get("reason") == "configuration_missing":
        return 2
    if options.token_counter and report.get("token_counter", {}).get("status") != "sanity_passed":
        return 1
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
