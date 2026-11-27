# SPDX-License-Identifier: MIT
"""Local, idempotent request usage ledger. No prompts or tool content are stored."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
import weakref

SCHEMA_VERSION = 3
MAX_BATCH = 512
MAX_ROWS = 5000
MAX_NUMBER = 2**53 - 1
MAX_TOKENS = 2**31 - 1
MAX_DISCOVERY = 1024
MAX_SCAN_ENTRIES = 4096
MAX_COLLECT_FILES = 32
MAX_COLLECT_BYTES = 256 * 1024
MAX_FILE_CHUNK = 64 * 1024
MODEL_RE = re.compile(r"^[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}$")
PROVIDERS = frozenset(("plan", "local", "openrouter", "cli", "unknown"))
STATUSES = frozenset(("ok", "failed", "aborted"))
EVENT_KEYS = frozenset(("instance_id", "event_seq", "timestamp", "provider", "requested_model",
                        "reported_model", "usage_measured", "input_tokens", "output_tokens",
                        "status", "elapsed_ms"))
# Schema v3 adds prompt-cache token fields; they stay optional so a v1/v2 producer
# keeps ingesting unchanged, and NULL means "not reported by that producer".
CACHE_KEYS = frozenset(("cache_read_tokens", "cache_creation_tokens"))


class LedgerError(ValueError):
    """A fixed, non-sensitive error code; no input or filesystem paths are exposed."""


def _instance(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return uuid.UUID(value).hex
    except (ValueError, AttributeError):
        raise LedgerError("invalid_instance") from None


def _model(value):
    if value is None:
        return None
    if (not isinstance(value, str) or not MODEL_RE.fullmatch(value)
            or re.match(r"(?i)^(sk-|xai-|ghp_|github_pat_|bearer)", value)
            or re.search(r"(?i)(?:^|/)(?:gsk_[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})(?:$|/)", value)
            or re.search(r"(?i)AKIA[0-9A-Z]{16}", value)
            or re.search(r"^[A-Za-z]:|://|\\", value)):
        raise LedgerError("invalid_model")
    return value


def _number(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= MAX_NUMBER:
        raise LedgerError("invalid_number")
    return value


def _event(instance, value):
    if not isinstance(value, dict) or (set(value) != EVENT_KEYS and
                                      set(value) != EVENT_KEYS | {"schema_version"} and
                                      set(value) != EVENT_KEYS | CACHE_KEYS and
                                      set(value) != EVENT_KEYS | CACHE_KEYS | {"schema_version"}):
        raise LedgerError("invalid_event")
    version = value.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2, SCHEMA_VERSION):
        raise LedgerError("invalid_event")
    if _instance(value["instance_id"]) != instance:
        raise LedgerError("instance_mismatch")
    seq = _number(value["event_seq"])
    if not seq:
        raise LedgerError("invalid_number")
    raw_stamp = value["timestamp"]
    try:
        if not isinstance(raw_stamp, str) or len(raw_stamp) > 40:
            raise ValueError()
        stamp = dt.datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        timestamp = stamp.astimezone(dt.timezone.utc).timestamp()
        if not math.isfinite(timestamp) or timestamp < 0 or timestamp > 253402300799:
            raise ValueError()
    except (ValueError, OverflowError, OSError):
        raise LedgerError("invalid_timestamp") from None
    if not isinstance(value["provider"], str) or value["provider"] not in PROVIDERS or not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise LedgerError("invalid_event")
    measured = value["usage_measured"]
    if type(measured) is not bool:
        raise LedgerError("invalid_event")
    incoming = _number(value["input_tokens"], nullable=True)
    outgoing = _number(value["output_tokens"], nullable=True)
    if any(v is not None and v > MAX_TOKENS for v in (incoming, outgoing)):
        raise LedgerError("invalid_number")
    if (not measured and (incoming is not None or outgoing is not None)) or (measured and incoming is None and outgoing is None):
        raise LedgerError("invalid_event")
    cache_read = _number(value.get("cache_read_tokens"), nullable=True)
    cache_write = _number(value.get("cache_creation_tokens"), nullable=True)
    if any(v is not None and v > MAX_TOKENS for v in (cache_read, cache_write)):
        raise LedgerError("invalid_number")
    elapsed = value["elapsed_ms"]
    if type(elapsed) not in (int, float) or not 0 <= elapsed <= MAX_NUMBER or not math.isfinite(elapsed):
        raise LedgerError("invalid_number")
    return (instance, seq, timestamp, value["provider"], _model(value["requested_model"]),
            _model(value["reported_model"]), int(measured), incoming, outgoing, value["status"], elapsed,
            version, cache_read, cache_write)


class _DirectoryScan:
    """Retain at most one physical page and its open iterator for a workspace."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stream = None
        self.position = 0
        self.page = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.position = 0
        self.page = None

    def __del__(self):
        self.close()

    def read(self, root, offset, result):
        with self.lock:
            if self.page:
                start, entries, end, more = self.page
                if start < offset < end:
                    return [row for row in entries if row[0] > offset], more, end
            if self.stream is None or self.position > offset or not offset:
                self.close()
                self.stream = os.scandir(root)
            entries = []
            try:
                for _ in range(MAX_SCAN_ENTRIES):
                    entry = next(self.stream)
                    self.position += 1
                    # Restoring a checkpoint after a process restart also consumes
                    # the physical enumeration budget, across as many calls as needed.
                    if self.position <= offset:
                        continue
                    try:
                        identity = _instance(entry.name)
                    except LedgerError:
                        continue
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False) or getattr(Path(entry.path), "is_junction", lambda: False)():
                        if "unsafe_source" not in result["errors"]:
                            result["errors"].append("unsafe_source")
                        continue
                    path = Path(entry.path) / "events.jsonl"
                    if path.exists() or path.is_symlink():
                        entries.append((self.position, identity, path))
                        if len(entries) >= MAX_DISCOVERY:
                            break
                more = max(offset, self.position)
            except StopIteration:
                more = 0
                self.stream.close()
                self.stream = None
            except OSError:
                self.close()
                raise
            self.page = (offset, entries, self.position, more)
            return entries, more, self.position


