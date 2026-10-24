"""Loopback dashboard for one OpenRouter routing session.

Read-only view built from data this launcher already produces:

* ``.route.log`` beside the launcher: every routing decision the proxy made.
* Claude Code transcripts: per-turn token usage and which provider answered.
* OpenRouter ``/api/v1/key``: monthly limit, spend and reset date.

The server binds loopback only, never prints the provider key, never serves
prompt text and never writes anything. Run it next to a routing session:

    python routeDashboard.py --port 8787
"""

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROUTE_LOG = ROOT / ".route.log"
PROJECTS = Path.home() / ".claude" / "projects"
ACCOUNT_TTL = 60
SESSION_SUFFIX = ".jsonl"
ROUTE_WINDOW = 60
SHORT_WINDOW = 10
TIMELINE_HOURS = 24
BYTES_CAP = 65536
SOURCE_LABELS = {"external": "OpenRouter", "plan": "Plan(Anthropic)"}

_LOCK = threading.Lock()
_SESSIONS = {}
_ACCOUNT = {"at": 0.0, "data": None}
_PRICING = {"at": 0.0, "data": None}
_SNAPSHOT = {"at": 0.0, "data": None}
PRICING_TTL = 3600
SCAN_DAYS = 7
SCAN_FILES = 300


def read_env(path):
    """Read the two routing settings without touching anything else."""
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
    return values


def parse_route_log(path=None, limit=5000):
    """Return the tail of the route log as events with a UTC timestamp."""
    path = Path(path or ROUTE_LOG)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines()[-limit:]:
        if not line.strip():
            continue
        stamp, _, rest = line.partition(" ")
        rest = rest.strip()
        if rest == "--- proxy start ---":
            events.append({"ts": stamp, "event": "proxy_start"})
            continue
        if not rest.startswith("{"):
            continue
        try:
            payload = json.loads(rest)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        events.append({"ts": stamp, **payload})
    return events


def _age_seconds(stamp, now):
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds())


def _local_hour(stamp):
    """Bucket key in local time so daily totals match the user's clock."""
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return then.astimezone().strftime("%Y-%m-%dT%H")


def routing_state(events, now=None, idle_after=300):
    """Summarise the newest decisions: what is serving traffic right now."""
    now = now or datetime.now(timezone.utc)
    selected = [e for e in events if e.get("event") == "route_selected"]
    last = selected[-1] if selected else None
    age = _age_seconds(last["ts"], now) if last else None
    if last is None:
        state = "unknown"
    elif age is not None and age > idle_after:
        state = "idle"
    elif last.get("provider") in ("openrouter", "local"):
        state = "external"
    else:
        state = "plan"

    def aggregate(minutes):
        counts = {"external": 0, "plan": 0}
        plan_reasons = {}
        failures = {}
        models = {}
        circuits = 0
        kept = []
        for event in events:
            age_sec = _age_seconds(event["ts"], now)
            if age_sec is None or age_sec > minutes * 60:
                continue
            name = event.get("event")
            if name == "route_selected":
                key = "external" if event.get("provider") in ("openrouter", "local") else "plan"
                counts[key] += 1
                if key == "plan":
                    reason = event.get("reason") or "unknown"
                    detail = event.get("detail")
                    label = f"{reason}:{detail}" if detail else reason
                    plan_reasons[label] = plan_reasons.get(label, 0) + 1
                served = event.get("model")
                if isinstance(served, str) and served and len(served) <= 200:
                    models[f"{key}:{served}"] = models.get(f"{key}:{served}", 0) + 1
            elif name == "external_attempt_failed":
                status = event.get("status")
                label = str(status) if isinstance(status, int) else (event.get("category") or "unknown")
                if event.get("validation"):
                    label = f"{label}:{event['validation']}"
                failures[label] = failures.get(label, 0) + 1
            elif name == "circuit_open":
                circuits += 1
            kept.append(event)
        return {"counts": counts,
                "plan_reasons": sorted(plan_reasons.items(), key=lambda item: -item[1])[:6],
                "failures": sorted(failures.items(), key=lambda item: -item[1])[:6],
                "models": sorted(models.items(), key=lambda item: -item[1])[:6],
                "circuit_opens": circuits, "events": kept}

    wide, short = aggregate(ROUTE_WINDOW), aggregate(SHORT_WINDOW)
    starts = [e for e in events if e.get("event") == "proxy_start"]
    last_model = last.get("model") if last and isinstance(last.get("model"), str) and 0 < len(last["model"]) <= 200 else None
    return {
        "state": state,
        "last": {"ts": last["ts"], "provider": last.get("provider"), "reason": last.get("reason"),
                 "detail": last.get("detail"), "model": last_model,
                 "age_seconds": round(age, 1) if age is not None else None} if last else None,
        "window_minutes": ROUTE_WINDOW,
        "counts": wide["counts"],
        "plan_reasons": wide["plan_reasons"],
        "failures": wide["failures"],
        "models": wide["models"],
        "circuit_opens": wide["circuit_opens"],
        "short": {"minutes": SHORT_WINDOW, "counts": short["counts"], "plan_reasons": short["plan_reasons"],
                  "failures": short["failures"], "models": short["models"],
                  "circuit_opens": short["circuit_opens"]},
        "proxy_started_at": starts[-1]["ts"] if starts else None,
        "recent": [{"ts": e["ts"], "event": e.get("event"), "provider": e.get("provider"),
                    "reason": e.get("reason"), "detail": e.get("detail"),
                    "status": e.get("status"), "category": e.get("category")}
                   for e in wide["events"][-15:]],
    }


