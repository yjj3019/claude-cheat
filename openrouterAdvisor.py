"""Session-only Claude Code launcher and one stdio MCP advisor tool."""

import http.client
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
import localProcessTree
import routeConfig
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


class ProviderChoiceError(ValueError):
    """Both providers are configured; a launch must not pick one silently."""

    code = "provider_choice_required"

    def __init__(self):
        super().__init__("Both OpenRouter and Local settings are configured; choose one explicitly")


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
        provider_config(preferred)
        return preferred
    ready, failures = [], []
    for name in ("openrouter", "local"):
        try:
            provider_config(name)
        except (OSError, ValueError) as error:
            failures.append((name, error))
        else:
            ready.append(name)
    if len(ready) == 2:
        raise ProviderChoiceError()
    if len(ready) == 1:
        return ready[0]
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
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
            temporary.unlink()
        except OSError:
            pass


def write_handoff(*, port, nonce, proxy_pid, provider, cli_pid, dual=None):
    """Record the live route so another terminal can take it over without changing the port.

    ``dual`` is the non-secret alternate topology ({provider, alias, model}) an ordinary
    session exposed in the CLI's /model picker; a takeover must keep accepting the same
    alias or the already-running CLI would start getting 403s for its own model entry.
    """
    if not isinstance(port, int) or not isinstance(proxy_pid, int):
        return  # nothing usable to record (also keeps mocked sessions out of the file)
    payload = {"port": port, "nonce": nonce, "proxy_pid": proxy_pid, "launcher_pid": cli_pid,
               "provider": provider, "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if dual:
        payload["dual"] = {"provider": dual["ROUTE_ALT_PROVIDER"], "alias": dual["ROUTE_ALT_ALIAS"],
                           "model": dual["ROUTE_ALT_MODEL"]}
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
                  openrouter_compat: bool = False, debug: bool = False,
                  port: int | None = None, nonce: str | None = None):
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
                     "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTOCOMPACT_PCT_OVERRIDE"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-5-5", ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-5-5",
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-5-5", ANTHROPIC_DEFAULT_FABLE_MODEL="claude-fable-5-1",
                   ANTHROPIC_SMALL_FAST_MODEL="claude-haiku-5-5", CLAUDE_CODE_SUBAGENT_MODEL="claude-sonnet-5-5")
    # Dual provider is an ordinary-session convenience: local-only and compatibility modes
    # rewrite requests and disable Plan fallback, so they never get a second provider.
    # Resolved before the CLI env so a dual OpenRouter participant also pins the stable
    # protocol below; the /model entry itself is applied after the env cleanup that follows.
    dual = None if isolated or compat else dual_provider_setup(provider)
    if provider == "openrouter" or (dual is not None and dual["ROUTE_ALT_PROVIDER"] == "openrouter"):
        # Claude Code 2.1.310 sends experimental betas whose request shape (inline tool_addition
        # blocks, context_management, advisor tool) the Messages API providers reject with HTTP 400.
        # Keep the routed session on the stable protocol; thinking and effort stay per request.
        env.update(CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1", CLAUDE_CODE_DISABLE_ADVISOR_TOOL="1")
    # The user's own Git Bash location is needed by the Bash tool; it is not provider routing.
    if isolated and "CLAUDE_CODE_GIT_BASH_PATH" in os.environ:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = os.environ["CLAUDE_CODE_GIT_BASH_PATH"]
    env.pop("LOCAL_ROUTE_COMPAT", None)
    env.pop("OPENROUTER_ROUTE_COMPAT", None)
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER"):
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
        env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
    command = [claude]
    if not isolated:
        # The probe reads the user's own login, including an env-provided OAuth token that
        # the routed child must not inherit. Base it on the caller's environment.
        probe_env = dict(os.environ)
        probe_env["CLAUDE_CONFIG_DIR"] = env["CLAUDE_CONFIG_DIR"]
        probe_env.pop("ANTHROPIC_BASE_URL", None)
        probe_env.pop("ANTHROPIC_AUTH_TOKEN", None)
        probe_env.pop("ANTHROPIC_API_KEY", None)
        for name in _CLI_ENV_STRIP:
            probe_env.pop(name, None)
        auth = run_owned(command + ["auth", "status", "--json"], probe_env, str(Path.home()), timeout=60)
        if not (auth.get("loggedIn") is True and auth.get("authMethod") == "claude.ai"
                and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") == "pro"):
            raise RuntimeError("Existing first-party Plan login required; no API fallback")
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
        config = provider_config(provider)
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
    if check_connection(provider, config=config) != 0:
        print(json.dumps({"event": "provider_connection_unavailable", "provider": provider,
                          "action": "continue", "plan_fallback": not isolated}), flush=True)
    proxy_env.update(config, LOCAL_ROUTE_TOKEN=nonce, LOCAL_ROUTE_ONLY="1" if local_only else "0")
    if dual:
        proxy_env.update(dual)
    if provider == "openrouter":
        # Keep routing decisions for later diagnosis; -Debug only shows them live in the terminal.
        proxy_env["ROUTE_LOG_PATH"] = str(ROOT / ".route.log")
        proxy_env["ROUTE_METRICS_PATH"] = str(ROOT / ".proxy-metrics.json")
        # Opt-in frame passthrough (OpenClaude.ps1 -Stream); the proxy reads it at startup.
        if os.environ.get("ROUTE_STREAM_PASSTHROUGH"):
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
        write_handoff(port=port, nonce=nonce, proxy_pid=proxy.pid, provider=provider,
                      cli_pid=os.getpid(), dual=dual)
        yield env
    finally:
        log_route_marker(f"launcher exit pid={os.getpid()}")
        clear_handoff(proxy_pid=proxy.pid)
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
    port, nonce = record["port"], record.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise RuntimeError("The recorded session has no token; restart the session normally")
    if provider == "auto":
        # The replacement must serve the same provider as the session it takes over.
        recorded = record.get("provider")
        provider = recorded if recorded in ("openrouter", "local") else configured_provider()
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


def launch(provider: str = "auto", local_only: bool = False, local_compat: bool = False, openrouter_compat: bool = False, debug: bool = False,
           print_prompt: str | None = None, print_json: bool = True) -> int:
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
                return subprocess.call(command + print_args, env=env)
            except OSError:
                raise startup_error("local_cli_start_failed") from None
    print(f"Sonnet replacement: {provider}. Opus/Haiku/Fable keep Claude Plan OAuth.")
    print("Initial request + 3 retries, then Plan fallback (subscription Sonnet); recovery checked on a later request.")
    print("Native-only features stay on Plan. Replacement replies are buffered until validated.")
    with routed_session(provider, debug=debug) as env:
        return subprocess.call([claude, "--model", "sonnet"] + print_args, env=env)


def self_test() -> None:
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
    with contextlib.redirect_stdout(io.StringIO()) as launch_output, patch("shutil.which", return_value="claude"), patch("builtins.input", side_effect=AssertionError("Launch must not ask for input")), patch(__name__ + ".routed_session", return_value=contextlib.nullcontext({})), patch("subprocess.call", return_value=0) as child:
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
            assert env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"
            assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
            assert "MAX_THINKING_TOKENS" not in env
            assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".claude")
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
        if arguments == ["--self-test"]:
            self_test()
            return 0
        parser = argparse.ArgumentParser(
            description="Claude-Cheat: Sonnet replacement with Plan fallback",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=r"""Examples:
  python openrouterAdvisor.py                         Use the saved default service
  python openrouterAdvisor.py --provider openrouter   Override it for this session
  python openrouterAdvisor.py --provider local        Use Local LLM for this session
  python openrouterAdvisor.py --check-local           Check Local model metadata only
  python openrouterAdvisor.py --takeover              Keep the live session's service

Setup: .\OpenClaude.ps1 -Setup
Save a default service in the dashboard to launch without --provider.
CLI settings are for connection tests, not request routing.
Exit codes: 0 = success, 2 = usage/service choice required, others = failure.
""")
        parser.add_argument("--provider", choices=("auto", "openrouter", "local"), default="auto",
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
        options = parser.parse_args(arguments)
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
            return takeover(options.provider, debug=options.debug)
        return launch(options.provider, local_only=options.local_mode or options.local_compat,
                      local_compat=options.local_compat, openrouter_compat=options.openrouter_compat,
                      debug=options.debug, print_prompt=options.print_prompt,
                      print_json=not options.print_text)
    except ProviderChoiceError:
        payload = {"event": "provider_choice_required", "providers": ["openrouter", "local"],
                   "action": "choose_provider",
                   "commands": ["OpenClaude.ps1 -Provider OpenRouter", "OpenClaude.ps1 -Provider Local"]}
        print(json.dumps(payload), file=sys.stderr)
        print("Both OpenRouter and Local settings are configured. Choose one explicitly:", file=sys.stderr)
        print("  OpenClaude.ps1 -Provider OpenRouter", file=sys.stderr)
        print("  OpenClaude.ps1 -Provider Local", file=sys.stderr)
        print("  (or run OpenClaude.ps1 -Setup to save a default service)", file=sys.stderr)
        print("\n[Claude-Cheat] OpenRouter와 Local LLM 설정이 모두 있어 실행할 서비스를 선택해야 합니다.", file=sys.stderr)
        print("  OpenRouter 사용: .\\OpenClaude.ps1 -Provider OpenRouter", file=sys.stderr)
        print("  Local LLM 사용:  .\\OpenClaude.ps1 -Provider Local", file=sys.stderr)
        print("  설정 확인:      .\\OpenClaude.ps1 -Setup", file=sys.stderr)
        print("  대시보드에서 서비스 설정을 저장한 뒤 '기본 실행 서비스'를 지정하면 옵션 없이 실행할 수 있습니다.", file=sys.stderr)
        print("  CLI 항목은 연결 테스트 전용입니다. CLI를 라우팅 서비스로 실행할 수는 없습니다.", file=sys.stderr)
        print("  설정은 유지되며 세션은 시작하지 않았습니다. 원하는 서비스의 명령을 실행하세요.", file=sys.stderr)
        return 2
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