_scans = weakref.WeakValueDictionary()
_scans_lock = threading.Lock()


class UsageLedger:
    """A workspace-local ledger. No retention deletion is performed."""

    def __init__(self, root):
        self.directory = Path(root) / ".usage-stats"
        self.path = self.directory / "usage.sqlite3"
        with _scans_lock:
            self._scan = _scans.get(self.directory)
            if self._scan is None:
                self._scan = _scans[self.directory] = _DirectoryScan()

    def close_scan(self):
        with self._scan.lock:
            self._scan.close()

    def _safe_path(self, *, create):
        if self.directory.is_symlink() or getattr(self.directory, "is_junction", lambda: False)():
            raise LedgerError("unsafe_store")
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1)):
            raise LedgerError("unsafe_store")

    def _connect(self, create=False):
        deadline = time.monotonic() + 0.2
        while True:
            try:
                return self._open_connection(create)
            except sqlite3.Error as error:
                # Concurrent first opens may fail the WAL mode switch immediately,
                # even with SQLite's busy handler installed.
                if ((getattr(error, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or time.monotonic() >= deadline):
                    raise LedgerError("storage_unavailable") from None
                time.sleep(0.01)

    def _open_connection(self, create):
        try:
            self._safe_path(create=create)
        except OSError:
            raise LedgerError("storage_unavailable") from None
        if not create and not self.path.exists():
            return None
        if create:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        try:
            uri = self.path.resolve().as_uri() + ("?mode=rw" if create else "?mode=ro")
            db = sqlite3.connect(uri, uri=True, timeout=0.2)
            db.execute("PRAGMA busy_timeout=200")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
            # synchronous is connection-scoped, not persisted, so the perf posture must be
            # (re)set on every rw connection; leaving it at the FULL default would defeat
            # the WAL writer batching the write-ahead flush costs (S1).
            db.execute("PRAGMA synchronous=NORMAL")
            if not create:
                db.execute("BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not create and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                # Another collector may have just created the empty file.
                db.close()
                return None
            if create and version in (0, 1, 2):
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    raise LedgerError("unsupported_schema")
                schema = """
                    CREATE TABLE events (
                      instance_id TEXT NOT NULL, event_seq INTEGER NOT NULL, timestamp REAL NOT NULL,
                      provider TEXT NOT NULL, requested_model TEXT, reported_model TEXT,
                      usage_measured INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                      status TEXT NOT NULL, elapsed_ms REAL NOT NULL, event_version INTEGER NOT NULL DEFAULT 1,
                      cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
                      PRIMARY KEY (instance_id, event_seq));
                    CREATE INDEX events_timestamp ON events(timestamp);
                    CREATE TABLE cursors (instance_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                      inode TEXT NOT NULL, offset INTEGER NOT NULL, invalid_lines INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE collector_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE collector_issues (instance_id TEXT NOT NULL, code TEXT NOT NULL,
                      count INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(instance_id, code));
                    PRAGMA user_version=3;
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)
            elif version in (1, 2) and create:
                if version == 1:
                    db.execute("ALTER TABLE events ADD COLUMN event_version INTEGER NOT NULL DEFAULT 1")
                db.execute("ALTER TABLE events ADD COLUMN cache_read_tokens INTEGER")
                db.execute("ALTER TABLE events ADD COLUMN cache_creation_tokens INTEGER")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version not in (1, 2, SCHEMA_VERSION):
                raise LedgerError("unsupported_schema")
            if db.in_transaction:
                db.commit()
            return db
        except (sqlite3.Error, OSError, LedgerError) as error:
            if "db" in locals():
                db.close()
            if isinstance(error, (sqlite3.Error, LedgerError)):
                raise
            raise LedgerError("storage_unavailable") from None

    def ingest(self, instance_id, events):
        """Insert one bounded batch atomically; return inserted and duplicate counts."""
        instance = _instance(instance_id)
        if not isinstance(events, (list, tuple)) or len(events) > MAX_BATCH:
            raise LedgerError("invalid_batch")
        rows = [_event(instance, value) for value in events]
        if not rows:
            return {"inserted": 0, "duplicates": 0}
        db = self._connect(create=True)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                inserted, _ = self._insert_rows(db, rows)
            return {"inserted": inserted, "duplicates": len(rows) - inserted}
        except sqlite3.Error:
            raise LedgerError("storage_unavailable") from None
        finally:
            db.close()

    @staticmethod
    def _insert_rows(db, rows, *, skip_conflicts=False):
        inserted = conflicts = 0
        for row in rows:
            existing = db.execute("SELECT instance_id,event_seq,timestamp,provider,requested_model,reported_model,"
                                  "usage_measured,input_tokens,output_tokens,status,elapsed_ms,event_version,"
                                  "cache_read_tokens,cache_creation_tokens "
                                  "FROM events WHERE instance_id=? AND event_seq=?", row[:2]).fetchone()
            if existing is not None:
                if tuple(existing) != tuple(row):
                    if not skip_conflicts:
                        raise LedgerError("event_conflict")
                    conflicts += 1
            else:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                inserted += 1
        return inserted, conflicts

    @staticmethod
    def _cursor(db, instance):
        return db.execute(
            "SELECT device,inode,offset,EXISTS(SELECT 1 FROM collector_state WHERE name=?) "
            "FROM cursors WHERE instance_id=?", ("discard_line:" + instance, instance)).fetchone()

    def source_ready_for_cleanup(self, directory, cutoff):
        """Only retire an old event file whose committed cursor reached its end."""
        path = directory / "events.jsonl"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino
                or info.st_mtime >= cutoff):
            return False
        if info.st_size == 0:
            return True
        instance = _instance(directory.name)
        db = self._connect(create=False)
        if db is None:
            return False
        try:
            cursor = db.execute("SELECT device,inode,offset,invalid_lines FROM cursors WHERE instance_id=?",
                                (instance,)).fetchone()
            if cursor != (str(info.st_dev), str(info.st_ino), info.st_size, 0):
                return False
            # A late append or file replacement during the SQLite read defers cleanup.
            current = path.lstat()
            return (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) == (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, 1)
        except sqlite3.Error:
            return False
        finally:
            db.close()

    def collect(self):
        """Consume bounded bytes from per-instance append-only logs, atomically with cursors.

        An incomplete trailing JSON line remains for a subsequent pass. Files are never
        modified. Source replacement/truncation is reported, not silently reinterpreted.
        """
        result = {"files_seen": 0, "events_inserted": 0, "duplicates": 0,
                  "invalid_lines": 0, "pending": False, "errors": []}
        root = self.directory / "instances"
        self._safe_path(create=False)
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            raise LedgerError("unsafe_store")
        if not root.exists():
            return result
        try:
            # Resume physical enumeration before testing entries for event files. The
            # offset is only a scan hint; each pass wraps so directory changes self-heal.
            scan_offset = 0
            prior = self._connect(create=False)
            if prior is not None:
                try:
                    row = prior.execute("SELECT value FROM collector_state WHERE name='discovery_offset'").fetchone()
                    scan_offset = int(row[0]) if row else 0
                finally:
                    prior.close()
            page, next_offset, observed = self._scan.read(root, scan_offset, result)
            paths = [(identity, path) for _, identity, path in page]
            positions = {identity: pos for pos, identity, _ in page}
            if next_offset:
                result["pending"] = True
                result["errors"].append("instance_discovery_limit")
            if not paths and not (scan_offset or next_offset):
                return result
            db = self._connect(create=True)
            try:
                if scan_offset or next_offset:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        probe = db.execute("SELECT value FROM collector_state WHERE name='discovery_probe'").fetchone()
                        if probe and observed > int(probe[0]):
                            # A real entry beyond the limit confirms the historical gap.
                            db.execute("INSERT OR IGNORE INTO collector_state VALUES ('discovery_limited','1')")
                            db.execute("DELETE FROM collector_state WHERE name='discovery_probe'")
                        elif not next_offset:
                            db.execute("DELETE FROM collector_state WHERE name='discovery_probe'")
                        elif not probe:
                            # Exactly reaching either cap is ambiguous until the next read.
                            db.execute("INSERT OR IGNORE INTO collector_state SELECT 'discovery_probe',? "
                                       "WHERE NOT EXISTS (SELECT 1 FROM collector_state WHERE name='discovery_limited')",
                                       (str(next_offset),))
                last = db.execute("SELECT value FROM collector_state WHERE name='last_instance'").fetchone()
                paged = bool(scan_offset or next_offset)
                if not paged:
                    paths.sort(key=lambda x: x[0])
                if last and not paged:
                    split = next((i for i, (name, _) in enumerate(paths) if name > last[0]), 0)
                    paths = paths[split:] + paths[:split]
                budget = MAX_COLLECT_BYTES
                last_visited = None
                for instance, path in paths[:MAX_COLLECT_FILES]:
                    if budget <= 0:
                        result["pending"] = True
                        break
                    result["files_seen"] += 1
                    last_visited = instance
                    try:
                        # Launchers may register before their first request creates the log.
                        if path.parent.is_symlink() or getattr(path.parent, "is_junction", lambda: False)():
                            raise LedgerError("unsafe_source")
                        if not path.exists() and not path.is_symlink():
                            continue
                        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                            raise LedgerError("unsafe_source")
                        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                        with os.fdopen(fd, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
                                raise LedgerError("unsafe_source")
                            cursor = self._cursor(db, instance)
                            offset = cursor[2] if cursor else 0
                            if cursor and ((cursor[0], cursor[1]) != (str(info.st_dev), str(info.st_ino)) or info.st_size < offset):
                                raise LedgerError("source_changed")
                            stream.seek(offset)
                            chunk = stream.read(min(MAX_FILE_CHUNK, budget))
                        budget -= len(chunk)
                        discarding = bool(cursor and cursor[3])
                        oversized = not discarding and len(chunk) == MAX_FILE_CHUNK and b"\n" not in chunk
                        consumed = chunk.rfind(b"\n") + 1
                        complete = chunk[:consumed]
                        if discarding:
                            complete = complete.partition(b"\n")[2]
                        if (oversized or discarding) and not consumed:
                            consumed = len(chunk)
                        result["pending"] |= (offset + consumed < info.st_size or bool(chunk and not consumed)
                                              or ((oversized or discarding) and b"\n" not in chunk))
                        if not consumed:
                            continue
                        rows, invalid = [], int(oversized)
                        for line in complete.splitlines():
                            try:
                                rows.append(_event(instance, json.loads(line)))
                            except (ValueError, UnicodeError, RecursionError, TypeError):
                                invalid += 1
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            if self._cursor(db, instance) != cursor:
                                # A peer already committed this portion of the source.
                                result["pending"] = True
                                continue
                            issues = {}
                            discard_key = "discard_line:" + instance
                            if oversized:
                                db.execute("INSERT INTO collector_state VALUES (?, '1')", (discard_key,))
                                issues["line_too_long"] = 1
                            if discarding and b"\n" in chunk:
                                db.execute("DELETE FROM collector_state WHERE name=?", (discard_key,))
                            previous = db.execute("SELECT MAX(event_seq) FROM events WHERE instance_id=?", (instance,)).fetchone()[0] or 0
                            gap = False
                            for row in rows:
                                if row[1] > previous + 1:
                                    gap = True
                                previous = max(previous, row[1])
                            if gap:
                                issues["sequence_gap"] = 1
                            inserted, conflicts = self._insert_rows(db, rows, skip_conflicts=True)
                            if conflicts:
                                issues["event_conflict"] = conflicts
                                invalid += conflicts
                            for code, count in issues.items():
                                db.execute("INSERT INTO collector_issues VALUES (?,?,?) ON CONFLICT(instance_id,code) "
                                           "DO UPDATE SET count=count+excluded.count", (instance, code, count))
                            db.execute("INSERT INTO cursors VALUES (?,?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET offset=excluded.offset,invalid_lines=cursors.invalid_lines+excluded.invalid_lines",
                                       (instance, str(info.st_dev), str(info.st_ino), offset + consumed, invalid))
                        result["errors"].extend(code for code in issues if code not in result["errors"])
                        result["events_inserted"] += inserted
                        result["duplicates"] += len(rows) - inserted - conflicts
                        result["invalid_lines"] += invalid
                    except (OSError, LedgerError) as error:
                        code = str(error) if isinstance(error, LedgerError) else "source_unavailable"
                        if code not in result["errors"]:
                            result["errors"].append(code)
                        result["pending"] = True
                        with db:
                            db.execute("INSERT INTO collector_issues VALUES (?,?,1) ON CONFLICT(instance_id,code) DO UPDATE SET count=count+1", (instance, code))
                if last_visited is not None:
                    with db:
                        db.execute("INSERT INTO collector_state VALUES ('last_instance',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (last_visited,))
                if paged:
                    if last_visited is not None and last_visited != paths[-1][0]:
                        next_offset = positions[last_visited]
                        result["pending"] = True
                    with db:
                        db.execute("INSERT INTO collector_state VALUES ('discovery_offset',?) "
                                   "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(next_offset),))
                # Unvisited files are not automatically backlog: old sessions may be fully collected.
                if len(paths) > MAX_COLLECT_FILES and not result["pending"]:
                    remaining = paths[MAX_COLLECT_FILES:]
                    placeholders = ",".join("?" for _ in remaining)
                    cursors = {row[0]: row[1:] for row in db.execute(
                        f"SELECT instance_id,device,inode,offset FROM cursors WHERE instance_id IN ({placeholders})",
                        [instance for instance, _ in remaining])}
                    for instance, path in remaining:
                        try:
                            info = path.stat(follow_symlinks=False)
                        except OSError:
                            result["pending"] = True
                            break
                        cursor = cursors.get(instance)
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or cursor and ((cursor[0], cursor[1]) != (str(info.st_dev), str(info.st_ino)) or info.st_size < cursor[2])
                                or info.st_size > (cursor[2] if cursor else 0)):
                            result["pending"] = True
                            break
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            raise LedgerError("storage_unavailable") from None
        return result

    def report(self, period, now=None):
        """Return bounded local-time day/week/month groups; unknown token values remain null."""
        if period not in ("day", "week", "month"):
            raise LedgerError("invalid_period")
        if now is not None and (not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None):
            raise LedgerError("invalid_timestamp")
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone()
        today = local.date()
        if period == "day":
            start = today - dt.timedelta(days=30)
            bucket = "date(timestamp, 'unixepoch', 'localtime')"
        elif period == "week":
            start = today - dt.timedelta(days=today.weekday() + 11 * 7)
            bucket = "date(timestamp, 'unixepoch', 'localtime', '-6 days', 'weekday 1')"
        else:
            index = today.year * 12 + today.month - 1 - 12
            start = dt.date(index // 12, index % 12 + 1, 1)
            bucket = "strftime('%Y-%m', timestamp, 'unixepoch', 'localtime')"
        response = {"period": period, "week_starts": "monday", "timezone": "system_local",
                    "since": start.isoformat(), "source": "proxy_events", "coverage": "collected_only",
                    "collection_started": None, "rows": [], "truncated": False, "storage_bytes": 0,
                    "invalid_lines": 0, "collection_issues": [], "model_provenance_unverified": 0}
        db = self._connect(create=False)
        if db is None:
            return response
        try:
            first = db.execute("SELECT MIN(timestamp) FROM events").fetchone()[0]
            if first is not None:
                response["collection_started"] = dt.datetime.fromtimestamp(first, dt.timezone.utc).isoformat()
            response["invalid_lines"] = db.execute("SELECT COALESCE(SUM(invalid_lines),0) FROM cursors").fetchone()[0]
            response["collection_issues"] = [row[0] for row in db.execute("SELECT DISTINCT code FROM collector_issues ORDER BY code")]
            if db.execute("SELECT 1 FROM collector_state WHERE name IN ('discovery_limited','discovery_probe')").fetchone():
                response["collection_issues"].append("instance_discovery_limit")
            if response["invalid_lines"] or response["collection_issues"]:
                response["coverage"] = "partial"
            start_epoch = time.mktime((start.year, start.month, start.day, 0, 0, 0, 0, 0, -1))
            version = db.execute("PRAGMA user_version").fetchone()[0]
            model = "CASE WHEN event_version >= 2 THEN reported_model ELSE NULL END" if version >= 2 else "NULL"
            response["model_provenance_unverified"] = db.execute(
                "SELECT COUNT(*) FROM events WHERE " + ("event_version < 2" if version >= 2 else "1")
                + " AND timestamp >= ? AND timestamp <= ?", (start_epoch, local.timestamp())).fetchone()[0]
            cache_columns = ", SUM(cache_read_tokens), SUM(cache_creation_tokens)" if version >= 3 else ", NULL, NULL"
            sql = f"""SELECT {bucket} AS bucket, provider, requested_model, {model} AS actual_model, instance_id,
                COUNT(*), SUM(status='ok'), SUM(status='failed'), SUM(status='aborted'),
                COUNT(input_tokens), SUM(input_tokens), COUNT(output_tokens), SUM(output_tokens),
                AVG(elapsed_ms), MAX(elapsed_ms){cache_columns}
                FROM events WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY bucket, provider, requested_model, actual_model, instance_id
                ORDER BY bucket DESC, provider, actual_model, instance_id LIMIT ?"""
            result = db.execute(sql, (start_epoch, local.timestamp(), MAX_ROWS + 1)).fetchall()
            keys = ("bucket", "provider", "requested_model", "reported_model", "instance_id", "requests",
                    "ok", "failed", "aborted", "input_samples", "input_tokens", "output_samples",
                    "output_tokens", "mean_elapsed_ms", "max_elapsed_ms")
            keys += ("cache_read_tokens", "cache_creation_tokens")
            response["truncated"] = len(result) > MAX_ROWS
            response["rows"] = [dict(zip(keys, row)) for row in result[:MAX_ROWS]]
            response["storage_bytes"] = self.path.stat().st_size
            return response
        except (sqlite3.Error, OSError):
            raise LedgerError("storage_unavailable") from None
        finally:
            db.close()
