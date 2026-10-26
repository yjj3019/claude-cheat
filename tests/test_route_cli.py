"""routeCli: binary validation, shim resolution, discovery, env allowlist, safe runs and the connection test."""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import routeCli as rc  # noqa: E402

SHIM = '''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\{rel}" %*
'''
TREE_RESULT = {"exit_code": 0, "timed_out": False, "truncated": False, "elapsed_ms": 1,
               "stdout": b"", "stderr_len": 0, "stderr_head": b""}
NODE = shutil.which("node")


def setUpModule():
    global TMP, SHIM_DIR, CMD, FAKE
    if os.name != "nt" or not NODE:
        raise unittest.SkipTest("needs Windows and node")
    TMP = tempfile.TemporaryDirectory()
    SHIM_DIR = Path(TMP.name) / "a b & c %x% ^ (d)"
    FAKE = SHIM_DIR / "node_modules" / "fake" / "bin" / "codex.js"
    FAKE.parent.mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "fake_cli.js", FAKE)
    CMD = SHIM_DIR / "codex.cmd"
    CMD.write_text(SHIM.format(rel=r"node_modules\fake\bin\codex.js"), encoding="utf-8")


def tearDownModule():
    TMP.cleanup()


def set_mode(mode):
    (FAKE.parent / "mode.txt").write_text(mode)


def side_file(name):
    return FAKE.parent / name