def _classify(message_id):
    # Anthropic message ids start with msg_01; replacement providers use other shapes.
    return "plan" if str(message_id or "").startswith("msg_01") else "external"


def _new_session_state():
    return {"offset": 0, "turns": 0, "external": 0, "plan": 0, "last_ts": None,
            "tokens": {"external": [0, 0, 0, 0], "plan": [0, 0, 0, 0]},
            "models": {}, "hours": {}}


def _read_appended(path, state):
    """Return complete new JSONL lines as bytes, keeping a partial trailing line for later."""
    stat = path.stat()
    size, offset = stat.st_size, state.get("offset", 0)
    if size < offset:
        state.clear()
        state.update(_new_session_state())
        offset = 0
    if size == offset:
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(size - offset)
    head, sep, _tail = chunk.rpartition(b"\n")
    if not sep:
        return []
    state["offset"] = offset + len(head) + 1
    return head.split(b"\n")


def _new_hour_bucket():
    return {"external": {"turns": 0, "tokens": [0, 0, 0, 0]},
            "plan": {"turns": 0, "tokens": [0, 0, 0, 0]}}


def _empty_usage(now=None):
    now = now or datetime.now()
    zero = {"external": [0, 0, 0, 0], "plan": [0, 0, 0, 0]}
    return {"totals": {k: list(v) for k, v in zero.items()},
            "today": {k: list(v) for k, v in zero.items()},
            "timeline": [], "cost_usd": None,
            "current_hour": now.strftime("%Y-%m-%dT%H"), "sessions": [], "session_count": 0}


def _session_cost(tokens, pricing):
    if not pricing or not pricing.get("ok"):
        return None
    return (tokens[0] * pricing["prompt"] + tokens[1] * pricing["completion"] +
            tokens[2] * pricing["cache_read"] + tokens[3] * pricing["cache_write"])


