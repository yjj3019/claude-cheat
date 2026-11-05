# Understanding connection checks and troubleshooting

This file carries the troubleshooting section moved out of [README.en.md](../README.en.md).
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
