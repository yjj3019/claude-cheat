# SPDX-License-Identifier: MIT
"""Version identity and update notices for OpenClaude.

The product version comes from ``VERSION``; the exact revision comes from git, so a
dashboard or launcher can always say which build is running. Update checks use
``git ls-remote`` — a read-only network call that never writes refs or objects — and
every result is cached, so a polled dashboard never blocks on the network.

Git startup costs about a second on Windows, so checkout facts are cached after the
first background read. An explicit refresh or ``--version`` can read them again.

Failure policy: a missing git, a detached checkout, a private remote or an offline
machine degrades to ``unknown`` with the reason attached. Nothing here raises into a
caller that only wants to render a badge.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
UPDATE_CACHE_FILE = ROOT / ".update-cache.json"
FALLBACK_VERSION = "0.0.0"

# Local git facts change only when the tree changes; the remote answer is polled rarely.
LOCAL_TTL_SECONDS = 60
REMOTE_TTL_SECONDS = 1800
UPDATE_CACHE_VERSION = 1
GIT_TIMEOUT_SECONDS = 6
_SEMVER = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)$")
# Porcelain v2 entry prefixes: changed (1), renamed (2) and unmerged (u) tracked paths.
_PORCELAIN_CHANGED = ("1 ", "2 ", "u ")

_LOCAL = {"at": 0.0, "signature": None, "data": None, "inflight": False}
_REMOTE = {"at": 0.0, "data": None, "inflight": False}
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.isoformat(timespec="seconds")


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "off", "false", "no", "disabled")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def declared_version() -> str:
    """The version written in VERSION, or a safe fallback when it is missing or wrong."""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_VERSION
    match = _SEMVER.match(text.splitlines()[0].strip() if text else "")
    return match.group(1) if match else FALLBACK_VERSION


def _git(args, timeout: int | None = None) -> tuple[int | None, str, str]:
    """Run one git command in the checkout without opening a Windows console.

    Returns ``(exit_code, stdout, error)``; ``exit_code`` is ``None`` when git could not
    be started at all (not installed, not on PATH, timed out).
    """
    command = ["git", "-C", str(ROOT), *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                   timeout=timeout or GIT_TIMEOUT_SECONDS,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                        "GIT_ASKPASS": "", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as error:
        return None, "", f"{type(error).__name__}: {error}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return completed.returncode, completed.stdout.strip(), (detail[-1][:200] if detail else "git failed")
    return 0, completed.stdout.strip(), ""


def _git_dir() -> Path | None:
    """Resolve ``.git`` without asking git (a worktree keeps a ``gitdir:`` pointer file)."""
    candidate = ROOT / ".git"
    try:
        if candidate.is_dir():
            return candidate
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("gitdir:"):
                    target = Path(line.split(":", 1)[1].strip())
                    return target if target.is_absolute() else (ROOT / target)
    except OSError:
        return None
    return None


def _local_signature():
    """Cheap stat of the files that change when HEAD or the index moves."""
    git_dir = _git_dir()
    if git_dir is None:
        return None
    parts = []
    for name in ("HEAD", "index"):
        try:
            parts.append((git_dir / name).stat().st_mtime_ns)
        except OSError:
            parts.append(None)
    return tuple(parts)


def _read_local() -> dict:
    """Version plus revision facts; two git calls cover commit, branch, dirtiness and date."""
    base = declared_version()
    info = {"version": base, "declared_version": base, "commit": None, "commit_short": None,
            "commit_date": None, "branch": None, "dirty": None,
            "upstream": None, "label": f"v{base}", "source": "version-file", "error": None}
    # Porcelain v2 --branch answers commit, branch and dirtiness in one process start.
    code, out, error = _git(["status", "--porcelain=v2", "--branch", "--untracked-files=no"])
    if code != 0:
        info.update({"source": "unavailable", "error": error})
        return info
    commit = None
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            value = line[len("# branch.oid "):].strip()
            commit = None if value == "(initial)" else value
        elif line.startswith("# branch.head "):
            value = line[len("# branch.head "):].strip()
            info["branch"] = None if value == "(detached)" else value
        elif line.startswith("# branch.ab "):
            info["upstream"] = line[len("# branch.ab "):].strip()
    if not commit:
        info.update({"source": "unavailable", "error": "no commit yet"})
        return info
    info["dirty"] = any(line.startswith(_PORCELAIN_CHANGED) for line in out.splitlines())
    info.update({"commit": commit, "commit_short": commit[:7], "source": "git"})
    code, out, _ = _git(["log", "-1", "--format=%cI", commit])
    if code == 0 and out:
        info["commit_date"] = out.strip()
    # An annotated release tag is not consulted: VERSION is the release name and a tag
    # lookup costs another git process start on every read.
    dirty_mark = "*" if info["dirty"] else ""
    info["label"] = f"v{info['version']} ({info['commit_short']}{dirty_mark})"
    return info


def _refresh_local() -> None:
    """Background checkout read: one at a time, never raising."""
    with _LOCK:
        if _LOCAL["inflight"]:
            return
        _LOCAL["inflight"] = True
        signature = _local_signature()
    try:
        data = _read_local()
        with _LOCK:
            _LOCAL.update({"at": time.monotonic(), "signature": signature, "data": data})
    except Exception as error:  # noqa: BLE001 - a badge must never break the dashboard
        with _LOCK:
            _LOCAL.update({"at": time.monotonic(), "signature": signature, "data": {
                **{"version": declared_version(), "commit": None, "commit_short": None,
                   "commit_date": None, "branch": None, "dirty": None,
                   "upstream": None, "label": f"v{declared_version()}"},
                "source": "unavailable", "error": f"{type(error).__name__}: {error}"}})
    finally:
        with _LOCK:
            _LOCAL["inflight"] = False


def _pending_local() -> dict:
    base = declared_version()
    return {"version": base, "declared_version": base, "commit": None, "commit_short": None,
            "commit_date": None, "branch": None, "dirty": None, "upstream": None,
            "label": f"v{base}", "source": "version-file", "error": None, "pending": True}


def local_info(refresh: bool = False, blocking: bool = False) -> dict:
    """Cached version facts.

    Without ``blocking`` this returns the last reading (or a placeholder) immediately and
    re-reads in the background, so a 1.1s git startup never lands in a render path.
    A cached answer is reused while it is younger than ``LOCAL_TTL_SECONDS``, so an
    explicit ``refresh`` is the only forced re-read; a background poll re-reads after
    the TTL instead of spawning git in a render path.
    """
    with _LOCK:
        cached = _LOCAL["data"]
        age = time.monotonic() - _LOCAL["at"] if cached is not None else None
        reusable = (cached is not None and not refresh
                    and age is not None and age < LOCAL_TTL_SECONDS)
    if reusable:
        return {**cached, "cached": True, "age_seconds": round(age or 0, 1)}
    if blocking:
        _refresh_local()
        with _LOCK:
            data = _LOCAL["data"] or _pending_local()
        return {**data, "cached": False, "age_seconds": 0.0}
    threading.Thread(target=_refresh_local, name="version-local", daemon=True).start()
    with _LOCK:
        cached = _LOCAL["data"]
    if cached is not None:
        return {**cached, "cached": True, "stale": True,
                "age_seconds": round(age or 0, 1) if age is not None else None}
    return {**_pending_local(), "cached": False}


def _count(span: str) -> int | None:
    code, out, _ = _git(["rev-list", "--count", span])
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _compare(local_sha: str, remote_sha: str) -> dict:
    """Classify the local checkout against a remote head."""
    if local_sha and remote_sha and local_sha == remote_sha:
        return {"state": "current", "behind_by": 0, "ahead_by": 0}
    # Only a commit already downloaded can be counted; otherwise the honest answer is
    # "different" and the reader runs git pull/fetch to see the detail.
    code, _, _ = _git(["cat-file", "-e", f"{remote_sha}^{{commit}}"])
    if code != 0:
        return {"state": "different", "behind_by": None, "ahead_by": None}
    behind = _count(f"{local_sha}..{remote_sha}")
    ahead = _count(f"{remote_sha}..{local_sha}")
    if behind is None or ahead is None:
        return {"state": "different", "behind_by": None, "ahead_by": None}
    if behind and not ahead:
        state = "behind"
    elif ahead and not behind:
        state = "ahead"
    elif behind and ahead:
        state = "diverged"
    else:
        state = "current"
    return {"state": state, "behind_by": behind, "ahead_by": ahead}


def _remote_head(remote: str, branch: str, timeout: int) -> tuple[str | None, str]:
    """The remote branch head via ls-remote: no objects, refs or working tree are touched."""
    code, out, error = _git(["ls-remote", "--heads", remote, f"refs/heads/{branch}"], timeout=timeout)
    if code != 0:
        return None, error or "ls-remote failed"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(f"refs/heads/{branch}"):
            return parts[0], ""
    return None, f"{remote} has no branch {branch}"


def _check_remote(local: dict, remote: str, branch: str | None, timeout: int) -> dict:
    result = {"state": "unknown", "checked_at": _iso(_now()), "remote": remote,
              "remote_branch": branch, "remote_commit": None, "behind_by": None,
              "ahead_by": None, "error": None, "local_commit": local.get("commit")}
    if not _env_flag("ROUTE_UPDATE_CHECK", True):
        return {**result, "state": "disabled", "checked_at": None}
    if not local.get("commit"):
        return {**result, "error": local.get("error") or "no local revision"}
    target_branch = branch or local.get("branch")
    if not target_branch:
        return {**result, "error": "detached HEAD; set ROUTE_UPDATE_BRANCH"}
    result["remote_branch"] = target_branch
    remote_sha, error = _remote_head(remote, target_branch, timeout)
    if not remote_sha:
        return {**result, "error": error}
    result["remote_commit"] = remote_sha
    result.update(_compare(local["commit"], remote_sha))
    return result


def _refresh_remote(local: dict, remote: str, branch: str | None, timeout: int) -> None:
    """Background refresh: one network check at a time, never raising into the caller."""
    with _LOCK:
        if _REMOTE["inflight"]:
            return
        _REMOTE["inflight"] = True
    try:
        data = _check_remote(local, remote, branch, timeout)
        _save_update_cache(data)
        with _LOCK:
            _REMOTE.update({"at": time.monotonic(), "data": data})
    except Exception as error:  # noqa: BLE001 - a badge must never break the dashboard
        with _LOCK:
            _REMOTE.update({"at": time.monotonic(), "data": {
                "state": "unknown", "checked_at": _iso(_now()), "remote": remote,
                "remote_branch": branch, "remote_commit": None, "behind_by": None,
                "ahead_by": None, "error": f"{type(error).__name__}: {error}"}})
    finally:
        with _LOCK:
            _REMOTE["inflight"] = False


def _save_update_cache(data: dict) -> None:
    """Persist the last remote answer so a fresh launcher can show it without waiting."""
    if not isinstance(data, dict) or not data.get("state") or data["state"] == "checking":
        return
    try:
        payload = {"version": UPDATE_CACHE_VERSION, "saved_at": _iso(_now()), "data": data}
        temporary = UPDATE_CACHE_FILE.with_suffix(UPDATE_CACHE_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, UPDATE_CACHE_FILE)
    except OSError:
        pass


def _load_update_cache() -> dict | None:
    """The previous remote answer, if one was ever written."""
    try:
        payload = json.loads(UPDATE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != UPDATE_CACHE_VERSION:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) and data.get("state") else None


def _seed_update_cache() -> None:
    """Show the last known answer immediately; the caller's refresh still runs behind it."""
    with _LOCK:
        if _REMOTE["data"] is not None:
            return
        cached = _load_update_cache()
        if cached is not None:
            _REMOTE.update({"at": 0.0, "data": cached})


