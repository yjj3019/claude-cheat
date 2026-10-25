// Reproducible performance harness for the claude-cheat request path.
//
//   node tests/bench_proxy.mjs --quick
//   node tests/bench_proxy.mjs --workers 5 --rounds 20
//   node tests/bench_proxy.mjs --soak-seconds 30
//
// Design goals:
//   * Fresh process per repetition: every measured proxy lives in its own child process
//     (tests/bench_proxy_worker.mjs), so JIT state, metrics counters and heap history do
//     not leak between repetitions. The parent reports the median per process and the
//     median across processes (inter-process variance), never a single run.
//   * Load separation: the parent generates every HTTP request; the child only hosts the
//     proxy with a zero-latency fake upstream, so observed memory belongs to the proxy.
//   * Correctness first: every response must equal the expected validated body, the fake
//     upstream's rewritten payload must hash-match the expected rewrite exactly, retry
//     counts are asserted exactly and a streaming-passthrough body is compared byte for
//     byte. A "faster" run that changes any result fails.
//   * Time-based soak: continuous concurrent load against one worker, with post-GC memory
//     samples at start/mid/end and configurable growth thresholds (the only soak failures).
//   * Machine-readable output plus a human summary; exit code 0 pass, 1 correctness or
//     budget failure, 2 usage error.
//
// The module also exports its pure aggregation/verdict helpers so
// tests/bench_harness.test.mjs can regression-test the harness itself without spawning
// anything or touching the network.
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { arch, cpus, platform, release, totalmem } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

export const EXIT = Object.freeze({ pass: 0, fail: 1, usage: 2 });
export const CLIENT_MODEL = 'claude-sonnet-5-5';
export const UPSTREAM_MODEL = 'bench/model';
const NONCE = 'bench-nonce';
const KEY = 'sk-or-bench';
const OAUTH = 'Bearer sk-ant-oat-bench';
const BETA = 'oauth-2025-04-20';
const ANTHROPIC_VERSION = '2023-06-01';
const POLLUTING_ENV = ['ROUTE_LOG_PATH', 'ROUTE_METRICS_PATH', 'ROUTE_STREAM_PASSTHROUGH', 'ROUTE_HANDOVER_TIMEOUT_MS'];
const HISTORY_TURNS = 120;
const HISTORY_KB_EACH = 2;
const SOAK_PAYLOAD_KB = 64;
const SOAK_WARMUP_MS = 1500;
const RETRY_MODES = ['fail-first', 'fail-twice', 'fail-always'];
const CALLS_PER_REQUEST = { 'fail-first': 2, 'fail-twice': 3, 'fail-always': 4 };
const RETRY_OAUTH_ERROR = { type: 'error', error: { type: 'api_error', message: 'Saved subscription OAuth required' } };

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, '..');
const WORKER_PATH = join(HERE, 'bench_proxy_worker.mjs');

const QUICK_PROFILE = Object.freeze({ workers: 2, rounds: 6, warmup: 1, retryProcesses: 1, retryRounds: 4,
  sizes: Object.freeze([16, 1024]) });
const FULL_PROFILE = Object.freeze({ workers: 3, rounds: 15, warmup: 2, retryProcesses: 2, retryRounds: 8,
  sizes: Object.freeze([16, 64, 256, 1024]) });

export class UsageError extends Error {
  constructor(message) {
    super(message);
    this.name = 'UsageError';
    this.exitCode = EXIT.usage;
  }
}

// ---------------------------------------------------------------- pure helpers

const round = value => Number(Number(value).toFixed(3));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const sha256 = text => createHash('sha256').update(text).digest('hex');
const truncate = (text, max = 300) => {
  const value = String(text ?? '');
  return value.length <= max ? value : `${value.slice(0, max)}…`;
};

export function median(values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function percentile(values, fraction) {
  if (!Array.isArray(values) || values.length === 0) return null;
  if (!(fraction >= 0 && fraction <= 1)) throw new RangeError('fraction must be between 0 and 1');
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(fraction * sorted.length) - 1));
  return sorted[index];
}

export function summarizeSamples(values) {
  if (!Array.isArray(values) || values.length === 0)
    return { count: 0, medianMs: null, p90Ms: null, minMs: null, maxMs: null };
  // An hour-long soak produces millions of samples; spread arguments would exceed V8's
  // call-stack limit, so min/max are folded instead of passed through Math.min(...).
  let min = Infinity, max = -Infinity;
  for (const value of values) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  return { count: values.length, medianMs: round(median(values)), p90Ms: round(percentile(values, 0.9)),
    minMs: round(min), maxMs: round(max) };
}

// A soak cannot keep every latency sample: at thousands of requests per second an hour
// would retain millions of numbers and write them all into the result JSON. Each phase
// keeps a bounded uniform reservoir for the percentiles while the exact count is kept
// separately; the pseudo-random generator is seeded so a run can be reproduced.
export const SOAK_SAMPLE_CAP = 20000;
export function createSampleReservoir(cap = SOAK_SAMPLE_CAP, seed = 0x9e3779b9) {
  const buckets = [[], [], []];
  const counts = [0, 0, 0];
  let state = seed >>> 0;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
  return {
    cap,
    record(phase, wallMs) {
      if (!(phase >= 0 && phase <= 2)) throw new RangeError('phase must be 0..2');
      counts[phase] += 1;
      const bucket = buckets[phase];
      if (bucket.length < cap) { bucket.push(wallMs); return; }
      const index = Math.floor(next() * counts[phase]);
      if (index < cap) bucket[index] = wallMs;
    },
    samples() {
      return buckets.flatMap((bucket, phase) => bucket.map(wallMs => ({ phase, wallMs })));
    },
    counts() { return counts.slice(); },
    sampledCounts() { return buckets.map(bucket => bucket.length); },
  };
}

// Per-process medians are the unit of aggregation: pooling raw requests from different
// processes would let one fast process dominate the result and hide variance.
export function aggregateProcesses(processes = []) {
  const measured = processes.filter(item => Number.isFinite(item?.medianMs));
  if (!measured.length) {
    return { medianMs: null, p90Ms: null, minMs: null, maxMs: null, spreadRatio: null, spreadPct: null,
      processMedians: processes.map(() => null) };
  }
  const medians = measured.map(item => item.medianMs);
  const min = Math.min(...medians);
  const max = Math.max(...medians);
  const p90s = measured.map(item => item.p90Ms).filter(Number.isFinite);
  return { medianMs: round(median(medians)), p90Ms: p90s.length ? round(median(p90s)) : null,
    minMs: round(min), maxMs: round(max),
    spreadRatio: min > 0 ? Number((max / min).toFixed(3)) : null,
    spreadPct: min > 0 ? Number((((max - min) / min) * 100).toFixed(1)) : null,
    processMedians: processes.map(item => Number.isFinite(item?.medianMs) ? round(item.medianMs) : null) };
}

