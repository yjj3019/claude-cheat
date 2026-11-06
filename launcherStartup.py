# SPDX-License-Identifier: MIT
"""Startup-only update and dashboard coordination for OpenClaude."""
import subprocess
import sys
import time
import webbrowser
import re
import os
import json
import contextlib
import stat
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
_bootstrap_lock = None
if __name__ == "__main__":
    # Acquire before importing project modules: another launcher may be replacing them.
    try:
        _bootstrap_lock = open(ROOT / ".openclaude-update.lock", "a+b")
        if os.name == "nt":
            import msvcrt
            _bootstrap_lock.seek(0, os.SEEK_END)
            if not _bootstrap_lock.tell():
                _bootstrap_lock.write(b"\0")
                _bootstrap_lock.flush()
        else:
            import fcntl
        deadline = time.monotonic() + 30
        while True:
            try:
                if os.name == "nt":
                    _bootstrap_lock.seek(0)
                    msvcrt.locking(_bootstrap_lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(_bootstrap_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    except OSError:
        print("OpenClaude: startup lock unavailable.", file=sys.stderr)
        sys.exit(1)

import routeDashboard as dash
import routeVersion


class UnsafeUpdate(Exception):
    pass


def _session_active():
    instances = ROOT / ".usage-stats" / "instances"
    try:
        paths = [entry / "handoff.json" for entry in instances.iterdir() if stat.S_ISDIR(entry.stat().st_mode)]
    except FileNotFoundError:
        paths = []
    paths.append(dash.SESSION_HANDOFF_FILE)
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if (not isinstance(record, dict) or type(record.get("port")) is not int
                or not 1 <= record["port"] <= 65535):
            raise ValueError("invalid_handoff")
        if dash._port_open(record["port"]):
            return True
    session, _ = dash._live_session()
    return bool(session or dash.live_sessions())


def update(lock_held=False):
    """Fast-forward the configured origin branch only when the checkout is clean."""
    try:
        lock = contextlib.nullcontext() if lock_held else dash.routeConfig.Store(ROOT / "openclaude-update")._cross_process_lock()
        with lock:
            if (ROOT / ".openclaude-update-incomplete").exists():
                raise UnsafeUpdate("update_incomplete")
            if not routeVersion._env_flag("ROUTE_UPDATE_CHECK", True):
                return False
            if (os.environ.get("ROUTE_UPDATE_REMOTE") or "origin") != "origin":
                print("OpenClaude: automatic update supports origin only; skipped.")
                return False
            if _session_active():
                print("OpenClaude: active session; automatic update skipped.")
                return False
            return _update_locked(_session_active)
    except (OSError, ValueError, dash.routeConfig.ConfigError):
        if (ROOT / ".openclaude-update-incomplete").exists():
            raise UnsafeUpdate("update_incomplete") from None
        print("OpenClaude: session or update state unavailable; automatic update skipped.")
        return False


def _update_locked(session_check=None):
    def git(*args, timeout=None):
        return routeVersion._git(list(args), timeout=timeout)

    marker = ROOT / ".openclaude-update-incomplete"
    if marker.exists():
        raise UnsafeUpdate("update_incomplete")
    code, dirty, _ = git("status", "--porcelain", "--untracked-files=all")
    if code != 0 or dirty:
        print("OpenClaude: local changes or unavailable Git; automatic update skipped.")
        return False
    code, branch, _ = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if code != 0 or not branch:
        print("OpenClaude: no active branch; automatic update skipped.")
        return False
    code, initial_head, _ = git("rev-parse", "HEAD")
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", initial_head):
        print("OpenClaude: current revision unavailable; automatic update skipped.")
        return False
    if os.environ.get("ROUTE_UPDATE_BRANCH") not in (None, "", branch):
        print("OpenClaude: configured update branch differs; automatic update skipped.")
        return False
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        code, path, _ = git("rev-parse", "--git-path", name)
        if code != 0 or not path or (ROOT / path).exists():
            print("OpenClaude: repository operation in progress; automatic update skipped.")
            return False
    code, upstream, _ = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if code != 0 or upstream != "origin/" + branch:
        print("OpenClaude: origin tracking branch is not configured; automatic update skipped.")
        return False
    code, _, _ = git("fetch", "--no-tags", "origin", branch, timeout=15)
    if code != 0:
        print("OpenClaude: update check failed; continuing with the current version.")
        return False
    code, remote, _ = git("rev-parse", "FETCH_HEAD")
    local_code, local, _ = git("rev-parse", "HEAD")
    if code != 0 or local_code != 0 or local != initial_head or not re.fullmatch(r"[0-9a-f]{40,64}", remote) or remote == local:
        return False
    local_version_code, local_version, _ = git("show", f"{local}:VERSION")
    remote_version_code, remote_version, _ = git("show", f"{remote}:VERSION")
    version_pattern = r"v?(\d+)\.(\d+)\.(\d+)"
    current_version = re.fullmatch(version_pattern, local_version.strip())
    next_version = re.fullmatch(version_pattern, remote_version.strip())
    if local_version_code != 0 or remote_version_code != 0 or not current_version or not next_version:
        print("OpenClaude: release versions cannot be compared; automatic update skipped.")
        return False
    if tuple(map(int, next_version.groups())) <= tuple(map(int, current_version.groups())):
        return False
    code, _, _ = git("merge-base", "--is-ancestor", local, remote)
    if code != 0:
        print("OpenClaude: branches cannot fast-forward; automatic update skipped.")
        return False
    code, dirty, _ = git("status", "--porcelain", "--untracked-files=all")
    if code != 0 or dirty:
        print("OpenClaude: local changes detected; automatic update skipped.")
        return False
    if session_check is not None and session_check():
        print("OpenClaude: active session; automatic update skipped.")
        return False
    branch_code, current_branch, _ = git("symbolic-ref", "--quiet", "--short", "HEAD")
    head_code, current_head, _ = git("rev-parse", "HEAD")
    if branch_code != 0 or head_code != 0 or current_branch != branch or current_head != initial_head:
        print("OpenClaude: checkout changed; automatic update skipped.")
        return False
    with marker.open("x", encoding="ascii") as handle:
        handle.write("Inspect the checkout before removing this update marker.\n")
    code, _, _ = git("merge", "--ff-only", "--no-overwrite-ignore", remote, timeout=15)
    if code != 0:
        status_code, dirty, _ = git("status", "--porcelain", "--untracked-files=all")
        head_code, head, _ = git("rev-parse", "HEAD")
        if status_code != 0 or dirty or head_code != 0 or head != local:
            raise UnsafeUpdate("update_incomplete")
        marker.unlink()
        print("OpenClaude: update could not be applied; continuing with the current version.")
        return False
    marker.unlink()
    print("OpenClaude: update applied; restarting launcher.")
    return True


def dashboard():
    """Reuse a verified dashboard or start one without stopping any existing process."""
    port = dash.DEFAULT_PORT
    # Open the token-backed write_url so the settings page can save; a plain
    # http://127.0.0.1:<port>/ visit is read-only by design (token_required).
    def open_write_url():
        webbrowser.open(f"http://127.0.0.1:{port}/#token={quote(getattr(dash, 'TOKEN', ''), safe='')}")
    if dash._port_open(port):
        record = dash._read_pid_record()
        if record and record.get("port") == port and dash._verify_pid_record(record) == "verified":
            open_write_url()
            return True
        print("OpenClaude: dashboard port is occupied by an unverified process.", file=sys.stderr)
        return False
    try:
        process = subprocess.Popen([sys.executable, str(ROOT / "routeDashboard.py"), "--no-takeover"],
                         cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         env={**os.environ, "DASHBOARD_TOKEN": dash.TOKEN},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        print("OpenClaude: could not start dashboard.", file=sys.stderr)
        return False
    for _ in range(50):
        record = dash._read_pid_record()
        if record and record.get("port") == port and dash._port_open(port):
            if record.get("pid") == process.pid or dash._verify_pid_record(record) == "verified":
                open_write_url()
                return True
        if process.poll() is not None and not dash._port_open(port):
            break
        time.sleep(0.1)
    print("OpenClaude: dashboard did not become ready.", file=sys.stderr)
    return False


if __name__ == "__main__":
    if sys.argv[1:] == ["--update"]:
        try:
            sys.exit(10 if update(lock_held=True) else 0)
        except UnsafeUpdate:
            print("OpenClaude: update incomplete; inspect the checkout before starting.", file=sys.stderr)
            sys.exit(20)
    if sys.argv[1:] == ["--dashboard"]:
        sys.exit(0 if dashboard() else 1)
    sys.exit(2)
