// Micro-benchmark for the proxy request path.
//
//   node tests/bench_proxy.mjs
//
// Sends a pre-serialized body of increasing size through createProxy with a
// zero-latency upstream, so the number is proxy overhead (parse, rewrite,
// re-serialize, buffered reply) rather than provider time.
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'bench-nonce';
const KEY = 'sk-or-bench';
const OAUTH = 'Bearer sk-ant-oat-bench';
const BETA = 'oauth-2025-04-20';
const MODEL = 'claude-sonnet-5-5';
const SIZES = [16, 64, 256, 1024];
const ROUNDS = 14;

const reply = model => JSON.stringify({ id: 'msg', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn',
  usage: { input_tokens: 1, output_tokens: 1 } });

let upstreamBytes = 0;
const server = createProxy({
  key: KEY, nonce: NONCE, openrouterModel: 'bench/model', retryDelayMs: 0, attemptTimeoutMs: 5000,
  fetchImpl: async (url, options) => {
    const body = JSON.parse(options.body);
    upstreamBytes = options.body.length;
    return new Response(reply(body.model), { headers: { 'content-type': 'application/json' } });
  },
});
await listenSafe(server);
const base = `http://127.0.0.1:${server.address().port}/v1/messages`;

function payload(bytes) {
  const filler = 'x'.repeat(Math.max(0, bytes * 1024 - 512));
  return JSON.stringify({ model: MODEL, max_tokens: 256,
    messages: [{ role: 'user', content: filler }], tools: [
      { name: 'Read', description: 'read a file', input_schema: { type: 'object',
        properties: { path: { type: 'string' } }, required: ['path'] } }] });
}

async function once(body) {
  const cpuBefore = process.cpuUsage();
  const started = performance.now();
  const response = await fetch(base, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': BETA },
    body });
  await response.text();
  const wall = performance.now() - started;
  const cpu = process.cpuUsage(cpuBefore);
  return { wall, cpuMs: (cpu.user + cpu.system) / 1000 };
}

const rows = [];
for (const kb of SIZES) {
  const body = payload(kb);
  await once(body); // warm up paths and buffers
  const samples = [];
  for (let round = 0; round < ROUNDS; round++) samples.push(await once(body));
  const walls = samples.map(sample => sample.wall).sort((a, b) => a - b);
  const cpus = samples.map(sample => sample.cpuMs).sort((a, b) => a - b);
  const median = values => values[Math.floor(values.length / 2)];
  rows.push({ kb, requestKb: Number((body.length / 1024).toFixed(1)),
    upstreamKb: Number((upstreamBytes / 1024).toFixed(1)),
    medianMs: Number(median(walls).toFixed(2)),
    p90Ms: Number(walls[Math.floor(walls.length * 0.9)].toFixed(2)),
    cpuMs: Number(median(cpus).toFixed(2)) });
  const row = rows.at(-1);
  console.log(`request ${String(kb).padStart(5)}KB → upstream ${String(row.upstreamKb).padStart(7)}KB · ` +
    `wall ${String(row.medianMs).padStart(5)}ms · cpu ${String(row.cpuMs).padStart(5)}ms · p90 ${row.p90Ms}ms`);
}

server.closeAllConnections();
server.close();
const worst = rows.at(-1);
console.log(`overhead at ${worst.kb}KB: ${(worst.medianMs / worst.kb * 1000).toFixed(1)}µs per KB · ` +
  `${(worst.kb / 1024 / (worst.medianMs / 1000)).toFixed(1)} MB/s`);

// --- targeted cases: long history and a retried attempt (the retry loop reuses one payload) ---
function historyPayload(messages, kbEach = 2) {
  const list = [];
  for (let index = 0; index < messages; index++) {
    list.push({ role: index % 2 ? 'assistant' : 'user',
      content: [{ type: 'text', text: `m${index} ` + 'h'.repeat(kbEach * 1024) }] });
  }
  return JSON.stringify({ model: MODEL, max_tokens: 256, messages: list });
}

async function timedRun(label, body, { failFirst = false, alwaysFail = false, rounds = 12 } = {}) {
  const seen = new Set();
  let marker = 0;
  const server = createProxy({
    key: KEY, nonce: NONCE, openrouterModel: 'bench/model', retryDelayMs: 0, attemptTimeoutMs: 5000,
    cooldownMs: 0,
    fetchImpl: async (url, options) => {
      const model = JSON.parse(options.body).model;
      if (url.includes('openrouter.ai') && alwaysFail) return new Response('down', { status: 503 });
      if (failFirst) {
        const key = String(++marker);
        if (!seen.has(key)) { seen.add(key); return new Response('boom', { status: 500 }); }
      }
      return new Response(reply(model), { headers: { 'content-type': 'application/json' } });
    },
  });
  await listenSafe(server);
  const target = `http://127.0.0.1:${server.address().port}/v1/messages`;
  const send = async () => {
    const started = performance.now();
    const response = await fetch(target, { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
        authorization: OAUTH, 'anthropic-beta': BETA }, body });
    await response.text();
    return performance.now() - started;
  };
  await send();
  const samples = [];
  for (let round = 0; round < rounds; round++) samples.push(await send());
  samples.sort((a, b) => a - b);
  server.closeAllConnections();
  server.close();
  console.log(`${label}: median ${samples[Math.floor(samples.length / 2)].toFixed(2)}ms · ` +
    `body ${(body.length / 1024).toFixed(0)}KB`);
  return samples[Math.floor(samples.length / 2)];
}

await timedRun('history 120 turns', historyPayload(120));
await timedRun('history 120 turns, 1 retry', historyPayload(120), { failFirst: true });
await timedRun('1MB body', payload(1024));
await timedRun('1MB body, 1 retry', payload(1024), { failFirst: true });
await timedRun('1MB body, 4 failed attempts', payload(1024), { alwaysFail: true });