export function fixtureMessage(model = CLIENT_MODEL) {
  return { id: 'msg_bench', type: 'message', role: 'assistant', model,
    content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 } };
}

export function fixtureSse(model = CLIENT_MODEL) {
  const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
  return frame('message_start', { message: { id: 'msg_bench', type: 'message', role: 'assistant', model,
    content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 1, output_tokens: 0 } } }) +
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'ok' } }) +
    frame('content_block_stop', { index: 0 }) +
    frame('message_delta', { delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 1 } }) +
    frame('message_stop');
}

// Mirror of the proxy's external rewrite for the fixed benchmark payloads (plain messages,
// no native thinking, no metadata, no unsupported fields). The hash of this body must equal
// what the fake upstream observed in every child. Accepts the client request as a JSON
// string or an already-parsed object.
export function expectedExternalBody(clientBody, { upstreamModel = UPSTREAM_MODEL } = {}) {
  const client = typeof clientBody === 'string' ? JSON.parse(clientBody) : clientBody;
  const external = { ...client, model: upstreamModel };
  delete external.metadata;
  external.thinking = { type: 'disabled' };
  if (client.tools) external.tools = client.tools.map(tool => ({ ...tool, input_schema: tool.input_schema }));
  external.provider = { allow_fallbacks: false };
  return external;
}

export function describeExternalPayload(externalBody) {
  const text = JSON.stringify(externalBody);
  return { hash: sha256(text), bytes: Buffer.byteLength(text),
    summary: { bytes: Buffer.byteLength(text), model: externalBody.model, stream: externalBody.stream === true,
      thinkingType: externalBody.thinking?.type, providerAllowFallbacks: externalBody.provider?.allow_fallbacks,
      metadataPresent: Object.hasOwn(externalBody, 'metadata'),
      messageCount: Array.isArray(externalBody.messages) ? externalBody.messages.length : 0,
      toolCount: Array.isArray(externalBody.tools) ? externalBody.tools.length : 0 } };
}

export function externalPayloadExpectation(clientBody, options) {
  return describeExternalPayload(expectedExternalBody(clientBody, options));
}

export function sizePayload(kb, { model = CLIENT_MODEL } = {}) {
  const filler = 'x'.repeat(Math.max(0, kb * 1024 - 512));
  return JSON.stringify({ model, max_tokens: 256,
    messages: [{ role: 'user', content: filler }], tools: [
      { name: 'Read', description: 'read a file', input_schema: { type: 'object',
        properties: { path: { type: 'string' } }, required: ['path'] } }] });
}

export function historyPayload(turns, { kbEach = HISTORY_KB_EACH, model = CLIENT_MODEL } = {}) {
  const messages = [];
  for (let index = 0; index < turns; index++) {
    messages.push({ role: index % 2 ? 'assistant' : 'user',
      content: [{ type: 'text', text: `m${index} ` + 'h'.repeat(kbEach * 1024) }] });
  }
  return JSON.stringify({ model, max_tokens: 256, messages });
}

export function streamPayload({ model = CLIENT_MODEL } = {}) {
  return JSON.stringify({ model, max_tokens: 256, stream: true,
    messages: [{ role: 'user', content: 'stream please' }] });
}

// Compare one captured HTTP response with the expected validated result. `expected.text`
// means an exact byte-for-byte body (streaming); `expected.body` means a deeply equal JSON
// body (every JSON response is parsed and compared, not just its size).
export function validateHttpResponse(observed, expected) {
  const failures = [];
  if (!observed || typeof observed !== 'object') return { ok: false, failures: ['no response captured'] };
  if (expected.status !== undefined && observed.status !== expected.status)
    failures.push(`status ${observed.status} != ${expected.status}`);
  if (expected.contentTypeIncludes && !String(observed.contentType || '').includes(expected.contentTypeIncludes))
    failures.push(`content-type ${observed.contentType || '<none>'} lacks "${expected.contentTypeIncludes}"`);
  if (expected.text !== undefined) {
    if (observed.text !== expected.text)
      failures.push(`body differs (expected ${expected.text.length} chars, received ${String(observed.text ?? '').length})`);
  } else if (expected.body !== undefined) {
    let parsed = null;
    try { parsed = JSON.parse(observed.text); } catch (error) { failures.push(`body is not JSON: ${error.message}`); }
    if (parsed !== null && !isDeepStrictEqual(parsed, expected.body))
      failures.push('body differs from expected validated payload');
  }
  return { ok: failures.length === 0, failures };
}

// Assert the child's fake upstream saw exactly the expected rewritten payloads and counts,
// and that no request was silently rerouted to the Plan API.
export function verifyUpstreamReport(report, expectations = {}) {
  const failures = [];
  const expectedCalls = expectations.expectedCalls || [];
  const entries = new Map((report?.bodies || []).map(entry => [entry.hash, entry]));
  const expectedTotal = expectedCalls.reduce((sum, entry) => sum + (entry.count || 0), 0);
  const observedCalls = Number(report?.calls ?? 0);
  if (observedCalls !== expectedTotal) failures.push(`upstream calls ${observedCalls} != expected ${expectedTotal}`);
  for (const expected of expectedCalls) {
    const short = String(expected.hash).slice(0, 12);
    const entry = entries.get(expected.hash);
    if (!entry) { failures.push(`upstream never saw expected payload ${short}…`); continue; }
    if (entry.count !== expected.count) failures.push(`payload ${short}… seen ${entry.count}x, expected ${expected.count}x`);
    if (expected.summary) {
      for (const [key, value] of Object.entries(expected.summary)) {
        const observed = entry[key];
        const matches = typeof value === 'number' && typeof observed === 'number' ?
          Math.abs(observed - value) < 1e-9 : observed === value;
        if (!matches) failures.push(`payload ${short}… ${key} = ${observed} != ${value}`);
      }
    }
  }
  const expectedHashes = new Set(expectedCalls.map(entry => entry.hash));
  for (const hash of entries.keys()) {
    if (!expectedHashes.has(hash)) failures.push(`unexpected rewritten payload ${String(hash).slice(0, 12)}…`);
  }
  const observedPlan = Number(report?.planCalls ?? 0);
  const expectedPlan = Number(expectations.planCalls ?? 0);
  if (observedPlan !== expectedPlan) failures.push(`Plan fallback attempts ${observedPlan} != expected ${expectedPlan}`);
  return { ok: failures.length === 0, failures };
}

