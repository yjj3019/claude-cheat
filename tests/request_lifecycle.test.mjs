// Request lifecycle: a client that stalls mid-request must not hold the proxy's turn until
// Node's long default timeout, and a handover must report its drained state before exiting.
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'request-lifecycle-nonce';
const MODEL = 'claude-sonnet-5-5';
const UPSTREAM_MODEL = 'lifecycle/model';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json' };

const message = model => ({ id: 'msg_lifecycle', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });

async function waitFor(predicate, label, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await predicate()) return;
    if (Date.now() >= deadline) throw new Error(`timed out after ${timeoutMs}ms waiting for ${label}`);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function withTimeout(promise, timeoutMs, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timed out after ${timeoutMs}ms waiting for ${label}`)), timeoutMs);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-lifecycle', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, attemptTimeoutMs: 3000, handoverTimeoutMs: 2000, onHandover: () => {}, ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await withTimeout(new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
      2000, 'the listener to close');
  });
  await listenSafe(server);
  const port = server.address().port;
  return {
    server,
    port,
    metrics: async () => {
      const response = await fetch(`http://127.0.0.1:${port}/api/metrics`, { headers: AUTH });
      assert.equal(response.status, 200, 'the proxy must keep serving metrics');
      return response.json();
    },
    drain: async () => {
      const response = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: AUTH });
      const report = await response.json();
      assert.equal(response.status, 200, `a released turn must drain: ${JSON.stringify(report)}`);
      assert.equal(report.drained, true);
      return report;
    },
  };
}

// Writes a large body in slices with pauses, so the upload is slow but never idle longer
// than the configured cap.
function uploadSlowly(port, body, { slice, gapMs }) {
  return new Promise((resolve, reject) => {
    const request = http.request({ host: '127.0.0.1', port, path: '/v1/messages', method: 'POST',
      headers: { ...HEADERS, 'content-length': Buffer.byteLength(body) } }, response => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { text += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body: text }));
    });
    request.on('error', reject);
    let offset = 0;
    const writeNext = () => {
      if (offset >= body.length) { request.end(); return; }
      const end = Math.min(offset + slice, body.length);
      const chunk = body.slice(offset, end);
      offset = end;
      request.write(chunk, error => {
        if (error) { reject(error); return; }
        setTimeout(writeNext, gapMs);
      });
    };
    writeNext();
  });
}

test('a stalled request body times out on idle, is counted aborted, and a handover then drains',
  { timeout: 10000 }, async t => {
    const log = t.mock.method(console, 'error', () => {});
    let upstreamCalls = 0;
    const { port, metrics, drain } = await startProxy(t, { requestIdleTimeoutMs: 120,
      fetchImpl: async () => { upstreamCalls += 1; throw new Error('a stalled body must never be routed'); } });
    const socket = net.connect(port, '127.0.0.1');
    t.after(() => socket.destroy());
    socket.on('error', () => {});
    let closedAt = null;
    socket.once('close', () => { closedAt = Date.now(); });
    await new Promise((resolve, reject) => {
      socket.once('connect', resolve);
      socket.once('error', reject);
    });
    const started = Date.now();
    // One valid chunk of a chunked body, then silence: the request is live but idle.
    socket.write('POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n' +
      `x-local-route-token: ${NONCE}\r\ncontent-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n` +
      '8\r\n{"model"\r\n');
    await waitFor(() => closedAt !== null, 'the idle body timeout to drop the socket', 3000);
    assert.ok(closedAt - started >= 100, `the socket was dropped early (${closedAt - started}ms)`);
    assert.equal(upstreamCalls, 0, 'an unfinished body is never routed');
    const payload = await metrics();
    assert.equal(payload.aborted, 1, 'the self-timeout is counted as an aborted request');
    assert.equal(payload.requests, 0, 'a body that never finished is not a routed request');
    const entries = log.mock.calls.map(call => JSON.parse(call.arguments[0]));
    const timeoutEntry = entries.find(entry => entry.event === 'client_body_timeout');
    assert.ok(timeoutEntry, 'a self-timeout must not be reported as peer interference');
    assert.equal(timeoutEntry.idle_ms, 120);
    assert.equal(JSON.stringify(entries).includes(NONCE), false, 'diagnostics must not leak the token');
    await drain();
  });

test('a slow but live large upload below the idle cap still completes',
  { timeout: 20000 }, async t => {
    const calls = [];
    const { port, metrics } = await startProxy(t, { requestIdleTimeoutMs: 300,
      fetchImpl: async (url, options) => {
        calls.push(JSON.parse(options.body).messages[0].content.length);
        return Response.json(message(UPSTREAM_MODEL));
      } });
    // 2 MiB of request body delivered in 64 KiB slices with a 40ms pause between them: never
    // idle for the cap, so the idle timer must not cut a legitimate upload.
    const body = JSON.stringify({ model: MODEL, max_tokens: 8, stream: false,
      messages: [{ role: 'user', content: 'x'.repeat(2 * 1024 * 1024 - 200) }] });
    const started = Date.now();
    const response = await uploadSlowly(port, body, { slice: 64 * 1024, gapMs: 40 });
    const elapsed = Date.now() - started;
    assert.equal(response.status, 200, response.body);
    assert.ok(elapsed >= 800, `the upload must really be slow (${elapsed}ms)`);
    assert.equal(calls.length, 1, 'the slow upload must reach the upstream');
    const payload = await metrics();
    assert.equal(payload.requests, 1);
    assert.equal(payload.aborted, 0, 'a live upload is not an aborted request');
  });

