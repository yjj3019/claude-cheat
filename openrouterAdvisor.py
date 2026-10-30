"""Session-only Claude Code launcher and one stdio MCP advisor tool."""

import http.client
import base64
import binascii
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import contextlib
import queue
import random
import secrets
import socket
import threading
import time
import argparse
import uuid
import localProcessTree
import routeConfig
import routeCli
import cliBackend
from urllib.parse import urlsplit, urlunsplit
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
LEGACY_MODEL = "deepseek/deepseek-v4.1-flash-1017"
PRO_SESSION_GATE = "CLAUDE_PRO_CONSULT_ENABLED"
LOCAL_STARTUP_MESSAGES = {
    "local_cli_not_found": "Claude Code executable not found. Install it or check PATH, then restart Local mode.",
    "local_node_not_found": "Node.js executable not found. Install it or check PATH, then restart Local mode.",
    "local_cli_unsupported": "Local CLI mode requires Claude Code 2.1.304 or later. Check claude --version.",
    "local_cli_version_check_failed": "Could not check Claude Code version. Run claude --version and check executable permissions.",
    "local_configuration_invalid": "Invalid Local configuration. Check openclaude.config.yaml and process environment: Messages URL, HTTP approval, model, key format, and same-origin model-list URL. Values hidden.",
    "local_output_budget_invalid": "Local output budget must be a positive integer. Check CLAUDE_CODE_MAX_OUTPUT_TOKENS.",
    "local_proxy_start_failed": "Local routing proxy did not start. Check Node.js, proxy files, and loopback permissions, then restart Local mode.",
    "local_cli_start_failed": "Could not start Claude Code. Check executable permissions and restart Local mode.",
    "local_process_cleanup_failed": "Windows Local process cleanup could not be prepared. Check Python and Windows job permissions, then restart Local mode. No unmanaged Local session was started.",
}


class LocalStartupError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(LOCAL_STARTUP_MESSAGES[code])


class OpenRouterStartupError(LocalStartupError):
    def __init__(self, code):
        self.code = code.replace("local_", "openrouter_", 1)
        RuntimeError.__init__(self, LOCAL_STARTUP_MESSAGES[code].replace("Local", "OpenRouter").replace("local routing", "OpenRouter routing"))


class OpenRouterConfigError(ValueError):
    def __init__(self):
        super().__init__(
            "Set a valid ANTHROPIC_AUTH_TOKEN and OPENROUTER_MODEL in openclaude.config.yaml "
            "(openrouter: api_key, model) beside openrouterAdvisor.py. "
            "No default model is selected. Values hidden. "
            "Or run OpenClaude.ps1 -Setup (OpenClaudeDashboard.ps1), open http://127.0.0.1:8787/ "
            "and fill the OpenRouter settings there.")


class PlanLoginError(RuntimeError):
    def __init__(self):
        super().__init__("Existing first-party Plan login required; run claude auth login, then restart Claude-Cheat")


class LauncherArgumentsError(ValueError):
    pass


class ProviderChoiceError(ValueError):
    """Several services are configured; a launch must not pick one silently."""

    code = "provider_choice_required"

    def __init__(self, providers=("openrouter", "local")):
        self.providers = list(providers)
        super().__init__("Multiple services are configured; choose one explicitly")


def read_env(path: Path) -> dict:
    """Legacy single-line KEY=VALUE reader (strict); kept for the deprecated .env.* files."""
    return routeConfig.read_legacy(path)


def settings(provider: str) -> dict:
    """One service's stored settings from openclaude.config.yaml, else its legacy .env file."""
    return routeConfig.Store(ROOT / routeConfig.CONFIG_NAME).read_provider(provider)


def read_key(values: dict | None = None) -> str:
    if values is None:
        values = settings("openrouter")
    token = values.get("ANTHROPIC_AUTH_TOKEN", "")
    if not re.fullmatch(r"sk-or-[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid OpenRouter ANTHROPIC_AUTH_TOKEN; value hidden")
    return token


def local_endpoint(url: str, allow_remote: bool, http_url: str = "") -> tuple:
    if not isinstance(url, str) or any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise ValueError("Invalid Local LLM URL; value hidden")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid Local LLM URL; value hidden") from None
    loopback = parsed.hostname in {"127.0.0.1", "::1"}
    internal_http = parsed.scheme == "http" and url == http_url
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or port == 0
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path != "/v1/messages" or (not loopback and not internal_http and (not allow_remote or parsed.scheme != "https"))):
        raise ValueError("Local LLM requires loopback Messages, approved internal HTTP, or explicitly allowed remote HTTPS")
    return parsed, (parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80))


