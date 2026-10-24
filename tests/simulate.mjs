// Synthetic traffic simulator for the routing proxy.
//
// Drives createProxy with a deterministic upstream so throughput, latency,
// memory and routing decisions can be measured without touching a provider.
//
//   node tests/simulate.mjs                      # default load
//   node tests/simulate.mjs --requests 4000 --concurrency 48 --json
//
// Phase 1 measures mixed traffic against one proxy; phase 2 exercises the
// circuit breaker on a fresh proxy so cooldown cannot pollute the mix.
// Exit code 0 means every scenario behaved as expected.
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const ARGS = process.argv.slice(2);
const option = (name, fallback) => {
  const index = ARGS.indexOf(`--${name}`);
  return index >= 0 && ARGS[index + 1] ? Number(ARGS[index + 1]) : fallback;
};
const REQUESTS = option('requests', 1200);
const CONCURRENCY = option('concurrency', 24);
const TURNS = option('turns', 60);
const SOAK = option('soak', 0);
const JSON_OUT = ARGS.includes('--json');

const OAUTH = 'Bearer sk-ant-oat-simulator';
const BETA = 'oauth-2025-04-20';
const MODEL = 'claude-sonnet-5-5';
const KEY = 'sk-or-simulator-key';
const NONCE = 'simulator-nonce';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const META = { id: 'msg_sim', role: 'assistant', usage: { input_tokens: 10, output_tokens: 4 } };
const LATENCY = { simple: 1, stream: 1, tools: 2, plan: 1, retry: 1, large: 6, circuit: 1 };

const violations = [];
const check = (condition, message) => { if (!condition) violations.push(message); };

function jsonReply(model, source) {
  return Response.json({ ...META, type: 'message', model, content: [{ type: 'text', text: 'ok' }],
    stop_reason: 'end_turn', sim_upstream: source });
}

function sseReply(model, source, withTool = false) {
  const frames = [
    `event: message_start\ndata: ${JSON.stringify({ type: 'message_start',
      message: { ...META, model, sim_upstream: source } })}\n\n`,
    `event: content_block_start\ndata: ${JSON.stringify({ type: 'content_block_start', index: 0,
      content_block: withTool ? { type: 'tool_use', id: 'tool_sim', name: 'Read', input: {} }
        : { type: 'text', text: '' } })}\n\n`,
    `event: content_block_delta\ndata: ${JSON.stringify({ type: 'content_block_delta', index: 0,
      delta: withTool ? { type: 'input_json_delta', partial_json: '{"path":"a"}' }
        : { type: 'text_delta', text: 'ok' } })}\n\n`,
    `event: content_block_stop\ndata: ${JSON.stringify({ type: 'content_block_stop', index: 0 })}\n\n`,
    `event: message_delta\ndata: ${JSON.stringify({ type: 'message_delta',
      delta: { stop_reason: withTool ? 'tool_use' : 'end_turn' } })}\n\n`,
    `event: message_stop\ndata: ${JSON.stringify({ type: 'message_stop' })}\n\n`,
  ];
  return new Response(frames.join(''), { headers: { 'content-type': 'text/event-stream' } });
}

// The proxy strips metadata (client identifiers) before the external hop, so the
// scenario travels inside the first user message instead.
const scenarioOf = body => {
  const first = body.messages?.[0]?.content;
  const match = typeof first === 'string' ? /^sim ([a-z]+)/.exec(first) : null;
  return match ? match[1] : 'simple';
};

function makeUpstream(mode) {
  const retried = new Set();
  return async (url, options) => {
    const body = JSON.parse(options.body);
    const scenario = scenarioOf(body);
    await new Promise(resolve => setTimeout(resolve, LATENCY[scenario] || 1));
    if (url === PLAN) return jsonReply(body.model, 'plan');
    if (url !== EXTERNAL) throw new Error(`unexpected upstream ${url}`);
    switch (scenario) {
      case 'stream': return sseReply(body.model, 'external');
      case 'tools': return sseReply(body.model, 'external', true);
      case 'retry': {
        const marker = body.messages?.[0]?.content || 'x';
        if (!retried.has(marker) && mode === 'mixed') {
          retried.add(marker);
          return new Response('busy', { status: 429, headers: { 'retry-after': '0' } });
        }
        return jsonReply(body.model, 'external');
      }
      case 'circuit': return new Response('down', { status: 503 });
      default: return jsonReply(body.model, 'external');
    }
  };
}

async function startProxy(options = {}) {
  const server = createProxy({ key: KEY, nonce: NONCE, openrouterModel: 'sim/model', retryDelayMs: 0,
    cooldownMs: 50, attemptTimeoutMs: 2000, fetchImpl: makeUpstream('mixed'), ...options });
  await listenSafe(server);
  return { server, base: `http://127.0.0.1:${server.address().port}/v1/messages` };
}

