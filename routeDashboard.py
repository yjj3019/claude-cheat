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
import hashlib
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
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROUTE_LOG = ROOT / ".route.log"
METRICS_FILE = ROOT / ".proxy-metrics.json"
PROXY_FILE = ROOT / "claudeRouteProxy.mjs"
PID_FILE = ROOT / ".dashboard.pid"
PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_PORT = 8787
ACCOUNT_TTL = 60
SESSION_SUFFIX = ".jsonl"
ROUTE_WINDOW = 60
ROUTING_EVENTS_CAP = 2000
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
        ["powershell -File OpenClaudeDashboard.ps1", "고정 포트(8787)로 이 페이지 열기"],
        ["python routeDashboard.py --stop", "대시보드만 종료 (실행기 세션은 유지)"],
    ],
    "endpoints": [
        ["GET /api/status", "위와 같은 요약 JSON"],
        ["GET /api/snapshot", "이 페이지가 쓰는 전체 데이터"],
        ["GET /api/config", "두 제공자 설정(비밀값은 마스킹)"],
        ["GET /api/metrics (프록시)", "프록시 카운터. 같은 세션 토큰 필요, 127.0.0.1 전용"],
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
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("\"'")
    return values


_ROUTE_TAIL = {"path": None, "events": [], "offset": 0, "digest": None}


def _parse_route_line(line):
    stamp, _, rest = line.partition(" ")
    rest = rest.strip()
    start = re.fullmatch(r"--- proxy start(?: code=(\S+))? ---", rest)
    if start:
        return {"ts": stamp, "event": "proxy_start", **({"code": start.group(1)} if start.group(1) else {})}
    if not rest.startswith("{"):
        return None
    try:
        payload = json.loads(rest)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {"ts": stamp, **payload}


def _parse_route_log_unlocked(path=None, limit=5000, incremental=True):
    """Return the tail of the route log as events with a UTC timestamp.

    The log only grows during a session, so a parked byte offset keeps each refresh to
    the lines appended since the previous one. A truncated or replaced file falls back
    to a full read, a trailing partial line waits for its newline, and
    `incremental=False` always reads everything.
    """
    path = Path(path or ROUTE_LOG)
    try:
        info = path.stat()
    except OSError:
        _ROUTE_TAIL.update({"path": None, "events": [], "offset": 0, "digest": None})
        return []
    path_key = str(path.resolve())
    state = _ROUTE_TAIL
    try:
        with path.open("rb") as handle:
            prefix = handle.read(256)
            digest = hashlib.sha256(prefix).hexdigest()
            # A rewritten or rotated file keeps its byte length often enough that size
            # alone cannot tell it apart from an append; the leading bytes can.
            if (incremental and state["path"] == path_key and state["digest"] == digest
                    and state["offset"] <= info.st_size):
                start = state["offset"]
            else:
                start = 0
                state["events"] = []
            if start <= len(prefix):
                chunk = prefix[start:] + handle.read()
            else:
                handle.seek(start)
                chunk = handle.read()
    except OSError:
        return list(state["events"])
    if chunk:
        end = chunk.rfind(b"\n")
        if end < 0:
            state.update({"path": path_key, "digest": digest})
            return list(state["events"])
        complete = chunk[: end + 1]
        for raw in complete.splitlines():
            parsed = _parse_route_line(raw.decode("utf-8", errors="replace"))
            if parsed:
                state["events"].append(parsed)
        state["events"] = state["events"][-limit:]
        start += len(complete)
    state.update({"path": path_key, "digest": digest, "offset": start})
    return list(state["events"])


_TAIL_LOCK = threading.Lock()


def parse_route_log(path=None, limit=5000, incremental=True):
    """Thread-safe wrapper: the parked offset is shared by request handlers and the refresher."""
    with _TAIL_LOCK:
        return _parse_route_log_unlocked(path, limit, incremental)


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
    # Bound the per-refresh work: one CLI session keeps far fewer events in a 60 minute
    # window, while a pathological log still renders in a few milliseconds.
    if len(events) > ROUTING_EVENTS_CAP:
        events = events[-ROUTING_EVENTS_CAP:]
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
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(size - offset)
    head, sep, _tail = chunk.rpartition(b"\n")
    if not sep:
        return [], offset
    # The caller commits the offset only after every line was parsed, so one bad record
    # cannot swallow the lines after it.
    return head.split(b"\n"), offset + len(head) + 1


def _new_hour_bucket():
    return {"external": {"turns": 0, "tokens": [0, 0, 0, 0]},
            "plan": {"turns": 0, "tokens": [0, 0, 0, 0]}}


def _empty_usage(now=None):
    now = now or datetime.now()
    zero = {"external": [0, 0, 0, 0], "plan": [0, 0, 0, 0]}
    return {"totals": {k: list(v) for k, v in zero.items()},
            "today": {k: list(v) for k, v in zero.items()},
            "timeline": [], "cost_usd": None, "today_cost_usd": None,
            "current_hour": now.strftime("%Y-%m-%dT%H"), "sessions": [], "session_count": 0}


def _session_cost(tokens, pricing):
    if not pricing or not pricing.get("ok"):
        return None
    return (tokens[0] * pricing["prompt"] + tokens[1] * pricing["completion"] +
            tokens[2] * pricing["cache_read"] + tokens[3] * pricing["cache_write"])


def _transcript_files(root):
    """Yield (path, mtime, size) for transcript files.

    Walks with ``os.scandir`` so Windows reuses the metadata already returned by the
    directory read: a ``Path.rglob`` plus ``stat`` pays for a second syscall per file.
    """
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield from _transcript_files(entry.path)
                    elif entry.name.endswith(SESSION_SUFFIX):
                        info = entry.stat()
                        yield Path(entry.path), info.st_mtime, info.st_size
                except OSError:
                    continue
    except OSError:
        return


_USAGE_CACHE = {"key": None, "data": None}


def scan_sessions(projects=None, max_files=SCAN_FILES, now=None, pricing=None):
    """Incrementally total token usage per provider from Claude Code transcripts."""
    projects = Path(projects or PROJECTS)
    now = now or datetime.now()
    if not projects.is_dir():
        return _empty_usage(now)
    cutoff = (now - timedelta(days=SCAN_DAYS)).timestamp()
    candidates = []
    total_size, newest = 0, 0.0
    for path, modified, size in _transcript_files(projects):
        total_size += size
        newest = max(newest, modified)
        if modified >= cutoff:
            candidates.append((modified, path))
    # Nothing was appended, removed or touched since the previous scan: reuse the result.
    price_key = None if not pricing else (pricing.get("prompt"), pricing.get("completion"),
                                          pricing.get("cache_read"), pricing.get("cache_write"))
    cache_key = (str(projects), len(candidates), newest, total_size, now.strftime("%Y%m%d%H"),
                 price_key, max_files)
    if _USAGE_CACHE["key"] == cache_key and _USAGE_CACHE["data"] is not None:
        return _USAGE_CACHE["data"]
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
                lines, next_offset = _read_appended(path, state)
            except OSError:
                continue
            for line in lines:
                if b'"assistant"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    usage = {}
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
            state["offset"] = next_offset
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
        today_cost = _session_cost(hour_totals["external"], pricing) if pricing and pricing.get("ok") else None
        usage = {"totals": totals, "today": hour_totals, "current_hour": current_hour,
                 "timeline": timeline,
                 # cost_usd is the retained window (7 days); today_cost_usd is today's spend.
                 "cost_usd": round(total_cost, 6) if pricing and pricing.get("ok") else None,
                 "today_cost_usd": round(today_cost, 6) if today_cost is not None else None,
                 "sessions": sessions[:8], "session_count": len(sessions)}
        _USAGE_CACHE["key"] = cache_key
        _USAGE_CACHE["data"] = usage
        return usage


_USAGE_SNAP = {"signature": None, "at": 0.0, "data": None}


def routing_signature(routing):
    """Cheap change signal: a new routing decision means new assistant tokens exist."""
    last = routing.get("last") or {}
    recent = routing.get("recent") or []
    return (last.get("ts"), len(recent), last.get("status"))


def usage_snapshot(pricing=None, signature=None, ttl=60):
    """Skip the transcript walk while nothing new was routed.

    A new assistant turn always produces a ``route_selected`` event, so an unchanged
    signature cannot hide new tokens; a periodic rescan still refreshes clock-derived
    fields such as the 24-hour timeline.
    """
    now = time.monotonic()
    cached = _USAGE_SNAP["data"]
    if (cached is not None and signature is not None
            and _USAGE_SNAP["signature"] == signature
            and now - _USAGE_SNAP["at"] < ttl):
        return cached
    data = scan_sessions(pricing=pricing)
    _USAGE_SNAP.update({"signature": signature, "at": now, "data": data})
    return data


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
    # Switching OPENROUTER_MODEL must not reuse the previous model's rates.
    if cached and cache.get("model") == model and now - cache.get("at", 0) < PRICING_TTL:
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
    cache["data"], cache["at"], cache["model"] = result, now, model
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
                       "detail": "실행 중인 프록시가 현재 코드보다 먼저 시작되었습니다. 수정된 라우팅 필터가 적용되지 않아 Plan으로 우회할 수 있으니 세션을 재시작하세요.",
                       "command": ".\\OpenClaude.ps1"})
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
    for alert in alerts:
        alert["code"] = ALERT_CODES.get(alert["title"])
    order = {"critical": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda item: order.get(item["level"], 3))
    return alerts


ALERT_CODES = {
    "외부 회로 차단": "circuit_open", "Plan 우회 급증": "plan_spike", "외부 요청 거부": "rejected",
    "현재 Plan 경로": "plan_state", "실행기 재시작 필요": "proxy_stale",
    "OpenRouter 잔액 부족": "low_balance", "크레딧 확인 실패": "account_error",
    "단가 조회 실패": "pricing_error",
}
INSIGHT_CODES = {
    "실행기 코드가 오래됨": "proxy_stale", "외부 회로 차단": "circuit_open",
    "Plan 우회 급증": "plan_spike", "Plan 비중 상승": "plan_share", "외부 요청 거부": "rejected",
    "한도 소진 예상": "burn", "사용 속도": "burn_rate", "캐시 재사용 저하": "cache",
    "응답 지연": "latency", "실패 응답 누적": "failed_requests", "세션 유휴": "idle",
}

PLAN_SHARE_WARN = 0.5
CACHE_LOW_RATIO = 0.15
CACHE_MIN_PROMPT = 200_000
LATENCY_WARN_MS = 20_000
IDLE_AFTER_SECONDS = 1800


def _share(part, total):
    return round((part or 0) / total * 100) if total else 0


