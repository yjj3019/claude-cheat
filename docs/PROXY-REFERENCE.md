# Proxy reference

This file carries the detailed sections moved out of [README.en.md](../README.en.md): routing, retries and recovery, the live dashboard, usage/route verification, and the optional first-token optimizations. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) and [VALIDATION.md](VALIDATION.md) for the other split-out guides.
## Routing, retries, and recovery

| Request | Route |
| --- | --- |
| Compatible Sonnet request | Configured OpenRouter model or local model |
| Unsupported native Sonnet feature or conversation history | Original Plan Sonnet request, unchanged |
| Opus, Haiku, Fable | Existing Plan OAuth path |
| Local fails all attempts, is busy, or is in cooldown | Local error; no Plan failure fallback |
| OpenRouter fails all attempts, or CLI backend fails its single attempt | Provider error; no Plan failure fallback |

- OpenRouter uses the required `OPENROUTER_MODEL` setting, with no default model.
- OpenRouter and Local are selected at launch; the router does not switch between them automatically. Select Sonnet in `/model` for eligible replacement requests.
- HTTP providers receive the initial attempt plus three retries. CLI backends receive one attempt with their configured deadline. Ordinary HTTP attempts have a 30-second header/idle timeout and a 300-second total ceiling while data keeps arriving. Local-only and compatibility modes retain their existing fixed deadlines.
- A `tool_use` stop without a tool call, or an `end_turn` with a tool call, is rejected. Truncation stops such as `max_tokens` remain distinct. OpenRouter usage counts must be nonnegative safe integers.
- OpenRouter keeps its one-second default retry delay. Local congestion responses (429/503/529) and timeouts use exponential backoff based on 1/2/4 seconds plus small positive jitter, bounded by `ROUTE_RETRY_MAX_WAIT_MS` (10 seconds by default). Empty replies are not assumed to mean congestion. Plan response time and the CLI's own retries add to the wait; cancellation stops retries and fallback.
- `Retry-After` accepts seconds or HTTP-date, bounded at ten minutes. A Local hint longer than the internal wait cap ends retries rather than retrying early, and also extends the circuit recovery wait. Local returns an error without Plan fallback.
- The ordinary Local fast path keeps the original conversation. A compatibility copy is built only for potentially convertible native-feature exclusions, avoiding discarded history/tool copies. This reduces proxy work without changing model selection, token budgets or Plan eligibility; it does not repair empty server responses or speed up inference.
- **Shared Local admission:** each proxy allows two active Local turns, eight queued turns, and at most 60 seconds in the queue. An active turn retains its slot across retries. Cancellation removes a queued turn. Queue overflow or timeout returns `local_busy` and 503 without Plan fallback. Admission rejection does not count as an external attempt or open the circuit.
- The dashboard shows Local active/queued turns and cumulative admission rejections. The existing authenticated `/api/metrics` exposes `localAdmission`: `active`, `queued`, `rejected`, `concurrency`, `queueLimit`, `queueWaitMs`. Rejections accumulate only during the current proxy process.
- This limits only traffic from this proxy, not other users or server-wide GPU load. Long parallel tasks can exceed the queue wait and return an error. Conversation, tools and output token budgets are not silently shortened. Restart the proxy to load the changed source.
- The circuit breaker distinguishes causes: transport failures, timeouts, 5xx, 408 and unusable 2xx answers open it as provider faults; 401/402/403 keep their provider-wide cooldown; a 429 uses the parsed `Retry-After` (bounded at ten minutes) as its cooldown. 400/404/405/413/415/422 stop retries without opening the breaker; other request-level failures retain bounded retries and do not open it. 408 and 429 retain their separate transient and rate-limit policies. Exactly one half-open probe is admitted, and late completions from older generations cannot overwrite the newer circuit state.
- **Low-latency TCP posture:** server sockets disable Nagle (`setNoDelay`) and enable keep-alive (initial 15s), and the request-receive ceiling sits below the turn ceiling. At startup, the external and Plan origins are pre-warmed once with a background HEAD (no credentials, failures ignored, `ROUTE_PREWARM=0` disables it). The bench harness includes a **p95 drift gate**: `--drift-json <previous result>` plus `--drift-max-ratio` (default 1.4) fail the run when a workload's p90 drifts past the baseline.
- **HTTP/2 upstream (`ROUTE_UPSTREAM_H2=1`):** the default upstream client becomes a `node:http2` client with one multiplexed session per origin (h2 for https, h2c for plaintext), so concurrent turns share a connection without HTTP/1.1 head-of-line blocking. Redirects fail closed, and a failed session is evicted so the next call reconnects. Stream cadence is observable as `latencyMs.chunkCadenceMs` (count/p50/p95/max) in `/api/metrics`.
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
| `ROUTE_STREAM_PASSTHROUGH` | off | `1` forwards validated frames immediately (full); `2` only on verifiable paths (forceJson validation excluded); `0` explicit off. Default remains buffered |
| `ROUTE_COOLDOWN_MS` | `30000` | Circuit cooldown (0–600000); a parsed `Retry-After` from a 429 takes precedence |
| `ROUTE_RETRY_BUDGET_MS` | `60000` | Aggregate retry-wait budget per request (0–600000); `0` disables, exhaustion is observable via `retryBudgetExceeded` in `/api/metrics` |
| `ROUTE_PREWARM` | on | `0` disables the one-time upstream origin pre-warm (HEAD) at startup |
| `ROUTE_UPSTREAM_H2` | off | `1` switches the upstream client to per-origin `node:http2` sessions (multiplexing; h2 over https, h2c over plaintext) |
| `ROUTE_STABILIZE_PROMPT_CACHE` | off | `1` peels the trailing `<total_tokens>…` footer from system text before the external request to keep the prefix cache (opt-in) |

