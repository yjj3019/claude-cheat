# Claude Code · Sonnet replacement with Plan fallback

[한국어](README.md) | **English**

[MIT License](LICENSE) · [License selection review](LICENSE-REVIEW.md)

A local router that preserves the existing Claude Code Plan login and settings while sending compatible Sonnet requests to the configured OpenRouter model or a local LLM. The repository includes the launcher, proxy, and synthetic regression tests. It does not contain API keys, OAuth credentials, personal settings, or private diagnostic scripts.

**Status: provider selection, retries, Plan fallback, and recovery are implemented. Preserving every native feature while actually replacing the main Sonnet model is not fully achieved.**

Check the installed Claude Code, Node.js, and Python versions for compatibility. No personal development-machine configuration or minimum-version matrix is published.
- See the [public history guide](docs/HISTORY.ko.md); private environment records are excluded.

## Setup and execution

Install Node.js, Python, and Claude Code separately, and sign in to Claude Code with the existing Plan subscription. The current authentication gate specifically checks `subscriptionType="pro"`; Max and other plans are not guaranteed to pass. No npm or pip packages are required by the checked-in source.

For OpenRouter, create `.env.openrouter` in this checkout's root:

```dotenv
ANTHROPIC_AUTH_TOKEN=YOUR_OPENROUTER_API_KEY
OPENROUTER_MODEL=REPLACE_WITH_OPENROUTER_MODEL_ID
```

Replace the placeholder with a real OpenRouter key. This file is read next to `openrouterAdvisor.py`, not relative to the current working directory. Use one KEY=VALUE per line; blank lines, full-line comments and matching quotes are supported, but multiline values and shell expansion are not. Do not add inline comments after a key. This value is read only by the router. It does not replace the CLI's subscription login. `.env` and `.env.*` are ignored by Git; do not publish credentials.