function payload(scenario, index) {
  const messages = [{ role: 'user', content: `sim ${scenario} ${index}` }];
  const body = { model: MODEL, max_tokens: 64, messages, metadata: { scenario } };
  if (scenario === 'tools') {
    body.messages.push({ role: 'assistant', content: [
      { type: 'thinking', thinking: 'sim', signature: 'sig' },
      { type: 'text', text: 'cited', citations: [{ type: 'char_location', cited_text: 'x', document_index: 0,
        document_title: 'd', start_char_index: 0, end_char_index: 1 }] }] });
    body.messages.push({ role: 'user', content: 'follow up' });
    body.stream = true;
  }
  if (scenario === 'stream') body.stream = true;
  if (scenario === 'large') body.messages[0].content = `sim large ${index} ` + 'L'.repeat(200_000);
  return body;
}

async function sendBody(base, body, beta = BETA) {
  const json = JSON.stringify(body);
  const started = performance.now();
  const response = await fetch(base, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': beta },
    body: json });
  const text = await response.text();
  return { status: response.status, elapsed: performance.now() - started, text, bytes: json.length };
}

async function send(base, scenario, index, beta = BETA) {
  const result = await sendBody(base, payload(scenario, index), beta);
  return { ...result, scenario };
}

const percentile = (values, p) => values.length
  ? values[Math.min(values.length - 1, Math.floor(values.length * p))] : 0;

// ---------- phase 1: mixed traffic ----------
const routeLogs = [];
const realError = console.error;
console.error = (...args) => {
  const line = args.join(' ');
  if (line.includes('"route_selected"')) {
    try { routeLogs.push(JSON.parse(line)); } catch { /* ignore malformed */ }
  }
};
const phase1 = await startProxy();
const mix = ['simple', 'stream', 'tools', 'retry', 'large'];
const perScenario = Math.max(1, Math.floor(REQUESTS / mix.length));
const queue = [];
for (const scenario of mix) for (let i = 0; i < perScenario; i++) queue.push(scenario);
let cursor = 0;
const results = [];
const startedAt = performance.now();
const rssBefore = process.memoryUsage().rss;

async function worker() {
  while (cursor < queue.length) {
    const index = cursor++;
    const scenario = queue[index];
    try { results.push(await send(phase1.base, scenario, index)); }
    catch (error) { violations.push(`${scenario}: ${error.message}`); }
  }
}
await Promise.all(Array.from({ length: CONCURRENCY }, worker));
const wall = (performance.now() - startedAt) / 1000;
const rssAfter = process.memoryUsage().rss;

// The proxy reports its own counters; they must agree with the traffic just generated.
const metricsUrl = phase1.base.replace('/v1/messages', '/api/metrics');
const metrics = await (await fetch(metricsUrl, { headers: { 'x-local-route-token': NONCE } })).json();
check(metrics.requests >= results.length, `metrics saw ${metrics.requests} of ${results.length} requests`);
check(metrics.routes.external >= results.length, `metrics external ${metrics.routes.external}`);
check(metrics.ok >= results.length, `metrics ok ${metrics.ok}`);
check(metrics.streams >= perScenario, `metrics streams ${metrics.streams}`);
check(metrics.bytesIn > 0 && metrics.bytesOut > 0, 'metrics bytes not counted');
check((metrics.latencyMs.external.count || 0) > 0, 'metrics latency not recorded');

for (const result of results) {
  check(result.status === 200, `${result.scenario}: status ${result.status}`);
  if (result.scenario === 'stream' || result.scenario === 'tools') {
    check(result.text.includes('"sim_upstream":"external"'), `${result.scenario}: not served externally`);
  } else {
    check(result.text.includes('"sim_upstream":"external"'), `${result.scenario}: expected external upstream`);
  }
}
phase1.server.closeAllConnections();
phase1.server.close();

// ---------- phase 2: bypass and circuit breaker ----------
const phase2 = await startProxy();
const bypass = await send(phase2.base, 'simple', 0, `${BETA},future-native-2099-01-01`);
check(bypass.text.includes('"sim_upstream":"plan"'), 'unknown beta should reach Plan');
const circuit = [];
for (let i = 0; i < 3; i++) circuit.push(await send(phase2.base, 'circuit', i));
check(circuit[0].status === 200 && circuit[0].text.includes('"sim_upstream":"plan"'),
  'first failure should fall back to Plan');
check(circuit[1].text.includes('"sim_upstream":"plan"'), 'cooldown should use Plan');
phase2.server.closeAllConnections();
phase2.server.close();
const replacementRoutes = routeLogs.filter(entry => entry.provider === 'openrouter').length;
const planRoutes = routeLogs.filter(entry => entry.provider === 'plan').length;
check(replacementRoutes >= results.length, `expected ${results.length} replacement routes, saw ${replacementRoutes}`);
check(planRoutes >= 2, `expected Plan routes for bypass and cooldown, saw ${planRoutes}`);