def build_insights(routing, usage, account, pricing, metrics=None, now=None):
    """Ranked observations: what changed, why it happened, and what to do next.

    Deterministic — every sentence is derived from a measured value, and each entry
    carries the evidence it was built from so the page can show the numbers.
    """
    now = now or datetime.now()
    insights = []

    def add(level, title, detail, action=None, evidence=None, score=0):
        insights.append({"level": level, "title": title, "detail": detail, "action": action,
                         "evidence": [item for item in (evidence or []) if item], "score": score})

    short = routing.get("short") or {}
    counts = short.get("counts") or {}
    minutes = short.get("minutes") or SHORT_WINDOW
    external, plan = counts.get("external") or 0, counts.get("plan") or 0
    total = external + plan
    state = routing.get("state") or "unknown"

    if proxy_stale(routing):
        add("warn", "실행기 코드가 오래됨",
            "실행 중인 프록시가 현재 소스보다 먼저 시작되어 최신 라우팅 규칙이 적용되지 않습니다. "
            "재시작하면 바로 반영됩니다.",
            ".\\OpenClaude.ps1", ["재시작 후 proxy_stale 해제"], score=90)

    opens = short.get("circuit_opens") or 0
    if opens:
        add("critical", "외부 회로 차단",
            f"최근 {minutes}분에 외부 시도가 연속 실패해 회로가 {opens}회 열렸습니다. "
            "열려 있는 동안의 요청은 Plan으로 처리됩니다.",
            "네트워크·키·모델 ID 확인 후 재시도",
            [f"회로 개방 {opens}회"], score=85)

    if total and plan / total >= PLAN_SHARE_WARN:
        top = (short.get("plan_reasons") or [[None, 0]])[0]
        detail = f"최근 {minutes}분 결정 {total}건 중 {plan}건이 Plan입니다"
        if top[0]:
            detail += f" (최다 사유 {top[0]}, {top[1]}건)"
            explanation = REASON_HELP.get(top[0])
            if explanation:
                detail += f". {explanation[0]} — {explanation[1]}"
        add("warn", "Plan 우회 급증", detail,
            evidence=[f"{code}: {count}" for code, count in (short.get("plan_reasons") or [])[:3]], score=80)
    elif total and plan / total >= 0.2:
        add("info", "Plan 비중 상승",
            f"최근 {minutes}분 Plan 비중이 {_share(plan, total)}%입니다. 추세가 오르면 원인 코드를 확인하세요.",
            evidence=[f"외부 {external} / Plan {plan}"], score=45)

    rejected = [(label, count) for label, count in short.get("failures") or []
                if str(label).startswith(("400", "413", "422"))]
    if rejected:
        label, count = rejected[0]
        add("warn", "외부 요청 거부",
            f"{label} 응답이 {count}건 발생했습니다. 요청 형식이나 모델 한도가 원인일 수 있어 실행기 갱신이 필요합니다.",
            ".\\OpenClaude.ps1", [f"{label}: {count}건"], score=70)

    today = usage.get("today") or {}
    prompt_tokens = (today.get("external") or [0, 0, 0, 0])[0]
    cached_tokens = (today.get("external") or [0, 0, 0, 0])[2]
    spent_today = usage.get("today_cost_usd")
    remaining = account.get("remaining_usd") if account.get("ok") else None
    hours = now.hour + now.minute / 60
    if isinstance(spent_today, (int, float)) and spent_today > 0 and hours >= 0.5:
        per_day = spent_today / hours * 24
        if isinstance(remaining, (int, float)) and per_day > 0:
            days = remaining / per_day
            if days < 7:
                add("critical" if days < 2 else "warn", "한도 소진 예상",
                    f"오늘 속도(${spent_today:.2f} / {hours:.1f}시간)가 이어지면 하루 약 ${per_day:.2f}, "
                    f"남은 ${remaining:.2f}는 약 {days:.1f}일 뒤 소진됩니다.",
                    "모델 단가나 사용량 조정", [f"하루 약 ${per_day:.2f}"], score=75)
            else:
                add("info", "사용 속도",
                    f"현재 속도라면 남은 한도(${remaining:.2f})는 약 {days:.0f}일 유지됩니다.",
                    evidence=[f"하루 약 ${per_day:.2f}"], score=30)
        elif account.get("ok") is False:
            add("info", "사용 속도", f"오늘 ${spent_today:.2f}를 사용했습니다(한도 정보 없음).",
                evidence=[f"{hours:.1f}시간 경과"], score=20)

    if prompt_tokens >= CACHE_MIN_PROMPT:
        ratio = cached_tokens / max(prompt_tokens, 1)
        if ratio < CACHE_LOW_RATIO:
            add("warn", "캐시 재사용 저하",
                f"오늘 입력 {prompt_tokens:,}토큰 중 캐시 재사용은 {_share(cached_tokens, prompt_tokens)}%입니다. "
                "캐시가 끊기면 같은 작업의 단가가 크게 오릅니다.",
                "프롬프트 앞부분 고정·세션 재시작 간격 단축",
                [f"캐시 {cached_tokens:,}토큰"], score=55)

    if metrics:
        raw_latency = metrics.get("latencyMs")
        latency = raw_latency if isinstance(raw_latency, dict) else {}
        p95 = (latency.get("external") or {}).get("p95")
        ttfb95 = (latency.get("ttfb") or {}).get("p95")
        if isinstance(p95, (int, float)) and p95 >= LATENCY_WARN_MS:
            if isinstance(ttfb95, (int, float)) and ttfb95 < p95 * 0.5:
                add("warn", "응답 지연",
                    f"p95 {p95 / 1000:.1f}초 중 첫 바이트까지 {ttfb95 / 1000:.1f}초, "
                    f"나머지 {(p95 - ttfb95) / 1000:.1f}초는 생성 대기입니다. 프록시 구간이 아니라 "
                    "모델이 답을 만드는 시간이므로, 먼저 보이게 하려면 OpenClaude.ps1 -Stream을 쓰세요.",
                    evidence=[f"p95 {p95 / 1000:.1f}s", f"첫 바이트 {ttfb95 / 1000:.1f}s"], score=60)
            else:
                add("warn", "응답 지연",
                    f"이 세션 외부 응답 p95가 {p95 / 1000:.1f}초입니다. 회로 차단·재시도가 늘기 전에 확인하세요.",
                    evidence=[f"p95 {p95 / 1000:.1f}s"], score=60)
        failed = metrics.get("failed")
        if isinstance(failed, int) and failed > 0:
            add("info", "실패 응답 누적",
                f"이 세션에서 실패로 끝난 요청이 {failed}건 있습니다.",
                evidence=[f"실패 {failed} / 요청 {metrics.get('requests')}"], score=40)

    last = routing.get("last") or {}
    age = last.get("age_seconds")
    if state == "idle" and isinstance(age, (int, float)) and age >= IDLE_AFTER_SECONDS:
        add("info", "세션 유휴",
            f"마지막 라우팅 결정이 {age / 60:.0f}분 전입니다. 새 세션이 실행 중이면 이 화면은 갱신만 하고 있습니다.",
            evidence=[f"{age / 60:.0f}분 경과"], score=15)

    insights.sort(key=lambda item: item["score"], reverse=True)
    insights = insights[:5]
    for item in insights:
        item["code"] = INSIGHT_CODES.get(item["title"])
    return insights


def provider_configured(provider):
    """True when every required field of a provider is present in its .env file."""
    values = _effective_values(provider)
    return all(bool(values.get(name)) for name in CONFIG_FILES[provider]["required"])


def any_provider_configured():
    return any(provider_configured(name) for name in CONFIG_FILES)


def summary_line(routing, usage, account, insights):
    """One sentence a person can read before looking at the cards."""
    state = routing.get("state") or "unknown"
    head = {"external": "OpenRouter로 정상 처리 중", "plan": "Plan으로 우회 중",
            "idle": "세션 대기 중", "unknown": "라우팅 기록 없음"}.get(state, "상태 확인 필요")
    parts = [head]
    today = usage.get("today") or {}
    tokens = sum(today.get("external") or [])
    cost = usage.get("today_cost_usd")
    if tokens:
        parts.append(f"오늘 {tokens:,}토큰" + (f" · ${cost:.3f}" if isinstance(cost, (int, float)) else ""))
    short = routing.get("short") or {}
    counts = short.get("counts") or {}
    total = (counts.get("external") or 0) + (counts.get("plan") or 0)
    if total:
        parts.append(f"최근 {short.get('minutes') or SHORT_WINDOW}분 Plan {_share(counts.get('plan'), total)}%")
    if account.get("ok") and isinstance(account.get("remaining_usd"), (int, float)):
        parts.append(f"남은 한도 ${account['remaining_usd']:.2f}")
    if insights:
        parts.append("우선 확인: " + insights[0]["title"])
    return " · ".join(parts)


def refresh_interval_ms(routing):
    """Active sessions refresh quickly; an idle session does not need two seconds."""
    return 2000 if (routing.get("state") or "unknown") in ("external", "plan") else 10000


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
        original = path.read_text(encoding="utf-8-sig", errors="replace")
        existed = True
    except FileNotFoundError:
        # A new file starts from the shipped example so its comments survive the save.
        original = base or ""
        existed = False
    # Any other OSError propagates: rewriting a file we could not read would drop settings.
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
    if existed:
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
    # Merge stored settings first: posting only one field must not look like a missing one,
    # and a stored value must be validated together with the new one.
    merged = _effective_values(provider, clean)
    for name in spec["required"]:
        if not merged.get(name):
            raise ValueError(f"missing_{name.lower()}")
    _validate_config(provider, merged)
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


MODELS_TTL = 600
_MODELS_CACHE = {}


def _models_endpoint(provider, values):
    """Where the provider publishes its model list; Local reuses the configured origin."""
    from urllib.parse import urlsplit

    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/models", {}
    explicit = (values.get("LOCAL_LLM_MODELS_URL") or "").strip()
    if explicit:
        target = urlsplit(explicit)
        if target.scheme not in ("http", "https") or not target.hostname:
            raise ValueError("invalid_models_url")
        return explicit, {}
    base = urlsplit((values.get("LOCAL_LLM_URL") or "").strip())
    if base.scheme not in ("http", "https") or not base.hostname:
        raise ValueError("invalid_local_url")
    path = base.path.rstrip("/")
    if path.endswith("/messages"):
        path = path[: -len("/messages")]
    return base._replace(path=path + "/models", query="", fragment="").geturl(), {}


def _parse_models(payload):
    """Accept both OpenAI-style {data: [...]} and Ollama-style {models: [...]} lists."""
    rows = None
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    rows = rows if isinstance(rows, list) else []
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id") or row.get("name") or row.get("model")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        models.append({
            "id": identifier.strip(),
            "name": row.get("name") if isinstance(row.get("name"), str) else identifier.strip(),
            "context_length": row.get("context_length") if isinstance(row.get("context_length"), int) else None,
            "prompt_usd_per_m": _price_per_million(pricing.get("prompt")),
            "completion_usd_per_m": _price_per_million(pricing.get("completion")),
        })
    models.sort(key=lambda entry: entry["id"])
    return models


def _price_per_million(value):
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def model_catalog(provider="openrouter", values=None, refresh=False, now=None):
    """Model ids and metadata for the settings picker; never returns credentials."""
    if provider not in CONFIG_FILES:
        raise ValueError("unknown_provider")
    import time as _time

    values = _effective_values(provider, values)
    key = values.get("ANTHROPIC_AUTH_TOKEN") if provider == "openrouter" else values.get("LOCAL_LLM_API_KEY")
    try:
        url, _ = _models_endpoint(provider, values)
    except ValueError as error:
        return {"provider": provider, "ok": False, "error": str(error), "models": []}
    cache_key = (provider, url)
    cached = _MODELS_CACHE.get(cache_key)
    stamp = now if isinstance(now, (int, float)) else _time.time()
    if cached and not refresh and stamp - cached["at"] < MODELS_TTL:
        return {**cached["data"], "cached": True, "age_seconds": round(stamp - cached["at"], 1)}
    headers = {"accept": "application/json"}
    if key:
        headers["authorization"] = "Bearer " + key
    try:
        status, payload = _http_json(url, headers)
    except Exception as error:  # network, timeout, HTTP status or JSON errors
        import urllib.error
        reason = "connection_failed"
        code = None
        if isinstance(error, urllib.error.HTTPError):
            code = error.code
            reason = "authentication_failed" if code in (401, 403) else "http_error"
        data = {"provider": provider, "ok": False, "error": reason, "status": code, "models": []}
        return data
    models = _parse_models(payload)
    data = {"provider": provider, "ok": bool(models), "error": None if models else "empty_catalog",
            "status": status, "models": models}
    if models:
        _MODELS_CACHE[cache_key] = {"at": stamp, "data": data}
    return data


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


def proxy_metrics():
    """Counters the routing proxy writes for this session, or None when it never ran."""
    try:
        info = METRICS_FILE.stat()
        data = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    age = max(0.0, round(time.time() - info.st_mtime, 1))
    data["age_seconds"] = age
    data["stale"] = age > 20
    return data


def snapshot():
    """Everything the page renders, in one JSON document."""
    now = datetime.now(timezone.utc)
    config = read_env(ROOT / ".env.openrouter")
    key = config.get("ANTHROPIC_AUTH_TOKEN")
    model = config.get("OPENROUTER_MODEL") or None
    routing = routing_state(parse_route_log(), now=now)
    pricing = model_pricing(key, model)
    account = account_state(key)
    usage = usage_snapshot(pricing=pricing, signature=routing_signature(routing))
    metrics = proxy_metrics()
    insights = build_insights(routing, usage, account, pricing, metrics=metrics)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "routing": routing,
        "usage": usage,
        "account": account,
        "pricing": pricing,
        "alerts": build_alerts(routing, usage, account, pricing),
        "insights": insights,
        "summary": summary_line(routing, usage, account, insights),
        "configured": any_provider_configured(),
        "refresh_ms": refresh_interval_ms(routing),
        "proxy_stale": proxy_stale(routing),
        "proxy_metrics": metrics,
        "proxy_modified_at": (stamp.isoformat(timespec="seconds") if (stamp := code_modified_at()) else None),
        "help": {"reasons": REASON_HELP, **HELP_SECTION},
        "model": model,
    }


def cached_snapshot():
    """Serve the last completed snapshot so a slow first scan never blocks the page."""
    data = _SNAPSHOT.get("data")
    error = _SNAPSHOT.get("error")
    if data is not None:
        return {**data, "snapshot_error": error} if error else data
    now = datetime.now(timezone.utc)
    config = read_env(ROOT / ".env.openrouter")
    key = config.get("ANTHROPIC_AUTH_TOKEN")
    routing = routing_state(parse_route_log(), now=now)
    account = account_state(key)
    usage = _USAGE_SNAP["data"] or _empty_usage()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "building": True,
        "routing": routing,
        "usage": usage,
        "account": account,
        "pricing": None,
        "alerts": build_alerts(routing, usage, account, None),
        "insights": [],
        "summary": summary_line(routing, usage, account, []),
        "configured": any_provider_configured(),
        "refresh_ms": refresh_interval_ms(routing),
        "proxy_stale": proxy_stale(routing),
        "help": {"reasons": REASON_HELP, **HELP_SECTION},
        "model": config.get("OPENROUTER_MODEL") or None,
    }


