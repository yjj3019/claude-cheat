"""The launcher installer is verified end to end: install, forward, remove.

The PowerShell scripts are user-facing entry points, so a broken forwarder would look
like a broken session. Runs on Windows with PowerShell and Node.js.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
FORWARDERS = {
    "OpenClaude.ps1": "OpenClaude.ps1",
    "OpenClaudeDashboard.ps1": "OpenClaudeDashboard.ps1",
    "startOpenClaude.ps1": "OpenClaude.ps1",
    "startDashboard.ps1": "OpenClaudeDashboard.ps1",
    "startClaudeOpenRouter.ps1": "OpenClaude.ps1",
    "startClaudeDashboard.ps1": "OpenClaudeDashboard.ps1",
}


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows with PowerShell required")
class InstallLauncherTests(unittest.TestCase):
    def setUp(self):
        self.target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "bin"

    def run_installer(self, *args):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(ROOT / "installLauncher.ps1"), "-Target", str(self.target), *args],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")

    def test_install_writes_every_forwarder_to_the_target(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        for name, script in FORWARDERS.items():
            path = self.target / name
            with self.subTest(forwarder=name):
                self.assertTrue(path.is_file(), f"{name} was not created")
                body = path.read_text(encoding="utf-8-sig")
                self.assertIn(str(ROOT / script), body)
                self.assertIn("@args", body)

    def test_forwarder_reaches_the_real_script(self):
        self.assertEqual(self.run_installer().returncode, 0)
        # An invalid provider is rejected by the target script's ValidateSet, which proves
        # the arguments travel through the forwarder without starting a session.
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(self.target / "OpenClaude.ps1"), "-Provider", "Nope"],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("Provider", combined)
        self.assertIn("ValidateSet", combined)

    def test_remove_deletes_every_forwarder(self):
        self.assertEqual(self.run_installer().returncode, 0)
        for name in FORWARDERS:
            self.assertTrue((self.target / name).is_file())
        result = self.run_installer("-Remove")
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        remaining = [name for name in FORWARDERS if (self.target / name).exists()]
        self.assertEqual(remaining, [])

    def test_target_may_not_be_the_checkout(self):
        # Writing forwarders over the scripts they forward to would recurse forever and
        # let -Remove delete the sources.
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(ROOT / "installLauncher.ps1"), "-Target", str(ROOT)],
                                cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be this checkout", result.stdout + result.stderr)
        self.assertIn("param([ValidateSet", (ROOT / "OpenClaude.ps1").read_text(encoding="utf-8-sig"))

    def test_a_foreign_file_is_never_overwritten_or_removed(self):
        self.target.mkdir(parents=True, exist_ok=True)
        foreign = self.target / "OpenClaude.ps1"
        foreign.write_text("# mine\n", encoding="utf-8")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertEqual(foreign.read_text(encoding="utf-8"), "# mine\n")
        self.assertIn("kept OpenClaude.ps1", result.stdout + result.stderr)
        removed = self.run_installer("-Remove")
        self.assertEqual(removed.returncode, 0, removed.stderr[-400:])
        self.assertTrue(foreign.exists(), "a user file must survive -Remove")

    def test_modified_generated_forwarder_is_preserved(self):
        self.assertEqual(self.run_installer().returncode, 0)
        path = self.target / "OpenClaude.ps1"
        modified = path.read_text(encoding="utf-8-sig") + "# user change\n"
        path.write_text(modified, encoding="utf-8")
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8-sig"), modified)
        self.assertEqual(self.run_installer("-Remove").returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8-sig"), modified)

    def test_encoding_only_change_is_preserved(self):
        self.assertEqual(self.run_installer().returncode, 0)
        path = self.target / "OpenClaude.ps1"
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        changed = raw[3:]
        path.write_bytes(changed)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual(self.run_installer("-Remove").returncode, 0)
        self.assertEqual(path.read_bytes(), changed)

    def test_install_is_idempotent(self):
        self.assertEqual(self.run_installer().returncode, 0)
        first = (self.target / "OpenClaude.ps1").read_text(encoding="utf-8-sig")
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.target / "OpenClaude.ps1").read_text(encoding="utf-8-sig"), first)


if __name__ == "__main__":
    unittest.main()
