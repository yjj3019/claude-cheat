# Claude Code · Sonnet replacement with Plan fallback

[한국어](README.md) | **English**

[MIT License](LICENSE) · [License selection review](LICENSE-REVIEW.md)

A local router that preserves the existing Claude Code Plan login and settings while sending compatible Sonnet requests to OpenRouter DeepSeek or a local LLM. The repository includes the launcher, proxy, and synthetic regression tests. It does not contain API keys, OAuth credentials, personal settings, or private diagnostic scripts.

**Status: provider selection, retries, Plan fallback, and recovery are implemented. Preserving every native feature while actually replacing the main Sonnet model is not fully achieved.**

Check the installed Claude Code, Node.js, and Python versions for compatibility. No personal development-machine configuration or minimum-version matrix is published.
- See the [public history guide](docs/HISTORY.ko.md); private environment records are excluded.

## Setup and execution

Install Node.js, Python, and Claude Code separately, and sign in to Claude Code with the existing Plan subscription. The current authentication gate specifically checks `subscriptionType="pro"`; Max and other plans are not guaranteed to pass. No npm or pip packages are required by the checked-in source.

For OpenRouter, create `.env.openrouter` in this checkout's root:

```dotenv
ANTHROPIC_AUTH_TOKEN=YOUR_OPENROUTER_API_KEY
```

Replace the placeholder with a real OpenRouter key. This file is read next to `openrouterAdvisor.py`, not relative to the current working directory. Use one KEY=VALUE per line; blank lines, full-line comments and matching quotes are supported, but multiline values and shell expansion are not. Do not add inline comments after a key. This value is read only by the router. It does not replace the CLI's subscription login. `.env` and `.env.*` are ignored by Git; do not publish credentials.

From the checkout root:

```powershell
# Default: OpenRouter DeepSeek
.\startClaudeOpenRouter.ps1
# Local Messages API, after configuring a real local service
.\startClaudeOpenRouter.ps1 -Provider Local
```

If you install a global launcher separately, configuration is read beside its target script. Cloning this repository neither installs a global command nor changes an existing launcher target. Run the script in the intended checkout explicitly.

## Routing, retries, and recovery

| Request | Route |
| --- | --- |
| Compatible Sonnet request | Selected OpenRouter DeepSeek or local model |
| Unsupported native Sonnet feature or conversation history | Original Plan Sonnet request, unchanged |
| Opus, Haiku, Fable | Existing Plan OAuth path |
| External provider fails all four attempts | One request to the original Plan Sonnet model |

- OpenRouter uses `deepseek/deepseek-v4.1-flash-1017`.
- OpenRouter and Local are selected at launch; the router does not switch between them automatically. Select Sonnet in `/model` for eligible replacement requests.
- The external provider receives the initial attempt plus three retries. Each attempt has a 30-second limit, with a one-second delay between attempts.
- Four full timeouts plus retry delays can take approximately 123 seconds before the Plan request begins. Plan response time and the CLI's own retries may add further time. Cancelling stops router retries and fallback.
- After all four fail, the router forwards the original request to Plan Sonnet once. A Plan failure does not loop back to another provider or switch to an Anthropic API key.
- During a 30-second cooldown, requests use Plan. Afterward, one compatible real request checks recovery; concurrent requests use Plan. A validated external response restores the external route. There are no separate paid background probes.
- External responses are buffered, up to 32 MiB, until model identity, message completion, block indices, and tool-input JSON are validated. Failed attempts do not expose partial text or tool calls to the CLI. This delays visible responses, and retries may incur duplicate inference costs.
- Plan requests, responses, streams, and errors pass through unchanged. Native advisor and experimental beta features are not forcibly disabled. Unsupported advisor, server tools, inline tools, signed thinking, and other native history bypass the external provider instead of being removed. Such a bypass is not counted as an external recovery check.
- The router does not block account-level usage credits, so Plan routing is not a guarantee of zero additional account charges.
- Model access and usage credits remain subject to the Claude app and account plan. There is no separate Fable ban or credits-OFF requirement. External providers do not receive the Plan OAuth credential.