def update_status(refresh: bool = False, async_refresh: bool = True, local: dict | None = None) -> dict:
    """Cached remote comparison; with ``async_refresh`` a stale cache revalidates behind."""

    def _options() -> tuple[str, str | None, int, int]:
        return (os.environ.get("ROUTE_UPDATE_REMOTE") or "origin",
                os.environ.get("ROUTE_UPDATE_BRANCH") or None,
                _env_int("ROUTE_UPDATE_TIMEOUT_MS", GIT_TIMEOUT_SECONDS * 1000) // 1000 or 1,
                _env_int("ROUTE_UPDATE_TTL_SECONDS", REMOTE_TTL_SECONDS))

    remote, branch, timeout, ttl = _options()
    if not _env_flag("ROUTE_UPDATE_CHECK", True):
        return {"state": "disabled", "checked_at": None, "remote": remote,
                "remote_branch": branch, "remote_commit": None, "behind_by": None,
                "ahead_by": None, "error": None}

    facts = local or local_info()
    if facts.get("pending"):
        # The checkout read has not finished yet. The last persisted answer is still the
        # best thing a launcher banner can show, clearly marked as a previous check.
        cached = _load_update_cache()
        if cached is not None:
            return {**cached, "cached": True, "stale": True, "previous_check": True}
        return {"state": "checking", "checked_at": None, "remote": remote,
                "remote_branch": branch, "remote_commit": None, "behind_by": None,
                "ahead_by": None, "error": None, "cached": False}
    _seed_update_cache()
    with _LOCK:
        cached = _REMOTE["data"]
        age = time.monotonic() - _REMOTE["at"] if cached is not None else None
        fresh = cached is not None and not refresh and age is not None and age < ttl
    if fresh:
        return {**cached, "cached": True, "age_seconds": round(age or 0, 1)}
    if async_refresh:
        threading.Thread(target=_refresh_remote, args=(facts, remote, branch, timeout),
                         name="update-check", daemon=True).start()
        if cached is not None:
            return {**cached, "cached": True, "stale": True,
                    "age_seconds": round(age or 0, 1) if age is not None else None}
        return {"state": "checking", "checked_at": None, "remote": remote,
                "remote_branch": branch, "remote_commit": None, "behind_by": None,
                "ahead_by": None, "error": None, "cached": False}
    data = _check_remote(facts, remote, branch, timeout)
    _save_update_cache(data)
    with _LOCK:
        _REMOTE.update({"at": time.monotonic(), "data": data})
    return {**data, "cached": False, "age_seconds": 0.0}