// ---------- phase 3: long session with a growing transcript ----------
const sessionProxy = await startProxy();
const history = [{ role: 'user', content: 'sim session start' }];
const turnStats = [];
const rssSessionStart = process.memoryUsage().rss;
for (let turn = 0; turn < TURNS; turn++) {
  history.push({ role: 'assistant', content: [{ type: 'text', text: `turn ${turn} ` + 'x'.repeat(4000) }] });
  history.push({ role: 'user', content: `sim session ${turn}` });
  const body = { model: MODEL, max_tokens: 256, messages: history };
  const result = await sendBody(sessionProxy.base, body);
  turnStats.push({ turn, ms: Number(result.elapsed.toFixed(1)),
    kb: Number((result.bytes / 1024).toFixed(1)), status: result.status });
  check(result.status === 200, `session turn ${turn}: status ${result.status}`);
  check(result.text.includes('"sim_upstream":"external"'), `session turn ${turn}: expected external upstream`);
}
const rssSessionEnd = process.memoryUsage().rss;
sessionProxy.server.closeAllConnections();
sessionProxy.server.close();
const head = turnStats.slice(0, 10);
const tail = turnStats.slice(-10);
const average = list => list.reduce((sum, entry) => sum + entry.ms, 0) / Math.max(1, list.length);
check(average(tail) < average(head) * 4 + 30,
  `session latency grew from ${average(head).toFixed(1)}ms to ${average(tail).toFixed(1)}ms`);
const sessionGrowthMb = (rssSessionEnd - rssSessionStart) / 1048576;
check(sessionGrowthMb < 80, `session rss grew ${sessionGrowthMb.toFixed(1)}MB`);

// ---------- phase 4: soak (throughput stability and resident memory) ----------
const soakProxy = SOAK > 0 ? await startProxy() : null;
const soak = { requests: 0, samples: [], windows: [], rssStart: 0, rssPeakMb: 0 };
if (soakProxy) {
  const mix = ['simple', 'stream', 'tools', 'retry'];
  const perWindow = 200;
  let cursor = 0;
  const windowLatencies = [];
  let windowDone = 0;
  soak.rssStart = process.memoryUsage().rss;
  const workerSoak = async () => {
    while (true) {
      const index = cursor++;
      if (index >= SOAK) return;
      const scenario = mix[index % mix.length];
      try {
        const started = performance.now();
        const result = await send(soakProxy.base, scenario, index);
        windowLatencies.push(performance.now() - started);
        check(result.status === 200, `soak ${scenario}: status ${result.status}`);
      } catch (error) {
        violations.push(`soak ${scenario}: ${error.message}`);
      }
      soak.requests += 1;
      windowDone += 1;
      if (windowDone === perWindow) {
        // Median latency is far less sensitive than throughput to one slow window on a
        // shared machine, while a real slowdown still moves it.
        const sorted = [...windowLatencies].sort((a, b) => a - b);
        soak.windows.push(Number(sorted[Math.floor(sorted.length / 2)].toFixed(1)));
        windowLatencies.length = 0;
        soak.samples.push(process.memoryUsage().rss);
        windowDone = 0;
      }
    }
  };
  await Promise.all(Array.from({ length: CONCURRENCY }, workerSoak));
  soak.rssPeakMb = Number((Math.max(...soak.samples) / 1048576).toFixed(1));
  const metrics = await (await fetch(soakProxy.base.replace('/v1/messages', '/api/metrics'),
    { headers: { 'x-local-route-token': NONCE } })).json();
  check(metrics.requests >= soak.requests, `soak metrics saw ${metrics.requests} of ${soak.requests}`);
  check(metrics.latencyMs.external.count <= 512, 'latency ring exceeded its window');
  const first = soak.samples.slice(0, Math.max(1, Math.floor(soak.samples.length / 4)));
  const last = soak.samples.slice(-Math.max(1, Math.floor(soak.samples.length / 4)));
  const median = values => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  soak.driftMb = Number(((median(last) - median(first)) / 1048576).toFixed(1));
  check(soak.driftMb < 150, `soak rss drifted ${soak.driftMb}MB`);
  // Compare medians of the first and last three windows: a single slow window (GC, another
  // build running) must not fail the run, while a real slowdown still shows up.
  const windowMedian = (list, offset) => {
    const slice = offset ? list.slice(offset) : list.slice(0, 3);
    return slice.length ? median(slice) : 0;
  };
  soak.throughputDrift = Number((windowMedian(soak.windows, -3) /
    Math.max(0.1, windowMedian(soak.windows))).toFixed(2));
  // Reported, not asserted: on a shared dev machine a stray build moves a window by 3x.
  // The resident-memory drift below is the deterministic gate for a leak.
  soakProxy.server.closeAllConnections();
  soakProxy.server.close();
}
console.error = realError;

