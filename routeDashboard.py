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
import os
import re
import secrets
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROUTE_LOG = ROOT / ".route.log"
PROXY_FILE = ROOT / "claudeRouteProxy.mjs"
PID_FILE = ROOT / ".dashboard.pid"
PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_PORT = 8787
ACCOUNT_TTL = 60
SESSION_SUFFIX = ".jsonl"
ROUTE_WINDOW = 60
SHORT_WINDOW = 10
TIMELINE_HOURS = 24
BYTES_CAP = 65536
SOURCE_LABELS = {"external": "OpenRouter", "plan": "Plan(Anthropic)"}
# Writing settings is limited to these two files and these keys; nothing else is touched.
CONFIG_FILES = {
    "openrouter": {"path": ROOT / ".env.openrouter",
                   "secret": {"ANTHROPIC_AUTH_TOKEN"},
                   "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                   "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")},
    "local": {"path": ROOT / ".env.local-llm",
              "secret": {"LOCAL_LLM_API_KEY"},
              "required": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL"),
              "fields": ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                         "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL")},
}
TOKEN = os.environ.pop("DASHBOARD_TOKEN", "") or secrets.token_urlsafe(24)

_LOCK = threading.Lock()
_SESSIONS = {}
_ACCOUNT = {"at": 0.0, "data": None}
_PRICING = {"at": 0.0, "data": None}
_SNAPSHOT = {"at": 0.0, "data": None}
PRICING_TTL = 3600

# Why a request left the replacement route, in one line, plus what the reader can do about it.
REASON_HELP = {
    "native_feature:plan_model": ["Plan 전용 모델", "Opus·Haiku 등은 설계상 Plan으로 갑니다. Sonnet으로 요청하세요."],
    "native_feature:token_count": ["토큰 수 계산", "count_tokens 요청은 항상 Plan입니다. 정상 동작입니다."],
    "native_feature:unsupported_beta": ["모르는 beta", "CLI가 새 beta를 보냈습니다. 실행기를 최신으로 갱신하세요."],
    "native_feature:unsupported_content_field": ["모르는 콘텐츠 필드", "이력 블록에 라우터가 모르는 필드가 있습니다. 실행기를 갱신하세요."],
    "native_feature:unsupported_content_block": ["모르는 콘텐츠 블록", "서버 도구·inline 블록처럼 외부가 표현할 수 없는 내용입니다."],
    "native_feature:unsupported_content": ["콘텐츠 형식", "메시지 내용 형식이 예상과 다릅니다."],
    "native_feature:unsupported_tool_definition": ["서버 전용 도구", "외부 제공자가 실행할 수 없는 도구 정의입니다."],
    "native_feature:unsupported_tool_choice": ["tool_choice 구성", "tool_choice에 예상 밖 필드가 있습니다."],
    "native_feature:unsupported_output_config": ["지원하지 않는 출력 설정", "structured output 등 외부가 지원하지 않는 설정입니다."],
    "native_feature:unsupported_thinking_config": ["thinking 구성", "thinking 설정에 예상 밖 필드가 있습니다."],
    "native_feature:unsupported_context_management": ["서버 문맥 편집", "외부 제공자는 문맥 편집을 지원하지 않습니다."],
    "native_feature:unsupported_message_shape": ["메시지 형식", "역할이나 필드 구성이 예상과 다릅니다."],
    "fallback": ["외부 실패 후 Plan", "이 요청만 Plan으로 처리했습니다. 다음 요청은 다시 외부로 시도합니다."],
    "circuit_open": ["외부 대기 중", "장애 후 30초 대기 또는 복구 확인 중입니다. 잠시 후 다시 시도하세요."],
}

HELP_SECTION = {
    "commands": [
        ["python routeDashboard.py --status", "한 줄 JSON + 종료 코드"],
        ["python routeDashboard.py --status --pretty", "읽기 좋게 들여쓰기"],
        ["python routeDashboard.py --status --usage", "토큰·비용까지 집계 (느림)"],
        ["python routeDashboard.py --status --offline", "OpenRouter 호출 생략"],
        ["powershell -File startClaudeDashboard.ps1", "고정 포트(8787)로 이 페이지 열기"],
        ["python routeDashboard.py --stop", "대시보드만 종료 (실행기 세션은 유지)"],
    ],
    "endpoints": [
        ["GET /api/status", "위와 같은 요약 JSON"],
        ["GET /api/snapshot", "이 페이지가 쓰는 전체 데이터"],
    ],
    "states": [["external", "OpenRouter 사용 중"], ["plan", "Plan 우회 중"], ["idle", "유휴(5분 이상 결정 없음)"],
               ["unknown", "기록 없음"]],
    "exit_codes": [["0", "정상"], ["2", "현재 Plan 경로"], ["3", "심각 경고"], ["4", "라우팅 기록 없음"]],
    "sources": [["라우팅 결정", "실행기 폴더의 .route.log (프록시가 기록)"],
                ["토큰·모델", "Claude Code 세션 기록 (~/.claude/projects)"],
                ["크레딧·단가", "OpenRouter /api/v1/key, /api/v1/models"],
                ["상태 JSON", "GET /api/status 또는 python routeDashboard.py --status"]],
}
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
        start = re.fullmatch(r"--- proxy start(?: code=(\S+))? ---", rest)
        if start:
            events.append({"ts": stamp, "event": "proxy_start",
                           **({"code": start.group(1)} if start.group(1) else {})})
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


def route_transitions(events, limit=12):
    """Collapse consecutive identical decisions into state changes with their duration."""
    selected = [event for event in events if event.get("event") == "route_selected"]
    entries = []
    for event in selected:
        provider = "external" if event.get("provider") in ("openrouter", "local") else "plan"
        key = (provider, event.get("reason"), event.get("detail"))
        if entries and entries[-1]["key"] == key:
            entries[-1]["count"] += 1
            entries[-1]["last_ts"] = event["ts"]
            if event.get("model"):
                entries[-1]["model"] = event["model"]
            continue
        entries.append({"key": key, "state": provider, "reason": event.get("reason"),
                        "detail": event.get("detail"), "model": event.get("model"),
                        "first_ts": event["ts"], "last_ts": event["ts"], "count": 1})
    for index, entry in enumerate(entries):
        following = entries[index + 1]["first_ts"] if index + 1 < len(entries) else None
        entry["seconds"] = _seconds_between(entry["first_ts"], following)
        entry["ongoing"] = following is None
        entry.pop("key", None)
    return entries[-limit:]


def _seconds_between(first, second):
    try:
        start = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(second).replace("Z", "+00:00")) if second else None
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


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
        "proxy_code": starts[-1].get("code") if starts else None,
        "transitions": route_transitions(events),
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


