"""Bounded, opt-in local diagnostic recording; never expose recorded content through status."""
import datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid

import routeConfig

MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 256
MAX_CONTENT = 64 * 1024
_LOCK = threading.RLock()
_SAFE_CODE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SECRET = re.compile(r"(?i)(\bBearer\s+|[\"']?\b[A-Za-z0-9_.-]*(?:api[_-]?key|authorization|password|secret|token)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)|\b(?:sk-(?:or-v1-)?|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}")
_QUOTED_SECRET = re.compile(r"(?i)([\"']?\b[A-Za-z0-9_.-]*(?:api[_-]?key|authorization|password|secret|token)[\"']?\s*[:=]\s*)([\"'])(?:\\.|(?!\2).)*?\2")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)")


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _redact(value):
    # Pattern-based filtering is not a guarantee: detailed recordings remain sensitive.
    value = _PRIVATE_KEY.sub("[redacted:private-key]", value)
    value = _QUOTED_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[redacted]" + match.group(2), value)
    return _SECRET.sub(lambda match: (match.group(1) or "") + "[redacted]", value)


def _redact_records(chunk):
    """Parse complete JSONL before filtering so JSON-escaped nested text is not missed."""
    def clean(value):
        if isinstance(value, dict):
            return {key: ("[redacted]" if isinstance(key, str) and re.search(
                r"(?i)(api[_-]?key|authorization|password|secret|token|credential|private[_-]?key)", key)
                else clean(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return _redact(value) if isinstance(value, str) else value

    records, dropped = [], 0
    for line in chunk.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
            records.append(json.dumps(clean(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            dropped += 1
    return ("\n".join(records) + "\n" if records else ""), dropped


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _code(value):
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) and not re.search(
        r"(?i)(?:^|[-_])(secret|token|password|bearer|sk)(?:[-_]|$)", value) else None


def _snapshot(value):
    """Copy only fixed diagnostic fields; never copy arbitrary body, path or exception text."""
    value = value if isinstance(value, dict) else {}
    session = value.get("session") if isinstance(value.get("session"), dict) else {}
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
    route = value.get("last_route") if isinstance(value.get("last_route"), dict) else {}
    config = value.get("configuration") if isinstance(value.get("configuration"), dict) else {}
    checks = value.get("checks") if isinstance(value.get("checks"), list) else []
    def number_fields(name, fields):
        row = metrics.get(name)
        return {key: _count(row.get(key)) for key in fields} if isinstance(row, dict) else None
    def timing(name):
        row = metrics.get(name)
        if not isinstance(row, dict):
            return None
        values = {key: (v if type(v := row.get(key)) in (int, float) and math.isfinite(v) and 0 <= v <= 180000 else None)
                  for key in ("p50", "p95", "max")}
        return {"count": _count(row.get("count")), **values}
    return {
        "source_scope": "dashboard_proxy_session_not_verified_as_captured_claude_session",
        "provider": session.get("provider") if session.get("provider") in ("local", "openrouter", "cli", "sonnet") else None,
        "session_active": session.get("active") if type(session.get("active")) is bool else None,
        "next_provider": config.get("next_provider") if config.get("next_provider") in ("local", "openrouter", "cli", "sonnet") else None,
        "configured_model": _code(config.get("configured_model")),
        "requested_model": _code(session.get("requested_model")),
        "reported_model": _code(session.get("loaded_model")),
        "requests": _count(metrics.get("requests")), "failed": _count(metrics.get("failed")),
        "retries": _count(metrics.get("retries")), "circuit_opens": _count(metrics.get("circuitOpens")),
        "attempts": number_fields("attemptCounts", ("external", "plan")),
        "cli_activity": number_fields("cliActivity", ("active", "limit", "queued", "rejected")),
        "cli_failures": number_fields("cliFailures", ("timeout", "status429", "status4xx", "status5xx", "validation", "connection")),
        "cli_usage": number_fields("cliUsage", ("responses", "inputSamples", "outputSamples", "inputTokens", "outputTokens")),
        "cli_process_ms": timing("cliProcessMs"),
        "last_provider": route.get("provider") if route.get("provider") in ("local", "openrouter", "cli", "plan") else None,
        "last_current_session": route.get("current_session") if type(route.get("current_session")) is bool else None,
        "checks": [{"component": _code(row.get("component")), "status": _code(row.get("status")), "code": _code(row.get("code"))}
                   for row in checks[:30] if isinstance(row, dict)],
    }


def _normal_path(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _opened_path(handle):
    """Resolve the opened handle, not an attacker-replaceable pathname."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
        function.restype = ctypes.c_ulong
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ValueError("transcript_unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return _normal_path(value)
    proc = Path("/proc/self/fd") / str(handle.fileno())
    if not proc.exists():
        raise ValueError("transcript_unavailable")
    return _normal_path(os.path.realpath(proc))


def _open_transcript(root, path, session_id):
    root, path = Path(root), Path(path)
    if not root.is_absolute() or not path.is_absolute() or path.name != session_id + ".jsonl":
        raise ValueError("transcript_unavailable")
    try:
        relative = path.relative_to(root)
        if len(relative.parts) < 2 or any(part in (".", "..") for part in relative.parts):
            raise ValueError("transcript_unavailable")
        # Reject linked directories and files. The open handle is verified again below.
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(current, "is_junction", lambda: False)():
                raise ValueError("transcript_unavailable")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("transcript_unavailable")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        handle = os.fdopen(descriptor, "rb")
        try:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not opened.st_ino
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or _opened_path(handle) != _normal_path(path)):
                raise ValueError("transcript_unavailable")
            return handle, opened
        except BaseException:
            handle.close()
            raise
    except (OSError, RuntimeError):
        raise ValueError("transcript_unavailable") from None


def _pending_transcript(root, path, session_id):
    """Accept a missing new-session file only beneath unlinked existing parents."""
    root, path = Path(root), Path(path)
    if not root.is_absolute() or not path.is_absolute() or path.name != session_id + ".jsonl":
        raise ValueError("transcript_unavailable")
    try:
        relative = path.relative_to(root)
        if len(relative.parts) < 2 or any(part in (".", "..") for part in relative.parts):
            raise ValueError("transcript_unavailable")
        current = root
        missing = False
        for part in (None, *relative.parts[:-1]):
            if part is not None:
                current = current / part
            if missing:
                continue
            try:
                info = current.lstat()
            except FileNotFoundError:
                # Claude may create its projects tree after emitting SessionStart. The
                # complete directory chain is revalidated on the opened file later.
                missing = True
                continue
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(current, "is_junction", lambda: False)():
                raise ValueError("transcript_unavailable")
        try:
            path.lstat()
        except FileNotFoundError:
            return
        raise ValueError("transcript_unavailable")
    except (OSError, RuntimeError):
        raise ValueError("transcript_unavailable") from None


class Blackbox:
    """One bounded recording store under the selected application root."""
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / ".blackbox"
        self.path = self.directory / "recording.json"

    def _prepare(self):
        if self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)():
            raise ValueError("unsafe_store")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.directory / ".recording.json.lock"
        for path in (self.path, lock):
            if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
                raise ValueError("unsafe_store")

    def _read(self):
        try:
            if self.path.is_symlink():
                raise ValueError("invalid_store")
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise ValueError("invalid_store")
                data = handle.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("invalid_store")
            value = json.loads(data)
        except FileNotFoundError:
            return {"version": 1, "active": False, "mode": None, "session_id": None, "started_at": None, "events": []}
        except (OSError, UnicodeError, ValueError):
            raise ValueError("invalid_store") from None
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("events"), list):
            raise ValueError("invalid_store")
        return value

    def _write(self, value):
        overflow = max(0, len(value["events"]) - MAX_EVENTS)
        value["events"] = value["events"][-MAX_EVENTS:]
        value["discarded"] = (_count(value.get("discarded")) or 0) + overflow
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        while len(raw) > MAX_BYTES and len(value["events"]) > 1:
            value["events"].pop(0)
            value["discarded"] = _count(value.get("discarded")) or 0
            value["discarded"] += 1
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("record_too_large")
        descriptor, tmp = tempfile.mkstemp(prefix=".recording-", suffix=".tmp", dir=str(self.directory))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    @staticmethod
    def _status(value):
        events = [row for row in value.get("events", []) if isinstance(row, dict) and row.get("session_id") == value.get("session_id")]
        detail_status = value.get("detail_status")
        capture = value.get("capture") if isinstance(value.get("capture"), dict) else {}
        expires = capture.get("expires_at")
        if detail_status == "waiting" and type(expires) in (int, float) and time.time() > expires:
            detail_status = "unavailable"
        return {"active": value.get("active") is True,
                "mode": value.get("mode") if value.get("mode") in ("metadata", "detailed") else None,
                "session_id": value.get("session_id") if isinstance(value.get("session_id"), str) and re.fullmatch(r"[a-f0-9]{32}", value["session_id"]) else None,
                "started_at": value.get("started_at") if isinstance(value.get("started_at"), str) and re.fullmatch(r"[0-9T:.+Z-]{1,40}", value["started_at"]) else None,
                "incidents": sum(1 for row in events if isinstance(row, dict) and row.get("event") == "incident"),
                "details_available": any(isinstance(row, dict) and row.get("content") for row in events),
                "detail_status": detail_status if detail_status in ("waiting", "connected", "unavailable", "not_requested") else "not_requested",
                "discarded": _count(value.get("discarded")) or 0}

    def _with_lock(self, operation):
        with _LOCK:
            self._prepare()
            with routeConfig.Store(self.path)._cross_process_lock():
                value = self._read()
                result = operation(value)
                if result is not None:
                    self._write(value)
                return self._status(value)

    def status(self):
        # Reads do not create a directory or lock file. Atomic replace gives a whole document.
        with _LOCK:
            if self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)():
                raise ValueError("unsafe_store")
            return self._status(self._read())

    def start(self, mode, snapshot):
        if mode not in ("metadata", "detailed"):
            raise ValueError("invalid_mode")
        def apply(value):
            if value.get("active"):
                raise ValueError("already_active")
            value.update(active=True, mode=mode, session_id=uuid.uuid4().hex, started_at=_stamp())
            value["detail_status"] = "waiting" if mode == "detailed" else "not_requested"
            value.pop("capture", None)
            value["events"].append({"event": "started", "at": value["started_at"],
                                    "session_id": value["session_id"], "snapshot": _snapshot(snapshot)})
            return True
        return self._with_lock(apply)

    def prepare_launch(self, expected_session_id, projects_root):
        """Issue a one-use hook token for a known, newly launched Claude session only."""
        try:
            session_id = str(uuid.UUID(expected_session_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid_session") from None
        if expected_session_id != session_id or not Path(projects_root).is_absolute():
            raise ValueError("invalid_session")
        issued = []
        def apply(value):
            if value.get("active") is not True or value.get("mode") != "detailed":
                return None
            capture = value.get("capture")
            # A connected session owns this recording. Pending registrations may be retried
            # only after expiration or a failed registration; never replace a live owner.
            if capture and self._status(value)["detail_status"] != "unavailable":
                return None
            token = secrets.token_urlsafe(32)
            value["capture"] = {"expected_session_id": session_id, "projects_root": str(projects_root),
                                "token_hash": hashlib.sha256(token.encode("ascii")).hexdigest(), "expires_at": time.time() + 120}
            value["detail_status"] = "waiting"
            issued.append(token)
            return True
        self._with_lock(apply)
        return issued[0] if issued else None

    def register_launch(self, token, event):
        """Register one authenticated SessionStart and snapshot EOF, without reading content."""
        if not isinstance(token, str) or not token.isascii() or len(token) > 128 or not isinstance(event, dict):
            raise ValueError("invalid_registration")
        def apply(value):
            capture = value.get("capture")
            if value.get("active") is not True or value.get("mode") != "detailed" or not isinstance(capture, dict):
                raise ValueError("invalid_registration")
            expected = capture.get("expected_session_id")
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            if (not isinstance(capture.get("token_hash"), str) or not hmac.compare_digest(digest, capture["token_hash"])
                    or type(capture.get("expires_at")) not in (int, float) or time.time() > capture["expires_at"]
                    or event.get("session_id") != expected or not isinstance(event.get("transcript_path"), str)):
                raise ValueError("invalid_registration")
            try:
                path = Path(event["transcript_path"])
                registered_ns = time.time_ns()
                try:
                    path.lstat()
                except FileNotFoundError:
                    _pending_transcript(capture["projects_root"], path, expected)
                    capture.update(path=str(path), offset=0, pending=True, registered_ns=registered_ns, skip_partial=False)
                    capture.pop("token_hash", None)
                    capture.pop("expires_at", None)
                    value["detail_status"] = "waiting"
                    return True
                handle, info = _open_transcript(capture["projects_root"], path, expected)
                with handle:
                    partial = False
                    if info.st_size:
                        handle.seek(info.st_size - 1)
                        partial = handle.read(1) != b"\n"
            except ValueError:
                # Never guess or retry a potentially unrelated session.
                value["detail_status"] = "unavailable"
                capture.pop("token_hash", None)
                return True
            capture.update(path=event["transcript_path"], offset=info.st_size, device=info.st_dev, inode=info.st_ino,
                           pending=False, skip_partial=partial)
            capture.pop("token_hash", None)
            capture.pop("expires_at", None)
            value["detail_status"] = "connected"
            return True
        return self._with_lock(apply)

    def mark(self, snapshot, transcript=None):
        def apply(value):
            if value.get("active") is not True:
                raise ValueError("not_active")
            if value.get("detail_status") == "waiting" and self._status(value)["detail_status"] == "unavailable":
                value["detail_status"] = "unavailable"
            event = {"event": "incident", "at": _stamp(), "session_id": value["session_id"],
                     "snapshot": _snapshot(snapshot)}
            capture = value.get("capture")
            if transcript is None and value.get("mode") == "detailed" and value.get("detail_status") in ("connected", "waiting") and isinstance(capture, dict) and capture.get("path"):
                try:
                    if capture.get("pending"):
                        try:
                            Path(capture["path"]).lstat()
                        except FileNotFoundError:
                            value["events"].append(event)
                            return True
                    handle, info = _open_transcript(capture["projects_root"], capture["path"], capture["expected_session_id"])
                    with handle:
                        if capture.get("pending"):
                            birth_ns = getattr(info, "st_birthtime_ns", None)
                            if type(birth_ns) is not int or birth_ns < capture.get("registered_ns", time.time_ns()):
                                raise ValueError("transcript_unavailable")
                            capture.update(device=info.st_dev, inode=info.st_ino, pending=False)
                            value["detail_status"] = "connected"
                        if (info.st_dev, info.st_ino) != (capture.get("device"), capture.get("inode")) or info.st_size < capture.get("offset", 0):
                            raise ValueError("transcript_unavailable")
                        start = max(capture["offset"], info.st_size - MAX_CONTENT)
                        omitted = start > capture["offset"]
                        handle.seek(start)
                        chunk = handle.read(info.st_size - start)
                        if len(chunk) != info.st_size - start:
                            raise ValueError("transcript_unavailable")
                    # Capture only complete JSONL records: never retain a split token/value.
                    prefix = 0
                    if omitted:
                        event["content_truncated"] = True
                        event["content_omission"] = "record_limit"
                    if omitted or capture.get("skip_partial"):
                        end = chunk.find(b"\n")
                        if end < 0:
                            if chunk:
                                event["content_truncated"] = True
                                event["content_omission"] = "incomplete_or_oversized_record"
                            capture["offset"] = info.st_size
                            capture["skip_partial"] = True
                            chunk = b""
                        else:
                            prefix = end + 1
                            chunk = chunk[prefix:]
                            capture["skip_partial"] = False
                    if chunk:
                        last = chunk.rfind(b"\n")
                        if last < 0:
                            event["content_truncated"] = True
                            event["content_omission"] = "incomplete_record_pending"
                            capture["offset"] = start + prefix
                            chunk = b""
                        else:
                            chunk = chunk[:last + 1]
                            capture["offset"] = start + prefix + len(chunk)
                    elif not capture.get("skip_partial"):
                        capture["offset"] = start + prefix
                    if chunk:
                        content, dropped = _redact_records(chunk)
                        if content:
                            event["content"] = content
                        event["content_truncated"] = event.get("content_truncated", False) or dropped > 0
                        if dropped:
                            event["content_omission"] = "invalid_record"
                        event["dropped_invalid_records"] = dropped
                except (OSError, ValueError, KeyError, TypeError):
                    value["detail_status"] = "unavailable"
            if transcript is not None:
                if value.get("mode") != "detailed" or not isinstance(transcript, dict) or transcript.get("session_id") != value["session_id"] or not isinstance(transcript.get("text"), str):
                    raise ValueError("invalid_transcript")
                # Include extra trailing context for redaction before truncating the record.
                event["content"] = _redact(transcript["text"][:MAX_CONTENT + 1024])[:MAX_CONTENT]
                event["content_truncated"] = len(transcript["text"]) > MAX_CONTENT
            value["events"].append(event)
            return True
        return self._with_lock(apply)

    def stop(self):
        def apply(value):
            if value.get("active") is not True:
                raise ValueError("not_active")
            value["events"].append({"event": "stopped", "at": _stamp(), "session_id": value["session_id"]})
            value["active"] = False
            value.pop("capture", None)
            return True
        return self._with_lock(apply)
