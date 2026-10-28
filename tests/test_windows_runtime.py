"""Windows runtime hardening: identity-checked tree kills and atomic JSON state writes.

No real process is killed here: the identity query and the taskkill primitive are
injected, and the JSON tests only touch a temporary directory.

    python -m unittest tests.test_windows_runtime
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import localProcessTree
import openrouterAdvisor as route


def rename_error(winerror, target):
    """The OSError Windows raises when a rename loses a sharing/locking race."""
    error = PermissionError(13, "Access is denied: %s" % target)
    error.winerror = winerror
    return error


@unittest.skipUnless(os.name == "nt", "Windows process identity and rename semantics")
class ProcessIdentityTests(unittest.TestCase):
    def test_process_identity_reads_pid_and_creation_time(self):
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 321

        def read_times(handle, created, exited, kernel_time, user_time):
            created._obj.dwHighDateTime, created._obj.dwLowDateTime = 7, 11
            return True

        kernel.GetProcessTimes.side_effect = read_times
        with mock.patch.object(localProcessTree, "_kernel", return_value=kernel):
            self.assertEqual(localProcessTree.process_identity(42), (42, (7 << 32) | 11))
        kernel.OpenProcess.assert_called_once_with(0x1000, False, 42)
        kernel.CloseHandle.assert_called_once_with(321)
        self.assertIsNone(localProcessTree.process_identity(mock.Mock(pid=42)))

    def test_unreadable_process_reports_no_identity(self):
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = None
        with mock.patch.object(localProcessTree, "_kernel", return_value=kernel):
            self.assertIsNone(localProcessTree.process_identity(42))
        kernel.GetProcessTimes.assert_not_called()
        kernel.CloseHandle.assert_not_called()

        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 321
        kernel.GetProcessTimes.return_value = False
        with mock.patch.object(localProcessTree, "_kernel", return_value=kernel):
            self.assertIsNone(localProcessTree.process_identity(42))
        kernel.CloseHandle.assert_called_once_with(321)

    def test_own_tree_walks_the_launcher_ancestors_without_looping(self):
        tree = {os.getpid(): 777, 777: 500, 500: 0}
        with mock.patch.object(localProcessTree, "_process_parents", return_value=tree):
            self.assertEqual(localProcessTree.own_tree_pids(), {os.getpid(), 777, 500})
        cycle = {os.getpid(): 900, 900: os.getpid()}
        with mock.patch.object(localProcessTree, "_process_parents", return_value=cycle):
            self.assertEqual(localProcessTree.own_tree_pids(), {os.getpid(), 900})
        with mock.patch.object(localProcessTree, "_process_parents", return_value={}):
            self.assertEqual(localProcessTree.own_tree_pids(), {os.getpid()})

    def test_own_tree_contains_this_launcher_for_real(self):
        self.assertIn(os.getpid(), localProcessTree.own_tree_pids())


@unittest.skipUnless(os.name == "nt", "Windows process identity and rename semantics")
class KillOwnedTreeTests(unittest.TestCase):
    def setUp(self):
        self.process = mock.Mock(pid=4321)
        self.identity = (4321, 1111)

    def kill(self, current, protected=(), failure=None, process=None):
        taskkill = mock.Mock(side_effect=failure)
        with mock.patch.object(localProcessTree, "process_identity", return_value=current), \
             mock.patch.object(localProcessTree, "own_tree_pids", return_value=set(protected)), \
             mock.patch.object(localProcessTree, "_taskkill_tree", taskkill):
            outcome = localProcessTree.kill_owned_tree(process or self.process, self.identity)
        return outcome, taskkill

    def test_verified_identity_issues_the_bounded_tree_kill(self):
        self.assertLessEqual(localProcessTree.TASKKILL_TIMEOUT_SECONDS, 5.0)
        outcome, taskkill = self.kill((4321, 1111))
        self.assertEqual(outcome, "tree")
        taskkill.assert_called_once_with(4321, localProcessTree.TASKKILL_TIMEOUT_SECONDS)
        self.process.kill.assert_called_once_with()

    def test_stale_identity_record_refuses_every_kill(self):
        for current in ((4321, 1112), (4321, 1111 + 1), (9999, 1111)):
            with self.subTest(current=current):
                self.process = mock.Mock(pid=4321)
                outcome, taskkill = self.kill(current)
                self.assertEqual(outcome, "refused")
                taskkill.assert_not_called()
                self.process.kill.assert_not_called()

    def test_unverifiable_identity_kills_the_root_only(self):
        # Either side of the comparison can be missing: capture failed, or the process
        # is gone/denied at re-check time. Both stay conservative and never widen the kill.
        for identity, current in (((4321, 1111), None), (None, (4321, 1111)), (None, None)):
            with self.subTest(identity=identity, current=current):
                self.process = mock.Mock(pid=4321)
                self.identity = identity
                outcome, taskkill = self.kill(current)
                self.assertEqual(outcome, "root")
                taskkill.assert_not_called()
                self.process.kill.assert_called_once_with()
        self.identity = (4321, 1111)

    def test_own_tree_pid_is_refused_without_ever_querying_it(self):
        process = mock.Mock(pid=os.getpid())
        with mock.patch.object(localProcessTree, "own_tree_pids", return_value={os.getpid()}), \
             mock.patch.object(localProcessTree, "process_identity") as identity, \
             mock.patch.object(localProcessTree, "_taskkill_tree") as taskkill:
            outcome = localProcessTree.kill_owned_tree(process, (os.getpid(), 1))
        self.assertEqual(outcome, "refused")
        identity.assert_not_called()
        taskkill.assert_not_called()
        process.kill.assert_not_called()

    def test_taskkill_failure_or_timeout_still_kills_the_owned_root(self):
        for failure in (subprocess.TimeoutExpired("taskkill", 5), OSError("synthetic"),
                        subprocess.CalledProcessError(1, "taskkill")):
            with self.subTest(failure=type(failure).__name__):
                self.process = mock.Mock(pid=4321)
                outcome, taskkill = self.kill((4321, 1111), failure=failure)
                self.assertEqual(outcome, "root")
                taskkill.assert_called_once()
                self.process.kill.assert_called_once_with()

    def test_taskkill_wait_is_bounded(self):
        with mock.patch.object(localProcessTree.subprocess, "run") as run:
            localProcessTree._taskkill_tree(4321)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertEqual(kwargs["timeout"], 5.0)
        self.assertFalse(kwargs["check"])

    def test_an_untraceable_process_object_is_refused(self):
        for pid in ("4321", None):
            with self.subTest(pid=pid):
                process = mock.Mock(pid=pid)
                outcome, taskkill = self.kill((4321, 1111), process=process)
                self.assertEqual(outcome, "refused")
                taskkill.assert_not_called()
                process.kill.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows process identity and rename semantics")
class RunOwnedKillTests(unittest.TestCase):
    def run_owned_timeout(self, outputs, identity_side_effect):
        process = mock.Mock(pid=4321)
        process.communicate.side_effect = outputs
        with mock.patch.object(route.subprocess, "Popen", return_value=process), \
             mock.patch.object(localProcessTree, "process_identity", side_effect=identity_side_effect), \
             mock.patch.object(localProcessTree, "own_tree_pids", return_value=set()), \
             mock.patch.object(localProcessTree, "_taskkill_tree") as taskkill:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                route.run_owned(["claude"], {}, ".", timeout=1)
        return process, taskkill, raised.exception

    def test_identity_is_captured_before_the_timeout_recheck(self):
        seen = []

        def identity(pid):
            seen.append(pid)
            return (pid, 111)

        process, taskkill, _ = self.run_owned_timeout(
            [subprocess.TimeoutExpired("claude", 1), ("", "")], identity)
        self.assertEqual(seen, [4321, 4321])
        taskkill.assert_called_once_with(4321, localProcessTree.TASKKILL_TIMEOUT_SECONDS)
        self.assertEqual(process.communicate.call_args_list[1].kwargs, {"timeout": 5})

    def test_changed_identity_refuses_the_tree_kill(self):
        seen = []

        def identity(pid):
            seen.append(pid)
            return (pid, 111) if len(seen) == 1 else (pid, 222)

        process, taskkill, _ = self.run_owned_timeout(
            [subprocess.TimeoutExpired("claude", 1), ("", "")], identity)
        taskkill.assert_not_called()
        process.kill.assert_not_called()

    def test_taskkill_timeout_still_kills_the_root_and_keeps_the_original_error(self):
        first, second = subprocess.TimeoutExpired("claude", 1), subprocess.TimeoutExpired("drain", 5)
        process, taskkill, raised = self.run_owned_timeout(
            [first, second], lambda pid: (pid, 111))
        taskkill.assert_called_once()
        process.kill.assert_called_once()
        self.assertIs(raised, first, "the reaping timeout must not mask the real failure")
        self.assertEqual(process.communicate.call_count, 2)

    def test_unverifiable_identity_kills_the_root_only(self):
        process, taskkill, _ = self.run_owned_timeout(
            [subprocess.TimeoutExpired("claude", 1), ("", "")], lambda pid: None)
        taskkill.assert_not_called()
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)


@unittest.skipUnless(os.name == "nt", "Windows process identity and rename semantics")
class AtomicJsonStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.handoff = self.directory / ".session-handoff.json"
        self.enterContext(mock.patch.object(route, "HANDOFF_FILE", self.handoff))
        self.enterContext(mock.patch.object(route.time, "sleep"))
        self.handoff.write_text(json.dumps({"port": 1111, "nonce": "stale", "proxy_pid": 1}), encoding="utf-8")
        self.temporary = self.handoff.with_suffix(self.handoff.suffix + ".tmp")

    def write_handoff(self):
        route.write_handoff(port=2222, nonce="fresh", proxy_pid=2, provider="local", cli_pid=os.getpid(), mode="ordinary")

    def assert_replaced(self):
        record = json.loads(self.handoff.read_text(encoding="utf-8"))
        self.assertEqual(record["port"], 2222)
        self.assertEqual(record["nonce"], "fresh")
        self.assertEqual(record["proxy_pid"], 2)
        self.assertEqual(record["provider"], "local")
        self.assertEqual(record["launcher_pid"], os.getpid())

    def assert_only_handoff_left(self):
        self.assertFalse(self.temporary.exists(), "a temp file was left behind")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), [".session-handoff.json"])

    def test_transient_rename_contention_is_retried_then_replaced(self):
        real_replace, attempts = os.replace, []

        def contended(source, target):
            attempts.append(Path(source).name)
            if len(attempts) == 1:
                raise rename_error(32, target)  # ERROR_SHARING_VIOLATION
            if len(attempts) == 2:
                raise rename_error(5, target)  # ERROR_ACCESS_DENIED
            real_replace(source, target)

        stderr = io.StringIO()
        with mock.patch.object(route.os, "replace", side_effect=contended), contextlib.redirect_stderr(stderr):
            self.write_handoff()
        self.assertEqual(len(attempts), 3)
        self.assert_replaced()
        self.assertEqual(stderr.getvalue(), "", "a successful retry must stay silent")
        self.assert_only_handoff_left()

    def test_lock_violation_is_retried_then_replaced(self):
        real_replace, attempts = os.replace, []

        def contended(source, target):
            attempts.append(Path(source).name)
            if len(attempts) == 1:
                raise rename_error(33, target)  # ERROR_LOCK_VIOLATION
            real_replace(source, target)

        with mock.patch.object(route.os, "replace", side_effect=contended):
            self.write_handoff()
        self.assertEqual(len(attempts), 2)
        self.assert_replaced()
        self.assert_only_handoff_left()

    def test_all_renames_failing_falls_back_to_in_place_json_with_one_diagnostic(self):
        attempts = []

        def denied(source, target):
            attempts.append(Path(source).name)
            raise rename_error(32, target)

        stderr = io.StringIO()
        with mock.patch.object(route.os, "replace", side_effect=denied), contextlib.redirect_stderr(stderr):
            self.write_handoff()
        self.assertEqual(len(attempts), route._WRITE_RETRY_ATTEMPTS)
        self.assert_replaced()
        events = [json.loads(line) for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(events, [{"event": "state_write_fallback", "file": ".session-handoff.json"}])
        self.assert_only_handoff_left()

    def test_non_contention_failure_is_not_retried(self):
        attempts = []

        def broken(source, target):
            error = OSError(2, "synthetic")
            error.winerror = 2  # ERROR_FILE_NOT_FOUND: retrying cannot help
            attempts.append(Path(source).name)
            raise error

        stderr = io.StringIO()
        with mock.patch.object(route.os, "replace", side_effect=broken), contextlib.redirect_stderr(stderr):
            self.write_handoff()
        self.assertEqual(len(attempts), 1)
        self.assert_replaced()
        # A non-retryable failure still needs the fallback, reported exactly once.
        self.assertEqual(len([line for line in stderr.getvalue().splitlines() if line.strip()]), 1)

    def test_rejected_state_is_never_partially_visible_and_temp_is_removed(self):
        with mock.patch.object(route.os, "replace", side_effect=lambda source, target: rename_error(32, target)), \
             mock.patch.object(route.Path, "write_text", side_effect=PermissionError(13, "denied")):
            self.write_handoff()  # best effort: state loss must not break a session
        self.assertFalse(self.temporary.exists())
        # The old record is intact rather than a torn rewrite; the next session replaces it.
        self.assertEqual(json.loads(self.handoff.read_text(encoding="utf-8"))["port"], 1111)

    def test_handoff_record_goes_through_the_atomic_writer(self):
        with mock.patch.object(route, "write_json_state") as writer:
            self.write_handoff()
        writer.assert_called_once()
        path, payload = writer.call_args.args
        self.assertEqual(path, self.handoff)
        self.assertEqual(payload["port"], 2222)
        self.assertEqual(payload["proxy_pid"], 2)


if __name__ == "__main__":
    unittest.main()