def code_modified_at(path=None):
    """When the proxy source last changed; a running proxy older than this uses stale filters."""
    try:
        return datetime.fromtimestamp(Path(path or PROXY_FILE).stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def proxy_stale(routing, path=None):
    """True when the running proxy loaded an older revision than the current source."""
    modified = code_modified_at(path)
    if modified is None:
        return None
    started_code = routing.get("proxy_code")
    if started_code:
        age = _seconds_between(started_code, modified.isoformat())
        return age is not None and abs(age) > 1
    started = routing.get("proxy_started_at")
    if not started:
        return None
    age = _seconds_between(started, modified.isoformat())
    return age is not None and age > 0


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
    if proxy_stale(routing):
        alerts.append({"level": "warn", "title": "실행기 재시작 필요",
                       "detail": "실행 중인 프록시가 현재 코드보다 먼저 시작되었습니다. 수정된 라우팅 필터가 적용되지 않아 Plan으로 우회할 수 있으니 세션을 재시작하세요."})
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


def _config_path(provider):
    return Path(CONFIG_FILES[provider]["path"])


def _mask(value):
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "•" * len(value)


def config_snapshot():
    """Current settings with secrets masked; the browser never receives a stored key."""
    result = {}
    for provider, spec in CONFIG_FILES.items():
        path = _config_path(provider)
        values = read_env(path)
        fields = {}
        for name in spec["fields"]:
            raw = values.get(name, "")
            if name in spec["secret"]:
                fields[name] = {"secret": True, "set": bool(raw), "preview": _mask(raw), "value": ""}
            else:
                fields[name] = {"secret": False, "set": bool(raw), "value": raw}
        result[provider] = {"path": str(path), "exists": path.is_file(), "fields": fields}
    return result


def _validate_local(values):
    from urllib.parse import urlsplit

    url = (values.get("LOCAL_LLM_URL") or "").strip()
    remote = (values.get("LOCAL_LLM_ALLOW_REMOTE") or "false").strip().lower()
    http_url = (values.get("LOCAL_LLM_HTTP_URL") or "").strip()
    if remote not in ("true", "false"):
        raise ValueError("invalid_allow_remote")
    if not url or any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise ValueError("invalid_local_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_local_url") from None
    loopback = parsed.hostname in ("127.0.0.1", "::1")
    internal = parsed.scheme == "http" and http_url and url == http_url
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != "/v1/messages"
            or (not loopback and not internal and (remote != "true" or parsed.scheme != "https"))):
        raise ValueError("invalid_local_url")
    model = (values.get("LOCAL_LLM_MODEL") or "").strip()
    if not model or any(ord(char) < 32 for char in model):
        raise ValueError("invalid_local_model")
    key = values.get("LOCAL_LLM_API_KEY") or ""
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError("invalid_local_key")
    models_url = (values.get("LOCAL_LLM_MODELS_URL") or "").strip()
    if models_url:
        if any(ord(char) < 33 or ord(char) == 127 for char in models_url):
            raise ValueError("invalid_models_url")
        try:
            other = urlsplit(models_url)
            other_port = other.port
        except ValueError:
            raise ValueError("invalid_models_url") from None
        origin = (parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80))
        other_origin = (other.scheme, other.hostname, other_port if other_port is not None else (443 if other.scheme == "https" else 80))
        if (other_origin != origin or other.username is not None or other.password is not None
                or other.query or other.fragment or not other.path.startswith("/")):
            raise ValueError("invalid_models_url")


def _validate_config(provider, values):
    if provider == "openrouter":
        model = (values.get("OPENROUTER_MODEL") or "").strip()
        if (not re.fullmatch(r"[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}", model)
                or model.startswith(("REPLACE_WITH_", "YOUR_"))):
            raise ValueError("invalid_model")
        token = values.get("ANTHROPIC_AUTH_TOKEN")
        if token and not re.fullmatch(r"sk-or-[A-Za-z0-9_-]+", token):
            raise ValueError("invalid_key")
        return
    _validate_local(values)


def _effective_values(provider, values=None):
    """Posted values win over stored ones; an empty secret keeps the stored value."""
    stored = read_env(_config_path(provider))
    spec = CONFIG_FILES[provider]
    if not isinstance(values, dict):
        return stored
    merged = dict(stored)
    for name in spec["fields"]:
        if name not in values or not isinstance(values[name], str):
            continue
        value = values[name].strip()
        if name in spec["secret"] and not value:
            continue
        merged[name] = value
    return merged


def _example_text(path):
    example = Path(path).with_name(Path(path).name + ".example")
    try:
        return example.read_text(encoding="utf-8") if example.is_file() else None
    except OSError:
        return None


def _write_env(path, updates, base=None):
    """Update managed keys in place, keeping comments and unrelated entries; atomic replace."""
    path = Path(path)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        original = base or ""
    lines = original.splitlines()
    written = set()
    output = []
    for line in lines:
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if match and match.group(1) in updates:
            name = match.group(1)
            if name in written:
                continue
            written.add(name)
            value = updates[name]
            output.append(line if value is None else f"{name}={value}")
            continue
        output.append(line)
    for name, value in updates.items():
        if name in written or value is None:
            continue
        output.append(f"{name}={value}")
        written.add(name)
    text = "\n".join(output).rstrip("\n") + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    if path.is_file():
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
    os.replace(temporary, path)


def save_config(provider, values):
    """Validate and persist one provider's settings; an empty secret keeps the stored value."""
    spec = CONFIG_FILES.get(provider)
    if spec is None:
        raise ValueError("unknown_provider")
    clean = {}
    for name in spec["fields"]:
        if name not in values:
            continue
        value = values[name]
        if not isinstance(value, str):
            raise ValueError("invalid_value")
        clean[name] = value.strip()
    for name in spec["required"]:
        if name in spec["secret"]:
            if not clean.get(name) and not read_env(_config_path(provider)).get(name):
                raise ValueError(f"missing_{name.lower()}")
        elif not clean.get(name):
            raise ValueError(f"missing_{name.lower()}")
    _validate_config(provider, clean)
    updates = {}
    for name in spec["fields"]:
        if name in spec["secret"]:
            updates[name] = clean.get(name) or None
        elif name in clean:
            updates[name] = clean[name]
    path = _config_path(provider)
    # On first save, start from the shipped example so its comments stay in the new file.
    _write_env(path, updates, base=None if path.is_file() else _example_text(path))
    return config_snapshot()


def _http_json(url, headers, body=None, timeout=12):
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
    with opener.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read(4 * 1024 * 1024 + 1))


def run_connection_test(provider, mode="metadata", values=None):
    """One safe connectivity check; unsaved form values may be supplied, nothing secret is returned."""
    import urllib.error
    from urllib.parse import urlsplit

    base = {"provider": provider, "mode": mode, "ok": False}
    values = _effective_values(provider, values)
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    try:
        if provider == "openrouter":
            key = values.get("ANTHROPIC_AUTH_TOKEN", "")
            model = values.get("OPENROUTER_MODEL", "")
            if not key or not model:
                return {**base, "reason": "configuration_missing"}
            if (not re.fullmatch(r"sk-or-[A-Za-z0-9_-]+", key)
                    or not re.fullmatch(r"[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}", model)):
                return {**base, "reason": "invalid_configuration"}
            headers["authorization"] = "Bearer " + key
            if mode == "metadata":
                status, data = _http_json("https://openrouter.ai/api/v1/models",
                                          {"authorization": "Bearer " + key})
                rows = data.get("data") if isinstance(data, dict) else None
                ids = [row.get("id") for row in rows or [] if isinstance(row, dict)]
                found = model in ids
                return {**base, "ok": found, "reason": "model_found" if found else "model_not_found",
                        "model": model, "model_count": len(ids)}
            status, data = _http_json("https://openrouter.ai/api/v1/messages", headers,
                                      {"model": model, "max_tokens": 16, "stream": False,
                                       "messages": [{"role": "user", "content": "Reply with exactly OK"}]})
            content = data.get("content") if isinstance(data, dict) else None
            text = "".join(block.get("text", "") for block in content or []
                           if isinstance(block, dict) and block.get("type") == "text").strip()
            if data.get("model") != model:
                return {**base, "reason": "wrong_model", "model": model, "status": status}
            if not text:
                return {**base, "reason": "empty_reply", "model": model, "status": status}
            return {**base, "ok": True, "reason": "ready", "model": model, "status": status}

        url = values.get("LOCAL_LLM_URL", "")
        model = values.get("LOCAL_LLM_MODEL", "")
        if not url or not model:
            return {**base, "reason": "configuration_missing"}
        try:
            _validate_local(values)
        except ValueError:
            return {**base, "reason": "invalid_configuration"}
        key = values.get("LOCAL_LLM_API_KEY", "")
        if key:
            headers["authorization"] = "Bearer " + key
        if mode == "metadata":
            models_url = values.get("LOCAL_LLM_MODELS_URL", "")
            if not models_url:
                parsed = urlsplit(url)
                models_url = f"{parsed.scheme}://{parsed.netloc}/v1/models"
            status, data = _http_json(models_url, headers)
            entries = data.get("data", data.get("models")) if isinstance(data, dict) else None
            ids = [entry.get("id", entry.get("name", entry.get("model"))) for entry in entries or []
                   if isinstance(entry, dict)]
            found = model in ids
            return {**base, "ok": found, "reason": "model_found" if found else "model_not_found",
                    "model": model, "model_count": len(ids)}
        status, data = _http_json(url, headers,
                                  {"model": model, "max_tokens": 16, "stream": False,
                                   "messages": [{"role": "user", "content": "Reply with exactly OK"}]})
        content = data.get("content") if isinstance(data, dict) else None
        text = "".join(block.get("text", "") for block in content or []
                       if isinstance(block, dict) and block.get("type") == "text").strip()
        if not text:
            return {**base, "reason": "empty_reply", "model": model, "status": status}
        return {**base, "ok": True, "reason": "ready", "model": model, "status": status}
    except urllib.error.HTTPError as error:
        error.close()
        return {**base, "reason": "http_error", "status": error.code}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {**base, "reason": "connection_failed"}
    except ValueError:
        return {**base, "reason": "invalid_response"}


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
        "proxy_stale": proxy_stale(routing),
        "help": {"reasons": REASON_HELP, **HELP_SECTION},
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
        "proxy_stale": proxy_stale(routing),
        "help": {"reasons": REASON_HELP, **HELP_SECTION},
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
<html lang="ko"><head><meta charset="utf-8"><title>OpenRouter 라우팅 콘솔</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
 color-scheme:dark;
 --bg:#070b12; --bg-soft:#0b1119; --panel:#0f1622; --panel-2:#121a28; --panel-3:#162031;
 --line:#1d2739; --line-2:#27344b; --line-3:#33465f;
 --fg:#e9eff8; --fg-2:#b6c2d4; --fg-3:#8391a7; --fg-4:#5f6d83;
 --accent:#5b9dff; --accent-soft:rgba(91,157,255,.16); --accent-line:rgba(91,157,255,.45);
 --ok:#43d9a3; --ok-soft:rgba(67,217,163,.14); --ok-line:rgba(67,217,163,.4);
 --warn:#f2b95e; --warn-soft:rgba(242,185,94,.14); --warn-line:rgba(242,185,94,.4);
 --bad:#ff7089; --bad-soft:rgba(255,112,137,.14); --bad-line:rgba(255,112,137,.4);
 --radius:16px; --radius-sm:11px; --radius-xs:8px;
 --shadow:0 18px 40px -24px rgba(0,0,0,.9), 0 2px 8px rgba(0,0,0,.35);
 --mono:ui-monospace,"Cascadia Mono","JetBrains Mono",Consolas,monospace;
 --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Malgun Gothic",sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:
 radial-gradient(1200px 600px at 15% -10%, rgba(91,157,255,.10), transparent 60%),
 radial-gradient(900px 500px at 100% 0%, rgba(67,217,163,.06), transparent 55%),
 var(--bg);
 color:var(--fg);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3,p{margin:0}