def version_report(refresh: bool = False, include_update: bool = True,
                   async_update: bool = True, blocking: bool = False) -> dict:
    """One document for the dashboard, the CLI and the launcher banner."""
    local = local_info(refresh=refresh, blocking=blocking)
    report = {"layout": "openclaude-version-1", "local": local,
              "generated_at": _iso(_now())}
    if include_update:
        report["update"] = update_status(refresh=refresh, async_refresh=async_update, local=local)
    return report


def notice(report: dict) -> str:
    """A one-line Korean summary: what is running and whether an update is waiting."""
    local = report.get("local") or {}
    label = local.get("label") or f"v{local.get('version') or FALLBACK_VERSION}"
    update = report.get("update")
    if update is None:
        return f"OpenClaude {label} · 업데이트 확인 안 함"
    state = update.get("state")
    tail = {
        "current": "최신 상태",
        "behind": f"업데이트 있음 · {update.get('behind_by') or '?'}개 커밋 뒤짐 (git pull)",
        "ahead": f"로컬이 {update.get('ahead_by') or '?'}개 커밋 앞섬 (git push)",
        "diverged": "로컬과 원격이 갈라짐 (확인 필요)",
        "different": "원격 커밋과 다름 (git fetch로 확인)",
        "checking": "업데이트 확인 중",
        "disabled": "업데이트 확인 꺼짐",
        "unknown": "업데이트 확인 불가" + (f": {update.get('error')}" if update.get("error") else ""),
    }.get(state or "unknown", f"업데이트 상태 {state or 'unknown'}")
    if update.get("previous_check"):
        # The checkout read had not finished: say which check this answer comes from.
        tail += " (마지막 확인 기준)"
    branch = local.get("branch")
    where = f" · {branch}" if branch else ""
    return f"OpenClaude {label}{where} · {tail}"


def summarize(report: dict) -> str:
    """Plain text for `--version`, using the same facts the badge shows."""
    local = report.get("local") or {}
    update = report.get("update") or {}
    lines = [f"OpenClaude {local.get('label') or FALLBACK_VERSION}"]
    if local.get("branch"):
        lines.append(f"브랜치: {local['branch']}")
    if local.get("commit"):
        lines.append(f"커밋: {local['commit']}")
    if local.get("commit_date"):
        lines.append(f"커밋 시각: {local['commit_date']}")
    if local.get("dirty"):
        lines.append("작업 트리: 커밋되지 않은 변경 있음")
    update = report.get("update")
    if update is None:
        lines.append("업데이트: 확인 안 함")
    else:
        lines.append("업데이트: " + ("최신 상태" if update.get("state") == "current"
                                    else notice(report).split(" · ")[-1]))
    if update and update.get("checked_at"):
        lines.append(f"확인 시각: {update['checked_at']}")
    if update and update.get("remote_commit") and update.get("state") in ("behind", "diverged", "different"):
        lines.append(f"원격 커밋: {update['remote_commit'][:7]}")
    return "\n".join(lines)
