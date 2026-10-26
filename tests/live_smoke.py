"""Opt-in live check against the configured provider.

The suite never calls a real provider. This script does one small request so a real
key, model id and network path can be confirmed after a configuration change.

    python tests/live_smoke.py                # OpenRouter: key limit + one 16-token reply
    python tests/live_smoke.py --provider local
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
        status, data, _ = call(url, key, {"model": model, "max_tokens": 16, "stream": False,
                                          "messages": [{"role": "user", "content": "Reply with exactly OK"}]})
        text = "".join(block.get("text", "") for block in (data.get("content") or [])
                       if isinstance(block, dict) and block.get("type") == "text").strip()
        result["inference"] = {"status": status, "ms": round((time.perf_counter() - started) * 1000, 1),
                               "reply": text[:40], "ok": status == 200 and bool(text)}
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
    options = parser.parse_args()

    values = load(options.provider)
    spec = dash.CONFIG_FILES[options.provider]
    present = {name: bool(values.get(name)) for name in spec["fields"] if name not in spec["secret"]}
    present["secret_present"] = any(bool(values.get(name)) for name in spec["secret"])
    if options.dry_run:
        report = {"provider": options.provider, "dry_run": True, "configured": present,
                  "file": str(spec["path"])}
    else:
        report = check_openrouter(values) if options.provider == "openrouter" else check_local(values)

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

    if options.dry_run:
        return 0 if all(values.get(name) for name in spec["required"]) else 2
    if report.get("reason") == "configuration_missing":
        return 2
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
