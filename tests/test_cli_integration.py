"""Opt-in end-to-end check: one real Claude Code turn through the launcher and proxy.

    $env:CLAUDE_CHEAT_LIVE = '1'
    python -m unittest tests.test_cli_integration

It costs one small turn on the configured plan/key, so it is not part of the default suite.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_LOG = ROOT / ".route.log"


@unittest.skipUnless(os.environ.get("CLAUDE_CHEAT_LIVE") == "1",
                     "runs a real Claude Code turn; set CLAUDE_CHEAT_LIVE=1")
class CliIntegrationTests(unittest.TestCase):
    def test_one_print_mode_turn_is_routed_and_answered(self):
        before = ROUTE_LOG.read_text(encoding="utf-8").count("\n") if ROUTE_LOG.is_file() else 0
        result = subprocess.run(
            [sys.executable, str(ROOT / "openrouterAdvisor.py"), "--provider", "openrouter",
             "--print", "Reply with exactly OK"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, (result.stderr or result.stdout)[-800:])
        lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, "the CLI produced no JSON result")
        payload = json.loads(lines[-1])
        self.assertIn("OK", str(payload.get("result", "")))
        self.assertEqual(payload.get("stop_reason"), "end_turn")
        after = ROUTE_LOG.read_text(encoding="utf-8").count("\n") if ROUTE_LOG.is_file() else 0
        self.assertGreater(after, before, "no routing decision was recorded for the turn")


if __name__ == "__main__":
    unittest.main()
