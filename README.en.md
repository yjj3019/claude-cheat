# Claude Code · Sonnet replacement with Pro fallback

[한국어](README.md) | **English**

[MIT License](LICENSE) · [License selection review](LICENSE-REVIEW.md)

A local router that preserves the existing Claude Code Pro login and settings while sending compatible Sonnet requests to OpenRouter DeepSeek or a local LLM. The repository includes the launcher, proxy, and synthetic regression tests. It does not contain API keys, OAuth credentials, personal settings, or private diagnostic scripts.

**Status: provider selection, retries, Pro fallback, and recovery are implemented. Preserving every native feature while actually replacing the main Sonnet model is not fully achieved.**

- See the [public history guide](docs/HISTORY.ko.md); private environment records are excluded.

## Setup and execution

Install Node.js, Python, and Claude Code separately, and sign in to Claude Code with the existing Pro subscription. No npm or pip packages are required by the checked-in source.

For OpenRouter, create `.env.openrouter` in this checkout's root:

```dotenv
ANTHROPIC_AUTH_TOKEN=YOUR_OPENROUTER_API_KEY
```

This value is read only by the router. It does not replace the CLI's subscription login. `.env` and `.env.*` are ignored by Git; do not publish credentials.

From the checkout root:

```powershell
# Default: OpenRouter DeepSeek
.\startClaudeOpenRouter.ps1
# Local Messages API, after configuring a real local service
.\startClaudeOpenRouter.ps1 -Provider Local
```

An existing global shim on the original PC also supports `startClaudeOpenRouter.ps1` and `startClaudeOpenRouter.ps1 -Provider Local` from other directories. Cloning this repository does not install that shim or redirect the existing runtime to this checkout.

## Routing, retries, and recovery

| Request | Route |
| --- | --- |
| Compatible Sonnet request | Selected OpenRouter DeepSeek or local model |
| Unsupported native Sonnet feature or conversation history | Original Pro Sonnet request, unchanged |
| Opus, Haiku, Fable | Existing Pro OAuth path |
| External provider fails all four attempts | One request to the original Pro Sonnet model |

- OpenRouter uses `deepseek/deepseek-v4.1-flash-1017`.
- The external provider receives the initial attempt plus three retries. Each attempt has a 30-second limit, with a one-second delay between attempts.
- After all four fail, the router forwards the original request to Pro Sonnet once. A Pro failure does not loop back to another provider or switch to an Anthropic API key.
- During a 30-second cooldown, requests use Pro. Afterward, one compatible real request checks recovery; concurrent requests use Pro. A validated external response restores the external route. There are no separate paid background probes.
- External responses are buffered, up to 32 MiB, until model identity, message completion, block indices, and tool-input JSON are validated. Failed attempts do not expose partial text or tool calls to the CLI. This delays visible responses, and retries may incur duplicate inference costs.
- Pro requests, responses, streams, and errors pass through unchanged. Native advisor and experimental beta features are not forcibly disabled. Unsupported advisor, server tools, inline tools, signed thinking, and other native history bypass the external provider instead of being removed. Such a bypass is not counted as an external recovery check.
- Model access and usage credits remain subject to the Claude app and account plan. There is no separate Fable ban or credits-OFF requirement. External providers do not receive the Pro OAuth credential.

The CLI or HUD can still display Sonnet/Pro for a replacement response. That label and its cost estimate do not prove the inference provider or actual OpenRouter billing. Check the safe `route_selected` metadata: `replacement`, `native_feature`, `fallback`, or `circuit_open`.

## Local LLM configuration

Real Local inference requires a separately configured service and remains a separate validation step.

For a future local service, create `.env.local-llm` in the checkout root, or set the corresponding process environment variables:

```dotenv
LOCAL_LLM_URL=http://127.0.0.1:11434/v1/messages
LOCAL_LLM_MODEL=YOUR_INSTALLED_MODEL_ID
# Set LOCAL_LLM_API_KEY separately only if the local server requires authentication.
```

Local mode requires an HTTP `/v1/messages` endpoint on a literal loopback address (`127.0.0.1` or `::1`) and rejects redirects. It does not read the OpenRouter key. The server and installed model must be configured and validated separately.

## Validation and limitations

Personal machine configurations, account usage, billing records, and live session logs are not stored in this repository. Run synthetic tests without external model inference:

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p test_local_config.py -v
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
| `openrouterAdvisor.py` | Pro authentication checks, environment isolation, proxy lifecycle, isolated consultation tool, self-tests |
| `claudeRouteProxy.mjs` | Routing, response validation, retries, fallback, recovery |
| `tests/claudeRouteProxy.test.mjs` | Synthetic proxy regression tests |

The private `tests/probeClaudeRoute.py` used during development is not distributed here.

## License

The code and documentation in this repository are available under the [MIT License](LICENSE). Commercial use, modification, and redistribution are permitted subject to retaining the copyright and permission notice. See the [official MIT text](https://opensource.org/license/mit).

The reviewed source uses Node.js and Python standard libraries and invokes a separately installed Claude CLI. No bundled third-party packages or separate source-license notices were found in the review. MIT was selected as a simple permissive license for reusing this small standalone router. See the [scope, rationale, and Apache-2.0 comparison](LICENSE-REVIEW.md).

Claude Code, Node.js, Python, hosted services, and models are not distributed by this repository. Their own licenses and service terms continue to apply; this project's license does not grant service access or change usage limits and billing.