Routing metadata uses `route_selected` with provider `openrouter`, `local` or `plan`. Reason `replacement` identifies a validated external response; `native_feature` identifies a native-feature bypass; provider failures and cooldowns return errors without switching to Plan. `external_attempt_failed` records a failed attempt, and `circuit_open` / `circuit_recovered` report circuit transitions. These logs omit keys and request bodies; requests sent to external services may still contain conversation, code, tool definitions and results.

For `native_feature`, `detail` reports the first exclusion: `token_count`, `plan_model`, `unsupported_beta`, `unsupported_request_field`, `unsupported_message_shape`, `unsupported_thinking_config`, `unsupported_output_config`, `unsupported_tool_choice`, `unsupported_content`, `unsupported_content_block`, `unsupported_content_field`, `unsupported_tool_caller`, or `unsupported_tool_definition`. Exclusion labels are fixed; user text, tool names, signatures and credentials are never logged. For `unsupported_beta`, `beta_features` lists up to 16 date-suffixed feature identifiers and `unsupported_beta_count` gives the total. Nonstandard or credential-like entries are redacted; raw header strings are not logged. Restart the launcher to enable new diagnostics; old logs cannot reveal these details.

Router logs (`route_selected` and the other events) are hidden by default and printed to stderr only with `-Debug` (or `python openrouterAdvisor.py --debug`). Startup errors and `provider_connection_unavailable` still print without it. Use `-Debug` to verify routing.

The CLI or HUD can still display Sonnet/Plan for a replacement response. That label and its cost estimate do not prove the inference provider or actual OpenRouter billing. Check the safe `route_selected` metadata: `replacement`, `native_feature`, `fallback`, or `circuit_open`.

## Live dashboard

The dashboard and settings share consistent colors, cards and controls with improved secondary-text contrast. On wide screens the dashboard places key metrics alongside the request flow, with four trend panels below. Runtime diagnostics and activity records expand on demand; refreshes preserve disclosure state and summary keyboard focus. Narrow screens stack the panels. The layout uses existing observed data and polling intervals, with no extra inference or polling.

```powershell
.\OpenClaudeDashboard.ps1
```

A **loopback dashboard** bound to `127.0.0.1`. `GET` state reads are read-only; only authenticated `POST` actions (settings save, connection tests, black-box recording) mutate state. The write token is never embedded in the page: it is handed out only through the `write_url` printed at startup (`http://127.0.0.1:8787/#token=…`), read from the fragment, stripped from the address bar, and then kept in that tab's `sessionStorage` so refreshes and in-page navigation keep working (closing the tab drops it). The token is also kept in `.dashboard.token` (a same-user file, Git-ignored) so a dashboard restart does **not** invalidate tabs that are already open; delete that file or set `DASHBOARD_TOKEN` to rotate it. A page opened without the token stays read-only and shows a banner. The port is **fixed at 8787**; an older dashboard instance is stopped automatically so the same port is reused. If another program holds that port the server fails with a message instead of silently moving; pass `--allow-port-fallback` to pick a free port instead.