Set `OPENROUTER_MODEL` to a concrete ID from the [OpenRouter model list](https://openrouter.ai/models). Existing key-only files must add it. Missing, empty, malformed or placeholder models fail startup without default-model selection or Plan fallback. Both file settings are proxy-only; inherited OpenRouter environment values do not override this file.

The selected model must support the [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true) and the tools you need. Model selection does not guarantee every model or native feature is compatible. Response model identity remains strict; dynamic aliases or presets returning another ID can fail and are unverified. Endpoint and retry policy remain fixed as documented in the example.

From the checkout root:

```powershell
# Default provider: OpenRouter; model comes from .env.openrouter
.\OpenClaude.ps1
# Local Messages API, after configuring a real local service
.\OpenClaude.ps1 -Provider Local
```

If you install a global launcher separately, configuration is read beside its target script. Cloning this repository neither installs a global command nor changes an existing launcher target. Run the script in the intended checkout explicitly.

> Scripts use the **product-name form**: `OpenClaude.ps1` (routing session) and `OpenClaudeDashboard.ps1` (dashboard). The previous names `startClaudeOpenRouter.ps1`, `startOpenClaude.ps1`, `startClaudeDashboard.ps1` and `startDashboard.ps1` remain as **compatibility shims**, so existing shortcuts and aliases keep working (they forward every argument and print a one-line notice).
>
> To run them as bare commands from any folder, install the forwarders once:
>
> ```powershell
> .\installLauncher.ps1          # writes OpenClaude, OpenClaudeDashboard (and the old names) into %USERPROFILE%\.local\bin
> .\installLauncher.ps1 -Remove  # removes every installed forwarder
> ```
>
> They embed this checkout's path, so re-run the installer if the repository moves.

Configure OpenRouter once in the web console — it runs without a routing session and shows setup guidance while the settings are empty:

```powershell
.\OpenClaudeDashboard.ps1        # http://127.0.0.1:8787/ (fixed port)
```

Fill the OpenRouter card with a **model ID** and **API key**, press **Model check**, then **Save**, and start the session. The check also runs on unsaved values, and saving without an existing `.env.openrouter` seeds the file from the shipped example so its comments stay. Starting the launcher with missing or invalid settings exits with the console address instead of opening a session. Editing the files by hand still works exactly as before.

A routed session starts Claude Code with experimental beta features disabled (`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL`). The inline `tool_addition`, `context_management`, and advisor tool shape that Claude Code 2.1.310 sends by default is rejected by external Messages API providers with HTTP 400, so Plan requests in the same session also use the stable protocol. Advisor, inline tools, and server-side context editing are therefore unavailable in that session. Thinking and effort settings still travel per request, and ordinary mode still never asks the external provider for thinking.

## Routing, retries, and recovery

| Request | Route |
| --- | --- |
| Compatible Sonnet request | Configured OpenRouter model or local model |
| Unsupported native Sonnet feature or conversation history | Original Plan Sonnet request, unchanged |
| Opus, Haiku, Fable | Existing Plan OAuth path |
| External provider fails all four attempts | One request to the original Plan Sonnet model |

- OpenRouter uses the required `OPENROUTER_MODEL` setting, with no default model.
- OpenRouter and Local are selected at launch; the router does not switch between them automatically. Select Sonnet in `/model` for eligible replacement requests.
- The external provider receives the initial attempt plus three retries. Each attempt has a 30-second limit, with a one-second delay between attempts.
- Four full timeouts plus retry delays can take approximately 123 seconds before the Plan request begins. Plan response time and the CLI's own retries may add further time. Cancelling stops router retries and fallback.
- After all four fail, the router forwards the original request to Plan Sonnet once. A Plan failure does not loop back to another provider or switch to an Anthropic API key.
- During a 30-second cooldown, requests use Plan. Afterward, one compatible real request checks recovery; concurrent requests use Plan. A validated external response restores the external route. There are no separate paid background probes.
- External responses are buffered, up to 32 MiB, until model identity, message completion, block indices, and tool-input JSON are validated. Failed attempts do not expose partial text or tool calls to the CLI. This delays visible responses, and retries may incur duplicate inference costs.
- Plan requests, responses, streams, and errors pass through unchanged. A routed session disables Claude Code's experimental betas, so the CLI does not build advisor or inline tool requests. `system`-role context turns, including text blocks, and the `interleaved-thinking-2025-05-14`, `effort-2025-11-24` and `context-1m-2025-08-07` betas the CLI always sends are accepted in every mode. Provider `citations` are returned in responses and echoed back in later history, so they are accepted on input too. Signed reasoning blocks are dropped from the external copy only, so a conversation returns to the replacement provider on its next request instead of staying on Plan. Unsupported server tools still bypass. Such a bypass is not counted as an external recovery check.
- The router does not block account-level usage credits, so Plan routing is not a guarantee of zero additional account charges.
- Model access and usage credits remain subject to the Claude app and account plan. There is no separate Fable ban or credits-OFF requirement. External providers do not receive the Plan OAuth credential.

Routing metadata uses `route_selected` with provider `openrouter`, `local` or `plan`. Reason `replacement` identifies a validated external response; `native_feature` identifies a native-feature bypass; `fallback` follows failed external attempts; `circuit_open` uses Plan during cooldown or another recovery probe. `external_attempt_failed` records a failed attempt, and `circuit_open` / `circuit_recovered` report circuit transitions. These logs omit keys and request bodies; requests sent to external services may still contain conversation, code, tool definitions and results.

For `native_feature`, `detail` reports the first exclusion: `token_count`, `plan_model`, `unsupported_beta`, `unsupported_request_field`, `unsupported_message_shape`, `unsupported_thinking_config`, `unsupported_output_config`, `unsupported_tool_choice`, `unsupported_content`, `unsupported_content_block`, `unsupported_content_field`, `unsupported_tool_caller`, or `unsupported_tool_definition`. Exclusion labels are fixed; user text, tool names, signatures and credentials are never logged. For `unsupported_beta`, `beta_features` lists up to 16 date-suffixed feature identifiers and `unsupported_beta_count` gives the total. Nonstandard or credential-like entries are redacted; raw header strings are not logged. Restart the launcher to enable new diagnostics; old logs cannot reveal these details.

Router logs (`route_selected` and the other events) are hidden by default and printed to stderr only with `-Debug` (or `python openrouterAdvisor.py --debug`). Startup errors and `provider_connection_unavailable` still print without it. Use `-Debug` to verify routing.

The CLI or HUD can still display Sonnet/Plan for a replacement response. That label and its cost estimate do not prove the inference provider or actual OpenRouter billing. Check the safe `route_selected` metadata: `replacement`, `native_feature`, `fallback`, or `circuit_open`.

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

Create `.env.local-llm` **next to the launcher**, or set the corresponding process environment variables. The configuration directory is not necessarily the working directory. Matching process environment variables override file values. Fill in the real server URL and model ID later. Only Anthropic-compatible `/v1/messages` is supported; no OpenAI-only `/v1/chat/completions` adapter is implemented.

```powershell
# Run in the checkout root; preserve an existing configuration.
if (-not (Test-Path -LiteralPath '.env.local-llm')) {
    Copy-Item -LiteralPath '.env.local-llm.example' -Destination '.env.local-llm'
}
notepad .env.local-llm
# Model metadata only; no inference or Plan login is used.
.\OpenClaude.ps1 -CheckLocal
# Normal launch still checks the saved Plan authentication.
.\OpenClaude.ps1 -Provider Local
```

```dotenv
LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages
LOCAL_LLM_MODEL=YOUR_INSTALLED_MODEL_ID
LOCAL_LLM_ALLOW_REMOTE=false
# Set LOCAL_LLM_API_KEY separately only if the local server requires authentication.
# Optional same-origin model-list URL; default: /v1/models.
# LOCAL_LLM_MODELS_URL=http://127.0.0.1:11434/api/tags
```

| Setting | Default / meaning |
| --- | --- |
| `LOCAL_LLM_URL` | `http://127.0.0.1:11434/v1/messages`; exact Messages path required |
| `LOCAL_LLM_MODEL` | Required at normal launch; exact server model ID |
| `LOCAL_LLM_API_KEY` | Optional server-specific Bearer credential |
| `LOCAL_LLM_ALLOW_REMOTE` | `false`; remote access needs explicit `true` and HTTPS |
| `LOCAL_LLM_HTTP_URL` | Optional exact HTTP Messages URL pin; empty by default. Plaintext HTTP is allowed only when it exactly matches `LOCAL_LLM_URL`. Keep real values in ignored `.env.local-llm`; prefer HTTPS. |
| `LOCAL_LLM_MODELS_URL` | Same-origin `/v1/models`; metadata only |

For an existing remote service, for example:

```dotenv
LOCAL_LLM_URL=https://llm.example.com/v1/messages
LOCAL_LLM_MODEL=YOUR_SERVER_MODEL_ID
LOCAL_LLM_ALLOW_REMOTE=true
# LOCAL_LLM_API_KEY=YOUR_SERVER_KEY
```

Replace the example host and model before use. Use `127.0.0.1` rather than `localhost`; the latter is not a permitted loopback literal.

By default, only literal loopback (`127.0.0.1` or `::1`) HTTP/HTTPS endpoints are allowed. To use an existing remote server, explicitly set `LOCAL_LLM_ALLOW_REMOTE=true` and an `https://server/v1/messages` URL. Remote HTTP without an explicit matching `LOCAL_LLM_HTTP_URL`, URL credentials/query/fragment, redirects, and invalid credential headers are rejected. Certificate verification is not disabled. Prefer HTTPS or a separately prepared loopback tunnel; an exact HTTP pin transmits prompts and server credentials without encryption. Opting in trusts the configured DNS name and its resolution.

`-CheckLocal` sends only a GET to the same-origin model-list endpoint. It disables environment proxies and redirects, uses only the Local key in a fresh `Authorization: Bearer` header, and limits responses to 1 MiB with a five-second socket timeout. Output contains only model counts, configured-model presence, and fixed error categories. Model metadata success does not establish Messages inference or tool compatibility. If the server has no model-list API, skip that optional check and validate inference separately. A failed check never invokes Plan fallback.

Local mode does not read the OpenRouter key. Real `.env*` settings are ignored by Git; only the credential-free `.env.local-llm.example` template is distributed. The server and model still need real validation later.

Run real loopback HTTP integration tests with `node --test tests/*.test.mjs` and configuration/metadata tests with `python -m unittest discover -s tests -p 'test_*.py' -v`. Their servers and Plan responses are synthetic; they do not contact real LLM providers or accounts.

Independent validation after these Local additions passed Node tests, Python tests (counts change per release; run the commands below), Python self-tests, Node/Python syntax checks, and PowerShell parsing. Real server/model/authentication/TLS deployment checks remain deferred.

## OpenRouter compatibility mode and startup checks

```powershell
.\OpenClaude.ps1 -Provider OpenRouter -OpenRouterCompat
```

Requires Claude Code 2.1.304 or newer and the same two `.env.openrouter` settings. All Claude model roles use the selected `OPENROUTER_MODEL`, without Plan login or fallback. The LocalCompat profile retains client plugins, skills, MCP, hooks, subagents, thinking and effort settings, ordinary configuration sources and default permissions. API-key helpers are disabled; the proxy route and temporary authentication are pinned in launch settings. Provider credentials stay in the proxy. Unknown native fields/betas and token counting are rejected. Ordinary routing and both Local modes remain available.

Responses are fully validated as JSON before conversion to client SSE. This mode uses one 120-second upstream attempt and a 30-second failure cooldown, with OpenRouter provider fallback disabled. Model identity, Messages/tool support and reasoning behavior still depend on the selected model; forwarding effort does not establish Sonnet-equivalent reasoning.

Every interactive launch now makes one minimal provider-only inference check before opening the CLI: a 15-second deadline and a 512-token budget. OpenRouter charges may apply. Logs contain `connection_check`, provider, inference/model/reply booleans, a fixed reason and optional HTTP status, never endpoints, keys or response bodies. Check failure logs `provider_connection_unavailable` with `action: continue` and still opens the CLI. Ordinary mode retains Plan routing/fallback; explicit Local-only and compatibility modes retain their Plan block. Configuration errors and missing executables remain startup errors. Standalone `-CheckLocal` and `-DiagnoseLocal` still exit after diagnosis.

`ready` confirms the configured model, exact diagnostic reply and normal completion. `timeout`, `authentication_failed`, `http_error` and `connection_failed` identify connection failures. `reply_mismatch`, `incomplete_response` and `invalid_response` identify response-validation failures: a reasoning model may exhaust this small budget or return extra text despite a working connection. The check does not prove complete CLI compatibility.

## Live dashboard

```powershell
.\OpenClaudeDashboard.ps1
```

A **read-only** page bound to `127.0.0.1`. The port is **fixed at 8787**; an older dashboard instance is stopped automatically so the same port is reused. If another program holds that port the server fails with a message instead of silently moving; pass `--allow-port-fallback` to pick a free port instead.

The refresh interval follows the situation: two seconds while routing is active, ten seconds while idle, fifteen when the tab is hidden. The header shows a one-line summary (`오늘 43.8M토큰 · $1.42 · 남은 한도 $48.25 · 우선 확인: …`).

The **insights** card ranks *what changed, why, and what to do next* instead of listing numbers, with the measurements it used as chips. A banner that shares an insight's cause is not repeated.

| Insight | Trigger | Suggested action |
| --- | --- | --- |
| Launcher code is older than the source | proxy start stamp < source mtime | `.\OpenClaude.ps1` (copy button) |
| External circuit open | a circuit opened inside the window | check network, key and model id |
| Plan surge / Plan share rising | Plan ≥50% / ≥20% of the last 10 minutes | explain the top reason code and its fix |
| External request rejected | repeated 400/413/422 | update the launcher |
| Limit exhaustion forecast | today's burn rate exhausts the limit within 7 days | adjust model price or volume |
| Burn rate | days of headroom estimate | reference |
| Cache reuse low | ≥200k prompt tokens with <15% reuse | keep the prompt prefix stable |
| Latency | session p95 ≥20s | splits **first-byte time from generation time** so a proxy problem is not confused with model speed |
| Session idle | no decision for 30 minutes | reference |

| Panel | Content |
| --- | --- |
| Now | **OpenRouter serving / Plan bypass / idle**, the **model that actually answered**, age, reason |
| Alerts | Shown when Plan bypass reaches 50% within ten minutes, a circuit opens, a 400/413/422 rejection repeats, or the balance drops below 10% |
| Last 60 minutes | Replacement versus Plan counts, circuit opens |
| Bypass reasons | Counts per `native_feature` `detail` with **meaning and action**, external failure statuses and validation classes |
| Credits | Key limit, spend this period, remaining, reset date (OpenRouter `/api/v1/key`) |
| Today | Replacement and Plan tokens, input/output, cache, **estimated cost** |
| Hourly tokens | The last 24 hours as replacement versus Plan bars |
| Route transitions | When the route changed, its reason, model, count and duration (last 12) |
| Per session | Turns, replacement/Plan turns, replacement tokens, **estimated cost**, model |
| Help | CLI commands (`--status` and options), HTTP API, reason code table (meaning/action), state badges, exit codes, data sources |

Estimated cost uses the model rates from OpenRouter `/api/v1/models`; confirm actual charges against the provider's own records. The **model that actually answered** is recorded by the proxy just before a response is delivered; the name the CLI shows is the request alias (`claude-sonnet-5-5` and so on), so this value is the real provider model.

The page shows no key, prompt or request body. It writes only `.env.openrouter` and `.env.local-llm` beside the launcher, and only when a save is requested. It is a separate process, so it can run beside the routing session.

### Editing settings and testing connections

Settings live on a **separate page** (`http://127.0.0.1:8787/settings`) reached from the top navigation; both pages share one server and session token.

| Behaviour | Detail |
| --- | --- |
| Save | Only the managed keys change; comments and unrelated entries stay, and the previous file is kept as `.env.*.bak` |
| Secrets | Never sent back to the browser (`sk-or-…cd0` style). Leaving the field empty keeps the stored value; only a new value replaces it |
| Validation | Model ID shape, key shape and Local URL rules (loopback, HTTPS, same origin) are checked before writing; a failure leaves the file untouched |
| Protection | POST requires the session token embedded in the page and a same-origin request, so other sites cannot call it |
| Tests | **Model check** reads the model list only; **inference test** sends one 16-token request (may cost a small amount) |
| Model picker | **목록** opens a searchable list with context length and price and fills the field. `GET /api/models?provider=openrouter\|local` (session token required, 10 minute cache) |
| When it applies | Saved settings take effect in the **next** session; restart a running one |

### Status API for scripts and agents

Check the current state without opening the page:

```powershell
python routeDashboard.py --status           # one JSON line plus an exit code
python routeDashboard.py --status --pretty  # indented
python routeDashboard.py --status --usage    # add token and cost totals (slower)
python routeDashboard.py --status --offline  # skip OpenRouter calls
```

| Exit code | Meaning |
| --- | --- |
| 0 | Healthy (replacement route serving, or idle) |
| 2 | Currently on the Plan route |
| 3 | Critical alert (circuit open, low balance, ...) |
| 4 | No routing record yet (no launcher session) |

The report includes `state`, `provider`, `reason`, `detail`, **`model`**, `last_event_age_seconds`, `window_counts`, `plan_reasons`, `failures`, `alerts` and `account`. A running dashboard serves the same document at `GET /api/status`. Neither path includes the key, prompts or request bodies.

A running proxy exposes its own counters at `GET /api/metrics` (same session token required, loopback only): request count, external/Plan split, p50/p95 latency, retries, streams, circuit opens and bytes in/out. The same snapshot is written to `.proxy-metrics.json` beside the launcher once a second and shown as the dashboard's **proxy throughput** card.

## Lowering the first-token delay (optional)

By default the proxy validates the **whole** reply before sending it, so a streamed answer appears only when generation finishes. That keeps the retry + Plan fallback guarantee but delays the first token.

```powershell
.\OpenClaude.ps1 -Stream        # forward frames as they validate (lower TTFB)
```

| Mode | First token (TTFB) | Behaviour on failure |
| --- | --- | --- |
| Default (buffered) | when the reply completes (measured 782ms / 782ms) | four attempts, then Plan fallback |
| `-Stream` (opt-in) | first valid content (measured **339ms** / 772ms total) | before the first token: same fallback; **after** it: the stream ends with an error so output is never duplicated |

Forwarding starts only after the first valid content, so empty replies, thinking-only replies and broken tool JSON still fall back. Environment form: `$env:ROUTE_STREAM_PASSTHROUGH = '1'; OpenClaude.ps1`.

## Understanding connection checks and troubleshooting

`-CheckLocal` accepts model lists shaped as `data[].id` or `models[].name` / `models[].model`. It prints safe metadata, not model names or response bodies. Example output, not a real-server result:

```json
{"local_check":true,"inference_checked":false,"model_count":2,"configured_model_found":true,"reason":"ready_metadata_only"}
```

| Reason | Meaning |
| --- | --- |
| `ready_metadata_only` | Model found; inference is still unverified |
| `model_not_found` / `model_not_configured` | Incorrect / missing configured ID |
| `connection_failed` | Check server, port, connection and TLS |
| `http_error` / `redirect_rejected` | Check reported status, auth and final URL |
| `invalid_model_list` / `response_too_large` | Unsupported response shape / size |
| `invalid_configuration_or_response` | Invalid configuration or parsing failure |

A failed check exits with code 1 and never invokes Plan fallback. A successful model list does not establish Messages support: even an OpenAI-only service may expose `/v1/models`. A service without a model-list endpoint may still support inference; configure an appropriate same-origin list endpoint or defer this optional check and test inference separately.

| Symptom | What to check |
| --- | --- |
| Missing `.env.openrouter` | Actual launcher directory, not current working directory |
| OpenRouter 401 / missing authentication header | Router key file and key validity; successful HUD installation does not prove inference auth |
| Old Local address remains active | Process environment overrides, global shim target, then restart |
| Sonnet continues using Plan | Check `route_selected` with `-Debug`; `native_feature` can be legitimate (Plan-only model, token counting, native history). Repeated `unsupported_beta` means the CLI added a beta the router does not know yet, so update the launcher |
| Dashboard shows `실행기 재시작 필요` | The running proxy started before the current source. Use the alert's **명령 복사** button to copy `.\OpenClaude.ps1`, restart the session, and compare **프록시 코드** with **소스 수정** in the status card |
| Session drops abruptly | Check `.route.log` for `parent_disconnected` (launcher exited). Make sure no other script kills `python` processes wholesale; stop the dashboard with `python routeDashboard.py --stop` |
| Repeated historical `503 ... restart required` | Stale session/proxy and first upstream error; restart with current launcher |
| HUD or MCP failure | Compare ordinary Claude; loading settings does not prove server connectivity |

A routed session also appends its routing events to `.route.log` beside the launcher: a `proxy start` line per session, then timestamped `route_selected`, `external_attempt_failed` and `circuit_open` entries. This explains past sessions that ran without `-Debug`, and the file is excluded from Git by the `*.log` rule.

Exit and relaunch to change providers. `/model` selects Claude models, not OpenRouter versus Local. To return to ordinary Plan use `claude` in a clean terminal. This launcher does not modify the parent terminal, but old user/system `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` or `CLAUDE_CONFIG_DIR` variables may still be inherited. Do not delete saved login or MCP settings as a blanket fix.

## Validation and limitations

Personal machine configurations, account usage, billing records, and live session logs are not stored in this repository. Run synthetic tests without external model inference:

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
node tests/simulate.mjs --soak 3000        # synthetic traffic, long session, memory
python tests/live_smoke.py --dry-run       # report which settings exist, no billing
```

- The Windows process-tree tests (`test_local_lifecycle`) and the concurrency stress (`test_dashboard_concurrency`) are **skipped by default**: they spawn real process trees or 12 threads, so they are slow and load sensitive. Enable them with `$env:CLAUDE_CHEAT_SLOW = '1'`.
- The process-tree suite needs **Git Bash**. `C:\Windows\System32\bash.exe` is WSL and cannot run the Windows python path the harness passes, so it skips itself.
- The dashboard enforces a **Host allowlist** (127.0.0.1, localhost, ::1) against DNS rebinding and sends `nosniff`, `no-referrer` and `X-Frame-Options: DENY`. Write endpoints require the session token and a same-origin request.
- Real provider calls stay out of the suite. After a configuration change, confirm once with `python tests/live_smoke.py` (16 tokens, well under a cent per run).

The tests cover payload preservation, authentication isolation, tool-result round trips, the initial request plus three retries, Plan fallback, recovery, cancellation, timeouts, and empty responses. Synthetic success does not establish live-provider or full native-feature parity.

### Simulation and performance

A mock upstream generates traffic so throughput, latency, memory and routing decisions can be measured without calling a provider.

```powershell
node tests/simulate.mjs                                   # mixed load, long session, circuit breaker
node tests/simulate.mjs --requests 4000 --concurrency 48 --json
node tests/bench_proxy.mjs                                # proxy overhead by body size
node --test tests/simulation.test.mjs                     # simulator smoke test
```

Scenarios cover a plain reply, SSE streaming, tool calls, a 429 retry, a 1MB body, an unsupported beta bypass, and the circuit breaker with cooldown; each response carries a marker so the simulator can assert which path served it. Any mismatch exits 1.

Reference numbers (this development machine, Node 24, client and proxy in one process):

| Metric | Value |
| --- | --- |
| Mixed throughput | ~700–980 req/s at 1,200 requests / concurrency 24, p50 16–25ms, p95 55–82ms |
| Long session | 120 turns, 247KB history: 3.1ms (3.9ms before the fix) |
| Body size | 1MB in 7.7–8.9ms (~120MB/s) |
| Retry cost | 1MB + one retry 10.7ms vs 12.8ms before, four failures 9.8ms vs 12.1ms (~17–19% better) |
| Circuit | 4 failures → Plan fallback, Plan during cooldown, external again after recovery |
| Long soak | **150,000 requests · 266MB peak · 4.3MB drift** (no leak) |

Long runs use `--soak`, which also samples resident memory and window throughput.

```powershell
node tests/simulate.mjs --soak 3000 --concurrency 16
```

Measured: 3,000 requests with a **245MB peak RSS and 5.6MB drift** (no leak), with the latency ring pinned at 512 samples. Resident-memory drift is the gate; per-window median latency is printed for reference, since the client and proxy share one process and GC noise shows up there.

Dashboard refresh cost is measured the same way (a snapshot is rebuilt every two seconds):

```powershell
python tests/bench_dashboard.py            # 10MB log, 2,000 transcript files
```

| Metric | Value |
| --- | --- |
| `.route.log` parsing | 38–278ms for a full 10MB read → **1–3ms incremental tail** (1ms with 200 new lines) |
| Transcript scan | 5s for the first 2,000-file scan (background), 40ms → **23ms** incremental (`os.scandir` metadata) |
| Snapshot refresh | 2,000 files and a 10MB log: **8ms with unchanged inputs**, 43ms for the first refresh after new data |

What the measurements changed: the request body was serialized **four times inside the retry loop** and is now serialized once, a history without signed reasoning no longer rebuilds its message array on every turn (~19% better on long sessions), the routing log is **read incrementally from a parked offset** (rotation and truncation are detected from a leading-byte fingerprint), the transcript walk is **skipped entirely when no new routing decision exists** (signature cache with a 60 second forced rescan), a single-chunk body is **forwarded without a copy**, and a window is capped at the newest 2,000 events so a pathological log cannot slow a refresh. The same commands re-measure it at any time.

- A real Local service still requires separate authentication, inference, and tool-call validation.
- Unsupported native features or signed history stay on the Plan route. External usage can be zero, and native history may prevent a safe return to the external provider in the same conversation.
- Running only a native advisor on the subscription path during external inference is not implemented. Full native-feature equivalence, including remote managed settings and organization policy with a custom `ANTHROPIC_BASE_URL`, is not guaranteed.
- Request bodies above 32 MiB receive HTTP 413 before fallback. Only versioned `claude-sonnet-*`, `claude-opus-*`, `claude-haiku-*`, `claude-fable-*` names whose version starts with a digit, the configured OpenRouter model ID, and the historical client alias are accepted. Unknown models receive HTTP 403.
- Allowed paths are POST `/v1/messages` and `/v1/messages/count_tokens` (optionally `?beta=true`), plus local readiness HEAD `/api/hello`. Other paths are rejected, not forwarded to the subscription route.
- A custom `CLAUDE_CONFIG_DIR` is preserved unless absent or set to the historical `~/.claude-openrouter`, in which case the child uses `~/.claude`. Old MCP/plugin registrations are not automatically migrated; directories are not copied or deleted.
- The per-session random token (nonce) of Local/compat isolated sessions appears in the Claude Code process's `--settings` argument and environment. Any same-user process (including EDR or audit tools that log command lines) can read it, and a leaked token allows consuming OpenRouter/Local usage through the local proxy for that session. It is regenerated per session and bound to `127.0.0.1` only; it grants no Plan access.
- If `claude` resolves to an npm `.cmd`/`.bat` shim, isolated modes (Local, OpenRouter compatibility) print a warning. cmd.exe may mis-parse quotes, `&` or `%` in arguments passed through a shim; this was not reproduced. Prefer the native `claude.exe` install.
- Diagnose HTTP 429 using the affected account's Usage screen and response. Account-specific limits and reset schedules are not published here.

See the [connection and recovery checklist](docs/LIVE-VALIDATION.ko.md) for a general procedure.

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
| `.env.local-llm.example` | Credential-free configuration template for an existing server |
| `docs/HISTORY.ko.md` | Historical investigation notes, separate from current policy |

The private `tests/probeClaudeRoute.py` used during development is not distributed here.

## License

The code and documentation in this repository are available under the [MIT License](LICENSE). Commercial use, modification, and redistribution are permitted subject to retaining the copyright and permission notice. See the [official MIT text](https://opensource.org/license/mit).

The reviewed source uses Node.js and Python standard libraries and invokes a separately installed Claude CLI. No bundled third-party packages or separate source-license notices were found in the review. MIT was selected as a simple permissive license for reusing this small standalone router. See the [scope, rationale, and Apache-2.0 comparison](LICENSE-REVIEW.md).

Claude Code, Node.js, Python, hosted services, and models are not distributed by this repository. Their own licenses and service terms continue to apply; this project's license does not grant service access or change usage limits and billing.
