import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcherStartup as startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(startup, "ROOT", self.root))
        self.enterContext(mock.patch.object(startup.dash, "SESSION_HANDOFF_FILE", self.root / "handoff.json"))

    def git(self, args, timeout=None):
        if args[:2] == ["status", "--porcelain"]:
            return 0, "", ""
        if args[0] == "symbolic-ref":
            return 0, "main", ""
        if args[:2] == ["rev-parse", "--git-path"]:
            return 0, str(self.root / args[-1]), ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "origin/main", ""
        if args == ["rev-parse", "HEAD"]:
            return 0, "a" * 40, ""
        if args == ["rev-parse", "FETCH_HEAD"]:
            return 0, "b" * 40, ""
        if args[0] == "show":
            return 0, "1.0.0" if args[1].startswith("a") else "1.1.0", ""
        return 0, "", ""

    def test_clean_newer_fast_forward_uses_protective_merge(self):
        with mock.patch.object(startup.routeVersion, "_git", side_effect=self.git) as git:
            self.assertTrue(startup._update_locked())
        self.assertIn(mock.call(["merge", "--ff-only", "--no-overwrite-ignore", "b" * 40], timeout=15), git.call_args_list)

    def test_dirty_checkout_never_fetches(self):
        with mock.patch.object(startup.routeVersion, "_git", return_value=(0, "?? file", "")) as git:
            self.assertFalse(startup._update_locked())
        self.assertEqual(git.call_count, 1)

    def test_equal_version_is_not_applied(self):
        def git(args, timeout=None):
            return (0, "1.0.0", "") if args[0] == "show" else self.git(args, timeout)
        with mock.patch.object(startup.routeVersion, "_git", side_effect=git) as call:
            self.assertFalse(startup._update_locked())
        self.assertFalse(any(item.args[0][0] == "merge" for item in call.call_args_list))

    def test_active_session_skips_update(self):
        with mock.patch.object(startup.dash.routeConfig.Store, "_cross_process_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(startup.dash, "_live_session", return_value=({"pid": 1}, {})), \
             mock.patch.object(startup, "_update_locked") as update:
            self.assertFalse(startup.update())
        update.assert_not_called()

    def test_incomplete_marker_blocks_even_when_update_is_disabled(self):
        (self.root / ".openclaude-update-incomplete").write_text("incomplete", encoding="utf-8")
        with mock.patch.object(startup.dash.routeConfig.Store, "_cross_process_lock", return_value=contextlib.nullcontext()), \
             mock.patch.dict(os.environ, {"ROUTE_UPDATE_CHECK": "0"}):
            with self.assertRaises(startup.UnsafeUpdate):
                startup.update()

    def test_session_detected_before_merge_blocks_update(self):
        with mock.patch.object(startup.routeVersion, "_git", side_effect=self.git) as git:
            self.assertFalse(startup._update_locked(lambda: True))
        self.assertFalse(any(call.args[0][0] == "merge" for call in git.call_args_list))

    def test_branch_change_during_update_prevents_merge(self):
        branch_checks = 0
        def git(args, timeout=None):
            nonlocal branch_checks
            if args[0] == "symbolic-ref":
                branch_checks += 1
                return 0, "main" if branch_checks == 1 else "other", ""
            return self.git(args, timeout)
        with mock.patch.object(startup.routeVersion, "_git", side_effect=git) as call:
            self.assertFalse(startup._update_locked())
        self.assertFalse(any(item.args[0][0] == "merge" for item in call.call_args_list))

    def test_head_change_during_fetch_prevents_merge(self):
        head_checks = 0
        def git(args, timeout=None):
            nonlocal head_checks
            if args == ["rev-parse", "HEAD"]:
                head_checks += 1
                return 0, "a" * 40 if head_checks == 1 else "c" * 40, ""
            return self.git(args, timeout)
        with mock.patch.object(startup.routeVersion, "_git", side_effect=git) as call:
            self.assertFalse(startup._update_locked())
        self.assertFalse(any(item.args[0][0] == "merge" for item in call.call_args_list))

    def test_unreadable_or_invalid_handoff_skips_update(self):
        startup.dash.SESSION_HANDOFF_FILE.write_text('{"port": null}', encoding="utf-8")
        with mock.patch.object(startup.dash.routeConfig.Store, "_cross_process_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(startup, "_update_locked") as update:
            self.assertFalse(startup.update())
        update.assert_not_called()

    def test_partial_merge_failure_stops_launch(self):
        merged = False
        def git(args, timeout=None):
            nonlocal merged
            if args[0] == "merge":
                merged = True
                return 1, "", "failure"
            if merged and args[0] == "status":
                return 0, " M VERSION", ""
            return self.git(args, timeout)
        with mock.patch.object(startup.routeVersion, "_git", side_effect=git):
            with self.assertRaises(startup.UnsafeUpdate):
                startup._update_locked()
        self.assertTrue((self.root / ".openclaude-update-incomplete").is_file())

    def test_new_dashboard_never_takes_over_existing_process(self):
        process = mock.Mock(pid=1234)
        record = {"pid": 1234, "port": startup.dash.DEFAULT_PORT}
        with mock.patch.object(startup.dash, "_port_open", side_effect=[False, True]), \
             mock.patch.object(startup.dash, "_read_pid_record", return_value=record), \
             mock.patch.object(startup.webbrowser, "open") as browser, \
             mock.patch.object(startup.subprocess, "Popen", return_value=process) as popen:
            self.assertTrue(startup.dashboard())
        self.assertIn("--no-takeover", popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs["env"]["DASHBOARD_TOKEN"], startup.dash.TOKEN)
        browser.assert_called_once()

    def test_existing_dashboard_reused_and_unknown_port_not_opened(self):
        record = {"pid": 1, "port": startup.dash.DEFAULT_PORT}
        with mock.patch.object(startup.dash, "_port_open", return_value=True), \
             mock.patch.object(startup.dash, "_read_pid_record", return_value=record), \
             mock.patch.object(startup.dash, "_verify_pid_record", return_value="verified"), \
             mock.patch.object(startup.webbrowser, "open") as browser, \
             mock.patch.object(startup.subprocess, "Popen") as process:
            self.assertTrue(startup.dashboard())
            browser.assert_called_once()
            process.assert_not_called()
        with mock.patch.object(startup.dash, "_port_open", return_value=True), \
             mock.patch.object(startup.dash, "_read_pid_record", return_value=None), \
             mock.patch.object(startup.webbrowser, "open") as browser:
            self.assertFalse(startup.dashboard())
            browser.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "git required")
    def test_real_fast_forward_against_local_remote(self):
        remote, writer, client = (self.root / name for name in ("remote.git", "writer", "client"))
        def git(*args):
            result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30,
                                    env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
        git("init", "--bare", "--initial-branch=main", remote)
        git("clone", remote, writer)
        git("-C", writer, "config", "user.name", "Test")
        git("-C", writer, "config", "user.email", "test@example.invalid")
        (writer / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        git("-C", writer, "add", "VERSION")
        git("-C", writer, "commit", "-m", "first")
        git("-C", writer, "push", "-u", "origin", "main")
        git("clone", remote, client)
        (writer / "VERSION").write_text("1.1.0\n", encoding="utf-8")
        git("-C", writer, "commit", "-am", "next")
        git("-C", writer, "push")
        with mock.patch.object(startup, "ROOT", client), mock.patch.object(startup.routeVersion, "ROOT", client), \
             mock.patch.object(startup.routeVersion, "GIT_TIMEOUT_SECONDS", 30):
            self.assertTrue(startup._update_locked())
        self.assertEqual((client / "VERSION").read_text(encoding="utf-8").strip(), "1.1.0")


if __name__ == "__main__":
    unittest.main()