Stopping a previous instance (including `python routeDashboard.py --stop`) happens **only when both the pid and the process creation time stored in `.dashboard.pid` match**. A recycled pid, our own or an ancestor process, or a legacy record without identity is refused with one line (`identity_mismatch` / `own_tree_pid` / `identity_unverifiable`) and the stale record is overwritten on the next start. Right after an upgrade, stop an older dashboard manually once (`Ctrl+C`) before starting. Non-Windows has no creation-time primitive, so the documented behavior is to refuse rather than signal a bare pid.

The refresh interval follows the situation: two seconds while routing is active, ten seconds while idle, fifteen when the tab is hidden. The header shows a one-line summary (`오늘 43.8M토큰 · $1.42 · 남은 한도 $48.25 · 우선 확인: …`).

**Recent request trends** use up to 60 minutes of in-memory, per-minute completion observations: counts, average response time, failure rate and reported output throughput. Missing intervals remain disconnected. Failure rate excludes cancellations; average latency includes them. The history starts with the new proxy and resets on restart.

The **request flow** shows Claude Code → Claude-Cheat proxy → the selected model service or Plan, using an SVG path and a central processing bar. In-flight requests remain visible as a count and status; a new request or an observed batch briefly activates the bar. Local slot occupancy includes retry waits. Particle density follows reported input/output token increments from completed external responses, capped at 24 per direction. The view adds no inference calls or polling.

The flow card separately shows externally routed successful responses observed between refreshes and cumulative proxy failures. A brief warm highlight indicates at least 10,000 reported input or output tokens in one refresh interval; it is not a temperature or overheating measurement. Partial coverage and unreported values are labeled; this is not a trace of individual request timing.

The first snapshot establishes a baseline. No completed response is shown separately from unreported usage; tokens can remain unavailable while work is active. Plan, failed, cancelled and usage-unreported replies are not estimated. Hidden/offscreen views suppress particles. Animation is enabled by default and the flow card offers a page-session-only toggle without changing system settings. This is observed usage visualization, not live token streaming or per-request tracing. The header and proxy box share the same observed status; the usage page's fetch time is not the background collection completion time.

Observed input and output totals and reporting sample counts come from successful responses of the selected external provider during the refresh interval. Bars cap at 10,000 tokens; read the numeric value for exact totals. Input excludes separately reported cache tokens. Plan, failures, cancellations and responses without usage are excluded; unknown, awaiting measurement and a reported zero remain distinct. Existing snapshots are reused without extra inference or polling.

The upper-right status indicator uses both color and text: red for Plan, green for external models, brown/amber for active warnings, and a neutral indication when idle or unknown.

The performance card reuses proxy counters and sends no additional inference requests. It reports **per-connection decisions for the last 60 minutes (Local, OpenRouter, CLI and Plan: requests, failed attempts, last model)** in addition to external/Plan route counts. The failure rate subtracts cancellations (`aborted`) from the proxy's `failed` counter, matching the documented completed-response definition. Observed output tokens/second is the sum of reported output tokens divided by total request time for eligible successful responses. It includes queueing, network and retries; multiple models may share one provider bucket. Reported usage from fully validated streams is included; Plan, cancellation, failures and unreported usage are excluded. GPU utilization and pure generation speed are unavailable. New token counters begin after restarting the proxy with the updated code; older omissions are not recovered.

**Every connection point is monitored the same way.** Local LLM, OpenRouter and CLI all count tokens from session records and route decisions/failed attempts from the route log. Selecting CLI no longer hides the usage card; unreported CLI tokens are labeled unreported with their provenance instead of being estimated as zero. Route-log events are scoped to the launcher instance, and a rotated log keeps the same session through a `--- proxy start … continued=1 … ---` marker.

**The monitor watches itself.** A server snapshot older than 30 seconds, or a failed refresh, raises `snapshot_stale` / `snapshot_refresh_failed` banners and makes `/api/status` exit with code 3. Failed pricing lookups retry after a minute (successful rates stay cached for an hour), account-limit caches are keyed by the API key, redirects are never followed (so a provider key cannot leak to another origin), and the Local models URL only receives the local key when it has the same origin as the inference URL. When the routing event cap (2,000) is reached, the event-window field says only part of the window was aggregated.


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