.wrap{max-width:1460px;margin:0 auto;padding:0 28px}
.mono{font-family:var(--mono);font-size:12px;letter-spacing:-.1px}
.muted{color:var(--fg-3)}

/* top bar */
.topbar{position:sticky;top:0;z-index:40;background:rgba(7,11,18,.82);
 backdrop-filter:blur(14px) saturate(140%);border-bottom:1px solid var(--line)}
.topbar-in{display:flex;align-items:center;justify-content:space-between;gap:20px;height:66px}
.brand{display:flex;align-items:center;gap:13px;min-width:0}
.logo{width:34px;height:34px;border-radius:11px;flex:0 0 auto;
 background:linear-gradient(145deg,#5b9dff,#2f6bd6);box-shadow:0 6px 18px -6px rgba(91,157,255,.8);
 position:relative}
.logo::after{content:"";position:absolute;inset:9px;border-radius:50%;
 border:2px solid rgba(255,255,255,.9);border-right-color:transparent;border-bottom-color:transparent;
 transform:rotate(35deg)}
.brand h1{font-size:15px;font-weight:650;letter-spacing:-.2px}
.brand p{font-size:11.5px;color:var(--fg-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top-actions{display:flex;align-items:center;gap:10px;flex:0 0 auto}

/* pills + chips */
.pill{display:inline-flex;align-items:center;gap:8px;height:30px;padding:0 12px;border-radius:999px;
 font-size:12px;font-weight:600;border:1px solid var(--line-2);background:var(--panel-2);color:var(--fg-2);
 white-space:nowrap}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 0 3px rgba(255,255,255,.04)}
.pill-ok{color:var(--ok);border-color:var(--ok-line);background:var(--ok-soft)}
.pill-warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}
.pill-bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}
.pill-mute{color:var(--fg-3)}
.chip{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:7px;
 font-size:11px;font-family:var(--mono);color:var(--fg-2);background:var(--panel-3);border:1px solid var(--line)}

/* layout */
main{padding:26px 0 80px}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:18px}
.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-5{grid-column:span 5}
.col-6{grid-column:span 6}.col-7{grid-column:span 7}.col-8{grid-column:span 8}
.col-12{grid-column:span 12}
@media(max-width:1180px){.col-3{grid-column:span 6}.col-4,.col-5,.col-7,.col-8{grid-column:span 12}
 .col-6{grid-column:span 12}}
@media(max-width:720px){.col-3{grid-column:span 12}.wrap{padding:0 16px}}

/* cards */
.card{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);
 border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.card-h{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
 padding:16px 18px 13px;border-bottom:1px solid var(--line)}
.card-h h2{font-size:13px;font-weight:650;letter-spacing:-.1px}
.card-h .sub{font-size:11.5px;color:var(--fg-4);font-family:var(--mono);margin-top:2px}
.card-b{padding:16px 18px 18px;flex:1;display:flex;flex-direction:column}
.card-b.tight{padding:12px 18px 16px}

/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:34px;padding:0 14px;
 border-radius:var(--radius-xs);border:1px solid var(--line-2);background:var(--panel-3);color:var(--fg);
 font:600 12.5px/1 var(--sans);cursor:pointer;transition:.16s ease;white-space:nowrap}
