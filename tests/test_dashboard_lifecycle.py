"""Dashboard pid-record lifecycle: a signal is sent only to a provably matching process.

Slow-path-free and mock-only: the identity query, the port probe and os.kill are all
injected, so no real dashboard or unrelated process is ever signalled.

    python -m unittest tests.test_dashboard_lifecycle
"""
import contextlib
import importlib.util
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "dashboard_lifecycle", Path(__file__).resolve().parents[1] / "routeDashboard.py")
dash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dash)


class DashboardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pid_file = self.root / ".dashboard.pid"
        self.enterContext(mock.patch.object(dash, "PID_FILE", self.pid_file))
        self.port = dash.DEFAULT_PORT
        self.enterContext(mock.patch.object(dash, "_port_open", return_value=True))
        self.enterContext(mock.patch.object(dash, "own_tree_pids", return_value=set()))
        self.killer = self.enterContext(mock.patch.object(dash.os, "kill"))

    def write_record(self, **fields):
        record = {"pid": 4321, "port": self.port, "process_created": 111, **fields}
        self.pid_file.write_text(json.dumps(record), encoding="utf-8")
        return record

    def write_legacy_record(self, **fields):
        """A record from a version that did not persist the process identity."""
        record = self.write_record(**fields)
        record.pop("process_created")
        self.pid_file.write_text(json.dumps(record), encoding="utf-8")
        return record

    def stop(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dash.main(["--stop"])
        return code, out.getvalue(), err.getvalue()

    def assert_no_stray_files(self):
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [".dashboard.pid"])

    # --- the record ----------------------------------------------------------
    def test_written_record_carries_the_identity_and_stays_reader_compatible(self):
        with mock.patch.object(dash, "process_identity", return_value=(os.getpid(), 987654321)):
            record = dash._write_pid_record(self.port)
        self.assertEqual((record["pid"], record["port"]), (os.getpid(), self.port))
        self.assertEqual(record["process_created"], 987654321)
        self.assertIn("started", record)
        stored = dash._read_pid_record()
        self.assertEqual((stored["pid"], stored["port"], stored["process_created"]),
                         (os.getpid(), self.port, 987654321))
        self.assert_no_stray_files()

    def test_a_platform_without_identity_writes_a_legacy_compatible_record(self):
        with mock.patch.object(dash, "process_identity", return_value=None):
            record = dash._write_pid_record(self.port)
        self.assertNotIn("process_created", record)
        self.assertEqual(dash._read_pid_record()["pid"], os.getpid())
        self.assert_no_stray_files()

    # --- verdicts ------------------------------------------------------------
    def test_verify_pid_record_classifies_every_case(self):
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)):
            self.assertEqual(dash._verify_pid_record({"pid": 4321, "process_created": 111}), "verified")
            self.assertEqual(dash._verify_pid_record({"pid": 4321, "process_created": 222}), "mismatch")
            self.assertEqual(dash._verify_pid_record({"pid": 4321}), "unverifiable")
        with mock.patch.object(dash, "process_identity", return_value=None):
            self.assertEqual(dash._verify_pid_record({"pid": 4321, "process_created": 111}), "unverifiable")
        with mock.patch.object(dash, "own_tree_pids", return_value={4321}):
            self.assertEqual(dash._verify_pid_record({"pid": 4321, "process_created": 111}), "own_tree")

    # --- --stop: verified ----------------------------------------------------
    def test_verified_identity_is_stopped_exactly_once(self):
        self.write_record()
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)):
            code, out, err = self.stop()
        self.assertEqual(code, 0)
        self.killer.assert_called_once_with(4321, signal.SIGTERM)
        self.assertIn('"stopped": true', out)
        self.assertEqual(err, "")

    # --- --stop: refusals ----------------------------------------------------
    def test_a_recycled_pid_is_refused_and_reported(self):
        self.write_record()
        with mock.patch.object(dash, "process_identity", return_value=(4321, 222)):
            code, out, err = self.stop()
        self.assertEqual(code, 1)
        self.killer.assert_not_called()
        self.assertEqual(json.loads(out), {"stopped": False, "pid": 4321, "port": self.port,
                                           "reason": "identity_mismatch"})
        self.assertEqual(err, "")
        self.assertTrue(self.pid_file.exists(), "the stale record is reported, not silently deleted")
        self.assert_no_stray_files()

    def test_a_legacy_record_without_identity_is_refused(self):
        self.write_legacy_record()
        # Even a live process with a plausible identity cannot be matched without a record.
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)):
            code, out, _ = self.stop()
        self.assertEqual(code, 1)
        self.killer.assert_not_called()
        self.assertEqual(json.loads(out)["reason"], "identity_unverifiable")
        self.assertTrue(self.pid_file.exists())

    def test_an_unreadable_identity_is_refused(self):
        self.write_record()
        with mock.patch.object(dash, "process_identity", return_value=None):
            code, out, _ = self.stop()
        self.assertEqual(code, 1)
        self.killer.assert_not_called()
        self.assertEqual(json.loads(out)["reason"], "identity_unverifiable")

    def test_own_pid_and_ancestor_pids_are_refused_before_querying_them(self):
        for pid, protected in ((4321, {4321}), (os.getpid(), {os.getpid(), 4321})):
            with self.subTest(pid=pid):
                self.killer.reset_mock()
                self.write_record(pid=pid)
                with mock.patch.object(dash, "own_tree_pids", return_value=protected), \
                     mock.patch.object(dash, "process_identity") as identity:
                    code, out, _ = self.stop()
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["reason"], "own_tree_pid")
                identity.assert_not_called()
                self.killer.assert_not_called()

    # --- --stop: preserved stale behaviour -----------------------------------
    def test_a_record_whose_port_is_closed_is_cleared_without_an_identity_query(self):
        self.write_record()
        with mock.patch.object(dash, "_port_open", return_value=False), \
             mock.patch.object(dash, "process_identity") as identity:
            code, out, _ = self.stop()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "stale_record")
        identity.assert_not_called()
        self.killer.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    # --- startup hand-over path ----------------------------------------------
    def test_startup_stops_a_verified_previous_instance(self):
        self.write_record()
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)), \
             mock.patch.object(dash, "_port_open", side_effect=[True, False]):
            self.assertTrue(dash._stop_previous(self.port))
        self.killer.assert_called_once_with(4321, signal.SIGTERM)

    def test_startup_ignores_a_recycled_record_and_reports_it(self):
        self.write_record()
        err = io.StringIO()
        with mock.patch.object(dash, "process_identity", return_value=(4321, 222)), \
             contextlib.redirect_stderr(err):
            self.assertFalse(dash._stop_previous(self.port))
        self.killer.assert_not_called()
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(events, [{"event": "dashboard_previous_instance_refused", "pid": 4321,
                                   "port": self.port, "reason": "identity_mismatch"}])

    def test_startup_ignores_a_legacy_record(self):
        self.write_legacy_record()
        with mock.patch.object(dash, "process_identity", return_value=(4321, 111)), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(dash._stop_previous(self.port))
        self.killer.assert_not_called()

    def test_startup_skips_a_record_for_another_port(self):
        self.write_record(port=self.port + 1)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(dash._stop_previous(self.port))
        self.assertEqual(err.getvalue(), "")
        self.killer.assert_not_called()

    def test_startup_overwrites_the_stale_record_and_leaves_nothing_behind(self):
        self.write_record()  # recycled pid from an earlier boot: unverifiable now
        observed = {}

        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = ("127.0.0.1", 4567)

            def serve_forever(self):
                observed["served_record"] = dash._read_pid_record()
                raise KeyboardInterrupt

            def server_close(self):
                observed["closed"] = True

        def identity(pid):
            return (pid, 555) if pid == os.getpid() else (4321, 222)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(dash, "process_identity", side_effect=identity), \
             mock.patch.object(dash, "_refresh_loop", lambda *args: None), \
             mock.patch.object(dash, "DashboardServer", FakeServer), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            dash.serve(port=self.port)
        self.killer.assert_not_called()
        self.assertEqual(observed["served_record"]["pid"], os.getpid())
        self.assertEqual(observed["served_record"]["process_created"], 555)
        self.assertEqual(observed["served_record"]["port"], 4567)
        self.assertTrue(observed["closed"])
        self.assertIn('"dashboard"', out.getvalue())
        self.assertIn("dashboard_previous_instance_refused", err.getvalue())
        self.assertFalse(self.pid_file.exists(), "the owner removes its record on exit")
        self.assertEqual(list(self.root.iterdir()), [], "no temp or pid file may be left behind")


if __name__ == "__main__":
    unittest.main()