def scan_sessions(projects=None, max_files=SCAN_FILES, now=None, pricing=None):
    """Incrementally total token usage per provider from Claude Code transcripts."""
    projects = Path(projects or PROJECTS)
    now = now or datetime.now()
    if not projects.is_dir():
        return _empty_usage(now)
    cutoff = (now - timedelta(days=SCAN_DAYS)).timestamp()
    candidates = []
    try:
        for entry in projects.rglob("*" + SESSION_SUFFIX):
            try:
                modified = entry.stat().st_mtime
            except OSError:
                continue
            if modified >= cutoff:
                candidates.append((modified, entry))
    except OSError:
        return _empty_usage(now)
    candidates.sort(reverse=True)
    if max_files:
        candidates = candidates[:max_files]

    with _LOCK:
        seen = set()
        for _mtime, path in candidates:
            key = str(path)
            seen.add(key)
            state = _SESSIONS.setdefault(key, _new_session_state())
            try:
                lines = _read_appended(path, state)
            except OSError:
                continue
            for line in lines:
                if b'"assistant"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "assistant":
                    continue
                message = record.get("message") or {}
                usage = message.get("usage") or {}
                source = _classify(message.get("id"))
                delta = [int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                         int(usage.get("cache_read_input_tokens") or 0),
                         int(usage.get("cache_creation_input_tokens") or 0)]
                state["turns"] += 1
                state[source] += 1
                for index in range(4):
                    state["tokens"][source][index] += delta[index]
                model = message.get("model")
                if model:
                    state["models"][model] = state["models"].get(model, 0) + 1
                stamp = record.get("timestamp")
                if stamp:
                    state["last_ts"] = stamp
                    hour = _local_hour(stamp)
                    if hour:
                        bucket = state["hours"].setdefault(hour, _new_hour_bucket())
                        bucket[source]["turns"] += 1
                        for index in range(4):
                            bucket[source]["tokens"][index] += delta[index]
        for key in list(_SESSIONS):
            if key not in seen:
                del _SESSIONS[key]

        sessions, totals = [], {"external": [0, 0, 0, 0], "plan": [0, 0, 0, 0]}
        hour_totals = {"external": [0, 0, 0, 0], "plan": [0, 0, 0, 0]}
        buckets = {}
        today = now.strftime("%Y-%m-%d")
        current_hour = now.strftime("%Y-%m-%dT%H")
        total_cost = 0.0
        for path, state in _SESSIONS.items():
            if not state["turns"]:
                continue
            cost = _session_cost(state["tokens"]["external"], pricing)
            total_cost += cost or 0.0
            sessions.append({
                "project": Path(path).parent.name.replace("D--", "").replace("--", "/"),
                "session": Path(path).name[:8],
                "last": state["last_ts"],
                "turns": state["turns"],
                "external": state["external"],
                "plan": state["plan"],
                "tokens": {name: list(value) for name, value in state["tokens"].items()},
                "cost_usd": round(cost, 6) if cost is not None else None,
                "models": sorted(state["models"].items(), key=lambda item: -item[1])[:3],
            })
            for name in ("external", "plan"):
                for index in range(4):
                    totals[name][index] += state["tokens"][name][index]
            for hour, bucket in state["hours"].items():
                merged = buckets.setdefault(hour, _new_hour_bucket())
                for name in ("external", "plan"):
                    merged[name]["turns"] += bucket[name]["turns"]
                    for index in range(4):
                        merged[name]["tokens"][index] += bucket[name]["tokens"][index]
                if hour.startswith(today):
                    for name in ("external", "plan"):
                        for index in range(4):
                            hour_totals[name][index] += bucket[name]["tokens"][index]

        timeline = []
        for offset in range(TIMELINE_HOURS - 1, -1, -1):
            hour = (now - timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
            bucket = buckets.get(hour) or _new_hour_bucket()
            timeline.append({"hour": hour,
                             "external": {"turns": bucket["external"]["turns"],
                                          "tokens": bucket["external"]["tokens"]},
                             "plan": {"turns": bucket["plan"]["turns"],
                                      "tokens": bucket["plan"]["tokens"]}})
        sessions.sort(key=lambda item: item["last"] or "", reverse=True)
        return {"totals": totals, "today": hour_totals, "current_hour": current_hour,
                "timeline": timeline, "cost_usd": round(total_cost, 6) if pricing and pricing.get("ok") else None,
                "sessions": sessions[:8], "session_count": len(sessions)}


def account_state(key, cache=None, now=None):
    """Monthly key limit and spend from OpenRouter, cached for a minute."""
    now = time.monotonic() if now is None else now
    cache = _ACCOUNT if cache is None else cache
    cached = cache.get("data")
    if cached and now - cache.get("at", 0) < ACCOUNT_TTL:
        return cached
    result = {"ok": False, "error": None}
    if not key:
        result["error"] = "configuration_missing"
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            request = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                             headers={"authorization": "Bearer " + key})
            with opener.open(request, timeout=8) as response:
                data = json.loads(response.read(65536 + 1))
            row = data.get("data") if isinstance(data, dict) else None
            if not isinstance(row, dict):
                result["error"] = "invalid_response"
            else:
                result = {"ok": True, "error": None,
                          "limit_usd": row.get("limit"),
                          "spent_usd": None,
                          "remaining_usd": row.get("limit_remaining"),
                          "reset": row.get("limit_reset"),
                          "is_free_tier": row.get("is_free_tier")}
                for budget in row.get("budgets") or []:
                    if isinstance(budget, dict) and budget.get("scope") == "key_limit":
                        result["spent_usd"] = budget.get("spend_usd")
                        result["resets_at"] = budget.get("resets_at")
                        break
        except urllib.error.HTTPError as error:
            result["error"] = f"http_{error.code}"
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            result["error"] = "connection_failed"
        except ValueError:
            result["error"] = "invalid_response"
    cache["data"], cache["at"] = result, now
    return result


