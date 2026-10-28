"""Launcher switches must not change the caller's environment after the run."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
LAUNCHER = ROOT / "OpenClaude.ps1"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows with PowerShell required")
class LauncherEnvironmentTests(unittest.TestCase):
    def run_script(self, body):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-Command", body], cwd=str(ROOT), capture_output=True, text=True,
                              timeout=180, encoding="utf-8", errors="replace")

    def test_usage_error_keeps_exit_two_without_a_powershell_error_stack(self):
        for script in (LAUNCHER, ROOT / "startOpenClaude.ps1"):
            with self.subTest(script=script.name):
                result = self.run_script(
                    "function global:python { $global:LASTEXITCODE = 2 }; "
                    f"& '{script}'; exit $LASTEXITCODE")
                self.assertEqual(result.returncode, 2, result.stderr[-400:])
                self.assertIn("exit code 2", result.stdout)
                self.assertNotIn("FullyQualifiedErrorId", result.stderr)
                self.assertNotIn("WriteErrorException", result.stderr)

    def test_runtime_failure_keeps_its_original_exit_code(self):
        result = self.run_script(
            "function global:python { $global:LASTEXITCODE = 7 }; "
            f"& '{LAUNCHER}'; exit $LASTEXITCODE")
        self.assertEqual(result.returncode, 7, result.stderr[-400:])
        self.assertIn("exited with code 7", result.stderr)

    def test_help_shows_examples_without_starting_a_session(self):
        result = self.run_script(f"& '{LAUNCHER}' -Help")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("Examples:", result.stdout)
        self.assertIn("--provider local", result.stdout)
        self.assertIn("saved default", result.stdout)
        self.assertIn("Setup: .\\OpenClaude.ps1 -Setup", result.stdout)
        self.assertNotIn("connection_check", result.stdout)

    def test_stream_switch_does_not_leak_into_the_shell(self):
        # A fast failing check exercises the script without opening a session.
        result = self.run_script(
            f"try {{ & '{LAUNCHER}' -Provider Local -CheckLocal -Stream }} catch {{ }}; "
            "if ($env:ROUTE_STREAM_PASSTHROUGH) { 'LEAKED' } else { 'clean' }")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("clean", result.stdout)
        self.assertNotIn("LEAKED", result.stdout)

    def test_stream_switch_restores_a_previous_value(self):
        result = self.run_script(
            "$env:ROUTE_STREAM_PASSTHROUGH = 'keep-me'; "
            f"try {{ & '{LAUNCHER}' -Provider Local -CheckLocal -Stream }} catch {{ }}; "
            "$env:ROUTE_STREAM_PASSTHROUGH")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("keep-me", result.stdout)

    def test_default_run_leaves_the_variable_unset(self):
        result = self.run_script(
            f"try {{ & '{LAUNCHER}' -Provider Local -CheckLocal }} catch {{ }}; "
            "if ($env:ROUTE_STREAM_PASSTHROUGH) { 'SET' } else { 'clean' }")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("clean", result.stdout)


if __name__ == "__main__":
    unittest.main()