def _refresh_loop(interval):
    while True:
        try:
            _SNAPSHOT["data"] = snapshot()
            _SNAPSHOT["at"] = time.monotonic()
            _SNAPSHOT["error"] = None
        except Exception as error:  # a broken snapshot must never freeze the page silently
            _SNAPSHOT["error"] = {"message": f"{type(error).__name__}: {error}",
                                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if _SNAPSHOT.get("reported") != str(error):
                _SNAPSHOT["reported"] = str(error)
                import traceback
                traceback.print_exc()
        time.sleep(interval)


# Shared page assets; one shell renders the dashboard and the settings page.
PAGE_CSS = "\n:root{\n color-scheme:dark;\n --bg:#070b12; --bg-soft:#0b1119; --panel:#0f1622; --panel-2:#121a28; --panel-3:#162031;\n --line:#1d2739; --line-2:#27344b; --line-3:#33465f;\n --fg:#e9eff8; --fg-2:#b6c2d4; --fg-3:#8391a7; --fg-4:#5f6d83;\n --accent:#5b9dff; --accent-soft:rgba(91,157,255,.16); --accent-line:rgba(91,157,255,.45);\n --ok:#43d9a3; --ok-soft:rgba(67,217,163,.14); --ok-line:rgba(67,217,163,.4);\n --warn:#f2b95e; --warn-soft:rgba(242,185,94,.14); --warn-line:rgba(242,185,94,.4);\n --bad:#ff7089; --bad-soft:rgba(255,112,137,.14); --bad-line:rgba(255,112,137,.4);\n --radius:16px; --radius-sm:11px; --radius-xs:8px;\n --shadow:0 18px 40px -24px rgba(0,0,0,.9), 0 2px 8px rgba(0,0,0,.35);\n --mono:ui-monospace,\"Cascadia Mono\",\"JetBrains Mono\",Consolas,monospace;\n --sans:\"Segoe UI Variable Text\",\"Segoe UI\",system-ui,-apple-system,\"Malgun Gothic\",sans-serif;\n}\n*{box-sizing:border-box}\nhtml,body{height:100%}\nbody{margin:0;background:\n radial-gradient(1200px 600px at 15% -10%, rgba(91,157,255,.10), transparent 60%),\n radial-gradient(900px 500px at 100% 0%, rgba(67,217,163,.06), transparent 55%),\n var(--bg);\n color:var(--fg);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased}\na{color:var(--accent);text-decoration:none}\na:hover{text-decoration:underline}\nh1,h2,h3,p{margin:0}\n.wrap{max-width:1460px;margin:0 auto;padding:0 28px}\n.mono{font-family:var(--mono);font-size:12px;letter-spacing:-.1px}\n.muted{color:var(--fg-3)}\n\n/* top bar */\n.topbar{position:sticky;top:0;z-index:40;background:rgba(7,11,18,.82);\n backdrop-filter:blur(14px) saturate(140%);border-bottom:1px solid var(--line)}\n.topbar-in{display:flex;align-items:center;justify-content:space-between;gap:20px;height:66px}\n.brand{display:flex;align-items:center;gap:13px;min-width:0}\n.logo{width:34px;height:34px;border-radius:11px;flex:0 0 auto;\n background:linear-gradient(145deg,#5b9dff,#2f6bd6);box-shadow:0 6px 18px -6px rgba(91,157,255,.8);\n position:relative}\n.logo::after{content:\"\";position:absolute;inset:9px;border-radius:50%;\n border:2px solid rgba(255,255,255,.9);border-right-color:transparent;border-bottom-color:transparent;\n transform:rotate(35deg)}\n.brand h1{font-size:15px;font-weight:650;letter-spacing:-.2px}\n.brand p{font-size:11.5px;color:var(--fg-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.top-actions{display:flex;align-items:center;gap:10px;flex:0 0 auto}\n\n/* pills + chips */\n.pill{display:inline-flex;align-items:center;gap:8px;height:30px;padding:0 12px;border-radius:999px;\n font-size:12px;font-weight:600;border:1px solid var(--line-2);background:var(--panel-2);color:var(--fg-2);\n white-space:nowrap}\n.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 0 3px rgba(255,255,255,.04)}\n.pill-ok{color:var(--ok);border-color:var(--ok-line);background:var(--ok-soft)}\n.pill-warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-soft)}\n.pill-bad{color:var(--bad);border-color:var(--bad-line);background:var(--bad-soft)}\n.pill-mute{color:var(--fg-3)}\n.chip{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:7px;\n font-size:11px;font-family:var(--mono);color:var(--fg-2);background:var(--panel-3);border:1px solid var(--line)}\n\n/* layout */\nmain{padding:26px 0 80px}\n.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:18px}\n.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-5{grid-column:span 5}\n.col-6{grid-column:span 6}.col-7{grid-column:span 7}.col-8{grid-column:span 8}\n.col-12{grid-column:span 12}\n@media(max-width:1180px){.col-3{grid-column:span 6}.col-4,.col-5,.col-7,.col-8{grid-column:span 12}\n .col-6{grid-column:span 12}}\n@media(max-width:720px){.col-3{grid-column:span 12}.wrap{padding:0 16px}}\n\n/* cards */\n.card{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);\n border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}\n.card-h{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;\n padding:16px 18px 13px;border-bottom:1px solid var(--line)}\n.card-h h2{font-size:13px;font-weight:650;letter-spacing:-.1px}\n.card-h .sub{font-size:11.5px;color:var(--fg-4);font-family:var(--mono);margin-top:2px}\n.card-b{padding:16px 18px 18px;flex:1;display:flex;flex-direction:column}\n.card-b.tight{padding:12px 18px 16px}\n\n/* buttons */\n.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:34px;padding:0 14px;\n border-radius:var(--radius-xs);border:1px solid var(--line-2);background:var(--panel-3);color:var(--fg);\n font:600 12.5px/1 var(--sans);cursor:pointer;transition:.16s ease;white-space:nowrap}\n.btn:hover:not(:disabled){border-color:var(--line-3);background:#1a2436;transform:translateY(-1px)}\n.btn:active:not(:disabled){transform:none}\n.btn:focus-visible{outline:2px solid var(--accent-line);outline-offset:2px}\n.btn:disabled{opacity:.5;cursor:not-allowed}\n.btn-primary{background:linear-gradient(180deg,#5b9dff,#3b78e8);border-color:#6ba8ff;color:#fff;\n box-shadow:0 8px 18px -10px rgba(91,157,255,.9)}\n.btn-primary:hover:not(:disabled){background:linear-gradient(180deg,#6ba8ff,#4482f0)}\n.btn-ghost{background:transparent}\n.btn-sm{height:28px;padding:0 10px;font-size:11.5px}\n.btn.loading::before{content:\"\";width:11px;height:11px;border-radius:50%;\n border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:spin .7s linear infinite}\n@keyframes spin{to{transform:rotate(360deg)}}\n\n/* metrics */\n.metric{display:flex;flex-direction:column;gap:2px}\n.metric .k{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--fg-4);font-weight:600}\n.metric .v{font-size:23px;font-weight:680;letter-spacing:-.6px;font-variant-numeric:tabular-nums}\n.metric .v small{font-size:12px;font-weight:600;color:var(--fg-3);letter-spacing:0}\n.metric-list{display:flex;flex-direction:column;gap:7px;margin-top:4px}\n.row-kv{display:flex;align-items:baseline;justify-content:space-between;gap:12px;font-size:12.5px}\n.row-kv .k{color:var(--fg-3)}\n.row-kv .v{color:var(--fg);font-variant-numeric:tabular-nums;text-align:right}\n.bar{height:7px;border-radius:999px;background:#0a1119;border:1px solid var(--line);overflow:hidden;margin:12px 0 10px}\n.bar>i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#3b78e8,#43d9a3)}\n.hairline{height:1px;background:var(--line);margin:12px 0}\n\n/* tables */\ntable{width:100%;border-collapse:collapse;font-size:12.5px}\nthead th{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg-4);font-weight:650;\n text-align:left;padding:0 10px 8px 0;border-bottom:1px solid var(--line)}\ntbody td{padding:9px 10px 9px 0;border-bottom:1px solid rgba(255,255,255,.035);vertical-align:middle}\ntbody tr:last-child td{border-bottom:0}\ntbody tr:hover td{background:rgba(91,157,255,.04)}\ntd.num,th.num{text-align:right;font-variant-numeric:tabular-nums}\ntd.id{font-family:var(--mono);font-size:11.5px;color:var(--fg-2)}\n\n/* alerts */\n.alerts{display:flex;flex-direction:column;gap:10px;margin-bottom:18px}\n.alert{display:flex;gap:12px;align-items:flex-start;padding:12px 15px;border-radius:var(--radius-sm);\n border:1px solid var(--line-2);background:var(--panel-2);font-size:12.5px}\n.alert .icon{width:18px;height:18px;border-radius:50%;flex:0 0 auto;margin-top:1px;\n display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}\n.alert b{display:block;font-size:12.5px;margin-bottom:1px}\n.alert .txt b{display:inline;font-size:inherit;margin:0;color:var(--fg);font-weight:650}\n.alert .txt{color:var(--fg-2)}\n.alert.critical{border-color:var(--bad-line);background:var(--bad-soft)}\n.alert.critical .icon{background:var(--bad);color:#3a0a13}\n.alert.warn{border-color:var(--warn-line);background:var(--warn-soft)}\n.alert.warn .icon{background:var(--warn);color:#3a2606}\n.alert.info{border-color:var(--accent-line);background:var(--accent-soft)}\n.alert.info .icon{background:var(--accent);color:#06152e}\n.alert code{font-family:var(--mono);font-size:11.5px;background:#080d15;border:1px solid var(--line);\n padding:1px 5px;border-radius:5px;color:var(--fg-2)}\n.notice{border:1px solid var(--accent-line);background:var(--accent-soft);border-radius:var(--radius-sm);\n padding:11px 13px;margin-bottom:14px;font-size:12.5px;color:var(--fg-2)}\n.notice b{color:var(--fg)}\n\n/* settings form */\n.setting-form{display:flex;flex-direction:column}\n.field{padding:11px 0;border-bottom:1px dashed rgba(255,255,255,.05)}\n.field:last-of-type{border-bottom:0}\n.field label{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--fg-2);margin-bottom:6px}\n.req{font-size:10px;font-weight:700;letter-spacing:.04em;color:var(--fg-4);text-transform:uppercase}\n.input{width:100%;height:38px;padding:0 12px;border-radius:var(--radius-xs);border:1px solid var(--line-2);\n background:#0a111c;color:var(--fg);font:12.5px var(--mono);transition:.15s ease}\n.input::placeholder{color:var(--fg-4);font-family:var(--sans)}\n.input:hover{border-color:var(--line-3)}\n.input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}\n.input[readonly]{opacity:.85}\n.input-group{display:flex;gap:8px}\n.input-group .input{flex:1}\n.hint{font-size:11.5px;color:var(--fg-4);margin-top:6px;line-height:1.5}\n.hint b{color:var(--fg-3)}\n.field.invalid .input{border-color:var(--bad-line);box-shadow:0 0 0 3px var(--bad-soft)}\n.switch{display:inline-flex;align-items:center;gap:10px;cursor:pointer;user-select:none;font-size:12.5px;\n color:var(--fg-2);font-weight:600}\n.switch input{position:absolute;opacity:0;width:0;height:0}\n.switch .track{width:38px;height:22px;border-radius:999px;background:#0a111c;border:1px solid var(--line-2);\n position:relative;transition:.18s ease;flex:0 0 auto}\n.switch .track::after{content:\"\";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;\n background:var(--fg-3);transition:.18s ease}\n.switch input:checked+.track{background:var(--accent-soft);border-color:var(--accent-line)}\n.switch input:checked+.track::after{left:18px;background:var(--accent)}\n.switch input:focus-visible+.track{box-shadow:0 0 0 3px var(--accent-soft)}\n.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}\n.save-state{font-size:11px;font-weight:600;color:var(--fg-4)}\n.save-state.dirty{color:var(--warn)}\n.result{margin-top:14px;border-top:1px solid var(--line);padding-top:12px;min-height:34px}\n.result-line{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:600}\n.result-line .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}\n.result-line.ok{color:var(--ok)} .result-line.ok .dot{background:var(--ok);box-shadow:0 0 0 3px var(--ok-soft)}\n.result-line.bad{color:var(--bad)} .result-line.bad .dot{background:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}\n.result-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}\n.empty{color:var(--fg-4);font-size:12px}\n.stored{display:flex;flex-direction:column;gap:6px;margin-top:auto;padding-top:12px}\n.stored-title{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--fg-4);font-weight:650;\n margin-bottom:2px}\n.stored .row-kv{font-size:12px}\n\n/* timeline */\n.timeline{display:flex;align-items:flex-end;gap:4px;height:132px;padding-top:6px}\n.tl-col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;min-width:0;position:relative;height:100%}\n.tl-ext{background:linear-gradient(180deg,#43d9a3,#2fb383);border-radius:3px 3px 0 0}\n.tl-plan{background:linear-gradient(180deg,#f2b95e,#c98f36);border-radius:3px 3px 0 0}\n.tl-col:hover .tl-ext,.tl-col:hover .tl-plan{filter:brightness(1.15)}\n.tl-labels{display:flex;gap:4px;margin-top:7px}\n.tl-labels span{flex:1;font-size:9.5px;color:var(--fg-4);text-align:center;font-family:var(--mono)}\n.legend{display:flex;gap:16px;font-size:11.5px;color:var(--fg-3);margin-top:10px}\n.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}\n\n/* help */\ndetails{border-top:1px solid var(--line);padding:10px 0}\ndetails:first-of-type{border-top:0}\nsummary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--fg-2);list-style:none;\n display:flex;align-items:center;justify-content:space-between}\nsummary::-webkit-details-marker{display:none}\nsummary::after{content:\"+\";color:var(--fg-4);font-weight:700}\ndetails[open] summary::after{content:\"\u2013\"}\ndetails .card-b{padding-top:10px}\n.code{background:#080d15;border:1px solid var(--line);border-radius:var(--radius-sm);padding:12px 14px;\n font:12px/1.85 var(--mono);color:var(--fg-2);white-space:pre;overflow-x:auto;margin:10px 0 4px}\n\n/* toasts */\n.toasts{position:fixed;right:22px;bottom:22px;z-index:80;display:flex;flex-direction:column;gap:9px}\n.toast{display:flex;align-items:center;gap:10px;min-width:230px;max-width:380px;padding:12px 14px;\n border-radius:var(--radius-sm);border:1px solid var(--line-2);background:#0d1522;color:var(--fg);\n font-size:12.5px;box-shadow:var(--shadow);animation:in .18s ease}\n.toast .dot{width:8px;height:8px;border-radius:50%}\n.toast.ok{border-color:var(--ok-line)} .toast.ok .dot{background:var(--ok)}\n.toast.warn{border-color:var(--warn-line)} .toast.warn .dot{background:var(--warn)}\n.toast.bad{border-color:var(--bad-line)} .toast.bad .dot{background:var(--bad)}\n.toast.out{opacity:0;transform:translateY(6px);transition:.25s ease}\n@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}\n\n.section-title{display:flex;align-items:center;gap:10px;margin:30px 0 14px}\n.section-title h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--fg-4);font-weight:650}\n.section-title .line{flex:1;height:1px;background:var(--line)}\n\n.top-nav{display:flex;align-items:center;gap:4px;margin-left:auto}\n.nav-link{display:inline-flex;align-items:center;height:30px;padding:0 12px;border-radius:9px;\n font-size:12.5px;font-weight:600;color:var(--fg-3);border:1px solid transparent}\n.nav-link:hover{color:var(--fg);background:var(--panel-2);text-decoration:none}\n.nav-link.active{color:var(--fg);background:var(--panel-3);border-color:var(--line-2)}\n@media(max-width:720px){.top-nav{display:none}}\n\n.steps{margin:0;padding-left:20px;color:var(--fg-2);font-size:12.5px;line-height:2}\n.steps li::marker{color:var(--fg-4);font-weight:700}\n.meta-line{display:flex;gap:12px;font-size:12.5px;color:var(--fg-2);padding:4px 0}\n.meta-line .k{min-width:52px;color:var(--fg-4);font-weight:650}\n\n.metrics-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:18px}\n.metrics-strip .m{display:flex;flex-direction:column;gap:3px;min-width:0}\n.metrics-strip .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg-4);font-weight:650}\n.metrics-strip .v{font-size:15px;font-weight:650;font-variant-numeric:tabular-nums;white-space:nowrap;\n overflow:hidden;text-overflow:ellipsis}\n\n.modal-backdrop{position:fixed;inset:0;z-index:90;display:flex;align-items:center;justify-content:center;\n padding:24px;background:rgba(4,7,12,.72);backdrop-filter:blur(4px)}\n.modal{width:min(760px,100%);max-height:min(72vh,760px);display:flex;flex-direction:column;\n background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line-2);\n border-radius:var(--radius);box-shadow:var(--shadow)}\n.modal-h{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;\n padding:16px 18px 13px;border-bottom:1px solid var(--line)}\n.modal-h h3{font-size:13px;font-weight:650}\n.modal-b{padding:14px 18px 18px;overflow:auto;display:flex;flex-direction:column;gap:10px}\n.model-list{display:flex;flex-direction:column;gap:4px;min-height:60px}\n.model-row{display:flex;align-items:baseline;justify-content:space-between;gap:14px;width:100%;\n padding:9px 11px;border-radius:9px;border:1px solid transparent;background:var(--panel);color:var(--fg);\n font-size:12px;cursor:pointer;text-align:left;transition:.14s ease}\n.model-row:hover{border-color:var(--accent-line);background:var(--accent-soft)}\n.model-row .meta{color:var(--fg-4);font-size:11.5px;white-space:nowrap}\n\n.insights{display:flex;flex-direction:column;gap:10px}\n.insight{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--panel)}\n.insight.critical{border-color:var(--bad-line);background:var(--bad-soft)}\n.insight.warn{border-color:var(--warn-line);background:var(--warn-soft)}\n.insight.info{border-color:var(--accent-line);background:var(--accent-soft)}\n.insight-head{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--fg)}\n.insight-head .dot{width:7px;height:7px;border-radius:50%;background:var(--fg-3);flex:0 0 auto}\n.insight.critical .dot{background:var(--bad)}\n.insight.warn .dot{background:var(--warn)}\n.insight.info .dot{background:var(--accent)}\n.insight-head b{font-weight:650}\n.insight-head .btn{margin-left:auto}\n.insight p{margin:6px 0 0;font-size:12.5px;line-height:1.65;color:var(--fg-2)}\n.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}\n"

JS_SHARED = "\nconst TOKEN = \"__DASHBOARD_TOKEN__\";\nconst esc = value => String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\nconst num = value => value === null || value === undefined || value === '' ? '\u2014' : Number(value).toLocaleString('ko-KR');\nconst usd = (value, digits = 3) => value === null || value === undefined ? '\u2014' : '$' + Number(value).toFixed(digits);\nconst tokens = row => row ? row.reduce((sum, value) => sum + value, 0) : 0;\nconst stateText = {external:'OpenRouter \uc0ac\uc6a9 \uc911', plan:'Plan \uc6b0\ud68c \uc911', idle:'\uc720\ud734', unknown:'\uae30\ub85d \uc5c6\uc74c'};\nconst statePill = {external:'pill-ok', plan:'pill-warn', idle:'pill-mute', unknown:'pill-mute'};\nconst PROVIDERS = {\n  openrouter: {title:'OpenRouter', file:'.env.openrouter', fields:[\n    ['OPENROUTER_MODEL','\ubaa8\ub378 ID','text','vendor/model','OpenRouter \ubaa8\ub378 \ubaa9\ub85d\uc758 \uc815\ud655\ud55c ID','\ud544\uc218'],\n    ['ANTHROPIC_AUTH_TOKEN','API \ud0a4','password','sk-or-...','\ube44\uc6cc \ub450\uba74 \uc800\uc7a5\ub41c \ud0a4\ub97c \uc720\uc9c0\ud569\ub2c8\ub2e4','\ud544\uc218']]},\n  local: {title:'Local LLM', file:'.env.local-llm', fields:[\n    ['LOCAL_LLM_URL','Messages URL','text','http://127.0.0.1:11434/v1/messages','Anthropic \ud638\ud658 /v1/messages \uc8fc\uc18c','\ud544\uc218'],\n    ['LOCAL_LLM_MODEL','\ubaa8\ub378 ID','text','\uc124\uce58\ub41c \ubaa8\ub378 ID','\uc11c\ubc84\uc5d0 \uc2e4\uc81c\ub85c \uc788\ub294 \ubaa8\ub378 \uc774\ub984','\ud544\uc218'],\n    ['LOCAL_LLM_API_KEY','\uc11c\ubc84 \ud0a4','password','\uc120\ud0dd \uc0ac\ud56d','\ube44\uc6cc \ub450\uba74 \uc800\uc7a5\ub41c \ud0a4\ub97c \uc720\uc9c0\ud569\ub2c8\ub2e4','\uc120\ud0dd'],\n    ['LOCAL_LLM_ALLOW_REMOTE','\uc6d0\uaca9 HTTPS \ud5c8\uc6a9','switch','','\ub8e8\ud504\ubc31\uc774 \uc544\ub2cc HTTPS \uc11c\ubc84\ub97c \ud5c8\uc6a9\ud569\ub2c8\ub2e4','\uc120\ud0dd'],\n    ['LOCAL_LLM_HTTP_URL','\ub0b4\ubd80 HTTP \uc608\uc678','text','http://host/v1/messages','\uc0ac\ub0b4 \uc11c\ubc84\uc6a9 \uc608\uc678 \uc8fc\uc18c (\uc120\ud0dd)','\uc120\ud0dd'],\n    ['LOCAL_LLM_MODELS_URL','\ubaa8\ub378 \ubaa9\ub85d URL','text','\uae30\ubcf8: /v1/models','\ubaa8\ub378 \ud655\uc778\uc5d0 \uc0ac\uc6a9\ud560 \uac19\uc740 \ucd9c\ucc98 \uc8fc\uc18c','\uc120\ud0dd']]},\n};\nconst ERROR_TEXT = {\n  unknown_provider:'\uc54c \uc218 \uc5c6\ub294 \uc81c\uacf5\uc790', invalid_values:'\uc785\ub825 \ud615\uc2dd \uc624\ub958', invalid_value:'\uac12 \ud615\uc2dd \uc624\ub958',\n  invalid_model:'\ubaa8\ub378 ID \ud615\uc2dd\uc774 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4', invalid_key:'API \ud0a4 \ud615\uc2dd\uc774 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4 (sk-or-...)',\n  missing_anthropic_auth_token:'API \ud0a4\uac00 \ud544\uc694\ud569\ub2c8\ub2e4', missing_openrouter_model:'\ubaa8\ub378 ID\uac00 \ud544\uc694\ud569\ub2c8\ub2e4',\n  missing_local_llm_url:'Messages URL\uc774 \ud544\uc694\ud569\ub2c8\ub2e4', missing_local_llm_model:'\ubaa8\ub378 ID\uac00 \ud544\uc694\ud569\ub2c8\ub2e4',\n  invalid_local_url:'Local URL \uaddc\uce59 \uc704\ubc18 (http(s), /v1/messages, \ub8e8\ud504\ubc31 \ub610\ub294 \ud5c8\uc6a9\ub41c HTTPS)',\n  invalid_local_model:'\ubaa8\ub378 ID\uac00 \ube44\uc5b4 \uc788\uac70\ub098 \uc798\ubabb\ub418\uc5c8\uc2b5\ub2c8\ub2e4', invalid_local_key:'\uc11c\ubc84 \ud0a4\uc5d0 \uc81c\uc5b4 \ubb38\uc790\uac00 \uc788\uc2b5\ub2c8\ub2e4',\n  invalid_allow_remote:'\uc6d0\uaca9 \ud5c8\uc6a9 \uac12\uc740 true/false\uc5ec\uc57c \ud569\ub2c8\ub2e4', invalid_models_url:'\ubaa8\ub378 \ubaa9\ub85d URL\uc774 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4',\n  invalid_configuration:'\uc124\uc815\uc774 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4', write_failed:'\ud30c\uc77c\uc744 \uc4f0\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4',\n  token_required:'\uc138\uc158 \ud1a0\ud070\uc774 \uc5c6\uc2b5\ub2c8\ub2e4 (\ud398\uc774\uc9c0 \uc0c8\ub85c\uace0\uce68 \ud6c4 \ub2e4\uc2dc \uc2dc\ub3c4)', origin_rejected:'\ub2e4\ub978 \ucd9c\ucc98\uc758 \uc694\uccad\uc785\ub2c8\ub2e4',\n  request_too_large:'\uc694\uccad\uc774 \ub108\ubb34 \ud07d\ub2c8\ub2e4', invalid_json:'JSON \ud615\uc2dd \uc624\ub958', invalid_payload:'\uc694\uccad \ud615\uc2dd \uc624\ub958',\n  invalid_mode:'\uc54c \uc218 \uc5c6\ub294 \ud14c\uc2a4\ud2b8 \ubaa8\ub4dc',\n  empty_catalog:'\ubaa8\ub378 \ubaa9\ub85d\uc774 \ube44\uc5b4 \uc788\uc2b5\ub2c8\ub2e4', http_error:'HTTP \uc624\ub958',\n  models_failed:'\ubaa9\ub85d\uc744 \uac00\uc838\uc624\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4', invalid_length:'\uc694\uccad \uae38\uc774 \uc624\ub958',\n};\nconst TEST_TEXT = {\n  ready:'\uc5f0\uacb0\uacfc \ucd94\ub860\uc774 \uc815\uc0c1\uc785\ub2c8\ub2e4', model_found:'\ubaa8\ub378\uc744 \ucc3e\uc558\uc2b5\ub2c8\ub2e4', model_not_found:'\uc124\uc815\ud55c \ubaa8\ub378\uc774 \ubaa9\ub85d\uc5d0 \uc5c6\uc2b5\ub2c8\ub2e4',\n  wrong_model:'\ub2e4\ub978 \ubaa8\ub378\uc774 \uc751\ub2f5\ud588\uc2b5\ub2c8\ub2e4', empty_reply:'\uc751\ub2f5 \ubcf8\ubb38\uc774 \ube44\uc5b4 \uc788\uc2b5\ub2c8\ub2e4', http_error:'HTTP \uc624\ub958',\n  connection_failed:'\uc5f0\uacb0 \uc2e4\ud328', invalid_response:'\uc751\ub2f5 \ud615\uc2dd \uc624\ub958', configuration_missing:'\uc124\uc815\uc774 \uba3c\uc800 \ud544\uc694\ud569\ub2c8\ub2e4',\n  invalid_configuration:'\uc800\uc7a5\ub41c \uc124\uc815\uc774 \uc62c\ubc14\ub974\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4',\n};\n\nlet CONFIG = null, SNAP = null;\nconst DIRTY = new Set(), BUSY = new Set(), RESULTS = {};\n\nfunction toast(kind, text) {\n  const host = document.getElementById('toasts');\n  const node = document.createElement('div');\n  node.className = 'toast ' + kind;\n  node.innerHTML = `<span class=\"dot\"></span><span>${esc(text)}</span>`;\n  host.appendChild(node);\n  setTimeout(() => { node.classList.add('out'); setTimeout(() => node.remove(), 260); }, 3400);\n}\n\nfunction card(title, subtitle, body, span) {\n  return `<section class=\"card ${span || 'col-12'}\">\n    <header class=\"card-h\"><div><h2>${esc(title)}</h2>${subtitle ? `<p class=\"sub\">${esc(subtitle)}</p>` : ''}</div></header>\n    <div class=\"card-b\">${body}</div></section>`;\n}\n\nfunction kv(label, value, mono) {\n  return `<div class=\"row-kv\"><span class=\"k\">${esc(label)}</span><span class=\"v${mono ? ' mono' : ''}\">${value}</span></div>`;\n}\n\nasync function loadConfig() {\n  try {\n    CONFIG = await (await fetch('/api/config', {cache:'no-store'})).json();\n    if (typeof renderSettings === 'function') renderSettings(true);\n  } catch (error) { /* keep the previous view */ }\n}\n\nfunction providerReady(provider) {\n  const fields = (CONFIG && CONFIG[provider] && CONFIG[provider].fields) || {};\n  return PROVIDERS[provider].fields\n    .filter(([, , , , , badge]) => badge === '\ud544\uc218')\n    .every(([name]) => {\n      const field = fields[name] || {};\n      return field.set || field.value;\n    });\n}\n\nwindow.addEventListener('error', event => {\n  const stamp = document.getElementById('stamp');\n  if (stamp) stamp.textContent = '\uc2a4\ud06c\ub9bd\ud2b8 \uc624\ub958: ' + (event.message || 'unknown');\n});\n\ndocument.addEventListener('click', event => {\n  const button = event.target.closest('[data-copy]');\n  if (!button) return;\n  const text = button.dataset.copy;\n  if (navigator.clipboard && navigator.clipboard.writeText) {\n    navigator.clipboard.writeText(text).then(() => toast('ok', '\ubcf5\uc0ac\ud588\uc2b5\ub2c8\ub2e4: ' + text))\n      .catch(() => toast('warn', '\ubcf5\uc0ac\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4: ' + text));\n  } else {\n    toast('warn', '\ubcf5\uc0ac\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4: ' + text);\n  }\n});\n"

JS_DASHBOARD = "/* ---------- dashboard ---------- */\nfunction renderHeader(data) {\n  const routing = data.routing || {};\n  const state = routing.state || 'unknown';\n  const pill = document.getElementById('live-pill');\n  pill.className = 'pill ' + (statePill[state] || 'pill-mute');\n  document.getElementById('live-text').textContent = stateText[state] || state;\n  const age = routing.last && routing.last.age_seconds;\n  const ageText = age === null || age === undefined ? '\uae30\ub85d \uc5c6\uc74c'\n    : age < 60 ? `${Math.round(age)}\ucd08 \uc804` : `${Math.round(age / 60)}\ubd84 \uc804`;\n  const stamp = document.getElementById('stamp');\n  stamp.textContent = data.summary || `${data.model || '\ubaa8\ub378 \ubbf8\uc124\uc815'} \u00b7 \ub9c8\uc9c0\ub9c9 \uacb0\uc815 ${ageText}`;\n  stamp.title = `${data.model || '\ubaa8\ub378 \ubbf8\uc124\uc815'} \u00b7 \ub9c8\uc9c0\ub9c9 \uacb0\uc815 ${ageText} \u00b7 \uac31\uc2e0 ${new Date(data.generated_at).toLocaleTimeString('ko-KR')}`;\n}\n\n\nfunction renderAlerts(data) {\n  const host = document.getElementById('alerts');\n  const alerts = data.alerts || [];\n  const guide = data.configured ? '' :\n    `<div class=\"alert info\"><span class=\"icon\">i</span><div><b>\ucc98\uc74c \uc124\uc815\ud558\uae30</b>\n      <span class=\"txt\">\uc544\ub798 \uc124\uc815\uc5d0\uc11c \ubaa8\ub378\uacfc \ud0a4\ub97c \uc785\ub825\ud558\uace0 <b>\ubaa8\ub378 \ud655\uc778</b> \u2192 <b>\uc800\uc7a5</b>\uc744 \ub204\ub978 \ub4a4\n      <code>OpenClaude.ps1</code>\ub85c \uc138\uc158\uc744 \uc2dc\uc791\ud558\uc138\uc694. \uc800\uc7a5\ud55c \uac12\uc740 \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9\ub429\ub2c8\ub2e4.</span></div></div>`;\n  // An insight with the same code already explains the banner in place, so show it once.\n  const explained = new Set((data.insights || []).map(item => item.code));\n  const shown = alerts.filter(alert => !explained.has(alert.code));\n  host.innerHTML = guide + shown.map(alert => {\n    const icon = alert.level === 'critical' ? '!' : alert.level === 'warn' ? '!' : 'i';\n    const action = alert.command\n      ? `<button class=\"btn btn-sm btn-ghost\" style=\"margin-top:9px\" data-copy=\"${esc(alert.command)}\">\uba85\ub839 \ubcf5\uc0ac</button>`\n      : '';\n    return `<div class=\"alert ${esc(alert.level)}\"><span class=\"icon\">${icon}</span>\n      <div><b>${esc(alert.title)}</b><span class=\"txt\">${esc(alert.detail)}</span>${action}</div></div>`;\n  }).join('');\n}\n\nfunction renderGrid(data) {\n  const routing = data.routing || {}, usage = data.usage || {}, account = data.account || {};\n  const last = routing.last || {};\n  const counts = routing.counts || {external:0, plan:0};\n  const short = routing.short || {minutes:10, counts:{external:0, plan:0}};\n  const total = (counts.external || 0) + (counts.plan || 0);\n  const share = total ? Math.round((counts.external || 0) / total * 100) : 0;\n\n  const statusCard = card('\uc9c0\uae08 \uc0c1\ud0dc', '\ub9c8\uc9c0\ub9c9 \ub77c\uc6b0\ud305 \uacb0\uc815',\n    `<div class=\"metric\"><span class=\"k\">\uacbd\ub85c</span>\n       <span class=\"v\" style=\"font-size:19px\">${esc(stateText[routing.state] || routing.state || '\u2014')}</span></div>\n     <div class=\"metric-list\">\n       ${kv('\uc0ac\uc720', esc([last.reason, last.detail].filter(Boolean).join(' / ') || '\u2014'), true)}\n       ${kv('\ubaa8\ub378', `<span class=\"mono\">${esc(last.model || '\u2014')}</span>`)}\n       ${kv('\ud504\ub85d\uc2dc \uc2dc\uc791', esc(routing.proxy_started_at ? new Date(routing.proxy_started_at).toLocaleString('ko-KR') : '\u2014'))}\n       ${kv('\ud504\ub85d\uc2dc \ucf54\ub4dc', esc(routing.proxy_code ? new Date(routing.proxy_code).toLocaleString('ko-KR') : '\u2014'))}\n       ${kv('\uc18c\uc2a4 \uc218\uc815', esc(data.proxy_modified_at ? new Date(data.proxy_modified_at).toLocaleString('ko-KR') : '\u2014'))}\n       ${data.proxy_stale ? '<div class=\"row-kv\"><span class=\"k\">\ud504\ub85d\uc2dc</span><span class=\"v\" style=\"color:var(--warn)\">\ucf54\ub4dc\ubcf4\ub2e4 \uc624\ub798\ub428 \u00b7 \uc7ac\uc2dc\uc791 \ud544\uc694</span></div>' : ''}\n     </div>`, 'col-3');\n\n  const splitCard = card(`\ucd5c\uadfc ${routing.window_minutes || 60}\ubd84`, '\uc678\ubd80 \ub300 Plan \ube44\uc728',\n    `<div class=\"metric\"><span class=\"k\">OpenRouter</span><span class=\"v\">${share}<small>%</small></span></div>\n     <div class=\"bar\"><i style=\"width:${share}%\"></i></div>\n     <div class=\"metric-list\">\n       ${kv('OpenRouter', num(counts.external) + '\uac74')}\n       ${kv('Plan', num(counts.plan) + '\uac74')}\n       ${kv(`\ucd5c\uadfc ${short.minutes}\ubd84`, `\uc678\ubd80 ${num(short.counts.external)} / Plan ${num(short.counts.plan)}`)}\n       ${kv('\ud68c\ub85c \ucc28\ub2e8', num(routing.circuit_opens) + '\ud68c')}\n     </div>`, 'col-3');\n\n  const limit = account.limit_usd, spent = account.spent_usd, remaining = account.remaining_usd;\n  const used = limit && spent !== null && spent !== undefined ? Math.min(100, Math.round(spent / limit * 100)) : 0;\n  const creditCard = card('\ud06c\ub808\ub527', 'OpenRouter \ud0a4 \ud55c\ub3c4',\n    account.ok ? `<div class=\"metric\"><span class=\"k\">\ub0a8\uc740 \uae08\uc561</span><span class=\"v\">${usd(remaining)}</span></div>\n      <div class=\"bar\"><i style=\"width:${used}%\"></i></div>\n      <div class=\"metric-list\">\n        ${kv('\uc774\ubc88 \ub2ec \uc0ac\uc6a9', usd(spent))}\n        ${kv('\ud55c\ub3c4', usd(limit))}\n        ${kv('\ub9ac\uc14b', esc(account.reset || '\u2014') + (account.resets_at ? ' \u00b7 ' + new Date(account.resets_at).toLocaleDateString('ko-KR') : ''))}\n      </div>`\n    : `<div class=\"metric\"><span class=\"k\">\uc0c1\ud0dc</span><span class=\"v\" style=\"font-size:18px;color:var(--fg-3)\">\ud655\uc778 \ubd88\uac00</span></div>\n       <div class=\"metric-list\">${kv('\uc0ac\uc720', esc(account.error || 'unknown'), true)}</div>`, 'col-3');\n\n  const today = usage.today || {external:[0,0,0,0], plan:[0,0,0,0]};\n  const tokensCard = card('\uc624\ub298 \uc0ac\uc6a9\ub7c9', '\ud1a0\ud070\uacfc \ucd94\uc815 \ube44\uc6a9',\n    `<div class=\"metric\"><span class=\"k\">OpenRouter \ud1a0\ud070</span><span class=\"v\">${num(tokens(today.external))}</span></div>\n     <div class=\"metric-list\">\n       ${kv('Plan \ud1a0\ud070', num(tokens(today.plan)))}\n       ${kv('\uc785\ub825 / \ucd9c\ub825', `${num(today.external[0])} / ${num(today.external[1])}`)}\n       ${kv('\uce90\uc2dc \uc77d\uae30 / \uc0dd\uc131', `${num(today.external[2])} / ${num(today.external[3])}`)}\n       ${kv('\ucd94\uc815 \ube44\uc6a9(\uc624\ub298)', `<span style=\"color:var(--ok)\">${usd(usage.today_cost_usd ?? usage.cost_usd)}</span>`)}\n     </div>`, 'col-3');\n\n  const pm = data.proxy_metrics;\n  const strip = list => `<div class=\"metrics-strip\">${list.map(([key, value]) =>\n    `<div class=\"m\"><span class=\"k\">${esc(key)}</span><span class=\"v\">${value}</span></div>`).join('')}</div>`;\n  const proxyCard = card('\ud504\ub85d\uc2dc \ucc98\ub9ac\ub7c9', pm ? (pm.stale ? `\uac31\uc2e0 ${pm.age_seconds}\ucd08 \uc9c0\uc5f0` : '\uc138\uc158 \uc2e4\ud589\uae30 \uc2e4\uc2dc\uac04') : '\uae30\ub85d \uc5c6\uc74c',\n    pm ? strip([\n      ['\uc694\uccad', num(pm.requests)],\n      ['\ucc98\ub9ac\uc728', `${(pm.requests / Math.max(1, (pm.uptimeMs || 1) / 1000)).toFixed(1)} req/s`],\n      ['\uc678\ubd80 / Plan', `${num(pm.routes?.external || 0)} / ${num(pm.routes?.plan || 0)}`],\n      ['p50 / p95', `${pm.latencyMs?.external?.p50 ?? '\u2014'} / ${pm.latencyMs?.external?.p95 ?? '\u2014'} ms`],\n      ['\uccab \ubc14\uc774\ud2b8 p50 / p95', `${pm.latencyMs?.ttfb?.p50 ?? '\u2014'} / ${pm.latencyMs?.ttfb?.p95 ?? '\u2014'} ms`],\n      ['\uc7ac\uc2dc\ub3c4', num(pm.retries)],\n      ['\uc2a4\ud2b8\ub9bc', num(pm.streams)],\n      ['\ud68c\ub85c', num(pm.circuitOpens)],\n      ['\uc1a1\uc2e0 / \uc218\uc2e0', `${((pm.bytesIn || 0) / 1048576).toFixed(1)} / ${((pm.bytesOut || 0) / 1048576).toFixed(1)} MB`],\n    ]) : '<p class=\"empty\">\uc774 \uc138\uc158\uc5d0\uc11c \ud504\ub85d\uc2dc \uae30\ub85d\uc774 \uc544\uc9c1 \uc5c6\uc2b5\ub2c8\ub2e4. \uc2e4\ud589\uae30\ub97c \uc2dc\uc791\ud558\uba74 \ucc44\uc6cc\uc9d1\ub2c8\ub2e4.</p>', 'col-12');\n  const insights = data.insights || [];\n  const insightRow = item => `<div class=\"insight ${esc(item.level)}\">\n      <div class=\"insight-head\"><span class=\"dot\"></span><b>${esc(item.title)}</b>\n        ${item.action ? `<button class=\"btn btn-sm btn-ghost\" data-copy=\"${esc(item.action)}\">\ubcf5\uc0ac</button>` : ''}</div>\n      <p>${esc(item.detail)}</p>\n      ${item.evidence && item.evidence.length\n        ? `<div class=\"chips\">${item.evidence.map(entry => `<span class=\"chip\">${esc(entry)}</span>`).join('')}</div>`\n        : ''}</div>`;\n  const insightsCard = card('\uc778\uc0ac\uc774\ud2b8', insights.length ? `${insights.length}\uac1c \u00b7 \uc6b0\uc120\uc21c\uc704\uc21c` : '\uc870\uce58\ud560 \ud56d\ubaa9 \uc5c6\uc74c',\n    insights.length ? `<div class=\"insights\">${insights.map(insightRow).join('')}</div>`\n      : '<p class=\"empty\">\uce21\uc815\uac12\uc774 \uc815\uc0c1 \ubc94\uc704\uc785\ub2c8\ub2e4. \uc0c1\ud0dc \uce74\ub4dc\uc758 \uc218\uce58\uac00 \uae30\uc900\uc785\ub2c8\ub2e4.</p>', 'col-12');\n  const reasons = (routing.plan_reasons || []).length\n    ? routing.plan_reasons.map(([code, count]) => {\n        const help = ((data.help || {}).reasons || {})[code];\n        return `<div style=\"padding:9px 0;border-bottom:1px dashed rgba(255,255,255,.05)\">\n          <div class=\"row-kv\"><span class=\"mono\">${esc(code)}</span><span class=\"v\">${num(count)}</span></div>\n          ${help ? `<p class=\"hint\" style=\"margin-top:4px\"><b>${esc(help[0])}</b> \u2014 ${esc(help[1])}</p>` : ''}</div>`;\n      }).join('')\n    : '<p class=\"empty\">Plan \uc6b0\ud68c \uc5c6\uc74c</p>';\n  const failures = (routing.failures || []).length\n    ? routing.failures.map(([code, count]) => kv(code, num(count), true)).join('')\n    : kv('\uc678\ubd80 \uc2e4\ud328', '\uc5c6\uc74c');\n  const reasonsCard = card('Plan \uc6b0\ud68c \uc0ac\uc720', '\ucd5c\uadfc 60\ubd84',\n    reasons + '<div class=\"hairline\"></div>' + failures, 'col-4');\n\n  const timeline = usage.timeline || [];\n  const peak = Math.max(1, ...timeline.map(hour => tokens(hour.external.tokens) + tokens(hour.plan.tokens)));\n  const bars = timeline.map(hour => {\n    const ext = tokens(hour.external.tokens), plan = tokens(hour.plan.tokens);\n    return `<div class=\"tl-col\" title=\"${esc(hour.hour)}\uc2dc \u00b7 OpenRouter ${num(ext)} \u00b7 Plan ${num(plan)}\">\n      <div class=\"tl-plan\" style=\"height:${Math.round(plan / peak * 100)}%\"></div>\n      <div class=\"tl-ext\" style=\"height:${Math.round(ext / peak * 100)}%\"></div></div>`;\n  }).join('');\n  const labels = timeline.map((hour, index) =>\n    `<span>${index % 3 === 0 ? hour.hour.slice(11) + '\uc2dc' : ''}</span>`).join('');\n  const timelineCard = card('\uc2dc\uac04\ub300\ubcc4 \ud1a0\ud070', '\ucd5c\uadfc 24\uc2dc\uac04',\n    `<div class=\"timeline\">${bars || '<p class=\"empty\">\ub370\uc774\ud130 \uc5c6\uc74c</p>'}</div>\n     <div class=\"tl-labels\">${labels}</div>\n     <div class=\"legend\"><span><i style=\"background:#43d9a3\"></i>OpenRouter</span>\n     <span><i style=\"background:#f2b95e\"></i>Plan</span></div>`, 'col-8');\n\n  const transitions = (routing.transitions || []).slice().reverse().map(entry => {\n    const duration = entry.ongoing ? '<span class=\"pill pill-ok\" style=\"height:22px\">\uc9c4\ud589 \uc911</span>'\n      : entry.seconds === null || entry.seconds === undefined ? '\u2014'\n      : entry.seconds < 90 ? `${Math.round(entry.seconds)}\ucd08` : `${Math.round(entry.seconds / 60)}\ubd84`;\n    return `<tr>\n      <td class=\"id\">${new Date(entry.first_ts).toLocaleTimeString('ko-KR')}</td>\n      <td><span class=\"pill ${entry.state === 'external' ? 'pill-ok' : 'pill-warn'}\" style=\"height:24px\">\n        ${esc(stateText[entry.state] || entry.state)}</span></td>\n      <td class=\"id\">${esc([entry.reason, entry.detail].filter(Boolean).join(' / ') || '\u2014')}</td>\n      <td class=\"id\">${esc(entry.model || '\u2014')}</td>\n      <td class=\"num\">${num(entry.count)}</td><td class=\"num\">${duration}</td></tr>`;\n  }).join('');\n  const transitionsCard = card('\ub77c\uc6b0\ud305 \uc804\ud658 \uae30\ub85d', '\ucd5c\uadfc 12\ud68c',\n    (routing.transitions || []).length\n      ? `<table><thead><tr><th>\uc2dc\uc791</th><th>\uacbd\ub85c</th><th>\uc0ac\uc720</th><th>\ubaa8\ub378</th><th class=\"num\">\uac74\uc218</th><th class=\"num\">\uc9c0\uc18d</th></tr></thead><tbody>${transitions}</tbody></table>`\n      : '<p class=\"empty\">\uc544\uc9c1 \uc804\ud658 \uae30\ub85d\uc774 \uc5c6\uc2b5\ub2c8\ub2e4. \uc2e4\ud589\uae30 \uc138\uc158\uc5d0\uc11c \uba54\uc2dc\uc9c0\ub97c \ubcf4\ub0b4\uba74 \uc313\uc785\ub2c8\ub2e4.</p>');\n\n  const sessions = (usage.sessions || []).map(row => `<tr>\n      <td>${esc(row.project)}</td><td class=\"id\">${esc(row.session)}</td>\n      <td class=\"num\">${num(row.turns)}</td><td class=\"num\">${num(row.external)}</td><td class=\"num\">${num(row.plan)}</td>\n      <td class=\"num\">${num(tokens(row.tokens.external))}</td><td class=\"num\">${usd(row.cost_usd, 4)}</td>\n      <td class=\"id\">${esc((row.models[0] || ['\u2014'])[0])}</td></tr>`).join('');\n  const sessionsCard = card('\uc138\uc158\ubcc4 \uc0ac\uc6a9', `${num(usage.session_count)}\uac1c \uc138\uc158 \u00b7 \ucd5c\uadfc \ud65c\ub3d9\uc21c`,\n    (usage.sessions || []).length\n      ? `<table><thead><tr><th>\ud504\ub85c\uc81d\ud2b8</th><th>\uc138\uc158</th><th class=\"num\">\ud134</th><th class=\"num\">\uc678\ubd80</th>\n         <th class=\"num\">Plan</th><th class=\"num\">\uc678\ubd80 \ud1a0\ud070</th><th class=\"num\">\ube44\uc6a9</th><th>\ubaa8\ub378</th></tr></thead>\n         <tbody>${sessions}</tbody></table>`\n      : '<p class=\"empty\">\uc138\uc158 \uae30\ub85d \uc5c6\uc74c</p>');\n\n  const recent = (routing.recent || []).slice().reverse().map(event => `<tr>\n      <td class=\"id\">${new Date(event.ts).toLocaleTimeString('ko-KR')}</td>\n      <td class=\"id\">${esc(event.event || '\u2014')}</td>\n      <td>${esc(event.provider || '\u2014')}</td>\n      <td class=\"id\">${esc([event.reason, event.detail].filter(Boolean).join(' / ') || '\u2014')}</td>\n      <td class=\"num\">${esc(event.status ?? '')}</td></tr>`).join('');\n  const recentCard = card('\ucd5c\uadfc \ub77c\uc6b0\ud305 \uc774\ubca4\ud2b8', '\ucd5c\ub300 15\uac74',\n    (routing.recent || []).length\n      ? `<table><thead><tr><th>\uc2dc\uac01</th><th>\uc774\ubca4\ud2b8</th><th>\uacbd\ub85c</th><th>\uc0ac\uc720</th><th class=\"num\">HTTP</th></tr></thead><tbody>${recent}</tbody></table>`\n      : '<p class=\"empty\">\uc544\uc9c1 \uae30\ub85d\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.</p>');\n\n  document.getElementById('grid').innerHTML =\n    [insightsCard, statusCard, splitCard, creditCard, tokensCard, proxyCard, reasonsCard, timelineCard, transitionsCard, sessionsCard, recentCard].join('');\n}\n\nfunction renderHelp(data) {\n  const help = data.help || {};\n  const commands = (help.commands || []).map(([command, description]) =>\n    `${command.padEnd(52)}# ${description}`).join('\\n');\n  const rows = list => (list || []).map(([left, right]) =>\n    `<div class=\"row-kv\"><span class=\"k mono\">${esc(left)}</span><span class=\"v\">${esc(right)}</span></div>`).join('');\n  const reasons = Object.entries(help.reasons || {}).map(([code, values]) =>\n    `<tr><td class=\"id\">${esc(code)}</td><td>${esc(values[0])}</td><td>${esc(values[1])}</td></tr>`).join('');\n  const body = `<details open><summary>\ud130\ubbf8\ub110 \uba85\ub839</summary>\n      <pre class=\"code\">${esc(commands)}</pre></details>\n    <details><summary>HTTP API</summary><div class=\"card-b\">${rows(help.endpoints)}</div></details>\n    <details><summary>\ub77c\uc6b0\ud305 \uc0ac\uc720 \ucf54\ub4dc ${Object.keys(help.reasons || {}).length}\uac1c</summary>\n      <table><thead><tr><th>\ucf54\ub4dc</th><th>\uc758\ubbf8</th><th>\uc870\uce58</th></tr></thead><tbody>${reasons}</tbody></table></details>\n    <details><summary>\uc0c1\ud0dc \u00b7 \uc885\ub8cc \ucf54\ub4dc \u00b7 \ub370\uc774\ud130 \ucd9c\ucc98</summary>\n      <div class=\"card-b\">\n        <div class=\"row-kv\" style=\"color:var(--fg-4)\"><span class=\"k\">\uc0c1\ud0dc</span><span class=\"v\">\uc758\ubbf8</span></div>\n        ${rows(help.states)}<div class=\"hairline\"></div>\n        <div class=\"row-kv\" style=\"color:var(--fg-4)\"><span class=\"k\">\uc885\ub8cc \ucf54\ub4dc</span><span class=\"v\">\uc758\ubbf8</span></div>\n        ${rows(help.exit_codes)}<div class=\"hairline\"></div>\n        <div class=\"row-kv\" style=\"color:var(--fg-4)\"><span class=\"k\">\ub370\uc774\ud130</span><span class=\"v\">\ucd9c\ucc98</span></div>\n        ${rows(help.sources)}\n      </div></details>`;\n  document.getElementById('help').innerHTML =\n    `<section class=\"card col-12\" id=\"help-card\"><header class=\"card-h\">\n      <div><h2>\ub3c4\uc6c0\ub9d0 \u2014 \ud654\uba74 \uc77d\ub294 \ubc95</h2><p class=\"sub\">\uc0c1\ud0dc \ubc30\uc9c0 \u00b7 \uc0ac\uc720 \ucf54\ub4dc \u00b7 CLI \u00b7 API</p></div></header>\n      <div class=\"card-b\">${body}</div></section>`;\n}\n\nfunction render(data) {\n  SNAP = data;\n  renderHeader(data);\n  renderAlerts(data);\n  renderGrid(data);\n  renderHelp(data);\n}\n\nasync function tick(force) {\n  try {\n    const response = await fetch('/api/snapshot', {cache:'no-store'});\n    const data = await response.json();\n    render(data);\n    if (force) toast('ok', '\uc0c8\ub85c\uace0\uce68\ud588\uc2b5\ub2c8\ub2e4');\n    return data;\n  } catch (error) {\n    document.getElementById('live-text').textContent = '\uc5f0\uacb0 \ub04a\uae40';\n    document.getElementById('live-pill').className = 'pill pill-bad';\n    return null;\n  }\n}\nloadConfig();\nlet pollTimer = null;\nasync function poll() {\n  clearTimeout(pollTimer);\n  const data = await tick();\n  const base = document.hidden ? 15000 : (data && data.refresh_ms ? data.refresh_ms : 5000);\n  pollTimer = setTimeout(poll, Math.max(1000, base));\n}\ndocument.addEventListener('visibilitychange', () => {\n  clearTimeout(pollTimer);\n  if (!document.hidden) poll();\n});\npoll();\n"

JS_SETTINGS = "/* ---------- settings ---------- */\nfunction fieldValue(provider, name) {\n  const fields = (CONFIG && CONFIG[provider] && CONFIG[provider].fields) || {};\n  return fields[name] || {value:'', set:false, preview:'', secret:false};\n}\n\nfunction renderSettings(force) {\n  if (!CONFIG) return;\n  const host = document.getElementById('settings');\n  if (!force && host.contains(document.activeElement)) return;\n  host.innerHTML = Object.entries(PROVIDERS).map(([provider, spec]) => {\n    const rows = spec.fields.map(([name, label, type, placeholder, hint, badge]) => {\n      const field = fieldValue(provider, name);\n      const id = `cfg-${provider}-${name}`;\n      const dirty = DIRTY.has(`${provider}:${name}`);\n      if (type === 'switch') {\n        return `<div class=\"field\"><label>${esc(label)}<span class=\"req\">${esc(badge)}</span></label>\n          <label class=\"switch\"><input type=\"checkbox\" id=\"${id}\" data-provider=\"${provider}\" data-name=\"${name}\"\n            ${field.value === 'true' ? 'checked' : ''}><span class=\"track\"></span>\n            <span>${field.value === 'true' ? '\ud5c8\uc6a9' : '\ud5c8\uc6a9 \uc548 \ud568'}</span></label>\n          <p class=\"hint\">${esc(hint)}</p></div>`;\n      }\n      const value = field.secret ? '' : esc(field.value || '');\n      const effectivePlaceholder = field.secret && field.set\n        ? `\uc800\uc7a5\ub428 (${field.preview}) \u2014 \ubcc0\uacbd\ud560 \ub54c\ub9cc \uc785\ub825` : placeholder;\n      const picker = name.endsWith('_MODEL')\n        ? `<button class=\"btn btn-sm btn-ghost\" type=\"button\" data-picker=\"${provider}\">\ubaa9\ub85d</button>` : '';\n      const reveal = field.secret\n        ? `<button class=\"btn btn-sm btn-ghost\" type=\"button\" data-reveal=\"${id}\">\ubcf4\uae30</button>` : '';\n      return `<div class=\"field${dirty ? ' invalid' : ''}\" data-field=\"${provider}:${name}\">\n        <label for=\"${id}\">${esc(label)}<span class=\"req\">${esc(badge)}</span></label>\n        <div class=\"input-group\"><input class=\"input\" id=\"${id}\" type=\"${type}\" value=\"${value}\"\n          placeholder=\"${esc(effectivePlaceholder)}\" data-provider=\"${provider}\" data-name=\"${name}\"\n          data-secret=\"${field.secret ? '1' : '0'}\" autocomplete=\"off\" spellcheck=\"false\">${reveal}${picker}</div>\n        <p class=\"hint\">${esc(hint)}</p></div>`;\n    }).join('');\n    const dirtyCount = spec.fields.filter(([name]) => DIRTY.has(`${provider}:${name}`)).length;\n    const stored = spec.fields.map(([name, label]) => {\n      const field = fieldValue(provider, name);\n      if (field.secret) {\n        return kv(label, field.set ? `<span class=\"mono\">${esc(field.preview)}</span>`\n                                   : '<span class=\"empty\">\uc124\uc815 \uc548 \ub428</span>');\n      }\n      return kv(label, field.value ? esc(field.value) : '<span class=\"empty\">\uc124\uc815 \uc548 \ub428</span>');\n    }).join('');\n    return `<section class=\"card col-6\" id=\"card-${provider}\">\n      <header class=\"card-h\">\n        <div><h2>${esc(spec.title)}</h2><p class=\"sub\">${esc(spec.file)}</p></div>\n        <span class=\"save-state ${dirtyCount ? 'dirty' : ''}\" id=\"state-${provider}\">\n          ${dirtyCount ? `\uc800\uc7a5\ub418\uc9c0 \uc54a\uc740 \ubcc0\uacbd ${dirtyCount}` : '\uc800\uc7a5\ub428'}</span>\n      </header>\n      <div class=\"card-b\">\n        ${providerReady(provider) ? '' : `<div class=\"notice\"><b>\uc544\uc9c1 \uc124\uc815\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.</b>\n          \uac12\uc744 \uc785\ub825\ud558\uace0 <b>\ubaa8\ub378 \ud655\uc778</b>\uc73c\ub85c \uac80\uc99d\ud55c \ub4a4 <b>\uc800\uc7a5</b>\ud558\uc138\uc694. \ubaa8\ub378 \ud655\uc778\uc740 \uc800\uc7a5 \uc804 \uac12\uc73c\ub85c\ub3c4 \uc2e4\ud589\ub429\ub2c8\ub2e4.</div>`}\n        <div class=\"setting-form\">${rows}</div>\n        <div class=\"actions\">\n          <button class=\"btn btn-primary\" data-busy-key=\"${provider}\" onclick=\"saveSettings('${provider}')\">\uc800\uc7a5</button>\n          <button class=\"btn\" data-busy-key=\"${provider}:metadata\" onclick=\"runTest('${provider}','metadata')\">\ubaa8\ub378 \ud655\uc778</button>\n          <button class=\"btn\" data-busy-key=\"${provider}:inference\" onclick=\"runTest('${provider}','inference')\">\ucd94\ub860 \ud14c\uc2a4\ud2b8</button>\n        </div>\n        <div class=\"result\" id=\"res-${provider}\"></div>\n        <div class=\"hairline\"></div>\n        <div class=\"stored\"><span class=\"stored-title\">\ud604\uc7ac \uc800\uc7a5\ub41c \uac12</span>${stored}</div>\n      </div></section>`;\n  }).join('');\n  wireSettings();\n  Object.keys(PROVIDERS).forEach(provider => renderResult(provider));\n}\n\nfunction wireSettings() {\n  document.querySelectorAll('#settings .input').forEach(input => {\n    input.addEventListener('input', () => {\n      const key = `${input.dataset.provider}:${input.dataset.name}`;\n      const original = fieldValue(input.dataset.provider, input.dataset.name);\n      const current = input.dataset.secret === '1' ? input.value : input.value;\n      const changed = input.dataset.secret === '1' ? current !== '' : current !== (original.value || '');\n      if (changed) DIRTY.add(key); else DIRTY.delete(key);\n      const field = input.closest('.field');\n      if (field) field.classList.toggle('invalid', false);\n      updateSaveState(input.dataset.provider);\n    });\n  });\n  document.querySelectorAll('#settings .switch input').forEach(input => {\n    input.addEventListener('change', () => {\n      DIRTY.add(`${input.dataset.provider}:${input.dataset.name}`);\n      const label = input.parentElement.querySelector('span:last-child');\n      if (label) label.textContent = input.checked ? '\ud5c8\uc6a9' : '\ud5c8\uc6a9 \uc548 \ud568';\n      updateSaveState(input.dataset.provider);\n    });\n  });\n  document.querySelectorAll('#settings [data-picker]').forEach(button => {\n    button.addEventListener('click', () => openModelPicker(button.dataset.picker));\n  });\n  document.querySelectorAll('#settings [data-reveal]').forEach(button => {\n    button.addEventListener('click', () => {\n      const input = document.getElementById(button.dataset.reveal);\n      if (!input) return;\n      const reveal = input.type === 'password';\n      input.type = reveal ? 'text' : 'password';\n      button.textContent = reveal ? '\uc228\uae30\uae30' : '\ubcf4\uae30';\n    });\n  });\n}\n\nfunction updateSaveState(provider) {\n  const spec = PROVIDERS[provider];\n  const count = spec.fields.filter(([name]) => DIRTY.has(`${provider}:${name}`)).length;\n  const node = document.getElementById(`state-${provider}`);\n  if (!node) return;\n  node.textContent = count ? `\uc800\uc7a5\ub418\uc9c0 \uc54a\uc740 \ubcc0\uacbd ${count}` : '\uc800\uc7a5\ub428';\n  node.classList.toggle('dirty', count > 0);\n}\n\nfunction setBusy(key, flag) {\n  if (flag) BUSY.add(key); else BUSY.delete(key);\n  document.querySelectorAll(`[data-busy-key=\"${key}\"]`).forEach(button => {\n    button.disabled = flag;\n    button.classList.toggle('loading', flag);\n  });\n}\n\nfunction collect(provider) {\n  const values = {};\n  for (const [name, , type] of PROVIDERS[provider].fields) {\n    const element = document.getElementById(`cfg-${provider}-${name}`);\n    if (!element) continue;\n    values[name] = type === 'switch' ? (element.checked ? 'true' : 'false') : element.value;\n  }\n  return values;\n}\n\nasync function post(path, payload) {\n  const response = await fetch(path, {method:'POST',\n    headers:{'content-type':'application/json', 'x-dashboard-token':TOKEN},\n    body: JSON.stringify(payload)});\n  return response.json();\n}\n\nasync function saveSettings(provider) {\n  setBusy(provider, true);\n  let result;\n  try { result = await post('/api/config', {provider, values: collect(provider)}); }\n  catch (error) { result = {saved:false, error:'connection_failed'}; }\n  setBusy(provider, false);\n  if (result.saved) {\n    PROVIDERS[provider].fields.forEach(([name]) => DIRTY.delete(`${provider}:${name}`));\n    CONFIG = result.config;\n    RESULTS[provider] = {ok:true, reason:'saved'};\n    renderSettings(true);\n    renderResult(provider, '\uc800\uc7a5\ud588\uc2b5\ub2c8\ub2e4. \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9\ub429\ub2c8\ub2e4.');\n    toast('ok', `${PROVIDERS[provider].title} \uc124\uc815\uc744 \uc800\uc7a5\ud588\uc2b5\ub2c8\ub2e4`);\n  } else {\n    RESULTS[provider] = {error: result.error};\n    renderSettings(true);\n    renderResult(provider);\n    toast('bad', ERROR_TEXT[result.error] || '\uc800\uc7a5\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4');\n  }\n}\n\nasync function runTest(provider, mode) {\n  const key = `${provider}:${mode}`;\n  setBusy(key, true);\n  renderResult(provider, mode === 'inference' ? '\ucd94\ub860\uc744 \ud655\uc778\ud558\ub294 \uc911\u2026' : '\ubaa8\ub378 \ubaa9\ub85d\uc744 \ud655\uc778\ud558\ub294 \uc911\u2026');\n  let result;\n  try { result = await post('/api/test', {provider, mode, values: collect(provider)}); }\n  catch (error) { result = {ok:false, reason:'connection_failed', mode}; }\n  setBusy(key, false);\n  RESULTS[provider] = {...result, mode};\n  renderResult(provider);\n  toast(result.ok ? 'ok' : 'warn',\n    `${PROVIDERS[provider].title} ${mode === 'inference' ? '\ucd94\ub860 \ud14c\uc2a4\ud2b8' : '\ubaa8\ub378 \ud655\uc778'} ${result.ok ? '\uc131\uacf5' : '\uc2e4\ud328'}`);\n}\n\nfunction renderResult(provider, pending) {\n  const box = document.getElementById(`res-${provider}`);\n  if (!box) return;\n  if (pending) { box.innerHTML = `<p class=\"empty\">${esc(pending)}</p>`; return; }\n  const result = RESULTS[provider];\n  if (!result) { box.innerHTML = '<p class=\"empty\">\uc544\uc9c1 \ud14c\uc2a4\ud2b8\ud558\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4. \ubcc0\uacbd \uc0ac\ud56d\uc740 \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9\ub429\ub2c8\ub2e4.</p>'; return; }\n  if (result.reason === 'saved') {\n    box.innerHTML = '<div class=\"result-line ok\"><span class=\"dot\"></span>\uc800\uc7a5 \uc644\ub8cc \u00b7 \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9\ub429\ub2c8\ub2e4</div>';\n    return;\n  }\n  if (result.error) {\n    box.innerHTML = `<div class=\"result-line bad\"><span class=\"dot\"></span>${esc(ERROR_TEXT[result.error] || result.error)}</div>`;\n    return;\n  }\n  const label = result.mode === 'inference' ? '\ucd94\ub860 \ud14c\uc2a4\ud2b8' : '\ubaa8\ub378 \ud655\uc778';\n  const text = TEST_TEXT[result.reason] || result.reason || '\uc54c \uc218 \uc5c6\ub294 \uacb0\uacfc';\n  const chips = [\n    result.model ? `<span class=\"chip\">${esc(result.model)}</span>` : '',\n    result.status ? `<span class=\"chip\">HTTP ${esc(result.status)}</span>` : '',\n    result.model_count !== undefined ? `<span class=\"chip\">\ubaa8\ub378 ${num(result.model_count)}\uac1c</span>` : '',\n  ].filter(Boolean).join('');\n  box.innerHTML = `<div class=\"result-line ${result.ok ? 'ok' : 'bad'}\"><span class=\"dot\"></span>${esc(label)} \u00b7 ${esc(text)}</div>\n    ${chips ? `<div class=\"result-meta\">${chips}</div>` : ''}`;\n}\n\nloadConfig();\n\nconst GUIDE = `<section class=\"card col-12\"><header class=\"card-h\">\n  <div><h2>\uc800\uc7a5\ud55c \uc124\uc815 \uc801\uc6a9\ud558\uae30</h2><p class=\"sub\">\uc124\uc815\uc740 \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9\ub429\ub2c8\ub2e4</p></div></header>\n  <div class=\"card-b\">\n    <ol class=\"steps\">\n      <li>\uac12\uc744 \uc785\ub825\ud558\uace0 <b>\ubaa8\ub378 \ud655\uc778</b>\uc73c\ub85c \uac80\uc99d\ud55c \ub4a4 <b>\uc800\uc7a5</b>\ud569\ub2c8\ub2e4.</li>\n      <li>\uc2e4\ud589 \uc911\uc778 \uc138\uc158\uc744 \uc885\ub8cc\ud558\uace0 <code>OpenClaude.ps1</code>\uc744 \ub2e4\uc2dc \uc2e4\ud589\ud569\ub2c8\ub2e4.</li>\n      <li>Local LLM\uc740 <code>OpenClaude.ps1 -Provider Local</code>\ub85c \uc2dc\uc791\ud569\ub2c8\ub2e4.</li>\n    </ol>\n    <div class=\"hairline\"></div>\n    <div class=\"meta-line\"><span class=\"k\">\ubcf4\uc548</span><span>\ud0a4\ub294 \ube0c\ub77c\uc6b0\uc800\ub85c \ub2e4\uc2dc \uc804\uc1a1\ub418\uc9c0 \uc54a\uace0, \uc800\uc7a5\uc5d0\ub294 \uc138\uc158 \ud1a0\ud070\uc774 \ud544\uc694\ud569\ub2c8\ub2e4.</span></div>\n    <div class=\"meta-line\"><span class=\"k\">\ubc31\uc5c5</span><span>\uc800\uc7a5 \uc804 \ub0b4\uc6a9\uc740 <span class=\"mono\">.env.*.bak</span>\uc5d0 \ub0a8\uc2b5\ub2c8\ub2e4.</span></div>\n    <div class=\"actions\"><a class=\"btn\" href=\"/\">\ub300\uc2dc\ubcf4\ub4dc\ub85c \uc774\ub3d9</a></div>\n  </div></section>`;\n\nasync function openModelPicker(provider) {\n  const spec = PROVIDERS[provider];\n  const modelField = spec.fields.find(([name]) => name.endsWith('_MODEL'))?.[0];\n  const target = modelField ? document.getElementById(`cfg-${provider}-${modelField}`) : null;\n  const modal = document.createElement('div');\n  modal.className = 'modal-backdrop';\n  modal.innerHTML = `<div class=\"modal\" role=\"dialog\" aria-modal=\"true\" aria-label=\"${esc(spec.title)} \ubaa8\ub378 \ubaa9\ub85d\">\n    <header class=\"modal-h\">\n      <div><h3>${esc(spec.title)} \ubaa8\ub378</h3><p class=\"hint\" id=\"picker-note\">\ubd88\ub7ec\uc624\ub294 \uc911\u2026</p></div>\n      <button class=\"btn btn-sm btn-ghost\" type=\"button\" data-close>\ub2eb\uae30</button>\n    </header>\n    <div class=\"modal-b\">\n      <input class=\"input\" id=\"picker-search\" placeholder=\"\ubaa8\ub378 \uac80\uc0c9\u2026\" autocomplete=\"off\" spellcheck=\"false\">\n      <div class=\"model-list\" id=\"picker-list\"><p class=\"empty\">\ubd88\ub7ec\uc624\ub294 \uc911\u2026</p></div>\n    </div></div>`;\n  document.body.appendChild(modal);\n  const close = () => modal.remove();\n  modal.addEventListener('click', event => {\n    if (event.target === modal || event.target.closest('[data-close]')) close();\n  });\n  const onKey = event => {\n    if (event.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }\n  };\n  document.addEventListener('keydown', onKey);\n\n  let models = [];\n  try {\n    const response = await fetch(`/api/models?provider=${provider}`, { headers: { 'x-dashboard-token': TOKEN } });\n    const data = await response.json();\n    models = Array.isArray(data.models) ? data.models : [];\n    document.getElementById('picker-note').textContent = data.ok\n      ? `${models.length}\uac1c \ubaa8\ub378${data.cached ? ' \u00b7 \uce90\uc2dc' : ''}` +\n        (data.age_seconds ? ` \u00b7 ${Math.round(data.age_seconds)}\ucd08 \uc804` : '')\n      : (ERROR_TEXT[data.error] || data.error || '\ubaa9\ub85d\uc744 \uac00\uc838\uc624\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4');\n  } catch (error) {\n    document.getElementById('picker-note').textContent = '\ubaa9\ub85d\uc744 \uac00\uc838\uc624\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4';\n  }\n\n  const list = document.getElementById('picker-list');\n  const renderRows = filter => {\n    const needle = (filter || '').toLowerCase();\n    const rows = models.filter(model => !needle ||\n      model.id.toLowerCase().includes(needle) || model.name.toLowerCase().includes(needle));\n    list.innerHTML = rows.length ? rows.slice(0, 400).map(model => {\n      const meta = [model.context_length ? num(model.context_length) + ' ctx' : '',\n        model.prompt_usd_per_m != null ? '$' + model.prompt_usd_per_m + '/M' : ''].filter(Boolean).join(' \u00b7 ');\n      return `<button type=\"button\" class=\"model-row\" data-id=\"${esc(model.id)}\">\n        <span class=\"mono\">${esc(model.id)}</span><span class=\"meta\">${esc(meta)}</span></button>`;\n    }).join('') : '<p class=\"empty\">\uc77c\uce58\ud558\ub294 \ubaa8\ub378\uc774 \uc5c6\uc2b5\ub2c8\ub2e4</p>';\n  };\n  renderRows('');\n  document.getElementById('picker-search').addEventListener('input', event =>\n    renderRows(event.target.value.trim()));\n  list.addEventListener('click', event => {\n    const row = event.target.closest('.model-row');\n    if (!row || !target) return;\n    target.value = row.dataset.id;\n    target.dispatchEvent(new Event('input', { bubbles: true }));\n    close();\n    document.removeEventListener('keydown', onKey);\n    toast('ok', row.dataset.id + ' \uc120\ud0dd \u00b7 \uc800\uc7a5\uc744 \ub20c\ub7ec \uc801\uc6a9');\n  });\n}\n\ndocument.getElementById('guide').innerHTML = GUIDE;\ndocument.getElementById('stamp').textContent = '\ubaa8\ub378\u00b7\ud0a4 \uc124\uc815\uacfc \uc5f0\uacb0 \ud655\uc778 \u00b7 \uc800\uc7a5\uc740 \uc0c8 \uc138\uc158\ubd80\ud130 \uc801\uc6a9';\nloadConfig();\n"

DASHBOARD_BODY = "\n  <div class=\"alerts\" id=\"alerts\"></div>\n\n  <div class=\"section-title\"><h2>\uc0c1\ud0dc</h2><span class=\"line\"></span></div>\n  <div class=\"grid\" id=\"grid\"></div>\n\n  <div class=\"section-title\"><h2>\ub3c4\uc6c0\ub9d0</h2><span class=\"line\"></span></div>\n  <div class=\"grid\" id=\"help\"></div>\n"

SETTINGS_BODY = "\n  <div class=\"alerts\" id=\"alerts\"></div>\n\n  <div class=\"section-title\"><h2>\uc124\uc815</h2><span class=\"line\"></span></div>\n  <div class=\"grid\" id=\"settings\"></div>\n\n  <div class=\"section-title\"><h2>\uc801\uc6a9 \ubc29\ubc95</h2><span class=\"line\"></span></div>\n  <div class=\"grid\" id=\"guide\"></div>\n"

def _page(kind="dashboard"):
    dashboard = kind == "dashboard"
    title = "OpenRouter 라우팅 콘솔" if dashboard else "OpenRouter 설정"
    nav = ('<a class="nav-link active" href="/">대시보드</a>'
           '<a class="nav-link" href="/settings">설정</a>' if dashboard else
           '<a class="nav-link" href="/">대시보드</a>'
           '<a class="nav-link active" href="/settings">설정</a>')
    actions = ('<span class="pill pill-mute" id="live-pill"><span class="dot"></span>'
               '<span id="live-text">확인 중</span></span>'
               '<button class="btn btn-sm" onclick="tick(true)">새로고침</button>'
               '<a class="btn btn-sm btn-ghost" href="#help">도움말</a>' if dashboard else
               '<button class="btn btn-sm" onclick="loadConfig(true)">다시 불러오기</button>')
    body = DASHBOARD_BODY if dashboard else SETTINGS_BODY
    script = JS_SHARED + (JS_DASHBOARD if dashboard else JS_SETTINGS)
    return ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<title>' + title + '</title>\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<style>' + PAGE_CSS + '</style></head><body>\n'
            '<header class="topbar"><div class="wrap topbar-in">\n'
            '  <div class="brand"><span class="logo"></span><div style="min-width:0">\n'
            '    <h1>' + title + '</h1><p id="stamp">불러오는 중…</p></div></div>\n'
            '  <nav class="top-nav">' + nav + '</nav>\n'
            '  <div class="top-actions">' + actions + '</div>\n'
            '</div></header>\n'
            '<main class="wrap">' + body + '</main>\n'
            '<div class="toasts" id="toasts"></div>\n'
            '<script>' + script + '</script></body></html>')