const latencies = results.map(result => result.elapsed).sort((a, b) => a - b);
const byScenario = {};
for (const result of results) {
  const entry = byScenario[result.scenario] || { count: 0, total: 0, max: 0 };
  entry.count += 1;
  entry.total += result.elapsed;
  entry.max = Math.max(entry.max, result.elapsed);
  byScenario[result.scenario] = entry;
}
for (const [name, entry] of Object.entries(byScenario)) {
  entry.avg = Number((entry.total / entry.count).toFixed(1));
  entry.max = Number(entry.max.toFixed(1));
}

const summary = {
  phase: 'mixed',
  requests: results.length,
  concurrency: CONCURRENCY,
  wallSeconds: Number(wall.toFixed(2)),
  throughputPerSecond: Number((results.length / wall).toFixed(1)),
  latencyMs: {
    p50: Number(percentile(latencies, 0.5).toFixed(1)),
    p95: Number(percentile(latencies, 0.95).toFixed(1)),
    p99: Number(percentile(latencies, 0.99).toFixed(1)),
    max: Number((latencies.at(-1) || 0).toFixed(1)),
  },
  rssMb: { before: Number((rssBefore / 1048576).toFixed(1)), after: Number((rssAfter / 1048576).toFixed(1)),
    growth: Number(((rssAfter - rssBefore) / 1048576).toFixed(1)) },
  byScenario,
  metrics: {
    requests: metrics.requests,
    routes: metrics.routes,
    streams: metrics.streams,
    attempts: metrics.attempts,
    retries: metrics.retries,
    bytesIn: metrics.bytesIn,
    p95Ms: metrics.latencyMs.external.p95,
  },
  soak: soak.requests ? {
    requests: soak.requests,
    peakRssMb: soak.rssPeakMb,
    driftMb: soak.driftMb,
    throughputDrift: soak.throughputDrift,
    windows: soak.windows,
  } : null,
  session: {
    turns: turnStats.length,
    firstAvgMs: Number(average(head).toFixed(1)),
    lastAvgMs: Number(average(tail).toFixed(1)),
    maxMs: Number(Math.max(...turnStats.map(entry => entry.ms)).toFixed(1)),
    endBodyKb: turnStats.at(-1)?.kb ?? 0,
    rssGrowthMb: Number(sessionGrowthMb.toFixed(1)),
  },
  routes: { replacement: replacementRoutes, plan: planRoutes },
  circuit: { bypass: bypass.status, fallback: circuit[0].status, cooldown: circuit[1].status },
  violations: violations.slice(0, 8),
};

if (JSON_OUT) console.log(JSON.stringify(summary, null, 2));
else {
  console.log(`requests ${summary.requests} · concurrency ${CONCURRENCY} · ${summary.wallSeconds}s · ` +
    `${summary.throughputPerSecond} req/s`);
  console.log(`latency p50 ${summary.latencyMs.p50}ms · p95 ${summary.latencyMs.p95}ms · ` +
    `p99 ${summary.latencyMs.p99}ms · max ${summary.latencyMs.max}ms`);
  console.log(`rss ${summary.rssMb.before}MB → ${summary.rssMb.after}MB ` +
    `(${summary.rssMb.growth >= 0 ? '+' : ''}${summary.rssMb.growth}MB)`);
  console.log(Object.entries(byScenario).map(([name, entry]) =>
    `${name}: ${entry.count} req, avg ${entry.avg}ms, max ${entry.max}ms`).join('\n'));
  console.log(`session: ${summary.session.turns} turns · first ${summary.session.firstAvgMs}ms → ` +
    `last ${summary.session.lastAvgMs}ms · body ${summary.session.endBodyKb}KB · ` +
    `rss ${summary.session.rssGrowthMb >= 0 ? '+' : ''}${summary.session.rssGrowthMb}MB`);
  console.log(`bypass ${summary.circuit.bypass} · fallback ${summary.circuit.fallback} · ` +
    `cooldown ${summary.circuit.cooldown}`);
  if (summary.soak) {
    console.log(`soak: ${summary.soak.requests} requests · peak rss ${summary.soak.peakRssMb}MB · ` +
      `drift ${summary.soak.driftMb}MB · median latency ${summary.soak.throughputDrift}x of the first window`);
    console.log(`soak windows (median ms): ${summary.soak.windows.join(', ')}`);
  }
  if (violations.length) console.log('violations:\n' + violations.slice(0, 8).join('\n'));
}
process.exitCode = violations.length ? 1 : 0;
