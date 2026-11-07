# Claude-Cheat · Claude Code Sonnet replacement with native Plan exceptions

> **Compatibility status:** retaining the Claude Code frontend is not proof of full feature parity. Agent Teams, parallel subagents, Local admission/fallback, reasoning and streaming still need end-to-end validation. See the [DeepSeek Harness / OpenCode source review (Korean)](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md) for evidence and proposed work; those proposals are not runtime changes. For the Claude Code version baseline and release checklist, see the [compatibility baseline (Korean)](docs/CLAUDE-CODE-COMPATIBILITY.ko.md).

[한국어](README.md) | **English**

[MIT License](LICENSE) · [License selection review](LICENSE-REVIEW.md)

**Current version: 1.0.5** — the dashboard auto-opens on its token-backed `#token=…` write_url, model lists load with a key alone, plus low-latency TCP/HTTP/2/streaming options. See the [release history (Korean)](docs/HISTORY.ko.md).

A local router that preserves the existing Claude Code Plan login and settings while sending compatible Sonnet requests to the configured OpenRouter model, local LLM, or verified CLI model backend. The repository includes the launcher, proxy, and synthetic regression tests. It does not contain API keys, OAuth credentials, personal settings, or private diagnostic scripts.

**Status: provider selection, retries, native Plan exceptions, and recovery are implemented. Provider failures do not fall back to Plan. Preserving every native feature while actually replacing the main Sonnet model is not fully achieved.**

Check the installed Claude Code, Node.js, and Python versions for compatibility. No personal development-machine configuration or minimum-version matrix is published.
- See the [public history guide](docs/HISTORY.ko.md); private environment records are excluded.

## Setup and execution

Install Node.js, Python, and Claude Code separately, and sign in to Claude Code with the existing Plan subscription. The current authentication gate specifically checks `subscriptionType="pro"`; Max and other plans are not guaranteed to pass. No npm or pip packages are required by the checked-in source.

All settings live in one file, `openclaude.config.yaml`, in this checkout's root. Saving from the settings page creates it; to start by hand, copy the example (an existing file is never overwritten):

```powershell
if (-not (Test-Path -LiteralPath 'openclaude.config.yaml')) {
    Copy-Item -LiteralPath 'openclaude.config.example.yaml' -Destination 'openclaude.config.yaml'
}
notepad openclaude.config.yaml
```

For OpenRouter, fill in this section:

```yaml
openrouter:
  model: "REPLACE_WITH_OPENROUTER_MODEL_ID"
  api_key: "YOUR_OPENROUTER_API_KEY"
```

Replace the placeholder with a real OpenRouter key. The file is read next to `openrouterAdvisor.py`, not relative to the current working directory. The format is a **strict YAML subset**: `section:` lines with two-space `key: "value"` lines and `#` comments only; quote your values. Tabs, anchors (`&` `*`), lists, `|` blocks and multi-line values are rejected with a line number, and an error never contains a value. A problem in one service's section does not block the other, and unknown top-level sections are preserved. No YAML library is needed. The key is read only by the router. It does not replace the CLI's subscription login. `openclaude.config.yaml` (and its `.bak`) is ignored by Git; do not publish credentials.

The old `.env.openrouter` / `.env.local-llm` files are **deprecated**. They are read, read-only, only for a service that has no section in the new file; the first save from the settings page moves both services' values into the new file and leaves the old files untouched. When the new file has a section for a service, only the new file is used for it.

**Saving from the dashboard:** the settings page saves only when it is opened through the token-backed `#token=…` write_url that the dashboard prints and auto-opens (`OpenClaude` and the launcher both open it now). Visiting `http://127.0.0.1:8787/` directly is read-only and rejects saves with `token_required`. Model lists load with only the API key entered; until a model is selected the check reports `model_not_selected`, and a blocked network shows `connection_failed`.