def provider_config(provider: str, require_model: bool = True) -> dict:
    if provider == "cli":
        values = settings("cli")
        kind = (values.get("CLI_KIND") or "").strip()
        model = (values.get("CLI_MODEL") or "").strip()
        binary = (values.get("CLI_BINARY") or "").strip()
        seconds = values.get("CLI_TIMEOUT_S") or "60"
        if kind not in routeCli.KINDS or model and not routeCli._MODEL_RE.fullmatch(model):
            raise routeCli.CliError("invalid_cli_configuration")
        if not re.fullmatch(r"[0-9]{1,3}", seconds) or not 10 <= int(seconds) <= 120:
            raise routeCli.CliError("invalid_cli_timeout")
        if binary:
            routeCli.validate_binary(kind, binary)
        return {"SONNET_PROVIDER": "cli", "CLI_KIND": kind, "CLI_MODEL": model,
                "CLI_BINARY": binary, "CLI_TIMEOUT_S": seconds}
    if provider == "openrouter":
        try:
            values = settings("openrouter")
            model = values.get("OPENROUTER_MODEL", "").strip()
            if not re.fullmatch(r"[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}", model) or model.startswith(("REPLACE_WITH_", "YOUR_")):
                raise ValueError("Invalid OpenRouter model")
            return {"SONNET_PROVIDER": provider, "OPENROUTER_API_KEY": read_key(values), "OPENROUTER_MODEL": model}
        except (OSError, ValueError):
            raise OpenRouterConfigError() from None
    if provider != "local":
        raise ValueError("Unknown Sonnet provider")
    values = settings("local")
    values = {**values, **{name: os.environ[name] for name in
              ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL") if name in os.environ}}
    url = values.get("LOCAL_LLM_URL", "http://127.0.0.1:11434/v1/messages")
    remote = values.get("LOCAL_LLM_ALLOW_REMOTE", "false").strip().lower()
    if remote not in {"true", "false"}:
        raise ValueError("LOCAL_LLM_ALLOW_REMOTE must be true or false")
    http_url = values.get("LOCAL_LLM_HTTP_URL", "")
    parsed, origin = local_endpoint(url, remote == "true", http_url)
    model = values.get("LOCAL_LLM_MODEL", "").strip()
    if (require_model and not model) or any(ord(char) < 32 for char in model):
        raise ValueError("Set LOCAL_LLM_MODEL in openclaude.config.yaml (local: model) or the process environment")
    key = values.get("LOCAL_LLM_API_KEY", "")
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError("Invalid Local LLM key; value hidden")
    models_url = values.get("LOCAL_LLM_MODELS_URL", "") or urlunsplit((parsed.scheme, parsed.netloc, "/v1/models", "", ""))
    if any(ord(char) < 33 or ord(char) == 127 for char in models_url):
        raise ValueError("Invalid Local model-list URL; value hidden")
    try:
        models = urlsplit(models_url)
        models_origin = (models.scheme, models.hostname, models.port if models.port is not None else (443 if models.scheme == "https" else 80))
    except ValueError:
        raise ValueError("Invalid Local model-list URL; value hidden") from None
    if (models_origin != origin or models.port == 0 or models.username is not None or models.password is not None
            or models.query or models.fragment or not models.path.startswith("/")):
        raise ValueError("Local model-list URL must have the same origin without credentials or query")
    return {"SONNET_PROVIDER": provider, "LOCAL_LLM_URL": url, "LOCAL_LLM_MODEL": model,
            "LOCAL_LLM_API_KEY": key, "LOCAL_LLM_ALLOW_REMOTE": remote, "LOCAL_LLM_HTTP_URL": http_url, "LOCAL_LLM_MODELS_URL": models_url}


def configured_provider() -> str:
    """Use the saved default, otherwise select the only usable service without guessing."""
    preferred = routeConfig.Store(ROOT / routeConfig.CONFIG_NAME).default_provider()
    if preferred != "auto":
        # A broken preference must not silently route to a different service.
        if preferred != "sonnet":
            provider_config(preferred)
        return preferred
    ready, failures = [], []
    for name in ("openrouter", "local", "cli"):
        try:
            provider_config(name)
        except (OSError, ValueError) as error:
            failures.append((name, error))
        else:
            ready.append(name)
    if len(ready) > 1:
        raise ProviderChoiceError(ready)
    if len(ready) == 1:
        return ready[0]
    if any(settings("cli").get(key) for key in ("CLI_KIND", "CLI_MODEL", "CLI_BINARY")):
        raise routeCli.CliError("invalid_cli_configuration")
    # Nothing is usable. Point a Local-only user at the Local section instead of telling
    # them to fill OpenRouter; a first-run install still gets the OpenRouter setup help.
    try:
        local_source = routeConfig.Store(ROOT / routeConfig.CONFIG_NAME).source("local")
    except (OSError, ValueError):
        local_source = "none"
    if local_source != "none":
        raise LocalStartupError("local_configuration_invalid")
    raise OpenRouterConfigError()


DUAL_ALIASES = {"openrouter": "claude-route-openrouter", "local": "claude-route-local"}


def dual_provider_setup(provider: str) -> dict | None:
    """Proxy env that exposes the other configured provider as one extra /model entry, or None.

    Opt-in through ``routing: dual_provider``. A missing or broken other provider keeps the
    session single-provider instead of failing startup: this is a convenience, not a need.
    """
    if provider not in DUAL_ALIASES:
        return None
    try:
        enabled = (settings("routing").get("ROUTE_DUAL_PROVIDER") or "").strip().lower()
    except (OSError, ValueError):
        return None
    if enabled not in {"true", "1", "yes"}:
        return None
    other = "local" if provider == "openrouter" else "openrouter"
    try:
        config = provider_config(other)
    except (OSError, ValueError):
        return None
    fragment = {"ROUTE_ALT_PROVIDER": other, "ROUTE_ALT_ALIAS": DUAL_ALIASES[other]}
    if other == "openrouter":
        fragment.update(ROUTE_ALT_MODEL=config["OPENROUTER_MODEL"], ROUTE_ALT_KEY=config["OPENROUTER_API_KEY"])
    else:
        fragment.update(ROUTE_ALT_URL=config["LOCAL_LLM_URL"], ROUTE_ALT_MODEL=config["LOCAL_LLM_MODEL"],
                        ROUTE_ALT_KEY=config.get("LOCAL_LLM_API_KEY", ""),
                        ROUTE_ALT_ALLOW_REMOTE=config["LOCAL_LLM_ALLOW_REMOTE"],
                        ROUTE_ALT_HTTP_URL=config.get("LOCAL_LLM_HTTP_URL", ""))
    return fragment


def dual_cli_env(fragment: dict) -> dict:
    """Show the alternate provider in Claude Code's /model picker (one custom entry)."""
    label = "OpenRouter" if fragment["ROUTE_ALT_PROVIDER"] == "openrouter" else "Local"
    name = f"{label} ({fragment['ROUTE_ALT_MODEL']})"
    if len(name) > 60:
        name = name[:57] + "..."
    return {"ANTHROPIC_CUSTOM_MODEL_OPTION": fragment["ROUTE_ALT_ALIAS"],
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": name,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": f"Send this model's requests to {label}"}


# Env names the CLI must never see: alternate-provider routing (proxy-only secrets) and the
# /model picker hooks this launcher owns. Shared by the session environment and the auth
# probe, which otherwise rebuilds the caller's environment and would inherit a stale export.
_CLI_ENV_STRIP = ("ROUTE_ALT_PROVIDER", "ROUTE_ALT_MODEL", "ROUTE_ALT_URL", "ROUTE_ALT_KEY",
                  "ROUTE_ALT_ALLOW_REMOTE", "ROUTE_ALT_HTTP_URL", "ROUTE_ALT_ALIAS",
                  "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
                  "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION")


class NoLocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_local() -> dict:
    """Read model metadata only: no CLI authentication, inference, fallback, or installation."""
    base = {"local_check": False, "inference_checked": False}
    try:
        config = provider_config("local", require_model=False)
        headers = {"Accept": "application/json"}
        if config["LOCAL_LLM_API_KEY"]:
            headers["Authorization"] = "Bearer " + config["LOCAL_LLM_API_KEY"]
        request = urllib.request.Request(config["LOCAL_LLM_MODELS_URL"], headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoLocalRedirect())
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                return {**base, "reason": "http_error", "status": response.status}
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            return {**base, "reason": "response_too_large"}
        data = json.loads(payload)
        if not isinstance(data, dict):
            return {**base, "reason": "invalid_model_list"}
        entries = data.get("data", data.get("models"))
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            return {**base, "reason": "invalid_model_list"}
        ids = [item.get("id", item.get("name", item.get("model"))) for item in entries]
        if any(not isinstance(item, str) or not item for item in ids):
            return {**base, "reason": "invalid_model_list"}
        model = config["LOCAL_LLM_MODEL"]
        found = bool(model) and model in ids
        return {**base, "local_check": found, "model_count": len(ids), "configured_model_found": found,
                "reason": "ready_metadata_only" if found else "model_not_found" if model else "model_not_configured"}
    except urllib.error.HTTPError as error:
        result = {**base, "reason": "redirect_rejected" if 300 <= error.code < 400 else "http_error", "status": error.code}
        error.close()
        return result
    except urllib.error.URLError:
        return {**base, "reason": "connection_failed"}
    except (TimeoutError, OSError):
        return {**base, "reason": "connection_failed"}
    except http.client.HTTPException:
        return {**base, "reason": "connection_failed"}
    except (ValueError, UnicodeError):
        return {**base, "reason": "invalid_configuration_or_response"}


def log_route_marker(text: str) -> None:
    """Append a launcher lifecycle marker beside the proxy events, so a dropped session is explainable."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        with (ROOT / ".route.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} --- {text} ---\n")
    except OSError:
        pass


# Overridable so tests never touch a real session's handoff file.
HANDOFF_FILE = Path(os.environ.get("ROUTE_HANDOFF_PATH") or (ROOT / ".session-handoff.json"))

# Renaming over a file some Windows reader or scanner holds open fails with these.
_WRITE_RETRY_WINERRORS = {5, 32, 33}  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_WRITE_RETRY_ATTEMPTS = 4


def _rename_with_retry(source: Path, target: Path) -> bool:
    """os.replace with bounded jittered retries for Windows sharing/locking contention."""
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        try:
            os.replace(source, target)
            return True
        except OSError as error:
            if getattr(error, "winerror", None) not in _WRITE_RETRY_WINERRORS:
                return False
            if attempt == _WRITE_RETRY_ATTEMPTS - 1:
                return False
            time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.01))
    return False


def write_json_state(path: Path, payload: dict) -> bool:
    """Write JSON state so no reader can observe a partially written file.

    The rename retries Windows contention, then falls back to a documented non-atomic
    in-place rewrite: a reader holding the target open can block every rename, and a
    stale handoff is worse than a briefly torn one. One diagnostic reports that fallback.
    Returns False when the state could not be written at all; temp files never survive.
    """
    text = json.dumps(payload)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if _rename_with_retry(temporary, path):
            return True
        path.write_text(text, encoding="utf-8")
        print(json.dumps({"event": "state_write_fallback", "file": path.name}),
              file=sys.stderr, flush=True)
        return True
    except (OSError, TypeError):
        return False
    finally:
        try:
            if temporary is not None:
                temporary.unlink()
        except OSError:
            pass


def write_handoff(*, port, nonce, proxy_pid, provider, cli_pid, mode, dual=None, instance_id=None, instance_dir=None):
    """Record the live route so another terminal can take it over without changing the port.

    ``dual`` is the non-secret alternate topology ({provider, alias, model}) an ordinary
    session exposed in the CLI's /model picker; a takeover must keep accepting the same
    alias or the already-running CLI would start getting 403s for its own model entry.
    """
    if not isinstance(port, int) or not isinstance(proxy_pid, int):
        return  # nothing usable to record (also keeps mocked sessions out of the file)
    payload = {"port": port, "nonce": nonce, "proxy_pid": proxy_pid, "launcher_pid": cli_pid,
               "provider": provider, "mode": mode, "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if dual:
        payload["dual"] = {"provider": dual["ROUTE_ALT_PROVIDER"], "alias": dual["ROUTE_ALT_ALIAS"],
                           "model": dual["ROUTE_ALT_MODEL"]}
    if instance_id and instance_dir:
        payload["instance_id"] = instance_id
        if not write_json_state(instance_dir / "handoff.json", payload):
            print(json.dumps({"event": "instance_handoff_unavailable"}), file=sys.stderr, flush=True)
    write_json_state(HANDOFF_FILE, payload)


def read_handoff():
    try:
        data = json.loads(HANDOFF_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("port"), int):
        return None
    return data


def clear_handoff(*, proxy_pid):
    try:
        current = json.loads(HANDOFF_FILE.read_text(encoding="utf-8"))
        if current.get("proxy_pid") == proxy_pid:
            HANDOFF_FILE.unlink()
    except (OSError, ValueError):
        pass


def instance_directory(root, instance_id):
    """Create one private session directory; never follow user-supplied links."""
    if str(uuid.UUID(instance_id)) != instance_id:
        raise ValueError("invalid_instance_id")
    current = Path(root)
    for part in (".usage-stats", "instances", instance_id):
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("unsafe_instance_directory")
        current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("unsafe_instance_directory")
    return current


def clear_instance_handoff(instance_dir, proxy_pid):
    """Remove only this launcher's own live pointer; retain observed usage data."""
    try:
        path = instance_dir / "handoff.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("proxy_pid") == proxy_pid:
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def port_open(port, timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def handover_timeout_seconds() -> float:
    """Client wait must cover the proxy's drain cap, or the launcher fails first."""
    try:
        milliseconds = int(os.environ.get("ROUTE_HANDOVER_TIMEOUT_MS") or 120000)
    except ValueError:
        milliseconds = 120000
    return max(milliseconds, 1000) / 1000 + 5


def request_handover(port, nonce, timeout=None):
    """Ask the running proxy to drain and exit so this launcher can rebind its port.

    A 409 means the drain cap expired with turns still active and the proxy reverted to
    serving; the body carries the report, so surface it instead of a bare HTTP error.
    """
    if timeout is None:
        timeout = handover_timeout_seconds()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/handover", data=b"",
                                     method="POST", headers={"x-local-route-token": nonce})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            report = json.loads(error.read() or b"{}")
        except ValueError:
            report = {}
        finally:
            # An unclosed HTTPError keeps the socket until GC; close it explicitly.
            error.close()
        if error.code == 409 and report.get("reverted"):
            raise RuntimeError(
                "The proxy kept serving an active turn and reverted the handover; "
                "retry after it finishes") from None
        raise RuntimeError(f"The proxy answered the handover with HTTP {error.code}") from None


def wait_for_port_release(port, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.1)
    return False


def clean_env() -> dict:
    # Keep OS execution variables; never inherit another provider's auth or model routing.
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_", "OPENROUTER_"))
            and k.upper() not in {"CLAUDE_CONFIG_DIR", "CLAUDECODE", PRO_SESSION_GATE}}


def run_owned(args: list, env: dict, cwd: str, prompt: str = "", timeout: int = 120) -> dict:
    process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    # Captured while the child is alive; the Popen handle keeps this pid ours until reaped.
    identity = localProcessTree.process_identity(process.pid)
    try:
        output, error_text = process.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # A tree kill by bare pid is only safe while the pid still names this child.
        localProcessTree.kill_owned_tree(process, identity)
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    if process.returncode:
        detail = " ".join((error_text or "").split())[:300]
        raise RuntimeError("Claude Plan request failed; no fallback attempted"
                           + (f" (exit {process.returncode}: {detail})" if detail else f" (exit {process.returncode})"))
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("Expected Claude JSON response")
    return result


@contextlib.contextmanager
def routed_session(provider: str = "openrouter", local_only: bool = False, local_compat: bool = False,
                   openrouter_compat: bool = False, debug: bool = False, port=None, nonce=None):
    options = dict(local_only=local_only, local_compat=local_compat, openrouter_compat=openrouter_compat,
                   debug=debug, port=port, nonce=nonce)
    if provider != "cli":
        with _routed_session(provider, **options) as env:
            yield env
        return
    if local_only or local_compat or openrouter_compat:
        raise ValueError("CLI backend requires ordinary Claude Code mode")
    values = provider_config("cli")
    with cliBackend.serve(values) as (backend, url, token):
        config = {**values, "LOCAL_LLM_URL": url, "LOCAL_LLM_MODEL": backend.protocol_model,
                  "LOCAL_LLM_API_KEY": token, "LOCAL_LLM_ALLOW_REMOTE": "false", "LOCAL_LLM_HTTP_URL": ""}
        with _routed_session(provider, **options, _config=config, _backend=backend) as env:
            yield env


@contextlib.contextmanager
def _routed_session(provider: str = "openrouter", local_only: bool = False, local_compat: bool = False,
                  openrouter_compat: bool = False, debug: bool = False,
                  port: int | None = None, nonce: str | None = None, _config=None, _backend=None):
    """Own one loopback proxy; ordinary sessions alone use the subscription login."""
    if (local_only and provider != "local") or (local_compat and not local_only):
        raise ValueError("Local CLI mode requires the local provider")
    if openrouter_compat and (provider != "openrouter" or local_only or local_compat):
        raise ValueError("OpenRouter compatibility mode requires the OpenRouter provider only")
    isolated = local_only or openrouter_compat
    compat = local_compat or openrouter_compat
    startup_error = OpenRouterStartupError if openrouter_compat else LocalStartupError
    claude = shutil.which("claude")
    if not claude:
        if isolated:
            raise startup_error("local_cli_not_found")
        raise FileNotFoundError("Claude Code executable not found")
    env = clean_env() if isolated else os.environ.copy()
    if compat:
        # Preserve client preferences, never inherited provider routing or credentials.
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_EFFORT_LEVEL", "MAX_THINKING_TOKENS",
                     "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                     "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTOCOMPACT_PCT_OVERRIDE",
                     "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "CLAUDE_CODE_FORK_SUBAGENT",
                     "CLAUDE_CODE_FORWARD_SUBAGENT_TEXT", "CLAUDE_CODE_ENABLE_TASKS",
                     "CLAUDE_CODE_ENABLE_TODO_TOOLS", "CLAUDE_CODE_TASK_LIST_ID",
                     "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_AGENT_VIEW",
                     "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND", "CLAUDE_CODE_AUTO_CONNECT_IDE",
                     "CLAUDE_CODE_PLUGIN_CACHE_DIR", "CLAUDE_CODE_PLUGIN_DIRS",
                     "CLAUDE_CODE_PLUGIN_DIR_WATCH", "CLAUDE_CODE_PLUGIN_SEED_DIR",
                     "CLAUDE_CODE_SYNC_PLUGIN_INSTALL", "CLAUDE_CODE_SYNC_PLUGIN_INSTALL_TIMEOUT_MS",
                     "CLAUDE_CODE_SYNC_SKILLS", "CLAUDE_CODE_SYNC_SKILLS_INSTALL_TIMEOUT_MS",
                     "CLAUDE_CODE_SYNC_SKILLS_WAIT_TIMEOUT_MS"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-5-5", ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-5-5",
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-5-5", ANTHROPIC_DEFAULT_FABLE_MODEL="claude-fable-5-1",
                   ANTHROPIC_SMALL_FAST_MODEL="claude-haiku-5-5", CLAUDE_CODE_SUBAGENT_MODEL="claude-sonnet-5-5")
    # Dual provider is an ordinary-session convenience: local-only and compatibility modes
    # rewrite requests and disable Plan fallback, so they never get a second provider.
    # Ordinary sessions keep native features; unsupported provider requests retain Plan fallback.
    dual = None if isolated or compat else dual_provider_setup(provider)
    if openrouter_compat:
        # Explicit no-Plan mode needs the provider's stable protocol.
        env.update(CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1", CLAUDE_CODE_DISABLE_ADVISOR_TOOL="1")
    # The user's own Git Bash location is needed by the Bash tool; it is not provider routing.
    if isolated and "CLAUDE_CODE_GIT_BASH_PATH" in os.environ:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = os.environ["CLAUDE_CODE_GIT_BASH_PATH"]
    env.pop("LOCAL_ROUTE_COMPAT", None)
    env.pop("OPENROUTER_ROUTE_COMPAT", None)
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER", "CLI_KIND", "CLI_MODEL", "CLI_BINARY", "CLI_TIMEOUT_S"):
        env.pop(name, None)
    for name in _CLI_ENV_STRIP:
        env.pop(name, None)
    # Remove only provider credentials/routing left by the former OpenRouter setup.
    for name in ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "ANTHROPIC_BASE_URL", PRO_SESSION_GATE]:
        env.pop(name, None)
    for name in ["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"]:
        if env.get(name) == LEGACY_MODEL:
            env.pop(name)
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if not config_dir or Path(config_dir).resolve() == (Path.home() / ".claude-openrouter").resolve():
        # An explicit ~/.claude moves .claude.json inside it and hides native MCP registrations.
        env.pop("CLAUDE_CONFIG_DIR", None)
    command = [claude]
    if not isolated:
        # The probe reads the user's own login, including an env-provided OAuth token that
        # the routed child must not inherit. Base it on the caller's environment.
        probe_env = dict(os.environ)
        if "CLAUDE_CONFIG_DIR" in env:
            probe_env["CLAUDE_CONFIG_DIR"] = env["CLAUDE_CONFIG_DIR"]
        else:
            probe_env.pop("CLAUDE_CONFIG_DIR", None)
        probe_env.pop("ANTHROPIC_BASE_URL", None)
        probe_env.pop("ANTHROPIC_AUTH_TOKEN", None)
        probe_env.pop("ANTHROPIC_API_KEY", None)
        for name in _CLI_ENV_STRIP:
            probe_env.pop(name, None)
        try:
            auth = run_owned(command + ["auth", "status", "--json"], probe_env, str(Path.home()), timeout=60)
        except RuntimeError:
            raise PlanLoginError() from None
        if not (auth.get("loggedIn") is True and auth.get("authMethod") == "claude.ai"
                and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") == "pro"):
            raise PlanLoginError()
    node = shutil.which("node")
    if not node:
        if isolated:
            raise startup_error("local_node_not_found")
        raise FileNotFoundError("Node.js required for local routing")
    nonce = nonce or secrets.token_urlsafe(32)
    proxy_env = clean_env()
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER",
                 "ROUTE_ALT_PROVIDER", "ROUTE_ALT_MODEL", "ROUTE_ALT_URL", "ROUTE_ALT_KEY", "ROUTE_ALT_ALLOW_REMOTE", "ROUTE_ALT_HTTP_URL", "ROUTE_ALT_ALIAS"):
        proxy_env.pop(name, None)
    try:
        config = _config if _config is not None else provider_config(provider)
    except (OSError, ValueError):
        if local_only:
            raise LocalStartupError("local_configuration_invalid") from None
        raise
    if provider == "openrouter" and not openrouter_compat:
        for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            if env.get(name) == config["OPENROUTER_MODEL"]:
                env.pop(name)
    if dual:
        env.update(dual_cli_env(dual))
    connection_result = check_cli_backend(_backend) if _backend is not None else check_connection(provider, config=config)
    if connection_result != 0:
        print(json.dumps({"event": "provider_connection_unavailable", "provider": provider,
                          "action": "continue", "plan_fallback": not isolated}), flush=True)
    proxy_env.update(config, LOCAL_ROUTE_TOKEN=nonce, LOCAL_ROUTE_ONLY="1" if local_only else "0")
    if dual:
        proxy_env.update(dual)
    instance_id, instance_dir = str(uuid.uuid4()), None
    try:
        instance_dir = instance_directory(ROOT, instance_id)
        proxy_env.update(ROUTE_INSTANCE_ID=instance_id,
                         ROUTE_INSTANCE_METRICS_PATH=str(instance_dir / "metrics.json"),
                         ROUTE_USAGE_EVENTS_PATH=str(instance_dir / "events.jsonl"))
    except (OSError, ValueError):
        # Monitoring must not prevent the original model session from starting.
        print(json.dumps({"event": "instance_monitoring_unavailable"}), file=sys.stderr, flush=True)
    if provider in {"openrouter", "local", "cli"}:
        # Keep routing decisions for later diagnosis; -Debug only shows them live in the terminal.
        proxy_env["ROUTE_LOG_PATH"] = str(ROOT / ".route.log")
        proxy_env["ROUTE_METRICS_PATH"] = str(ROOT / ".proxy-metrics.json")
        # Opt-in frame passthrough (OpenClaude.ps1 -Stream); the proxy reads it at startup.
        if provider == "openrouter" and os.environ.get("ROUTE_STREAM_PASSTHROUGH"):
            proxy_env["ROUTE_STREAM_PASSTHROUGH"] = os.environ["ROUTE_STREAM_PASSTHROUGH"]
    proxy_env.pop("LOCAL_ROUTE_COMPAT", None)
    proxy_env.pop("OPENROUTER_ROUTE_COMPAT", None)
    if local_compat:
        proxy_env["LOCAL_ROUTE_COMPAT"] = "1"
    if openrouter_compat:
        proxy_env["OPENROUTER_ROUTE_COMPAT"] = "1"
    # Every mode ties the proxy (and its API key) to this launcher: it exits when the pipe closes.
    # A long session keeps V8 lazily expanding its heap (measured 1.1GB after 1.2M
    # requests while the live set stayed collectable at 256MB). Cap it so a routing
    # proxy cannot drift into gigabytes of resident memory.
    proxy_command = [node, "--max-old-space-size=512", str(ROOT / "claudeRouteProxy.mjs"),
                     "--parent-pipe"] + (["--port", str(port)] if port else [])
    try:
        proxy = subprocess.Popen(proxy_command, env=proxy_env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                 stderr=None if debug else subprocess.DEVNULL)
    except OSError:
        if isolated:
            raise startup_error("local_proxy_start_failed") from None
        raise
    ready = queue.Queue()
    def read_ready():
        try:
            ready.put(proxy.stdout.readline())
        except (OSError, UnicodeError):
            ready.put("")
    threading.Thread(target=read_ready, daemon=True).start()
    try:
        try:
            port = json.loads(ready.get(timeout=15))["port"]
        except (queue.Empty, ValueError, KeyError, TypeError):
            if isolated:
                raise startup_error("local_proxy_start_failed") from None
            raise RuntimeError("Local routing proxy did not start") from None
        if not isinstance(port, int) or not 1 <= port <= 65535:
            if isolated:
                raise startup_error("local_proxy_start_failed")
            raise RuntimeError("Invalid local routing port")
        headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_CUSTOM_HEADERS": (headers + "\n" if headers else "") + f"X-Local-Route-Token: {nonce}"})
        if isolated:
            # A proxy Bearer token takes precedence over saved OAuth and API-key helpers.
            env.update(ANTHROPIC_AUTH_TOKEN=nonce, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        if local_only and not local_compat:
            env.update(CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1",
                       CLAUDE_CODE_DISABLE_ADVISOR_TOOL="1", MAX_THINKING_TOKENS="0",
                       CLAUDE_CODE_AUTO_CONNECT_IDE="false", CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        log_route_marker(f"launcher start pid={os.getpid()} proxy={proxy.pid} port={port}")
        # Remember the live route so a second terminal can take it over without the CLI noticing.
        mode = "openrouter_compat" if openrouter_compat else "local_compat" if local_compat else "local_only" if local_only else "ordinary"
        write_handoff(port=port, nonce=nonce, proxy_pid=proxy.pid, provider=provider,
                      cli_pid=os.getpid(), dual=dual, mode=mode,
                      instance_id=instance_id if instance_dir else None, instance_dir=instance_dir)
        yield env
    finally:
        log_route_marker(f"launcher exit pid={os.getpid()}")
        clear_handoff(proxy_pid=proxy.pid)
        if instance_dir is not None:
            clear_instance_handoff(instance_dir, proxy.pid)
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)
        proxy.stdout.close()
        proxy.stdin.close()


def diagnose_local() -> int:
    """One synthetic Local inference; no CLI, native features, retries or Plan fallback."""
    config = provider_config("local")
    node = shutil.which("node")
    if not node:
        raise FileNotFoundError("Node.js required for Local diagnostics")
    env = clean_env()
    env.update(config)
    return subprocess.call([node, str(ROOT / "claudeRouteProxy.mjs"), "--diagnose-local"], env=env)


def check_cli_backend(backend):
    import cliBackend
    result = {"connection_check": False, "provider": "cli", "kind": backend.kind,
              "inference_checked": False, "model_confirmed": False, "reply_confirmed": False}
    try:
        result["inference_checked"] = True
        message, metadata = backend.generate({"model": backend.protocol_model, "max_tokens": 512,
            "messages": [{"role": "user", "content": "Reply with exactly OK"}]})
        reply = "".join(b.get("text", "") for b in message["content"] if b.get("type") == "text").strip()
        result.update(connection_check=reply == "OK", reply_confirmed=reply == "OK",
                      model_confirmed=bool(metadata["reported_model"] and (not backend.model or metadata["reported_model"] == backend.model)),
                      reason="ready" if reply == "OK" else "unexpected_reply")
    except cliBackend.BackendError as error:
        result["reason"] = error.code
    print(json.dumps(result), flush=True)
    return 0 if result["connection_check"] else 1


def check_connection(provider: str, config: dict | None = None) -> int:
    """One provider-only inference before interactive startup; no Plan route."""
    failed = {"connection_check": False, "provider": provider, "inference_checked": False,
              "model_confirmed": False, "reply_confirmed": False}
    try:
        if config is None:
            config = provider_config(provider)
    except (OSError, ValueError):
        print(json.dumps({**failed, "reason": "invalid_configuration"}), flush=True)
        if provider == "local":
            raise LocalStartupError("local_configuration_invalid") from None
        raise
    node = shutil.which("node")
    if not node:
        print(json.dumps({**failed, "reason": "node_unavailable"}), flush=True)
        error = LocalStartupError if provider == "local" else OpenRouterStartupError
        raise error("local_node_not_found")
    env = clean_env()
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE",
                 "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "LOCAL_ROUTE_COMPAT", "LOCAL_ROUTE_ONLY", "SONNET_PROVIDER"):
        env.pop(name, None)
    env.update(config)
    try:
        return subprocess.call([node, str(ROOT / "claudeRouteProxy.mjs"), "--check-connection"], env=env)
    except OSError:
        print(json.dumps({**failed, "reason": "connection_check_failed"}), flush=True)
        return 1


def local_cli_command(claude: str, compat: bool = False) -> list:
    try:
        version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise LocalStartupError("local_cli_version_check_failed") from None
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version.stdout.strip())
    if not match or tuple(map(int, match.groups())) < (2, 1, 304):
        raise LocalStartupError("local_cli_unsupported")
    try:
        output_tokens = float(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or "8192")
    except ValueError:
        raise LocalStartupError("local_output_budget_invalid") from None
    if not output_tokens.is_integer() or output_tokens <= 0:
        raise LocalStartupError("local_output_budget_invalid")
    # Set the budget before context calculation; safe mode still probes API-key helpers at startup.
    settings = {"alwaysThinkingEnabled": False, "disableAllHooks": True, "autoMemoryEnabled": False,
                "apiKeyHelper": "", "env": {
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(8192, int(output_tokens)))}}
    if compat:
        for name in ("alwaysThinkingEnabled", "disableAllHooks", "autoMemoryEnabled"):
            settings.pop(name)
    # Keep the inherited workspace explicit when compaction exposes transcript paths.
    workspace_context = (
        f"Local session workspace directory (JSON string): {json.dumps(str(Path.cwd()), ensure_ascii=False)}\n"
        "Resolve relative file-tool paths from this workspace unless the user explicitly specifies another base. "
        "Paths mentioned for transcripts, history, configuration or logs do not change that default base. "
        "Retain this workspace context after compaction. Existing tool permissions still apply.")
    # Safe mode keeps all file tools; retain setting sources so deny/ask rules still apply.
    if compat:
        return [claude, "--model", "sonnet", "--permission-mode", "default",
                "--settings", json.dumps(settings), "--append-system-prompt", workspace_context]
    return [claude, "--safe-mode", "--model", "sonnet", "--permission-mode", "default",
            "--tools", "Bash,Read,Edit,Write,Glob,Grep", "--disallowedTools", "mcp__*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-chrome", "--settings", json.dumps(settings), "--append-system-prompt", workspace_context]