**Proactive reactions.** The page does not wait for you to look: every snapshot refresh recomputes the conditions below on the server and surfaces them as banners, a tab-title badge and optional desktop notifications.

| Reaction | Trigger | Behavior |
| --- | --- | --- |
| Default service unconfigured | the routing default points at a service whose required values are missing, or routing cannot be read | critical banner + `⛔` tab title + settings/restart hint |
| Service choice required | two or more services are configured while the default is `auto` | banner + `⚠` tab title |
| Session or CLI settings differ | the running session's provider, or its CLI kind/model, differs from the saved values | banner + copyable `.\OpenClaude.ps1` |
| Plan-only features keep routing to Plan | an external service is configured, but this session has 0 external attempts and every decision is `native_feature` Plan | inspect the native-feature reason; `-OpenRouterCompat` also blocks native Plan exceptions |
| Connection dropped | `/api/snapshot` fails | exponential backoff (1, 2, 4, 8, 16s) and a recovery toast |
| Tab title badge | any warning/critical item exists | `⛔ n · …` or `⚠ n · …` |
| Desktop notification | new critical item while notifications are enabled | one per cause (re-notifies after the cause clears and returns) |

The server-side checks are read-only: they never restart a session, write settings or call a provider. Banners and the title badge work even when notification permission is denied.

**Process health card.** The dashboard shows what the running proxy reports about itself: **RSS and heap usage**, **event-loop delay p95/max** and the process PID. Event-loop delay measures how late timers ran because the proxy's own work — body parsing, header/field validation, logging, metrics persistence — blocked the loop; it is separate from upstream latency. When a delay of **100 ms or more** occurs, the card lists the **latest three with timestamps**, so a spike can be matched to a snapshot/GC moment. An event-loop p95 of **250 ms or more** raises a proactive warning, and **RSS of 1.5 GiB or more** raises a memory warning. The route log writes lines in **asynchronous 10 ms batches with a bounded queue** instead of one synchronous append per line, and the card shows its write max, queue, and drop counters. Snapshot polling uses **ETag/304**, so unchanged data skips body transfer and DOM re-render (`x-dashboard-cache: HIT/MISS`). The figures require the same session token as `/api/metrics`, and the `.proxy-metrics.json` snapshot is written **asynchronously** so persistence never blocks request handling.

**Code changes apply automatically.** When `routeDashboard.py`, `routeConfig.py` or `localProcessTree.py` changes, the running dashboard restarts itself and serves the new page. If the live proxy was started from older code (`proxy_hash` mismatch), the dashboard requests an **automatic takeover on the same port and token while no request is in flight**, so the Claude Code session keeps working on the new proxy. Attempts and failures are recorded in `.auto-update.log`. Without a running dashboard, or in a mode that cannot take over, the change applies at the next session start.

**Multiple sessions can run at once.** The dashboard scans `.usage-stats/instances/*/handoff.json`, reads every live session's own metrics, and shows the busiest one when no choice is saved. Restarting another session does not reset the view; if the shown session disappears, the dashboard switches to the busiest remaining session instead of a new empty one. With two or more live sessions the header exposes a **session picker**, and the choice is stored in `.dashboard.session` across dashboard restarts. The restarted proxy's own time series still begins at its new proxy start.

**Layout is fixed independently of content.** From 1400 px wide, the **overview** and **request flow** cards keep a fixed 560 px height; long model/state text truncates to one line, metric values ellipsize, and insights clamp to three lines. Changing token counts or sentences no longer pushes the charts and diagnostics up or down. Narrow screens keep automatic height for readability.

**What needs a person comes first** when there are open alerts. Alerts can be **acknowledged, snoozed for an hour, resolved or reopened**, with the state persisted in `.dashboard-attention.json`. Acknowledged, snoozed and resolved items appear under **Usage and activity history**. Per-session **retry health** (rate limit, retry count, Retry-After) separates upstream throttling from local load.

The diagnostics API aggregates timeout kinds and observed response stages in `metrics.externalTimeouts`. `routingTimingMs` includes `externalHeaders`, `externalFirstBody`, and `externalTimeout` distributions over at most 512 recent samples. A timeout after body progress can also reflect a slow client, so it does not by itself identify the upstream as the cause. New metrics start collecting after the proxy is restarted.

