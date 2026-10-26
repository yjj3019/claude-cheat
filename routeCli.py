"""Find, validate and safely run the codex, grok and claude CLIs for the dashboard connection test."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import localProcessTree

KINDS = ("codex", "grok", "claude")
PROMPT = "Reply with exactly OK"
NODE_FALLBACK = r"C:\Program Files\nodejs\node.exe"
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_SHIM_RE = re.compile(r'"%dp0%\\([^"]+)"\s*%\*')
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FLAG_RE = re.compile(r"unknown (option|argument|flag)|unexpected argument|unrecognized (option|argument)"
                      r"|invalid (option|argument)", re.I)
_AUTH_RE = re.compile(r"not logged in|log ?in|authenticat|unauthori[sz]ed|api key|401|403", re.I)
_ALLOWED = {n.upper() for n in (
    "PATH PATHEXT ComSpec SystemRoot SystemDrive windir TEMP TMP USERNAME USERPROFILE HOMEDRIVE HOMEPATH "
    "APPDATA LOCALAPPDATA ProgramData ProgramFiles ProgramFiles(x86) ProgramW6432 HOME "
    "HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS "
    "CURL_CA_BUNDLE").split()}
_PER_KIND = {"codex": {"CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY"},
             "grok": {"XAI_API_KEY"}, "claude": {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"}}
_lock = threading.Lock()


class CliError(ValueError):
    """A CLI problem identified by a short code; the message never carries a path or secret."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def validate_binary(kind, path):
    """Return the normalized absolute path of an existing <kind>.exe/.cmd/bare file, else raise CliError."""
    if kind not in KINDS:
        raise CliError("binary_name_mismatch")
    if not isinstance(path, str) or not path or "\0" in path or not os.path.isabs(path):
        raise CliError("binary_not_absolute")
    path = os.path.normpath(path)
    stem, ext = os.path.splitext(os.path.basename(path))
    if stem.lower() != kind or ext.lower() not in (".exe", ".cmd", ""):
        raise CliError("binary_name_mismatch")
    if not os.path.isfile(path):
        raise CliError("binary_not_found")
    return path


def _registry_path():
    """User then machine PATH entries re-read from the Windows registry, %VARS% expanded."""
    if os.name != "nt":
        return []
    import winreg
    entries = []
    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as key:
                value = winreg.QueryValueEx(key, "Path")[0]
        except OSError:
            continue
        entries += [os.path.expandvars(p) for p in str(value).split(";") if p]
    return entries


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def _known_dirs(kind):
    home = Path.home()
    dirs = [home / ".grok" / "bin"] if kind == "grok" else []
    if kind != "grok" and os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "npm")
    return [str(d) for d in dirs + [home / ".local" / "bin"]]


def _first_in(kind, directories):
    names = (kind + ".exe", kind + ".cmd") if os.name == "nt" else (kind,)
    for directory in directories:
        for name in names:
            try:
                return validate_binary(kind, os.path.join(directory, name))
            except CliError:
                pass
    return None


def find_binary(kind, pinned=None):
    """Locate a CLI: pinned, current PATH, registry PATH (the session's may be stale), known dirs."""
    if pinned:
        found, via = validate_binary(kind, pinned), "pinned"
    else:
        current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        seen = {_norm(p) for p in current}
        fresh = [p for p in _registry_path() if _norm(p) not in seen]
        found, via = None, None
        for label, directories in (("PATH", current), ("registry-PATH", fresh), ("known-dir", _known_dirs(kind))):
            found = _first_in(kind, directories)
            if found:
                via = label
                break
    if not found:
        return None
    return {"kind": kind, "path": found, "dir": os.path.dirname(found), "name": os.path.basename(found), "via": via}


def resolve_command(path):
    """Argv prefix that starts the program without cmd.exe: an npm .cmd shim becomes [node, script]."""
    if os.path.splitext(path)[1].lower() != ".cmd":
        return [path]
    shim_dir = Path(path).resolve().parent
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise CliError("unsupported_shim") from None
    tokens = _SHIM_RE.findall(text)
    if not tokens:
        raise CliError("unsupported_shim")
    target = (shim_dir / tokens[-1]).resolve()
    if not target.is_file() or not target.is_relative_to(shim_dir):
        raise CliError("unsupported_shim")
    if target.suffix.lower() == ".exe":
        return [str(target)]
    if target.suffix.lower() not in (".js", ".mjs", ".cjs"):
        raise CliError("unsupported_shim")
    for node in (shim_dir / "node.exe", shutil.which("node"), NODE_FALLBACK):
        if node and os.path.isfile(node):
            return [str(node), str(target)]
    raise CliError("node_not_found")


