import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from blackbox import Blackbox, MAX_BYTES, MAX_EVENTS


class BlackboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.box = Blackbox(self.root)

    def test_status_is_read_only_before_start(self):
        self.assertFalse(self.box.status()["active"])
        self.assertFalse((self.root / ".blackbox").exists())

    def test_metadata_allows_only_fixed_safe_fields(self):
        self.box.start("metadata", {"session": {"active": True, "provider": "cli", "secret": "private"},
                                    "metrics": {"requests": 2, "failed": True, "raw": "private"},
                                    "checks": [{"component": "proxy", "status": "ok", "code": "proxy_authenticated", "message": "private"}]})
        self.box.mark({"metrics": {"requests": 3}})
        raw = self.box.path.read_text(encoding="utf-8")
        self.assertNotIn("private", raw)
        data = json.loads(raw)
        self.assertEqual(data["events"][0]["snapshot"]["requests"], 2)
        self.assertEqual(data["events"][0]["snapshot"]["source_scope"], "dashboard_proxy_session_not_verified_as_captured_claude_session")
        self.assertIsNone(data["events"][0]["snapshot"]["failed"])
        self.assertEqual(self.box.status()["incidents"], 1)

    def test_detailed_requires_explicit_mode_and_matching_id(self):
        session = self.box.start("metadata", {})["session_id"]
        with self.assertRaisesRegex(ValueError, "invalid_transcript"):
            self.box.mark({}, {"session_id": session, "text": "private"})
        self.box.stop()
        session = self.box.start("detailed", {})["session_id"]
        with self.assertRaisesRegex(ValueError, "invalid_transcript"):
            self.box.mark({}, {"session_id": "different", "text": "private"})
        self.box.mark({}, {"session_id": session, "text": 'tool output: {"api_key":"test-fixture-secret", "ANTHROPIC_API_KEY":"fixture-env-secret", "password":"sensitive value with spaces"} Bearer fake-bearer-secret'})
        raw = self.box.path.read_text(encoding="utf-8")
        self.assertNotIn("test-fixture-secret", raw)
        self.assertNotIn("fake-bearer-secret", raw)
        self.assertNotIn("fixture-env-secret", raw)
        self.assertNotIn("sensitive value with spaces", raw)
        self.assertTrue(self.box.status()["details_available"])
        self.assertNotIn("tool output", json.dumps(self.box.status()))

    def test_new_recording_does_not_report_prior_incidents(self):
        self.box.start("metadata", {})
        self.box.mark({})
        self.box.stop()
        self.box.start("metadata", {})
        self.assertEqual(self.box.status()["incidents"], 0)
        self.assertFalse(self.box.status()["details_available"])

    def test_events_bounded(self):
        self.box.start("metadata", {})
        for _ in range(MAX_EVENTS + 5):
            self.box.mark({})
        self.assertLessEqual(len(json.loads(self.box.path.read_text(encoding="utf-8"))["events"]), MAX_EVENTS)

    def test_rejects_duplicate_start_and_mark_without_active_recording(self):
        with self.assertRaisesRegex(ValueError, "not_active"):
            self.box.mark({})
        self.box.start("metadata", {})
        with self.assertRaisesRegex(ValueError, "already_active"):
            self.box.start("detailed", {})

    def test_invalid_store_fails_closed(self):
        self.box.start("metadata", {})
        self.box.path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_store"):
            self.box.mark({})

    def test_concurrent_marks_and_duplicate_start(self):
        self.box.start("metadata", {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: Blackbox(self.root).mark({}), range(30)))
        self.assertEqual(self.box.status()["incidents"], 30)
        self.box.stop()
        def attempt(_):
            try:
                Blackbox(self.root).start("metadata", {})
                return "started"
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ["started", "already_active"])

    def test_large_detail_keeps_file_bounded(self):
        session = self.box.start("detailed", {})["session_id"]
        for _ in range(34):
            self.box.mark({}, {"session_id": session, "text": "가" * (64 * 1024)})
        self.assertLessEqual(self.box.path.stat().st_size, MAX_BYTES)
        self.assertGreater(self.box.status()["discarded"], 0)

    def test_refuses_linked_state_file(self):
        self.box.start("metadata", {})
        original = self.root / "other.json"
        try:
            os.link(self.box.path, original)
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable")
        with self.assertRaisesRegex(ValueError, "unsafe_store"):
            self.box.mark({})

    def _fixture_transcript(self):
        root = self.root / "projects"
        directory = root / "project"
        directory.mkdir(parents=True)
        session = str(uuid.uuid4())
        path = directory / (session + ".jsonl")
        path.write_text("old-private-conversation\n", encoding="utf-8")
        return root, session, path

    def test_authenticated_session_records_only_new_bytes(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.assertTrue(token)
        self.assertNotIn(token, self.box.path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "invalid_registration"):
            self.box.register_launch("wrong-token", {"session_id": session, "transcript_path": str(path)})
        with self.assertRaisesRegex(ValueError, "invalid_registration"):
            self.box.register_launch(token, {"session_id": str(uuid.uuid4()), "transcript_path": str(path)})
        self.assertEqual(self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})["detail_status"], "connected")
        with self.assertRaisesRegex(ValueError, "invalid_registration"):
            self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": "new-tool-result", "api_key": "fake-secret-value",
                                     "text": '{"api_key":"fake-nested-secret"}'}) + "\n")
        result = self.box.mark({})
        saved = self.box.path.read_text(encoding="utf-8")
        self.assertTrue(result["details_available"])
        self.assertNotIn("old-private-conversation", saved)
        self.assertNotIn("fake-secret-value", saved)
        self.assertNotIn("fake-nested-secret", saved)
        self.assertIn("new-tool-result", saved)
        self.box.mark({})
        self.assertEqual(saved.count("new-tool-result"), self.box.path.read_text(encoding="utf-8").count("new-tool-result"))

    def test_registration_rejects_path_outside_project_root(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        outside = self.root / (session + ".jsonl")
        outside.write_text("outside", encoding="utf-8")
        result = self.box.register_launch(token, {"session_id": session, "transcript_path": str(outside)})
        self.assertEqual(result["detail_status"], "unavailable")
        self.assertNotIn("outside", self.box.path.read_text(encoding="utf-8"))

    def test_changed_transcript_fails_closed(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        path.write_text("x", encoding="utf-8")
        self.assertEqual(self.box.mark({})["detail_status"], "unavailable")
        self.assertFalse(self.box.status()["details_available"])

    def test_hardlinked_transcript_is_not_registered(self):
        root, session, path = self._fixture_transcript()
        try:
            os.link(path, self.root / "linked")
        except (OSError, NotImplementedError):
            self.skipTest("hard links unavailable")
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.assertEqual(self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})["detail_status"], "unavailable")

    def test_metadata_mode_never_registers_transcript(self):
        root, session, _ = self._fixture_transcript()
        self.box.start("metadata", {})
        self.assertIsNone(self.box.prepare_launch(session, root))

    def test_expired_launch_token_is_rejected(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        with patch("blackbox.time.time", return_value=100):
            token = self.box.prepare_launch(session, root)
        with patch("blackbox.time.time", return_value=221):
            with self.assertRaisesRegex(ValueError, "invalid_registration"):
                self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
            self.assertEqual(self.box.status()["detail_status"], "unavailable")
            self.assertIsNotNone(self.box.prepare_launch(str(uuid.uuid4()), root))

    def test_problem_marker_keeps_recent_bounded_bytes(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('"discarded-old-data"\n' + ('"' + "x" * 100 + '"\n') * 700 + '"latest-problem"\n')
        self.box.mark({})
        event = json.loads(self.box.path.read_text(encoding="utf-8"))["events"][-1]
        self.assertTrue(event["content_truncated"])
        self.assertIn("latest-problem", event["content"])
        self.assertNotIn("discarded-old-data", event["content"])

    def test_new_session_file_created_after_registration(self):
        root = self.root / "new-projects"
        directory = root / "new-project"
        session = str(uuid.uuid4())
        path = directory / (session + ".jsonl")
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        result = self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        self.assertEqual(result["detail_status"], "waiting")
        self.assertEqual(self.box.mark({})["detail_status"], "waiting")
        directory.mkdir(parents=True)
        path.write_text('"new-session-content"\n', encoding="utf-8")
        if not hasattr(path.stat(), "st_birthtime_ns"):
            self.assertEqual(self.box.mark({})["detail_status"], "unavailable")
        else:
            result = self.box.mark({})
            self.assertEqual(result["detail_status"], "connected")
            self.assertTrue(result["details_available"])

    def test_partial_lines_are_not_recorded(self):
        root, session, path = self._fixture_transcript()
        path.write_text("previous-incomplete", encoding="utf-8")
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('-secret-continuation\n"new-record"\nunfinished')
        self.box.mark({})
        data = self.box.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-continuation", data)
        self.assertNotIn("unfinished", data)
        self.assertIn("new-record", data)

    def test_pending_cannot_adopt_preexisting_file(self):
        root = self.root / "projects-new"
        directory = root / "project"
        directory.mkdir(parents=True)
        session = str(uuid.uuid4())
        path = directory / (session + ".jsonl")
        old = self.root / "old-file"
        old.write_text("old-user-data\n", encoding="utf-8")
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        old.rename(path)
        self.assertEqual(self.box.mark({})["detail_status"], "unavailable")
        self.assertNotIn("old-user-data", self.box.path.read_text(encoding="utf-8"))

    def test_truncation_skips_partial_secret_record(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('TOKEN=very-sensitive-prefix-' + 'X' * (64 * 1024) + '\n"complete-record"\n')
        self.box.mark({})
        event = json.loads(self.box.path.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["content"].splitlines(), ['"complete-record"'])
        self.assertTrue(event["content_truncated"])

    def test_authenticated_pending_session_does_not_expire_or_transfer_ownership(self):
        root = self.root / "new-root"
        session = str(uuid.uuid4())
        path = root / "project" / (session + ".jsonl")
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with patch("blackbox.time.time", return_value=10**12):
            self.assertEqual(self.box.status()["detail_status"], "waiting")
            self.assertIsNone(self.box.prepare_launch(str(uuid.uuid4()), root))

    def test_large_single_line_is_omitted_and_flagged(self):
        root, session, path = self._fixture_transcript()
        self.box.start("detailed", {})
        token = self.box.prepare_launch(session, root)
        self.box.register_launch(token, {"session_id": session, "transcript_path": str(path)})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('"' + 's' * (64 * 1024 + 100) + '"')
        self.box.mark({})
        event = json.loads(self.box.path.read_text(encoding="utf-8"))["events"][-1]
        self.assertNotIn("content", event)
        self.assertTrue(event["content_truncated"])
        self.assertEqual(event["content_omission"], "incomplete_or_oversized_record")


if __name__ == "__main__":
    unittest.main()
