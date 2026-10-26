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
    def settings_files(self, path):
        spec = {"path": path, "secret": {"ANTHROPIC_AUTH_TOKEN"},
                "required": ("ANTHROPIC_AUTH_TOKEN", "OPENROUTER_MODEL"),
                "fields": ("OPENROUTER_MODEL", "ANTHROPIC_AUTH_TOKEN")}
        return mock.patch.object(dash, "CONFIG_FILES", {"openrouter": spec})

    def test_a_read_error_never_rewrites_the_settings_file(self):
        path = self.root / "openclaude.config.yaml"
        original = 'openrouter:\n  api_key: "sk-or-v1-abcdefghijkl"\n  model: "v/a"\n# comment\n'
        path.write_text(original, encoding="utf-8")
        with mock.patch.object(dash.Path, "read_text", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                dash.routeConfig.Store(path).write_provider("openrouter", {"OPENROUTER_MODEL": "v/b"})
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "openclaude.config.yaml.bak").exists())

    def test_a_non_utf8_settings_file_is_reported_without_raising_and_never_rewritten(self):
        path = self.root / "openclaude.config.yaml"
        broken = b'openrouter:\n  model: "v/a"\n# \xff\xfe broken comment\n'
        path.write_bytes(broken)
        with self.settings_files(path):
            snapshot = dash.config_snapshot()  # must not raise
            self.assertEqual(dash.read_settings("openrouter"), {})
            with self.assertRaises(dash.routeConfig.ConfigError):
                dash.save_config("openrouter", {"OPENROUTER_MODEL": "v/b", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-bbbbbbbbbbbb"})
        self.assertEqual(snapshot["openrouter"]["error"]["code"], "config_not_utf8")
        self.assertEqual(path.read_bytes(), broken)

    def test_saving_only_a_new_key_keeps_the_stored_model(self):
        path = self.root / "openclaude.config.yaml"
        path.write_text('openrouter:\n  api_key: "sk-or-v1-aaaaaaaaaaaa"\n  model: "v/a"\n', encoding="utf-8")
        with self.settings_files(path):
            saved = dash.save_config("openrouter", {"ANTHROPIC_AUTH_TOKEN": "sk-or-v1-bbbbbbbbbbbb"})
            stored = dash.read_settings("openrouter")
        self.assertEqual(stored, {"OPENROUTER_MODEL": "v/a", "ANTHROPIC_AUTH_TOKEN": "sk-or-v1-bbbbbbbbbbbb"})
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

    def assistant_turn(self, tokens, message_id=None, request_id=None, row_uuid=None,
                       output=1, model="vendor/model", stamp=None):
        record = {"type": "assistant",
                  "timestamp": stamp or datetime.now(timezone.utc).isoformat(),
                  "message": {"model": model,
                              "usage": {"input_tokens": tokens, "output_tokens": output}}}
        if message_id is not None:
            record["message"]["id"] = message_id
        if request_id is not None:
            record["requestId"] = request_id
        if row_uuid is not None:
            record["uuid"] = row_uuid
        return record

    def write_transcript(self, path, records, mtime_ns, partial=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records) + partial,
                        encoding="utf-8")
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def recent_mtime_ns(self, seconds_ago):
        # Inside the 7-day scan window, while still distinct between rewrites.
        return int((time.time() - seconds_ago) * 1_000_000_000)

    def scan_usage(self, cache, restart=False, save=False, max_files=None):
        """Scan the fixture projects with the repo's cache file patched out."""
        if restart:
            dash._SESSIONS.clear()
            getattr(dash, "_CLAIM_OWNER", {}).clear()
            dash._USAGE_CACHE["data"] = None
        dash._USAGE_CACHE["key"] = None
        projects = self.root / "projects"
        options = {} if max_files is None else {"max_files": max_files}
        with mock.patch.object(dash, "USAGE_CACHE_FILE", cache):
            if not save:
                return dash.scan_sessions(projects=projects, now=datetime.now(), **options)
            # Force the throttled save so the restore path can be exercised.
            with mock.patch.object(dash, "_USAGE_CACHE_META", {"saved_at": 0.0}), \
                 mock.patch.object(dash.time, "monotonic", return_value=1_000_000.0):
                return dash.scan_sessions(projects=projects, now=datetime.now(), **options)

    def deferred_of(self, path):
        return dash._SESSIONS[str(path)].get("deferred", 0)

    def age_out(self, path):
        old = int((time.time() - (dash.SCAN_DAYS + 1) * 86400) * 1_000_000_000)
        os.utime(path, ns=(old, old))

    def cached_claims(self, cache):
        payload = json.loads(cache.read_text(encoding="utf-8"))
        return payload, {path: entry.get("claims", {})
                         for path, entry in payload.get("files", {}).items()}

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

    # --- usage cache: cross-file exactly-once ledger ---------------------------
    def test_a_duplicate_row_copied_to_another_transcript_is_counted_once(self):
        # Pins: each file was aggregated on its own, so /resume copies double counted.
        cache = self.root / ".usage-cache.json"
        original = self.root / "projects" / "p1" / "a.jsonl"
        copy = self.root / "projects" / "p1" / "b.jsonl"
        row = self.assistant_turn(10, message_id="msg_dup", request_id="req_dup")
        self.write_transcript(original, [row], self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True)
        self.assertEqual(first["totals"]["external"], [10, 1, 0, 0])
        copied = dict(row)
        copied["uuid"] = "copied-row-uuid"
        self.write_transcript(copy, [copied], self.recent_mtime_ns(200))
        second = self.scan_usage(cache)
        self.assertEqual(second["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(second["session_count"], 1)

    def test_a_larger_duplicate_corrects_only_the_difference(self):
        # Pins: a streamed row that grew was added again instead of correcting the claim.
        cache = self.root / ".usage-cache.json"
        original = self.root / "projects" / "p1" / "a.jsonl"
        duplicate = self.root / "projects" / "p1" / "b.jsonl"
        row = self.assistant_turn(10, message_id="msg_grow", request_id="req_grow", output=2)
        self.write_transcript(original, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 2, 0, 0])
        grown = dict(row)
        grown["message"] = dict(row["message"])
        grown["message"]["usage"] = {"input_tokens": 12, "output_tokens": 9}
        self.write_transcript(duplicate, [grown], self.recent_mtime_ns(200))
        usage = self.scan_usage(cache)
        self.assertEqual(usage["totals"]["external"], [12, 9, 0, 0])
        self.assertEqual(usage["sessions"][0]["turns"], 1)

    def test_a_smaller_duplicate_never_subtracts(self):
        cache = self.root / ".usage-cache.json"
        original = self.root / "projects" / "p1" / "a.jsonl"
        duplicate = self.root / "projects" / "p1" / "b.jsonl"
        row = self.assistant_turn(10, message_id="msg_small", request_id="req_small", output=5)
        self.write_transcript(original, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 5, 0, 0])
        shrunk = dict(row)
        shrunk["message"] = dict(row["message"])
        shrunk["message"]["usage"] = {"input_tokens": 4, "output_tokens": 2}
        self.write_transcript(duplicate, [shrunk], self.recent_mtime_ns(200))
        usage = self.scan_usage(cache)
        self.assertEqual(usage["totals"]["external"], [10, 5, 0, 0])
        self.assertEqual(usage["sessions"][0]["turns"], 1)

    def test_streamed_rows_of_one_message_are_counted_once_with_their_maxima(self):
        # Pins: real Claude Code transcripts repeat one message.id as output grows.
        cache = self.root / ".usage-cache.json"
        path = self.root / "projects" / "p1" / "s.jsonl"
        rows = [self.assistant_turn(10, message_id="msg_stream", request_id="req_stream", output=2),
                self.assistant_turn(10, message_id="msg_stream", request_id="req_stream", output=9),
                self.assistant_turn(10, message_id="msg_stream", request_id="req_stream", output=9)]
        self.write_transcript(path, rows, self.recent_mtime_ns(300))
        usage = self.scan_usage(cache, restart=True)
        self.assertEqual(usage["totals"]["external"], [10, 9, 0, 0])
        self.assertEqual(usage["sessions"][0]["turns"], 1)
        self.assertEqual(usage["sessions"][0]["models"], [("vendor/model", 1)])

    def test_two_turns_sharing_a_message_id_with_different_request_ids_both_count(self):
        # Preservation: the requestId is part of the key, so separate API calls stay separate.
        cache = self.root / ".usage-cache.json"
        first = self.root / "projects" / "p1" / "a.jsonl"
        second = self.root / "projects" / "p1" / "b.jsonl"
        self.write_transcript(first, [self.assistant_turn(10, message_id="msg_shared", request_id="req_1")],
                              self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(second, [self.assistant_turn(20, message_id="msg_shared", request_id="req_2")],
                              self.recent_mtime_ns(200))
        usage = self.scan_usage(cache)
        self.assertEqual(usage["totals"]["external"], [30, 2, 0, 0])
        self.assertEqual(usage["session_count"], 2)

    def test_a_fork_copy_with_one_new_turn_counts_only_the_new_turn(self):
        cache = self.root / ".usage-cache.json"
        parent = self.root / "projects" / "p1" / "parent.jsonl"
        fork = self.root / "projects" / "p1" / "fork.jsonl"
        history = [self.assistant_turn(10, message_id="msg_1", request_id="req_1", row_uuid="uuid-1"),
                   self.assistant_turn(20, message_id="msg_2", request_id="req_2", row_uuid="uuid-2")]
        self.write_transcript(parent, history, self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [30, 2, 0, 0])
        copied = [dict(record) for record in history]
        copied.append(self.assistant_turn(5, message_id="msg_3", request_id="req_3", row_uuid="uuid-3"))
        self.write_transcript(fork, copied, self.recent_mtime_ns(200))
        usage = self.scan_usage(cache)
        self.assertEqual(usage["totals"]["external"], [35, 3, 0, 0])
        self.assertEqual(usage["session_count"], 2)

    def test_the_message_id_fallback_dedupes_without_a_request_id(self):
        cache = self.root / ".usage-cache.json"
        first = self.root / "projects" / "p1" / "a.jsonl"
        second = self.root / "projects" / "p1" / "b.jsonl"
        self.write_transcript(first, [self.assistant_turn(10, message_id="msg_only", row_uuid="uuid-a")],
                              self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(second, [self.assistant_turn(10, message_id="msg_only", row_uuid="uuid-b")],
                              self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [10, 1, 0, 0])

    def test_the_uuid_fallback_dedupes_rows_without_a_message_id(self):
        cache = self.root / ".usage-cache.json"
        first = self.root / "projects" / "p1" / "a.jsonl"
        second = self.root / "projects" / "p1" / "b.jsonl"
        self.write_transcript(first, [self.assistant_turn(10, row_uuid="row-uuid-1")],
                              self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(second, [self.assistant_turn(10, row_uuid="row-uuid-1")],
                              self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [10, 1, 0, 0])

    def test_rows_without_any_identity_are_still_counted(self):
        # Preservation: a row with no message.id and no uuid has no key, so it counts as before.
        cache = self.root / ".usage-cache.json"
        first = self.root / "projects" / "p1" / "a.jsonl"
        second = self.root / "projects" / "p1" / "b.jsonl"
        self.write_transcript(first, [self.assistant(10), self.assistant(20)], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [30, 2, 0, 0])
        self.write_transcript(second, [self.assistant(30)], self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [60, 3, 0, 0])

    def test_the_persisted_claim_ledger_adds_nothing_on_a_restart(self):
        cache = self.root / ".usage-cache.json"
        path = self.root / "projects" / "p1" / "s.jsonl"
        row = self.assistant_turn(10, message_id="msg_persist", request_id="req_persist")
        self.write_transcript(path, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True, save=True)["totals"]["external"],
                         [10, 1, 0, 0])
        payload, claims = self.cached_claims(cache)
        self.assertEqual(payload["version"], dash.USAGE_CACHE_VERSION)
        self.assertEqual(payload["claims_evicted"], 0)
        flat = [values for file_claims in claims.values() for values in file_claims.values()]
        self.assertEqual(flat, [[10, 1, 0, 0]])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        appended = [row, self.assistant_turn(7, message_id="msg_persist2", request_id="req_persist2")]
        self.write_transcript(path, appended, self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [17, 2, 0, 0])

    def test_a_persisted_claim_corrects_a_duplicate_after_a_restart(self):
        cache = self.root / ".usage-cache.json"
        original = self.root / "projects" / "p1" / "a.jsonl"
        duplicate = self.root / "projects" / "p1" / "b.jsonl"
        row = self.assistant_turn(10, message_id="msg_restart", request_id="req_restart")
        self.write_transcript(original, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True, save=True)["totals"]["external"],
                         [10, 1, 0, 0])
        grown = dict(row)
        grown["message"] = dict(row["message"])
        grown["message"]["usage"] = {"input_tokens": 25, "output_tokens": 1}
        self.write_transcript(duplicate, [grown], self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [25, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [25, 1, 0, 0])

    def test_a_reset_transcript_releases_its_claims(self):
        # Pins: the identity reset must hand ownership back, or the re-read rows are
        # skipped as duplicates of the counts that were just cleared.
        cache = self.root / ".usage-cache.json"
        path = self.root / "projects" / "p1" / "a.jsonl"
        duplicate = self.root / "projects" / "p1" / "b.jsonl"
        row = self.assistant_turn(10, message_id="msg_reset", request_id="req_reset")
        self.write_transcript(path, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        rewritten = self.assistant_turn(12, message_id="msg_reset", request_id="req_reset")
        self.write_transcript(path, [rewritten], self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [12, 1, 0, 0])
        self.write_transcript(duplicate, [rewritten], self.recent_mtime_ns(100))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [12, 1, 0, 0])

    def test_malformed_claim_ledgers_are_rejected_and_rescanned(self):
        cache = self.root / ".usage-cache.json"
        first = self.root / "projects" / "p1" / "a.jsonl"
        second = self.root / "projects" / "p1" / "b.jsonl"
        self.write_transcript(first, [self.assistant_turn(10, message_id="msg_a", request_id="req_a")],
                              self.recent_mtime_ns(300))
        self.write_transcript(second, [self.assistant_turn(20, message_id="msg_b", request_id="req_b")],
                              self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache, restart=True, save=True)["totals"]["external"],
                         [30, 2, 0, 0])
        payload = json.loads(cache.read_text(encoding="utf-8"))
        key = str(first)
        claim_key = next(iter(payload["files"][key]["claims"]))
        damaged_variants = [
            [],
            {claim_key: [1, 2, 3]},
            {claim_key: [1, 2, 3, 4, 5]},
            {claim_key: [1, -1, 0, 0]},
            {"not-a-hex-key": [1, 2, 3, 4]},
        ]
        for claims in damaged_variants:
            damaged = json.loads(json.dumps(payload))
            damaged["files"][key]["claims"] = claims
            cache.write_text(json.dumps(damaged), encoding="utf-8")
            self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"],
                             [30, 2, 0, 0], str(claims)[:40])
        # One turn owned by two files is a corrupt ledger: rescan from scratch.
        twice_owned = json.loads(json.dumps(payload))
        twice_owned["files"][str(second)]["claims"][claim_key] = [10, 1, 0, 0]
        cache.write_text(json.dumps(twice_owned), encoding="utf-8")
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [30, 2, 0, 0])
        # More claims than the cap is oversized, not trusted.
        with mock.patch.object(dash, "USAGE_CLAIM_CAP", 0, create=True):
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"],
                             [30, 2, 0, 0])

    def test_claim_eviction_at_the_cap_keeps_the_totals_monotonic(self):
        cache = self.root / ".usage-cache.json"
        oldest = self.root / "projects" / "p1" / "oldest.jsonl"
        newest = self.root / "projects" / "p1" / "newest.jsonl"
        self.write_transcript(oldest, [self.assistant_turn(10, message_id="msg_a1", request_id="req_a1"),
                                       self.assistant_turn(10, message_id="msg_a2", request_id="req_a2")],
                              self.recent_mtime_ns(300))
        first = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(first["totals"]["external"], [20, 2, 0, 0])
        with mock.patch.object(dash, "USAGE_CLAIM_CAP", 2):
            self.write_transcript(newest, [self.assistant_turn(5, message_id="msg_b1", request_id="req_b1"),
                                           self.assistant_turn(5, message_id="msg_b2", request_id="req_b2")],
                                  self.recent_mtime_ns(200))
            second = self.scan_usage(cache, save=True)
            payload, claims = self.cached_claims(cache)
            self.assertEqual(payload["claims_evicted"], 2)
            self.assertEqual(sum(len(file_claims) for file_claims in claims.values()), 2)
            third = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(second["totals"]["external"], [30, 4, 0, 0])
        self.assertGreaterEqual(second["totals"]["external"][0], first["totals"]["external"][0])
        self.assertEqual(third["totals"]["external"], [30, 4, 0, 0])

    # --- usage cache: deferred claims -----------------------------------------
    def fixture_owner_and_copy(self, cache):
        """Owner claims the turn first; the copy is skipped and flagged deferred."""
        owner = self.root / "projects" / "p1" / "owner.jsonl"
        copy = self.root / "projects" / "p1" / "copy.jsonl"
        row = self.assistant_turn(10, message_id="msg_defer", request_id="req_defer")
        self.write_transcript(owner, [row], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(copy, [dict(row)], self.recent_mtime_ns(200))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [10, 1, 0, 0])
        return owner, copy, row

    def test_an_owner_aging_out_promotes_the_deferred_copy(self):
        # Pins: once the owner leaves the window its claimed turn was counted nowhere.
        cache = self.root / ".usage-cache.json"
        owner, copy, _row = self.fixture_owner_and_copy(cache)
        self.assertEqual(self.deferred_of(copy), 1)
        self.assertEqual(self.deferred_of(owner), 0)
        self.age_out(owner)
        promoted = self.scan_usage(cache, save=True)
        self.assertEqual(promoted["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(promoted["session_count"], 1)
        self.assertEqual(self.deferred_of(copy), 0)
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])

    def test_a_removed_owner_promotes_the_deferred_copy(self):
        cache = self.root / ".usage-cache.json"
        owner, _copy, _row = self.fixture_owner_and_copy(cache)
        owner.unlink()
        self.assertEqual(self.scan_usage(cache, save=True)["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])

    def test_an_owner_dropped_by_the_file_cap_promotes_the_deferred_copy(self):
        cache = self.root / ".usage-cache.json"
        _owner, _copy, _row = self.fixture_owner_and_copy(cache)
        # mtime order is copy then owner, so a cap of one keeps only the copy.
        self.assertEqual(self.scan_usage(cache, save=True, max_files=1)["totals"]["external"],
                         [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True, max_files=1)["totals"]["external"],
                         [10, 1, 0, 0])

    def test_an_owner_identity_rewrite_promotes_the_deferred_copy(self):
        # Pins: releasing claims on a rewrite dropped the turn because the copy was
        # already committed and never re-read.
        cache = self.root / ".usage-cache.json"
        owner, copy, _row = self.fixture_owner_and_copy(cache)
        self.write_transcript(owner, [self.assistant_turn(7, message_id="msg_new",
                                                          request_id="req_new")],
                              self.recent_mtime_ns(100))
        promoted = self.scan_usage(cache, save=True)
        self.assertEqual(promoted["totals"]["external"], [17, 2, 0, 0])
        self.assertEqual(promoted["session_count"], 2)
        self.assertEqual(self.deferred_of(copy), 0)
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [17, 2, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [17, 2, 0, 0])

    def test_the_owner_returning_after_promotion_keeps_the_turn_counted_once(self):
        cache = self.root / ".usage-cache.json"
        owner, copy, row = self.fixture_owner_and_copy(cache)
        self.age_out(owner)
        self.assertEqual(self.scan_usage(cache, save=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(owner, [dict(row)], self.recent_mtime_ns(100))
        back = self.scan_usage(cache, save=True)
        self.assertEqual(back["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.deferred_of(owner), 1)
        # When the copy leaves, the owner's deferred copy takes the turn back.
        self.age_out(copy)
        self.assertEqual(self.scan_usage(cache, save=True)["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])

    def test_only_deferred_files_are_re_read_when_the_owner_leaves(self):
        cache = self.root / ".usage-cache.json"
        owner, copy, _row = self.fixture_owner_and_copy(cache)
        unrelated = self.root / "projects" / "p1" / "unrelated.jsonl"
        self.write_transcript(unrelated, [self.assistant_turn(5, message_id="msg_unrelated",
                                                              request_id="req_unrelated")],
                              self.recent_mtime_ns(250))
        self.assertEqual(self.scan_usage(cache)["totals"]["external"], [15, 2, 0, 0])
        self.age_out(owner)
        calls, reads = {}, {}
        real_read = dash._read_appended

        def spy(path, state):
            calls[path.name] = calls.get(path.name, 0) + 1
            lines, next_offset, identity, stale = real_read(path, state)
            reads[path.name] = reads.get(path.name, 0) + (1 if lines else 0)
            return lines, next_offset, identity, stale

        with mock.patch.object(dash, "_read_appended", side_effect=spy):
            promoted = self.scan_usage(cache, save=True)
        self.assertEqual(promoted["totals"]["external"], [15, 2, 0, 0])
        self.assertEqual(calls.get("copy.jsonl"), 2, "main pass + one promotion re-read")
        self.assertEqual(calls.get("unrelated.jsonl"), 1, "unchanged file is not re-read")
        self.assertNotIn("owner.jsonl", calls)
        self.assertEqual(reads.get("copy.jsonl"), 1)
        self.assertEqual(reads.get("unrelated.jsonl"), 0)

    def test_promotion_keeps_the_deferred_files_own_turns_counted_once(self):
        # The promotion reset clears the file's own counters and claims; the re-read must
        # restore them without double counting or dropping them.
        cache = self.root / ".usage-cache.json"
        owner = self.root / "projects" / "p1" / "owner.jsonl"
        copy = self.root / "projects" / "p1" / "copy.jsonl"
        shared = self.assistant_turn(10, message_id="msg_shared_a", request_id="req_shared_a")
        own = self.assistant_turn(4, message_id="msg_copy_own", request_id="req_copy_own")
        self.write_transcript(owner, [shared], self.recent_mtime_ns(300))
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])
        self.write_transcript(copy, [dict(shared), own], self.recent_mtime_ns(200))
        both = self.scan_usage(cache)
        self.assertEqual(both["totals"]["external"], [14, 2, 0, 0])
        self.assertEqual(self.deferred_of(copy), 1)
        self.age_out(owner)
        promoted = self.scan_usage(cache, save=True)
        self.assertEqual(promoted["totals"]["external"], [14, 2, 0, 0])
        self.assertEqual(promoted["session_count"], 1)
        self.assertEqual(self.deferred_of(copy), 0)
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [14, 2, 0, 0])

    def test_a_restart_promotes_a_deferred_copy_after_the_owner_ages_out(self):
        # Pins: the deferred flag must survive in the cache so a restart can promote it.
        cache = self.root / ".usage-cache.json"
        owner, _copy, _row = self.fixture_owner_and_copy(cache)
        self.assertEqual(self.scan_usage(cache, restart=True, save=True)["totals"]["external"],
                         [10, 1, 0, 0])
        payload = json.loads(cache.read_text(encoding="utf-8"))
        deferred = {path: entry["deferred"] for path, entry in payload["files"].items()}
        self.assertEqual(sorted(deferred.values()), [0, 1])
        self.age_out(owner)
        promoted = self.scan_usage(cache, restart=True, save=True)
        self.assertEqual(promoted["totals"]["external"], [10, 1, 0, 0])
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])

    def test_a_deferred_file_that_rewrites_drops_its_pending_flag(self):
        cache = self.root / ".usage-cache.json"
        _owner, copy, _row = self.fixture_owner_and_copy(cache)
        self.write_transcript(copy, [self.assistant_turn(4, message_id="msg_copy_new",
                                                         request_id="req_copy_new")],
                              self.recent_mtime_ns(100))
        rewritten = self.scan_usage(cache, save=True)
        self.assertEqual(rewritten["totals"]["external"], [14, 2, 0, 0])
        self.assertEqual(self.deferred_of(copy), 0)

    def test_rows_without_a_claim_do_not_set_a_deferred_flag(self):
        cache = self.root / ".usage-cache.json"
        path = self.root / "projects" / "p1" / "plain.jsonl"
        self.write_transcript(path, [self.assistant(3), self.assistant(4)],
                              self.recent_mtime_ns(300))
        usage = self.scan_usage(cache, restart=True)
        self.assertEqual(usage["totals"]["external"], [7, 2, 0, 0])
        self.assertEqual(self.deferred_of(path), 0)

    def test_a_cache_with_a_broken_deferred_field_is_rescanned(self):
        cache = self.root / ".usage-cache.json"
        owner, copy, _row = self.fixture_owner_and_copy(cache)
        self.assertEqual(self.scan_usage(cache, restart=True, save=True)["totals"]["external"],
                         [10, 1, 0, 0])
        payload = json.loads(cache.read_text(encoding="utf-8"))
        keys = [str(owner), str(copy)]
        variants = [lambda entry: entry.pop("deferred", None),
                    lambda entry: entry.update({"deferred": "many"}),
                    lambda entry: entry.update({"deferred": -1}),
                    lambda entry: entry.update({"deferred": True})]
        for mutate in variants:
            damaged = json.loads(json.dumps(payload))
            for key in keys:
                entry = damaged["files"][key]
                mutate(entry)
                # Poisoned totals prove the entry was rejected, not silently trusted.
                entry["turns"] = 9
                entry["tokens"] = {"external": [9, 9, 0, 0], "plan": [0, 0, 0, 0]}
            cache.write_text(json.dumps(damaged), encoding="utf-8")
            usage = self.scan_usage(cache, restart=True)
            self.assertEqual(usage["totals"]["external"], [10, 1, 0, 0], str(damaged)[:40])
        # An older cache version is not trusted either.
        cache.write_text(json.dumps({"version": dash.USAGE_CACHE_VERSION - 1,
                                     "files": payload["files"]}), encoding="utf-8")
        self.assertEqual(self.scan_usage(cache, restart=True)["totals"]["external"], [10, 1, 0, 0])

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