Routing metadata uses `route_selected` with provider `openrouter`, `local` or `plan`. Reason `replacement` identifies a validated external response; `native_feature` identifies a native-feature bypass; `fallback` follows failed external attempts; `circuit_open` uses Plan during cooldown or another recovery probe. `external_attempt_failed` records a failed attempt, and `circuit_open` / `circuit_recovered` report circuit transitions. These logs omit keys and request bodies; requests sent to external services may still contain conversation, code, tool definitions and results.

For `native_feature`, `detail` reports the first exclusion: `token_count`, `plan_model`, `unsupported_beta`, `unsupported_request_field`, `unsupported_message_shape`, `unsupported_thinking_config`, `unsupported_output_config`, `unsupported_tool_choice`, `native_thinking_history`, `unsupported_content`, `unsupported_content_block`, `unsupported_content_field`, `unsupported_tool_caller`, or `unsupported_tool_definition`. Exclusion labels are fixed; user text, tool names, signatures and credentials are never logged. For `unsupported_beta`, `beta_features` lists up to 16 date-suffixed feature identifiers and `unsupported_beta_count` gives the total. Nonstandard or credential-like entries are redacted; raw header strings are not logged. Restart the launcher to enable new diagnostics; old logs cannot reveal these details.

The CLI or HUD can still display Sonnet/Plan for a replacement response. That label and its cost estimate do not prove the inference provider or actual OpenRouter billing. Check the safe `route_selected` metadata: `replacement`, `native_feature`, `fallback`, or `circuit_open`.

## Local-only Claude CLI mode

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local -LocalMode
```

This opt-in mode launches the real Claude CLI with `--bare`, Sonnet as the routing alias, and the standard Bash/Read/Edit/Write/Glob/Grep tools. Claude Code **2.1.304 or later** is required; verification uses 2.1.304. Older versions are rejected before the proxy or session starts. Standard permission prompts stay enabled (`default`); this mode does not enable bypassPermissions or auto mode. Existing launch commands without `-LocalMode` are unchanged.

The session disables native beta integrations, advisor, and thinking. Bare mode skips automatic hooks, plugins, MCP, skills, subagents, auto memory, and CLAUDE.md discovery; include necessary project instructions explicitly. Two CLI compatibility headers (`interleaved-thinking-2025-05-14` and `effort-2025-11-24`) are accepted only here. Unknown native features remain blocked. See the official [bare-mode reference](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode).

The CLI gets a temporary loopback-proxy key, never the Local server key or Plan OAuth. Plan login is not queried. All proxy Plan paths are blocked, including native-feature bypass and failure fallback. A failed Local request stops after one upstream attempt. The CLI may still have its own retries. Local-only requests allow 120 seconds for long-context compaction; ordinary mode retains its 30-second limit. Unsupported native history and token-count requests fail explicitly. Validate long-session compaction against your server and CLI version using the [compaction procedure](docs/LIVE-VALIDATION.ko.md#local-모드의-긴-대화압축-점검).

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

The Local-only proxy exits immediately when its parent pipe closes, cleaning its listener and active connections even if the launcher is killed before `finally` runs. It logs `local_parent_disconnected`. This owns only the proxy: killing the launcher alone may leave the CLI or Bash descendants alive. Prefer normal exit or Ctrl+C. See the [shutdown verification scope](docs/LIVE-VALIDATION.ko.md#실행-전-오류와-비정상-종료-점검).

To prevent low output budgets from truncating compaction, this mode supplies `CLAUDE_CODE_MAX_OUTPUT_TOKENS` through its per-launch settings, defaulting to 8192. A larger positive whole-number value from the parent environment is preserved; a smaller value is raised to 8192. These per-launch settings take precedence over budget values in user/project files; use the parent environment for a larger budget. Neither the files nor the parent environment is changed. The budget applies to ordinary replies in this Local session as well as compaction. The CLI still applies model caps and remaining-context adjustments; the proxy does not raise request `max_tokens`. Your server must support the output budget. Launches without `-LocalMode` and diagnostic modes are unchanged.

For this mode only, the upstream request uses non-streaming JSON with thinking disabled. The proxy strictly validates the configured response model and converts validated text/tool responses to CLI SSE when needed; IDs, ordered tool inputs, stop reasons, and token usage are preserved. `tool_use.provider_specific_fields` objects/null are preserved as opaque data when present. The CLI may omit them from later history; compatibility with servers that require this state is not established. Unknown response fields and thinking blocks are rejected rather than dropped. The default routing path is unchanged. CLI model/cost displays remain aliases and estimates; verify `route_selected` with `provider: local`. A blocked feature logs `local_mode_rejected`, never a Plan success.

Use a new Local session rather than resuming a native Plan conversation. This mode cannot be combined with `-CheckLocal` or `-DiagnoseLocal`. Exit and relaunch without `-LocalMode` to restore the original integrations.

## Local inference diagnostic mode

```powershell
.\startClaudeOpenRouter.ps1 -Provider Local -DiagnoseLocal
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
.\startClaudeOpenRouter.ps1 -CheckLocal
# Normal launch still checks the saved Plan authentication.
.\startClaudeOpenRouter.ps1 -Provider Local
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