def model_pricing(key, model, cache=None, now=None):
    """USD per token for the configured model, cached for an hour."""
    now = time.monotonic() if now is None else float(now)
    cache = _PRICING if cache is None else cache
    cached = cache.get("data")
    if cached and now - cache.get("at", 0) < PRICING_TTL:
        return cached
    result = {"ok": False, "error": None}
    if not key or not model:
        result["error"] = "configuration_missing"
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            request = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                             headers={"authorization": "Bearer " + key})
            with opener.open(request, timeout=8) as response:
                data = json.loads(response.read(4 * 1024 * 1024 + 1))
            rows = data.get("data") if isinstance(data, dict) else None
            row = next((entry for entry in rows or [] if isinstance(entry, dict) and entry.get("id") == model), None)
            pricing = (row or {}).get("pricing") or {}
            if not pricing:
                result["error"] = "model_not_found"
            else:
                def rate(name):
                    try:
                        return float(pricing.get(name) or 0)
                    except (TypeError, ValueError):
                        return 0.0
                result = {"ok": True, "error": None, "prompt": rate("prompt"),
                          "completion": rate("completion"), "cache_read": rate("input_cache_read"),
                          "cache_write": rate("input_cache_write")}
        except urllib.error.HTTPError as error:
            result["error"] = f"http_{error.code}"
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            result["error"] = "connection_failed"
        except ValueError:
            result["error"] = "invalid_response"
    cache["data"], cache["at"] = result, now
    return result


def build_alerts(routing, usage, account, pricing):
    """Actionable warnings; the page shows them in severity order."""
    alerts = []
    short = routing.get("short") or {}
    counts = short.get("counts") or {}
    total = (counts.get("external") or 0) + (counts.get("plan") or 0)
    minutes = short.get("minutes") or SHORT_WINDOW
    if short.get("circuit_opens"):
        alerts.append({"level": "critical", "title": "외부 회로 차단",
                       "detail": f"최근 {minutes}분에 회로가 {short['circuit_opens']}회 열렸습니다. 외부 시도가 실패해 Plan으로 우회 중입니다."})
    if total and (counts.get("plan") or 0) / total >= 0.5:
        top = (short.get("plan_reasons") or [[None, 0]])[0]
        detail = f"최근 {minutes}분 결정 {total}건 중 {counts['plan']}건이 Plan입니다."
        if top[0]:
            detail += f" 주요 사유: {top[0]}"
        alerts.append({"level": "warn", "title": "Plan 우회 급증", "detail": detail})
    rejected = [(label, count) for label, count in short.get("failures") or []
                if str(label).startswith(("400", "413", "422"))]
    if rejected:
        label, count = rejected[0]
        alerts.append({"level": "warn", "title": "외부 요청 거부",
                       "detail": f"{label} 응답 {count}건. 요청 형식이나 모델 한도 문제일 수 있습니다."})
    if routing.get("state") == "plan":
        last = routing.get("last") or {}
        reason = last.get("reason") or "-"
        if last.get("detail"):
            reason = f"{reason} / {last['detail']}"
        alerts.append({"level": "info", "title": "현재 Plan 경로", "detail": f"마지막 결정 사유: {reason}"})
    if account.get("ok"):
        limit, remaining = account.get("limit_usd"), account.get("remaining_usd")
        if all(isinstance(value, (int, float)) for value in (limit, remaining)) and limit > 0:
            if remaining / limit <= 0.1:
                alerts.append({"level": "critical", "title": "OpenRouter 잔액 부족",
                               "detail": f"한도 ${limit:.2f} 중 남은 금액 ${remaining:.2f}입니다."})
    elif account.get("error") and account["error"] != "configuration_missing":
        alerts.append({"level": "info", "title": "크레딧 확인 실패", "detail": str(account["error"])})
    if pricing and not pricing.get("ok") and pricing.get("error") not in (None, "configuration_missing"):
        alerts.append({"level": "info", "title": "단가 조회 실패",
                       "detail": "비용 추정을 표시할 수 없습니다: " + str(pricing.get("error"))})
    order = {"critical": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda item: order.get(item["level"], 3))
    return alerts