export function verifyMetrics(metrics, expected = {}) {
  const failures = [];
  for (const [key, value] of Object.entries(expected)) {
    const observed = metrics?.[key];
    const matches = typeof value === 'number' && typeof observed === 'number' ?
      Math.abs(observed - value) < 1e-9 : observed === value;
    if (!matches) failures.push(`metrics.${key} = ${observed} != expected ${value}`);
  }
  return { ok: failures.length === 0, failures };
}

export function summarizeSoak({ samples = [], counts = null, memory = {}, thresholds = {} } = {}) {
  // ``samples`` may be a bounded reservoir: ``counts`` carries the exact per-phase totals
  // so the report never confuses "measured" with "sampled".
  const buckets = [0, 1, 2].map(phase => {
    const stats = summarizeSamples(samples.filter(sample => sample.phase === phase).map(sample => sample.wallMs));
    const total = Array.isArray(counts) ? counts[phase] : stats.count;
    return { phase, ...stats, count: total,
      ...(Array.isArray(counts) ? { sampled: stats.count } : {}) };
  });
  const start = memory.start, mid = memory.mid, end = memory.end;
  const failures = [];
  const growth = { gcAvailable: Boolean(start?.gc && end?.gc), rssGrowthMb: null, heapGrowthMb: null,
    midRssGrowthMb: null, midHeapGrowthMb: null };
  if (start && end && Number.isFinite(start.rss) && Number.isFinite(end.rss)) {
    growth.rssGrowthMb = round((end.rss - start.rss) / 1048576);
    if (Number.isFinite(thresholds.maxRssGrowthMb) && growth.rssGrowthMb > thresholds.maxRssGrowthMb)
      failures.push(`soak RSS growth ${growth.rssGrowthMb}MB > ${thresholds.maxRssGrowthMb}MB`);
  }
  // Without --expose-gc, heapUsed includes uncollected garbage and a growth check would be
  // meaningless, so the heap threshold is only enforced when both samples came post-GC.
  if (growth.gcAvailable && Number.isFinite(start.heapUsed) && Number.isFinite(end.heapUsed)) {
    growth.heapGrowthMb = round((end.heapUsed - start.heapUsed) / 1048576);
    if (Number.isFinite(thresholds.maxHeapGrowthMb) && growth.heapGrowthMb > thresholds.maxHeapGrowthMb)
      failures.push(`soak retained heap growth ${growth.heapGrowthMb}MB > ${thresholds.maxHeapGrowthMb}MB`);
  }
  if (start && mid && Number.isFinite(start.rss) && Number.isFinite(mid.rss))
    growth.midRssGrowthMb = round((mid.rss - start.rss) / 1048576);
  if (start && mid && growth.gcAvailable && mid.gc)
    growth.midHeapGrowthMb = round((mid.heapUsed - start.heapUsed) / 1048576);
  return { pass: failures.length === 0, failures, buckets, growth };
}

export function evaluateVerdict({ correctnessFailures = [], workloads = [], retry = [], soak = null,
  thresholds = {} } = {}) {
  const failures = correctnessFailures.map(failure =>
    typeof failure === 'string' ? failure : failure?.message ?? JSON.stringify(failure));
  if (Number.isFinite(thresholds.maxMedianMs)) {
    const sizeWorkloads = workloads.filter(workload => workload.kind === 'size' && Number.isFinite(workload.medianMs));
    if (sizeWorkloads.length) {
      const largest = sizeWorkloads.reduce((best, workload) => workload.bodyKb > best.bodyKb ? workload : best);
      if (largest.medianMs > thresholds.maxMedianMs)
        failures.push(`median ${largest.medianMs}ms for ${largest.name} > budget ${thresholds.maxMedianMs}ms`);
    }
  }
  if (Number.isFinite(thresholds.maxSpreadRatio)) {
    for (const workload of [...workloads, ...retry]) {
      if (Number.isFinite(workload.spreadRatio) && workload.spreadRatio > thresholds.maxSpreadRatio)
        failures.push(`inter-process spread ${workload.spreadRatio}x for ${workload.name} > budget ${thresholds.maxSpreadRatio}x`);
    }
  }
  if (soak && soak.pass === false) {
    for (const failure of soak.failures || []) failures.push(failure);
  }
  return { pass: failures.length === 0, exitCode: failures.length ? EXIT.fail : EXIT.pass, failures };
}

export function parseArgs(argv = []) {
  const options = { quick: false, help: false, workers: 3, rounds: 15, warmup: 2, retryProcesses: 2,
    sizes: null, soakSeconds: 0, soakConcurrency: 8, maxRssGrowthMb: 192, maxHeapGrowthMb: 12,
    maxMedianMs: null, maxSpreadRatio: null, out: null, provided: new Set() };
  const set = (name, value) => { options[name] = value; options.provided.add(name); };
  const value = (index, flag) => {
    if (index + 1 >= argv.length) throw new UsageError(`missing value for ${flag}`);
    return argv[index + 1];
  };
  const integer = (raw, flag, min, max) => {
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < min || parsed > max)
      throw new UsageError(`${flag} must be an integer between ${min} and ${max} (got ${raw})`);
    return parsed;
  };
  const number = (raw, flag, min) => {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed < min)
      throw new UsageError(`${flag} must be a number >= ${min} (got ${raw})`);
    return parsed;
  };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case '--quick': options.quick = true; break;
      case '--help': case '-h': options.help = true; break;
      case '--workers': set('workers', integer(value(index, arg), arg, 1, 16)); index += 1; break;
      case '--rounds': set('rounds', integer(value(index, arg), arg, 1, 200)); index += 1; break;
      case '--warmup': set('warmup', integer(value(index, arg), arg, 0, 20)); index += 1; break;
      case '--retry-processes': set('retryProcesses', integer(value(index, arg), arg, 1, 8)); index += 1; break;
      case '--sizes': {
        const raw = value(index, arg);
        const sizes = raw.split(',').map(part => Number(part.trim()));
        if (!sizes.length || sizes.length > 8 || sizes.some(size => !Number.isInteger(size) || size < 1 || size > 2048))
          throw new UsageError(`--sizes must be 1-8 comma-separated integer KB values between 1 and 2048 (got ${raw})`);
        set('sizes', sizes);
        index += 1;
        break;
      }
      case '--soak-seconds': set('soakSeconds', integer(value(index, arg), arg, 0, 3600)); index += 1; break;
      case '--soak-concurrency': set('soakConcurrency', integer(value(index, arg), arg, 1, 64)); index += 1; break;
      case '--max-rss-growth-mb': set('maxRssGrowthMb', number(value(index, arg), arg, 0)); index += 1; break;
      case '--max-heap-growth-mb': set('maxHeapGrowthMb', number(value(index, arg), arg, 0)); index += 1; break;
      case '--max-median-ms': set('maxMedianMs', number(value(index, arg), arg, 0)); index += 1; break;
      case '--max-spread-ratio': set('maxSpreadRatio', number(value(index, arg), arg, 1)); index += 1; break;
      case '--out': set('out', value(index, arg)); index += 1; break;
      default: throw new UsageError(`unknown option: ${arg}`);
    }
  }
  return options;
}

