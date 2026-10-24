"""Measure the dashboard's per-refresh work: route log parsing and transcript scanning.

    python tests/bench_dashboard.py [--files 2000] [--log-mb 10]

The dashboard rebuilds a snapshot every few seconds, so these two functions decide
how much CPU it costs while a session runs. Run this before and after a change.
"""
import argparse
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

SPEC = importlib.util.spec_from_file_location(
    "route_dashboard", Path(__file__).resolve().parents[1] / "routeDashboard.py")
dash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dash)


def route_log(path, megabytes):
    """A realistic log: one JSON event per line, mostly replacements with some Plan turns."""
    stamp = datetime.now(timezone.utc).isoformat()
    line = json.dumps({"event": "route_selected", "provider": "openrouter", "reason": "replacement",
                       "model": "vendor/model"})
    plan = json.dumps({"event": "route_selected", "provider": "plan", "reason": "native_feature",
                       "detail": "unsupported_beta", "model": "claude-sonnet-5-5"})
    target = megabytes * 1024 * 1024
    with path.open("w", encoding="utf-8") as handle:
        written = 0
        index = 0
        while written < target:
            row = f"{stamp} {plan if index % 20 == 0 else line}\n"
            handle.write(row)
            written += len(row)
            index += 1
    return path


def transcripts(root, projects, per_project, lines):
    """Synthetic Claude Code transcripts with usage records."""
    for project in range(projects):
        folder = root / f"project-{project}"
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(per_project):
            path = folder / f"session-{index}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for turn in range(lines):
                    record = {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                              "message": {"model": "vendor/model", "usage": {
                                  "input_tokens": 1200, "output_tokens": 300,
                                  "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 500}}}
                    handle.write(json.dumps(record) + "\n")
    return root


def timed(label, function, rounds=3):
    samples = []
    result = None
    for _ in range(rounds):
        started = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(f"{label:<44} {samples[len(samples) // 2]:8.1f}ms  (min {samples[0]:.1f})")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--log-mb", type=int, default=10)
    options = parser.parse_args()

    with TemporaryDirectory() as directory:
        root = Path(directory)
        log = route_log(root / ".route.log", options.log_mb)
        print(f"route log: {log.stat().st_size / 1048576:.1f}MB")
        started = time.perf_counter()
        dash.parse_route_log(log)
        print(f"{'parse_route_log (cold, full read)':<44} {(time.perf_counter() - started) * 1000:8.1f}ms")
        timed("parse_route_log (incremental, unchanged)", lambda: dash.parse_route_log(log))
        with log.open("a", encoding="utf-8") as handle:
            for _ in range(200):
                handle.write(json.dumps({"event": "route_selected", "provider": "openrouter",
                                         "reason": "replacement"}) + "\n")
        timed("parse_route_log (200 new lines)", lambda: dash.parse_route_log(log))

        projects = transcripts(root / "projects", projects=max(1, options.files // 40),
                               per_project=40, lines=120)
        count = sum(1 for _ in projects.rglob("*.jsonl"))
        print(f"transcripts: {count} files")
        dash._SESSIONS.clear()
        timed("scan_sessions (first)", lambda: dash.scan_sessions(projects=projects, pricing=None),
              rounds=2)
        timed("scan_sessions (incremental)", lambda: dash.scan_sessions(projects=projects, pricing=None))
        newest = sorted(projects.rglob("*.jsonl"))[0]
        with newest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                                     "message": {"model": "vendor/model", "usage": {"input_tokens": 10}}}) + "\n")
        timed("scan_sessions (one file appended)", lambda: dash.scan_sessions(projects=projects, pricing=None))


if __name__ == "__main__":
    main()