Run real loopback HTTP integration tests with `node --test tests/*.test.mjs` and configuration/metadata tests with `python -m unittest discover -s tests -p test_local_config.py -v`. Their servers and Plan responses are synthetic; they do not contact real LLM providers or accounts.

Independent validation after these Local additions passed **138/138 Node tests**, **13/13 Python tests**, Python self-tests, Node/Python syntax checks, and PowerShell parsing. Real server/model/authentication/TLS deployment checks remain deferred.

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
| Sonnet continues using Plan | `native_feature` metadata and unsupported conversation history |
| Repeated historical `503 ... restart required` | Stale session/proxy and first upstream error; restart with current launcher |
| HUD or MCP failure | Compare ordinary Claude; loading settings does not prove server connectivity |

Exit and relaunch to change providers. `/model` selects Claude models, not OpenRouter versus Local. To return to ordinary Plan use `claude` in a clean terminal. This launcher does not modify the parent terminal, but old user/system `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` or `CLAUDE_CONFIG_DIR` variables may still be inherited. Do not delete saved login or MCP settings as a blanket fix.

## Validation and limitations

Personal machine configurations, account usage, billing records, and live session logs are not stored in this repository. Run synthetic tests without external model inference:

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p 'test_local_*.py' -v
python openrouterAdvisor.py --self-test
node --check claudeRouteProxy.mjs
```

The tests cover payload preservation, authentication isolation, tool-result round trips, the initial request plus three retries, Plan fallback, recovery, cancellation, timeouts, and empty responses. Synthetic success does not establish live-provider or full native-feature parity.

- A real Local service still requires separate authentication, inference, and tool-call validation.
- Unsupported native features or signed history stay on the Plan route. External usage can be zero, and native history may prevent a safe return to the external provider in the same conversation.
- Running only a native advisor on the subscription path during external inference is not implemented. Full native-feature equivalence, including remote managed settings and organization policy with a custom `ANTHROPIC_BASE_URL`, is not guaranteed.
- Request bodies above 16 MiB receive HTTP 413 before fallback. Only versioned `claude-sonnet-*`, `claude-opus-*`, `claude-haiku-*`, `claude-fable-*` names whose version starts with a digit, and the specified DeepSeek ID, are accepted. Unknown models receive HTTP 403.
- Allowed paths are POST `/v1/messages` and `/v1/messages/count_tokens` (optionally `?beta=true`), plus local readiness HEAD `/api/hello`. Other paths are rejected, not forwarded to the subscription route.
- A custom `CLAUDE_CONFIG_DIR` is preserved unless absent or set to the historical `~/.claude-openrouter`, in which case the child uses `~/.claude`. Old MCP/plugin registrations are not automatically migrated; directories are not copied or deleted.
- Diagnose HTTP 429 using the affected account's Usage screen and response. Account-specific limits and reset schedules are not published here.

See the [connection and recovery checklist](docs/LIVE-VALIDATION.ko.md) for a general procedure.

## Source map

| File | Purpose |
| --- | --- |
| `startClaudeOpenRouter.ps1` | PowerShell provider selector and launcher |
| `openrouterAdvisor.py` | Plan authentication checks, environment isolation, proxy lifecycle, isolated consultation tool, self-tests |
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