export function resolveConfig(options) {
  const profile = options.quick ? QUICK_PROFILE : FULL_PROFILE;
  const pick = (name, fallback) => options.provided?.has(name) ? options[name] : fallback;
  return Object.freeze({ quick: Boolean(options.quick), help: Boolean(options.help),
    workers: pick('workers', profile.workers), rounds: pick('rounds', profile.rounds),
    warmup: pick('warmup', profile.warmup), retryProcesses: pick('retryProcesses', profile.retryProcesses),
    retryRounds: profile.retryRounds, sizes: [...pick('sizes', profile.sizes)],
    soakSeconds: pick('soakSeconds', 0), soakConcurrency: pick('soakConcurrency', 8),
    maxRssGrowthMb: pick('maxRssGrowthMb', 192), maxHeapGrowthMb: pick('maxHeapGrowthMb', 12),
    maxMedianMs: pick('maxMedianMs', null), maxSpreadRatio: pick('maxSpreadRatio', null),
    out: pick('out', null) });
}

// ---------------------------------------------------------------- worker client

export async function spawnWorker({ mode = 'ok', stream = false, streamPassthrough = false,
  attemptTimeoutMs = 5000 } = {}) {
  const args = ['--expose-gc', WORKER_PATH, '--mode', mode, '--upstream-model', UPSTREAM_MODEL,
    '--nonce', NONCE, '--key', KEY, '--retry-delay-ms', '0', '--cooldown-ms', '0',
    '--attempt-timeout-ms', String(attemptTimeoutMs)];
  if (stream) args.push('--stream');
  if (streamPassthrough) args.push('--stream-passthrough');
  const env = { ...process.env };
  for (const name of POLLUTING_ENV) delete env[name];
  const child = spawn(process.execPath, args, { cwd: REPO_ROOT, env, stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
  const stderrTail = () => stderr ? `\nlast stderr:\n${truncate(stderr, 1200)}` : '';

  let exited = false;
  let exitInfo = null;
  const pending = new Map();
  const exitWaiters = [];
  let nextId = 1;
  let readyResolve;
  let readyReject;
  const ready = new Promise((resolvePromise, rejectPromise) => {
    readyResolve = resolvePromise; readyReject = rejectPromise;
  });
  ready.catch(() => {});
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message?.type === 'ready') { readyResolve(message); return; }
    if (message?.id !== undefined && pending.has(message.id)) {
      const entry = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.type === 'error') entry.reject(new Error(`worker: ${message.message}`));
      else entry.resolve(message);
    }
  });
  child.on('exit', (code, signal) => {
    exited = true;
    exitInfo = { code, signal };
    readyReject(new Error(`worker exited before ready (code ${code}, signal ${signal})${stderrTail()}`));
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error(`worker exited (code ${code}, signal ${signal})${stderrTail()}`));
    }
    pending.clear();
    for (const resolveExit of exitWaiters.splice(0)) resolveExit(exitInfo);
  });
  child.on('error', error => readyReject(new Error(`worker failed to spawn: ${error.message}`)));

  const withTimeout = (promise, ms, label) => new Promise((resolvePromise, rejectPromise) => {
    const timer = setTimeout(() => rejectPromise(new Error(`${label} after ${ms}ms${stderrTail()}`)), ms);
    promise.then(value => { clearTimeout(timer); resolvePromise(value); },
      error => { clearTimeout(timer); rejectPromise(error); });
  });

  const command = (cmd, timeoutMs = 60000) => {
    if (exited) return Promise.reject(new Error(`worker is not running${stderrTail()}`));
    const id = nextId++;
    return new Promise((resolvePromise, rejectPromise) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        rejectPromise(new Error(`worker command '${cmd}' timed out after ${timeoutMs}ms${stderrTail()}`));
      }, timeoutMs);
      pending.set(id, { resolve: resolvePromise, reject: rejectPromise, timer });
      try { child.stdin.write(`${JSON.stringify({ cmd, id })}\n`); }
      catch (error) { pending.delete(id); clearTimeout(timer); rejectPromise(error); }
    });
  };

  const waitForExit = ms => new Promise(resolvePromise => {
    if (exited) return resolvePromise(true);
    const timer = setTimeout(() => resolvePromise(false), ms);
    exitWaiters.push(info => { clearTimeout(timer); resolvePromise(info); });
  });

  let closed = false;
  const close = async () => {
    if (closed) return;
    closed = true;
    try { if (!exited) await command('close', 5000); } catch {}
    if (!exited) {
      try { child.stdin.end(); } catch {}
      if (!await waitForExit(3000)) child.kill('SIGKILL');
    }
    lines.close();
  };
  const kill = () => { try { child.kill('SIGKILL'); } catch {} };

  let readyInfo;
  try {
    readyInfo = await withTimeout(ready, 20000, 'worker startup timed out');
  } catch (error) {
    kill();
    throw error;
  }
  return Object.freeze({ port: readyInfo.port, pid: readyInfo.pid, gc: readyInfo.gc, ready: readyInfo,
    command, close, kill,
    sample: async () => (await command('sample', 30000)).memory,
    report: async () => await command('report', 60000) });
}