def child_env(kind, base=None):
    """Allowlisted environment: never ANTHROPIC_BASE_URL (proxy recursion), tokens or other secrets."""
    allowed = _ALLOWED | _PER_KIND.get(kind, set())
    result = {}
    for name, value in (os.environ if base is None else base).items():
        upper = name.upper()
        if upper in allowed or upper.startswith("XDG_") or (kind == "grok" and upper.startswith("GROK_")):
            result[name] = value
    return result


def _rmtree(path):
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.2)


def run_safely(argv, *, stdin_text=None, cwd, env, timeout, max_bytes=1048576):
    """Run argv without a shell, with file-backed stdio (no pipes a grandchild could keep open)."""
    tmp = tempfile.mkdtemp(prefix="routecli-")
    out_path, err_path = os.path.join(tmp, "out"), os.path.join(tmp, "err")
    started = time.monotonic()
    timed_out = truncated = False
    try:
        in_path = os.path.join(tmp, "in")
        Path(in_path).write_bytes((stdin_text or "").encode("utf-8"))
        with open(in_path, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
            try:
                process = subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=ferr, cwd=cwd, env=env, shell=False,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, ValueError):
                raise CliError("spawn_failed") from None
        identity = localProcessTree.process_identity(process.pid)
        while process.poll() is None:
            if time.monotonic() - started > timeout:
                timed_out = True
            elif os.path.getsize(out_path) + os.path.getsize(err_path) > max_bytes:
                truncated = True
            else:
                time.sleep(0.05)
                continue
            localProcessTree.kill_owned_tree(process, identity)
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                pass
            break
        size = os.path.getsize(out_path) + os.path.getsize(err_path)
        with open(out_path, "rb") as f:
            stdout = f.read(max_bytes)
        with open(err_path, "rb") as f:
            stderr_head = f.read(2048)
        return {"exit_code": None if timed_out or truncated else process.returncode, "timed_out": timed_out,
                "truncated": truncated or size > max_bytes, "elapsed_ms": int((time.monotonic() - started) * 1000),
                "stdout": stdout, "stderr_len": os.path.getsize(err_path), "stderr_head": stderr_head}
    finally:
        _rmtree(tmp)


def probe_version(argv_prefix, env, timeout=10):
    """Run `<prefix> --version` in an empty temp cwd and extract the first x.y.z."""
    cwd = tempfile.mkdtemp(prefix="routecli-cwd-")
    try:
        result = run_safely([*argv_prefix, "--version"], cwd=cwd, env=env, timeout=timeout)
    except CliError:
        return {"ok": False, "version": None, "reason": "version_failed"}
    finally:
        _rmtree(cwd)
    if result["timed_out"]:
        return {"ok": False, "version": None, "reason": "timeout"}
    match = _VERSION_RE.search(result["stdout"].decode("utf-8", "replace"))
    if result["exit_code"] != 0 or not match:
        return {"ok": False, "version": None, "reason": "version_failed"}
    return {"ok": True, "version": match.group(), "reason": None}


def detect_all(pinned=None):
    """One status entry per kind; every problem becomes found=False with a reason, never an exception."""
    entries = []
    for kind in KINDS:
        entry = {"kind": kind, "found": False, "name": None, "dir": None, "via": None, "version": None, "reason": None}
        try:
            binary = find_binary(kind, (pinned or {}).get(kind))
            if not binary:
                raise CliError("not_found")
            entry.update(name=binary["name"], dir=binary["dir"], via=binary["via"])
            probe = probe_version(resolve_command(binary["path"]), child_env(kind))
            entry.update(found=probe["ok"], version=probe["version"], reason=probe["reason"])
        except CliError as error:
            entry["reason"] = error.code
        except Exception:
            entry["reason"] = "version_failed"
        entries.append(entry)
    return entries


