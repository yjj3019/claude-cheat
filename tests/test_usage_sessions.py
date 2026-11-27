# SPDX-License-Identifier: MIT
"""Independent live-session pointers; all paths and credentials are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import openrouterAdvisor as route


class UsageInstanceTest(unittest.TestCase):
    def test_automatic_replacement_preserves_a_new_primary_selected_during_drain(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(route, "HANDOFF_FILE", root / "primary.json"))
        def publish(name, pid, replacing=None):
            directory = route.instance_directory(root, name)
            route.write_handoff(port=1234, nonce="fixture", proxy_pid=pid, cli_pid=os.getpid(),
                                mode="ordinary", provider="local", instance_id=name, instance_dir=directory,
                                replacing_instance=replacing)
            return directory
        a, b, new_a = (str(uuid.uuid4()) for _ in range(3))
        publish(a, 101)
        publish(b, 202)
        directory = publish(new_a, 303, replacing=a)
        self.assertEqual(route.read_handoff()["instance_id"], b)
        self.assertEqual(route.read_instance_handoff(directory)["instance_id"], new_a)
        route.clear_handoff(proxy_pid=303, instance_id=new_a)
        self.assertEqual(route.read_handoff()["instance_id"], b)
    def test_new_process_can_publish_during_old_launcher_cleanup(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "handoff.json"
        self.enterContext(patch.object(route, "HANDOFF_FILE", path))
        route.write_handoff(port=1111, nonce="old", proxy_pid=101, cli_pid=os.getpid(),
                            mode="ordinary", provider="local")
        # Start a second interpreter before pausing the cleaner after its ownership read.
        writer = """
import os, sys, openrouterAdvisor as route
print("ready", flush=True)
if sys.stdin.readline().strip() == "publish":
    route.write_handoff(port=2222, nonce="new", proxy_pid=202, cli_pid=os.getpid(),
                        mode="ordinary", provider="local")
"""
        with subprocess.Popen([sys.executable, "-c", writer], text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, env={**os.environ, "ROUTE_HANDOFF_PATH": str(path)},
                              cwd=Path(route.__file__).parent) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                original_read = Path.read_text

                def read_and_start_writer(target, *args, **kwargs):
                    data = original_read(target, *args, **kwargs)
                    if target == path:
                        child.stdin.write("publish\n")
                        child.stdin.flush()
                        try:
                            child.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass  # Publication waits for this cleaner to release the lock.
                    return data

                with patch.object(Path, "read_text", read_and_start_writer):
                    route.clear_handoff(proxy_pid=101)
                self.assertEqual(child.wait(timeout=10), 0)
            finally:
                child.stdin.close()
        self.assertEqual(route.read_handoff()["proxy_pid"], 202)
        # Also cover a later cleanup when Windows has recycled the old proxy PID.
        route.clear_handoff(proxy_pid=202, nonce="old")
        self.assertEqual(route.read_handoff()["nonce"], "new")
        route.clear_handoff(proxy_pid=202, nonce="new")
        self.assertIsNone(route.read_handoff())

    def test_late_keeper_updates_its_instance_without_replacing_a_new_global_handoff(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(route, "HANDOFF_FILE", root / "global.json"))
        self.enterContext(patch.object(route, "log_route_marker"))
        self.enterContext(patch.object(route, "port_open", return_value=False))
        first = str(uuid.uuid4())
        folder = route.instance_directory(root, first)
        route.write_handoff(port=1111, nonce="old", proxy_pid=101, cli_pid=1, mode="ordinary",
                            provider="local", instance_id=first, instance_dir=folder)
        route.write_handoff(port=2222, nonce="new", proxy_pid=202, cli_pid=2, mode="ordinary",
                            provider="local")
        route.keep_instance_alive(folder)
        self.assertEqual(route.read_handoff()["proxy_pid"], 202)
        self.assertEqual(route.read_instance_handoff(folder)["launcher_pid"], os.getpid())

    def test_two_instances_have_distinct_handoffs_and_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = str(uuid.uuid4()), str(uuid.uuid4())
            a, b = route.instance_directory(root, first), route.instance_directory(root, second)
            with patch.object(route, 'HANDOFF_FILE', root / '.session-handoff.json'):
                for name, folder, port, pid in ((first, a, 1111, 101), (second, b, 2222, 202)):
                    route.write_handoff(port=port, nonce='synthetic', proxy_pid=pid, provider='local',
                                        cli_pid=1, mode='ordinary', instance_id=name, instance_dir=folder)
                self.assertEqual(json.loads((a / 'handoff.json').read_text())['instance_id'], first)
                self.assertEqual(json.loads((b / 'handoff.json').read_text())['instance_id'], second)
                route.clear_instance_handoff(a, 202)
                self.assertTrue((a / 'handoff.json').is_file())
                route.clear_instance_handoff(a, 101)
                self.assertFalse((a / 'handoff.json').exists())
                self.assertTrue((b / 'handoff.json').is_file())
                self.assertEqual(json.loads(route.HANDOFF_FILE.read_text())['instance_id'], second)

    def test_bad_instance_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, AttributeError)):
                route.instance_directory(Path(directory), '../escape')


if __name__ == '__main__':
    unittest.main()