// ---------------------------------------------------------------- request helpers

function clientHeaders({ oauth = true } = {}) {
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    'anthropic-version': ANTHROPIC_VERSION, 'anthropic-beta': BETA };
  if (oauth) headers.authorization = OAUTH;
  return headers;
}

async function post(port, bodyText, { oauth = true, timeoutMs = 60000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error('bench request timed out')), timeoutMs);
  const started = performance.now();
  try {
    const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: 'POST',
      headers: clientHeaders({ oauth }), body: bodyText, signal: controller.signal });
    const text = await response.text();
    return { status: response.status, text, contentType: response.headers.get('content-type') || '',
      wallMs: performance.now() - started };
  } finally {
    clearTimeout(timer);
  }
}

async function fetchMetrics(port) {
  const response = await fetch(`http://127.0.0.1:${port}/api/metrics`,
    { headers: { 'x-local-route-token': NONCE } });
  if (!response.ok) throw new Error(`metrics endpoint returned ${response.status}`);
  return await response.json();
}

async function runSequentialWorkload(worker, { name, kind, bodyText, expected, warmup, rounds, oauth = true }) {
  const samples = [];
  const failures = [];
  let sent = 0;
  for (let index = -warmup; index < rounds; index++) {
    let observed;
    try {
      observed = await post(worker.port, bodyText, { oauth });
    } catch (error) {
      failures.push({ workload: name, request: index, message: `request failed: ${error.message}` });
      break;
    }
    sent += 1;
    const check = validateHttpResponse(observed, expected);
    if (!check.ok) {
      failures.push({ workload: name, request: index, message: check.failures.join('; '),
        observed: truncate(observed.text, 300) });
      break;
    }
    if (index >= 0) samples.push(observed.wallMs);
  }
  return { name, kind, bodyKb: round(Buffer.byteLength(bodyText) / 1024), sent, samples,
    expectation: externalPayloadExpectation(bodyText), failures, ...summarizeSamples(samples) };
}

// ---------------------------------------------------------------- phases

async function runPerfProcess(index, config) {
  const worker = await spawnWorker({ mode: 'ok' });
  const result = { index, workloads: [], failures: [], memoryStart: null, memoryEnd: null,
    metrics: null, upstream: null, reportConfig: null };
  try {
    result.memoryStart = await worker.sample();
    const plans = config.sizes.map(kb => ({ name: `size ${kb}KB`, kind: 'size', bodyText: sizePayload(kb) }));
    plans.push({ name: `history ${HISTORY_TURNS} turns`, kind: 'history',
      bodyText: historyPayload(HISTORY_TURNS) });
    for (const plan of plans) {
      const workload = await runSequentialWorkload(worker, { ...plan, warmup: config.warmup,
        rounds: config.rounds, expected: { status: 200, contentTypeIncludes: 'application/json',
          body: fixtureMessage(CLIENT_MODEL) } });
      result.workloads.push(workload);
    }
    const report = await worker.report();
    result.memoryEnd = report.memory;
    result.reportConfig = report.config;
    result.upstream = report.upstream;
    result.metrics = await fetchMetrics(worker.port);
    const expectedCalls = result.workloads.map(workload => ({ ...workload.expectation, count: workload.sent }));
    const upstreamCheck = verifyUpstreamReport(report.upstream, { expectedCalls, planCalls: 0 });
    if (!upstreamCheck.ok)
      result.failures.push(...upstreamCheck.failures.map(message => `[perf p${index}] ${message}`));
    const totalSent = result.workloads.reduce((sum, workload) => sum + workload.sent, 0);
    const metricCheck = verifyMetrics(result.metrics, { attempts: totalSent, externalAttempts: totalSent,
      retries: 0, failed: 0, ok: totalSent, streams: 0, circuitOpens: 0 });
    if (!metricCheck.ok)
      result.failures.push(...metricCheck.failures.map(message => `[perf p${index}] ${message}`));
  } finally {
    await worker.close();
  }
  return result;
}

async function runRetryProcess({ index, mode, config }) {
  const oauth = mode !== 'fail-always';
  const bodyText = sizePayload(1024);
  const worker = await spawnWorker({ mode });
  const failures = [];
  try {
    const expected = mode === 'fail-always' ?
      { status: 403, contentTypeIncludes: 'application/json', body: RETRY_OAUTH_ERROR } :
      { status: 200, contentTypeIncludes: 'application/json', body: fixtureMessage(CLIENT_MODEL) };
    const workload = await runSequentialWorkload(worker, { name: `retry ${mode}`, kind: 'retry', bodyText,
      expected, warmup: 1, rounds: config.retryRounds, oauth });
    const report = await worker.report();
    const metrics = await fetchMetrics(worker.port);
    failures.push(...workload.failures.map(failure => `[retry ${mode} p${index}] ${failure.message}`));
    const callsPerRequest = CALLS_PER_REQUEST[mode];
    const upstreamCheck = verifyUpstreamReport(report.upstream,
      { expectedCalls: [{ ...workload.expectation, count: workload.sent * callsPerRequest }], planCalls: 0 });
    failures.push(...upstreamCheck.failures.map(message => `[retry ${mode} p${index}] ${message}`));
    const expectedMetrics = mode === 'fail-always' ?
      { attempts: workload.sent * 4, externalAttempts: workload.sent, retries: workload.sent * 3,
        failed: workload.sent, ok: 0, circuitOpens: workload.sent } :
      { attempts: workload.sent * callsPerRequest, externalAttempts: workload.sent,
        retries: workload.sent * (callsPerRequest - 1), failed: 0, ok: workload.sent, circuitOpens: 0 };
    const metricCheck = verifyMetrics(metrics, expectedMetrics);
    failures.push(...metricCheck.failures.map(message => `[retry ${mode} p${index}] ${message}`));
    return { index, mode, sent: workload.sent, callsPerRequest, samples: workload.samples,
      expectation: workload.expectation, failures, metrics, upstream: report.upstream,
      ...summarizeSamples(workload.samples) };
  } finally {
    await worker.close();
  }
}