.btn:hover:not(:disabled){border-color:var(--line-3);background:#1a2436;transform:translateY(-1px)}
.btn:active:not(:disabled){transform:none}
.btn:focus-visible{outline:2px solid var(--accent-line);outline-offset:2px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:linear-gradient(180deg,#5b9dff,#3b78e8);border-color:#6ba8ff;color:#fff;
 box-shadow:0 8px 18px -10px rgba(91,157,255,.9)}
.btn-primary:hover:not(:disabled){background:linear-gradient(180deg,#6ba8ff,#4482f0)}
.btn-ghost{background:transparent}
.btn-sm{height:28px;padding:0 10px;font-size:11.5px}
.btn.loading::before{content:"";width:11px;height:11px;border-radius:50%;
 border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* metrics */
.metric{display:flex;flex-direction:column;gap:2px}
.metric .k{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--fg-4);font-weight:600}
.metric .v{font-size:23px;font-weight:680;letter-spacing:-.6px;font-variant-numeric:tabular-nums}
.metric .v small{font-size:12px;font-weight:600;color:var(--fg-3);letter-spacing:0}
.metric-list{display:flex;flex-direction:column;gap:7px;margin-top:4px}
.row-kv{display:flex;align-items:baseline;justify-content:space-between;gap:12px;font-size:12.5px}
.row-kv .k{color:var(--fg-3)}
.row-kv .v{color:var(--fg);font-variant-numeric:tabular-nums;text-align:right}
.bar{height:7px;border-radius:999px;background:#0a1119;border:1px solid var(--line);overflow:hidden;margin:12px 0 10px}
.bar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#3b78e8,#43d9a3)}
.hairline{height:1px;background:var(--line);margin:12px 0}

/* tables */
table{width:100%;border-collapse:collapse;font-size:12.5px}
thead th{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg-4);font-weight:650;
 text-align:left;padding:0 10px 8px 0;border-bottom:1px solid var(--line)}
tbody td{padding:9px 10px 9px 0;border-bottom:1px solid rgba(255,255,255,.035);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:rgba(91,157,255,.04)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.id{font-family:var(--mono);font-size:11.5px;color:var(--fg-2)}

/* alerts */
.alerts{display:flex;flex-direction:column;gap:10px;margin-bottom:18px}
.alert{display:flex;gap:12px;align-items:flex-start;padding:12px 15px;border-radius:var(--radius-sm);
 border:1px solid var(--line-2);background:var(--panel-2);font-size:12.5px}
.alert .icon{width:18px;height:18px;border-radius:50%;flex:0 0 auto;margin-top:1px;
 display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
.alert b{display:block;font-size:12.5px;margin-bottom:1px}
.alert .txt b{display:inline;font-size:inherit;margin:0;color:var(--fg);font-weight:650}
.alert .txt{color:var(--fg-2)}
.alert.critical{border-color:var(--bad-line);background:var(--bad-soft)}
.alert.critical .icon{background:var(--bad);color:#3a0a13}
.alert.warn{border-color:var(--warn-line);background:var(--warn-soft)}
.alert.warn .icon{background:var(--warn);color:#3a2606}
.alert.info{border-color:var(--accent-line);background:var(--accent-soft)}
.alert.info .icon{background:var(--accent);color:#06152e}
.alert code{font-family:var(--mono);font-size:11.5px;background:#080d15;border:1px solid var(--line);
 padding:1px 5px;border-radius:5px;color:var(--fg-2)}
.notice{border:1px solid var(--accent-line);background:var(--accent-soft);border-radius:var(--radius-sm);
 padding:11px 13px;margin-bottom:14px;font-size:12.5px;color:var(--fg-2)}
.notice b{color:var(--fg)}

/* settings form */
.setting-form{display:flex;flex-direction:column}
.field{padding:11px 0;border-bottom:1px dashed rgba(255,255,255,.05)}
.field:last-of-type{border-bottom:0}
.field label{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--fg-2);margin-bottom:6px}
.req{font-size:10px;font-weight:700;letter-spacing:.04em;color:var(--fg-4);text-transform:uppercase}
.input{width:100%;height:38px;padding:0 12px;border-radius:var(--radius-xs);border:1px solid var(--line-2);
 background:#0a111c;color:var(--fg);font:12.5px var(--mono);transition:.15s ease}
.input::placeholder{color:var(--fg-4);font-family:var(--sans)}
.input:hover{border-color:var(--line-3)}
.input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.input[readonly]{opacity:.85}
.input-group{display:flex;gap:8px}
.input-group .input{flex:1}
.hint{font-size:11.5px;color:var(--fg-4);margin-top:6px;line-height:1.5}
.hint b{color:var(--fg-3)}
.field.invalid .input{border-color:var(--bad-line);box-shadow:0 0 0 3px var(--bad-soft)}
.switch{display:inline-flex;align-items:center;gap:10px;cursor:pointer;user-select:none;font-size:12.5px;
 color:var(--fg-2);font-weight:600}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .track{width:38px;height:22px;border-radius:999px;background:#0a111c;border:1px solid var(--line-2);
 position:relative;transition:.18s ease;flex:0 0 auto}
.switch .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;
 background:var(--fg-3);transition:.18s ease}
.switch input:checked+.track{background:var(--accent-soft);border-color:var(--accent-line)}
.switch input:checked+.track::after{left:18px;background:var(--accent)}
.switch input:focus-visible+.track{box-shadow:0 0 0 3px var(--accent-soft)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.save-state{font-size:11px;font-weight:600;color:var(--fg-4)}
.save-state.dirty{color:var(--warn)}
.result{margin-top:14px;border-top:1px solid var(--line);padding-top:12px;min-height:34px}
.result-line{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:600}
.result-line .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.result-line.ok{color:var(--ok)} .result-line.ok .dot{background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}
.result-line.bad{color:var(--bad)} .result-line.bad .dot{background:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}
.result-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.empty{color:var(--fg-4);font-size:12px}
.stored{display:flex;flex-direction:column;gap:6px;margin-top:auto;padding-top:12px}
.stored-title{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--fg-4);font-weight:650;
 margin-bottom:2px}
.stored .row-kv{font-size:12px}

/* timeline */
.timeline{display:flex;align-items:flex-end;gap:4px;height:132px;padding-top:6px}
.tl-col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;min-width:0;position:relative;height:100%}
.tl-ext{background:linear-gradient(180deg,#43d9a3,#2fb383);border-radius:3px 3px 0 0}
.tl-plan{background:linear-gradient(180deg,#f2b95e,#c98f36);border-radius:3px 3px 0 0}
.tl-col:hover .tl-ext,.tl-col:hover .tl-plan{filter:brightness(1.15)}
.tl-labels{display:flex;gap:4px;margin-top:7px}
.tl-labels span{flex:1;font-size:9.5px;color:var(--fg-4);text-align:center;font-family:var(--mono)}
.legend{display:flex;gap:16px;font-size:11.5px;color:var(--fg-3);margin-top:10px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}

/* help */
details{border-top:1px solid var(--line);padding:10px 0}
details:first-of-type{border-top:0}
summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--fg-2);list-style:none;
 display:flex;align-items:center;justify-content:space-between}
summary::-webkit-details-marker{display:none}
summary::after{content:"+";color:var(--fg-4);font-weight:700}
details[open] summary::after{content:"–"}
details .card-b{padding-top:10px}
.code{background:#080d15;border:1px solid var(--line);border-radius:var(--radius-sm);padding:12px 14px;
 font:12px/1.85 var(--mono);color:var(--fg-2);white-space:pre;overflow-x:auto;margin:10px 0 4px}

/* toasts */
.toasts{position:fixed;right:22px;bottom:22px;z-index:80;display:flex;flex-direction:column;gap:9px}
.toast{display:flex;align-items:center;gap:10px;min-width:230px;max-width:380px;padding:12px 14px;
 border-radius:var(--radius-sm);border:1px solid var(--line-2);background:#0d1522;color:var(--fg);
 font-size:12.5px;box-shadow:var(--shadow);animation:in .18s ease}
.toast .dot{width:8px;height:8px;border-radius:50%}
.toast.ok{border-color:var(--ok-line)} .toast.ok .dot{background:var(--ok)}
.toast.warn{border-color:var(--warn-line)} .toast.warn .dot{background:var(--warn)}
.toast.bad{border-color:var(--bad-line)} .toast.bad .dot{background:var(--bad)}
.toast.out{opacity:0;transform:translateY(6px);transition:.25s ease}
@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

.section-title{display:flex;align-items:center;gap:10px;margin:30px 0 14px}
.section-title h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--fg-4);font-weight:650}
.section-title .line{flex:1;height:1px;background:var(--line)}
</style></head><body>
<header class="topbar"><div class="wrap topbar-in">
  <div class="brand">
    <span class="logo"></span>
    <div style="min-width:0">
      <h1>OpenRouter 라우팅 콘솔</h1>
      <p id="stamp">불러오는 중…</p>
    </div>
  </div>
  <div class="top-actions">
    <span class="pill pill-mute" id="live-pill"><span class="dot"></span><span id="live-text">확인 중</span></span>
    <button class="btn btn-sm" onclick="tick(true)">새로고침</button>
    <a class="btn btn-sm btn-ghost" href="#help">도움말</a>
  </div>
</div></header>

<main class="wrap">
  <div class="alerts" id="alerts"></div>

  <div class="section-title"><h2>설정</h2><span class="line"></span></div>
  <div class="grid" id="settings"></div>

  <div class="section-title"><h2>상태</h2><span class="line"></span></div>
  <div class="grid" id="grid"></div>

  <div class="section-title"><h2>도움말</h2><span class="line"></span></div>
  <div class="grid" id="help"></div>
</main>
<div class="toasts" id="toasts"></div>

<script>
const TOKEN = "__DASHBOARD_TOKEN__";
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => value === null || value === undefined || value === '' ? '—' : Number(value).toLocaleString('ko-KR');
const usd = (value, digits = 3) => value === null || value === undefined ? '—' : '$' + Number(value).toFixed(digits);
const tokens = row => row ? row.reduce((sum, value) => sum + value, 0) : 0;
const stateText = {external:'OpenRouter 사용 중', plan:'Plan 우회 중', idle:'유휴', unknown:'기록 없음'};
const statePill = {external:'pill-ok', plan:'pill-warn', idle:'pill-mute', unknown:'pill-mute'};
const PROVIDERS = {
  openrouter: {title:'OpenRouter', file:'.env.openrouter', fields:[
    ['OPENROUTER_MODEL','모델 ID','text','vendor/model','OpenRouter 모델 목록의 정확한 ID','필수'],
    ['ANTHROPIC_AUTH_TOKEN','API 키','password','sk-or-...','비워 두면 저장된 키를 유지합니다','필수']]},
  local: {title:'Local LLM', file:'.env.local-llm', fields:[
    ['LOCAL_LLM_URL','Messages URL','text','http://127.0.0.1:11434/v1/messages','Anthropic 호환 /v1/messages 주소','필수'],
    ['LOCAL_LLM_MODEL','모델 ID','text','설치된 모델 ID','서버에 실제로 있는 모델 이름','필수'],
    ['LOCAL_LLM_API_KEY','서버 키','password','선택 사항','비워 두면 저장된 키를 유지합니다','선택'],
    ['LOCAL_LLM_ALLOW_REMOTE','원격 HTTPS 허용','switch','','루프백이 아닌 HTTPS 서버를 허용합니다','선택'],
    ['LOCAL_LLM_HTTP_URL','내부 HTTP 예외','text','http://host/v1/messages','사내 서버용 예외 주소 (선택)','선택'],
    ['LOCAL_LLM_MODELS_URL','모델 목록 URL','text','기본: /v1/models','모델 확인에 사용할 같은 출처 주소','선택']]},
};
const ERROR_TEXT = {
  unknown_provider:'알 수 없는 제공자', invalid_values:'입력 형식 오류', invalid_value:'값 형식 오류',
  invalid_model:'모델 ID 형식이 올바르지 않습니다', invalid_key:'API 키 형식이 올바르지 않습니다 (sk-or-...)',
  missing_anthropic_auth_token:'API 키가 필요합니다', missing_openrouter_model:'모델 ID가 필요합니다',
  missing_local_llm_url:'Messages URL이 필요합니다', missing_local_llm_model:'모델 ID가 필요합니다',
  invalid_local_url:'Local URL 규칙 위반 (http(s), /v1/messages, 루프백 또는 허용된 HTTPS)',
  invalid_local_model:'모델 ID가 비어 있거나 잘못되었습니다', invalid_local_key:'서버 키에 제어 문자가 있습니다',
  invalid_allow_remote:'원격 허용 값은 true/false여야 합니다', invalid_models_url:'모델 목록 URL이 올바르지 않습니다',
  invalid_configuration:'설정이 올바르지 않습니다', write_failed:'파일을 쓰지 못했습니다',
  token_required:'세션 토큰이 없습니다 (페이지 새로고침 후 다시 시도)', origin_rejected:'다른 출처의 요청입니다',
  request_too_large:'요청이 너무 큽니다', invalid_json:'JSON 형식 오류', invalid_payload:'요청 형식 오류',
  invalid_mode:'알 수 없는 테스트 모드', invalid_length:'요청 길이 오류',
};
const TEST_TEXT = {
  ready:'연결과 추론이 정상입니다', model_found:'모델을 찾았습니다', model_not_found:'설정한 모델이 목록에 없습니다',
  wrong_model:'다른 모델이 응답했습니다', empty_reply:'응답 본문이 비어 있습니다', http_error:'HTTP 오류',
  connection_failed:'연결 실패', invalid_response:'응답 형식 오류', configuration_missing:'설정이 먼저 필요합니다',
  invalid_configuration:'저장된 설정이 올바르지 않습니다',
};

let CONFIG = null, SNAP = null;
const DIRTY = new Set(), BUSY = new Set(), RESULTS = {};

function toast(kind, text) {
  const host = document.getElementById('toasts');
  const node = document.createElement('div');
  node.className = 'toast ' + kind;
  node.innerHTML = `<span class="dot"></span><span>${esc(text)}</span>`;
  host.appendChild(node);
  setTimeout(() => { node.classList.add('out'); setTimeout(() => node.remove(), 260); }, 3400);
}

function card(title, subtitle, body, span) {
  return `<section class="card ${span || 'col-12'}">
    <header class="card-h"><div><h2>${esc(title)}</h2>${subtitle ? `<p class="sub">${esc(subtitle)}</p>` : ''}</div></header>
    <div class="card-b">${body}</div></section>`;
}

function kv(label, value, mono) {
  return `<div class="row-kv"><span class="k">${esc(label)}</span><span class="v${mono ? ' mono' : ''}">${value}</span></div>`;
}

/* ---------- settings ---------- */
function fieldValue(provider, name) {
  const fields = (CONFIG && CONFIG[provider] && CONFIG[provider].fields) || {};
  return fields[name] || {value:'', set:false, preview:'', secret:false};
}

function renderSettings(force) {
  if (!CONFIG) return;
  const host = document.getElementById('settings');
  if (!force && host.contains(document.activeElement)) return;
  host.innerHTML = Object.entries(PROVIDERS).map(([provider, spec]) => {
    const rows = spec.fields.map(([name, label, type, placeholder, hint, badge]) => {
      const field = fieldValue(provider, name);
      const id = `cfg-${provider}-${name}`;
      const dirty = DIRTY.has(`${provider}:${name}`);
      if (type === 'switch') {
        return `<div class="field"><label>${esc(label)}<span class="req">${esc(badge)}</span></label>
          <label class="switch"><input type="checkbox" id="${id}" data-provider="${provider}" data-name="${name}"
            ${field.value === 'true' ? 'checked' : ''}><span class="track"></span>
            <span>${field.value === 'true' ? '허용' : '허용 안 함'}</span></label>
          <p class="hint">${esc(hint)}</p></div>`;
      }
      const value = field.secret ? '' : esc(field.value || '');
      const effectivePlaceholder = field.secret && field.set
        ? `저장됨 (${field.preview}) — 변경할 때만 입력` : placeholder;
      const reveal = field.secret
        ? `<button class="btn btn-sm btn-ghost" type="button" data-reveal="${id}">보기</button>` : '';
      return `<div class="field${dirty ? ' invalid' : ''}" data-field="${provider}:${name}">
        <label for="${id}">${esc(label)}<span class="req">${esc(badge)}</span></label>
        <div class="input-group"><input class="input" id="${id}" type="${type}" value="${value}"
          placeholder="${esc(effectivePlaceholder)}" data-provider="${provider}" data-name="${name}"
          data-secret="${field.secret ? '1' : '0'}" autocomplete="off" spellcheck="false">${reveal}</div>
        <p class="hint">${esc(hint)}</p></div>`;
    }).join('');
    const dirtyCount = spec.fields.filter(([name]) => DIRTY.has(`${provider}:${name}`)).length;
    const stored = spec.fields.map(([name, label]) => {
      const field = fieldValue(provider, name);
      if (field.secret) {
        return kv(label, field.set ? `<span class="mono">${esc(field.preview)}</span>`
                                   : '<span class="empty">설정 안 됨</span>');
      }
      return kv(label, field.value ? esc(field.value) : '<span class="empty">설정 안 됨</span>');
    }).join('');
    return `<section class="card col-6" id="card-${provider}">
      <header class="card-h">
        <div><h2>${esc(spec.title)}</h2><p class="sub">${esc(spec.file)}</p></div>
        <span class="save-state ${dirtyCount ? 'dirty' : ''}" id="state-${provider}">
          ${dirtyCount ? `저장되지 않은 변경 ${dirtyCount}` : '저장됨'}</span>
      </header>
      <div class="card-b">
        ${providerReady(provider) ? '' : `<div class="notice"><b>아직 설정이 없습니다.</b>
          값을 입력하고 <b>모델 확인</b>으로 검증한 뒤 <b>저장</b>하세요. 모델 확인은 저장 전 값으로도 실행됩니다.</div>`}
        <div class="setting-form">${rows}</div>
        <div class="actions">
          <button class="btn btn-primary" data-busy-key="${provider}" onclick="saveSettings('${provider}')">저장</button>
          <button class="btn" data-busy-key="${provider}:metadata" onclick="runTest('${provider}','metadata')">모델 확인</button>
          <button class="btn" data-busy-key="${provider}:inference" onclick="runTest('${provider}','inference')">추론 테스트</button>
        </div>
        <div class="result" id="res-${provider}"></div>
        <div class="hairline"></div>
        <div class="stored"><span class="stored-title">현재 저장된 값</span>${stored}</div>
      </div></section>`;
  }).join('');
  wireSettings();
  Object.keys(PROVIDERS).forEach(provider => renderResult(provider));
}

function wireSettings() {
  document.querySelectorAll('#settings .input').forEach(input => {
    input.addEventListener('input', () => {
      const key = `${input.dataset.provider}:${input.dataset.name}`;
      const original = fieldValue(input.dataset.provider, input.dataset.name);
      const current = input.dataset.secret === '1' ? input.value : input.value;
      const changed = input.dataset.secret === '1' ? current !== '' : current !== (original.value || '');
      if (changed) DIRTY.add(key); else DIRTY.delete(key);
      const field = input.closest('.field');
      if (field) field.classList.toggle('invalid', false);
      updateSaveState(input.dataset.provider);
    });
  });
  document.querySelectorAll('#settings .switch input').forEach(input => {
    input.addEventListener('change', () => {
      DIRTY.add(`${input.dataset.provider}:${input.dataset.name}`);
      const label = input.parentElement.querySelector('span:last-child');
      if (label) label.textContent = input.checked ? '허용' : '허용 안 함';
      updateSaveState(input.dataset.provider);
    });
  });
  document.querySelectorAll('#settings [data-reveal]').forEach(button => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.reveal);
      if (!input) return;
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.textContent = reveal ? '숨기기' : '보기';
    });
  });
}

function updateSaveState(provider) {
  const spec = PROVIDERS[provider];
  const count = spec.fields.filter(([name]) => DIRTY.has(`${provider}:${name}`)).length;
  const node = document.getElementById(`state-${provider}`);
  if (!node) return;
  node.textContent = count ? `저장되지 않은 변경 ${count}` : '저장됨';
  node.classList.toggle('dirty', count > 0);
}

function setBusy(key, flag) {
  if (flag) BUSY.add(key); else BUSY.delete(key);
  document.querySelectorAll(`[data-busy-key="${key}"]`).forEach(button => {
    button.disabled = flag;
    button.classList.toggle('loading', flag);
  });
}

function collect(provider) {
  const values = {};
  for (const [name, , type] of PROVIDERS[provider].fields) {
    const element = document.getElementById(`cfg-${provider}-${name}`);
    if (!element) continue;
    values[name] = type === 'switch' ? (element.checked ? 'true' : 'false') : element.value;
  }
  return values;
}

async function post(path, payload) {
  const response = await fetch(path, {method:'POST',
    headers:{'content-type':'application/json', 'x-dashboard-token':TOKEN},
    body: JSON.stringify(payload)});
  return response.json();
}

async function saveSettings(provider) {
  setBusy(provider, true);
  let result;
  try { result = await post('/api/config', {provider, values: collect(provider)}); }
  catch (error) { result = {saved:false, error:'connection_failed'}; }
  setBusy(provider, false);
  if (result.saved) {
    PROVIDERS[provider].fields.forEach(([name]) => DIRTY.delete(`${provider}:${name}`));
    CONFIG = result.config;
    RESULTS[provider] = {ok:true, reason:'saved'};
    renderSettings(true);
    renderResult(provider, '저장했습니다. 새 세션부터 적용됩니다.');
    toast('ok', `${PROVIDERS[provider].title} 설정을 저장했습니다`);
  } else {
    RESULTS[provider] = {error: result.error};
    renderSettings(true);
    renderResult(provider);
    toast('bad', ERROR_TEXT[result.error] || '저장에 실패했습니다');
  }
}

async function runTest(provider, mode) {
  const key = `${provider}:${mode}`;
  setBusy(key, true);
  renderResult(provider, mode === 'inference' ? '추론을 확인하는 중…' : '모델 목록을 확인하는 중…');
  let result;
  try { result = await post('/api/test', {provider, mode, values: collect(provider)}); }
  catch (error) { result = {ok:false, reason:'connection_failed', mode}; }
  setBusy(key, false);
  RESULTS[provider] = {...result, mode};
  renderResult(provider);
  toast(result.ok ? 'ok' : 'warn',
    `${PROVIDERS[provider].title} ${mode === 'inference' ? '추론 테스트' : '모델 확인'} ${result.ok ? '성공' : '실패'}`);
}

function renderResult(provider, pending) {
  const box = document.getElementById(`res-${provider}`);
  if (!box) return;
  if (pending) { box.innerHTML = `<p class="empty">${esc(pending)}</p>`; return; }
  const result = RESULTS[provider];
  if (!result) { box.innerHTML = '<p class="empty">아직 테스트하지 않았습니다. 변경 사항은 새 세션부터 적용됩니다.</p>'; return; }
  if (result.reason === 'saved') {
    box.innerHTML = '<div class="result-line ok"><span class="dot"></span>저장 완료 · 새 세션부터 적용됩니다</div>';
    return;
  }
  if (result.error) {
    box.innerHTML = `<div class="result-line bad"><span class="dot"></span>${esc(ERROR_TEXT[result.error] || result.error)}</div>`;
    return;
  }
  const label = result.mode === 'inference' ? '추론 테스트' : '모델 확인';
  const text = TEST_TEXT[result.reason] || result.reason || '알 수 없는 결과';
  const chips = [
    result.model ? `<span class="chip">${esc(result.model)}</span>` : '',
    result.status ? `<span class="chip">HTTP ${esc(result.status)}</span>` : '',
    result.model_count !== undefined ? `<span class="chip">모델 ${num(result.model_count)}개</span>` : '',
  ].filter(Boolean).join('');
  box.innerHTML = `<div class="result-line ${result.ok ? 'ok' : 'bad'}"><span class="dot"></span>${esc(label)} · ${esc(text)}</div>
    ${chips ? `<div class="result-meta">${chips}</div>` : ''}`;
}

async function loadConfig() {
  try {
    CONFIG = await (await fetch('/api/config', {cache:'no-store'})).json();
    renderSettings(true);
  } catch (error) { /* keep the previous view */ }
}

/* ---------- dashboard ---------- */
function renderHeader(data) {
  const routing = data.routing || {};
  const state = routing.state || 'unknown';
  const pill = document.getElementById('live-pill');
  pill.className = 'pill ' + (statePill[state] || 'pill-mute');
  document.getElementById('live-text').textContent = stateText[state] || state;
  const age = routing.last && routing.last.age_seconds;
  const ageText = age === null || age === undefined ? '기록 없음'
    : age < 60 ? `${Math.round(age)}초 전` : `${Math.round(age / 60)}분 전`;
  document.getElementById('stamp').textContent =
    `${data.model || '모델 미설정'} · 마지막 결정 ${ageText} · 갱신 ${new Date(data.generated_at).toLocaleTimeString('ko-KR')}`;
}

function providerReady(provider) {
  const fields = (CONFIG && CONFIG[provider] && CONFIG[provider].fields) || {};
  return PROVIDERS[provider].fields
    .filter(([, , , , , badge]) => badge === '필수')
    .every(([name]) => {
      const field = fields[name] || {};
      return field.set || field.value;
    });
}

function renderAlerts(data) {
  const host = document.getElementById('alerts');
  const alerts = data.alerts || [];
  const guide = Object.keys(PROVIDERS).some(providerReady) ? '' :
    `<div class="alert info"><span class="icon">i</span><div><b>처음 설정하기</b>
      <span class="txt">아래 설정에서 모델과 키를 입력하고 <b>모델 확인</b> → <b>저장</b>을 누른 뒤
      <code>startClaudeOpenRouter.ps1</code>로 세션을 시작하세요. 저장한 값은 새 세션부터 적용됩니다.</span></div></div>`;
  host.innerHTML = guide + alerts.map(alert => {
    const icon = alert.level === 'critical' ? '!' : alert.level === 'warn' ? '!' : 'i';
    return `<div class="alert ${esc(alert.level)}"><span class="icon">${icon}</span>
      <div><b>${esc(alert.title)}</b><span class="txt">${esc(alert.detail)}</span></div></div>`;
  }).join('');
}

function renderGrid(data) {
  const routing = data.routing || {}, usage = data.usage || {}, account = data.account || {};
  const last = routing.last || {};
  const counts = routing.counts || {external:0, plan:0};
  const short = routing.short || {minutes:10, counts:{external:0, plan:0}};
  const total = (counts.external || 0) + (counts.plan || 0);
  const share = total ? Math.round((counts.external || 0) / total * 100) : 0;

  const statusCard = card('지금 상태', '마지막 라우팅 결정',
    `<div class="metric"><span class="k">경로</span>
       <span class="v" style="font-size:19px">${esc(stateText[routing.state] || routing.state || '—')}</span></div>
     <div class="metric-list">
       ${kv('사유', esc([last.reason, last.detail].filter(Boolean).join(' / ') || '—'), true)}
       ${kv('모델', `<span class="mono">${esc(last.model || '—')}</span>`)}
       ${kv('프록시 시작', esc(routing.proxy_started_at ? new Date(routing.proxy_started_at).toLocaleString('ko-KR') : '—'))}
       ${data.proxy_stale ? '<div class="row-kv"><span class="k">프록시</span><span class="v" style="color:var(--warn)">코드보다 오래됨 · 재시작 필요</span></div>' : ''}
     </div>`, 'col-3');

  const splitCard = card(`최근 ${routing.window_minutes || 60}분`, '외부 대 Plan 비율',
    `<div class="metric"><span class="k">OpenRouter</span><span class="v">${share}<small>%</small></span></div>
     <div class="bar"><i style="width:${share}%"></i></div>
     <div class="metric-list">
       ${kv('OpenRouter', num(counts.external) + '건')}
       ${kv('Plan', num(counts.plan) + '건')}
       ${kv(`최근 ${short.minutes}분`, `외부 ${num(short.counts.external)} / Plan ${num(short.counts.plan)}`)}
       ${kv('회로 차단', num(routing.circuit_opens) + '회')}
     </div>`, 'col-3');

  const limit = account.limit_usd, spent = account.spent_usd, remaining = account.remaining_usd;
  const used = limit && spent !== null && spent !== undefined ? Math.min(100, Math.round(spent / limit * 100)) : 0;
  const creditCard = card('크레딧', 'OpenRouter 키 한도',
    account.ok ? `<div class="metric"><span class="k">남은 금액</span><span class="v">${usd(remaining)}</span></div>
      <div class="bar"><i style="width:${used}%"></i></div>
      <div class="metric-list">
        ${kv('이번 달 사용', usd(spent))}
        ${kv('한도', usd(limit))}
        ${kv('리셋', esc(account.reset || '—') + (account.resets_at ? ' · ' + new Date(account.resets_at).toLocaleDateString('ko-KR') : ''))}
      </div>`
    : `<div class="metric"><span class="k">상태</span><span class="v" style="font-size:18px;color:var(--fg-3)">확인 불가</span></div>
       <div class="metric-list">${kv('사유', esc(account.error || 'unknown'), true)}</div>`, 'col-3');

  const today = usage.today || {external:[0,0,0,0], plan:[0,0,0,0]};
  const tokensCard = card('오늘 사용량', '토큰과 추정 비용',
    `<div class="metric"><span class="k">OpenRouter 토큰</span><span class="v">${num(tokens(today.external))}</span></div>
     <div class="metric-list">
       ${kv('Plan 토큰', num(tokens(today.plan)))}
       ${kv('입력 / 출력', `${num(today.external[0])} / ${num(today.external[1])}`)}
       ${kv('캐시 읽기 / 생성', `${num(today.external[2])} / ${num(today.external[3])}`)}
       ${kv('추정 비용', `<span style="color:var(--ok)">${usd(usage.cost_usd)}</span>`)}
     </div>`, 'col-3');

  const reasons = (routing.plan_reasons || []).length
    ? routing.plan_reasons.map(([code, count]) => {
        const help = ((data.help || {}).reasons || {})[code];
        return `<div style="padding:9px 0;border-bottom:1px dashed rgba(255,255,255,.05)">
          <div class="row-kv"><span class="mono">${esc(code)}</span><span class="v">${num(count)}</span></div>
          ${help ? `<p class="hint" style="margin-top:4px"><b>${esc(help[0])}</b> — ${esc(help[1])}</p>` : ''}</div>`;
      }).join('')
    : '<p class="empty">Plan 우회 없음</p>';
  const failures = (routing.failures || []).length
    ? routing.failures.map(([code, count]) => kv(code, num(count), true)).join('')
    : kv('외부 실패', '없음');
  const reasonsCard = card('Plan 우회 사유', '최근 60분',
    reasons + '<div class="hairline"></div>' + failures, 'col-4');

  const timeline = usage.timeline || [];
  const peak = Math.max(1, ...timeline.map(hour => tokens(hour.external.tokens) + tokens(hour.plan.tokens)));
  const bars = timeline.map(hour => {
    const ext = tokens(hour.external.tokens), plan = tokens(hour.plan.tokens);
    return `<div class="tl-col" title="${esc(hour.hour)}시 · OpenRouter ${num(ext)} · Plan ${num(plan)}">
      <div class="tl-plan" style="height:${Math.round(plan / peak * 100)}%"></div>
      <div class="tl-ext" style="height:${Math.round(ext / peak * 100)}%"></div></div>`;
  }).join('');
  const labels = timeline.map((hour, index) =>
    `<span>${index % 3 === 0 ? hour.hour.slice(11) + '시' : ''}</span>`).join('');
  const timelineCard = card('시간대별 토큰', '최근 24시간',
    `<div class="timeline">${bars || '<p class="empty">데이터 없음</p>'}</div>
     <div class="tl-labels">${labels}</div>
     <div class="legend"><span><i style="background:#43d9a3"></i>OpenRouter</span>
     <span><i style="background:#f2b95e"></i>Plan</span></div>`, 'col-8');

  const transitions = (routing.transitions || []).slice().reverse().map(entry => {
    const duration = entry.ongoing ? '<span class="pill pill-ok" style="height:22px">진행 중</span>'
      : entry.seconds === null || entry.seconds === undefined ? '—'
      : entry.seconds < 90 ? `${Math.round(entry.seconds)}초` : `${Math.round(entry.seconds / 60)}분`;
    return `<tr>
      <td class="id">${new Date(entry.first_ts).toLocaleTimeString('ko-KR')}</td>
      <td><span class="pill ${entry.state === 'external' ? 'pill-ok' : 'pill-warn'}" style="height:24px">
        ${esc(stateText[entry.state] || entry.state)}</span></td>
      <td class="id">${esc([entry.reason, entry.detail].filter(Boolean).join(' / ') || '—')}</td>
      <td class="id">${esc(entry.model || '—')}</td>
      <td class="num">${num(entry.count)}</td><td class="num">${duration}</td></tr>`;
  }).join('');
  const transitionsCard = card('라우팅 전환 기록', '최근 12회',
    (routing.transitions || []).length
      ? `<table><thead><tr><th>시작</th><th>경로</th><th>사유</th><th>모델</th><th class="num">건수</th><th class="num">지속</th></tr></thead><tbody>${transitions}</tbody></table>`
      : '<p class="empty">아직 전환 기록이 없습니다. 실행기 세션에서 메시지를 보내면 쌓입니다.</p>');

  const sessions = (usage.sessions || []).map(row => `<tr>
      <td>${esc(row.project)}</td><td class="id">${esc(row.session)}</td>
      <td class="num">${num(row.turns)}</td><td class="num">${num(row.external)}</td><td class="num">${num(row.plan)}</td>
      <td class="num">${num(tokens(row.tokens.external))}</td><td class="num">${usd(row.cost_usd, 4)}</td>
      <td class="id">${esc((row.models[0] || ['—'])[0])}</td></tr>`).join('');
  const sessionsCard = card('세션별 사용', `${num(usage.session_count)}개 세션 · 최근 활동순`,
    (usage.sessions || []).length
      ? `<table><thead><tr><th>프로젝트</th><th>세션</th><th class="num">턴</th><th class="num">외부</th>
         <th class="num">Plan</th><th class="num">외부 토큰</th><th class="num">비용</th><th>모델</th></tr></thead>
         <tbody>${sessions}</tbody></table>`
      : '<p class="empty">세션 기록 없음</p>');

  const recent = (routing.recent || []).slice().reverse().map(event => `<tr>
      <td class="id">${new Date(event.ts).toLocaleTimeString('ko-KR')}</td>
      <td class="id">${esc(event.event || '—')}</td>
      <td>${esc(event.provider || '—')}</td>
      <td class="id">${esc([event.reason, event.detail].filter(Boolean).join(' / ') || '—')}</td>
      <td class="num">${esc(event.status ?? '')}</td></tr>`).join('');
  const recentCard = card('최근 라우팅 이벤트', '최대 15건',
    (routing.recent || []).length
      ? `<table><thead><tr><th>시각</th><th>이벤트</th><th>경로</th><th>사유</th><th class="num">HTTP</th></tr></thead><tbody>${recent}</tbody></table>`
      : '<p class="empty">아직 기록이 없습니다.</p>');

  document.getElementById('grid').innerHTML =
    [statusCard, splitCard, creditCard, tokensCard, reasonsCard, timelineCard, transitionsCard, sessionsCard, recentCard].join('');
}

function renderHelp(data) {
  const help = data.help || {};
  const commands = (help.commands || []).map(([command, description]) =>
    `${command.padEnd(52)}# ${description}`).join('\\n');
  const rows = list => (list || []).map(([left, right]) =>
    `<div class="row-kv"><span class="k mono">${esc(left)}</span><span class="v">${esc(right)}</span></div>`).join('');
  const reasons = Object.entries(help.reasons || {}).map(([code, values]) =>
    `<tr><td class="id">${esc(code)}</td><td>${esc(values[0])}</td><td>${esc(values[1])}</td></tr>`).join('');
  const body = `<details open><summary>터미널 명령</summary>
      <pre class="code">${esc(commands)}</pre></details>
    <details><summary>HTTP API</summary><div class="card-b">${rows(help.endpoints)}</div></details>
    <details><summary>라우팅 사유 코드 ${Object.keys(help.reasons || {}).length}개</summary>
      <table><thead><tr><th>코드</th><th>의미</th><th>조치</th></tr></thead><tbody>${reasons}</tbody></table></details>
    <details><summary>상태 · 종료 코드 · 데이터 출처</summary>
      <div class="card-b">
        <div class="row-kv" style="color:var(--fg-4)"><span class="k">상태</span><span class="v">의미</span></div>
        ${rows(help.states)}<div class="hairline"></div>
        <div class="row-kv" style="color:var(--fg-4)"><span class="k">종료 코드</span><span class="v">의미</span></div>
        ${rows(help.exit_codes)}<div class="hairline"></div>
        <div class="row-kv" style="color:var(--fg-4)"><span class="k">데이터</span><span class="v">출처</span></div>
        ${rows(help.sources)}
      </div></details>`;
  document.getElementById('help').innerHTML =
    `<section class="card col-12" id="help-card"><header class="card-h">
      <div><h2>도움말 — 화면 읽는 법</h2><p class="sub">상태 배지 · 사유 코드 · CLI · API</p></div></header>
      <div class="card-b">${body}</div></section>`;
}

function render(data) {
  SNAP = data;
  renderHeader(data);
  renderAlerts(data);
  renderGrid(data);
  renderHelp(data);
}

async function tick(force) {
  try {
    const response = await fetch('/api/snapshot', {cache:'no-store'});
    render(await response.json());
    if (force) toast('ok', '새로고침했습니다');
  } catch (error) {
    document.getElementById('live-text').textContent = '연결 끊김';
    document.getElementById('live-pill').className = 'pill pill-bad';
  }
}
loadConfig();
tick();
setInterval(() => tick(), 2000);
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
            self._send(200, "text/html; charset=utf-8", PAGE.replace("__DASHBOARD_TOKEN__", TOKEN))
        elif self.path.split("?")[0] == "/api/config":
            try:
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(config_snapshot(), ensure_ascii=False))
            except Exception:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": "config_failed"}))
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

    def _authorized(self):
        token = self.headers.get("x-dashboard-token") or ""
        return bool(token) and secrets.compare_digest(token, TOKEN)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/config", "/api/test"):
            return self._send(404, "application/json; charset=utf-8", json.dumps({"error": "not_found"}))
        if not self._authorized():
            return self._send(403, "application/json; charset=utf-8", json.dumps({"error": "token_required"}))
        origin = self.headers.get("origin")
        port = self.server.server_address[1]
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._send(403, "application/json; charset=utf-8", json.dumps({"error": "origin_rejected"}))
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_length"}))
        if length > BYTES_CAP:
            return self._send(413, "application/json; charset=utf-8", json.dumps({"error": "request_too_large"}))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_json"}))
        if not isinstance(payload, dict):
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_payload"}))
        provider = payload.get("provider")
        if provider not in CONFIG_FILES:
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "unknown_provider"}))
        if path == "/api/config":
            values = payload.get("values")
            if not isinstance(values, dict):
                return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_values"}))
            try:
                saved = save_config(provider, values)
            except ValueError as error:
                return self._send(400, "application/json; charset=utf-8",
                                  json.dumps({"error": str(error)}))
            except OSError:
                return self._send(500, "application/json; charset=utf-8", json.dumps({"error": "write_failed"}))
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps({"saved": True, "provider": provider, "config": saved},
                                         ensure_ascii=False))
        mode = payload.get("mode") or "metadata"
        if mode not in ("metadata", "inference"):
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_mode"}))
        test_values = payload.get("values")
        if test_values is not None and not isinstance(test_values, dict):
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_values"}))
        return self._send(200, "application/json; charset=utf-8",
                          json.dumps(run_connection_test(provider, mode, test_values), ensure_ascii=False))

    def log_message(self, *args):
        pass


