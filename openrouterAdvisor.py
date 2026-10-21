"""Session-only Claude Code launcher and one stdio MCP advisor tool."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import contextlib
import queue
import secrets
import shlex
import threading
import argparse
from urllib.parse import urlsplit, urlunsplit
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"
PRO_MODELS = {"opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}
PRO_SESSION_GATE = "CLAUDE_PRO_CONSULT_ENABLED"
TOOL_NAME = "pro_consult"
TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Ask Opus, Haiku or Fable using the existing Claude Plan login, with no API fallback. "
        "Model availability and usage credits are managed by Claude and the account plan. "
        "Include relevant evidence and prior advice; this advisor has no file access or memory. "
        "Do not include secrets."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 64000},
                       "model": {"type": "string", "enum": list(PRO_MODELS)}},
        "required": ["model", "question"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": True},
}


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


def read_key() -> str:
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
        return {"SONNET_PROVIDER": provider, "OPENROUTER_API_KEY": read_key()}
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


def pro_command() -> tuple[list, dict]:
    claude = shutil.which("claude")
    if not claude:
        raise FileNotFoundError("Claude Code executable not found")
    env = clean_env()
    # Verified saved Plan login location; credentials are read only by the official CLI.
    env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
    settings = {"forceLoginMethod": "claudeai", "fastMode": False,
                "availableModels": list(PRO_MODELS.values()), "enforceAvailableModels": True}
    return [claude, "--safe-mode", "--setting-sources", "", "--settings", json.dumps(settings)], env


def consult(model: str, question: str) -> dict:
    if not isinstance(model, str) or model not in PRO_MODELS:
        raise ValueError("Unsupported consultation model")
    if not isinstance(question, str) or not question.strip() or len(question) > 64000:
        raise ValueError("question must contain 1 to 64000 characters")
    if os.environ.get(PRO_SESSION_GATE) != "launcher-enabled":
        raise RuntimeError("Start through the launcher to enable Plan consultation")
    command, env = pro_command()
    with tempfile.TemporaryDirectory(prefix="claude-pro-consult-") as cwd:
        auth = run_owned(command + ["auth", "status", "--json"], env, cwd, timeout=30)
        if not (auth.get("loggedIn") is True and auth.get("authMethod") == "claude.ai"
                and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") == "pro"):
            raise RuntimeError("Existing first-party Plan login required; no API fallback")
        data = run_owned(command + ["-p", "--model", PRO_MODELS[model], "--tools", "",
                         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                         "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
                         "--output-format", "json", "--permission-mode", "dontAsk",
                         "--system-prompt", "Give concise independent advice using only the supplied evidence. No tools or file access. Distinguish evidence from assumptions."],
                         env, cwd, prompt=question)
    if data.get("is_error") is not False or data.get("subtype") != "success":
        raise RuntimeError("Plan consultation failed; no fallback attempted")
    if set(data.get("modelUsage", {})) != {PRO_MODELS[model]}:
        raise ValueError("Response did not confirm the exact requested Plan model")
    advice = data.get("result")
    if not isinstance(advice, str) or not advice.strip():
        raise ValueError("Claude returned no textual advice")
    return {"status": "ok", "model": PRO_MODELS[model], "auth": "Claude Plan",
            "credits_state": "managed-by-claude", "advice": advice}


def handle(request: dict) -> dict | None:
    request_id = request.get("id")
    if "id" not in request:
        return None
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "claude_pro_advisor", "version": "2.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        try:
            params = request.get("params", {})
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("Expected tool arguments")
            if params.get("name") == TOOL_NAME and set(arguments) == {"model", "question"}:
                answer = consult(arguments["model"], arguments["question"])
            else:
                raise ValueError("Unknown tool or invalid arguments")
            result = {"content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}]}
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
            detail = type(exc).__name__
            print(json.dumps({"event": "advisor_error", "kind": detail}), file=sys.stderr, flush=True)
            result = {"isError": True, "content": [{"type": "text", "text": f"Plan consultation stopped ({detail}); no successful advice. Check Claude login and account availability. No API or OpenRouter fallback was attempted."}]}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve() -> None:
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Expected JSON-RPC object")
            response = handle(request)
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=True), flush=True)


def file_mcp_sources() -> list[str]:
    # Reference original files, so MCP credentials never enter CLI JSON or new files.
    sources = []
    user_config = Path.home() / ".claude.json"
    if user_config.exists():
        data = json.loads(user_config.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            raise ValueError("Invalid user MCP registration structure; values hidden")
        if data.get("mcpServers"):
            sources.append(str(user_config))
    current = Path.cwd().resolve()
    for directory in (current, *current.parents):
        project_config = directory / ".mcp.json"
        if project_config.is_file():
            sources.append(str(project_config))
            break
        if (directory / ".git").exists():
            break
    return sources


@contextlib.contextmanager
def routed_session(provider: str = "openrouter", local_only: bool = False):
    """Own one loopback proxy; ordinary sessions alone use the subscription login."""
    if local_only and provider != "local":
        raise ValueError("Local CLI mode requires the local provider")
    claude = shutil.which("claude")
    if not claude:
        raise FileNotFoundError("Claude Code executable not found")
    env = clean_env() if local_only else os.environ.copy()
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER"):
        env.pop(name, None)
    # Remove only provider credentials/routing left by the former OpenRouter setup.
    for name in ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL", PRO_SESSION_GATE]:
        env.pop(name, None)
    for name in ["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"]:
        if env.get(name) == DEEPSEEK:
            env.pop(name)
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if not config_dir or Path(config_dir).resolve() == (Path.home() / ".claude-openrouter").resolve():
        env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
    command = [claude]
    if not local_only:
        auth = run_owned(command + ["auth", "status", "--json"], env, str(Path.home()), timeout=60)
        if not (auth.get("loggedIn") is True and auth.get("authMethod") == "claude.ai"
                and auth.get("apiProvider") == "firstParty" and auth.get("subscriptionType") == "pro"):
            raise RuntimeError("Existing first-party Plan login required; no API fallback")
    node = shutil.which("node")
    if not node:
        raise FileNotFoundError("Node.js required for local routing")
    nonce = secrets.token_urlsafe(32)
    proxy_env = clean_env()
    for name in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_ALLOW_REMOTE", "LOCAL_LLM_HTTP_URL", "LOCAL_LLM_MODELS_URL", "SONNET_PROVIDER"):
        proxy_env.pop(name, None)
    proxy_env.update(provider_config(provider), LOCAL_ROUTE_TOKEN=nonce, LOCAL_ROUTE_ONLY="1" if local_only else "0")
    proxy = subprocess.Popen([node, str(ROOT / "claudeRouteProxy.mjs")], env=proxy_env,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8")
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(proxy.stdout.readline()), daemon=True).start()
    try:
        try:
            port = json.loads(ready.get(timeout=15))["port"]
        except (queue.Empty, ValueError, KeyError, TypeError):
            raise RuntimeError("Local routing proxy did not start") from None
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError("Invalid local routing port")
        headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_CUSTOM_HEADERS": (headers + "\n" if headers else "") + f"X-Local-Route-Token: {nonce}"})
        if local_only:
            env.update(ANTHROPIC_API_KEY=nonce, CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1",
                       CLAUDE_CODE_DISABLE_ADVISOR_TOOL="1", MAX_THINKING_TOKENS="0")
        yield env
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait(timeout=5)
        proxy.stdout.close()


def hud_configuration() -> tuple[dict, list[str]]:
    """Load only the installed HUD, without importing provider settings or other plugins."""
    plugin_base = Path.home() / ".claude-openrouter" / "plugins"
    registry = plugin_base / "installed_plugins.json"
    if not registry.is_file():
        return {}, []
    data = json.loads(registry.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("plugins", {}), dict):
        raise ValueError("Invalid HUD plugin registry")
    entries = data.get("plugins", {}).get("claude-hud@claude-hud", [])
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("Invalid HUD installation records")
    entry = next((item for item in entries if item.get("scope") == "user"), None)
    if entry is None:
        return {}, []
    if not isinstance(entry.get("installPath"), str):
        raise ValueError("Invalid HUD installation path")
    plugin = Path(entry["installPath"]).resolve()
    if not plugin.is_relative_to((plugin_base / "cache").resolve()):
        raise ValueError("HUD installation must remain within its plugin cache")
    renderer = plugin / "dist" / "index.js"
    node = shutil.which("node")
    if not node or not renderer.is_file() or not (plugin / ".claude-plugin" / "plugin.json").is_file():
        raise RuntimeError("Installed HUD renderer or Node.js is unavailable")
    # Claude Code uses the installed Git Bash for status lines on this Windows host.
    command = f'{shlex.quote(Path(node).resolve().as_posix())} {shlex.quote(renderer.as_posix())}'
    return {"statusLine": {"type": "command", "command": command}}, ["--plugin-dir", str(plugin)]


def diagnose_local() -> int:
    """One synthetic Local inference; no CLI, native features, retries or Plan fallback."""
    config = provider_config("local")
    node = shutil.which("node")
    if not node:
        raise FileNotFoundError("Node.js required for Local diagnostics")
    env = clean_env()
    env.update(config)
    return subprocess.call([node, str(ROOT / "claudeRouteProxy.mjs"), "--diagnose-local"], env=env)


def local_cli_command(claude: str) -> list:
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=10, check=True)
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version.stdout.strip())
    if not match or tuple(map(int, match.groups())) < (2, 1, 304):
        raise RuntimeError("Local CLI mode requires Claude Code 2.1.304 or later")
    return [claude, "--bare", "--model", "sonnet", "--permission-mode", "default",
            "--tools", "Bash,Read,Edit,Write,Glob,Grep", "--settings", json.dumps({"alwaysThinkingEnabled": False})]


def launch(provider: str = "openrouter", local_only: bool = False) -> int:
    claude = shutil.which("claude")
    if not claude:
        raise FileNotFoundError("Claude Code executable not found")
    if local_only:
        print("Local CLI mode: built-in file/shell tools; native integrations disabled. No Plan fallback.")
        command = local_cli_command(claude)
        with routed_session(provider, local_only=True) as env:
            return subprocess.call(command, env=env)
    print(f"Sonnet replacement: {provider}. Opus/Haiku/Fable keep Claude Plan OAuth.")
    print("Initial request + 3 retries, then Plan fallback (subscription Sonnet); recovery checked on a later request.")
    print("Native-only features stay on Plan. Replacement replies are buffered until validated.")
    with routed_session(provider) as env:
        return subprocess.call([claude, "--model", "sonnet"], env=env)


def self_test() -> None:
    import contextlib
    import io
    from unittest.mock import Mock, patch

    assert handle({"id": 1, "method": "initialize"})["result"]["protocolVersion"] == "2025-06-18"
    assert handle({"id": 2, "method": "tools/list"})["result"]["tools"][0]["name"] == TOOL_NAME
    assert handle({"method": "notifications/initialized"}) is None
    assert handle({"id": 3, "method": "unknown"})["error"]["code"] == -32601
    auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
    original_env = os.environ.copy()
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        with patch("pathlib.Path.home", return_value=home), patch("shutil.which", return_value="node"):
            assert hud_configuration() == ({}, [])
            plugin_base = home / ".claude-openrouter" / "plugins"
            plugin = plugin_base / "cache" / "claude-hud" / "claude-hud" / "test"
            (plugin / "dist").mkdir(parents=True)
            (plugin / ".claude-plugin").mkdir()
            (plugin / "dist" / "index.js").touch()
            (plugin / ".claude-plugin" / "plugin.json").write_text('{}')
            registry = plugin_base / "installed_plugins.json"
            data = {"plugins": {"claude-hud@claude-hud": [{"scope": "user", "installPath": str(plugin)}]}}
            registry.write_text(json.dumps(data))
            hud, plugin_args = hud_configuration()
            assert hud["statusLine"]["type"] == "command" and plugin_args == ["--plugin-dir", str(plugin.resolve())]
            data["plugins"]["claude-hud@claude-hud"][0]["installPath"] = str(home / "untrusted")
            registry.write_text(json.dumps(data))
            try:
                hud_configuration()
                raise AssertionError("HUD path outside plugin cache accepted")
            except ValueError:
                pass
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        home = base / "home"
        home.mkdir()
        user_config = home / ".claude.json"
        user_config.write_text(json.dumps({"mcpServers": {"existing": {"type": "http", "url": "https://example.invalid", "headers": {"Authorization": "test-secret"}}}}))
        project = base / "project"
        project.mkdir()
        (project / ".git").mkdir()
        nested = project / "nested"
        nested.mkdir()
        outer = base / ".mcp.json"
        outer.write_text('{"mcpServers":{}}')
        with patch("pathlib.Path.home", return_value=home), patch("pathlib.Path.cwd", return_value=nested):
            assert file_mcp_sources() == [str(user_config)]  # stop at repository boundary
            project_config = project / ".mcp.json"
            project_config.write_text('{"mcpServers":{"project-server":{"command":"test"}}}')
            assert file_mcp_sources() == [str(user_config), str(project_config)]
            local_config = nested / ".mcp.json"
            local_config.write_text('{"mcpServers":{}}')
            assert file_mcp_sources() == [str(user_config), str(local_config)]
            user_config.write_text('[]')
            try:
                file_mcp_sources()
                raise AssertionError("Invalid MCP source accepted")
            except ValueError:
                pass
    with tempfile.TemporaryDirectory() as directory, patch.dict(globals(), ROOT=Path(directory)):
        for quote in ('"', "'", ""):
            (Path(directory) / ".env.openrouter").write_text("ANTHROPIC_AUTH_TOKEN=" + quote + "sk-or-test-placeholder" + quote)
            assert read_key() == "sk-or-test-placeholder"
    with contextlib.redirect_stderr(io.StringIO()), patch("subprocess.Popen") as spawn:
        for model in ("unknown", "sonnet", "opus[1m]", "anthropic/claude-opus-5.5", None):
            failure = handle({"id": 4, "method": "tools/call", "params": {"name": TOOL_NAME, "arguments": {"model": model, "question": "check"}}})
            assert failure["result"]["isError"] is True
        with patch.dict(os.environ, {PRO_SESSION_GATE: ""}):
            failure = handle({"id": 4, "method": "tools/call", "params": {"name": TOOL_NAME, "arguments": {"model": "opus", "question": "check"}}})
            assert failure["result"]["isError"] is True
        spawn.assert_not_called()
    hostile = {"ANTHROPIC_AUTH_TOKEN": "test-secret", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
               "CLAUDE_CODE_OAUTH_TOKEN": "test-oauth", "OPENROUTER_API_KEY": "test-secret",
               "CLAUDE_CONFIG_DIR": "other", "CLAUDECODE": "1", PRO_SESSION_GATE: "launcher-enabled"}
    with patch.dict(os.environ, hostile), patch("shutil.which", return_value="claude"):
        for model, actual in PRO_MODELS.items():
            response = {"is_error": False, "subtype": "success", "result": "4", "modelUsage": {actual: {}}}
            with patch(__name__ + ".run_owned", side_effect=[auth, response]) as run:
                answer = consult(model, "2 + 2?")
                assert answer["model"] == actual
                assert answer["auth"] == "Claude Plan"
                assert answer["credits_state"] == "managed-by-claude"
                assert run.call_count == 2
                args, env, cwd = run.call_args.args
                assert "--bare" not in args and "--fallback-model" not in args
                assert "--safe-mode" in args and "--strict-mcp-config" in args
                assert all(not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "OPENROUTER_")) for key in env)
                assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".claude")
                assert "test-secret" not in repr(run.call_args_list)
            for bad_auth in ({}, dict(auth, authMethod="api_key"), dict(auth, subscriptionType="max"), dict(auth, apiProvider="thirdParty")):
                with patch(__name__ + ".run_owned", return_value=bad_auth) as run:
                    try:
                        consult(model, "check")
                        raise AssertionError("bad auth accepted")
                    except RuntimeError:
                        pass
                    assert run.call_count == 1
            for bad in ({}, dict(response, is_error=True), dict(response, modelUsage={"claude-sonnet-5-5": {}}), dict(response, result="")):
                with patch(__name__ + ".run_owned", side_effect=[auth, bad]) as run:
                    try:
                        consult(model, "check")
                        raise AssertionError("bad response accepted")
                    except (ValueError, RuntimeError):
                        pass
                    assert run.call_count == 2
        for failure in (RuntimeError("quota exceeded"), subprocess.TimeoutExpired("claude", 120), OSError("offline")):
            with patch(__name__ + ".run_owned", side_effect=[auth, failure]) as run:
                try:
                    consult("opus", "check")
                    raise AssertionError("failed call accepted")
                except (RuntimeError, subprocess.SubprocessError, OSError):
                    pass
                assert run.call_count == 2  # one auth check, one attempt; no fallback or retry
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
    with contextlib.redirect_stdout(io.StringIO()) as launch_output, patch("shutil.which", return_value="claude"), patch("builtins.input", side_effect=AssertionError("Launch must not ask for input")), patch(__name__ + ".routed_session", return_value=contextlib.nullcontext({PRO_SESSION_GATE: "launcher-enabled"})), patch("subprocess.call", return_value=0) as child:
        assert launch() == 0
        child.assert_called_once()
        assert child.call_args.kwargs["env"][PRO_SESSION_GATE] == "launcher-enabled"
        args = child.call_args.args[0]
        assert args == ["claude", "--model", "sonnet"]
        assert "sk-or-test-placeholder" not in repr(args)
        assert "Fable is blocked" not in launch_output.getvalue()
        assert "remain OFF" not in launch_output.getvalue()
    assert dict(os.environ) == original_env
    proxy = Mock()
    proxy.stdout.readline.return_value = '{"port": 12345}\n'
    with patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_key", return_value="sk-or-test-placeholder"), patch("subprocess.Popen", return_value=proxy) as start:
        with routed_session() as env:
            assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
            assert env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == os.environ.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL")
            assert env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") == os.environ.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS")
            assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".claude")
            assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
            assert "OPENROUTER_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
            assert start.call_args.kwargs["env"]["OPENROUTER_API_KEY"] == "sk-or-test-placeholder"
            assert "sk-or-test-placeholder" not in repr(start.call_args.args)
        proxy.terminate.assert_called_once()
    assert dict(os.environ) == original_env
    proxy = Mock()
    proxy.stdout.readline.return_value = '{"port": 12345}\n'
    with patch.dict(os.environ, {"LOCAL_LLM_URL": "http://127.0.0.1:11434/v1/messages", "LOCAL_LLM_MODELS_URL": "http://127.0.0.1:11434/v1/models", "LOCAL_LLM_MODEL": "fixture-local", "LOCAL_LLM_API_KEY": "test-local-key"}), patch(__name__ + ".run_owned", return_value=auth), patch("shutil.which", return_value="executable"), patch(__name__ + ".read_key", side_effect=AssertionError("Local must not read OpenRouter key")), patch("subprocess.Popen", return_value=proxy) as start:
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
    print("PASS: MCP discovery, key parser, isolated consultation, app-managed credits, Plan auth gate, native feature environment preservation, local/OpenRouter credential separation and launcher lifecycle")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--serve"]:
            serve()
        elif sys.argv[1:] == ["--self-test"]:
            self_test()
        else:
            parser = argparse.ArgumentParser(description="Sonnet replacement with Plan fallback")
            parser.add_argument("--provider", choices=("openrouter", "local"), default="openrouter")
            parser.add_argument("--check-local", action="store_true", help="Check local model metadata without inference or Plan login")
            parser.add_argument("--diagnose-local", action="store_true", help="One synthetic Local inference without CLI, native features or Plan fallback")
            parser.add_argument("--local-mode", action="store_true", help="Interactive Local-only CLI with native integrations disabled and no Plan fallback")
            options = parser.parse_args()
            if options.local_mode and (options.provider != "local" or options.check_local or options.diagnose_local):
                parser.error("--local-mode requires --provider local and cannot be combined with diagnostic checks")
            if options.diagnose_local and (options.provider != "local" or options.check_local):
                parser.error("--diagnose-local requires --provider local and cannot be combined with --check-local")
            if options.diagnose_local:
                sys.exit(diagnose_local())
            if options.check_local:
                result = check_local()
                print(json.dumps(result))
                sys.exit(0 if result["local_check"] else 1)
            sys.exit(launch(options.provider, local_only=options.local_mode))
    except (OSError, RuntimeError, EOFError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"event": "openrouter_start_failed", "kind": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
