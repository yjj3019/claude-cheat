// Child process for tests/bench_proxy.mjs.
//
// The harness deliberately separates the proxy from the load generator: this process
// hosts createProxy with a zero-latency fake upstream and does nothing else, while the
// parent process sends all HTTP load. Memory growth observed here is therefore
// attributable to the proxy code path, not to the benchmark client.
//
// Protocol (stdout, one JSON object per line):
//   {"type":"ready","port":PORT,...}                         first line, after listen
//   {"type":"sample","id":N,"memory":{...}}                  reply to {"cmd":"sample"}
//   {"type":"report","id":N,"upstream":{...},"memory":{...}} reply to {"cmd":"report"}
//   {"type":"closing","id":N}                                reply to {"cmd":"close"}
// Every reply echoes the request id so the parent can correlate concurrent commands.
// Diagnostics from the proxy are written to stderr; the parent only keeps the tail for
// error reporting. Per-request success logging is suppressed: it is stderr I/O, not the
// request path, and would otherwise dominate small-payload measurements.
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const writeStderr = console.error.bind(console);
const EXTERNAL_URL = 'https://openrouter.ai/api/v1/messages';
const BASELINE_ENV = ['ROUTE_LOG_PATH', 'ROUTE_METRICS_PATH', 'ROUTE_STREAM_PASSTHROUGH', 'ROUTE_HANDOVER_TIMEOUT_MS'];
const MODES = new Set(['ok', 'fail-first', 'fail-twice', 'fail-always']);

function usage(message) {
  writeStderr(`bench_proxy_worker: ${message}`);
  writeStderr('usage: node --expose-gc tests/bench_proxy_worker.mjs [--mode ok|fail-first|fail-twice|fail-always] ' +
    '[--stream] [--stream-passthrough] [--upstream-model MODEL] [--nonce NONCE] [--key KEY] ' +
    '[--retry-delay-ms N] [--cooldown-ms N] [--attempt-timeout-ms N]');
  process.exit(2);
}

function parseNumber(raw, flag) {
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0) usage(`${flag} must be a non-negative number`);
  return value;
}

function parseArgs(argv) {
  const options = { mode: 'ok', stream: false, streamPassthrough: false, upstreamModel: 'bench/model',
    nonce: 'bench-nonce', key: 'sk-or-bench', retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 5000 };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    const value = () => {
      if (index + 1 >= argv.length) usage(`missing value for ${arg}`);
      return argv[++index];
    };
    switch (arg) {
      case '--mode': {
        const mode = value();
        if (!MODES.has(mode)) usage(`unknown mode: ${mode}`);
        options.mode = mode;
        break;
      }
      case '--stream': options.stream = true; break;
      case '--stream-passthrough': options.streamPassthrough = true; break;
      case '--upstream-model': options.upstreamModel = value(); break;
      case '--nonce': options.nonce = value(); break;
      case '--key': options.key = value(); break;
      case '--retry-delay-ms': options.retryDelayMs = parseNumber(value(), '--retry-delay-ms'); break;
      case '--cooldown-ms': options.cooldownMs = parseNumber(value(), '--cooldown-ms'); break;
      case '--attempt-timeout-ms': options.attemptTimeoutMs = parseNumber(value(), '--attempt-timeout-ms'); break;
      default: usage(`unknown option: ${arg}`);
    }
  }
  return options;
}

// Fixed, validated upstream fixtures. The parent builds the exact expected client bodies
// from the same fixtures, so any drift between the two processes is a harness failure.
function fixtureMessage(model) {
  return { id: 'msg_bench', type: 'message', role: 'assistant', model,
    content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 } };
}

function fixtureSse(model) {
  const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
  return frame('message_start', { message: { id: 'msg_bench', type: 'message', role: 'assistant', model,
    content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 1, output_tokens: 0 } } }) +
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'ok' } }) +
    frame('content_block_stop', { index: 0 }) +
    frame('message_delta', { delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 1 } }) +
    frame('message_stop');
}

