"""Session-only Claude Code launcher and one stdio MCP advisor tool."""

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
import secrets
import threading
import argparse
import localProcessTree
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
    "local_configuration_invalid": "Invalid Local configuration. Check .env.local-llm and process environment: Messages URL, HTTP approval, model, key format, and same-origin model-list URL. Values hidden.",
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
            "Set a valid ANTHROPIC_AUTH_TOKEN and OPENROUTER_MODEL in .env.openrouter beside openrouterAdvisor.py. "
            "No default model is selected. Values hidden. "
            "Or run startClaudeDashboard.ps1, open http://127.0.0.1:8787/ and fill the OpenRouter settings there.")


def read_env(path: Path) -> dict:
    # shortcut: single-line KEY=VALUE only; add a dotenv library if multiline values are needed.
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            raise ValueError("Invalid provider environment entry; value hidden")
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def read_key(values: dict | None = None) -> str:
    if values is None:
        values = read_env(ROOT / ".env.openrouter")
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
            values = read_env(ROOT / ".env.openrouter")
            model = values.get("OPENROUTER_MODEL", "").strip()
            if not re.fullmatch(r"[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}", model) or model.startswith(("REPLACE_WITH_", "YOUR_")):
                raise ValueError("Invalid OpenRouter model")
            return {"SONNET_PROVIDER": provider, "OPENROUTER_API_KEY": read_key(values), "OPENROUTER_MODEL": model}
        except (OSError, ValueError):
            raise OpenRouterConfigError() from None
    if provider != "local":
        raise ValueError("Unknown Sonnet provider")
    path = ROOT / ".env.local-llm"
    values = read_env(path) if path.is_file() else {}
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
        raise ValueError("Set LOCAL_LLM_MODEL in .env.local-llm or the process environment")
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


def clean_env() -> dict:
    # Keep OS execution variables; never inherit another provider's auth or model routing.
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_", "OPENROUTER_"))
            and k.upper() not in {"CLAUDE_CONFIG_DIR", "CLAUDECODE", PRO_SESSION_GATE}}


def run_owned(args: list, env: dict, cwd: str, prompt: str = "", timeout: int = 120) -> dict:
    process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    try:
        output, _ = process.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
        finally:
            process.kill()
            process.communicate(timeout=5)
        raise
    if process.returncode:
        raise RuntimeError("Claude Plan request failed; no fallback attempted")
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("Expected Claude JSON response")
    return result