def takeover(provider: str = "auto", debug: bool = False) -> int:
    """Hand the live port to a fresh proxy so the running CLI keeps working across a restart."""
    record = read_handoff()
    if not record:
        raise RuntimeError("No running session to take over (missing .session-handoff.json)")
    # Older records lack mode metadata and cannot prove that fallback was permitted.
    if record.get("mode") != "ordinary":
        raise RuntimeError("Session mode cannot be safely preserved during takeover; restart the session instead")
    port, nonce = record["port"], record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise RuntimeError("The recorded session has no token; restart the session normally")
    if provider == "auto":
        # The replacement must serve the same provider as the session it takes over.
        recorded = record.get("provider")
        provider = recorded if recorded in ("openrouter", "local", "cli") else configured_provider()
    if not port_open(port):
        raise RuntimeError(f"Nothing is listening on the recorded port {port}; start a normal session")
    # The recorded session may expose the other provider in the CLI's /model picker
    # (routing: dual_provider). The replacement must keep accepting the exact alias the
    # running CLI already offers, so refuse before draining when the current settings
    # would drop or change that topology: mid-session 403s on a picker entry are worse
    # than an explicit "restart instead". Credentials are rebuilt from disk either way.
    recorded_dual = record.get("dual")
    if recorded_dual is not None:
        wanted = dual_provider_setup(provider)
        topology = (isinstance(recorded_dual, dict) and isinstance(wanted, dict)
                    and wanted.get("ROUTE_ALT_PROVIDER") == recorded_dual.get("provider")
                    and wanted.get("ROUTE_ALT_ALIAS") == recorded_dual.get("alias")
                    and wanted.get("ROUTE_ALT_MODEL") == recorded_dual.get("model"))
        if not topology:
            alt = recorded_dual.get("provider") if isinstance(recorded_dual, dict) else None
            raise RuntimeError(
                f"The recorded session exposes the {alt or 'alternate'} service in /model"
                " (routing: dual_provider), but the current settings no longer provide that"
                " same alternate; restart the CLI session instead of taking over")
    # Check predictable local failures before stopping the only working proxy. This cannot
    # guarantee a successful bind, authentication or replacement startup after the drain.
    if not shutil.which("claude") or not shutil.which("node"):
        raise RuntimeError("Claude Code and Node.js are required; existing session was not changed")
    try:
        provider_config(provider)
    except (OSError, ValueError):
        raise RuntimeError("Replacement provider configuration is invalid; existing session was not changed") from None
    try:
        report = request_handover(port, nonce)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"The running proxy refused the handover: {error}") from None
    if not report.get("drained"):
        # A revert means the proxy is still listening and serving; starting a replacement
        # now would race it for the port, so stop here instead of killing anything.
        raise RuntimeError("The previous proxy is still serving an active turn; retry later")
    if not wait_for_port_release(port):
        raise RuntimeError("The previous proxy did not release its port")
    log_route_marker(f"takeover port={port} provider={provider}")
    with routed_session(provider, port=port, nonce=nonce, debug=debug):
        print(json.dumps({"takeover": True, "port": port, "provider": provider,
                          "drained": bool(report.get("drained")),
                          "waited_ms": report.get("waitedMs"),
                          "forced": bool(report.get("forced"))}), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0


_CLAUDE_MANAGEMENT = {"--help", "-h", "--version", "-v", "auth", "mcp", "plugin", "plugins", "agents", "doctor", "install", "update", "upgrade", "import", "ultrareview",
                      "attach", "logs", "respawn", "stop", "kill", "rm", "purge", "gateway", "setup-token"}

# Direct mode must not silently route a purported first-party Sonnet request to a
# different endpoint/model. The values are checked but never echoed or modified.
_SONNET_ROUTE_KEYS = frozenset({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"})


def prepare_blackbox_hook(env, native_args):
    """Connect an explicitly enabled detailed recording only to a known new session.

    User-supplied settings and resumed sessions are left untouched. The helper's
    argv form avoids shell interpretation of executable and workspace paths.
    """
    env.pop("CLAUDE_CHEAT_BLACKBOX_TOKEN", None)
    try:
        import blackbox
        recorder = blackbox.Blackbox(ROOT)
        state = recorder.status()
        if not state.get("active") or state.get("mode") != "detailed":
            return None
        flags = {value.split("=", 1)[0] for value in native_args}
        if flags & {"--resume", "-r", "--continue", "-c", "--session-id", "--fork-session",
                    "--from-pr", "--settings", "--setting-sources"}:
            print("[Claude-Cheat] 상세 블랙박스: 기존 세션/사용자 설정 인자는 자동 연결하지 않습니다. Claude는 그대로 실행합니다.", file=sys.stderr)
            return None
        helper = ROOT / "blackbox_hook.py"
        if not helper.is_file():
            raise ValueError("missing_helper")
        session_id = str(uuid.uuid4())
        config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        projects = config_dir / "projects"
        token = recorder.prepare_launch(session_id, projects)
        if token is None:
            print("[Claude-Cheat] 상세 블랙박스가 이 새 세션에 연결되지 않았습니다. 기존 기록 상태를 확인하세요.", file=sys.stderr)
            return None
        env["CLAUDE_CHEAT_BLACKBOX_TOKEN"] = token
        hook = {"SessionStart": [{"hooks": [{"type": "command", "command": sys.executable,
                                               "args": [str(helper)], "timeout": 3}]}]}
        return session_id, hook
    except (ImportError, OSError, ValueError, TypeError):
        print("[Claude-Cheat] 상세 블랙박스 연결을 준비하지 못했습니다. Claude는 그대로 실행합니다.", file=sys.stderr)
        return None


def validate_original_sonnet(env, native_args):
    flags = {value.split("=", 1)[0] for value in native_args}
    if flags & {"--model", "-m", "--settings", "--setting-sources"}:
        raise LauncherArgumentsError("original_sonnet_conflicting_arguments")
    if any(env.get(key) for key in _SONNET_ROUTE_KEYS):
        raise LauncherArgumentsError("original_sonnet_routing_environment_conflict")
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    cwd = Path.cwd()
    candidates = [config_dir / "settings.json"]
    for parent in (cwd, *cwd.parents):
        candidates.extend((parent / ".claude" / "settings.json", parent / ".claude" / "settings.local.json"))
    for path in dict.fromkeys(candidates):
        try:
            with path.open("rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except FileNotFoundError:
            continue
        except OSError:
            raise LauncherArgumentsError("original_sonnet_settings_unreadable") from None
        try:
            if len(raw) > 1024 * 1024:
                raise ValueError()
            config = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(config, dict) or not isinstance(config.get("env", {}), dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise LauncherArgumentsError("original_sonnet_settings_unreadable") from None
        if config.get("apiKeyHelper") or any(config.get("env", {}).get(key) for key in _SONNET_ROUTE_KEYS):
            raise LauncherArgumentsError("original_sonnet_settings_routing_conflict")


def original_sonnet_advisor(command, env, native_args):
    """Request Opus advisor only when the installed Claude version is known to support it."""
    if any(value.split("=", 1)[0] == "--advisor" for value in native_args):
        return []  # Explicit user selection (including off) takes precedence.
    if env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1":
        print("[Claude-Cheat] Advisor is disabled by the existing environment setting.", file=sys.stderr)
        return []
    try:
        result = routeCli.probe_version(command, routeCli.child_env("claude", env))
        version = result.get("version") if result.get("ok") else None
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("unknown_version")
        if tuple(int(x) for x in version.split(".")) < (2, 1, 260):
            raise ValueError("unsupported_version")
    except (ValueError, TypeError, OSError, routeCli.CliError):
        print("[Claude-Cheat] Opus advisor was not enabled: Claude Code version could not be verified as 2.1.260 or newer.", file=sys.stderr)
        return []
    print("[Claude-Cheat] Requesting Opus advisor; availability depends on Claude, account and policy and may use additional tokens.", file=sys.stderr)
    return ["--advisor", "opus"]


def validate_claude_args(values, *, local_only=False, compat=False, print_prompt=None):
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) or "\0" in value for value in values):
        raise LauncherArgumentsError("invalid_cli_arguments")
    result = list(values)
    if result and local_only and not compat:
        raise LauncherArgumentsError("Native CLI arguments require ordinary or compatibility mode; LocalMode is restricted")
    flags = {value.split("=", 1)[0] for value in result}
    if compat and "--settings" in flags:
        raise LauncherArgumentsError("--settings cannot override compatibility routing; use ordinary mode for custom settings")
    if print_prompt is not None and flags & {"-p", "--print", "--output-format"}:
        raise LauncherArgumentsError("Do not combine launcher -Print with native print/output arguments")
    if print_prompt is not None and result and result[0] in _CLAUDE_MANAGEMENT:
        raise LauncherArgumentsError("Do not combine launcher -Print with native management commands")
    return result


def decode_claude_args(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        values = json.loads(raw.decode("utf-8"))
        return validate_claude_args(values)
    except (ValueError, UnicodeError, binascii.Error):
        raise LauncherArgumentsError("invalid_cli_arguments") from None


def launch(provider: str = "auto", local_only: bool = False, local_compat: bool = False, openrouter_compat: bool = False, debug: bool = False,
           print_prompt: str | None = None, print_json: bool = True, cli_args: list[str] | None = None) -> int:
    native_args = validate_claude_args(cli_args or [], local_only=local_only,
                                       compat=local_compat or openrouter_compat, print_prompt=print_prompt)
    if native_args and native_args[0] in _CLAUDE_MANAGEMENT:
        claude = shutil.which("claude")
        if not claude:
            raise FileNotFoundError("Claude Code executable not found")
        return subprocess.call(routeCli.resolve_command(claude) + native_args, env=os.environ.copy())
    if provider == "auto":
        provider = configured_provider()
    if (local_only and provider != "local") or (local_compat and not local_only):
        raise ValueError("Local CLI mode requires the local provider")
    if openrouter_compat and (provider != "openrouter" or local_only or local_compat):
        raise ValueError("OpenRouter compatibility mode requires the OpenRouter provider only")
    isolated = local_only or openrouter_compat
    compat = local_compat or openrouter_compat
    startup_error = OpenRouterStartupError if openrouter_compat else LocalStartupError
    # --print runs one non-interactive turn through the same proxy, which is what makes an
    # automated end-to-end check possible.
    print_args = []
    if print_prompt is not None:
        print_args = ["-p", print_prompt] + (["--output-format", "json"] if print_json else [])
    claude = shutil.which("claude")
    if not claude:
        if isolated:
            raise startup_error("local_cli_not_found")
        raise FileNotFoundError("Claude Code executable not found")
    if provider == "sonnet":
        env = os.environ.copy()
        validate_original_sonnet(env, native_args)
        print("Original Sonnet: direct Claude Code session; proxy metrics are not available.")
        executable = routeCli.resolve_command(claude)
        command = executable + ["--model", "sonnet"] + original_sonnet_advisor(executable, env, native_args)
        recording = prepare_blackbox_hook(env, native_args)
        if recording:
            session_id, hooks = recording
            command += ["--session-id", session_id, "--settings", json.dumps({"hooks": hooks})]
        command += print_args + native_args
        return subprocess.call(command, env=env)
    if isolated and Path(claude).suffix.lower() in (".cmd", ".bat"):
        # shortcut: warn only; cmd.exe can mis-parse quotes/&/% in the workspace path or settings JSON passed through a shim.
        # Upgrade to a hard error if an injection is ever reproduced.
        print("Warning: 'claude' resolves to a .cmd/.bat shim; prefer the native claude.exe for isolated modes.", file=sys.stderr)
    if isolated:
        print("OpenRouter compatibility mode: client integrations enabled. No Plan fallback." if openrouter_compat else
              "Local compatibility mode: client integrations enabled. No Plan fallback." if local_compat else
              "Local CLI mode: built-in file/shell tools; native integrations disabled. No Plan fallback.")
        try:
            command = local_cli_command(claude, compat=True) if compat else local_cli_command(claude)
        except LocalStartupError as exc:
            if openrouter_compat:
                raise OpenRouterStartupError(exc.code) from None
            raise
        if openrouter_compat:
            prompt_index = command.index("--append-system-prompt") + 1
            command[prompt_index] = command[prompt_index].replace("Local session", "OpenRouter session")
        session = routed_session(provider, openrouter_compat=True, debug=debug) if openrouter_compat else (
            routed_session(provider, local_only=True, local_compat=True, debug=debug) if local_compat else routed_session(provider, local_only=True, debug=debug))
        with session as env:
            # CLI settings.env outranks process env: pin the same isolated route at that boundary.
            settings_index = command.index("--settings") + 1
            settings = json.loads(command[settings_index])
            for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"):
                settings["env"][name] = env[name]
            # Model and hardening pins exist only in the modes that set them.
            for name in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                         "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                         "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
                         "CLAUDE_CODE_DISABLE_ADVISOR_TOOL", "MAX_THINKING_TOKENS", "CLAUDE_CODE_AUTO_CONNECT_IDE",
                         "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"):
                if name in env:
                    settings["env"][name] = env[name]
            for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_ROUTE_COMPAT",
                         "ANTHROPIC_MODEL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                         "CLAUDE_CODE_USE_FOUNDRY", "LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY",
                         "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER", PRO_SESSION_GATE):
                settings["env"][name] = ""
            command[settings_index] = json.dumps(settings)
            try:
                return subprocess.call((routeCli.resolve_command(command[0]) + command[1:] if native_args else command) + print_args + native_args, env=env)
            except OSError:
                raise startup_error("local_cli_start_failed") from None
    print(f"Sonnet replacement: {provider}. Opus/Haiku/Fable keep Claude Plan OAuth.")
    print("One CLI backend attempt, then Plan fallback; backend tools are executed only by front Claude Code." if provider == "cli" else
          "Initial request + 3 retries, then Plan fallback (subscription Sonnet); recovery checked on a later request.")
    print("Native-only features stay on Plan. Replacement replies are buffered until validated.")
    with routed_session(provider, debug=debug) as env:
        command = [claude, "--model", "sonnet"]
        recording = prepare_blackbox_hook(env, native_args)
        session_settings = {}
        if provider in {"local", "cli"}:
            # Avoid the native Sonnet output cap exhausting a smaller Local context window.
            budget = env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or "8192"
            try:
                tokens = float(budget)
            except ValueError:
                raise LocalStartupError("local_output_budget_invalid") from None
            if not tokens.is_integer() or tokens <= 0:
                raise LocalStartupError("local_output_budget_invalid")
            session_settings["env"] = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(tokens))}
        if recording:
            session_id, hooks = recording
            command += ["--session-id", session_id]
            session_settings["hooks"] = hooks
        if session_settings:
            command += ["--settings", json.dumps(session_settings)]
        return subprocess.call((routeCli.resolve_command(command[0]) + command[1:] if native_args else command) + print_args + native_args, env=env)