def _command(kind, tmp, model):
    """Return (argv after the prefix, stdin text, reply reader). The prompt never goes in argv."""
    if kind == "codex":
        # shortcut: the effectiveness of these --disable flags is UNVERIFIED; the read-only sandbox,
        # --ephemeral and the empty cwd are the actual containment. Revisit after checking real codex behaviour.
        features = ("shell_tool unified_exec apps plugins hooks multi_agent browser_use computer_use view_image "
                    "image_generation skill_search memories").split()
        argv = ["exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                "-s", "read-only", "-C", tmp, "-o", os.path.join(tmp, "last.txt")]
        for feature in features:
            argv += ["--disable", feature]
        return argv + (["-m", model] if model else []) + ["-"], PROMPT, lambda r: _read_text(os.path.join(tmp, "last.txt"))
    if kind == "grok":
        prompt = os.path.join(tmp, "prompt.txt")
        Path(prompt).write_text(PROMPT, encoding="utf-8")
        # --tools/--permission-mode are deliberately absent: their semantics are unverified.
        argv = ["--cwd", tmp, "--prompt-file", prompt, "--output-format", "plain", "--disable-web-search",
                "--no-subagents", "--max-turns", "1"] + (["-m", model] if model else [])
        return argv, None, lambda r: _ANSI_RE.sub("", r["stdout"].decode("utf-8", "replace"))
    argv = ["-p", "--output-format", "json", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--tools", ""] + (["--model", model] if model else [])
    return argv, PROMPT, _claude_reply


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _claude_reply(result):
    try:
        data = json.loads(result["stdout"].decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("is_error"):
        raise CliError("nonzero_exit")
    return data["result"] if isinstance(data.get("result"), str) else ""


def _classify(result, reply_of):
    if result["timed_out"]:
        return "timeout"
    if result["truncated"]:
        return "unexpected_reply"
    head = result["stderr_head"].decode("utf-8", "replace")
    if result["exit_code"] != 0:
        if _FLAG_RE.search(head):
            return "unsupported_flag"
        return "not_logged_in" if _AUTH_RE.search(head) else "nonzero_exit"
    try:
        reply = reply_of(result).strip()
    except CliError as error:
        return error.code
    if not reply:
        return "empty_reply"
    return "ready" if re.fullmatch(r"\W*OK\W*", reply, re.I) else "unexpected_reply"


def connection_test(kind, *, binary=None, model=None, timeout_s=60):
    """One bounded, serialized `reply OK` round trip through a CLI; reasons only, never CLI output."""
    result = {"ok": False, "reason": None, "kind": kind, "version": None, "exit_code": None, "elapsed_ms": 0}
    if kind not in KINDS:
        return {**result, "reason": "invalid_kind"}
    if model and not _MODEL_RE.fullmatch(model):
        return {**result, "reason": "invalid_model"}
    try:
        timeout_s = float(timeout_s)
        timeout_s = 60.0 if not math.isfinite(timeout_s) else min(120.0, max(10.0, timeout_s))
    except (TypeError, ValueError):
        timeout_s = 60.0
    if not _lock.acquire(blocking=False):
        return {**result, "reason": "busy"}
    tmp = tempfile.mkdtemp(prefix="routecli-cwd-")
    try:
        found = find_binary(kind, binary or None)
        if not found:
            return {**result, "reason": "not_found"}
        prefix = resolve_command(found["path"])
        env = child_env(kind)
        probe = probe_version(prefix, env, min(10, timeout_s))
        result["version"] = probe["version"]
        if not probe["ok"]:
            return {**result, "reason": probe["reason"] or "version_failed"}
        argv, stdin_text, reply_of = _command(kind, tmp, model)
        run = run_safely([*prefix, *argv], stdin_text=stdin_text, cwd=tmp, env=env, timeout=timeout_s)
        reason = _classify(run, reply_of)
        return {**result, "ok": reason == "ready", "reason": reason, "exit_code": run["exit_code"],
                "elapsed_ms": run["elapsed_ms"]}
    except CliError as error:
        return {**result, "reason": error.code}
    finally:
        _rmtree(tmp)
        _lock.release()