`externalTimeouts.localAttempts` counts finished Local attempts (including client cancellations) and timeouts by input request size (up to 64 KiB, up to 256 KiB, or larger) and this proxy's occupied Local slots at attempt start (one or at least two). Retries are separate attempts; occupied slots include retry waits. These counts do not measure total server load or tokens.

**Per-session monitoring** compares every live session: requests and requests/minute, external/Plan routes, failures/cancellations, in-flight/active/queued work, external p95, output tokens/second, input/output tokens, RSS and event-loop p95, and the last activity. A **load** badge appears when work is in flight/queued or event-loop p95 reaches 250 ms; the **show** button switches the detailed cards to that session.

**Overview** now puts the status LED, current route and model, a 60-minute external/Plan split bar, four measured tiles (requests, failure rate, observed speed, external p95), and the shown session's project/branch/port with today's tokens, cost and remaining credit on one card. Per-session rows show the **project, branch and cwd**; new sessions record the exact checkout in their handoff, while sessions without that metadata are matched to a Claude transcript and marked **inferred**.

| Panel | Content |
| --- | --- |
| Now | **OpenRouter, Local LLM or CLI serving / Plan bypass / idle**, the **last request's actual routing model**, age, reason |
| Performance | Live proxy session requests, failure rate, average requests per minute, external and Plan routes, response and first-byte latency. Observed output token throughput is shown only when response usage is reported. |
| Process health | Proxy RSS/heap, **event-loop delay p95/max**, PID — separates the proxy's own bottlenecks from upstream latency. |
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

The settings page can show and restore the history of supported settings saved through the dashboard. Credentials, server addresses, executable paths, and security settings are excluded. If the settings file changes after the history was loaded, refresh it before restoring. Resetting a service removes its history. Direct file edits are not recorded.

Settings live on a **separate page** (`http://127.0.0.1:8787/settings`) reached from the top navigation; both pages share one server and the write token from `write_url`.

| Behaviour | Detail |
| --- | --- |
| Save | Only the managed keys change; other comments, sections, line endings (CRLF) and a byte-order mark stay, and the previous file is kept **byte-for-byte** as `openclaude.config.yaml.bak`. When a service has no section yet and its deprecated `.env` still holds values, the new section is seeded with them so a stored credential is not silently shadowed. Concurrent saves are serialised by a lock file; an unreadable file (encoding or syntax error) is never overwritten and reports a code and line number |
| Secrets | Never sent back to the browser (`sk-or-…cd0` style). Leaving the field empty keeps the stored value; only a new value replaces it |
| Server addresses | The Messages, HTTP-exception and model-list URLs are masked like keys (`http://12•••••.1:11434/v1/messages`: only the first and last two host characters, `user:pass@` and queries hidden; a non-standard custom path becomes `/•••`). The original is never sent to the browser; empty keeps it |
| Clear | Optional addresses (HTTP exception, model list) are erased with the **Clear** button (the `clear` list of `POST /api/config`). Required fields and keys cannot be cleared |
| Validation | Model ID shape, key shape and Local URL rules (loopback, HTTPS, same origin) are checked before writing; a failure leaves the file untouched |
| Protection | Both reading settings (`GET /api/config`) and saving them require the write token delivered only via `write_url`, never embedded in the page; POST also requires a same-origin request, so other sites cannot call it |
| Tests | **Model check** reads the model list only; **inference test** sends one request with a 16-token OpenRouter or 512-token Local output limit (may incur costs or use server resources). Local verifies the selected model ID, exact `OK` reply, and normal completion. Unsaved form values only contribute the model ID and timeout; addresses, API keys and CLI binaries always come from saved settings, and concurrent tests return `test_busy` |
| Model picker | **목록** opens a searchable list with context length and price and fills the field. `GET /api/models?provider=openrouter\|local` (session token required, 10 minute cache) |
| When it applies | Saved settings take effect in the **next** session; restart a running one |

### Current model checks and duplicate selection

Distinguish the configured model (settings for the next session), the connection-test model (the response model ID is checked against the requested ID), and the last routed model (recorded after response validation). An ordinary session can still start after a failed connection check, with a warning; Local failures do not fall back to Plan. Model-list metadata checks do not prove inference works.

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