def snapshot():
    """Everything the page renders, in one JSON document."""
    now = datetime.now(timezone.utc)
    config = read_env(ROOT / ".env.openrouter")
    key = config.get("ANTHROPIC_AUTH_TOKEN")
    model = config.get("OPENROUTER_MODEL") or None
    routing = routing_state(parse_route_log(), now=now)
    pricing = model_pricing(key, model)
    account = account_state(key)
    usage = scan_sessions(pricing=pricing)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "routing": routing,
        "usage": usage,
        "account": account,
        "pricing": pricing,
        "alerts": build_alerts(routing, usage, account, pricing),
        "model": model,
    }


def cached_snapshot():
    """Serve the last completed snapshot so a slow first scan never blocks the page."""
    data = _SNAPSHOT.get("data")
    if data is not None:
        return data
    now = datetime.now(timezone.utc)
    config = read_env(ROOT / ".env.openrouter")
    key = config.get("ANTHROPIC_AUTH_TOKEN")
    routing = routing_state(parse_route_log(), now=now)
    account = account_state(key)
    usage = _empty_usage()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "building": True,
        "routing": routing,
        "usage": usage,
        "account": account,
        "pricing": None,
        "alerts": build_alerts(routing, usage, account, None),
        "model": config.get("OPENROUTER_MODEL") or None,
    }


def _refresh_loop(interval):
    while True:
        try:
            _SNAPSHOT["data"] = snapshot()
            _SNAPSHOT["at"] = time.monotonic()
        except Exception:
            pass
        time.sleep(interval)


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>OpenRouter 라우팅 대시보드</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#0b1020;color:#e6ebf5;
 font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