PAGE = _page("dashboard")
SETTINGS_PAGE = _page("settings")





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
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dashboard"):
            self._send(200, "text/html; charset=utf-8", PAGE.replace("__DASHBOARD_TOKEN__", TOKEN))
        elif path == "/settings":
            self._send(200, "text/html; charset=utf-8", SETTINGS_PAGE.replace("__DASHBOARD_TOKEN__", TOKEN))
        elif path == "/api/config":
            try:
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(config_snapshot(), ensure_ascii=False))
            except Exception:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": "config_failed"}))
        elif path == "/api/models":
            if not self._authorized():
                return self._send(403, "application/json; charset=utf-8",
                                  json.dumps({"error": "token_required"}))
            query = parse_qs(urlsplit(self.path).query)
            provider = (query.get("provider") or ["openrouter"])[0]
            refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            if provider not in CONFIG_FILES:
                return self._send(400, "application/json; charset=utf-8",
                                  json.dumps({"error": "unknown_provider"}))
            try:
                catalog = model_catalog(provider, refresh=refresh)
            except Exception:
                return self._send(500, "application/json; charset=utf-8",
                                  json.dumps({"error": "models_failed"}))
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps(catalog, ensure_ascii=False))
        elif path == "/api/snapshot":
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
        bound = self.server.server_address[0]
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                           f"http://{_display_host(bound)}:{port}"}
        if origin and origin not in allowed_origins:
            return self._send(403, "application/json; charset=utf-8", json.dumps({"error": "origin_rejected"}))
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._send(400, "application/json; charset=utf-8", json.dumps({"error": "invalid_length"}))
        if length < 0:
            # A negative length would make read() wait for EOF and hang the handler.
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


def _display_host(host):
    """Bracket IPv6 literals so a URL or an Origin comparison stays valid."""
    host = str(host or "")
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


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
    url = f"http://{_display_host(host)}:{server.server_address[1]}/"
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
    insights = build_insights(routing, usage, account, pricing)
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
        "insights": insights,
        "summary": summary_line(routing, usage, account, insights),
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
        if not isinstance(record.get("port"), int) or not _port_open(record["port"]):
            # The recorded instance is gone (crash or reboot): drop the stale record instead
            # of signalling a PID that may now belong to an unrelated process.
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            print(json.dumps({"stopped": False, "reason": "stale_record", "pid": record.get("pid")}))
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
