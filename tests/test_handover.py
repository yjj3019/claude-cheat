# SPDX-License-Identifier: MIT
"""Opt-in loopback takeover with two real launchers, Node proxies and synthetic login."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]

WRAPPER = """
import shutil, sys
from pathlib import Path
import openrouterAdvisor as advisor

advisor.ROOT = Path(__file__).resolve().parent
advisor.HANDOFF_FILE = advisor.ROOT / "handoff.json"
which = shutil.which
advisor.shutil.which = lambda name: "fixture" if name == "claude" else which(name)
advisor.dual_provider_setup = lambda *args: None
advisor.check_connection = lambda *args, **kwargs: 0

def login(*args, **kwargs):
    if (advisor.ROOT / "reset-during-login").exists():
        advisor.routeConfig.Store(advisor.ROOT / advisor.routeConfig.CONFIG_NAME).reset_provider("openrouter", "credential")
    return {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}

advisor.run_owned = login
wait_for_release = advisor.wait_for_port_release

def release(port):
    released = wait_for_release(port)
    if released and (advisor.ROOT / "edit-after-drain").exists():
        # Simulate a concurrent save after the old proxy is already gone.
        (advisor.ROOT / advisor.routeConfig.CONFIG_NAME).write_text("openrouter:", encoding="utf-8")
    return released

advisor.wait_for_port_release = release
if "--takeover" in sys.argv:
    sys.exit(advisor.main(sys.argv[1:]))
with advisor.routed_session("openrouter"):
    sys.stdin.readline()  # Represents the life of the native frontend.
"""


@unittest.skipUnless(os.environ.get("CLAUDE_CHEAT_SLOW") == "1" and shutil.which("node"),
                     "requires Node and CLAUDE_CHEAT_SLOW=1")
class HandoverTests(unittest.TestCase):
    def test_takeover_rebinds_the_same_port_with_the_same_token(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for filename in ("claudeRouteProxy.mjs", "localAdmission.mjs"):
            shutil.copyfile(ROOT / filename, root / filename)
        wrapper = root / "launch.py"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        config = root / "openclaude.config.yaml"
        settings = 'openrouter:\n  model: "fixture/model"\n  api_key: "sk-or-fixture"\n'
        config.write_text(settings, encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                                  "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC"}}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        handoff = root / "handoff.json"

        def start(*args):
            proc = subprocess.Popen([sys.executable, str(wrapper), *args], cwd=root, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def stop():
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            self.addCleanup(stop)
            return proc

        def await_record(owner):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                self.assertIsNone(owner.poll(), "launcher exited before publishing its handoff")
                try:
                    record = json.loads(handoff.read_text(encoding="utf-8"))
                except (FileNotFoundError, ValueError):
                    record = {}
                if record.get("launcher_pid") == owner.pid:
                    return record
                time.sleep(.05)
            self.fail("launcher timed out while publishing its handoff")

        first = start()
        initial = await_record(first)
        self.assertEqual(initial["mode"], "ordinary")
        request = urllib.request.Request(f"http://127.0.0.1:{initial['port']}/api/hello", method="HEAD",
                                         headers={"x-local-route-token": initial["nonce"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        marker = root / "reset-during-login"
        marker.touch()
        refused = start("--takeover", "--provider", "openrouter")
        self.assertNotEqual(refused.wait(timeout=15), 0)
        self.assertEqual(json.loads(handoff.read_text(encoding="utf-8")), initial)
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        marker.unlink()
        config.write_text(settings, encoding="utf-8")
        edit = root / "edit-after-drain"
        edit.touch()
        replacement = start("--takeover", "--provider", "openrouter")
        latest = await_record(replacement)
        self.assertEqual(config.read_text(encoding="utf-8"), "openrouter:")
        edit.unlink()
        config.write_text(settings, encoding="utf-8")
        self.assertEqual(latest["port"], initial["port"])
        self.assertNotEqual(latest["proxy_pid"], initial["proxy_pid"])
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        # A second takeover must retire the intermediate owner while continuing
        # to serve the original frontend on the same authenticated endpoint.
        successor = start("--takeover", "--provider", "openrouter")
        final = await_record(successor)
        self.assertEqual(final["frontend_pid"], first.pid)
        self.assertEqual(final["frontend_started"], initial.get("launcher_started"))
        self.assertEqual(replacement.wait(timeout=15), 0)
        with opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        first.stdin.close()
        self.assertEqual(first.wait(timeout=15), 0)
        self.assertEqual(successor.wait(timeout=15), 0)
        self.assertFalse(handoff.exists())


if __name__ == "__main__":
    unittest.main()