The CLI card's **CLI 자동 검색** (auto-detect) button re-runs `GET /api/cli` and fills **CLI kind** and, when empty, **executable path** with the selected (or first detected) supported backend. Detection only checks the supported backends (Claude Code, Codex, Grok) in PATH and their standard install locations — it does not search PATH for arbitrary commands or add new backends. Filled values are not saved automatically; press **save**.

The Codex backend runs in an isolated read-only temporary workspace. That restriction does not determine the front-end Claude Code tools' access to the project. The backend is instructed to follow actual host tool permissions and results; this does not bypass a genuine access restriction.

Local LLM and OpenRouter requests also receive guidance to judge tool access from the host's stated policies and actual tool results, respecting explicit read-only restrictions. This guidance is not inserted into direct Sonnet or Plan fallback requests.

For Codex, the CLI model picker reads its local model cache without running inference. The cache does not guarantee current account access or backend compatibility. If unavailable, enter a model ID manually. Claude Code and Grok currently have no verified catalog integration and require manual entry; the Grok backend is not yet enabled.

CLI monitoring reports proxy requests in flight, HTTP 429 responses, failure categories, and the last 60 minutes of reported input/output tokens and usage coverage. The CLI adapter has a concurrency limit of four and rejects excess requests without a queue. Proxy in-flight requests are not the actual CLI process count or GPU load. Coverage uses successful CLI responses as its denominator; unreported usage is not treated as zero. HTTP 4xx can include CLI backend validation errors. Restart the session to collect the new metrics; older proxies leave them unreported.

CLI process time measures just before process start through observed termination, including startup, CLI preparation and communication; it is not pure model inference time. It retains up to 512 validated successful response samples and excludes failures, cancellations and unreported timings. An unreported actual model is never inferred from the requested model.

Run `python tests/live_cli_smoke.py --run` explicitly to make one real Codex inference through the authenticated local adapter, using the saved CLI configuration and login. Usage may be incurred. The isolated test checks a synthetic tool proposal and executes no tool. It does not verify the proxy, front-end Claude Code, Agent Teams or SSH/MCP combinations.

Saved settings apply to the next session, not to requests already running. Finish work and subagents, exit Claude Code normally, restart, and check the active session and recent route in the dashboard. Plan is currently a fallback route, not a separately selectable default; there is no automatic Local-to-CLI switching. A started response is not replayed on another route. Saved settings do not prove connectivity: inference tests make real calls and may incur usage. CLI backends require their own login and a verified version; Grok is not currently enabled. Verify the model and executable after changing CLI kind.

Verified versions are Claude Code **2.1.312** and Codex **0.165.0**. Other versions fail closed until validated. Grok is detected and selectable, but backend execution is currently refused with `backend_version_unverified` because its tool isolation has not been verified. Installed does not mean connected or supported.

Backend processes run without a shell in an empty temporary directory, with isolated configuration and a filtered environment. Prompts and responses stay in memory. Responses must reference advertised tools and valid argument JSON objects, but the adapter **does not validate arguments against the advertised `input_schema`**. The request's `max_tokens` is included in the prompt, but is **not enforced as an exact generation token limit**; do not use the CLI backend when a strict per-request output limit is required. Cancellation cleans up owned children. Each request makes one backend attempt and reports failure without Plan fallback, with a configurable 10–120 second deadline (default 60) and at most four concurrent backend requests.

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

### Version and update notice

The dashboard header and the launcher's first line name the running build. The product version comes from `VERSION` (for example `1.0.0`) and the revision from git, so you see **`v1.0.0 (4148f2f)`**; uncommitted changes add a `*`.

An interactive `OpenClaude` launch checks the current `origin` tracking branch once. It applies a fast-forward only when the worktree is clean, no routed session is active, and the remote has a higher stable three-part `VERSION`. Local changes, untracked files, conflicts, in-progress Git operations, and incomparable versions cause the update to be skipped. A network failure keeps the current version; an incomplete update stops the launch for inspection.

An interactive launch also starts the dashboard in the background or reuses its verified instance, then opens the monitoring page. An unverified port occupant is left alone. Help, version, setup, diagnostics, takeover, and print commands do not start the dashboard automatically.

If an interrupted update leaves `.openclaude-update-incomplete`, automatic restarts and ordinary launches stop. Inspect and recover the checkout before removing that marker and starting again.