test('a stalled request header is dropped by the header idle timeout',
  { timeout: 10000 }, async t => {
    const { port, metrics, drain } = await startProxy(t, { requestIdleTimeoutMs: 150,
      fetchImpl: async () => { throw new Error('a stalled header must never reach the upstream'); } });
    const socket = net.connect(port, '127.0.0.1');
    t.after(() => socket.destroy());
    socket.on('error', () => {});
    let received = '';
    socket.on('data', chunk => { received += chunk.toString(); });
    await new Promise((resolve, reject) => {
      socket.once('connect', resolve);
      socket.once('error', reject);
    });
    const started = Date.now();
    socket.write('POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n');
    const closedAt = await withTimeout(new Promise(resolve => socket.once('close', () => resolve(Date.now()))),
      3000, 'the header idle timeout to drop the socket');
    assert.ok(closedAt - started >= 100, `the header was dropped early (${closedAt - started}ms)`);
    assert.match(received, /408 Request Timeout/, 'the client learns the request was refused, not reset');
    const payload = await metrics();
    assert.equal(payload.requests, 0, 'a request whose headers never finished is never counted');
    await drain();
  });

test('a handover exits only after its drained report has flushed', { timeout: 10000 }, async t => {
  const { server, port } = await startProxy(t, { onHandover: null,
    fetchImpl: async () => { throw new Error('the handover test never routes a request'); } });
  const realExit = process.exit;
  const exits = [];
  let finished = false, finishedAt = null;
  process.exit = code => { exits.push({ code, at: performance.now(), finished }); };
  t.after(() => { process.exit = realExit; });
  server.on('request', (req, res) => {
    if (req.url !== '/api/handover') return;
    res.once('finish', () => { finished = true; finishedAt = performance.now(); });
    // Hold the report back so an exit scheduled at drain time would win the race.
    const end = res.end.bind(res);
    res.end = (...args) => { setTimeout(() => end(...args), 150); return res; };
  });
  const response = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: AUTH });
  const report = await response.json();
  assert.equal(response.status, 200);
  assert.equal(report.drained, true);
  await waitFor(() => exits.length === 1, 'the process exit after the drain', 3000);
  assert.equal(exits[0].code, 0);
  assert.equal(exits[0].finished, true, 'the process must not exit before the report flushed');
  assert.ok(exits[0].at > finishedAt, 'the exit must be scheduled after the flush, not at drain time');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(exits.length, 1, 'the exit must run exactly once');
});

test('a handover write that never flushes still exits within the bounded linger',
  { timeout: 10000 }, async t => {
    const { server, port } = await startProxy(t, { onHandover: null,
      fetchImpl: async () => { throw new Error('the handover test never routes a request'); } });
    const realExit = process.exit;
    const exits = [];
    process.exit = code => { exits.push({ code, at: Date.now() }); };
    t.after(() => { process.exit = realExit; });
    server.on('request', (req, res) => {
      if (req.url !== '/api/handover') return;
      res.end = () => res; // the drained report never leaves the process
    });
    const client = new AbortController();
    const started = Date.now();
    const pending = fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: AUTH,
      signal: client.signal }).catch(error => error);
    await waitFor(() => exits.length === 1, 'the bounded exit', 3000);
    const elapsed = Date.now() - started;
    assert.equal(exits[0].code, 0);
    assert.ok(elapsed >= 200 && elapsed <= 2500, `the fallback linger must be bounded (${elapsed}ms)`);
    client.abort();
    await pending;
    assert.equal(exits.length, 1);
  });

test('the injected handover hook runs once and never schedules a process exit',
  { timeout: 10000 }, async t => {
    let handedOver = 0;
    const { port } = await startProxy(t, { onHandover: () => { handedOver += 1; },
      fetchImpl: async () => { throw new Error('the handover test never routes a request'); } });
    const realExit = process.exit;
    const exits = [];
    process.exit = code => { exits.push(code); };
    t.after(() => { process.exit = realExit; });
    const response = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: AUTH });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).drained, true);
    assert.equal(handedOver, 1, 'the injected hook must run exactly once');
    await new Promise(resolve => setTimeout(resolve, 400));
    assert.deepEqual(exits, [], 'with a hook the process must not schedule its own exit');
  });

test('a request idle timeout of 0 disables the body idle timer', { timeout: 10000 }, async t => {
  const { port, drain } = await startProxy(t, { requestIdleTimeoutMs: 0,
    fetchImpl: async () => { throw new Error('an unfinished body must never be routed'); } });
  const socket = net.connect(port, '127.0.0.1');
  t.after(() => socket.destroy());
  socket.on('error', () => {});
  let closed = false;
  socket.once('close', () => { closed = true; });
  await new Promise((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  socket.write('POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n' +
    `x-local-route-token: ${NONCE}\r\ncontent-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n` +
    '8\r\n{"model"\r\n');
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(closed, false, '0 must disable the idle timeout');
  socket.destroy();
  await waitFor(() => closed, 'the client socket to close', 2000);
  await drain(); // the close path still releases the turn
});

test('invalid lifecycle and retry settings are rejected at creation', () => {
  const base = { key: 'sk-or-lifecycle', nonce: NONCE, openrouterModel: UPSTREAM_MODEL };
  assert.throws(() => createProxy({ ...base, requestIdleTimeoutMs: -1 }));
  assert.throws(() => createProxy({ ...base, requestIdleTimeoutMs: 'soon' }));
  assert.throws(() => createProxy({ ...base, retryMaxWaitMs: -1 }));
  assert.throws(() => createProxy({ ...base, retryMaxWaitMs: Number.NaN }));
});
