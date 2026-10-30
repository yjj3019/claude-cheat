# Claude-Cheat · Claude Code Sonnet replacement with Plan fallback

> **Compatibility status:** retaining the Claude Code frontend is not proof of full feature parity. Agent Teams, parallel subagents, Local admission/fallback, reasoning and streaming still need end-to-end validation. See the [DeepSeek Harness / OpenCode source review (Korean)](docs/HARNESS-COMPATIBILITY-REVIEW.ko.md) for evidence and proposed work; those proposals are not runtime changes.

[한국어](README.md) | **English**

[MIT License](LICENSE) · [License selection review](LICENSE-REVIEW.md)

A local router that preserves the existing Claude Code Plan login and settings while sending compatible Sonnet requests to the configured OpenRouter model, local LLM, or verified CLI model backend. The repository includes the launcher, proxy, and synthetic regression tests. It does not contain API keys, OAuth credentials, personal settings, or private diagnostic scripts.

**Status: provider selection, retries, Plan fallback, and recovery are implemented. Preserving every native feature while actually replacing the main Sonnet model is not fully achieved.**

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

Set `model` to a concrete ID from the [OpenRouter model list](https://openrouter.ai/models). Existing key-only files must add it. Missing, empty, malformed or placeholder models fail startup without default-model selection or Plan fallback. Both file settings are proxy-only; inherited OpenRouter environment values do not override this file.

The selected model must support the [Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true) and the tools you need. Model selection does not guarantee every model or native feature is compatible. Response model identity remains strict; dynamic aliases or presets returning another ID can fail and are unverified. Endpoint and retry policy remain fixed as documented in the example.

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
- Native features and Plan fallback still go to Plan (the subscription) as before. A failure opens **that provider's own circuit breaker**; one service going down does not affect requests to the other.
- The other provider's credential reaches **only the proxy process**, never the CLI environment. The routing log's `route_selected`/`circuit_open` events carry a `provider` field saying where a request went.
- Special modes (`-LocalMode`, `-LocalCompat`, `-OpenRouterCompat`) rewrite requests and disable Plan fallback, so they ignore this feature entirely (no extra entry is added).
- The `/model` entry is injected at session start through `ANTHROPIC_CUSTOM_MODEL_OPTION*`, so changing the option requires restarting the session. Whether the picker shows the entry depends on the CLI version (the hook was confirmed in 2.1.310); the proxy's model-id-to-provider dispatch is covered by unit tests.
- `-Takeover` stores this topology (provider, alias, model id — no credentials) in the handoff record and refuses to drain when the current settings could no longer serve the same alias (feature disabled, alternate changed, or the provider switched), because a picker entry that suddenly starts returning 403 mid-session is worse than an explicit restart. The alternate credential is always re-read from the current settings.

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
.\OpenClaude.ps1 -Setup           # starts the same dashboard and opens its settings page in the browser
```

`-Setup` never creates or edits `openclaude.config.yaml` or any legacy `.env.*` file and does not start a session; you save the values on the web page yourself. The `[setup required]` hint printed by `OpenClaude.ps1` when settings are missing also mentions this command.

Pick OpenRouter, Local LLM or CLI in the **service** select box at the top; only that service's fields are shown (each option is tagged `configured` / `setup needed`, and the last choice is remembered in the browser). For OpenRouter, fill in a **model ID** and **API key**, press **Model check**, then **Save**, and start the session. The check also runs on unsaved values, and saving without an existing `openclaude.config.yaml` seeds the file from `openclaude.config.example.yaml` so its comments stay. API keys and server addresses are shown only in part and the stored original is never sent back to the browser, so type the full value only when changing it (empty keeps it; the optional HTTP-exception and model-list addresses have a **Clear** button). Starting the launcher with missing or invalid settings exits with the console address instead of opening a session. Editing the files by hand still works exactly as before.

Ordinary OpenRouter, Local and CLI launches preserve native Claude Code beta and advisor settings. Unsupported requests retain the original Plan route. Explicit compatibility and restricted profiles have separate policies.

Ordinary Local-only-provider sessions do not set these switches and retain SSH/MCP, tool search, plugins, hooks, and user/project permission settings. Known deferred MCP tool and CLI control fields use the existing compatibility validator and buffered JSON transport, converted to CLI SSE when needed. Native `tool_addition` declarations move into the Local tool list while conversation content and tool results stay intact. Conflicting definitions and unknown shapes retain the original conversation and tool definitions on Plan. Only a null `tool_use.provider_specific_fields` value added to CLI history is normalized away on Local and Plan; objects and signatures are not removed by this rule. Cache boundaries can change with this translation. Unsupported native features, Opus/Haiku/Fable selections, and Local failures retain the existing Plan route. Ordinary external thinking remains disabled so Local signatures cannot contaminate Plan history.

Ordinary Local launches default to an 8,192-token output budget to reduce context overflows caused by native Sonnet output limits. An explicit positive whole-number `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is preserved. Only this setting is passed per launch; user/project settings files are unchanged. Large inputs or explicit output limits can still exceed the model context. Use an ordinary launch to retain native client integrations; `-LocalMode` is a separate restricted mode that disables MCP.

## Routing, retries, and recovery

| Request | Route |
| --- | --- |
| Compatible Sonnet request | Configured OpenRouter model or local model |
| Unsupported native Sonnet feature or conversation history | Original Plan Sonnet request, unchanged |
| Opus, Haiku, Fable | Existing Plan OAuth path |
| HTTP provider fails all four attempts, or CLI backend fails its single attempt | One request to the original Plan Sonnet model |

- OpenRouter uses the required `OPENROUTER_MODEL` setting, with no default model.
- OpenRouter and Local are selected at launch; the router does not switch between them automatically. Select Sonnet in `/model` for eligible replacement requests.
- HTTP providers receive the initial attempt plus three retries. CLI backends receive one attempt with their configured deadline. Ordinary HTTP attempts have a 30-second header/idle timeout and a 300-second total ceiling while data keeps arriving. Local-only and compatibility modes retain their existing fixed deadlines.
- OpenRouter keeps its one-second default retry delay. Local congestion responses (429/503/529) and timeouts use exponential backoff based on 1/2/4 seconds plus small positive jitter, bounded by `ROUTE_RETRY_MAX_WAIT_MS` (10 seconds by default). Empty replies are not assumed to mean congestion. Plan response time and the CLI's own retries add to the wait; cancellation stops retries and fallback.
- `Retry-After` accepts seconds or HTTP-date, bounded at ten minutes. A Local hint longer than the internal wait cap ends retries rather than retrying early, and also extends the circuit recovery wait. Ordinary mode falls back to Plan; modes without Plan return an error.
- The ordinary Local fast path keeps the original conversation. A compatibility copy is built only for potentially convertible native-feature exclusions, avoiding discarded history/tool copies. This reduces proxy work without changing model selection, token budgets or Plan eligibility; it does not repair empty server responses or speed up inference.
- **Shared Local admission:** each proxy allows two active Local turns, eight queued turns, and at most 60 seconds in the queue. An active turn retains its slot across retries. Cancellation removes a queued turn. Queue overflow or timeout uses `local_busy`: ordinary mode routes to Plan, while modes without Plan return 503. Admission rejection does not count as an external attempt or open the circuit.
- The dashboard shows Local active/queued turns and cumulative admission rejections. The existing authenticated `/api/metrics` exposes `localAdmission`: `active`, `queued`, `rejected`, `concurrency`, `queueLimit`, `queueWaitMs`. Rejections accumulate only during the current proxy process.
- This limits only traffic from this proxy, not other users or server-wide GPU load. Long parallel tasks can exceed the queue wait and use Plan. Conversation, tools and output token budgets are not silently shortened, and no inference probes or server polling are added. Restart the proxy to load the changed source.
- The circuit breaker distinguishes causes: transport failures, timeouts, 5xx, 408 and unusable 2xx answers open it as provider faults; 401/402/403 keep their provider-wide cooldown; a 429 uses the parsed `Retry-After` (bounded at ten minutes) as its cooldown, and a request-level 4xx never opens it. Exactly one half-open probe is admitted, and late completions from older generations cannot overwrite the newer circuit state.
- A client that stalls mid-body is dropped after an idle timeout (30 seconds by default, `ROUTE_REQUEST_IDLE_TIMEOUT_MS`, `0` disables it), releasing its turn; slow but live uploads are not cut. A disconnected client's upstream read, timers and retry are released instead of retried.
- After all four fail, the router forwards the original request to Plan Sonnet once. A Plan failure does not loop back to another provider or switch to an Anthropic API key.
- **Tool call ids are brought to the Anthropic contract on the Plan route.** An external provider may mint ids such as `Bash:0#abc` that the native route rejects. Just before a Plan request is forwarded, only those ids are remapped per request while `tool_use`/`tool_result` pairing is preserved; ids that already conform stay byte-identical so the prompt cache is unaffected. The external route is never rewritten, and an empty id is rejected locally with a 400 before any upstream call.
- During a 30-second cooldown, requests use Plan. Afterward, one compatible real request checks recovery; concurrent requests use Plan. A validated external response restores the external route. There are no separate paid background probes.
- External responses are buffered, up to 32 MiB, until model identity, message completion, block indices, and tool-input JSON are validated. Failed attempts do not expose partial text or tool calls to the CLI. This delays visible responses, and retries may incur duplicate inference costs.
- Plan requests, responses, streams, and errors pass through unchanged. Ordinary sessions preserve native feature settings; unsupported requests use Plan. `system`-role context turns, including text blocks, and the `interleaved-thinking-2025-05-14`, `effort-2025-11-24` and `context-1m-2025-08-07` betas the CLI always sends are accepted in every mode. Provider `citations` are returned in responses and echoed back in later history, so they are accepted on input too. Signed reasoning blocks are dropped from the external copy only, so a conversation returns to the replacement provider on its next request instead of staying on Plan. Unsupported server tools still bypass. Such a bypass is not counted as an external recovery check.
- The router does not block account-level usage credits, so Plan routing is not a guarantee of zero additional account charges.
- Model access and usage credits remain subject to the Claude app and account plan. There is no separate Fable ban or credits-OFF requirement. External providers do not receive the Plan OAuth credential.

Proxy behavior can be tuned with these environment variables (the defaults are enough; set them before starting a session).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ROUTE_REQUEST_IDLE_TIMEOUT_MS` | `30000` | Idle timeout for the request body and headers. `0` disables it. Slow but live uploads are unaffected |
| `ROUTE_LOCAL_QUEUE_WAIT_MS` | `60000` | Local admission wait, 0–60000 ms; expiry uses existing Plan/error policy. This is not an end-to-end inference deadline |
| `ROUTE_RETRY_MAX_WAIT_MS` | `10000` | Cap on one in-proxy retry wait; a longer `Retry-After` goes to the client as a header |
| `ROUTE_HANDOVER_TIMEOUT_MS` | `120000` | How long a handover waits for in-flight turns before reverting to serving (409) |
| `ROUTE_STREAM_PASSTHROUGH` | off | `1` forwards validated frames immediately to cut the first-token delay |
| `ROUTE_STABILIZE_PROMPT_CACHE` | off | `1` peels the trailing `<total_tokens>…` footer from system text before the external request to keep the prefix cache (opt-in) |

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

By default, only literal loopback (`127.0.0.1` or `::1`) HTTP/HTTPS endpoints are allowed. To use an existing remote server, explicitly set `LOCAL_LLM_ALLOW_REMOTE` (`allow_remote: true`) and an `https://server/v1/messages` URL. Remote HTTP without an explicit matching `LOCAL_LLM_HTTP_URL`, URL credentials/query/fragment, redirects, and invalid credential headers are rejected. Certificate verification is not disabled. Prefer HTTPS or a separately prepared loopback tunnel; an exact HTTP pin transmits prompts and server credentials without encryption. Opting in trusts the configured DNS name and its resolution.

`-CheckLocal` sends only a GET to the same-origin model-list endpoint. It disables environment proxies and redirects, uses only the Local key in a fresh `Authorization: Bearer` header, and limits responses to 1 MiB with a five-second socket timeout. Output contains only model counts, configured-model presence, and fixed error categories. Model metadata success does not establish Messages inference or tool compatibility. If the server has no model-list API, skip that optional check and validate inference separately. A failed check never invokes Plan fallback.

Local mode does not read the OpenRouter key. Real `openclaude.config.yaml` / `.env*` settings are ignored by Git; only the credential-free `openclaude.config.example.yaml` template is distributed (the old `.env.*.example` files are deprecated). The server and model still need real validation later.

Run real loopback HTTP integration tests with `node --test tests/*.test.mjs` and configuration/metadata tests with `python -m unittest discover -s tests -p 'test_*.py' -v`. Their servers and Plan responses are synthetic; they do not contact real LLM providers or accounts.

Independent validation after these Local additions passed Node tests, Python tests (counts change per release; run the commands below), Python self-tests, Node/Python syntax checks, and PowerShell parsing. Real server/model/authentication/TLS deployment checks remain deferred.

## OpenRouter compatibility mode and startup checks

```powershell
.\OpenClaude.ps1 -Provider OpenRouter -OpenRouterCompat
```

Requires Claude Code 2.1.304 or newer and the same two `openclaude.config.yaml` OpenRouter settings. All Claude model roles use the selected `OPENROUTER_MODEL`, without Plan login or fallback. The LocalCompat profile retains client plugins, skills, MCP, hooks, subagents, thinking and effort settings, ordinary configuration sources and default permissions. API-key helpers are disabled; the proxy route and temporary authentication are pinned in launch settings. Provider credentials stay in the proxy. Unknown native fields/betas and token counting are rejected. Ordinary routing and both Local modes remain available.

Responses are fully validated as JSON before conversion to client SSE. This mode uses one 120-second upstream attempt and a 30-second failure cooldown, with OpenRouter provider fallback disabled. Model identity, Messages/tool support and reasoning behavior still depend on the selected model; forwarding effort does not establish Sonnet-equivalent reasoning.

Every interactive launch now makes one minimal provider-only inference check before opening the CLI: a 15-second deadline and a 512-token budget for HTTP providers; CLI backends use their configured deadline and native output constraints. OpenRouter charges may apply. Logs contain `connection_check`, provider, inference/model/reply booleans, a fixed reason and optional HTTP status, never endpoints, keys or response bodies. Check failure logs `provider_connection_unavailable` with `action: continue` and still opens the CLI. Ordinary mode retains Plan routing/fallback; explicit Local-only and compatibility modes retain their Plan block. Configuration errors and missing executables remain startup errors. Standalone `-CheckLocal` and `-DiagnoseLocal` still exit after diagnosis.

`ready` confirms the configured model, exact diagnostic reply and normal completion. `timeout`, `authentication_failed`, `http_error` and `connection_failed` identify connection failures. `reply_mismatch`, `incomplete_response` and `invalid_response` identify response-validation failures: a reasoning model may exhaust this small budget or return extra text despite a working connection. The check does not prove complete CLI compatibility.

## Live dashboard

The dashboard and settings share consistent colors, cards and controls with improved secondary-text contrast. On wide screens the dashboard places key metrics alongside the request flow, with four trend panels below. Runtime diagnostics and activity records expand on demand; refreshes preserve disclosure state and summary keyboard focus. Narrow screens stack the panels. The layout uses existing observed data and polling intervals, with no extra inference or polling.

```powershell
.\OpenClaudeDashboard.ps1
```

A **read-only** page bound to `127.0.0.1`. The port is **fixed at 8787**; an older dashboard instance is stopped automatically so the same port is reused. If another program holds that port the server fails with a message instead of silently moving; pass `--allow-port-fallback` to pick a free port instead.

Stopping a previous instance (including `python routeDashboard.py --stop`) happens **only when both the pid and the process creation time stored in `.dashboard.pid` match**. A recycled pid, our own or an ancestor process, or a legacy record without identity is refused with one line (`identity_mismatch` / `own_tree_pid` / `identity_unverifiable`) and the stale record is overwritten on the next start. Right after an upgrade, stop an older dashboard manually once (`Ctrl+C`) before starting. Non-Windows has no creation-time primitive, so the documented behavior is to refuse rather than signal a bare pid.

The refresh interval follows the situation: two seconds while routing is active, ten seconds while idle, fifteen when the tab is hidden. The header shows a one-line summary (`오늘 43.8M토큰 · $1.42 · 남은 한도 $48.25 · 우선 확인: …`).

**Recent request trends** use up to 60 minutes of in-memory, per-minute completion observations: counts, average response time, failure rate and reported output throughput. Missing intervals remain disconnected. Failure rate excludes cancellations; average latency includes them. The history starts with the new proxy and resets on restart.

The **request flow** shows Claude Code → Claude-Cheat proxy → the selected model service or Plan, using an SVG path and a central processing bar. In-flight requests remain visible as a count and status; a new request or an observed batch briefly activates the bar. Local slot occupancy includes retry waits. Particle density follows reported input/output token increments from completed external responses, capped at 24 per direction. The view adds no inference calls or polling.

The flow card separately shows externally routed successful responses observed between refreshes and cumulative proxy failures. A brief warm highlight indicates at least 10,000 reported input or output tokens in one refresh interval; it is not a temperature or overheating measurement. Partial coverage and unreported values are labeled; this is not a trace of individual request timing.

The first snapshot establishes a baseline. No completed response is shown separately from unreported usage; tokens can remain unavailable while work is active. Plan, failed, cancelled and usage-unreported replies are not estimated. Hidden/offscreen views suppress particles. Animation is enabled by default and the flow card offers a page-session-only toggle without changing system settings. This is observed usage visualization, not live token streaming or per-request tracing. The header and proxy box share the same observed status; the usage page's fetch time is not the background collection completion time.

Observed input and output totals and reporting sample counts come from successful responses of the selected external provider during the refresh interval. Bars cap at 10,000 tokens; read the numeric value for exact totals. Input excludes separately reported cache tokens. Plan, failures, cancellations and responses without usage are excluded; unknown, awaiting measurement and a reported zero remain distinct. Existing snapshots are reused without extra inference or polling.

The upper-right status indicator uses both color and text: red for Plan, green for external models, brown/amber for active warnings, and a neutral indication when idle or unknown.

The performance card reuses proxy counters and sends no additional inference requests. Observed output tokens/second is the sum of reported output tokens divided by total request time for eligible successful responses. It includes queueing, network and retries; multiple models may share one provider bucket. Reported usage from fully validated streams is included; Plan, cancellation, failures and unreported usage are excluded. GPU utilization and pure generation speed are unavailable. New token counters begin after restarting the proxy with the updated code; older omissions are not recovered.

The summary at the top places the active service and key request metrics first. Charts immediately below show the last 60 minutes of external versus Plan routing and hourly token usage over the last 24 hours, with expandable hourly values. They reuse existing observations without sending additional model requests. Metrics wrap into multiple rows on narrow screens.

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
| Latency | session p95 ≥20s | shows **response and first-byte p95 separately**; they can describe different requests and include network, queueing and buffering |
| Session idle | no decision for 30 minutes | reference |

| Panel | Content |
| --- | --- |
| Now | **OpenRouter, Local LLM or CLI serving / Plan bypass / idle**, the **last request's actual routing model**, age, reason |
| Performance | Live proxy session requests, failure rate, average requests per minute, external and Plan routes, response and first-byte latency. Observed output token throughput is shown only when response usage is reported. |
| Alerts | Shown when Plan bypass reaches 50% within ten minutes, a circuit opens, a 400/413/422 rejection repeats, or the balance drops below 10% |
| Last 60 minutes | Replacement versus Plan counts, circuit opens |
| Bypass reasons | Counts per `native_feature` `detail` with **meaning and action**, external failure statuses and validation classes |
| Credits | Key limit, spend this period, remaining, reset date (OpenRouter `/api/v1/key`) |
| Today | Replacement and Plan tokens, input/output, cache (**5m and 1h TTL split**), **cost with its status** |
| Hourly tokens | The last 24 hours as replacement versus Plan bars |
| Route transitions | When the route changed, its reason, model, count and duration (last 12) |
| Per session | **Unique turns** (deduplicated), replacement/Plan turns, replacement tokens, **cost and status**, model |
| Help | CLI commands (`--status` and options), HTTP API, reason code table (meaning/action), state badges, exit codes, data sources |

**Turns are counted exactly once.** Claude Code writes one assistant row per streamed update, and `/resume` or a fork copies the history into another transcript, so per-file totals count the same turn several times (42% of assistant rows were duplicates in this machine's 7-day window). The dashboard identifies a turn by `message.id`+`requestId` (falling back to `msg:`/`uuid:`), so a duplicate adds no turn and only its larger token delta is applied. Lower turn counts than earlier versions are therefore expected.

**Cost carries a status.** Replacement traffic priced from the catalog is **estimated**, Plan traffic is **included** (a $0 attribution), and a model without a rate card is **unknown** (null cost). Cache-write tokens are split between the 5m and 1h TTLs, priced at the 1h rate when the card has one and at the standard write rate otherwise, with the choice shown. Confirm actual charges against the provider's records. The **model that actually answered** is recorded by the proxy just before a response is delivered; the name the CLI shows is the request alias (`claude-sonnet-5-5` and so on), so this value is the real provider model.

The aggregation cache (`.usage-cache.json`) carries the turn-ownership ledger and is about **0.5MB** for a real 7-day window (5.4MB for a synthetic 2,000-file/100k-turn fixture; above 200k claims the oldest files' claims are evicted whole). A cache version change forces one full rescan.

The page shows no key, prompt or request body. It writes only `openclaude.config.yaml` beside the launcher, and only when a save is requested. It is a separate process, so it can run beside the routing session.

### Editing settings and testing connections

Settings live on a **separate page** (`http://127.0.0.1:8787/settings`) reached from the top navigation; both pages share one server and session token.

| Behaviour | Detail |
| --- | --- |
| Save | Only the managed keys change; other comments, sections, line endings (CRLF) and a byte-order mark stay, and the previous file is kept **byte-for-byte** as `openclaude.config.yaml.bak`. When a service has no section yet and its deprecated `.env` still holds values, the new section is seeded with them so a stored credential is not silently shadowed. Concurrent saves are serialised by a lock file; an unreadable file (encoding or syntax error) is never overwritten and reports a code and line number |
| Secrets | Never sent back to the browser (`sk-or-…cd0` style). Leaving the field empty keeps the stored value; only a new value replaces it |
| Server addresses | The Messages, HTTP-exception and model-list URLs are masked like keys (`http://12•••••.1:11434/v1/messages`: only the first and last two host characters, `user:pass@` and queries hidden; a non-standard custom path becomes `/•••`). The original is never sent to the browser; empty keeps it |
| Clear | Optional addresses (HTTP exception, model list) are erased with the **Clear** button (the `clear` list of `POST /api/config`). Required fields and keys cannot be cleared |
| Validation | Model ID shape, key shape and Local URL rules (loopback, HTTPS, same origin) are checked before writing; a failure leaves the file untouched |
| Protection | Both reading settings (`GET /api/config`) and saving them require the session token embedded in the page; POST also requires a same-origin request, so other sites cannot call it |
| Tests | **Model check** reads the model list only; **inference test** sends one request with a 16-token OpenRouter or 512-token Local output limit (may incur costs or use server resources). Local verifies the selected model ID, exact `OK` reply, and normal completion |
| Model picker | **목록** opens a searchable list with context length and price and fills the field. `GET /api/models?provider=openrouter\|local` (session token required, 10 minute cache) |
| When it applies | Saved settings take effect in the **next** session; restart a running one |

### Current model checks and duplicate selection

Distinguish the configured model (settings for the next session), the connection-test model (the response model ID is checked against the requested ID), and the last routed model (recorded after response validation). An ordinary session can still start after a failed connection check, with a warning and Plan fallback available. Model-list metadata checks do not prove inference works.

The dashboard does not read Claude Code's current `/model` selection in real time. With no live session or no requests yet, settings and historical records are not proof of a current model connection. During a live session, it compares the latest authenticated proxy metrics with that session's logs rather than attributing an old session's route or code hash to the current proxy. The current-state card distinguishes the session provider, loaded primary model and last processed model. A Plan or dual-provider request whose model is not reported remains unknown rather than being guessed.

The 60-minute and 10-minute route statistics use actual current-session logs. When those records are absent, the dashboard says the log window is unavailable and points to the proxy's cumulative counters. Session totals are not presented as time-window counts.

The picker lists an exact model ID only once per service, trimming surrounding whitespace and retaining the first row's metadata. IDs differing in case or a suffix such as `:latest` remain distinct. Selecting an item fills the input; saving is a separate action. Per-request provider selection and alternate-alias collision checks also apply. This does not prohibit different servers using the same model name or multiple independent sessions.

### CLI model backends

### Original Sonnet and black box

Select **Original Claude Sonnet** as the default service or run `OpenClaude.ps1 -Provider Sonnet` to launch Claude Code with `--model sonnet` directly, without a proxy. Existing sessions are not stopped. Conflicting provider credentials, route or model settings cause a safe stop with guidance; values are not printed or changed. Managed policy and the model actually answering are not verified merely by requesting Sonnet.

On supported Claude Code versions, direct Sonnet requests Opus Advisor by default; an explicitly selected or disabled advisor takes precedence. Account, model or organization policy may reject the launch or prevent activation. Advisor reads the full conversation and tool results and consumes additional usage. Direct sessions do not go through the proxy, so advisor calls and tokens are not observable there; an independent advisor for Local, OpenRouter or CLI replacements is not yet implemented. [Advisor requirements](https://code.claude.com/docs/en/advisor)

### Usage statistics

The **Usage** menu shows daily, weekly and monthly observations by provider, requested and reported model, and launcher instance. Weeks start on Monday in the computer's local time zone. New instance records are not combined with the older single-session history. Existing sessions and direct Sonnet are not observed by the new proxy ledger; unreported models or tokens remain unknown.

While visible, Usage refreshes the existing cache every 30 seconds and pauses polling in hidden tabs. On a temporary fetch failure it retains the last successful result for the same period and marks it stale. The per-launcher request and failure counters cover the launcher lifetime, independently of the selected reporting period. Historical folders without a live handoff or event file do not consume the corresponding 32-file processing limit. Discovery stops after 4,096 directory entries and reports partial coverage; entries beyond that limit may remain undiscovered.

`.usage-stats/` holds a local SQLite ledger and per-instance event files without prompt or tool content. The 13-month display window does not delete stored data. These are observed usage and latency, not billing or GPU load. Recording errors or process termination may lose the last asynchronous event; check the coverage warning and collection start date.

Open the **Black Box** menu to start recording and mark an incident when a problem occurs. Metadata mode records limited diagnostics. Explicit detailed mode can capture conversation and tool input/output added after a newly launched session is bound; continuing an existing session, custom `--settings`, disabled hooks or managed policy can prevent binding. Check the connection status. A direct Sonnet session has no proxy metrics; a separate proxy snapshot may belong to another session.

Records are stored in `.blackbox/recording.json` with a 2 MiB / 256 event bound; older events are discarded. Detailed records can contain sensitive information even after pattern-based redaction. Windows directory permissions depend on the parent ACL. Only complete recent transcript records within 64 KiB are included at each incident; older or oversized content may be omitted. The recording directory is gitignored.

The OpenRouter and Local settings provide separate actions to delete the stored API credential or reset that service's managed settings. They sanitize the service's managed values in the active YAML, existing `.bak`, and its legacy `.env` while preserving other services and the default selection. If the reset service remains the default, configure it again or select another before the next launch.

Deletion is irreversible and a failure across multiple files may leave some values behind. Running sessions can retain loaded credentials until stopped. Environment variables, external copies and backups, arbitrary comments outside managed fields, and provider-issued keys are not removed or revoked; revoke a key at its provider when necessary.

Choose the CLI kind in its settings, save those settings, and then select **CLI** as the default execution service. Run `OpenClaude.ps1` for a new session; a one-off override is `-Provider CLI`. Claude Code remains the front end and executes SSH/MCP tools, skills, plugins, hooks and permissions. The backend returns validated JSON text or tool-call proposals; it cannot execute its own tools.

The Codex backend runs in an isolated read-only temporary workspace. That restriction does not determine the front-end Claude Code tools' access to the project. The backend is instructed to follow actual host tool permissions and results; this does not bypass a genuine access restriction.

Local LLM and OpenRouter requests also receive guidance to judge tool access from the host's stated policies and actual tool results, respecting explicit read-only restrictions. This guidance is not inserted into direct Sonnet or Plan fallback requests.

For Codex, the CLI model picker reads its local model cache without running inference. The cache does not guarantee current account access or backend compatibility. If unavailable, enter a model ID manually. Claude Code and Grok currently have no verified catalog integration and require manual entry; the Grok backend is not yet enabled.

CLI monitoring reports proxy requests in flight, HTTP 429 responses, failure categories, and the last 60 minutes of reported input/output tokens and usage coverage. The CLI adapter has a concurrency limit of four and rejects excess requests without a queue. Proxy in-flight requests are not the actual CLI process count or GPU load. Coverage uses successful CLI responses as its denominator; unreported usage is not treated as zero. HTTP 4xx can include CLI backend validation errors. Restart the session to collect the new metrics; older proxies leave them unreported.

CLI process time measures just before process start through observed termination, including startup, CLI preparation and communication; it is not pure model inference time. It retains up to 512 validated successful response samples and excludes failures, cancellations and unreported timings. An unreported actual model is never inferred from the requested model.

Run `python tests/live_cli_smoke.py --run` explicitly to make one real Codex inference through the authenticated local adapter, using the saved CLI configuration and login. Usage may be incurred. The isolated test checks a synthetic tool proposal and executes no tool. It does not verify the proxy, front-end Claude Code, Agent Teams or SSH/MCP combinations.

Saved settings apply to the next session, not to requests already running. Finish work and subagents, exit Claude Code normally, restart, and check the active session and recent route in the dashboard. Plan is currently a fallback route, not a separately selectable default; there is no automatic Local-to-CLI switching. A started response is not replayed on another route. Saved settings do not prove connectivity: inference tests make real calls and may incur usage. CLI backends require their own login and a verified version; Grok is not currently enabled. Verify the model and executable after changing CLI kind.

Verified versions are Claude Code **2.1.312** and Codex **0.165.0**. Other versions fail closed until validated. Grok is detected and selectable, but backend execution is currently refused with `backend_version_unverified` because its tool isolation has not been verified. Installed does not mean connected or supported.

Backend processes run without a shell in an empty temporary directory, with isolated configuration and a filtered environment. Prompts and responses stay in memory. Responses must reference advertised tools and valid argument JSON objects, but the adapter **does not validate arguments against the advertised `input_schema`**. The request's `max_tokens` is included in the prompt, but is **not enforced as an exact generation token limit**; do not use the CLI backend when a strict per-request output limit is required. Cancellation cleans up owned children. Each request makes one backend attempt before ordinary Plan fallback, with a configurable 10–120 second deadline (default 60) and at most four concurrent backend requests.

Explicit connection tests and startup checks send a fixed `OK` request using the backend CLI's own login and may consume quota. Readonly `/api/diagnostics` never starts inference or login. Requested model, reported model and measured usage remain separate; missing native metadata is shown as unknown.

Forward native front-end arguments with `-CliArgs @('--resume', 'session id', '--plugin-dir', 'C:\my-plugin')`; direct Python invocation accepts arguments after `--`. Help, version and management commands bypass routing and inference checks. Conflicting restricted-mode options fail explicitly. Front-end Claude Code login and backend login are separate requirements. Backend model quality and complete native feature parity are not guaranteed.

See the [requirement audit](docs/REQUIREMENTS-AUDIT.ko.md) for implementation evidence and remaining validation limits.

### Request-shape measurement (sizing a CLI backend)

The **request shape (CLI candidates)** card on the dashboard counts requests by **model family x tools**, to find out how many requests a text-only CLI could serve (no tools, tool results or images).
- The proxy writes one content-free event per request (`request_shape`: family, tools, tool blocks, non-text media, stream) to the route log. It never records the model id, a prompt or a tool name.
- It is recorded in Local and OpenRouter sessions (when a route log exists), and is also exposed as `request_shapes` in `/api/status`.
- Note: Opus, Haiku and Fable requests always go to Plan today, so a CLI candidate must be a tool-less **Sonnet** request. If this number is small, CLI routing would have little effect.

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

A running proxy exposes its own counters at `GET /api/metrics` (same session token required, loopback only): request count, external/Plan split, p50/p95 latency, retries, streams, circuit opens and bytes in/out. The dashboard authenticates this response with the handoff token before identifying a **current session**, and uses the counters from that verified proxy. The token, port and PID are not returned in the settings response.

Counters retained in `.proxy-metrics.json` are shown as **previous run history** when no live proxy is verified; they do not generate current-session failure or latency insights. `proxy_stale` compares the last recorded proxy code with the current source. It does not prove that an old proxy is currently running.

### Read-only diagnostics API for AI debugging

`GET http://127.0.0.1:8787/api/diagnostics` returns versioned JSON for agents and scripts while the dashboard is running. Loopback and Host checks apply. No session token is required for this redacted, read-only report.

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/diagnostics | ConvertTo-Json -Depth 8
# or
curl.exe --fail --silent http://127.0.0.1:8787/api/diagnostics
```

This request never edits settings, calls provider APIs, performs inference or scans transcripts. It authenticates the recorded loopback proxy once and reads configuration, routing history and existing snapshot cache state. Keys, tokens, server addresses, filesystem paths, PIDs, prompts, bodies and raw exceptions are excluded. A model containing a URL, absolute path or sensitive value is returned as `null` with `model_status: "redacted"`.

| Field | Meaning |
| --- | --- |
| `schema_version` | Currently `1` |
| `status` | Component summary: `ok`, `degraded` or `error` |
| `checks[]` | Component, status, stable machine-readable code and recommended action code |
| `configuration` | Saved default, next provider, configured model and provider configuration validity |
| `session` | Authenticated proxy presence, its loaded primary model, alternate and Plan fallback policy |
| `last_route` | Last routed model, timestamp, age and whether it belongs to the current session |
| `metrics` | Current proxy request/failure/retry/circuit counters; historical files are not treated as current |
| `snapshot` | Availability, age since last successful refresh and refresh-error presence |
| `next_check` | A separate authenticated metadata-check request; not executed automatically |

Useful check codes include `configuration_incomplete`, `routing_configuration_invalid`, `provider_choice_required`, `session_not_running`, `proxy_model_matches_settings`, `model_settings_changed`, `proxy_provider_mismatch`, `proxy_model_unreported`, `proxy_model_redacted`, `upstream_not_checked`, `snapshot_refresh_failed`, `snapshot_stale` (over 30 seconds) and `request_failures_recorded`.

A successfully generated report returns **HTTP 200 even when a component has an error**; inspect its body rather than assuming HTTP 200 means inference works. An invalid Host returns 403. A report-generation failure returns 500 with the fixed code `diagnostics_failed`, without the exception text. No live session is a degraded/waiting state rather than proof that the dashboard is broken. Older proxies may not report a loaded model: `session.loaded_model` remains `null` instead of being inferred from settings. `session.plan_fallback` describes routing policy, not a guarantee after streaming output has already started.

The existing `POST /api/test` accepts `{"provider":"local","mode":"metadata"}` for a model-list-only check and requires the page's `x-dashboard-token`. Actual inference is a separate `mode:"inference"` request and may cost money. A healthy local proxy and matching model metadata do not prove upstream inference succeeds.

The Settings **tool round-trip test** and authenticated `POST /api/test` with `{"provider":"local","mode":"tool_roundtrip"}` use the saved Local, OpenRouter or CLI settings. No `values` are accepted. Up to two real inference calls are made without executing tools; CLI token use depends on its backend. Each dashboard process runs one such test at a time (`probe_busy` otherwise); separate command-line probes are not covered by that limit. The page separates tool success, reported model equality and token reporting, and does not persist the result. It tests the saved backend at the start, not the currently running Claude Code session. Closing the browser does not guarantee cancellation; per-socket timeouts do not impose an overall wall-clock limit.

### How the components stay aligned

- Account and model context follows the verified live session, then the saved default, recent routing history, and configured services. Changing the default affects the next launch while preserving the current session.
- The last-request label follows the actual routing record, independently of the session's default provider. This also applies to dual-provider sessions.
- Local context skips OpenRouter account and pricing calls and shows them as not applicable. Shared token totals are labeled as external-service usage; OpenRouter rates are not applied in Local context.
- Changing the service, model or pricing version invalidates the usage view cache. A cold page response does not wait for OpenRouter.
- Saving service settings preserves default and current-session metadata. Failed config reads are not interpreted as empty settings and cannot overwrite the default.
- `--offline` skips both external calls and local proxy checks. Its `active_session` is `null`; routing state and exit codes describe saved history rather than verified liveness.

These targeted checks use temporary configuration and a real loopback Node proxy, without launching Claude Code or calling external inference:

```powershell
python -m unittest discover -s tests -p test_route_dashboard.py -k test_real_proxy
node --test tests/dashboard_dom.test.mjs
```

The UI tests execute the JavaScript embedded in the actual Python-generated pages, including computed appends. They do not validate visual browser layout or real model response quality.

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
| Missing `openclaude.config.yaml` | Actual launcher directory, not current working directory |
| OpenRouter 401 / missing authentication header | Router key file and key validity; successful HUD installation does not prove inference auth |
| Old Local address remains active | Process environment overrides, global shim target, then restart |
| Sonnet continues using Plan | Check `route_selected` with `-Debug`; `native_feature` can be legitimate (Plan-only model, token counting, native history). Repeated `unsupported_beta` means the CLI added a beta the router does not know yet, so update the launcher |
| Dashboard shows `실행기 재시작 필요` | The running proxy started before the current source. Use the alert's **명령 복사** button to copy `.\OpenClaude.ps1`, restart the session, and compare **프록시 코드** with **소스 수정** in the status card |
| Session drops abruptly | Check `.route.log` for `parent_disconnected` (launcher exited). Make sure no other script kills `python` processes wholesale; stop the dashboard with `python routeDashboard.py --stop` |
| Repeated historical `503 ... restart required` | Stale session/proxy and first upstream error; restart with current launcher |
| HUD or MCP failure | Compare ordinary Claude; loading settings does not prove server connectivity |

A routed session also appends its routing events to `.route.log` beside the launcher: a `proxy start` line per session, then timestamped `route_selected`, `external_attempt_failed` and `circuit_open` entries. This explains past sessions that ran without `-Debug`, and the file is excluded from Git by the `*.log` rule.

The lines of one turn share an **8-hex `traceId`** (also exposed as `metrics.last.traceId`), so four `external_attempt_failed` events plus the `circuit_open` and `route_selected` can be reconstructed together. All diagnostics pass **one logging sink that scrubs credentials**: the configured API key, `Bearer …`, `sk-*`/`sk-ant-*`/`sk-or-*` and `x-api-key` values become `[redacted:provider-key|bearer-token|api-key]` tags while model names, status codes and trace ids survive. A provider error body is read up to **64KiB**, scrubbed the same way and relayed, with a truncation event for longer bodies. Scrubbing is pattern-based, so it cannot guarantee detection of obfuscated credentials.

Exit and relaunch to change providers. `/model` selects Claude models, not OpenRouter versus Local. To return to ordinary Plan use `claude` in a clean terminal. This launcher does not modify the parent terminal, but old user/system `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` or `CLAUDE_CONFIG_DIR` variables may still be inherited. Do not delete saved login or MCP settings as a blanket fix.

The security model, trust boundaries, CSRF/DNS-rebinding handling and residual risks are documented in [`docs/SECURITY.ko.md`](docs/SECURITY.ko.md) (Korean).

## Restarting the proxy without losing the session

```powershell
OpenClaude -Takeover        # run in a second terminal
```

The new launcher reads the live port and token from `.session-handoff.json`, asks the running proxy to drain (`POST /api/handover`, session token required), answers new requests with 503 + `Retry-After: 1`, and waits for in-flight turns (default cap **120 s**, `ROUTE_HANDOVER_TIMEOUT_MS`) before rebinding the same port with the same token. If the cap expires while a turn is still active, the proxy does **not** exit: it reverts to serving and replies HTTP **409** with `{"drained": false, "reverted": true, …}`; the launcher then aborts the replacement and reports a retryable error instead of truncating the live response. Concurrent handover requests share one drain run and the exit hook fires once. The CLI keeps its base URL and simply reconnects; new requests during the drain get 503.

## Validation and limitations

Personal machine configurations, account usage, billing records, and live session logs are not stored in this repository. Run synthetic tests without external model inference:

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
node tests/simulate.mjs --soak 3000        # synthetic traffic, long session, memory
node tests/fuzz.mjs --iterations 2000      # proxy fuzzing (deterministic seed)
python -m unittest tests.test_fuzz_dashboard   # dashboard fuzzing
python tests/live_smoke.py --dry-run       # report which settings exist, no billing
```

- The Windows process-tree tests (`test_local_lifecycle`) and the concurrency stress (`test_dashboard_concurrency`) are **skipped by default**: they spawn real process trees or 12 threads, so they are slow and load sensitive. Enable them with `$env:CLAUDE_CHEAT_SLOW = '1'`.
- The process-tree suite needs **Git Bash**. `C:\Windows\System32\bash.exe` is WSL and cannot run the Windows python path the harness passes, so it skips itself.
- The dashboard enforces a **Host allowlist** (127.0.0.1, localhost, ::1) against DNS rebinding and sends `nosniff`, `no-referrer` and `X-Frame-Options: DENY`. Write endpoints require the session token and a same-origin request.
- Real provider calls stay out of the suite. After a configuration change, confirm once with `python tests/live_smoke.py` (16 tokens, well under a cent per run).
- `python tests/model_quality_probe.py --provider local --run` opts into up to two real model calls; `openrouter` and `cli` use their saved settings. It checks one synthetic tool proposal and continuation without executing tools. Reported model equality and complete token usage are separate metadata. This direct backend probe has no Plan fallback, does not exercise Claude Code or the proxy, and does not prove coding quality or native feature parity. Direct Sonnet is not supported. Calls can incur cost and shared-server load.
- The launcher has a **non-interactive `--print "prompt"`** mode (JSON by default, `--print-text` for people). `python openrouterAdvisor.py --print "Reply with exactly OK"` and `$env:CLAUDE_CHEAT_LIVE = '1'; python -m unittest tests.test_cli_integration` invoke a real CLI and provider and may incur charges.
- `node --test tests/cli_smoke.test.mjs` tests the experimental smoke harness with a synthetic CLI and mocked provider. Separately, `node tests/cli_smoke.mjs` uses user settings and a real CLI and provider, **may incur charges**, and remains unverified against real CLI versions. Windows `.cmd` shell wrappers are not supported. A timeout terminates only the direct child; descendant process cleanup is not guaranteed.

The tests cover payload preservation, authentication isolation, tool-result round trips, the initial request plus three retries, Plan fallback, recovery, cancellation, timeouts, and empty responses. Synthetic success does not establish live-provider or full native-feature parity.

### Simulation and performance

A mock upstream generates traffic so throughput, latency, memory and routing decisions can be measured without calling a provider.

```powershell
node tests/simulate.mjs                                   # mixed load, long session, circuit breaker
node tests/simulate.mjs --requests 4000 --concurrency 48 --json
node tests/bench_proxy.mjs --quick                        # fresh-process runs + correctness gates
node tests/bench_proxy.mjs --soak-seconds 10              # time-based soak (child RSS, post-GC heap)
node --test tests/bench_harness.test.mjs                  # regression tests for the harness itself
node --test tests/simulation.test.mjs                     # simulator smoke test
```

Scenarios cover a plain reply, SSE streaming, tool calls, a 429 retry, a 1MB body, an unsupported beta bypass, and the circuit breaker with cooldown; each response carries a marker so the simulator can assert which path served it. Any mismatch exits 1.

Reference numbers (this development machine, Node 24). Simulator and soak figures share one process; the separated harness figures come from bench_proxy.mjs.

| Metric | Value |
| --- | --- |
| Mixed throughput | ~700–980 req/s at 1,200 requests / concurrency 24, p50 16–25ms, p95 55–82ms |
| Long session | 120 turns, 247KB history: 3.1ms (3.9ms before the fix) |
| Body size | 1MB in 7.7–8.9ms (~120MB/s); fresh-process harness 1MB in 8.4–9.8ms |
| Retry cost | 1MB + one retry 10.7ms vs 12.8ms before, four failures 9.8ms vs 12.1ms (~17–19% better) |
| Circuit | 4 failures → Plan fallback, Plan during cooldown, external again after recovery |
| Long soak | 150,000 requests at 266MB; **1.2M requests reached 1.09GB RSS** (V8 heap expansion — a 256MB cap run passes, so it is collectable) |
| Session-cache restart | Cold scan **1,119ms → 20ms** on real data (357 files, 464MB): `.usage-cache.json` (v5) stores per-file aggregates with size, mtime and head/tail 4KB sha256 fingerprints plus the turn-ownership ledger, so an in-place rewrite is rescanned and unchanged files are not read |
| Page transfer | 39.3KB → **12.0KB** with gzip when the client accepts it; loopback render 2.7ms per request |
| Large route log | A cold read parses only the **last 4MB** while keeping the offset absolute, so a weeks-old log does not slow startup |
| Heap cap | The launcher starts the proxy with `--max-old-space-size=512`; a 1.2M-request re-measure peaks at **510MB with a 239MB drift** (1.09GB without the cap) |

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
- If `CLAUDE_CONFIG_DIR` is absent, empty, or the historical `~/.claude-openrouter`, the child leaves it unset to preserve native Claude Code configuration discovery. Explicit paths, including `~/.claude`, are preserved. Injecting `~/.claude` changes where personal `.claude.json` MCP registrations are read; see the [official MCP configuration locations](https://code.claude.com/docs/en/mcp-quickstart#find-your-configuration-on-disk). No directories, settings, or authentication files are copied, migrated, or deleted.
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
