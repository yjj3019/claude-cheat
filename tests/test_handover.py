"""End-to-end takeover: a second process restarts the proxy on the same port.

    $env:CLAUDE_CHEAT_SLOW = '1'
    python -m unittest tests.test_handover
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("NODE") or "node"
CONFIG = ROOT / ".env.openrouter"


def read_config():
    values = {}
    try:
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


def port_open(port, timeout=0.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_until(predicate, timeout=40.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.15)
    return False


CONFIG_VALUES = read_config()


@unittest.skipUnless(os.environ.get("CLAUDE_CHEAT_SLOW") == "1",
                     "spawns a real proxy; set CLAUDE_CHEAT_SLOW=1")
@unittest.skipUnless(CONFIG_VALUES.get("ANTHROPIC_AUTH_TOKEN") and CONFIG_VALUES.get("OPENROUTER_MODEL"),
                     "needs a configured .env.openrouter")
class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.handoff = Path(self.enterContext(tempfile.TemporaryDirectory())) / "handoff.json"
        nonce = "handover-" + os.urandom(8).hex()
        environment = {
            **os.environ,
            "OPENROUTER_API_KEY": CONFIG_VALUES["ANTHROPIC_AUTH_TOKEN"],
            "OPENROUTER_MODEL": CONFIG_VALUES["OPENROUTER_MODEL"],
            "LOCAL_ROUTE_TOKEN": nonce,
            "SONNET_PROVIDER": "openrouter",
        }
        self.proxy = subprocess.Popen([NODE, str(ROOT / "claudeRouteProxy.mjs"), "--port", "0"],
                                      env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, encoding="utf-8")
        line = self.proxy.stdout.readline()
        self.port = json.loads(line)["port"]
        self.nonce = nonce
        self.handoff.write_text(json.dumps({"port": self.port, "nonce": nonce,
                                            "proxy_pid": self.proxy.pid, "launcher_pid": os.getpid(),
                                            "provider": "openrouter"}), encoding="utf-8")

    def tearDown(self):
        if self.proxy.poll() is None:
            self.proxy.kill()
        self.proxy.wait(timeout=10)

    def hello(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/hello", method="HEAD",
                                         headers={"x-local-route-token": self.nonce})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            return response.status

    def test_takeover_rebinds_the_same_port_with_the_same_token(self):
        self.assertEqual(self.hello(), 200)

        takeover = subprocess.Popen([sys.executable, str(ROOT / "openrouterAdvisor.py"), "--takeover",
                                     "--provider", "openrouter"],
                                    env={**os.environ, "ROUTE_HANDOFF_PATH": str(self.handoff)},
                                    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
        try:
            # The old proxy must accept the handover, drain and exit.
            self.assertTrue(wait_until(lambda: self.proxy.poll() is not None, timeout=40),
                            "the old proxy did not exit")
            self.assertTrue(wait_until(lambda: port_open(self.port), timeout=40),
                            "nothing rebound the port")
            self.assertEqual(self.hello(), 200, "the same token must still work after the takeover")
            record = json.loads(self.handoff.read_text(encoding="utf-8"))
            self.assertNotEqual(record.get("proxy_pid"), self.proxy.pid, "the handoff was not refreshed")
            self.assertEqual(record.get("port"), self.port)
            self.assertIn("takeover", takeover.stdout.readline() + takeover.stdout.readline())
        finally:
            takeover.terminate()
            try:
                takeover.wait(timeout=10)
            except subprocess.TimeoutExpired:
                takeover.kill()


if __name__ == "__main__":
    unittest.main()
