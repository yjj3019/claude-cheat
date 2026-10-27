// EXPERIMENTAL, opt-in: one real Claude Code print-mode turn through this proxy.
// node tests/cli_smoke.mjs [--prompt "Reply with exactly OK"]
// Makes real provider requests and may cost money. Not included in the default test suite.
// Live compatibility is unverified; use synthetic tests/cli_smoke.test.mjs for offline checks.
// Exit codes: 0 pass, 1 fail, 2 settings missing.
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { StringDecoder } from 'node:string_decoder';
import { listenSafe } from './listenSafe.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const OUTPUT_LIMIT = 1024 * 1024;

// The proxy's route-log mirror is selected at module load. Do not inherit a user's log
// destination into this separate diagnostic, and restore the caller's environment immediately.
const savedRouteLog = process.env.ROUTE_LOG_PATH;
delete process.env.ROUTE_LOG_PATH;
let createProxy, redactDiagnostic;
try { ({ createProxy, redactDiagnostic } = await import('../claudeRouteProxy.mjs')); }
finally { if (savedRouteLog !== undefined) process.env.ROUTE_LOG_PATH = savedRouteLog; }

// The settings live in openclaude.config.yaml (or the deprecated .env.openrouter).
function readSettings(provider) {
  const run = spawnSync(process.platform === 'win32' ? 'python' : 'python3',
    ['-c', 'import json, routeConfig; print(json.dumps(routeConfig.read_provider(__import__("sys").argv[1])))', provider],
    { cwd: ROOT, encoding: 'utf8', timeout: 10000 });
  try { return JSON.parse(run.stdout || '{}'); } catch { return {}; }
}

function runChild(command, args, env, timeoutMs) {
  return new Promise(resolveResult => {
    let child;
    try {
      child = spawn(command, args, { env, stdio: ['ignore', 'pipe', 'pipe'], shell: false,
        windowsHide: true });
    } catch (error) {
      resolveResult({ status: null, stdout: '', stderr: '', error, timedOut: false, outputExceeded: false });
      return;
    }
    let stdout = '', stderr = '', bytes = 0, error = null, timedOut = false, outputExceeded = false;
    const outDecoder = new StringDecoder('utf8'), errDecoder = new StringDecoder('utf8');
    let settled = false;
    const finish = status => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stdout += outDecoder.end();
      stderr += errDecoder.end();
      resolveResult({ status, stdout, stderr, error, timedOut, outputExceeded });
    };
    const stop = () => {
      // Never wait indefinitely for inherited pipe handles or a child that ignores SIGTERM.
      child.kill('SIGKILL');
      child.stdout.destroy();
      child.stderr.destroy();
      child.unref();
      finish(null);
    };
    const consume = target => chunk => {
      bytes += chunk.length;
      if (bytes > OUTPUT_LIMIT) { outputExceeded = true; stop(); return; }
      if (target === 'stdout') stdout += outDecoder.write(chunk);
      else stderr += errDecoder.write(chunk);
    };
    child.stdout.on('data', consume('stdout'));
    child.stderr.on('data', consume('stderr'));
    child.on('error', err => { error = err; });
    const timer = setTimeout(() => { timedOut = true; stop(); }, timeoutMs);
    child.on('close', finish);
  });
}

// Injected command/fetch are for synthetic tests only. This function never reads user settings.
export async function runSmoke({ key, model, command, args, env = process.env, fetchImpl,
  timeoutMs = 120000 }) {
  const nonce = randomBytes(24).toString('hex');
  const server = createProxy({ key, nonce, openrouterModel: model, metricsPath: null,
    ...(fetchImpl ? { fetchImpl } : {}) });
  try {
    await listenSafe(server);
    const base = `http://127.0.0.1:${server.address().port}`;
    const result = await runChild(command, args, { ...env, ANTHROPIC_BASE_URL: base,
      ANTHROPIC_AUTH_TOKEN: key, ANTHROPIC_CUSTOM_HEADERS: `X-Local-Route-Token: ${nonce}`,
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: '1', CLAUDE_CODE_DISABLE_ADVISOR_TOOL: '1' }, timeoutMs);
    let metrics = null;
    try {
      const response = await fetch(`${base}/api/metrics`, { headers: { 'x-local-route-token': nonce },
        signal: AbortSignal.timeout(5000) });
      if (response.ok) metrics = await response.json();
    } catch { /* Missing metrics is a failure, never a success. */ }
    let parsed = null;
    try { parsed = JSON.parse(result.stdout.trim().split('\n').at(-1)); } catch { /* not JSON */ }
    const reply = typeof parsed?.result === 'string' ? parsed.result : '';
    const validResult = parsed?.type === 'result' && parsed?.subtype === 'success' && parsed?.is_error === false;
    const scrub = value => redactDiagnostic(String(value), [key, nonce]);
    return { ok: result.status === 0 && !result.timedOut && !result.outputExceeded && validResult &&
        Boolean(reply.trim()) && (metrics?.requests ?? 0) > 0 && metrics?.last?.outcome === 'ok' &&
        ['external', 'plan'].includes(metrics?.last?.route) && metrics?.failed === 0 && metrics?.aborted === 0,
      exit: result.status, reply: scrub(reply).slice(0, 120), requests: metrics?.requests ?? 0,
      // A route may be selected even if its final reply failed; preserve that evidence.
      completed: metrics?.ok ?? 0, failed: metrics?.failed ?? 0,
      routes: metrics?.routes ?? null, firstByteMs: metrics?.latencyMs?.ttfb?.p50 ?? null,
      timedOut: result.timedOut, outputExceeded: result.outputExceeded,
      stderr: result.status === 0 ? undefined : scrub(result.stderr).slice(0, 300),
      spawnError: result.error ? scrub(result.error.message) : undefined };
  } finally {
    if (server.listening) {
      const closed = new Promise(resolveClose => server.close(resolveClose));
      server.closeAllConnections();
      await closed;
    }
  }
}

async function main() {
  const args = process.argv.slice(2);
  const promptIndex = args.indexOf('--prompt');
  const prompt = promptIndex >= 0 ? args[promptIndex + 1] : 'Reply with exactly OK';
  if (typeof prompt !== 'string' || !prompt.trim()) {
    console.error(JSON.stringify({ ok: false, reason: 'invalid_prompt' }));
    return 2;
  }
  const config = readSettings('openrouter');
  const key = config.ANTHROPIC_AUTH_TOKEN, model = config.OPENROUTER_MODEL;
  if (!key || !model) {
    console.error(JSON.stringify({ ok: false, reason: 'configuration_missing' }));
    return 2;
  }
  // Windows .cmd files need a shell; do not enable shell or reinterpret a shim here.
  const cli = process.platform === 'win32' && process.env.USERPROFILE
    ? `${process.env.USERPROFILE}\\.local\\bin\\claude.exe` : null;
  const command = cli && existsSync(cli) ? cli : 'claude';
  const report = await runSmoke({ key, model, command, args: ['-p', prompt, '--output-format', 'json'] });
  console.log(JSON.stringify(report));
  return report.ok ? 0 : 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); }
  catch { console.error(JSON.stringify({ ok: false, reason: 'smoke_failed' })); process.exitCode = 1; }
}
