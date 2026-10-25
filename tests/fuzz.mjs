// Fuzzer for the routing proxy: hostile bodies, paths and headers must never crash the
// process, hang the client or return something that is not JSON.
//
//   node tests/fuzz.mjs [--iterations 300] [--seed 1234]
//
// Deterministic: the same seed reproduces the same sequence.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createConnection } from 'node:net';
import { createProxy, DEEPSEEK } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const argv = process.argv.slice(2);
const value = (name, fallback) => {
  const index = argv.indexOf(name);
  return index >= 0 ? Number(argv[index + 1]) : fallback;
};
const ITERATIONS = value('--iterations', 300);
const SEED = value('--seed', 1234);

let state = SEED >>> 0;
const random = () => {
  state = (state + 0x6d2b79f5) >>> 0;
  let t = state;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = list => list[Math.floor(random() * list.length)];
const int = (max) => Math.floor(random() * max);

const NONCE = 'fuzz-token';
const MODEL = 'fuzz/model';
const META = { id: 'msg_fuzz', type: 'message', role: 'assistant', model: MODEL,
  usage: { input_tokens: 1, output_tokens: 1 } };

// A correct upstream: JSON for plain requests, SSE for stream:true, so the fuzzer measures
// request handling rather than provoking the retry loop.
const sseReply = () => {
  const frames = [
    `event: message_start\ndata: ${JSON.stringify({ type: 'message_start', message: META })}\n\n`,
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
  ];
  return new Response(frames.join(''), { headers: { 'content-type': 'text/event-stream' } });
};
const server = createProxy({
  key: 'sk-or-fuzz', nonce: NONCE, openrouterModel: MODEL,
  retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 2000, planHeaderMs: 2000, planIdleMs: 2000,
  fetchImpl: async (url, options) => {
    let body = {};
    try { body = JSON.parse(options?.body || '{}'); } catch { /* malformed client body */ }
    if (body.stream === true) return sseReply();
    return Response.json({ ...META, content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' });
  },
});
await listenSafe(server);
const port = server.address().port;
const base = `http://127.0.0.1:${port}`;

const PATHS = ['/v1/messages', '/v1/messages?beta=true', '/v1/messages/count_tokens', '/api/metrics',
  '/api/hello', '/', '/../etc/passwd', '/v1/messages/../../x', '/Ｖ１/messages', '/v1/messages?x=' + 'a'.repeat(2000),
  '/v1/messages//', '//v1/messages', '/v1/messages#fragment'];
const HEADERS = [{}, { 'content-type': 'text/plain' }, { 'content-type': 'application/json; charset=utf-16' },
  { 'x-local-route-token': NONCE }, { 'x-local-route-token': 'wrong' }, { 'x-local-route-token': '' },
  { authorization: 'Bearer ' + 'x'.repeat(4000) }, { 'anthropic-beta': 'a,'.repeat(500) },
  { 'x-forwarded-for': '127.0.0.1' }, { accept: 'text/event-stream' },
  { 'accept-encoding': 'gzip, br, zstd' }];
// Framing abuse (negative Content-Length, bogus chunked) is exercised on raw sockets below:
// fetch rewrites those headers, so a timeout there says nothing about the server.

function payload() {
  switch (int(8)) {
    case 0: return JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, messages: [{ role: 'user', content: 'x' }] });
    case 1: return JSON.stringify({ model: pick([null, 0, [], {}, '', 'x'.repeat(300)]), messages: pick([null, 'x', [], {}]) });
    case 2: return JSON.stringify({ model: 'claude-opus-5-5', max_tokens: -5, messages: [] });
    case 3: return '{"model":';
    case 4: return '['.repeat(200) + ']'.repeat(200);
    case 5: return JSON.stringify({ model: 'claude-sonnet-5-5', messages: [{ role: 'system', content: 'x' }], stream: true });
    case 6: return 'x'.repeat(int(5000));
    default: return JSON.stringify({ model: '\u0000\u0001\ud800', messages: [{ role: 'user', content: [{ type: 'text', text: 'x'.repeat(2000) }] }] });
  }
}

const STATUSES = new Set([200, 400, 401, 403, 404, 413, 422, 500, 502, 503]);
const failures = [];

for (let index = 0; index < ITERATIONS; index += 1) {
  const path = pick(PATHS);
  const method = pick(['POST', 'POST', 'POST', 'GET', 'HEAD', 'PUT', 'DELETE', 'OPTIONS']);
  const headers = { ...pick(HEADERS) };
  if (random() > 0.2) headers['x-local-route-token'] = NONCE;
  const body = method === 'GET' || method === 'HEAD' ? undefined : payload();
  try {
    const response = await fetch(base + path, { method, headers, body, redirect: 'manual',
      signal: AbortSignal.timeout(8000) });
    const text = await response.text();
    if (!STATUSES.has(response.status)) failures.push({ index, path, method, reason: 'status', got: response.status });
    // HEAD and 204/304 responses carry no body by definition.
    const bodyless = method === 'HEAD' || [204, 304].includes(response.status);
    if (!bodyless && response.headers.get('content-type')?.includes('json')) {
      try { JSON.parse(text); } catch { failures.push({ index, path, method, headers, reason: 'invalid json', text: text.slice(0, 120) }); }
    }
  } catch (error) {
    // A malformed request may be reset by the client side; a timeout or crash is a real defect.
    if (!/fetch failed|socket|ECONNRESET|UND_ERR/.test(String(error))) {
      failures.push({ index, path, method, headers, reason: 'threw', error: String(error) });
    }
  }
}

// Framing abuse on raw sockets: the server must answer or close, never hang.
const RAW = ['GET / HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n',
  'POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n{}',
  'POST /v1/messages HTTP/1.1\r\nHost: x\r\nBadHeader\r\n\r\n',
  'POST /v1/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n',
  'GET / HTTP/9.9\r\n\r\n'];
for (const raw of RAW) {
  try {
    const socket = createConnection(port, '127.0.0.1');
    await once(socket, 'connect');
    socket.write(raw);
    await Promise.race([once(socket, 'close'), new Promise(resolve => setTimeout(resolve, 3000))]);
    socket.destroy();
  } catch (error) {
    failures.push({ raw: raw.slice(0, 40), reason: 'socket', error: String(error) });
  }
}

// The proxy must still be healthy after the abuse.
const control = await fetch(base + '/v1/messages', { method: 'POST',
  headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-fuzz',
    'anthropic-beta': 'oauth-2025-04-20,interleaved-thinking-2025-05-14' },
  body: JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, messages: [{ role: 'user', content: 'ok' }] }) });
assert.equal(control.status, 200, `the proxy stopped serving after the fuzz run: ${control.status} ${await control.clone().text()}`);
const reply = await control.json();
assert.equal(reply.content[0].text, 'ok');

server.closeAllConnections();
server.close();

if (failures.length) {
  console.error(JSON.stringify({ seed: SEED, iterations: ITERATIONS, failures: failures.slice(0, 10) }, null, 2));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, seed: SEED, iterations: ITERATIONS, rawFrames: RAW.length }));