async function runStreamingCase() {
  const worker = await spawnWorker({ mode: 'ok', stream: true, streamPassthrough: true });
  try {
    const bodyText = streamPayload();
    const observed = await post(worker.port, bodyText);
    const check = validateHttpResponse(observed, { status: 200,
      contentTypeIncludes: 'text/event-stream', text: fixtureSse(CLIENT_MODEL) });
    const report = await worker.report();
    const metrics = await fetchMetrics(worker.port);
    const failures = check.failures.map(message => `[streaming] ${message}`);
    const upstreamCheck = verifyUpstreamReport(report.upstream,
      { expectedCalls: [{ ...externalPayloadExpectation(bodyText), count: 1 }], planCalls: 0 });
    failures.push(...upstreamCheck.failures.map(message => `[streaming] ${message}`));
    const metricCheck = verifyMetrics(metrics, { attempts: 1, externalAttempts: 1, retries: 0,
      streams: 1, ok: 1, failed: 0 });
    failures.push(...metricCheck.failures.map(message => `[streaming] ${message}`));
    return { ok: failures.length === 0, failures,
      expectedFrames: (fixtureSse(CLIENT_MODEL).match(/^event: /gm) || []).length,
      approximateFrames: (observed.text.match(/^event: /gm) || []).length,
      bytes: Buffer.byteLength(observed.text), passthrough: Boolean(worker.ready?.config?.streamPassthrough),
      expectation: externalPayloadExpectation(bodyText), metrics, upstream: report.upstream };
  } finally {
    await worker.close();
  }
}

async function runSoak(config) {
  const worker = await spawnWorker({ mode: 'ok' });
  const failures = [];
  try {
    const bodyText = sizePayload(SOAK_PAYLOAD_KB);
    const expected = { status: 200, contentTypeIncludes: 'application/json', body: fixtureMessage(CLIENT_MODEL) };
    const expectation = externalPayloadExpectation(bodyText);
    const soakMs = config.soakSeconds * 1000;
    const warmupMs = Math.min(SOAK_WARMUP_MS, Math.max(250, Math.round(soakMs / 4)));

    // Warm up the request path, JIT and V8's committed heap *before* the measured window.
    // Without this, RSS growth is dominated by the one-time heap expansion every fresh
    // process performs under load (hundreds of MB of committed space with tiny heapUsed),
    // which says nothing about a leak. The measured start sample is post-warmup.
    let warmupRequests = 0;
    const warmupDeadline = performance.now() + warmupMs;
    const warmupLoop = async () => {
      while (performance.now() < warmupDeadline) {
        try {
          const observed = await post(worker.port, bodyText, { timeoutMs: 30000 });
          warmupRequests += 1;
          const check = validateHttpResponse(observed, expected);
          if (!check.ok && failures.length < 5) failures.push(`[soak warmup] ${check.failures.join('; ')}`);
        } catch (error) {
          if (failures.length < 5) failures.push(`[soak warmup] request failed: ${error.message}`);
        }
      }
    };
    await Promise.all(Array.from({ length: config.soakConcurrency }, warmupLoop));

    const startMemory = await worker.sample();
    const reservoir = createSampleReservoir();
    const errors = [];
    let sent = 0;
    // Two contiguous load windows with a clean memory sample in between: the GC that
    // produces the mid sample would otherwise stall the worker and inflate that bucket's
    // latency. Bucket 0 = first half, buckets 1/2 = quarters of the second half.
    const load = async (durationMs, phaseFor) => {
      const begin = performance.now();
      const deadline = begin + durationMs;
      const loop = async () => {
        while (performance.now() < deadline) {
          const phase = phaseFor(performance.now() - begin, durationMs);
          let observed;
          try {
            observed = await post(worker.port, bodyText, { timeoutMs: 30000 });
          } catch (error) {
            if (errors.length < 5) errors.push(`request failed: ${error.message}`);
            continue;
          }
          sent += 1;
          const check = validateHttpResponse(observed, expected);
          if (!check.ok && errors.length < 5) errors.push(check.failures.join('; '));
          reservoir.record(phase, observed.wallMs);
        }
      };
      await Promise.all(Array.from({ length: config.soakConcurrency }, loop));
    };
    await load(soakMs / 2, () => 0);
    const midMemory = await worker.sample();
    await load(soakMs / 2, elapsed => elapsed < soakMs / 4 ? 1 : 2);
    await delay(150);
    const endMemory = await worker.sample();
    const report = await worker.report();
    const metrics = await fetchMetrics(worker.port);
    const samples = reservoir.samples();
    const sampleCounts = reservoir.counts();
    const soak = summarizeSoak({ samples, counts: sampleCounts,
      memory: { start: startMemory, mid: midMemory, end: endMemory },
      thresholds: { maxRssGrowthMb: config.maxRssGrowthMb, maxHeapGrowthMb: config.maxHeapGrowthMb } });
    const allFailures = [...soak.failures, ...errors];
    const totalSent = warmupRequests + sent;
    const upstreamCheck = allFailures.length ? { ok: true, failures: [] } :
      verifyUpstreamReport(report.upstream, { expectedCalls: [{ ...expectation, count: totalSent }], planCalls: 0 });
    if (!upstreamCheck.ok) allFailures.push(...upstreamCheck.failures.map(message => `[soak] ${message}`));
    if (!allFailures.length) {
      const metricCheck = verifyMetrics(metrics, { attempts: totalSent, externalAttempts: totalSent, retries: 0,
        failed: 0, ok: totalSent, streams: 0 });
      allFailures.push(...metricCheck.failures.map(message => `[soak] ${message}`));
    }
    return { pass: allFailures.length === 0, failures: allFailures, requests: sent, totalRequests: totalSent,
      warmupRequests, warmupMs, errors: errors.length, rps: round(sent / config.soakSeconds),
      // samplesMs keeps the bounded reservoir (see SOAK_SAMPLE_CAP); sampleCounts has the
      // exact totals, so an hour-long report stays small without hiding how much was sent.
      samplesMs: samples.map(sample => round(sample.wallMs)), sampleCounts, sampleCap: reservoir.cap,
      sampledSamples: samples.length, buckets: soak.buckets, growth: soak.growth,
      memory: { start: startMemory, mid: midMemory, end: endMemory },
      expectation: { hash: expectation.hash, bytes: expectation.bytes }, metricsAttempts: metrics.attempts,
      upstream: report.upstream };
  } finally {
    await worker.close();
  }
}

// ---------------------------------------------------------------- reporting

function envInfo() {
  const list = cpus();
  return { node: process.version, platform: platform(), release: release(), arch: arch(),
    cpus: list.length, cpuModel: list[0]?.model ?? null, totalMemMb: Math.round(totalmem() / 1048576),
    execArgv: process.execArgv };
}