h1{font-size:18px;margin:0 0 4px;font-weight:600}
.sub{color:#8b97ad;font-size:12px;margin-bottom:20px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:#141b2d;border:1px solid #24304a;border-radius:12px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8b97ad;margin:0 0 10px;font-weight:600}
.big{font-size:26px;font-weight:700;line-height:1.2}
.badge{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600}
.ext{background:#0f3b2e;color:#5ee6a8;border:1px solid #1c6b52}
.plan{background:#3a1f12;color:#ffb27a;border:1px solid #6d3a1d}
.idle{background:#232b3d;color:#9aa7bf;border:1px solid #34405a}
.unknown{background:#232b3d;color:#9aa7bf;border:1px solid #34405a}
.kv{display:flex;justify-content:space-between;padding:3px 0;color:#b9c4d6}
.kv b{color:#e6ebf5;font-weight:600}
.bar{height:8px;border-radius:6px;background:#1e2740;overflow:hidden;margin:10px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3f7dff,#5ee6a8)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:5px 4px;border-bottom:1px solid #202a41;white-space:nowrap}
th{color:#8b97ad;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#9aa7bf}
.wide{grid-column:1/-1}
.muted{color:#77839a}
.alerts{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.alert{border-radius:10px;padding:10px 12px;border:1px solid;font-size:13px}
.alert b{display:block;margin-bottom:2px}
.alert.critical{background:#3a1220;border-color:#7d2740;color:#ffb3c4}
.alert.warn{background:#3a2a12;border-color:#7d5527;color:#ffd39b}
.alert.info{background:#1a2438;border-color:#2c3b58;color:#a9bad6}
.timeline{display:flex;align-items:flex-end;gap:3px;height:120px;margin-top:6px}
.tl-col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%}
.tl-plan{background:#c9803a}
.tl-ext{background:#3fae7a}
.tl-labels{display:flex;gap:3px;margin-top:4px}
.tl-labels span{flex:1;font-size:9px;color:#77839a;text-align:center;font-family:ui-monospace,Consolas,monospace}
.legend{display:flex;gap:14px;font-size:11px;color:#9aa7bf;margin-top:8px}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}
</style></head><body>
<h1>OpenRouter 라우팅 대시보드</h1>
<div class="sub" id="stamp">불러오는 중…</div>
<div class="alerts" id="alerts"></div>
<div class="grid" id="grid"></div>
<script>
const esc = value => String(value ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('ko-KR');
const usd = value => value === null || value === undefined ? '—' : '$' + Number(value).toFixed(3);
const usd4 = value => value === null || value === undefined ? '—' : '$' + Number(value).toFixed(4);
const tokens = row => row ? (row[0] + row[1] + row[2] + row[3]) : 0;
const stateText = {external:'OpenRouter 사용 중', plan:'Plan 우회 중', idle:'유휴', unknown:'기록 없음'};
const stateClass = {external:'ext', plan:'plan', idle:'idle', unknown:'unknown'};

function card(title, body, cls) {
  return `<div class="card ${cls || ''}"><h2>${title}</h2>${body}</div>`;
}

function render(data) {
  const r = data.routing, u = data.usage, a = data.account;
  document.getElementById('stamp').textContent =
    `갱신 ${new Date(data.generated_at).toLocaleTimeString('ko-KR')} · 설정 모델 ${data.model || '미설정'}`
    + ` · 최근 응답 모델 ${r.last?.model || '—'} · 읽기 전용`;
  const age = r.last?.age_seconds;
  const ageText = age === null || age === undefined ? '아직 기록 없음'
    : age < 60 ? `${Math.round(age)}초 전` : `${Math.round(age / 60)}분 전`;
  const lastDetail = r.last ? [r.last.reason, r.last.detail].filter(Boolean).join(' / ') : '—';
  const status = card('지금 상태',
    `<div><span class="badge ${stateClass[r.state] || 'unknown'}">${stateText[r.state] || r.state}</span></div>
     <div class="kv"><span>마지막 결정</span><b>${esc(ageText)}</b></div>
     <div class="kv"><span>사유</span><b>${esc(lastDetail)}</b></div>
     <div class="kv"><span>실제 사용 모델</span><b class="mono">${esc(r.last?.model || '—')}</b></div>
     <div class="kv"><span>프록시 시작</span><b>${esc(r.proxy_started_at ? new Date(r.proxy_started_at).toLocaleString('ko-KR') : '—')}</b></div>`);

  const total = r.counts.external + r.counts.plan;
  const pct = total ? Math.round(r.counts.external / total * 100) : 0;
  const modelRows = (r.models || []).slice(0, 3)
    .map(([label, count]) => `<div class="kv"><span class="mono">${esc(label)}</span><b>${num(count)}</b></div>`).join('');
  const split = card(`최근 ${r.window_minutes}분 라우팅`,
    `<div class="big">${pct}% <span class="muted" style="font-size:13px">OpenRouter</span></div>
     <div class="bar"><i style="width:${pct}%"></i></div>
     <div class="kv"><span>OpenRouter</span><b>${num(r.counts.external)}건</b></div>
     <div class="kv"><span>Plan</span><b>${num(r.counts.plan)}건</b></div>
     <div class="kv"><span>회로 차단</span><b>${num(r.circuit_opens)}회</b></div>
     ${modelRows || '<div class="muted">모델 기록 없음</div>'}`);

  const reasons = r.plan_reasons.length
    ? r.plan_reasons.map(([k, v]) => `<div class="kv"><span class="mono">${esc(k)}</span><b>${num(v)}</b></div>`).join('')
    : '<div class="muted">Plan 우회 없음</div>';
  const failRows = r.failures.length
    ? r.failures.map(([k, v]) => `<div class="kv"><span class="mono">${esc(k)}</span><b>${num(v)}</b></div>`).join('')
    : '<div class="muted">실패 없음</div>';
  const why = card('Plan 우회 사유 / 외부 실패', reasons + '<div style="height:8px"></div>' + failRows);

  const limit = a.limit_usd, spent = a.spent_usd ?? null, remaining = a.remaining_usd;
  const used = limit && spent !== null ? Math.min(100, Math.round(spent / limit * 100)) : 0;
  const credit = card('OpenRouter 크레딧', a.ok
    ? `<div class="big">${usd(remaining)} <span class="muted" style="font-size:13px">남음</span></div>
       <div class="bar"><i style="width:${used}%"></i></div>
       <div class="kv"><span>이번 달 사용</span><b>${usd(spent)}</b></div>
       <div class="kv"><span>한도</span><b>${usd(limit)}</b></div>
       <div class="kv"><span>리셋</span><b>${esc(a.reset || '—')}${a.resets_at ? ' · ' + new Date(a.resets_at).toLocaleDateString('ko-KR') : ''}</b></div>`
    : `<div class="big muted">확인 불가</div><div class="mono">${esc(a.error || 'unknown')}</div>`);

  const todayExt = u.today.external, todayPlan = u.today.plan;
  const tokensCard = card('오늘 토큰 사용',
    `<div class="kv"><span>OpenRouter</span><b>${num(tokens(todayExt))}</b></div>
     <div class="kv"><span>Plan</span><b>${num(tokens(todayPlan))}</b></div>
     <div class="kv"><span>입력 / 출력 (외부)</span><b>${num(todayExt[0])} / ${num(todayExt[1])}</b></div>
     <div class="kv"><span>캐시 읽기 / 생성 (외부)</span><b>${num(todayExt[2])} / ${num(todayExt[3])}</b></div>
     <div class="kv"><span>추정 비용 (외부, 누적)</span><b>${usd4(u.cost_usd)}</b></div>`);

  const timeline = u.timeline || [];
  const peak = Math.max(1, ...timeline.map(hour => tokens(hour.external.tokens) + tokens(hour.plan.tokens)));
  const bars = timeline.map(hour => {
    const ext = tokens(hour.external.tokens), plan = tokens(hour.plan.tokens);
    return `<div class="tl-col" title="${esc(hour.hour)}시 · OpenRouter ${num(ext)} · Plan ${num(plan)}">
      <div class="tl-plan" style="height:${Math.round(plan / peak * 100)}%"></div>
      <div class="tl-ext" style="height:${Math.round(ext / peak * 100)}%"></div></div>`;
  }).join('');
  const labels = timeline.map((hour, index) =>
    `<span>${index % 3 === 0 ? hour.hour.slice(11) + '시' : ''}</span>`).join('');
  const timelineCard = card('시간대별 토큰 (최근 24시간)',
    timeline.length ? `<div class="timeline">${bars}</div><div class="tl-labels">${labels}</div>
      <div class="legend"><span><i style="background:#3fae7a"></i>OpenRouter</span>
      <span><i style="background:#c9803a"></i>Plan</span></div>`
      : '<div class="muted">데이터 없음</div>', 'wide');

  const rows = u.sessions.map(s => `<tr>
      <td>${esc(s.project)}</td><td class="mono">${esc(s.session)}</td>
      <td class="num">${num(s.turns)}</td><td class="num">${num(s.external)}</td><td class="num">${num(s.plan)}</td>
      <td class="num">${num(tokens(s.tokens.external))}</td><td class="num">${usd4(s.cost_usd)}</td>
      <td class="mono">${esc((s.models[0] || ['—'])[0])}</td></tr>`).join('');
  const sessions = card('세션별 사용 (최근 활동순)',
    u.sessions.length ? `<table><tr><th>프로젝트</th><th>세션</th><th>턴</th><th>외부</th><th>Plan</th><th>외부 토큰</th><th>비용(추정)</th><th>모델</th></tr>${rows}</table>`
      : '<div class="muted">세션 기록 없음</div>', 'wide');

  const recent = r.recent.slice().reverse().map(e => `<tr>
      <td class="mono">${esc(new Date(e.ts).toLocaleTimeString('ko-KR'))}</td>
      <td class="mono">${esc(e.event || '—')}</td>
      <td>${esc(e.provider || '—')}</td>
      <td>${esc([e.reason, e.detail].filter(Boolean).join(' / ') || '—')}</td>
      <td class="num">${esc(e.status ?? '')}</td></tr>`).join('');
  const recentCard = card('최근 라우팅 이벤트',
    r.recent.length ? `<table><tr><th>시각</th><th>이벤트</th><th>경로</th><th>사유</th><th>HTTP</th></tr>${recent}</table>`
      : '<div class="muted">아직 기록이 없습니다. 실행기로 세션을 시작하고 메시지를 보내면 나타납니다.</div>', 'wide');

  document.getElementById('alerts').innerHTML = (data.alerts || []).map(alert =>
    `<div class="alert ${esc(alert.level)}"><b>${esc(alert.title)}</b>${esc(alert.detail)}</div>`).join('');
  document.getElementById('grid').innerHTML =
    [status, split, credit, tokensCard, why, timelineCard, sessions, recentCard].join('');
}

async function tick() {
  try {
    const response = await fetch('/api/snapshot', { cache: 'no-store' });
    render(await response.json());
  } catch (error) {
    document.getElementById('stamp').textContent = '연결 끊김 — 대시보드 서버가 실행 중인지 확인하세요';
  }
}
tick(); setInterval(tick, 2000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, payload):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif self.path.split("?")[0] == "/api/snapshot":
            try:
                self._send(200, "application/json; charset=utf-8", json.dumps(cached_snapshot(), ensure_ascii=False))
            except Exception:
                self._send(500, "application/json; charset=utf-8",
                           json.dumps({"error": "snapshot_failed"}))
        elif self.path.split("?")[0] == "/api/status":
            try:
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(status_report(), ensure_ascii=False))
            except Exception:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": "status_failed"}))
        else:
            self._send(404, "application/json; charset=utf-8", json.dumps({"error": "not_found"}))

    def log_message(self, *args):
        pass


def serve(host="127.0.0.1", port=8787, refresh=3):
    threading.Thread(target=_refresh_loop, args=(max(1, refresh),), daemon=True).start()
    fallback = None
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        # A requested port can be reserved by Windows; any free loopback port is equivalent here.
        server = ThreadingHTTPServer((host, 0), Handler)
        fallback = "requested_port_unavailable"
    url = f"http://{host}:{server.server_address[1]}/"
    print(json.dumps({"dashboard": url, "route_log": str(ROUTE_LOG), "read_only": True,
                      **({"port_fallback": fallback} if fallback else {})}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def status_report(include_usage=False, offline=False):
    """Compact machine-readable status for scripts and agents.

    Deliberately cheap: routing state comes from ``.route.log`` and only the key
    limit needs the network, so a check stays well under a second. Token totals
    are opt-in because they scan transcripts.
    """
    now = datetime.now(timezone.utc)
    config = read_env(ROOT / ".env.openrouter")
    key = config.get("ANTHROPIC_AUTH_TOKEN")
    model = config.get("OPENROUTER_MODEL") or None
    routing = routing_state(parse_route_log(), now=now)
    account = {"ok": False, "error": "offline"} if offline else account_state(key)
    pricing, usage = None, _empty_usage()
    if include_usage:
        pricing = None if offline else model_pricing(key, model)
        usage = scan_sessions(pricing=pricing)
    alerts = build_alerts(routing, usage, account, pricing)
    last = routing.get("last") or {}
    critical = any(alert["level"] == "critical" for alert in alerts)
    report = {
        "ok": not critical and routing["state"] in ("external", "idle"),
        "state": routing["state"],
        "provider": last.get("provider"),
        "reason": last.get("reason"),
        "detail": last.get("detail"),
        "model": last.get("model"),
        "last_event_age_seconds": last.get("age_seconds"),
        "proxy_started_at": routing.get("proxy_started_at"),
        "window_minutes": routing.get("window_minutes"),
        "window_counts": routing.get("counts"),
        "plan_reasons": routing.get("plan_reasons"),
        "failures": routing.get("failures"),
        "models": routing.get("models"),
        "alerts": alerts,
        "account": {name: account.get(name) for name in
                    ("ok", "error", "limit_usd", "spent_usd", "remaining_usd", "resets_at")},
        "config": {"model": model, "route_log_exists": ROUTE_LOG.is_file(),
                   "route_log": str(ROUTE_LOG)},
        "generated_at": now.isoformat(timespec="seconds"),
    }
    if include_usage:
        report["usage"] = {"today": usage["today"], "cost_usd": usage["cost_usd"],
                           "session_count": usage["session_count"]}
    return report


def status_exit_code(report):
    """0 healthy, 2 currently on Plan, 3 critical alert, 4 no routing record yet."""
    if any(alert["level"] == "critical" for alert in report.get("alerts") or []):
        return 3
    if report.get("state") == "plan":
        return 2
    if report.get("state") == "unknown":
        return 4
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only loopback dashboard for OpenRouter routing")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback address to bind")
    parser.add_argument("--port", type=int, default=8787, help="Port to listen on")
    parser.add_argument("--refresh", type=int, default=3, help="Seconds between background scans")
    parser.add_argument("--status", action="store_true", help="Print one JSON status report and exit")
    parser.add_argument("--usage", action="store_true", help="Include token and cost totals (slower)")
    parser.add_argument("--offline", action="store_true", help="Skip OpenRouter account and pricing calls")
    parser.add_argument("--pretty", action="store_true", help="Indent the status report")
    args = parser.parse_args(argv)
    if args.status:
        report = status_report(include_usage=args.usage, offline=args.offline)
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
        return status_exit_code(report)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("Bind a loopback address only")
    serve(args.host, args.port, args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