class DashboardServer(ThreadingHTTPServer):
    """Exclusive loopback listener: a stale instance must never share the port on Windows."""

    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _read_pid_record(path=None):
    """Previous instance record, so a redeploy can take the same port instead of moving."""
    try:
        data = json.loads(Path(path or PID_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("pid"), int) and isinstance(data.get("port"), int):
        return data
    return None


def _write_pid_record(port, path=None):
    record = {"pid": os.getpid(), "port": port,
              "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        Path(path or PID_FILE).write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass
    return record


def _port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def _stop_previous(port, path=None, timeout=5.0):
    """Terminate a dashboard this launcher started before, keeping the requested port stable."""
    record = _read_pid_record(path)
    if not record or record.get("pid") == os.getpid() or record.get("port") != port:
        return False
    if not _port_open(port):
        return False
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.15)
    return not _port_open(port)


def serve(host="127.0.0.1", port=DEFAULT_PORT, refresh=3, allow_fallback=False):
    threading.Thread(target=_refresh_loop, args=(max(1, refresh),), daemon=True).start()
    # A previous dashboard keeps its port between restarts; take it over instead of moving.
    _stop_previous(port)
    fallback = None
    try:
        server = DashboardServer((host, port), Handler)
    except OSError as error:
        if not allow_fallback:
            raise SystemExit(
                f"port {port} is unavailable ({error}). Stop the other listener or start with "
                f"--port <port>, or pass --allow-port-fallback to pick a free port.") from None
        server = DashboardServer((host, 0), Handler)
        fallback = "requested_port_unavailable"
    _write_pid_record(server.server_address[1])
    url = f"http://{host}:{server.server_address[1]}/"
    print(json.dumps({"dashboard": url, "route_log": str(ROUTE_LOG), "read_only": True,
                      **({"port_fallback": fallback} if fallback else {})}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        record = _read_pid_record()
        if record and record.get("pid") == os.getpid():
            try:
                PID_FILE.unlink()
            except OSError:
                pass


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
    stale = proxy_stale(routing)
    report = {
        "ok": not critical and not stale and routing["state"] in ("external", "idle"),
        "state": routing["state"],
        "proxy_stale": stale,
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
                   "route_log": str(ROUTE_LOG), "proxy_stale": proxy_stale(routing),
                   "proxy_code": routing.get("proxy_code"),
                   "code_modified_at": (code_modified_at().isoformat(timespec="seconds")
                                        if code_modified_at() else None)},
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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--refresh", type=int, default=3, help="Seconds between background scans")
    parser.add_argument("--status", action="store_true", help="Print one JSON status report and exit")
    parser.add_argument("--usage", action="store_true", help="Include token and cost totals (slower)")
    parser.add_argument("--offline", action="store_true", help="Skip OpenRouter account and pricing calls")
    parser.add_argument("--pretty", action="store_true", help="Indent the status report")
    parser.add_argument("--allow-port-fallback", action="store_true",
                        help="Pick a free port instead of failing when the requested port is busy")
    parser.add_argument("--stop", action="store_true",
                        help="Stop only the dashboard recorded in .dashboard.pid")
    args = parser.parse_args(argv)
    if args.stop:
        record = _read_pid_record()
        if not record:
            print(json.dumps({"stopped": False, "reason": "no_record"}))
            return 1
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except OSError as error:
            print(json.dumps({"stopped": False, "pid": record["pid"], "reason": str(error)}))
            return 1
        print(json.dumps({"stopped": True, "pid": record["pid"], "port": record["port"]}))
        return 0
    if args.status:
        report = status_report(include_usage=args.usage, offline=args.offline)
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
        return status_exit_code(report)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("Bind a loopback address only")
    serve(args.host, args.port, args.refresh, allow_fallback=args.allow_port_fallback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