// Commit/branch are read straight from .git so the harness never pays a git subprocess
// (which can take seconds on a busy Windows machine). Dirty-tree detection is the one
// optional extra: it costs a `git status` walk, so --quick skips it (dirty = null).
function readGitRef(ref) {
  try { return readFileSync(join(REPO_ROOT, '.git', ...ref.split('/')), 'utf8').trim(); }
  catch {
    try {
      const packed = readFileSync(join(REPO_ROOT, '.git', 'packed-refs'), 'utf8');
      const line = packed.split(/\r?\n/).find(entry => entry.endsWith(` ${ref}`));
      return line ? line.split(' ')[0] : null;
    } catch { return null; }
  }
}

function gitInfo({ quick = false } = {}) {
  const info = { commit: null, shortCommit: null, branch: null, dirty: null };
  try {
    const head = readFileSync(join(REPO_ROOT, '.git', 'HEAD'), 'utf8').trim();
    let ref = null;
    if (head.startsWith('ref: ')) {
      ref = head.slice(5).trim();
      info.branch = ref.replace(/^refs\/heads\//, '');
      info.commit = readGitRef(ref);
    } else {
      info.commit = head;
    }
  } catch { /* fall through to the subprocess fallback */ }
  if (!info.commit) {
    try {
      info.commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'], timeout: 5000 }).trim() || null;
    } catch { info.commit = null; }
  }
  info.shortCommit = info.commit ? info.commit.slice(0, 7) : null;
  if (!quick && info.commit) {
    try {
      const status = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'],
        { cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 8000 });
      info.dirty = status.trim().length > 0;
    } catch { info.dirty = null; }
  }
  return info;
}

function sourceInfo() {
  try {
    const bytes = readFileSync(join(REPO_ROOT, 'claudeRouteProxy.mjs'));
    return { file: 'claudeRouteProxy.mjs', bytes: bytes.length, sha256: sha256(bytes) };
  } catch { return null; }
}

function formatMs(value) {
  return Number.isFinite(value) ? `${value.toFixed(2)}ms` : 'n/a';
}

function printSummary({ config, workloads, retry, streaming, soak, verdict, correctnessCount, outPath, durationMs }) {
  const say = line => console.log(line);
  say('');
  const pad = (text, width) => String(text).padEnd(width);
  const padStart = (text, width) => String(text).padStart(width);
  say(`${pad('workload', 22)} ${padStart('body', 8)} ${padStart('median', 9)} ${padStart('p90', 9)} ` +
    `${pad('process medians', 22)} ${padStart('spread', 9)}`);
  for (const workload of workloads) {
    const medians = workload.processMedians.map(value => Number.isFinite(value) ? value.toFixed(2) : 'n/a').join(', ');
    const spread = workload.spreadRatio === null ? 'n/a' : `${workload.spreadRatio.toFixed(2)}x`;
    say(`${pad(workload.name, 22)} ${padStart(`${workload.bodyKb}KB`, 8)} ` +
      `${padStart(formatMs(workload.medianMs), 9)} ${padStart(formatMs(workload.p90Ms), 9)} ` +
      `${pad(medians, 22)} ${padStart(spread, 9)}`);
  }
  if (retry.length) {
    say('');
    say('retry scenarios (exact attempt counts asserted):');
    for (const group of retry) {
      const medians = group.processMedians.map(value => Number.isFinite(value) ? value.toFixed(2) : 'n/a').join(', ');
      say(`  ${pad(group.name, 20)} median ${formatMs(group.medianMs)}  per-process ${medians}  ` +
        `calls/request ${CALLS_PER_REQUEST[group.mode]}  ${group.failures.length ? 'FAIL' : 'OK'}`);
    }
  }
  say('');
  say(`streaming passthrough: ${streaming.ok ? 'OK' : 'FAIL'} (${streaming.approximateFrames} frames, ` +
    `${streaming.bytes} bytes, exact body match${streaming.passthrough ? ', frame passthrough enabled' : ''})`);
  if (soak) {
    const bucket = phase => soak.buckets.find(item => item.phase === phase);
    say(`soak ${config.soakSeconds}s @${config.soakConcurrency} (warmup ${(soak.warmupMs / 1000).toFixed(2)}s): ` +
      `start ${formatMs(bucket(0)?.medianMs)} / mid ${formatMs(bucket(1)?.medianMs)} / end ${formatMs(bucket(2)?.medianMs)} · ` +
      `${soak.requests} req (${soak.rps} rps) · RSS ${soak.growth.rssGrowthMb ?? 'n/a'}MB · ` +
      `heap ${soak.growth.heapGrowthMb ?? 'n/a'}MB${soak.growth.gcAvailable ? '' : ' (gc unavailable)'} → ${soak.pass ? 'OK' : 'FAIL'}`);
  } else {
    say('soak: off (pass --soak-seconds N)');
  }
  say('');
  say(`correctness: ${correctnessCount.ok ? 'OK' : 'FAILED'} (${correctnessCount.validated} responses validated, ` +
    `${correctnessCount.failures} failure${correctnessCount.failures === 1 ? '' : 's'}, upstream payloads hash-verified)`);
  if (!verdict.pass) {
    for (const failure of verdict.failures.slice(0, 20)) say(`  ✗ ${failure}`);
    if (verdict.failures.length > 20) say(`  … ${verdict.failures.length - 20} more`);
  }
  say(`run: ${(durationMs / 1000).toFixed(2)}s · result: ${outPath}`);
  say(`verdict: ${verdict.pass ? 'PASS' : 'FAIL'} (exit ${verdict.exitCode})`);
}