async function main() {
  for (const name of BASELINE_ENV) delete process.env[name];
  const options = parseArgs(process.argv.slice(2));

  // Keep the proxy's per-request diagnostics out of the measured path; retain the first
  // few lines for crash forensics and count the rest.
  let suppressed = 0;
  console.error = (...args) => {
    suppressed += 1;
    if (suppressed <= 5) writeStderr('[bench-worker]', ...args);
  };

  const upstream = { calls: 0, planCalls: 0, urls: {}, statuses: {}, bodies: new Map() };
  let callIndex = 0;

  function summarizePayload(text) {
    const body = JSON.parse(text);
    return { bytes: Buffer.byteLength(text), model: body.model, stream: body.stream === true,
      thinkingType: body.thinking?.type, providerAllowFallbacks: body.provider?.allow_fallbacks,
      metadataPresent: Object.hasOwn(body, 'metadata'),
      messageCount: Array.isArray(body.messages) ? body.messages.length : 0,
      toolCount: Array.isArray(body.tools) ? body.tools.length : 0 };
  }

  function recordPayload(url, text) {
    upstream.calls += 1;
    upstream.urls[url] = (upstream.urls[url] || 0) + 1;
    if (url !== EXTERNAL_URL) {
      upstream.planCalls += 1;
      return null;
    }
    const hash = createHash('sha256').update(text).digest('hex');
    let entry = upstream.bodies.get(hash);
    if (!entry) {
      entry = { hash, count: 0, ...summarizePayload(text) };
      upstream.bodies.set(hash, entry);
    }
    entry.count += 1;
    return hash;
  }

  // Retry fixtures are driven by a deterministic call sequence because the benchmark
  // retry workloads are sequential: mode fail-first = fail,ok ; fail-twice = fail,fail,ok ;
  // fail-always = fail,...
  function failsThisCall() {
    switch (options.mode) {
      case 'ok': return false;
      case 'fail-first': return callIndex % 2 === 0;
      case 'fail-twice': return callIndex % 3 !== 2;
      case 'fail-always': return true;
      default: return false;
    }
  }

  const fetchImpl = async (url, requestOptions) => {
    const text = String(requestOptions?.body ?? '');
    const hash = recordPayload(url, text);
    if (!hash) return new Response('bench: unexpected Plan request', { status: 599 });
    const failing = failsThisCall();
    callIndex += 1;
    if (failing) {
      const status = options.mode === 'fail-always' ? 503 : 500;
      upstream.statuses[status] = (upstream.statuses[status] || 0) + 1;
      return new Response('bench-upstream-failure', { status });
    }
    upstream.statuses[200] = (upstream.statuses[200] || 0) + 1;
    const model = JSON.parse(text).model;
    return options.stream
      ? new Response(fixtureSse(model), { headers: { 'content-type': 'text/event-stream' } })
      : new Response(JSON.stringify(fixtureMessage(model)), { headers: { 'content-type': 'application/json' } });
  };

  const server = createProxy({ key: options.key, nonce: options.nonce, openrouterModel: options.upstreamModel,
    retryDelayMs: options.retryDelayMs, cooldownMs: options.cooldownMs, attemptTimeoutMs: options.attemptTimeoutMs,
    streamPassthrough: options.streamPassthrough, metricsPath: null, fetchImpl });

  await listenSafe(server);
  const port = server.address().port;
  const cpuBase = process.cpuUsage();

  function memorySample() {
    const gc = typeof global.gc === 'function';
    // Two collections: the first promotes survivors, the second reclaims what the
    // promotion made collectible, so heapUsed reflects retained memory rather than garbage.
    if (gc) { global.gc(); global.gc(); }
    const usage = process.memoryUsage();
    const cpu = process.cpuUsage();
    return { gc, rss: usage.rss, heapUsed: usage.heapUsed, heapTotal: usage.heapTotal,
      external: usage.external, arrayBuffers: usage.arrayBuffers,
      cpuUserMs: Number(((cpu.user - cpuBase.user) / 1000).toFixed(2)),
      cpuSystemMs: Number(((cpu.system - cpuBase.system) / 1000).toFixed(2)),
      at: new Date().toISOString() };
  }

  function emit(message) { process.stdout.write(`${JSON.stringify(message)}\n`); }

  let closing = false;
  async function shutdown(code) {
    if (closing) return;
    closing = true;
    try { server.closeAllConnections?.(); } catch {}
    await new Promise(resolve => { try { server.close(resolve); } catch { resolve(); } });
    process.exit(code);
  }

  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on('line', line => {
    let request;
    try { request = JSON.parse(line); } catch { return; }
    if (!request || typeof request !== 'object' || typeof request.cmd !== 'string') return;
    switch (request.cmd) {
      case 'sample':
        emit({ type: 'sample', id: request.id, memory: memorySample() });
        break;
      case 'report':
        emit({ type: 'report', id: request.id,
          upstream: { calls: upstream.calls, planCalls: upstream.planCalls, urls: upstream.urls,
            statuses: upstream.statuses, bodies: [...upstream.bodies.values()] },
          memory: memorySample(), suppressedErrorLines: suppressed,
          config: { mode: options.mode, stream: options.stream, streamPassthrough: options.streamPassthrough } });
        break;
      case 'close':
        emit({ type: 'closing', id: request.id });
        setTimeout(() => { shutdown(0); }, 10);
        break;
      default:
        emit({ type: 'error', id: request.id, message: `unknown command: ${request.cmd}` });
    }
  });
  input.on('close', () => { shutdown(0); });

  emit({ type: 'ready', port, pid: process.pid, gc: typeof global.gc === 'function', node: process.version,
    execArgv: process.execArgv, config: { mode: options.mode, stream: options.stream,
      streamPassthrough: options.streamPassthrough, upstreamModel: options.upstreamModel } });
}

try {
  await main();
} catch (error) {
  writeStderr(`bench_proxy_worker: ${error?.stack || error}`);
  process.exit(1);
}
