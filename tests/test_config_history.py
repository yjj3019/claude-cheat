# SPDX-License-Identifier: MIT
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import routeConfig as cfg


class ConfigHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / cfg.CONFIG_NAME
        self.path.write_text('openrouter:\n  model: "old/model"\n  api_key: "secret-value"\n', encoding="utf-8")
        self.store = cfg.Store(self.path)

    def save(self, model):
        self.store.write_provider("openrouter", {"OPENROUTER_MODEL": model}, record_history=True)

    def test_history_restore_conflict_and_secret(self):
        self.save("new/model")
        result = self.store.history("test-key")
        self.assertEqual([e["values"]["model"] for e in result["entries"]], ["old/model", "new/model"])
        self.assertNotIn("secret-value", str(result))
        self.path.write_text(self.path.read_text(encoding="utf-8") + "# manual change\n", encoding="utf-8")
        with self.assertRaisesRegex(cfg.ConfigError, "history_conflict"):
            self.store.write_provider("openrouter", {}, restore_id=1, expected_token=result["token"], history_key="test-key")
        result = self.store.history("test-key")
        self.store.write_provider("openrouter", {}, restore_id=1, expected_token=result["token"], history_key="test-key")
        self.assertEqual(self.store.read_provider("openrouter")["OPENROUTER_MODEL"], "old/model")
        self.assertEqual(self.store.read_provider("openrouter")["ANTHROPIC_AUTH_TOKEN"], "secret-value")

    def test_reset_removes_provider_history_from_primary_and_backup(self):
        self.save("new/model")
        self.save("third/model")
        self.store.reset_provider("openrouter", "service")
        result = self.store.history("test-key")
        self.assertEqual(result["entries"], [])
        with self.assertRaisesRegex(cfg.ConfigError, "history_not_found"):
            self.store.write_provider("openrouter", {}, restore_id=1,
                                      expected_token=result["token"], history_key="test-key")
        self.assertNotIn(cfg._HISTORY_PREFIX, self.path.with_name(self.path.name + ".bak").read_text(encoding="utf-8"))

    def test_corrupt_history_fails_closed_and_failed_write_preserves_file(self):
        self.save("new/model")
        original = self.path.read_bytes()
        with mock.patch.object(cfg, "_replace_with_retry", return_value=False):
            with self.assertRaises(OSError):
                self.save("other/model")
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_bytes(original + b'# openclaude-history-v1: invalid\n')
        with self.assertRaisesRegex(cfg.ConfigError, "invalid_history"):
            self.save("other/model")

    def test_history_trail_is_bounded_to_the_cap(self):
        # The in-memory trim alone used to leave the older lines in the file (S4), so the
        # store must keep both the parsed trail and the file bounded at the configured cap.
        for index in range(cfg.HISTORY_MAX_ENTRIES + 25):
            self.save(f"model-{index:04d}")
        entries = self.store.history("test-key")["entries"]
        self.assertEqual(len(entries), cfg.HISTORY_MAX_ENTRIES)
        self.assertEqual(entries[0]["id"], 27)
        self.assertEqual(entries[-1]["id"], cfg.HISTORY_MAX_ENTRIES + 26)
        self.assertEqual(self.path.read_text(encoding="utf-8").count(cfg._HISTORY_PREFIX), cfg.HISTORY_MAX_ENTRIES)

    def test_concurrent_saves_have_unique_revisions(self):
        failures = []
        def save(value):
            try:
                self.save(value)
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=save, args=("model/" + str(i),)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        entries = self.store.history("test-key")["entries"]
        self.assertEqual([entry["id"] for entry in entries], list(range(1, 7)))

    def test_restore_validation_and_credential_reset(self):
        self.save("new/model")
        self.store.reset_provider("openrouter", "credential")
        result = self.store.history("test-key")
        def reject(candidate):
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", candidate)
            raise ValueError("missing_key")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "missing_key"):
            self.store.write_provider("openrouter", {}, validate=reject, restore_id=1,
                                      expected_token=result["token"], history_key="test-key")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("secret-value", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