function printUsage(message) {
  if (message) console.error(`bench_proxy: ${message}`);
  const lines = [
    'usage: node tests/bench_proxy.mjs [options]',
    '',
    'A fresh child process hosts the proxy (zero-latency fake upstream) for every repetition;',
    'the parent process generates all HTTP load. Results are medians of per-process medians.',
    '',
    'options:',
    '  --quick                  fast CI profile: 2 processes, 6 rounds, 2 sizes (under ~20s)',
    '  --workers N              fresh proxy processes per perf repetition (1-16, default 3)',
    '  --rounds N               measured requests per workload per process (1-200, default 15)',
    '  --warmup N               unmeasured warmup requests per workload (0-20, default 2)',
    '  --sizes LIST             comma-separated KB request sizes (default 16,64,256,1024)',
    '  --retry-processes N      fresh processes per retry scenario (1-8, default 2)',
    '  --soak-seconds N         time-based soak duration in seconds, 0 = off (default 0)',
    '  --soak-concurrency N     concurrent requests during the soak (1-64, default 8)',
    '  --max-rss-growth-mb N    soak RSS growth threshold after warmup (default 192)',
    '  --max-heap-growth-mb N   soak post-GC retained-heap growth threshold (default 12)',
    '  --max-median-ms N        opt-in latency budget for the largest payload (default: none)',
    '  --max-spread-ratio N     opt-in inter-process spread budget, max/min (default: none)',
    '  --out PATH               JSON result path (default .bench/proxy-<utc>.json)',
    '  --help, -h               show this help',
    '',
    'exit codes: 0 pass · 1 correctness or budget failure · 2 usage error',
    '',
    'correctness asserts (a faster run that changes any result fails):',
    '  * every JSON response deep-equals the expected validated body',
    '  * every streaming passthrough body matches byte for byte',
    '  * every child saw the identical rewritten payload (SHA-256 + summary)',
    '  * retry scenarios issue exactly 1, 2 or 4 failed attempts per request',
  ];
  for (const line of lines) console.log(line);
}

// ---------------------------------------------------------------- entry point

export async function main(argv = process.argv.slice(2)) {
  let config;
  try {
    config = resolveConfig(parseArgs(argv));
  } catch (error) {
    if (error instanceof UsageError) { printUsage(error.message); return EXIT.usage; }
    throw error;
  }
  if (config.help) { printUsage(); return EXIT.pass; }

  const startedAt = Date.now();
  const env = envInfo();
  const git = gitInfo({ quick: config.quick });
  const source = sourceInfo();
  const correctnessFailures = [];
  const perfProcesses = [];
  const retryGroups = [];
  let passedResponses = 0;

  console.log(`claude-cheat perf harness · node ${env.node} · ${git.shortCommit || 'unknown commit'}` +
    `${git.dirty ? ' (dirty tree)' : ''} · ${env.cpus} cpus`);
  console.log(`${config.quick ? 'quick' : 'standard'} profile · ${config.workers} fresh processes × ` +
    `${config.rounds} rounds (warmup ${config.warmup}) · sizes ${config.sizes.join(',')}KB · ` +
    `${config.retryProcesses} process(es)/retry scenario` +
    `${config.soakSeconds ? ` · soak ${config.soakSeconds}s @${config.soakConcurrency}` : ''}`);

  for (let index = 0; index < config.workers; index++) {
    const result = await runPerfProcess(index, config);
    perfProcesses.push(result);
    passedResponses += result.workloads.reduce((sum, workload) => sum + workload.sent, 0);
    for (const workload of result.workloads)
      for (const failure of workload.failures) correctnessFailures.push(`[perf p${index} ${workload.name}] ${failure.message}`);
    correctnessFailures.push(...result.failures);
  }
  const workloadNames = [...new Set(perfProcesses.flatMap(process => process.workloads.map(workload => workload.name)))];
  const workloads = workloadNames.map(name => {
    const group = perfProcesses.map(process => ({ index: process.index,
      ...process.workloads.find(workload => workload.name === name) }));
    const aggregate = aggregateProcesses(group);
    return { name, kind: group[0].kind, bodyKb: group[0].bodyKb, requestsPerProcess: group[0].sent,
      ...aggregate, samplesByProcess: group.map(item => item.samples.map(sample => round(sample))),
      expectation: { hash: group[0].expectation.hash, bytes: group[0].expectation.bytes } };
  });

  for (const mode of RETRY_MODES) {
    const group = [];
    for (let index = 0; index < config.retryProcesses; index++) {
      const result = await runRetryProcess({ index, mode, config });
      group.push(result);
      passedResponses += result.sent;
      correctnessFailures.push(...result.failures);
    }
    const aggregate = aggregateProcesses(group);
    const representative = group[0];
    retryGroups.push({ name: `retry ${mode}`, mode, callsPerRequest: CALLS_PER_REQUEST[mode],
      attemptsPerProcess: representative?.sent ?? 0, ...aggregate,
      failures: group.flatMap(item => item.failures),
      samplesByProcess: group.map(item => item.samples.map(sample => round(sample))),
      expectation: representative ? { hash: representative.expectation.hash,
        bytes: representative.expectation.bytes } : null,
      metrics: group.map(item => ({ index: item.index, attempts: item.metrics?.attempts ?? null,
        retries: item.metrics?.retries ?? null, failed: item.metrics?.failed ?? null })) });
  }
  const streaming = await runStreamingCase();
  passedResponses += 1;
  correctnessFailures.push(...streaming.failures);
  const soak = config.soakSeconds > 0 ? await runSoak(config) : null;
  if (soak) passedResponses += soak.totalRequests;

  const verdict = evaluateVerdict({ correctnessFailures, workloads, retry: retryGroups, soak,
    thresholds: { maxMedianMs: config.maxMedianMs, maxSpreadRatio: config.maxSpreadRatio } });
  const durationMs = Date.now() - startedAt;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const outPath = config.out ? resolve(process.cwd(), config.out) :
    join(REPO_ROOT, '.bench', `proxy-${stamp}.json`);
  const result = { schema: 'claude-cheat/bench-proxy/1', generatedAt: new Date().toISOString(),
    durationMs, quick: config.quick, config,
    env, source, git,
    correctness: { ok: correctnessFailures.length === 0, validatedResponses: passedResponses,
      failures: correctnessFailures },
    workloads,
    processes: perfProcesses.map(process => ({ index: process.index, memoryStart: process.memoryStart,
      memoryEnd: process.memoryEnd, metrics: process.metrics, upstream: process.upstream,
      reportConfig: process.reportConfig })),
    retry: retryGroups, streaming, soak, verdict };
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, `${JSON.stringify(result, null, 2)}\n`);
  printSummary({ config, workloads, retry: retryGroups, streaming, soak, verdict,
    correctnessCount: { ok: correctnessFailures.length === 0, validated: passedResponses,
      failures: correctnessFailures.length },
    outPath, durationMs });
  return verdict.exitCode;
}

if (import.meta.main) {
  main().then(code => { process.exitCode = code; }).catch(error => {
    console.error(error?.stack || String(error));
    process.exitCode = EXIT.fail;
  });
}
