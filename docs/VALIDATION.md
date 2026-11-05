# Validation and limitations

This file carries the validation section moved out of [README.en.md](../README.en.md).
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
node tests/bench_proxy.mjs --drift-json .bench\previous-result.json --drift-max-ratio 1.4   # workload p90 drift gate
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
| Circuit | At measurement time: 4 failures → Plan fallback, then external after recovery; provider failures now return errors |
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
