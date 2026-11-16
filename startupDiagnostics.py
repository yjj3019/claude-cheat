"""Bounded, credential-free startup evidence shared by launcher and monitoring."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CODES = {"starting", "ready", "spawn_failed", "configuration_invalid", "port_in_use", "bind_denied",
         "proxy_boot_failed", "ready_timeout", "invalid_ready", "ownership_failed"}

def record(root, instance, provider, stage, code, exit_code=None):
    if code not in CODES:
        code = "proxy_boot_failed"
    data = {"instance_id": instance, "provider": provider, "stage": stage, "code": code,
            "exit_code": exit_code if type(exit_code) is int else None,
            "at": datetime.now(timezone.utc).isoformat()}
    directory = Path(root) / ".usage-stats" / "startup"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporary, directory / (instance + ".json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        for old in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[100:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return data

def recent(root):
    directory = Path(root) / ".usage-stats" / "startup"
    try:
        paths = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    except OSError:
        return []
    rows = []
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("code") in CODES:
                rows.append({key: data.get(key) for key in ("instance_id", "provider", "stage", "code", "exit_code", "at")})
        except (OSError, ValueError, AttributeError):
            continue
    return rows