def self_test() -> None:
    # Keep diagnostics independent of user settings, credentials and inherited routing.
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(globals(), ROOT=Path(directory), HANDOFF_FILE=Path(directory) / "handoff.json"), \
            patch.dict(os.environ, {}, clear=True):
        _self_test_isolated()


def _self_test_isolated() -> None:
    import contextlib
    import io
    from unittest.mock import Mock, patch

    auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
    original_env = os.environ.copy()
    with tempfile.TemporaryDirectory() as directory, patch.dict(globals(), ROOT=Path(directory)):
        for quote in ('"', "'", ""):
            (Path(directory) / ".env.openrouter").write_text("ANTHROPIC_AUTH_TOKEN=" + quote + "sk-or-test-placeholder" + quote)
            assert read_key() == "sk-or-test-placeholder"
    process = Mock(pid=123, returncode=0)
    process.communicate.side_effect = [subprocess.TimeoutExpired("claude", 1), ("", "")]
    with patch("subprocess.Popen", return_value=process), patch.object(localProcessTree, "process_identity", return_value=(123, 1)), patch.object(localProcessTree, "own_tree_pids", return_value=set()), patch("subprocess.run") as terminate:
        try:
            run_owned(["claude"], {}, ".", timeout=1)
            raise AssertionError("timeout accepted")
        except subprocess.TimeoutExpired:
            pass
        process.kill.assert_called_once()
        if os.name == "nt":
            terminate.assert_called_once()
    with contextlib.redirect_stdout(io.StringIO()) as launch_output, patch(__name__ + ".configured_provider", return_value="openrouter"), patch("shutil.which", return_value="claude"), patch("builtins.input", side_effect=AssertionError("Launch must not ask for input")), patch(__name__ + ".routed_session", return_value=contextlib.nullcontext({})), patch("subprocess.call", return_value=0) as child:
        assert launch() == 0
        child.assert_called_once()
        args = child.call_args.args[0]
        assert args == ["claude", "--model", "sonnet"]
        assert "sk-or-test-placeholder" not in repr(args)
        assert "Fable is blocked" not in launch_output.getvalue()
        assert "remain OFF" not in launch_output.getvalue()
    assert dict(os.environ) == original_env
    proxy = Mock()
    proxy.stdout.readline.return_value = '{"port": 12345}\n'
    with patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_env", return_value={"ANTHROPIC_AUTH_TOKEN": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".provider_config", return_value={"SONNET_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".check_connection", return_value=0), patch("subprocess.Popen", return_value=proxy) as start:
        with routed_session() as env:
            assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
            assert "CLAUDE_CODE_DISABLE_ADVISOR_TOOL" not in env
            assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in env
            assert "MAX_THINKING_TOKENS" not in env
            assert "CLAUDE_CONFIG_DIR" not in env
            assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
            assert "OPENROUTER_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
            assert start.call_args.kwargs["env"]["OPENROUTER_API_KEY"] == "sk-or-test-placeholder"
            assert start.call_args.kwargs["env"]["OPENROUTER_MODEL"] == "fixture/model"
            assert "OPENROUTER_MODEL" not in env
            assert "sk-or-test-placeholder" not in repr(start.call_args.args)
            assert start.call_args.kwargs["stderr"] == subprocess.DEVNULL
        proxy.terminate.assert_called_once()
    assert dict(os.environ) == original_env
    with patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_env", return_value={"ANTHROPIC_AUTH_TOKEN": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".provider_config", return_value={"SONNET_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".check_connection", return_value=0), patch("subprocess.Popen", return_value=proxy) as start:
        with routed_session(debug=True):
            assert start.call_args.kwargs["stderr"] is None
    proxy = Mock()
    proxy.stdout.readline.return_value = '{"port": 12345}\n'
    with patch.dict(os.environ, {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODELS_URL": "http://127.0.0.1:11434/v1/models", "LOCAL_LLM_MODEL": "fixture-local", "LOCAL_LLM_API_KEY": "test-local-key"}), patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_key", side_effect=AssertionError("Local must not read OpenRouter key")), patch(__name__ + ".check_connection", return_value=0), patch("subprocess.Popen", return_value=proxy) as start:
        with routed_session("local") as env:
            assert "LOCAL_LLM_API_KEY" not in env and "LOCAL_LLM_URL" not in env and "LOCAL_LLM_HTTP_URL" not in env
            assert start.call_args.kwargs["env"]["SONNET_PROVIDER"] == "local"
            assert start.call_args.kwargs["env"]["LOCAL_LLM_MODEL"] == "fixture-local"
            assert "OPENROUTER_API_KEY" not in start.call_args.kwargs["env"]
        proxy.terminate.assert_called_once()
    assert dict(os.environ) == original_env
    with tempfile.TemporaryDirectory() as directory, patch.dict(globals(), ROOT=Path(directory)):
        with patch(__name__ + ".read_key", side_effect=AssertionError("Local must not read OpenRouter key")):
            (Path(directory) / ".env.local-llm").write_text(
                "LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages\nLOCAL_LLM_MODEL=fixture-local\nLOCAL_LLM_API_KEY=test-local-key\n")
            with patch.dict(os.environ, {}, clear=True):
                config = provider_config("local")
                assert config["LOCAL_LLM_MODEL"] == "fixture-local"
                assert "OPENROUTER_API_KEY" not in config
                for invalid in ("https://example.invalid/v1/messages", "http://localhost:11434/v1/messages",
                                "http://127.0.0.1:11434/v1/messages?redirect=1", "http://user@127.0.0.1/v1/messages"):
                    with patch.dict(os.environ, {"LOCAL_LLM_URL": invalid}):
                        try:
                            provider_config("local")
                            raise AssertionError("Unsafe local URL accepted")
                        except ValueError:
                            pass
    with patch(__name__ + ".run_owned", return_value={**auth, "authMethod": "api_key"}), patch("shutil.which", return_value="executable"), patch("subprocess.Popen") as start:
        try:
            with routed_session():
                raise AssertionError("API auth accepted")
        except RuntimeError:
            pass
        start.assert_not_called()
    print("PASS: key parser, app-managed credits, Plan auth gate, native feature environment preservation, local/OpenRouter credential separation and launcher lifecycle")


def main(argv=None) -> int:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        native_args = []
        if "--" in arguments:
            split = arguments.index("--")
            arguments, native_args = arguments[:split], arguments[split + 1:]
        if arguments == ["--self-test"] and not native_args:
            self_test()
            return 0
        parser = argparse.ArgumentParser(
            description="Claude-Cheat: Sonnet replacement with Plan fallback",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=r"""Examples:
  python openrouterAdvisor.py                         Use the saved default service
  python openrouterAdvisor.py --provider openrouter   Override it for this session
  python openrouterAdvisor.py --provider local        Use Local LLM for this session
  python openrouterAdvisor.py --provider cli          Keep Claude Code; use the saved CLI model backend
  python openrouterAdvisor.py --provider sonnet       Run original Sonnet without the routing proxy
  python openrouterAdvisor.py -- --continue           Forward native Claude session arguments
  python openrouterAdvisor.py --check-local           Check Local model metadata only
  python openrouterAdvisor.py --takeover              Keep the live session's service

Setup: .\OpenClaude.ps1 -Setup
Save a default service in the dashboard to launch without --provider.
CLI backend mode keeps Claude Code as the tool host. Select claude/codex/grok in dashboard settings.
Exit codes: 0 = success, 2 = usage/service choice required, others = failure.
""")
        parser.add_argument("--provider", choices=("auto", "openrouter", "local", "cli", "sonnet"), default="auto",
                            help="auto uses the saved default, then the only configured service; explicit values override the default")
        parser.add_argument("--check-local", action="store_true", help="Check local model metadata without inference or Plan login")
        parser.add_argument("--diagnose-local", action="store_true", help="One synthetic Local inference without CLI, native features or Plan fallback")
        parser.add_argument("--local-mode", action="store_true", help="Interactive Local-only CLI with native integrations disabled and no Plan fallback")
        parser.add_argument("--local-compat", action="store_true", help="Opt-in Local-only CLI compatibility mode with client integrations and no Plan fallback")
        parser.add_argument("--openrouter-compat", action="store_true", help="Opt-in OpenRouter-only CLI compatibility mode with client integrations and no Plan fallback")
        parser.add_argument("--debug", action="store_true", help="Show proxy routing logs (route_selected, circuit_open, ...) on stderr")
        parser.add_argument("--takeover", action="store_true",
                            help="Restart the routing proxy on the running session's port (no new CLI)")
        parser.add_argument("--print", dest="print_prompt", metavar="PROMPT",
                            help="Run one non-interactive turn through the proxy and exit (automation/CI)")
        parser.add_argument("--print-text", action="store_true",
                            help="With --print, keep the CLI's human output instead of JSON")
        parser.add_argument("--cli-args-b64", default=None, help=argparse.SUPPRESS)
        options = parser.parse_args(arguments)
        if options.cli_args_b64 is not None:
            if native_args:
                parser.error("Use either -- suffix or encoded CLI arguments, not both")
            native_args = decode_claude_args(options.cli_args_b64)
        if options.takeover and (options.local_mode or options.local_compat or options.openrouter_compat
                                 or options.check_local or options.diagnose_local or options.print_prompt is not None
                                 or options.print_text):
            parser.error("--takeover cannot be combined with session modes, diagnostics or --print")
        if native_args and (options.takeover or options.check_local or options.diagnose_local):
            parser.error("Native Claude arguments cannot be combined with takeover or connection diagnostics")
        validate_claude_args(native_args, local_only=options.local_mode or options.local_compat,
                             compat=options.local_compat or options.openrouter_compat, print_prompt=options.print_prompt)
        if options.openrouter_compat and options.provider == "auto":
            # The flag itself names the provider; keep the pre-Auto behavior for this mode.
            options.provider = "openrouter"
        if options.openrouter_compat and (options.provider != "openrouter" or options.local_mode or options.local_compat or options.check_local or options.diagnose_local):
            parser.error("--openrouter-compat requires --provider openrouter and cannot be combined with Local modes or diagnostic checks")
        if options.local_compat and (options.provider != "local" or options.local_mode or options.check_local or options.diagnose_local):
            parser.error("--local-compat requires --provider local and cannot be combined with --local-mode or diagnostic checks")
        if options.local_mode and (options.provider != "local" or options.check_local or options.diagnose_local):
            parser.error("--local-mode requires --provider local and cannot be combined with diagnostic checks")
        if options.diagnose_local and (options.provider != "local" or options.check_local):
            parser.error("--diagnose-local requires --provider local and cannot be combined with --check-local")
        if options.diagnose_local:
            return diagnose_local()
        if options.check_local:
            result = check_local()
            print(json.dumps(result))
            return 0 if result["local_check"] else 1
        if options.local_mode or options.local_compat or options.openrouter_compat:
            try:
                localProcessTree.ensure_windows_local_tree()
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                error = OpenRouterStartupError if options.openrouter_compat else LocalStartupError
                raise error("local_process_cleanup_failed") from None
        if options.takeover:
            if options.provider == "sonnet":
                parser.error("--takeover is unavailable for original Sonnet sessions")
            return takeover(options.provider, debug=options.debug)
        return launch(options.provider, local_only=options.local_mode or options.local_compat,
                      local_compat=options.local_compat, openrouter_compat=options.openrouter_compat,
                      debug=options.debug, print_prompt=options.print_prompt,
                      print_json=not options.print_text, **({"cli_args": native_args} if native_args else {}))
    except ProviderChoiceError as error:
        payload = {"event": "provider_choice_required", "providers": error.providers,
                   "action": "choose_provider",
                   "commands": ["OpenClaude.ps1 -Provider " + ("CLI" if p == "cli" else p.title()) for p in error.providers]}
        print(json.dumps(payload), file=sys.stderr)
        print("Multiple services are configured. Choose one explicitly:" if "cli" in error.providers else
              "Both OpenRouter and Local settings are configured. Choose one explicitly:", file=sys.stderr)
        for provider in error.providers:
            print("  OpenClaude.ps1 -Provider " + ("CLI" if provider == "cli" else provider.title()), file=sys.stderr)
        print("  (or run OpenClaude.ps1 -Setup to save a default service)", file=sys.stderr)
        print("\n[Claude-Cheat] 실행할 서비스를 선택해야 합니다. 저장된 기본 서비스가 있으면 추가 옵션 없이 실행됩니다.", file=sys.stderr)
        print("  OpenRouter 사용: .\\OpenClaude.ps1 -Provider OpenRouter", file=sys.stderr)
        print("  Local LLM 사용:  .\\OpenClaude.ps1 -Provider Local", file=sys.stderr)
        print("  설정 확인:      .\\OpenClaude.ps1 -Setup", file=sys.stderr)
        print("  대시보드에서 서비스 설정을 저장한 뒤 '기본 실행 서비스'를 지정하면 옵션 없이 실행할 수 있습니다.", file=sys.stderr)
        print("  CLI 백엔드도 선택할 수 있으며 앞단 Claude Code의 MCP·Skills·Plugins·hooks를 유지합니다.", file=sys.stderr)
        print("  설정은 유지되며 세션은 시작하지 않았습니다. 원하는 서비스의 명령을 실행하세요.", file=sys.stderr)
        return 2
    except LauncherArgumentsError as error:
        print(json.dumps({"event": "invalid_launcher_arguments", "project": "Claude-Cheat", "code": str(error),
                          "action": "review_cli_arguments"}), file=sys.stderr)
        if str(error).startswith("original_sonnet_"):
            print("[Claude-Cheat] 원본 Sonnet 직접 실행을 확인할 수 없습니다. Claude 설정과 환경변수의 제공자 주소·인증·모델 덮어쓰기 또는 --model/--settings 인자를 확인하세요. 값은 표시하지 않았으며 설정을 변경하지 않았습니다.", file=sys.stderr)
        return 2
    except PlanLoginError:
        if os.environ.get("CLAUDE_CHEAT_HUMAN_GUIDANCE") != "1":
            print(json.dumps({"event": "claude_login_required", "project": "Claude-Cheat", "code": "plan_login_required",
                              "action": "login_front_claude", "command": "claude auth login"}), file=sys.stderr)
        print("\n[Claude-Cheat] Claude Code 로그인이 필요하여 세션을 시작하지 않았습니다.", file=sys.stderr)
        print("  1. 현재 터미널에서 claude auth login 을 실행하고 로그인을 완료하세요.", file=sys.stderr)
        print("  2. claude auth status 로 로그인 상태를 확인하세요.", file=sys.stderr)
        print("  3. OpenClaude.ps1 을 다시 실행하세요. Local LLM 설정은 그대로 유지됩니다.", file=sys.stderr)
        return 1
    except (cliBackend.BackendError, routeCli.CliError) as error:
        print(json.dumps({"event": "cli_backend_start_failed", "project": "Claude-Cheat", "code": error.code,
                          "action": "review_cli_backend", "command": ".\\OpenClaude.ps1 -Setup"}), file=sys.stderr)
        return 1
    except routeConfig.ConfigError as exc:
        print(json.dumps({"event": "routing_configuration_invalid", "code": exc.code,
                          "line": exc.line, "action": "review_default_provider",
                          "command": ".\\OpenClaude.ps1 -Setup"}), file=sys.stderr)
        print("[Claude-Cheat] 기본 실행 서비스 설정을 확인하세요. 설정 화면에서 자동 선택 또는 사용할 서비스를 저장하세요.", file=sys.stderr)
        return 2
    except (LocalStartupError, OpenRouterConfigError) as exc:
        payload = {"event": "openrouter_start_failed", "kind": type(exc).__name__,
                   "code": exc.code if isinstance(exc, LocalStartupError) else "openrouter_configuration_invalid",
                   "message": str(exc)}
        if isinstance(exc, OpenRouterConfigError):
            payload.update({"action": "open_dashboard", "dashboard": "http://127.0.0.1:8787/",
                            "command": f"powershell -File \"{ROOT / 'OpenClaudeDashboard.ps1'}\""})
            print("[setup required] openclaude.config.yaml (openrouter section) is missing or invalid", file=sys.stderr)
            print(f"  powershell -File \"{ROOT / 'OpenClaudeDashboard.ps1'}\"   ->  http://127.0.0.1:8787/",
                  file=sys.stderr)
            print("  or run: OpenClaude.ps1 -Setup   (opens the settings page in your browser)",
                  file=sys.stderr)
            print("  fill the OpenRouter settings there, save, then run OpenClaude.ps1 again",
                  file=sys.stderr)
        print(json.dumps(payload), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, EOFError, ValueError, subprocess.SubprocessError,
            http.client.HTTPException) as exc:
        message = " ".join(str(exc).split())[:300]
        print(json.dumps({"event": "openrouter_start_failed", "kind": type(exc).__name__,
                          **({"message": message} if message else {})}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
