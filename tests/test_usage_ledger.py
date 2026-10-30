"""Fixture-only tests for bounded local statistics; no real session data."""
import datetime as dt
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from usage_ledger import LedgerError, UsageLedger


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root)
        self.session = str(uuid.uuid4())
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def event(self, seq=1, **values):
        return {"instance_id": self.session, "event_seq": seq, "timestamp": self.now.isoformat(),
                "provider": "cli", "requested_model": "synthetic-model", "reported_model": None,
                "usage_measured": True, "input_tokens": 9, "output_tokens": 2,
                "status": "ok", "elapsed_ms": 15.5, **values}

    def log_path(self):
        path = self.root / ".usage-stats" / "instances" / self.session / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_empty_report_does_not_create_files(self):
        data = self.ledger.report("day", self.now)
        self.assertEqual(data["rows"], [])
        self.assertIsNone(data["collection_started"])
        self.assertEqual(data["coverage"], "collected_only")
        self.assertFalse((self.root / ".usage-stats").exists())

    def test_credential_shaped_model_ids_are_rejected_before_storage(self):
        for value in ("gsk_" + "x" * 32, "provider/AIza" + "y" * 35):
            for field in ("requested_model", "reported_model"):
                with self.subTest(field=field), self.assertRaisesRegex(LedgerError, "invalid_model"):
                    self.ledger.ingest(self.session, [self.event(**{field: value})])
        self.assertFalse(self.ledger.path.exists())

    def test_legacy_provenance_migration_preserves_and_replays(self):
        self.ledger.directory.mkdir()
        identity = uuid.UUID(self.session).hex
        legacy = self.event(provider="local", reported_model="guessed-model")
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.executescript("""
                CREATE TABLE events (instance_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
                    timestamp REAL NOT NULL, provider TEXT NOT NULL, requested_model TEXT,
                    reported_model TEXT, usage_measured INTEGER NOT NULL, input_tokens INTEGER,
                    output_tokens INTEGER, status TEXT NOT NULL, elapsed_ms REAL NOT NULL,
                    PRIMARY KEY(instance_id, event_seq));
                CREATE INDEX events_timestamp ON events(timestamp);
                CREATE TABLE cursors (instance_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                    inode TEXT NOT NULL, offset INTEGER NOT NULL, invalid_lines INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE collector_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE collector_issues (instance_id TEXT NOT NULL, code TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(instance_id, code));
                PRAGMA user_version=1;
            """)
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (identity, 1, self.now.timestamp(), "local", "synthetic-model", "guessed-model",
                        1, 9, 2, "ok", 15.5))
            db.execute("INSERT INTO cursors VALUES (?,?,?,?,?)", (identity, "1", "2", 88, 0))
        before = self.ledger.report("day", self.now)
        self.assertIsNone(before["rows"][0]["reported_model"])
        self.assertEqual(before["model_provenance_unverified"], 1)
        self.assertEqual(self.ledger.ingest(self.session, [legacy])["duplicates"], 1)
        v2 = self.event(2, provider="local", reported_model="actual-model", schema_version=2)
        self.assertEqual(self.ledger.ingest(self.session, [v2])["inserted"], 1)
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT reported_model,event_version FROM events ORDER BY event_seq").fetchall(),
                             [("guessed-model", 1), ("actual-model", 2)])
            self.assertEqual(db.execute("SELECT offset FROM cursors").fetchone()[0], 88)
        report = self.ledger.report("day", self.now)
        self.assertEqual({row["reported_model"] for row in report["rows"]}, {None, "actual-model"})
        self.assertEqual(sum(row["requests"] for row in report["rows"]), 2)
        self.assertEqual(report["model_provenance_unverified"], 1)

    def test_idempotent_and_conflicting_sequences_do_not_double_count(self):
        event = self.event()
        self.assertEqual(self.ledger.ingest(self.session, [event]), {"inserted": 1, "duplicates": 0})
        self.assertEqual(self.ledger.ingest(self.session, [event]), {"inserted": 0, "duplicates": 1})
        with self.assertRaisesRegex(LedgerError, "event_conflict"):
            self.ledger.ingest(self.session, [self.event(input_tokens=10)])
        row = self.ledger.report("day", self.now)["rows"][0]
        self.assertEqual((row["requests"], row["input_tokens"], row["reported_model"]), (1, 9, None))

    def test_missing_usage_is_not_zero_and_session_provider_are_distinct(self):
        self.ledger.ingest(self.session, [self.event(), self.event(2, usage_measured=False,
                            input_tokens=None, output_tokens=None), self.event(3, provider="plan", status="failed",
                            usage_measured=False, input_tokens=None, output_tokens=None)])
        rows = self.ledger.report("day", self.now)["rows"]
        cli = next(row for row in rows if row["provider"] == "cli")
        plan = next(row for row in rows if row["provider"] == "plan")
        self.assertEqual((cli["requests"], cli["input_samples"], cli["input_tokens"]), (2, 1, 9))
        self.assertEqual((plan["failed"], plan["input_samples"], plan["input_tokens"]), (1, 0, None))

    def test_local_day_week_month_buckets_and_cutoffs(self):
        current = self.now.astimezone()
        self.ledger.ingest(self.session, [self.event(timestamp=current.isoformat()),
                           self.event(2, timestamp=(current - dt.timedelta(days=45)).isoformat())])
        day = self.ledger.report("day", self.now)
        week = self.ledger.report("week", self.now)
        month = self.ledger.report("month", self.now)
        self.assertEqual(sum(row["requests"] for row in day["rows"]), 1)
        self.assertEqual(sum(row["requests"] for row in week["rows"]), 2)
        self.assertEqual(sum(row["requests"] for row in month["rows"]), 2)
        self.assertEqual(week["week_starts"], "monday")
        self.assertTrue(all(dt.date.fromisoformat(row["bucket"]).weekday() == 0 for row in week["rows"]))

    def test_rejects_secrets_extra_fields_and_wrong_instance_before_writing(self):
        for event in (self.event(requested_model="sk-" + "x" * 40),
                      self.event(secret="fixture"), self.event(instance_id=str(uuid.uuid4())),
                      self.event(timestamp="not-a-date"), self.event(event_seq=True),
                      self.event(usage_measured=False)):
            with self.subTest(keys=list(event)), self.assertRaises(LedgerError):
                self.ledger.ingest(self.session, [event])
        self.assertFalse(self.ledger.path.exists())

    def test_jsonl_incremental_partial_and_replay(self):
        path = self.log_path()
        first = json.dumps(self.event()).encode() + b"\n"
        second = json.dumps(self.event(2)).encode() + b"\n"
        path.write_bytes(first + second[:20])
        first_pass = self.ledger.collect()
        self.assertEqual(first_pass["events_inserted"], 1)
        self.assertTrue(first_pass["pending"])
        with path.open("ab") as f:
            f.write(second[20:])
        self.assertEqual(self.ledger.collect()["events_inserted"], 1)
        self.assertEqual(self.ledger.collect()["events_inserted"], 0)
        self.assertEqual(self.ledger.report("day", self.now)["rows"][0]["requests"], 2)

    def test_invalid_records_are_flagged_without_echo_and_valid_records_still_saved(self):
        path = self.log_path()
        path.write_bytes(b'{"credential":"PRIVATE-CONTENT"}\n' + json.dumps(self.event()).encode() + b"\n")
        result = self.ledger.collect()
        self.assertEqual((result["invalid_lines"], result["events_inserted"]), (1, 1))
        self.assertNotIn("PRIVATE-CONTENT", json.dumps(result))
        report = self.ledger.report("day", self.now)
        self.assertEqual((report["coverage"], report["invalid_lines"]), ("partial", 1))

    def test_truncation_detected_without_rewriting_source(self):
        path = self.log_path()
        path.write_text(json.dumps(self.event()) + "\n", encoding="utf-8")
        self.ledger.collect()
        path.write_text("", encoding="utf-8")
        self.assertIn("source_changed", self.ledger.collect()["errors"])
        self.assertEqual(path.read_text(encoding="utf-8"), "")
        report = self.ledger.report("day", self.now)
        self.assertEqual(report["coverage"], "partial")
        self.assertIn("source_changed", report["collection_issues"])

    def test_cursor_and_events_roll_back_together_on_failed_insert(self):
        path = self.log_path()
        path.write_text(json.dumps(self.event()) + "\n", encoding="utf-8")
        with patch.object(UsageLedger, "_insert_rows", side_effect=LedgerError("event_conflict")):
            self.assertIn("event_conflict", self.ledger.collect()["errors"])
        self.assertEqual(self.ledger.collect()["events_inserted"], 1)
        self.assertEqual(self.ledger.collect()["events_inserted"], 0)

    def test_empty_registered_session_is_not_an_error(self):
        self.log_path()  # directory exists but no request has created events.jsonl
        self.assertEqual(self.ledger.collect()["errors"], [])

    def test_round_robin_advances_past_empty_and_incomplete_sessions(self):
        base = self.root / ".usage-stats" / "instances"
        for number in range(1, 33):
            directory = base / str(uuid.UUID(int=number))
            directory.mkdir(parents=True)
            if number % 3 == 0:
                (directory / "events.jsonl").write_bytes(b'{"partial":')
            elif number % 3 == 1:
                (directory / "events.jsonl").write_bytes(b"")
        later = str(uuid.UUID(int=33))
        directory = base / later
        directory.mkdir()
        (directory / "events.jsonl").write_text(
            json.dumps(self.event(instance_id=later)) + "\n", encoding="utf-8")
        self.assertEqual(self.ledger.collect()["events_inserted"], 1)
        self.assertEqual(self.ledger.collect()["events_inserted"], 0)
        self.assertEqual(self.ledger.collect()["events_inserted"], 0)
        self.assertEqual(self.ledger.report("day", self.now)["rows"][0]["requests"], 1)

    def test_distinct_instances_are_not_merged(self):
        other = str(uuid.uuid4())
        self.ledger.ingest(self.session, [self.event()])
        self.ledger.ingest(other, [self.event(instance_id=other)])
        rows = self.ledger.report("day", self.now)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["requests"] for row in rows), 2)
        self.assertNotEqual(rows[0]["instance_id"], rows[1]["instance_id"])

    def test_discovery_limit_is_persistently_reported_as_incomplete(self):
        self.log_path().write_bytes(b"")
        base = self.root / ".usage-stats" / "instances"
        for _ in range(2):
            folder = base / str(uuid.uuid4())
            folder.mkdir()
            (folder / "events.jsonl").write_bytes(b"")
        with patch("usage_ledger.MAX_DISCOVERY", 2):
            result = self.ledger.collect()
        self.assertTrue(result["pending"])
        self.assertIn("instance_discovery_limit", result["errors"])
        report = self.ledger.report("day", self.now)
        self.assertEqual(report["coverage"], "partial")
        self.assertIn("instance_discovery_limit", report["collection_issues"])

    def test_empty_historical_directories_do_not_delay_real_event(self):
        base = self.root / ".usage-stats" / "instances"
        for number in range(1, 41):
            (base / str(uuid.UUID(int=number))).mkdir(parents=True)
        path = self.log_path()
        path.write_text(json.dumps(self.event()) + "\n", encoding="utf-8")
        result = self.ledger.collect()
        self.assertEqual(result["events_inserted"], 1)
        self.assertFalse(result["pending"])
        self.assertEqual(result["files_seen"], 1)

    def test_more_than_one_batch_is_pending_only_until_all_events_are_collected(self):
        base = self.root / ".usage-stats" / "instances"
        for number in range(1, 34):
            instance = str(uuid.UUID(int=number))
            folder = base / instance
            folder.mkdir(parents=True)
            (folder / "events.jsonl").write_text(json.dumps(self.event(instance_id=instance)) + "\n", encoding="utf-8")
        first = self.ledger.collect()
        self.assertEqual(first["events_inserted"], 32)
        self.assertTrue(first["pending"])
        second = self.ledger.collect()
        self.assertEqual(second["events_inserted"], 1)
        self.assertFalse(second["pending"])
        self.assertEqual(self.ledger.collect()["events_inserted"], 0)

    def test_total_scan_limit_marks_coverage_even_without_candidate_files(self):
        base = self.root / ".usage-stats" / "instances"
        base.mkdir(parents=True)
        for number in range(3):
            (base / f"unrelated-{number}").mkdir()
        with patch("usage_ledger.MAX_SCAN_ENTRIES", 2):
            result = self.ledger.collect()
        self.assertTrue(result["pending"])
        self.assertIn("instance_discovery_limit", result["errors"])
        self.assertEqual(self.ledger.report("day", self.now)["coverage"], "partial")

    def test_missing_sequence_is_reported_not_assumed_zero(self):
        path = self.log_path()
        path.write_text(json.dumps(self.event(1)) + "\n" + json.dumps(self.event(3)) + "\n", encoding="utf-8")
        self.assertEqual(self.ledger.collect()["events_inserted"], 2)
        report = self.ledger.report("day", self.now)
        self.assertEqual(report["coverage"], "partial")
        self.assertIn("sequence_gap", report["collection_issues"])
        self.assertEqual(report["rows"][0]["requests"], 2)

    def test_unsupported_database_not_overwritten(self):
        self.ledger.directory.mkdir()
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db:
            db.execute("CREATE TABLE untouched (value TEXT)")
            db.commit()
        with self.assertRaisesRegex(LedgerError, "unsupported_schema"):
            self.ledger.ingest(self.session, [self.event()])
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='untouched'").fetchone())

    def test_rejects_symlink_source_when_supported(self):
        path = self.log_path()
        target = self.root / "other.jsonl"
        target.write_text(json.dumps(self.event()) + "\n", encoding="utf-8")
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        result = self.ledger.collect()
        self.assertIn("unsafe_source", result["errors"])
        self.assertEqual(result["events_inserted"], 0)


if __name__ == "__main__":
    unittest.main()
