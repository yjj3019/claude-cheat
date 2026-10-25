"""Regression tests for the defects found in the source audit."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import routeDashboard as dash


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    # --- settings file safety -------------------------------------------------
    def test_a_read_error_never_rewrites_the_settings_file(self):
        path = self.root / ".env.openrouter"
        original = "ANTHROPIC_AUTH_TOKEN=sk-or-v1-abcdefghijkl\nOPENROUTER_MODEL=v/a\n# comment\n"
        path.write_text(original, encoding="utf-8")
        with mock.patch.object(dash.Path, "read_text", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                dash._write_env(path, {"OPENROUTER_MODEL": "v/b"})
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / ".env.openrouter.bak").exists())

    def test_a_non_utf8_settings_file_is_read_without_raising(self):
        path = self.root / ".env.openrouter"
        path.write_bytes(b"OPENROUTER_MODEL=v/a\n# \xff\xfe broken comment\n")
        values = dash.read_env(path)
        self.assertEqual(values.get("OPENROUTER_MODEL"), "v/a")
        with mock.patch.object(dash, "CONFIG_FILES", {
                "openrouter": {"path": path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                               "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                               "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}}):
            snapshot = dash.config_snapshot()  # must not raise
        self.assertEqual(snapshot["openrouter"]["fields"]["OPENROUTER_MODEL"]["value"], "v/a")

    def test_saving_only_a_new_key_keeps_the_stored_model(self):
        path = self.root / ".env.openrouter"
        path.write_text("ANTHROPIC_AUTH_TOKEN=sk-or-v1-aaaaaaaaaaaa\nOPENROUTER_MODEL=v/a\n",
                        encoding="utf-8")
        with mock.patch.object(dash, "CONFIG_FILES", {
                "openrouter": {"path": path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                               "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                               "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}}):
            saved = dash.save_config("openrouter", {"ANTHROPIC_AUTH_TOKEN": "sk-or-v1-bbbbbbbbbbbb"})
        text = path.read_text(encoding="utf-8")
        self.assertIn("sk-or-v1-bbbbbbbbbbbb", text)
        self.assertIn("OPENROUTER_MODEL=v/a", text)
        self.assertTrue(saved["openrouter"]["fields"]["ANTHROPIC_AUTH_TOKEN"]["set"])

    # --- transcript scanning --------------------------------------------------
    def test_a_malformed_record_does_not_swallow_the_next_one(self):
        projects = self.root / "projects"
        (projects / "p").mkdir(parents=True)
        transcript = projects / "p" / "s.jsonl"
        good = {"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"model": "vendor/model", "usage": {"input_tokens": 7, "output_tokens": 3}}}
        transcript.write_text(
            json.dumps({"type": "assistant", "message": "oops"}) + "\n" + json.dumps(good) + "\n",
            encoding="utf-8")
        dash._SESSIONS.clear()
        usage = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(usage["totals"]["external"][0], 7)
        self.assertEqual(usage["totals"]["external"][1], 3)

    def test_a_bad_token_count_is_skipped_without_double_counting(self):
        projects = self.root / "projects"
        (projects / "p").mkdir(parents=True)
        transcript = projects / "p" / "s.jsonl"
        good = {"type": "assistant", "timestamp": "2026-10-25T01:00:00+00:00",
                "message": {"model": "vendor/model", "usage": {"input_tokens": 5, "output_tokens": 2}}}
        bad = {"type": "assistant", "timestamp": "2026-10-25T01:00:01+00:00",
               "message": {"model": "vendor/model", "usage": {"input_tokens": "many"}}}
        transcript.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n" + json.dumps(good) + "\n",
                              encoding="utf-8")
        dash._SESSIONS.clear()
        first = dash.scan_sessions(projects=projects, now=datetime.now())
        second = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(first["totals"]["external"][0], 10)
        self.assertEqual(second["totals"]["external"][0], 10, "a rescan must not double count")

    # --- usage cache ----------------------------------------------------------
    def transcript(self, name, records):
        projects = self.root / "projects" / "p"
        projects.mkdir(parents=True, exist_ok=True)
        path = projects / name
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return self.root / "projects"

    def assistant(self, tokens, stamp=None):
        return {"type": "assistant", "timestamp": stamp or datetime.now(timezone.utc).isoformat(),
                "message": {"model": "vendor/model",
                            "usage": {"input_tokens": tokens, "output_tokens": 1}}}

    def write_transcript(self, path, records, mtime_ns, partial=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records) + partial,
                        encoding="utf-8")
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def recent_mtime_ns(self, seconds_ago):
        # Inside the 7-day scan window, while still distinct between rewrites.
        return int((time.time() - seconds_ago) * 1_000_000_000)

    def scan_usage(self, cache, restart=False, save=False):
        """Scan the fixture projects with the repo's cache file patched out."""
        if restart:
            dash._SESSIONS.clear()
            dash._USAGE_CACHE["data"] = None
        dash._USAGE_CACHE["key"] = None
        projects = self.root / "projects"
        with mock.patch.object(dash, "USAGE_CACHE_FILE", cache):
            if not save:
                return dash.scan_sessions(projects=projects, now=datetime.now())
            # Force the throttled save so the restore path can be exercised.
            with mock.patch.object(dash, "_USAGE_CACHE_META", {"saved_at": 0.0}), \
                 mock.patch.object(dash.time, "monotonic", return_value=1_000_000.0):
                return dash.scan_sessions(projects=projects, now=datetime.now())

    def test_usage_cache_avoids_rereading_unchanged_transcripts(self):
        def record(tokens):
            return {"type": "assistant", "timestamp": "2026-10-25T01:00:00+00:00",
                    "message": {"model": "vendor/model", "usage": {"input_tokens": tokens, "output_tokens": 1}}}
        projects = self.transcript("s1.jsonl", [record(5), record(7)])
        cache = self.root / ".usage-cache.json"
        with mock.patch.object(dash, "USAGE_CACHE_FILE", cache), \
             mock.patch.dict(dash._USAGE_CACHE_META, {"saved_at": 0.0}, clear=True):
            first = dash.scan_sessions(projects=projects, now=datetime.now())
            self.assertTrue(cache.is_file(), "the scan must persist its aggregates")
            dash._SESSIONS.clear()
            dash._USAGE_CACHE["key"] = None
            sizes = {}
            real_read = dash._read_appended

            def spy(path, state):
                sizes[path.name] = (state["offset"], path.stat().st_size)
                return real_read(path, state)

            with mock.patch.object(dash, "_read_appended", side_effect=spy):
                second = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(second["totals"]["external"][0], 12)
        self.assertEqual(sizes["s1.jsonl"][0], sizes["s1.jsonl"][1],
                         "the cached offset must already be at the end of the file")

    def test_usage_cache_reads_only_the_new_tail(self):
        def record(tokens):
            return {"type": "assistant", "timestamp": "2026-10-25T01:00:00+00:00",
                    "message": {"model": "vendor/model", "usage": {"input_tokens": tokens, "output_tokens": 1}}}
        projects = self.transcript("s1.jsonl", [record(5)])
        cache = self.root / ".usage-cache.json"
        with mock.patch.object(dash, "USAGE_CACHE_FILE", cache):
            dash.scan_sessions(projects=projects, now=datetime.now())
            path = projects / "p" / "s1.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record(3)) + "\n")
            dash._SESSIONS.clear()
            dash._USAGE_CACHE["key"] = None
            usage = dash.scan_sessions(projects=projects, now=datetime.now())
        self.assertEqual(usage["totals"]["external"][0], 8, "the appended record must be counted once")

    def test_a_corrupt_or_old_usage_cache_falls_back_to_a_full_scan(self):
        def record(tokens):
            return {"type": "assistant", "timestamp": "2026-10-25T01:00:00+00:00",
                    "message": {"model": "vendor/model", "usage": {"input_tokens": tokens, "output_tokens": 1}}}
        projects = self.transcript("s1.jsonl", [record(4)])
        cache = self.root / ".usage-cache.json"
        with mock.patch.object(dash, "USAGE_CACHE_FILE", cache):
            for content in ("not json", json.dumps({"version": 1, "files": {}}),
                            json.dumps({"version": dash.USAGE_CACHE_VERSION, "files": {"x": {"offset": -1}}})):
                cache.write_text(content, encoding="utf-8")
                dash._SESSIONS.clear()
                dash._USAGE_CACHE["key"] = None
                usage = dash.scan_sessions(projects=projects, now=datetime.now())
                self.assertEqual(usage["totals"]["external"][0], 4, content[:20])

    # --- usage cache: source identity and recovery ---------------------------
    def test_an_in_place_rewrite_of_the_same_size_replaces_the_totals(self):
        # Pins: a same-byte-count rewrite used to look like "no new data" because only
        # the offset was persisted, so the old counts survived until the process died.
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(111)], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True)
        self.assertEqual(first["totals"]["external"], [111, 1, 0, 0])
        size = path.stat().st_size
        self.write_transcript(path, [self.assistant(222)], self.recent_mtime_ns(200))
        self.assertEqual(path.stat().st_size, size, "the rewrite must keep the byte count")
        second = self.scan_usage(cache)
        self.assertEqual(second["totals"]["external"], [222, 1, 0, 0])

    def test_a_same_size_rewrite_is_detected_after_a_restart(self):
        # Pins: the persisted state carried no size/mtime/fingerprint, so a cold start
        # trusted a stale offset at the same byte count and kept the old totals.
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(111)], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(first["totals"]["external"], [111, 1, 0, 0])
        cached = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(cached["version"], dash.USAGE_CACHE_VERSION)
        entry = cached["files"][str(path)]
        self.assertEqual(entry["offset"], path.stat().st_size)
        self.assertEqual(entry["size"], path.stat().st_size)
        self.assertEqual(entry["mtime_ns"], path.stat().st_mtime_ns)
        self.assertRegex(entry["head_sha"], r"^[0-9a-f]{64}$")
        self.assertRegex(entry["tail_sha"], r"^[0-9a-f]{64}$")
        # An untouched transcript costs no fingerprint reads on restore.
        with mock.patch.object(dash, "_source_identity", wraps=dash._source_identity) as identity:
            restored = self.scan_usage(cache, restart=True)
        identity.assert_not_called()
        self.assertEqual(restored["totals"]["external"], [111, 1, 0, 0])
        size = path.stat().st_size
        self.write_transcript(path, [self.assistant(222)], self.recent_mtime_ns(200))
        self.assertEqual(path.stat().st_size, size)
        after = self.scan_usage(cache, restart=True)
        self.assertEqual(after["totals"]["external"], [222, 1, 0, 0])

    def test_a_transcript_rewritten_larger_is_rescanned_not_appended(self):
        # Pins: growing the file made the old offset an "append" point, so the new
        # content was half-parsed while the old counts were still added on top.
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(100)], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True)
        self.assertEqual(first["totals"]["external"], [100, 1, 0, 0])
        old_size = path.stat().st_size
        self.write_transcript(path, [self.assistant(1000), self.assistant(500)],
                              self.recent_mtime_ns(200))
        self.assertGreater(path.stat().st_size, old_size)
        second = self.scan_usage(cache)
        self.assertEqual(second["totals"]["external"], [1500, 2, 0, 0])

    def test_a_truncated_transcript_drops_the_counts_that_are_gone(self):
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(111), self.assistant(222)],
                              self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True)
        self.assertEqual(first["totals"]["external"], [333, 2, 0, 0])
        self.write_transcript(path, [self.assistant(50)], self.recent_mtime_ns(200))
        second = self.scan_usage(cache)
        self.assertEqual(second["totals"]["external"], [50, 1, 0, 0])

    def test_a_truncated_then_regrown_transcript_is_rescanned_from_the_start(self):
        # Pins: truncating and regrowing past the old offset made the fresh bytes look
        # like an append to the old offset, mixing the two generations together.
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(300), self.assistant(300)],
                              self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True)
        self.assertEqual(first["totals"]["external"], [600, 2, 0, 0])
        old_size = path.stat().st_size
        self.write_transcript(path, [self.assistant(7)] * 40, self.recent_mtime_ns(200))
        self.assertGreaterEqual(path.stat().st_size, old_size + 1)
        second = self.scan_usage(cache)
        self.assertEqual(second["totals"]["external"], [280, 40, 0, 0])

    def test_a_partial_trailing_line_is_counted_once_after_it_completes(self):
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        complete = [self.assistant(5), self.assistant(7)]
        record = json.dumps(self.assistant(11))
        self.write_transcript(path, complete, self.recent_mtime_ns(300), partial=record[:40])
        first = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(first["totals"]["external"], [12, 2, 0, 0])
        restored = self.scan_usage(cache, restart=True)
        self.assertEqual(restored["totals"]["external"], [12, 2, 0, 0],
                         "the partial line must not be counted twice")
        self.write_transcript(path, complete, self.recent_mtime_ns(200), partial=record + "\n")
        final = self.scan_usage(cache, restart=True)
        self.assertEqual(final["totals"]["external"], [23, 3, 0, 0])

    def test_old_or_corrupt_usage_caches_are_never_trusted(self):
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(4)], self.recent_mtime_ns(300))
        stale = {"offset": path.stat().st_size, "turns": 9, "external": 9, "plan": 0,
                 "tokens": {"external": [9, 9, 0, 0], "plan": [0, 0, 0, 0]},
                 "last_ts": None, "models": {}, "hours": {}}
        payloads = ["{not json", "[]",
                    json.dumps({"version": 2, "files": {str(path): stale}}),
                    json.dumps({"version": dash.USAGE_CACHE_VERSION, "files": [str(path)]})]
        for payload in payloads:
            cache.write_text(payload, encoding="utf-8")
            usage = self.scan_usage(cache, restart=True)
            self.assertEqual(usage["totals"]["external"], [4, 1, 0, 0], payload[:40])

    def test_a_cache_without_a_usable_identity_is_rescanned(self):
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(4)], self.recent_mtime_ns(300))
        size = path.stat().st_size
        base = {"offset": size, "size": size, "mtime_ns": path.stat().st_mtime_ns,
                "head_sha": "0" * 64, "tail_sha": "0" * 64,
                "turns": 9, "external": 9, "plan": 0,
                "tokens": {"external": [9, 9, 0, 0], "plan": [0, 0, 0, 0]},
                "last_ts": None, "models": {}, "hours": {}}
        broken = [{"size": None}, {"size": "4000"}, {"size": size - 1},
                  {"mtime_ns": None}, {"mtime_ns": "now"},
                  {"head_sha": None}, {"head_sha": "abc"}, {"tail_sha": 12345}]
        entries = []
        for override in broken:
            entry = dict(base)
            entry.update(override)
            entries.append(entry)
        # A field that is absent is as unusable as a malformed one.
        for missing in ("size", "mtime_ns", "head_sha", "tail_sha"):
            entry = dict(base)
            del entry[missing]
            entries.append(entry)
        for entry in entries:
            cache.write_text(json.dumps({"version": dash.USAGE_CACHE_VERSION,
                                         "files": {str(path): entry}}), encoding="utf-8")
            usage = self.scan_usage(cache, restart=True)
            self.assertEqual(usage["totals"]["external"], [4, 1, 0, 0], str(entry)[:60])

    def test_a_restored_then_appended_transcript_counts_each_record_once(self):
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(4)], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(first["totals"]["external"], [4, 1, 0, 0])
        self.assertTrue(cache.is_file())
        self.write_transcript(path, [self.assistant(4), self.assistant(6)],
                              self.recent_mtime_ns(200))
        second = self.scan_usage(cache, restart=True)
        self.assertEqual(second["totals"]["external"], [10, 2, 0, 0])
        third = self.scan_usage(cache, restart=True)
        self.assertEqual(third["totals"]["external"], [10, 2, 0, 0])

    def test_a_cache_with_malformed_nested_totals_is_rescanned_instead_of_crashing(self):
        # Pins: a corrupt hour bucket or model count reached the merge/sort loop and
        # raised TypeError, killing the whole dashboard refresh instead of rescanning.
        path = self.root / "projects" / "p" / "s.jsonl"
        cache = self.root / ".usage-cache.json"
        self.write_transcript(path, [self.assistant(4)], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(first["totals"]["external"], [4, 1, 0, 0])
        payload = json.loads(cache.read_text(encoding="utf-8"))
        entry = payload["files"][str(path)]
        bad_hours = {datetime.now().strftime("%Y-%m-%dT%H"):
                     {"external": {"turns": "many", "tokens": [1, 2, 3, 4]},
                      "plan": {"turns": 0, "tokens": [0, 0, 0, 0]}}}
        bad_models = {"vendor/model": "many"}
        for broken in ({"hours": bad_hours}, {"models": bad_models}):
            damaged = dict(entry)
            damaged.update(broken)
            payload["files"][str(path)] = damaged
            cache.write_text(json.dumps(payload), encoding="utf-8")
            usage = self.scan_usage(cache, restart=True)
            self.assertEqual(usage["totals"]["external"], [4, 1, 0, 0], str(broken)[:30])

    def test_identity_reads_are_bounded_and_windowed(self):
        path = self.root / "big.jsonl"
        path.write_bytes(b"a" * 20000)

        class CountingHandle:
            def __init__(self, handle):
                self.handle, self.total = handle, 0

            def seek(self, position):
                return self.handle.seek(position)

            def read(self, count=-1):
                data = self.handle.read(count)
                self.total += len(data)
                return data

        with path.open("rb") as raw:
            handle = CountingHandle(raw)
            head_sha, tail_sha = dash._source_identity(handle, 20000)
            self.assertLessEqual(handle.total, 2 * dash.USAGE_IDENTITY_BYTES,
                                 "identity must not read the whole transcript")
        # A change strictly between the two windows is out of scope (documented limitation).
        path.write_bytes(b"a" * 5000 + b"b" * 10000 + b"a" * 5000)
        with path.open("rb") as raw:
            self.assertEqual(dash._source_identity(raw, 20000), (head_sha, tail_sha))
        path.write_bytes(b"c" + b"a" * 19999)
        with path.open("rb") as raw:
            self.assertNotEqual(dash._source_identity(raw, 20000)[0], head_sha)
        path.write_bytes(b"a" * 19999 + b"c")
        with path.open("rb") as raw:
            self.assertNotEqual(dash._source_identity(raw, 20000)[1], tail_sha)

    # --- route log concurrency -----------------------------------------------
    def test_concurrent_reads_do_not_double_count(self):
        log = self.root / ".route.log"
        lines = [f"2026-10-25T0{index % 9}:00:00+00:00 "
                 + json.dumps({"event": "route_selected", "provider": "openrouter",
                               "reason": "replacement"}) + "\n" for index in range(50)]
        log.write_text("".join(lines), encoding="utf-8")
        with mock.patch.object(dash, "_ROUTE_TAIL",
                               {"path": None, "events": [], "offset": 0, "digest": None}):
            results = []

            def worker():
                for _ in range(10):
                    results.append(len(dash.parse_route_log(log)))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertTrue(results)
        self.assertEqual(max(results), 50, "duplicate lines were appended by concurrent readers")

    # --- caches ---------------------------------------------------------------
    def test_pricing_cache_is_keyed_by_model(self):
        payload = {"data": [{"id": "a/a", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                            {"id": "b/b", "pricing": {"prompt": "0.00001", "completion": "0.00002"}}]}
        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def read(self, size=-1):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout=8):
                self.calls += 1
                return FakeResponse(json.dumps(payload).encode("utf-8"))

        opener = FakeOpener()
        cache = {}
        with mock.patch.object(dash.urllib.request, "build_opener", return_value=opener):
            first = dash.model_pricing("sk-or-v1-x", "a/a", cache=cache, now=100.0)
            second = dash.model_pricing("sk-or-v1-x", "b/b", cache=cache, now=101.0)
            third = dash.model_pricing("sk-or-v1-x", "b/b", cache=cache, now=102.0)
        self.assertEqual(first["prompt"], 1e-06)
        self.assertEqual(second["prompt"], 1e-05)
        self.assertEqual(third["prompt"], 1e-05)
        self.assertEqual(opener.calls, 2, "the second model must not reuse the first model's cache")

    # --- snapshot failure visibility -----------------------------------------
    def test_a_broken_snapshot_is_reported_instead_of_frozen(self):
        data = {"generated_at": "2026-10-25T00:00:00+00:00", "routing": {}, "usage": {}, "alerts": []}
        with mock.patch.dict(dash._SNAPSHOT, {"data": data, "error": {"message": "boom"}}, clear=True):
            payload = dash.cached_snapshot()
        self.assertEqual(payload["snapshot_error"], {"message": "boom"})
        self.assertIn("generated_at", payload)

    def test_scalar_metrics_do_not_break_the_insights(self):
        routing = {"state": "external", "last": {"age_seconds": 1},
                   "short": {"minutes": 10, "counts": {"external": 1, "plan": 0},
                             "plan_reasons": [], "failures": [], "circuit_opens": 0}}
        insights = dash.build_insights(routing, {"today": {"external": [0, 0, 0, 0]}, "cost_usd": None},
                                       {"ok": False}, None, metrics={"latencyMs": 5})
        self.assertIsInstance(insights, list)

    # --- stop safety ----------------------------------------------------------
    def test_stop_ignores_a_record_whose_port_is_closed(self):
        pid_file = self.root / ".dashboard.pid"
        pid_file.write_text(json.dumps({"pid": os.getpid(), "port": 1,
                                        "started": "2026-10-25T00:00:00+00:00"}), encoding="utf-8")
        with mock.patch.object(dash, "PID_FILE", pid_file), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = dash.main(["--stop"])
        self.assertEqual(code, 1)
        self.assertIn("stale_record", output.getvalue())
        self.assertFalse(pid_file.exists(), "a stale record must be cleared, not signalled")

    # --- transfer size --------------------------------------------------------
    def test_the_page_is_compressed_when_the_client_accepts_gzip(self):
        import gzip as gzip_module
        import threading
        import urllib.request

        server = dash.DashboardServer(("127.0.0.1", 0), dash.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"accept-encoding": "gzip"})
            with opener.open(request, timeout=15) as response:
                compressed = response.read()
                encoding = response.headers.get("content-encoding")
                self.assertEqual(response.status, 200)
                self.assertEqual(encoding, "gzip")
                self.assertEqual(response.headers.get("vary"), "accept-encoding")
            plain = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/", timeout=15).read()
            self.assertEqual(gzip_module.decompress(compressed), plain)
            self.assertLess(len(compressed), len(plain) * 0.6, "compression should be worth it")
        finally:
            server.shutdown()
            server.server_close()

    def test_a_large_route_log_is_read_from_the_tail_only(self):
        log = self.root / ".route.log"
        with log.open("w", encoding="utf-8") as handle:
            for index in range(4000):
                handle.write(f"2026-10-25T01:{index % 60:02d}:00+00:00 " + json.dumps(
                    {"event": "route_selected", "provider": "openrouter", "reason": "replacement"}) + "\n")
        with mock.patch.object(dash, "ROUTE_TAIL_BYTES", 4096):
            events = dash.parse_route_log(log, limit=5000, incremental=False)
        self.assertTrue(events)
        self.assertLessEqual(len(events), 5000)
        self.assertEqual(events[-1]["event"], "route_selected")
        # The parked offset is the real end of the file, so the next refresh stays incremental.
        self.assertEqual(dash._ROUTE_TAIL["offset"], log.stat().st_size)

    # --- rate limiting --------------------------------------------------------
    def test_write_requests_are_rate_limited_per_minute(self):
        with mock.patch.dict(dash._RATE, {"window": -1, "counts": {}}, clear=True):
            for index in range(dash.RATE_LIMIT_PER_MINUTE):
                self.assertTrue(dash._rate_ok("127.0.0.1", now=100.0), f"request {index + 1} should pass")
            self.assertFalse(dash._rate_ok("127.0.0.1", now=100.0), "the burst limit must stop here")
            self.assertTrue(dash._rate_ok("127.0.0.1", now=160.0), "a new minute starts fresh")
            self.assertTrue(dash._rate_ok("127.0.0.2", now=160.0), "clients are counted separately")

    # --- IPv6 ----------------------------------------------------------------
    def test_ipv6_hosts_are_bracketed(self):
        self.assertEqual(dash._display_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(dash._display_host("::1"), "[::1]")
        self.assertEqual(dash._display_host("[::1]"), "[::1]")


if __name__ == "__main__":
    unittest.main()