def alive(pid):
    handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 258
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def wait_gone(pid, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end and alive(pid):
        time.sleep(0.1)
    return not alive(pid)


class FakeCase(unittest.TestCase):
    def setUp(self):
        for name in ("gpid.txt", "argv.json", "env.txt", "mode.txt"):
            side_file(name).unlink(missing_ok=True)
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

    def run_fake(self, mode, *args, **kw):
        set_mode(mode)
        kw.setdefault("timeout", 10)
        return rc.run_safely([NODE, str(FAKE), *args], cwd=self.work, env=rc.child_env("codex"), **kw)

    def skip_probe(self):
        """Node starts slowly on some machines: only tests of the probe itself spawn it."""
        patcher = mock.patch.object(rc, "probe_version", return_value={"ok": True, "version": "9.9.9", "reason": None})
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_binary(self, kind):
        """Make find/resolve return the fake script, for kinds that have no shim."""
        for target, value in (("find_binary", {"kind": kind, "path": str(FAKE), "dir": "", "name": "", "via": "PATH"}),
                              ("resolve_command", [NODE, str(FAKE)])):
            patcher = mock.patch.object(rc, target, side_effect=lambda *a, v=value, **k: v)
            patcher.start()
            self.addCleanup(patcher.stop)


class ValidateBinary(unittest.TestCase):
    def test_table(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("codex.exe", "CODEX.EXE", "codex.cmd", "codex", "codex-evil.exe", "codex2.exe",
                         "mycodex.cmd", "codex.bat", "grok.exe"):
                Path(d, name).write_text("x")
            ok = {n: rc.validate_binary("codex", os.path.join(d, n)) for n in ("codex.exe", "CODEX.EXE", "codex.cmd", "codex")}
            self.assertTrue(all(os.path.isabs(p) for p in ok.values()))
            for name in ("codex-evil.exe", "codex2.exe", "mycodex.cmd", "codex.bat", "grok.exe"):
                with self.assertRaises(rc.CliError) as ctx:
                    rc.validate_binary("codex", os.path.join(d, name))
                self.assertEqual(ctx.exception.code, "binary_name_mismatch", name)
            for path, code in (("codex.exe", "binary_not_absolute"), ("", "binary_not_absolute"),
                               (None, "binary_not_absolute"), (os.path.join(d, "nope", "codex.exe"), "binary_not_found")):
                with self.assertRaises(rc.CliError) as ctx:
                    rc.validate_binary("codex", path)
                self.assertEqual(ctx.exception.code, code)
                self.assertNotIn(d, str(ctx.exception))
            with self.assertRaises(rc.CliError):
                rc.validate_binary("other", os.path.join(d, "codex.exe"))


class ResolveCommand(unittest.TestCase):
    def shim(self, rel, name="codex.cmd", create=True):
        d = Path(tempfile.mkdtemp()) / "a b & c %x% ^ (d)"
        (d / "sub").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, d.parent, True)
        (d / name).write_text(SHIM.format(rel=rel), encoding="utf-8")
        if create:
            (d / "sub" / "t.js").write_text("x")
            (d / "sub" / "t.exe").write_text("x")
            (d.parent / "evil.js").write_text("x")
        return d

    def test_js_shim_in_special_directory(self):
        self.assertEqual([os.path.normcase(os.path.realpath(p)) for p in rc.resolve_command(str(CMD))],
                         [os.path.normcase(os.path.realpath(NODE)), os.path.normcase(os.path.realpath(FAKE))])

    def test_shim_dir_node_preferred(self):
        d = self.shim(r"sub\t.js")
        (d / "node.exe").write_text("x")
        self.assertEqual(os.path.realpath(rc.resolve_command(str(d / "codex.cmd"))[0]), os.path.realpath(d / "node.exe"))

    def test_exe_target_and_native(self):
        d = self.shim(r"sub\t.exe")
        self.assertEqual(rc.resolve_command(str(d / "codex.cmd")), [str((d / "sub" / "t.exe").resolve())])
        self.assertEqual(rc.resolve_command(r"C:\x\grok.exe"), [r"C:\x\grok.exe"])

    def test_unsupported(self):
        for rel, create in ((r"..\..\evil.js", True), (r"sub\missing.js", True), (r"sub\t.exe.txt", True)):
            d = self.shim(rel, create=create)
            (d / "sub" / "t.exe.txt").write_text("x")
            with self.assertRaises(rc.CliError) as ctx:
                rc.resolve_command(str(d / "codex.cmd"))
            self.assertEqual(ctx.exception.code, "unsupported_shim", rel)
        d = self.shim("x")
        (d / "codex.cmd").write_text("@echo hi\r\n")
        with self.assertRaises(rc.CliError) as ctx:
            rc.resolve_command(str(d / "codex.cmd"))
        self.assertEqual(ctx.exception.code, "unsupported_shim")

    def test_no_node(self):
        d = self.shim(r"sub\t.js")
        with mock.patch.object(rc.shutil, "which", return_value=None), \
                mock.patch.object(rc, "NODE_FALLBACK", str(d / "no-node.exe")):
            with self.assertRaises(rc.CliError) as ctx:
                rc.resolve_command(str(d / "codex.cmd"))
        self.assertEqual(ctx.exception.code, "node_not_found")


class FindBinary(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        self.d = {n: base / n for n in ("pin", "path", "reg", "home", "appdata")}
        for p in self.d.values():
            p.mkdir()
        (self.d["home"] / ".grok" / "bin").mkdir(parents=True)
        (self.d["appdata"] / "npm").mkdir()
        env = mock.patch.dict(os.environ, {"PATH": "", "USERPROFILE": str(self.d["home"]),
                                           "HOME": str(self.d["home"]), "APPDATA": str(self.d["appdata"])})
        env.start()
        self.addCleanup(env.stop)
        self.registry = []
        reg = mock.patch.object(rc, "_registry_path", side_effect=lambda: self.registry)
        reg.start()
        self.addCleanup(reg.stop)

    def put(self, directory, name):
        Path(directory, name).write_text("x")

    def test_precedence(self):
        self.put(self.d["pin"], "codex.exe")
        self.put(self.d["path"], "codex.exe")
        self.put(self.d["reg"], "codex.exe")
        os.environ["PATH"] = str(self.d["path"])
        self.registry = [str(self.d["reg"])]
        pinned = rc.find_binary("codex", str(self.d["pin"] / "codex.exe"))
        self.assertEqual((pinned["via"], pinned["name"], pinned["dir"]), ("pinned", "codex.exe", str(self.d["pin"])))
        self.assertEqual(rc.find_binary("codex")["via"], "PATH")
        with self.assertRaises(rc.CliError):
            rc.find_binary("codex", str(self.d["pin"] / "codex-evil.exe"))

    def test_stale_path_uses_registry(self):
        self.put(self.d["reg"], "grok.exe")
        self.registry = [str(self.d["reg"])]
        found = rc.find_binary("grok")
        self.assertEqual((found["via"], found["kind"]), ("registry-PATH", "grok"))

    def test_known_dirs_and_extension_order(self):
        self.put(self.d["home"] / ".grok" / "bin", "grok.exe")
        self.assertEqual(rc.find_binary("grok")["via"], "known-dir")
        self.put(self.d["appdata"] / "npm", "codex.cmd")
        self.assertEqual(rc.find_binary("codex")["name"], "codex.cmd")
        self.put(self.d["appdata"] / "npm", "codex.exe")
        self.assertEqual(rc.find_binary("codex")["name"], "codex.exe")
        self.assertIsNone(rc.find_binary("claude"))
        (self.d["home"] / ".local" / "bin").mkdir(parents=True)
        self.put(self.d["home"] / ".local" / "bin", "claude.exe")
        self.assertEqual(rc.find_binary("claude")["via"], "known-dir")


class ChildEnv(unittest.TestCase):
    BASE = {"Path": "p", "SystemRoot": "r", "HTTPS_PROXY": "h", "https_proxy": "h", "NODE_EXTRA_CA_CERTS": "c",
            "XDG_CONFIG_HOME": "x", "ANTHROPIC_BASE_URL": "u", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_FOO": "1",
            "OPENROUTER_API_KEY": "k", "LOCAL_X": "1", "DASHBOARD_TOKEN": "d", "ROUTE_Y": "1", "RANDOM": "1",
            "CODEX_HOME": "c", "OPENAI_API_KEY": "o", "XAI_API_KEY": "x", "GROK_THING": "g",
            "CLAUDE_CONFIG_DIR": "d", "ANTHROPIC_API_KEY": "a"}

    def test_allowlist(self):
        for kind, extra in (("codex", {"CODEX_HOME", "OPENAI_API_KEY"}), ("grok", {"XAI_API_KEY", "GROK_THING"}),
                            ("claude", {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"})):
            names = set(rc.child_env(kind, self.BASE))
            common = {"Path", "SystemRoot", "HTTPS_PROXY", "https_proxy", "NODE_EXTRA_CA_CERTS", "XDG_CONFIG_HOME"}
            self.assertEqual(names, common | extra, kind)
        self.assertNotIn("CODEX_HOME", rc.child_env("grok", self.BASE))


class RunSafely(FakeCase):
    def test_success(self):
        result = self.run_fake("ok")
        self.assertEqual((result["exit_code"], result["timed_out"], result["truncated"]), (0, False, False))
        self.assertEqual(result["stdout"].strip(), b"OK")
        self.assertEqual(result["stderr_len"], 0)

    def test_failure_reports_stderr_head_only(self):
        result = self.run_fake("nonzero")
        self.assertEqual((result["exit_code"], result["stderr_head"].strip()), (1, b"boom"))
        self.assertNotIn("stderr", result)

    def test_timeout_kills_tree(self):
        result = self.run_fake("tree_slow", timeout=5)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        grandchild = int(side_file("gpid.txt").read_text())
        self.assertTrue(wait_gone(grandchild), "grandchild survived the timeout kill")

    def test_output_cap(self):
        result = self.run_fake("flood", max_bytes=1 << 20)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), 1 << 20)
        self.assertLess(result["elapsed_ms"], 8000)

    def test_inherited_handles_do_not_hang(self):
        made = []
        real = tempfile.mkdtemp
        with mock.patch.object(rc.tempfile, "mkdtemp", side_effect=lambda **k: made.append(real(**k)) or made[-1]):
            result = self.run_fake("spawn_child")
        self.assertEqual(result["exit_code"], 0)
        self.assertLess(result["elapsed_ms"], 3500)  # the grandchild holds the handles for 4 s
        wait_gone(int(side_file("gpid.txt").read_text()), 8)
        for d in made:
            shutil.rmtree(d, ignore_errors=True)

    def test_temp_dir_removed(self):
        made = []
        real = tempfile.mkdtemp
        with mock.patch.object(rc.tempfile, "mkdtemp", side_effect=lambda **k: made.append(real(**k)) or made[-1]):
            self.run_fake("ok")
        self.assertTrue(made)
        self.assertFalse(any(os.path.exists(d) for d in made))

    def test_prompt_not_in_argv(self):
        self.run_fake("argv_dump", "-p", stdin_text="SECRET-PROMPT")
        seen = json.loads(side_file("argv.json").read_text())
        self.assertNotIn("SECRET-PROMPT", " ".join(seen["argv"]))
        self.assertEqual(seen["stdin"], "SECRET-PROMPT")

    def test_spawn_failed(self):
        with self.assertRaises(rc.CliError) as ctx:
            rc.run_safely(["no-such-binary-xyz"], cwd=self.work, env={}, timeout=2)
        self.assertEqual(ctx.exception.code, "spawn_failed")

    def test_probe_version(self):
        set_mode("ok")
        self.assertEqual(rc.probe_version([NODE, str(FAKE)], rc.child_env("codex")),
                         {"ok": True, "version": "9.9.9", "reason": None})
        set_mode("version_fail")
        self.assertEqual(rc.probe_version([NODE, str(FAKE)], rc.child_env("codex"))["reason"], "version_failed")
        slow = rc.probe_version([NODE, "-e", "setTimeout(()=>{},30000)", "--"], rc.child_env("codex"), timeout=1)
        self.assertEqual((slow["ok"], slow["reason"]), (False, "timeout"))
        self.assertEqual(rc.probe_version(["no-such-binary-xyz"], {})["reason"], "version_failed")


class ConnectionTest(FakeCase):
    def capped(self, seconds=4):
        """Real run_safely, but the command (not the probe) gets a short timeout so slow-mode tests stay fast."""
        real = rc.run_safely
        patcher = mock.patch.object(
            rc, "run_safely", side_effect=lambda argv, **k: real(argv, **(k if "--version" in argv else {**k, "timeout": seconds})))
        patcher.start()
        self.addCleanup(patcher.stop)

    def codex(self, mode, **kw):
        set_mode(mode)
        return rc.connection_test("codex", binary=str(CMD), **kw)

    def test_reasons_through_shim(self):
        self.capped()
        result = self.codex("ok")  # the whole path, including the real probe through the shim
        self.assertEqual((result["ok"], result["reason"], result["version"], result["exit_code"]),
                         (True, "ready", "9.9.9", 0))
        self.assertEqual(self.codex("version_fail")["reason"], "version_failed")
        self.skip_probe()
        for mode, ok, reason in (("ok", True, "ready"), ("nonzero", False, "nonzero_exit"),
                                 ("nonzero_auth", False, "not_logged_in"), ("nonzero_flag", False, "unsupported_flag"),
                                 ("empty", False, "empty_reply"), ("wrong_reply", False, "unexpected_reply"),
                                 ("slow", False, "timeout"), ("flood", False, "unexpected_reply")):
            result = self.codex(mode)
            self.assertEqual((result["ok"], result["reason"], result["kind"]), (ok, reason, "codex"), mode)
            self.assertEqual(set(result), {"ok", "reason", "kind", "version", "exit_code", "elapsed_ms"})

    def test_a_temp_failure_does_not_poison_the_lock(self):
        with mock.patch.object(rc.tempfile, "mkdtemp", side_effect=OSError("denied")):
            result = rc.connection_test("codex", binary=str(CMD))
        self.assertEqual(result["reason"], "temp_failed")
        self.assertFalse(rc._lock.locked(), "the busy flag must be released")
        self.skip_probe()
        self.assertEqual(self.codex("ok")["reason"], "ready")

    def test_validation_reasons(self):
        for binary, code in ((r"relative\codex.exe", "binary_not_absolute"), (str(SHIM_DIR / "codex-evil.exe"), "binary_name_mismatch"),
                             (str(SHIM_DIR / "gone" / "codex.exe"), "binary_not_found")):
            self.assertEqual(rc.connection_test("codex", binary=binary)["reason"], code)
        bad = SHIM_DIR.parent / "bad" / "codex.cmd"
        bad.parent.mkdir(exist_ok=True)
        bad.write_text("@echo hi\r\n")
        self.assertEqual(rc.connection_test("codex", binary=str(bad))["reason"], "unsupported_shim")
        good = SHIM_DIR.parent / "nonode" / "codex.cmd"
        good.parent.mkdir(exist_ok=True)
        good.write_text(SHIM.format(rel=r"t.js"))
        (good.parent / "t.js").write_text("x")
        with mock.patch.object(rc.shutil, "which", return_value=None), mock.patch.object(rc, "NODE_FALLBACK", r"C:\nope\node.exe"):
            self.assertEqual(rc.connection_test("codex", binary=str(good))["reason"], "node_not_found")
        with mock.patch.object(rc, "find_binary", return_value=None):
            self.assertEqual(rc.connection_test("grok")["reason"], "not_found")

    def test_spawn_failed(self):
        with mock.patch.object(rc, "find_binary", return_value={"path": "x"}), \
                mock.patch.object(rc, "resolve_command", return_value=["no-such-binary-xyz"]), \
                mock.patch.object(rc, "probe_version", return_value={"ok": True, "version": "1.0.0", "reason": None}):
            self.assertEqual(rc.connection_test("grok")["reason"], "spawn_failed")

    def test_invalid_kind_and_model_run_nothing(self):
        with mock.patch.object(rc, "find_binary", side_effect=AssertionError("ran")):
            self.assertEqual(rc.connection_test("gemini")["reason"], "invalid_kind")
            for model in ("-x", "a b", "x" * 101, "a;b", "$(x)"):
                self.assertEqual(rc.connection_test("codex", model=model)["reason"], "invalid_model", model)

    def test_timeout_clamping(self):
        self.skip_probe()
        seen = []

        def fake(argv, **kw):
            seen.append(kw["timeout"])
            return TREE_RESULT
        with mock.patch.object(rc, "run_safely", side_effect=fake):
            for value in (1, 500, None, "x", float("nan"), 30):
                self.codex("ok", timeout_s=value)
        self.assertEqual(seen, [10, 120, 60, 60, 60, 30])

    def test_busy_lock(self):
        self.skip_probe()
        entered, release = threading.Event(), threading.Event()

        def blocking(argv, **kw):
            entered.set()
            release.wait(10)
            return TREE_RESULT
        results = []
        with mock.patch.object(rc, "run_safely", side_effect=blocking):
            thread = threading.Thread(target=lambda: results.append(self.codex("ok")))
            thread.start()
            self.assertTrue(entered.wait(10))
            started = time.monotonic()
            busy = self.codex("ok")
            self.assertLess(time.monotonic() - started, 1)
            release.set()
            thread.join(10)
        self.assertEqual((busy["ok"], busy["reason"]), (False, "busy"))
        self.assertEqual(results[0]["reason"], "empty_reply")
        self.assertEqual(self.codex("ok")["reason"], "ready")  # lock released

    def test_commands_and_fixed_prompt(self):
        self.skip_probe()
        prompt = "Reply with exactly OK"
        seen = {}
        for kind in ("codex", "grok", "claude"):
            self.patch_binary(kind) if kind != "codex" else None
            set_mode("argv_dump")
            side_file("argv.json").unlink(missing_ok=True)
            result = rc.connection_test(kind, binary=str(CMD) if kind == "codex" else None, model="m-1.5:x/y")
            self.assertEqual((result["ok"], result["reason"]), (True, "ready"), kind)
            seen[kind] = json.loads(side_file("argv.json").read_text())
            self.assertNotIn("Reply", " ".join(seen[kind]["argv"]), kind)
        codex, grok, claude = (seen[k] for k in ("codex", "grok", "claude"))
        self.assertEqual((codex["stdin"], claude["stdin"], grok["promptFile"]), (prompt, prompt, prompt))
        self.assertEqual(codex["argv"][:3], ["exec", "--ephemeral", "--ignore-user-config"])
        self.assertEqual(codex["argv"][-3:], ["-m", "m-1.5:x/y", "-"])
        self.assertEqual(codex["argv"].count("--disable"), 12)
        self.assertIn("read-only", codex["argv"])
        self.assertEqual(grok["argv"][-2:], ["-m", "m-1.5:x/y"])
        for flag in ("--disable-web-search", "--no-subagents", "--prompt-file", "--max-turns"):
            self.assertIn(flag, grok["argv"])
        self.assertNotIn("--tools", grok["argv"])
        self.assertNotIn("--permission-mode", grok["argv"])
        self.assertEqual(claude["argv"][-2:], ["--model", "m-1.5:x/y"])
        self.assertEqual(claude["argv"][claude["argv"].index("--tools") + 1], "")
        self.assertIn("--safe-mode", claude["argv"])

    def test_grok_ansi_and_claude_json(self):
        self.skip_probe()
        self.patch_binary("grok")
        set_mode("ok")
        self.assertEqual(rc.connection_test("grok")["reason"], "ready")
        set_mode("wrong_reply")
        self.assertEqual(rc.connection_test("grok")["reason"], "unexpected_reply")
        self.patch_binary("claude")
        set_mode("ok")
        self.assertEqual(rc.connection_test("claude")["reason"], "ready")
        set_mode("claude_error")
        result = rc.connection_test("claude")
        self.assertEqual((result["ok"], result["reason"], result["exit_code"]), (False, "nonzero_exit", 0))

    def test_env_is_allowlisted(self):
        self.skip_probe()
        leaked = {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_X": "1",
                  "OPENROUTER_API_KEY": "k", "LOCAL_X": "1", "DASHBOARD_TOKEN": "d", "ROUTE_X": "1", "MY_SECRET": "s",
                  "OPENAI_API_KEY": "o", "HTTPS_PROXY": "http://p", "XAI_API_KEY": "x"}
        with mock.patch.dict(os.environ, leaked):
            self.assertEqual(self.codex("env_dump")["reason"], "ready")
        names = set(side_file("env.txt").read_text().split("\n"))
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_X", "OPENROUTER_API_KEY", "LOCAL_X",
                     "DASHBOARD_TOKEN", "ROUTE_X", "MY_SECRET", "XAI_API_KEY"):
            self.assertNotIn(name, names)
        self.assertTrue({"OPENAI_API_KEY", "HTTPS_PROXY"} <= names)


class RealMachine(unittest.TestCase):
    def test_detect_all_does_not_raise(self):
        if not any(rc.find_binary(k) for k in rc.KINDS):
            self.skipTest("no real CLI installed")
        entries = rc.detect_all()
        self.assertEqual([e["kind"] for e in entries], list(rc.KINDS))
        for entry in entries:
            self.assertEqual(set(entry), {"kind", "found", "name", "dir", "via", "version", "reason"})


if __name__ == "__main__":
    unittest.main()