@contextlib.contextmanager
def routed_session(provider: str = "openrouter", local_only: bool = False, local_compat: bool = False, openrouter_compat: bool = False, debug: bool = False):
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
    if provider == "openrouter":
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
        auth = run_owned(command + ["auth", "status", "--json"], env, str(Path.home()), timeout=60)
        if not (auth.get("loggedIn") is True and auth.get("authMethod") == "claude.ai"
                and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") == "pro"):
            raise RuntimeError("Existing first-party Plan login required; no API fallback")
    node = shutil.which("node")
    if not node:
        if isolated:
            raise startup_error("local_node_not_found")
        raise FileNotFoundError("Node.js required for local routing")
    nonce = secrets.token_urlsafe(32)
    proxy_env = clean_env()
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER"):
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
    if check_connection(provider, config=config) != 0:
        print(json.dumps({"event": "provider_connection_unavailable", "provider": provider,
                          "action": "continue", "plan_fallback": not isolated}), flush=True)
    proxy_env.update(config, LOCAL_ROUTE_TOKEN=nonce, LOCAL_ROUTE_ONLY="1" if local_only else "0")
    if provider == "openrouter":
        # Keep routing decisions for later diagnosis; -Debug only shows them live in the terminal.
        proxy_env["ROUTE_LOG_PATH"] = str(ROOT / ".route.log")
        proxy_env["ROUTE_METRICS_PATH"] = str(ROOT / ".proxy-metrics.json")
    proxy_env.pop("LOCAL_ROUTE_COMPAT", None)
    proxy_env.pop("OPENROUTER_ROUTE_COMPAT", None)
    if local_compat:
        proxy_env["LOCAL_ROUTE_COMPAT"] = "1"
    if openrouter_compat:
        proxy_env["OPENROUTER_ROUTE_COMPAT"] = "1"
    # Every mode ties the proxy (and its API key) to this launcher: it exits when the pipe closes.
    proxy_command = [node, str(ROOT / "claudeRouteProxy.mjs"), "--parent-pipe"]
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
        yield env
    finally:
        log_route_marker(f"launcher exit pid={os.getpid()}")
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


def launch(provider: str = "openrouter", local_only: bool = False, local_compat: bool = False, openrouter_compat: bool = False, debug: bool = False) -> int:
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
                return subprocess.call(command, env=env)
            except OSError:
                raise startup_error("local_cli_start_failed") from None
    print(f"Sonnet replacement: {provider}. Opus/Haiku/Fable keep Claude Plan OAuth.")
    print("Initial request + 3 retries, then Plan fallback (subscription Sonnet); recovery checked on a later request.")
    print("Native-only features stay on Plan. Replacement replies are buffered until validated.")
    with routed_session(provider, debug=debug) as env:
        return subprocess.call([claude, "--model", "sonnet"], env=env)


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
    with patch("subprocess.Popen", return_value=process), patch("subprocess.run") as terminate:
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
    with patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_env", return_value={"ANTHROPIC_AUTH_TOKEN": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".check_connection", return_value=0), patch("subprocess.Popen", return_value=proxy) as start:
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
    with patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_env", return_value={"ANTHROPIC_AUTH_TOKEN": "sk-or-test-placeholder", "OPENROUTER_MODEL": "fixture/model"}), patch(__name__ + ".check_connection", return_value=0), patch("subprocess.Popen", return_value=proxy) as start:
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
        parser = argparse.ArgumentParser(description="Sonnet replacement with Plan fallback")
        parser.add_argument("--provider", choices=("openrouter", "local"), default="openrouter")
        parser.add_argument("--check-local", action="store_true", help="Check local model metadata without inference or Plan login")
        parser.add_argument("--diagnose-local", action="store_true", help="One synthetic Local inference without CLI, native features or Plan fallback")
        parser.add_argument("--local-mode", action="store_true", help="Interactive Local-only CLI with native integrations disabled and no Plan fallback")
        parser.add_argument("--local-compat", action="store_true", help="Opt-in Local-only CLI compatibility mode with client integrations and no Plan fallback")
        parser.add_argument("--openrouter-compat", action="store_true", help="Opt-in OpenRouter-only CLI compatibility mode with client integrations and no Plan fallback")
        parser.add_argument("--debug", action="store_true", help="Show proxy routing logs (route_selected, circuit_open, ...) on stderr")
        options = parser.parse_args(arguments)
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
        return launch(options.provider, local_only=options.local_mode or options.local_compat, local_compat=options.local_compat, openrouter_compat=options.openrouter_compat, debug=options.debug)
    except (LocalStartupError, OpenRouterConfigError) as exc:
        payload = {"event": "openrouter_start_failed", "kind": type(exc).__name__,
                   "code": exc.code if isinstance(exc, LocalStartupError) else "openrouter_configuration_invalid",
                   "message": str(exc)}
        if isinstance(exc, OpenRouterConfigError):
            payload.update({"action": "open_dashboard", "dashboard": "http://127.0.0.1:8787/",
                            "command": "powershell -File startClaudeDashboard.ps1"})
            print("[setup required] .env.openrouter is missing or invalid", file=sys.stderr)
            print("  powershell -File startClaudeDashboard.ps1   ->  http://127.0.0.1:8787/", file=sys.stderr)
            print("  fill the OpenRouter settings there, save, then run startClaudeOpenRouter.ps1 again",
                  file=sys.stderr)
        print(json.dumps(payload), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, EOFError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"event": "openrouter_start_failed", "kind": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