```powershell
python routeDashboard.py --version           # summary plus a live remote check
python routeDashboard.py --version --json    # full JSON document
python routeDashboard.py --version --offline # skip the network
python openrouterAdvisor.py --version        # same check from the launcher
.\OpenClaude.ps1 -Version                    # identical
```

An explicit `--version` check reports `current`, `behind`, `ahead`, `diverged`, `different`, `unknown`, or `disabled`. This explicit check is read-only.

- The dashboard does not periodically query the remote. Its badge shows the local version; clicking it refreshes local information.
- `ROUTE_UPDATE_CHECK=0` disables automatic updates. Remote and branch environment variables still apply to explicit `--version` checks; automatic updates are restricted to the current `origin` tracking branch.
- `VERSION` is the release name: bump it when you release, and the dashboard, launcher and `--version` all report the same value.

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
| `session` | Authenticated proxy presence, its loaded primary model, alternate and provider-failure Plan fallback policy; native exceptions are separate |
| `last_route` | Last routed model, timestamp, current-session status and bounded `fallback_failure` category when known |
| `capabilities` | Model features remain `unknown`; `tool_roundtrip_evidence` reports only a prior explicit dashboard backend probe, not a general capability guarantee |
| `metrics` | Current proxy request/failure/retry/circuit counters; historical files are not treated as current |
| `snapshot` | Availability, age since last successful refresh and refresh-error presence |
| `next_check` | A separate authenticated metadata-check request; not executed automatically |

Useful check codes include `configuration_incomplete`, `routing_configuration_invalid`, `provider_choice_required`, `session_not_running`, `proxy_model_matches_settings`, `model_settings_changed`, `proxy_provider_mismatch`, `proxy_model_unreported`, `proxy_model_redacted`, `upstream_not_checked`, `snapshot_refresh_failed`, `snapshot_stale` (over 30 seconds) and `request_failures_recorded`.

A successfully generated report returns **HTTP 200 even when a component has an error**; inspect its body rather than assuming HTTP 200 means inference works. An invalid Host returns 403. A report-generation failure returns 500 with the fixed code `diagnostics_failed`, without the exception text. No live session is a degraded/waiting state rather than proof that the dashboard is broken. Older proxies may not report a loaded model: `session.loaded_model` remains `null` instead of being inferred from settings. `session.plan_fallback` describes routing policy, not a guarantee after streaming output has already started.

The existing `POST /api/test` accepts `{"provider":"local","mode":"metadata"}` for a model-list-only check and requires the page's `x-dashboard-token`. Actual inference is a separate `mode:"inference"` request and may cost money. A healthy local proxy and matching model metadata do not prove upstream inference succeeds.

The Settings **tool round-trip test** and authenticated `POST /api/test` with `{"provider":"local","mode":"tool_roundtrip"}` use the saved Local, OpenRouter or CLI settings. No `values` are accepted. Up to two real inference calls are made without executing tools; CLI token use depends on its backend. Each dashboard process runs one such test at a time (`probe_busy` otherwise); separate command-line probes are not covered by that limit. The page separates tool success, reported model equality and token reporting. A bounded summary is kept in dashboard-process memory only, for up to 24 hours, and is lost on restart. Diagnostics compares saved settings by value; changing and then restoring them can match again. The result does not track server, CLI binary, or authentication changes and does not prove current or end-to-end capability. It tests the saved backend at the start, not the currently running Claude Code session. Closing the browser does not guarantee cancellation; per-socket timeouts do not impose an overall wall-clock limit.

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

By default the proxy validates the **whole** reply before sending it, so a streamed answer appears only when generation finishes. Provider failures return an error without Plan fallback.

```powershell
.\OpenClaude.ps1 -Stream        # forward frames as they validate (lower TTFB)
```

| Mode | First token (TTFB) | Behaviour on failure |
| --- | --- | --- |
| Default (buffered) | when the reply completes (measured 782ms / 782ms) | retries, then the selected provider's error |
| `-Stream` (opt-in) | first valid content (measured **339ms** / 772ms total) | before the first token: same provider policy; **after** it: the stream ends with an error |

Forwarding starts only after the first valid content; empty replies, thinking-only replies and broken tool JSON follow the same provider-specific failure policy. Environment form: `$env:ROUTE_STREAM_PASSTHROUGH = '1'; OpenClaude.ps1`.
