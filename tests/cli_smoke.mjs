// EXPERIMENTAL, opt-in: one real Claude Code CLI print-mode turn through this proxy.
//
//   node tests/cli_smoke.mjs [--prompt "Reply with exactly OK"]
//
// Status: not passing in the author's environment. The CLI alone finishes a print-mode
// turn (exit 0, ~48s), but with ANTHROPIC_BASE_URL pointing at this proxy it blocks before
// sending any request (proxy metrics stay at 0), so the launcher's settings/config-dir
// plumbing is not yet reproduced here. Kept as a starting point; do not add it to CI.
//
// Exit codes: 0 pass, 1 fail, 2 the CLI or settings are missing.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const args = process.argv.slice(2);
const promptIndex = args.indexOf('--prompt');
const prompt = promptIndex >= 0 ? args[promptIndex + 1] : 'Reply with exactly OK';

// The settings live in openclaude.config.yaml (or the deprecated .env.openrouter). Ask the one
// Python reader instead of keeping a second parser here.
function readSettings(provider) {
  const run = spawnSync(process.platform === 'win32' ? 'python' : 'python3',
    ['-c', 'import json, routeConfig; print(json.dumps(routeConfig.read_provider(__import__("sys").argv[1])))', provider],
    { cwd: ROOT, encoding: 'utf8' });
  try { return JSON.parse(run.stdout || '{}'); } catch { return {}; }
}

const config = readSettings('openrouter');
const key = config.ANTHROPIC_AUTH_TOKEN;
const model = config.OPENROUTER_MODEL;
if (!key || !model) {
  console.error(JSON.stringify({ ok: false, reason: 'configuration_missing' }));
  process.exit(2);
}

const cli = process.platform === 'win32'
  ? ['claude.cmd', 'claude.exe'].map(name => `${process.env.USERPROFILE}\\.local\\bin\\${name}`).find(existsSync)
  : null;
const command = cli || 'claude';

const nonce = 'cli-smoke-' + Math.random().toString(36).slice(2, 10);
const metricsPath = join(mkdtempSync(join(tmpdir(), 'cli-smoke-')), 'metrics.json');
const server = createProxy({ key, nonce, openrouterModel: model, metricsPath });
await listenSafe(server);
const port = server.address().port;

const result = spawnSync(command, ['-p', prompt, '--output-format', 'json'], {
  // No shell and a closed stdin: print mode must not wait for input.
  encoding: 'utf8', timeout: 120000, input: '', shell: false,
  env: {
    ...process.env,
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${port}`,
    // A proxy bearer token takes precedence over the saved OAuth flow, which would try to
    // reach the real API first and never come back through this proxy.
    ANTHROPIC_AUTH_TOKEN: key,
    ANTHROPIC_CUSTOM_HEADERS: `X-Local-Route-Token: ${nonce}`,
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: '1',
    CLAUDE_CODE_DISABLE_ADVISOR_TOOL: '1',
  },
});

let metrics = null;
try { metrics = JSON.parse(readFileSync(metricsPath, 'utf8')); } catch { /* nothing recorded */ }
server.closeAllConnections();
server.close();

let parsed = null;
try { parsed = JSON.parse(result.stdout.trim().split('\n').at(-1)); } catch { /* not json */ }
const reply = parsed?.result || parsed?.message || '';
const report = {
  ok: result.status === 0 && Boolean(String(reply).trim()) &&
      (metrics?.requests ?? 0) > 0,
  exit: result.status,
  reply: String(reply).slice(0, 120),
  requests: metrics?.requests ?? 0,
  routes: metrics?.routes ?? null,
  firstByteMs: metrics?.latencyMs?.ttfb?.p50 ?? null,
  stderr: result.status === 0 ? undefined : String(result.stderr || '').slice(0, 300),
  spawnError: result.error?.message,
};
console.log(JSON.stringify(report));
process.exit(report.ok ? 0 : 1);