Set `model` to a concrete ID from the [OpenRouter model list](https://openrouter.ai/models). Existing key-only files must add it. Missing, empty, malformed or placeholder models fail startup without default-model selection or Plan fallback. Optionally set `provider` to an upstream slug such as `open-inference/fp4`; blank means Auto. A specified slug uses `only` and disables fallback to other OpenRouter providers. An unavailable or unsupported provider may fail; provider failures do not trigger Claude Plan fallback. Changes apply to new sessions or takeover. Model-list checks do not verify provider availability. File settings are proxy-only; inherited OpenRouter environment values do not override this file.

The optional `reasoning` key (added 2026-11-07) asks OpenRouter for reasoning. Blank or `off` sends no `reasoning` key (previous behaviour). A positive integer such as `8192` is a reasoning token budget (`reasoning.max_tokens`); `low|medium|high|xhigh|max|minimal` request a named effort (`reasoning.effort`). It applies only to ordinary-session external OpenRouter requests — never to Plan, Local, CLI paths or compatibility modes. `reasoning_details` in the response are passed through to Claude Code unmodified and are displayed only if the CLI understands them. A provider that does not support reasoning for the model may ignore it or return an error; clear the key in that case. Dual-provider sessions apply the same setting to their OpenRouter side.

The selected model must support the [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true) and the tools you need. Model selection does not guarantee every model or native feature is compatible. Response model identity remains strict; dynamic aliases or presets returning another ID can fail and are unverified. Endpoint and retry policy remain fixed as documented in the example.

> **Requirement ledger**: behavior you explicitly requested is recorded in [`docs/USER-REQUIREMENTS.ko.md`](docs/USER-REQUIREMENTS.ko.md). If code, tests, or docs contradict it, the change is reverted. R1 — routing to OpenRouter without an extra option — is pinned by regression tests.

From the checkout root:

```powershell
# Use the default service saved in the dashboard
.\OpenClaude.ps1
# Local Messages API, after configuring a real local service
.\OpenClaude.ps1 -Provider Local
```

`-Provider` defaults to **Auto**: it first uses `routing.default_provider` from `openclaude.config.yaml`. If the preference is absent or `auto`, exactly one configured service is selected automatically. With multiple services configured and no explicit preference, it exits with code 2 and actionable guidance:

```text
Both OpenRouter and Local settings are configured. Choose one explicitly:
  OpenClaude.ps1 -Provider OpenRouter
  OpenClaude.ps1 -Provider Local
```

The settings editor selector marks whether each service is configured. The default launch section displays the saved preference and the service a live session is using; runtime state is also exposed as `providers` and `active_session` in `/api/status` and `/api/snapshot`. `-Takeover` without `-Provider` follows the provider recorded for the session it takes over.

### Save a default service and launch without flags

1. Run `.\OpenClaude.ps1 -Setup`.
2. Under **설정할 서비스** (service to edit), choose OpenRouter, Local LLM or CLI, enter its settings and save them.
3. Under **기본 실행 서비스** (default launch service), choose the service and press **기본값 저장** (save default).
4. Run `.\OpenClaude.ps1` to start a new session. Repeating `-Provider` is no longer necessary.

The settings editor selection is remembered in the browser; the launch default is saved in the shared settings file, so other browsers and terminals using this checkout read the same default. Editing another service does not change the default. Neither save action starts a session or changes a running one.

```yaml
routing:
  default_provider: "local"  # auto | openrouter | local | cli
```

An explicit `-Provider OpenRouter`, `-Provider Local`, or `-Provider CLI` overrides the saved choice for that invocation without changing the file. `-Takeover` keeps the live session's service. An invalid preference or a broken selected service fails explicitly instead of silently selecting another service. The dashboard can reset an invalid preference to `auto` without requiring a configured service. The isolated modes (`-LocalMode`, `-LocalCompat`, `-OpenRouterCompat`) still require an explicit `-Provider`.

CLI is also a saved default service. Claude Code stays the front end; the selected CLI serves only as the model backend. See the backend section below for verified versions and restrictions.

Run `.\OpenClaude.ps1 -Help` or `python openrouterAdvisor.py --help` for options, runnable examples and exit codes without starting a session. The old `startOpenClaude.ps1` command remains a forwarding shim. A provider-choice usage error retains exit code 2 and no longer produces a redundant PowerShell error stack.

The authenticated `POST /api/default-provider` endpoint accepts `{"provider":"local"}` (also `auto`, `openrouter` and `cli`). `GET /api/config` reports `default_provider` and `default_provider_error`. Existing Host, token, Origin, request-size and rate checks protect writes. Provider settings and the default have separate save actions; a failed default save leaves the previous preference intact.

This workflow follows the relevant [CLI design guidance](https://github.com/cursor/plugins/blob/main/cli-for-agent/skills/cli-for-agents/SKILL.md) and [documentation reliability guidance](https://github.com/cursor/plugins/blob/main/agent-compatibility/agents/docs-reliability-review.md): explicit flags, practical examples, actionable errors and docs matching actual behavior. Claude-Cheat is not a Cursor plugin or a port of the entire repository, and this work does not implement CLI-backed request routing.

### Using both services in one session (opt-in)

With `routing: dual_provider: true` in `openclaude.config.yaml` and **both services configured**, an ordinary session lists the other service as one extra entry in Claude Code's `/model` picker (model id `claude-route-local` or `claude-route-openrouter`, for example `Local (qwen3-coder:latest)`).

- The session's own provider (the one `-Provider` chose) stays the default. **Only requests** sent while the extra entry is selected go to the other provider.
- Native features can still use Plan. Provider failures return an error without Plan fallback. A failure opens **that provider's own circuit breaker**, independently of the other service.
- The other provider's credential reaches **only the proxy process**, never the CLI environment. The routing log's `route_selected`/`circuit_open` events carry a `provider` field saying where a request went.
- Alternate Local requests use the same MCP and inline-tool compatibility conversion as primary Local requests. OpenRouter converts only validated inline-tool declarations; native requests that cannot be converted can still use Plan.
- Special modes (`-LocalMode`, `-LocalCompat`, `-OpenRouterCompat`) rewrite requests and disable Plan fallback, so they ignore this feature entirely (no extra entry is added).
- The `/model` entry is injected at session start through `ANTHROPIC_CUSTOM_MODEL_OPTION*`, so changing the option requires restarting the session. Whether the picker shows the entry depends on the CLI version (the hook was confirmed in 2.1.310); the proxy's model-id-to-provider dispatch is covered by unit tests.
- `-Takeover` stores this topology (provider, alias, model id — no credentials) in the handoff record and refuses to drain when the current settings could no longer serve the same alias (feature disabled, alternate changed, or the provider switched), because a picker entry that suddenly starts returning 403 mid-session is worse than an explicit restart. The alternate credential is always re-read from the current settings.

If you install a global launcher separately, configuration is read beside its target script. Cloning this repository neither installs a global command nor changes an existing launcher target. Run the script in the intended checkout explicitly.

> Scripts use the **product-name form**: `OpenClaude.ps1` (routing session) and `OpenClaudeDashboard.ps1` (dashboard). The previous names `startClaudeOpenRouter.ps1`, `startOpenClaude.ps1`, `startClaudeDashboard.ps1` and `startDashboard.ps1` remain as **compatibility shims**, so existing shortcuts and aliases keep working (they forward every argument and print a one-line notice). In a terminal whose policy requires signatures, use the equivalent `OpenClaude.cmd` / `OpenClaudeDashboard.cmd` entry points.
>
> To run them as bare commands from any folder, install the forwarders once **from the checkout folder**:
>
> ```powershell
> cd <checkout>
> .\installLauncher.cmd          # writes the .cmd forwarders and adds the folder to the user PATH
> .\installLauncher.cmd -Remove  # removes every installed forwarder
> .\installLauncher.cmd -NoPath  # installs the forwarders without touching PATH
> ```
>
> `.\installLauncher.ps1` behaves the same where the execution policy allows `.ps1`. After that first install, these work from any folder:
>
> ```powershell
> OpenClaude                 # routing session
> OpenClaudeDashboard        # dashboard
> installLauncher            # re-run the installation refresh from anywhere
> OpenClaude -Help           # options without starting a session
> ```
>
> From another folder, type **`OpenClaude` or `OpenClaudeDashboard`** (no extension). The `.ps1` scripts exist only in the checkout, so `openClaude.ps1` or `installLauncher.ps1` (or a bare filename without `.\`) cannot be found on PATH. The forwarders are `.cmd` files, so the PowerShell execution policy never applies to them; each one re-launches the real `.ps1` with `-ExecutionPolicy Bypass`. Same-named `.ps1` forwarders written by earlier versions are removed on install, because PowerShell would otherwise resolve those first. They embed this checkout's path, so re-run the installer if the repository moves. If the default folder is missing from the user PATH, the installer now **adds it automatically**; terminals that were already open need to be reopened to see it.
>
> Verify with `Get-Command OpenClaude, OpenClaudeDashboard, installLauncher | Select-Object Name, Source` (they should resolve to `%USERPROFILE%\.local\bin\*.cmd`). If it does not resolve, open a new terminal first (PATH changes apply to new processes), then re-run the installer from the checkout, or use `.\installLauncher.cmd` when the policy blocks `.ps1`. `-Remove` deletes the forwarders but leaves the PATH entry, so remove that entry in Windows Environment Variables if you want it gone too.

>
> **If the execution policy blocks `.ps1` files**: the scripts are unsigned, so `AllSigned` (or a managed policy that requires signatures) fails with "the file is not digitally signed". Use the `.cmd` entry points instead — `.\installLauncher.cmd` to install, `.\OpenClaude.cmd` / `.\OpenClaudeDashboard.cmd` in the checkout, and the installed `OpenClaude`/`OpenClaudeDashboard` commands all work regardless of policy. If something still blocks, run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installLauncher.ps1`, or set the current-user policy to `RemoteSigned` (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, no admin required). A GPO that enforces `AllSigned` overrides the bypass; contact your administrator in that case.

Configure OpenRouter once in the web console — it runs without a routing session and shows setup guidance while the settings are empty:

```powershell
.\OpenClaudeDashboard.ps1        # http://127.0.0.1:8787/ (use the printed write_url for writes)
.\OpenClaude.ps1 -Setup           # starts the same dashboard and opens its settings page in the browser
```

`-Setup` never creates or edits `openclaude.config.yaml` or any legacy `.env.*` file and does not start a session; you save the values on the web page yourself. The `[setup required]` hint printed by `OpenClaude.ps1` when settings are missing also mentions this command.

Pick OpenRouter, Local LLM or CLI in the **service** select box at the top; only that service's fields are shown (each option is tagged `configured` / `setup needed`, and the last choice is remembered in the browser). For OpenRouter, fill in a **model ID** and **API key**, press **Model check**, then **Save**, and start the session. The check also runs on unsaved values, and saving without an existing `openclaude.config.yaml` seeds the file from `openclaude.config.example.yaml` so its comments stay. API keys and server addresses are shown only in part and the stored original is never sent back to the browser, so type the full value only when changing it (empty keeps it; the optional HTTP-exception and model-list addresses have a **Clear** button). Starting the launcher with missing or invalid settings exits with the console address instead of opening a session. Editing the files by hand still works exactly as before.

Ordinary OpenRouter, Local and CLI launches preserve native Claude Code beta and advisor settings. Unsupported requests retain the original Plan route. Explicit compatibility and restricted profiles have separate policies.

Ordinary Local-only-provider sessions do not set these switches and retain SSH/MCP, tool search, plugins, hooks, and user/project permission settings. Known deferred MCP tool and CLI control fields use the existing compatibility validator and buffered JSON transport, converted to CLI SSE when needed. Native `tool_addition` declarations move into the Local tool list while conversation content and tool results stay intact. Conflicting definitions and unknown shapes retain the original conversation and tool definitions on Plan. Only a null `tool_use.provider_specific_fields` value added to CLI history is normalized away on Local and Plan; objects and signatures are not removed by this rule. Cache boundaries can change with this translation. Unsupported native features and Opus/Haiku/Fable requests retain the existing Plan route; Local failures return an error without Plan fallback. Ordinary external thinking remains disabled so Local signatures cannot contaminate Plan history.

Ordinary Local launches default to an 8,192-token output budget to reduce context overflows caused by native Sonnet output limits. An explicit positive whole-number `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is preserved. Only this setting is passed per launch; user/project settings files are unchanged. Large inputs or explicit output limits can still exceed the model context. Use an ordinary launch to retain native client integrations; `-LocalMode` is a separate restricted mode that disables MCP.

## Reference guides

Detailed routing, circuit, dashboard, troubleshooting and validation content now lives beside this README and is linked here to keep this file short:

- [Proxy reference — routing, retries, fallback, dashboard, usage, first-token options](docs/PROXY-REFERENCE.md)
- [Troubleshooting — connection checks and returning to stock Claude](docs/TROUBLESHOOTING.md)
- [Validation and limitations — bench, simulation, reference numbers](docs/VALIDATION.md)

## Local compatibility mode

```powershell
.\OpenClaude.ps1 -Provider Local -LocalCompat
```

This separate opt-in mode restores normal plugin, skill, MCP, hook, subagent, project-instruction and thinking/effort settings on Claude Code 2.1.304 or later. Default permissions and deny/ask rules remain active. The API-key helper stays disabled; per-launch settings pin the loopback route and temporary authentication above saved environment settings. Existing commands and `-LocalMode` retain their behavior. Do not combine `-LocalCompat` with `-LocalMode` or diagnostics.

All Claude main/fast/subagent model roles use the same configured Local model. No Plan login or Plan fallback is used. Effort and thinking parameters are preserved, without claiming Sonnet-equivalent reasoning effects. Unknown betas, unsupported fields and token counting fail explicitly.

The current server returns an SSE model identifier that does not match the configured model. Compatibility mode therefore validates complete JSON replies and converts them into CLI SSE frames, preserving signatures and provider metadata without inventing them. Tokens are buffered; the existing 120-second whole-request deadline, one upstream attempt and 30-second cooldown remain. Arbitrary plugins and every native server feature are not guaranteed. See the [live validation scope](docs/LIVE-VALIDATION.ko.md#local-호환-모드-검증-2026-10-24).

## Local-only Claude CLI mode

```powershell
.\OpenClaude.ps1 -Provider Local -LocalMode
```

This opt-in mode launches the real Claude CLI with `--safe-mode`, Sonnet as the routing alias, and Bash/Read/Edit/Write/Glob/Grep. Claude Code **2.1.304 or later** is required; verification uses 2.1.304. Older versions are rejected before the proxy or session starts. Standard permission prompts (`default`) and existing user/project deny/ask rules stay enabled; this mode does not enable bypassPermissions or auto mode. Existing launch commands without `-LocalMode` are unchanged.

This resolves the earlier three-tool bare-mode limitation. Real CLI/Local-server verification on 2026-10-23 covered Write/Glob/Grep with ASCII, spaces, Korean, and combined paths. See the [compatibility results](docs/LIVE-VALIDATION.ko.md#local-모드-파일-도구-호환성-개선-2026-10-23).

Fresh Local sessions append their startup working directory to the default system instructions so relative paths keep an explicit default base after compaction. Transcript, configuration, and log paths do not change that base; a different base explicitly selected by the user applies to that request. Start a new Local session with the same command to apply this change. See the [relative-path validation and scope](docs/LIVE-VALIDATION.ko.md#local-모드-자동-압축-후-상대-경로-기준-개선-2026-10-23).

The session disables native beta integrations, advisor, and thinking. Safe mode skips user/project hooks, plugins, skills, subagents, and CLAUDE.md discovery. Empty strict MCP configuration, MCP tool denial, and hook/memory/IDE/Chrome/background-task restrictions remain explicit; API-key helpers are disabled for this invocation. Include necessary project instructions explicitly. Managed-policy hook/command exceptions follow CLI policy and were not tested here. The `interleaved-thinking-2025-05-14` and `effort-2025-11-24` compatibility headers are accepted in every routing mode. Unknown native features remain blocked. See the official [CLI reference](https://code.claude.com/docs/en/cli-reference).

The CLI gets a temporary loopback-proxy Bearer token, never the Local server key or Plan OAuth. Plan login is not queried. All proxy Plan paths are blocked, including native-feature bypass and failure fallback. A failed Local request stops after one upstream attempt. The CLI may still have its own retries. `system`-role context messages (nonblank strings or text blocks) keep their role and text in every mode; system turns carrying tool or thinking blocks and extra message fields remain blocked. Local-only requests allow 120 seconds for long-context compaction; ordinary mode retains its 30-second limit. Unsupported native server-tool history and token-count requests fail explicitly. Validate long-session compaction against your server and CLI version using the [compaction procedure](docs/LIVE-VALIDATION.ko.md#local-모드의-긴-대화압축-점검).

Local-only error messages distinguish these cases. Failures retain the default 30-second cooldown and show remaining seconds rounded up. Send a new request after the wait to check recovery; the failed task does not complete automatically.

| Code | Meaning and action |
|---|---|
| `local_timeout` | Request timed out; wait before a new request |
| `local_authentication_failed` | Server returned 401/403; check the Local key/access permissions and restart the session after credential changes |
| `local_empty_response` | Empty or whitespace-only reply; check server response behavior |
| `local_upstream_error` | Other connection, status, or response-validation failure; check fixed categories in the routing log |
| `local_cooldown` | Retry wait is active; includes the previous cause and remaining seconds |
| `local_recovery_in_progress` | Another request is checking recovery; wait for it to finish |

CLI follow-up requests may make the cooldown message the final visible error, so it preserves the previous failure cause. The `local_mode_rejected` log also includes `code`, applicable `retry_after_seconds`, and `previous_error_code`. An active recovery check has no invented countdown. HTTP 400, the `api_error` envelope, and existing CLI retry behavior remain unchanged. Messages never include raw server responses, addresses, or keys.

Before inference starts, Local-only failures add a fixed `code` and action message to `openrouter_start_failed`, with exit code 1 and no raw exception, address, or key. Ordinary launches and diagnostics retain their behavior.

| Startup code | Check |
|---|---|
| `local_cli_not_found` / `local_node_not_found` | Claude Code/Node.js installation and PATH |
| `local_cli_unsupported` / `local_cli_version_check_failed` | `claude --version`, minimum 2.1.304, executable permissions |
| `local_configuration_invalid` | Local file/environment URL, HTTP approval, model, key format, and model-list origin |
| `local_output_budget_invalid` | Positive integer `CLAUDE_CODE_MAX_OUTPUT_TOKENS` |
| `local_proxy_start_failed` | Node.js, proxy files, and loopback permissions; the existing 15-second readiness deadline remains |
| `local_cli_start_failed` | Claude Code installation and executable permissions |
| `local_process_cleanup_failed` | Python, Windows Job permissions, and guardian file; no CLI starts if cleanup preparation fails |

On Windows, the existing `-LocalMode` CLI entry point puts its launcher in a [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) before creating the CLI or proxy. One hidden guardian waits for the launcher to exit, then closes the Job to clean the CLI, proxy, Bash, and descendants. It does not search process names or kill global processes. Failure to prepare ownership blocks CLI startup.

The existing proxy parent-pipe guard remains and now also covers ordinary launches (the proxy exits when the launcher's pipe closes). Diagnostics, imported library calls, and non-Windows shutdown behavior are unchanged; the Windows job-object tree guard still applies to Local and OpenRouter compatibility modes only. Start a new Local session to acquire this protection; existing sessions are not retroactively enrolled. Prefer normal exit or Ctrl+C. See the [process-tree verification scope](docs/LIVE-VALIDATION.ko.md#windows-local-프로세스-트리-정리).

To prevent low output budgets from truncating compaction, this mode supplies `CLAUDE_CODE_MAX_OUTPUT_TOKENS` through its per-launch settings, defaulting to 8192. A larger positive whole-number value from the parent environment is preserved; a smaller value is raised to 8192. These per-launch settings take precedence over budget values in user/project files; use the parent environment for a larger budget. Neither the files nor the parent environment is changed. The budget applies to ordinary replies in this Local session as well as compaction. The CLI still applies model caps and remaining-context adjustments; the proxy does not raise request `max_tokens`. Your server must support the output budget. Launches without `-LocalMode` and diagnostic modes are unchanged.

For this mode only, the upstream request uses non-streaming JSON with thinking disabled. The proxy strictly validates the configured response model and converts validated text/tool responses to CLI SSE when needed; IDs, ordered tool inputs, stop reasons, and token usage are preserved. `tool_use.provider_specific_fields` objects/null are preserved as opaque data when present. The CLI may omit them from later history; compatibility with servers that require this state is not established. Unknown response fields and thinking blocks are rejected rather than dropped. The default routing path is unchanged. CLI model/cost displays remain aliases and estimates; verify `route_selected` with `provider: local`. A blocked feature logs `local_mode_rejected`, never a Plan success.

Use a new Local session rather than resuming a native Plan conversation. This mode cannot be combined with `-CheckLocal` or `-DiagnoseLocal`. Exit and relaunch without `-LocalMode` to restore the original integrations.

## Local inference diagnostic mode

```powershell
.\OpenClaude.ps1 -Provider Local -DiagnoseLocal
```

This separate mode sends one fixed synthetic Messages request directly to the configured Local server. It does not launch Claude Code, load CLI hooks/MCP/history, send beta headers or tools, retry, or call Plan. The request has a 512-token output limit and a 60-second deadline; redirects remain blocked. The existing endpoint restrictions and explicit HTTP pin still apply. An actual inference may consume server resources.

Success requires `local_diagnostic`, `inference_checked`, `model_confirmed` and `reply_confirmed` to be `true`: the response must identify the exact configured model, contain `LOCAL_DIAGNOSTIC_OK`, and finish with `end_turn`. Failure exits with code 1 and prints only fixed categories. This verifies minimal Local Messages inference, not normal Claude Code feature compatibility. `-CheckLocal` remains metadata-only; these two diagnostic switches cannot be combined. Normal launch is unchanged.

## Local LLM configuration

Real Local inference requires a separately configured service and remains a separate validation step.

Configure the `local:` section of `openclaude.config.yaml` **next to the launcher**, or set the corresponding process environment variables. The configuration directory is not necessarily the working directory. Matching process environment variables override file values. Fill in the real server URL and model ID later. Only Anthropic-compatible `/v1/messages` is supported; no OpenAI-only `/v1/chat/completions` adapter is implemented.

```powershell
# Run in the checkout root; preserve an existing configuration.
if (-not (Test-Path -LiteralPath 'openclaude.config.yaml')) {
    Copy-Item -LiteralPath 'openclaude.config.example.yaml' -Destination 'openclaude.config.yaml'
}
notepad openclaude.config.yaml
# Model metadata only; no inference or Plan login is used.
.\OpenClaude.ps1 -CheckLocal
# Normal launch still checks the saved Plan authentication.
.\OpenClaude.ps1 -Provider Local
```

```yaml
local:
  url: "http://127.0.0.1:11434/v1/messages"
  model: "YOUR_INSTALLED_MODEL_ID"
  allow_remote: false
  # Set api_key separately only if the local server requires authentication.
  # Optional same-origin model-list URL; default: /v1/models.
  # models_url: "http://127.0.0.1:11434/api/tags"
```

The setting names below are the environment-variable names; in the file they are `url`, `model`, `api_key`, `allow_remote`, `http_url` and `models_url` under `local:`. An empty value (`""`) means "not set" and never hides a default.

| Setting | Default / meaning |
| --- | --- |
| `LOCAL_LLM_URL` | `http://127.0.0.1:11434/v1/messages`; exact Messages path required |
| `LOCAL_LLM_MODEL` | Required at normal launch; exact server model ID |
| `LOCAL_LLM_API_KEY` | Optional server-specific Bearer credential |
| `LOCAL_LLM_ALLOW_REMOTE` | `false`; remote access needs explicit `true` and HTTPS |
| `LOCAL_LLM_HTTP_URL` | Optional exact HTTP Messages URL pin; empty by default. Plaintext HTTP is allowed only when it exactly matches `LOCAL_LLM_URL`. Keep real values in the ignored `openclaude.config.yaml`; prefer HTTPS. |
| `LOCAL_LLM_MODELS_URL` | Same-origin `/v1/models`; metadata only |

For an existing remote service, for example:

```yaml
local:
  url: "https://llm.example.com/v1/messages"
  model: "YOUR_SERVER_MODEL_ID"
  allow_remote: true
  # api_key: "YOUR_SERVER_KEY"
```

Replace the example host and model before use. Use `127.0.0.1` rather than `localhost`; the latter is not a permitted loopback literal.

By default, only literal loopback (`127.0.0.1` or `::1`) HTTP/HTTPS endpoints are allowed. The unnormalized path must be exactly `/v1/messages`. To use an existing remote server, explicitly set `LOCAL_LLM_ALLOW_REMOTE` (`allow_remote: true`) and an `https://server/v1/messages` URL. Remote HTTP without an explicit matching `LOCAL_LLM_HTTP_URL`, URL credentials/query/fragment, redirects, and invalid credential headers are rejected. Certificate verification is not disabled. Prefer HTTPS or a separately prepared loopback tunnel; an exact HTTP pin transmits prompts and server credentials without encryption and does not verify that the destination is private. Opting in trusts the configured DNS name and its resolution.

`-CheckLocal` sends only a GET to the same-origin model-list endpoint. It disables environment proxies and redirects, uses only the Local key in a fresh `Authorization: Bearer` header, and limits responses to 1 MiB with a five-second socket timeout. Output contains only model counts, configured-model presence, and fixed error categories. Model metadata success does not establish Messages inference or tool compatibility. If the server has no model-list API, skip that optional check and validate inference separately. A failed check never invokes Plan fallback.

Local mode does not read the OpenRouter key. Real `openclaude.config.yaml` / `.env*` settings are ignored by Git; only the credential-free `openclaude.config.example.yaml` template is distributed (the old `.env.*.example` files are deprecated). The server and model still need real validation later.

Run real loopback HTTP integration tests with `node --test tests/*.test.mjs` and configuration/metadata tests with `python -m unittest discover -s tests -p 'test_*.py' -v`. Their servers and Plan responses are synthetic; they do not contact real LLM providers or accounts.

Independent validation after these Local additions passed Node tests, Python tests (counts change per release; run the commands below), Python self-tests, Node/Python syntax checks, and PowerShell parsing. Real server/model/authentication/TLS deployment checks remain deferred.

## OpenRouter compatibility mode and startup checks

```powershell
.\OpenClaude.ps1 -Provider OpenRouter -OpenRouterCompat
```

Requires Claude Code 2.1.304 or newer and the same two `openclaude.config.yaml` OpenRouter settings. All Claude model roles use the selected `OPENROUTER_MODEL`, without Plan login or fallback. The LocalCompat profile retains client plugins, skills, MCP, hooks, subagents, thinking and effort settings, ordinary configuration sources and default permissions. API-key helpers are disabled; the proxy route and temporary authentication are pinned in launch settings. Provider credentials stay in the proxy. Unknown native fields/betas and token counting are rejected. Ordinary routing and both Local modes remain available.

**Ordinary OpenRouter sessions also work without an extra option.** A session started with `OpenClaude` (or `-Provider OpenRouter`) routes supported requests to the configured OpenRouter model. `-OpenRouterCompat` also blocks native Plan exceptions.

> A regression in `e075542` (CLI backend integration) narrowed that stable-protocol setup to compat mode only, so ordinary OpenRouter sessions diverted every request to Plan. It is restored here, with tests pinned against a repeat.

Responses are fully validated as JSON before conversion to client SSE. This mode uses one 120-second upstream attempt and a 30-second failure cooldown, with OpenRouter provider fallback disabled. Model identity, Messages/tool support and reasoning behavior still depend on the selected model; forwarding effort does not establish Sonnet-equivalent reasoning.

Every interactive launch now makes one minimal provider-only inference check before opening the CLI: a 15-second deadline and a 512-token budget for HTTP providers; CLI backends use their configured deadline and native output constraints. OpenRouter charges may apply. Logs contain `connection_check`, provider, inference/model/reply booleans, a fixed reason and optional HTTP status, never endpoints, keys or response bodies. Check failure logs `provider_connection_unavailable` with `action: continue` and still opens the CLI. Ordinary sessions retain native Plan routing but do not fall back on provider failure; explicit Local-only and compatibility modes block Plan. Configuration errors and missing executables remain startup errors. Standalone `-CheckLocal` and `-DiagnoseLocal` still exit after diagnosis.

`ready` confirms the configured model, exact diagnostic reply and normal completion. `timeout`, `authentication_failed`, `http_error` and `connection_failed` identify connection failures. `reply_mismatch`, `incomplete_response` and `invalid_response` identify response-validation failures: a reasoning model may exhaust this small budget or return extra text despite a working connection. The check does not prove complete CLI compatibility.

## Restarting the proxy without losing the session

```powershell
OpenClaude -Takeover        # run in a second terminal
```

The new launcher reads the live port and token from `.session-handoff.json`, asks the running proxy to drain (`POST /api/handover`, session token required), answers new requests with 503 + `Retry-After: 1`, and waits for in-flight turns (default cap **120 s**, `ROUTE_HANDOVER_TIMEOUT_MS`) before rebinding the same port with the same token. If the cap expires while a turn is still active, the proxy does **not** exit: it reverts to serving and replies HTTP **409** with `{"drained": false, "reverted": true, …}`; the launcher then aborts the replacement and reports a retryable error instead of truncating the live response. Concurrent handover requests share one drain run and the exit hook fires once. The CLI keeps its base URL and simply reconnects; new requests during the drain get 503.

## Source map

| File | Purpose |
| --- | --- |
| `OpenClaude.ps1` | PowerShell provider selector and launcher |
| `openrouterAdvisor.py` | Plan authentication checks, environment isolation, proxy lifecycle, self-tests |
| `localProcessTree.py` | Windows Local CLI Job ownership and process-tree guardian |
| `claudeRouteProxy.mjs` | Routing, response validation, retries, fallback, recovery |
| `tests/claudeRouteProxy.test.mjs` | Synthetic proxy regression tests |
| `tests/localLlmIntegration.test.mjs` | Real loopback HTTP tests with synthetic Messages servers and Plan responses |
| `tests/test_local_config.py` | Configuration policy and model-metadata checks using synthetic HTTP servers |
| `tests/test_local_lifecycle.py` | Windows synthetic CLI/launcher termination, proxy and connection cleanup |
| `openclaude.config.example.yaml` | Credential-free unified configuration template (OpenRouter and Local) |
| `.env.local-llm.example` | Credential-free Local template (deprecated; replaced by the file above) |
| `docs/HISTORY.ko.md` | Historical investigation notes, separate from current policy |

The private `tests/probeClaudeRoute.py` used during development is not distributed here.

## License

The code and documentation in this repository are available under the [MIT License](LICENSE). Commercial use, modification, and redistribution are permitted subject to retaining the copyright and permission notice. See the [official MIT text](https://opensource.org/license/mit).

The reviewed source uses Node.js and Python standard libraries and invokes a separately installed Claude CLI. No bundled third-party packages or separate source-license notices were found in the review. MIT was selected as a simple permissive license for reusing this small standalone router. See the [scope, rationale, and Apache-2.0 comparison](LICENSE-REVIEW.md).

Claude Code, Node.js, Python, hosted services, and models are not distributed by this repository. Their own licenses and service terms continue to apply; this project's license does not grant service access or change usage limits and billing.


### Feature-preservation fixes (2026-10-29)

Compatibility launches retain explicitly set frontend agent/team/task/background/IDE/plugin/skill preferences, without enabling unset flags or inheriting provider credentials. Restricted LocalMode remains restricted; account synchronization still needs native authentication. Native management commands (`attach`, `logs`, `respawn`, `stop`, `kill`, `rm`, `purge`, `gateway`, `setup-token`, `upgrade`) pass directly to Claude without provider setup checks.

Plan HTTP-date retry hints are converted to seconds. Capability-rejection errors and retry headers have regression coverage; credential redaction and the error-size cap remain. Empty-response logs add fixed `validation_detail` categories without recording response content.

Remote Control is disabled by Claude Code for custom API base URLs. Detached `--bg` sessions still require a persistent proxy lifecycle design: management-command passthrough alone does not solve that. Full live Agent Teams/MCP/plugin parity is not established. See the [source review and verification status](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md).

The final section of the [routing compatibility review](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md) studies performance and feature preservation using observed routing data. Retry-budget, end-to-end deadline and streaming changes are proposals, not deployed behavior.


### Routing bottleneck diagnostics (P0)

New proxies add `attemptCounts` (actual external/Plan calls) and `routingTimingMs` (Local admission wait, external fetch/read/validation, retry wait, Plan call, total). The runtime diagnostics panel and AI `/api/diagnostics` expose these observations. Missing fields from older proxies remain unavailable, not zero.

Timing summaries retain at most 512 samples per stage, including failures/cancellation. Total starts after request body reading/validation; external timing is not pure inference time. Stage percentiles cannot be added. Existing `externalAttempts` remains the count of initial external request entries. Empty-response diagnostics allow only bounded shape metadata, approved stop reasons and safe usage numbers, never prompts, response text or tool arguments. No extra inference/polling or routing-policy changes are introduced. Restart the launcher to load new proxy instrumentation.