// Stream ownership: a client that goes away must get every resource handed back — the
// upstream reader, the attempt timers, the request accounting, and the inflight slot a
// handover drains on. A client may leave before its request body is even complete.
import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'stream-ownership-nonce';
const MODEL = 'claude-sonnet-5-5';
const UPSTREAM_MODEL = 'ownership/model';
const OAUTH = 'Bearer sk-ant-oat-ownership';
const BETA = 'oauth-2025-04-20';
const META = { id: 'msg_ownership', role: 'assistant', usage: { input_tokens: 1, output_tokens: 1 } };
const HEADERS = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
  authorization: OAUTH, 'anthropic-beta': BETA };
const AUTH = { 'x-local-route-token': NONCE };

const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
const requestBody = (extra = {}) => JSON.stringify({ model: MODEL, max_tokens: 1024, stream: true,
  messages: [{ role: 'user', content: 'hold the stream open' }], ...extra });
const sse = stream => new Response(stream, { headers: { 'content-type': 'text/event-stream' } });

// A fake upstream that records what the proxy does with it: pulls ask for more data,
// cancels hand the body back, and the signal is the one the proxy passes to fetch.
function instrumentedStream(parts) {
  const encoder = new TextEncoder();
  const record = { pulls: 0, cancels: 0, aborted: false, abortReason: undefined };
  return {
    record,
    watch(signal) {
      const note = () => { record.aborted = true; record.abortReason = signal.reason; };
      if (signal.aborted) note();
      else signal.addEventListener('abort', note, { once: true });
    },
    stream: new ReadableStream({
      start(controller) { for (const part of parts) controller.enqueue(encoder.encode(part)); },
      // Never resolves: the provider is still generating and leaves the stream open.
      pull() { record.pulls += 1; return new Promise(() => {}); },
      cancel() { record.cancels += 1; },
    }),
  };
}

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

// A late abort() on an already-aborted controller is a no-op, so a leaked attempt timer
// cannot be observed through the signal; the timer registry itself is the only witness.
function trackTimers(t, delays) {
  const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;
  const created = [];
  globalThis.setTimeout = function (callback, delay, ...args) {
    const entry = { delay, cleared: false, fired: false, handle: null };
    entry.handle = realSetTimeout.call(globalThis, (...callbackArgs) => {
      entry.fired = true;
      if (typeof callback === 'function') callback(...callbackArgs);
    }, delay, ...args);
    created.push(entry);
    return entry.handle;
  };
  globalThis.clearTimeout = function (handle) {
    const entry = created.find(item => item.handle === handle);
    if (entry) entry.cleared = true;
    return realClearTimeout.call(globalThis, handle);
  };
  t.after(() => {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
  });
  return () => created.filter(entry => delays.includes(entry.delay));
}

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-ownership', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, attemptTimeoutMs: 5000, handoverTimeoutMs: 2000, onHandover: () => {}, ...config });
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
    base: `http://127.0.0.1:${port}/v1/messages`,
    metrics: async () => {
      const response = await fetch(`http://127.0.0.1:${port}/api/metrics`, { headers: AUTH });
      assert.equal(response.status, 200, 'the proxy must keep serving metrics after a client disconnects');
      return response.json();
    },
    // A released turn lets the drain finish: 200, not a 409 revert after the cap.
    drain: async () => {
      const response = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: AUTH });
      const report = await response.json();
      assert.equal(response.status, 200, `a released turn must drain: ${JSON.stringify(report)}`);
      assert.equal(report.drained, true);
      assert.equal(report.inflight, 0);
      return report;
    },
  };
}

test('a passthrough client that disconnects mid-stream releases its upstream, timers, and turn',
  { timeout: 10000 }, async t => {
    const attemptTimeoutMs = 1000, attemptCeilingMs = 4000;
    const attemptTimers = trackTimers(t, [attemptTimeoutMs, attemptCeilingMs]);
    const upstream = instrumentedStream([
      frame('message_start', { message: { ...META, model: UPSTREAM_MODEL } }),
      frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
      frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'hello' } }),
    ]);
    const calls = [];
    const { base, metrics, drain } = await startProxy(t, { streamPassthrough: true,
      attemptTimeoutMs, attemptCeilingMs,
      fetchImpl: async (url, options) => {
        calls.push(url);
        upstream.watch(options.signal);
        return sse(upstream.stream);
      } });
    const client = new AbortController();
    const response = await fetch(base, { method: 'POST', headers: HEADERS, body: requestBody(), signal: client.signal });
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.match(new TextDecoder().decode(first.value), /hello/);
    assert.ok(attemptTimers().some(entry => entry.delay === attemptTimeoutMs),
      'the attempt timer must be armed before the client leaves');
    client.abort();
    await waitFor(() => upstream.record.cancels >= 1, 'the upstream body to be cancelled');
    await waitFor(() => attemptTimers().every(entry => entry.cleared), 'the attempt timers to be cleared');
    assert.equal(upstream.record.aborted, true, 'the upstream signal must be aborted');
    assert.equal(upstream.record.abortReason, 'client_close', 'the client, not a timer, ended the attempt');
    assert.ok(attemptTimers().every(entry => !entry.fired), 'no attempt timeout may fire after the client is gone');
    const payload = await metrics();
    assert.equal(payload.requests, 1);
    assert.equal(payload.aborted, 1, 'the aborted turn must be recorded exactly once');
    assert.equal(payload.ok, 0);
    assert.equal(payload.failed, 1);
    assert.equal(payload.attempts, 1, 'a client that is gone must not trigger a retry');
    assert.equal(payload.retries, 0);
    assert.equal(payload.routes.plan, 0, 'a client that is gone must not trigger a fallback');
    assert.equal(calls.length, 1, 'the upstream must be called once');
    await drain();
  });

test('a buffered client that disconnects before the answer is forwarded releases the same way',
  { timeout: 10000 }, async t => {
    const attemptTimeoutMs = 1000, attemptCeilingMs = 4000;
    const attemptTimers = trackTimers(t, [attemptTimeoutMs, attemptCeilingMs]);
    const upstream = instrumentedStream([
      frame('message_start', { message: { ...META, model: UPSTREAM_MODEL } }),
      frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
      frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'hello' } }),
    ]);
    const calls = [];
    const { base, metrics, drain } = await startProxy(t, { attemptTimeoutMs, attemptCeilingMs,
      fetchImpl: async (url, options) => {
        calls.push(url);
        upstream.watch(options.signal);
        return sse(upstream.stream);
      } });
    const client = new AbortController();
    const pending = fetch(base, { method: 'POST', headers: HEADERS, body: requestBody(), signal: client.signal });
    // Buffered mode forwards nothing until the whole answer validates, so the proof the
    // proxy is mid-turn is the upstream read it has already started.
    await waitFor(() => upstream.record.pulls >= 1, 'the proxy to read the open upstream');
    client.abort();
    await assert.rejects(pending, undefined, 'an aborted request must not resolve with a response');
    await waitFor(() => upstream.record.cancels >= 1, 'the upstream body to be cancelled');
    await waitFor(() => attemptTimers().length > 0 && attemptTimers().every(entry => entry.cleared),
      'the attempt timers to be cleared');
    assert.equal(upstream.record.aborted, true, 'the upstream signal must be aborted');
    assert.equal(upstream.record.abortReason, 'client_close', 'the client, not a timer, ended the attempt');
    assert.ok(attemptTimers().every(entry => !entry.fired), 'no attempt timeout may fire after the client is gone');
    const payload = await metrics();
    assert.equal(payload.requests, 1);
    assert.equal(payload.aborted, 1, 'the aborted turn must be recorded exactly once');
    assert.equal(payload.ok, 0);
    assert.equal(payload.failed, 1);
    assert.equal(payload.attempts, 1, 'a client that is gone must not trigger a retry');
    assert.equal(payload.retries, 0);
    assert.equal(payload.routes.plan, 0, 'a client that is gone must not trigger a fallback');
    assert.equal(calls.length, 1);
    await drain();
  });

test('a client that disconnects while sending its body still returns its turn to a handover',
  { timeout: 10000 }, async t => {
    let upstreamCalls = 0;
    const { server, port, metrics, drain } = await startProxy(t, { handoverTimeoutMs: 2000,
      fetchImpl: async () => {
        upstreamCalls += 1;
        return Response.json({ ...META, type: 'message', model: UPSTREAM_MODEL,
          content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' });
      } });
    const arrived = new Promise(resolve => server.once('request', resolve));
    // Half a JSON body over a chunked request, then the socket dies. The proxy has taken
    // its inflight slot but never reaches routing, so only the close path can return it.
    const failure = await new Promise(resolve => {
      const request = http.request({ host: '127.0.0.1', port, path: '/v1/messages', method: 'POST',
        headers: HEADERS }, response => {
        response.resume();
        resolve(new Error(`unexpected response ${response.statusCode}`));
      });
      request.on('error', resolve);
      request.write('{"model":"claude-sonnet-5-5",');
      arrived.then(() => request.destroy());
    });
    assert.ok(failure instanceof Error, 'destroying the socket must fail the client request');
    assert.equal(upstreamCalls, 0, 'an unfinished body is never routed upstream');
    const payload = await metrics();
    assert.equal(payload.requests, 0, 'a request that never reached routing is not a routed request');
    assert.equal(payload.aborted, 0, 'the abort counter follows routed requests only');
    await drain();
  });

test('an early rejection returns its inflight slot so a later handover can drain',
  { timeout: 10000 }, async t => {
    const { base, metrics, drain } = await startProxy(t, {
      fetchImpl: async () => { throw new Error('an early rejection must never reach the upstream'); } });
    const response = await fetch(base, { method: 'POST', headers: HEADERS, body: '{' });
    assert.equal(response.status, 400);
    await response.text();
    const payload = await metrics();
    assert.equal(payload.requests, 0);
    await drain();
  });

test('a client that disconnects while the socket is backpressured resolves the drain wait',
  { timeout: 15000 }, async t => {
    const attemptTimeoutMs = 4000, attemptCeilingMs = 8000;
    const attemptTimers = trackTimers(t, [attemptTimeoutMs, attemptCeilingMs]);
    const parts = [
      frame('message_start', { message: { ...META, model: UPSTREAM_MODEL } }),
      frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    ];
    // 400 × 8 KiB overflows even a generously sized socket buffer, so the proxy must wait
    // for drain instead of buffering the whole reply in memory.
    for (let index = 0; index < 400; index += 1) {
      parts.push(frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'x'.repeat(8192) } }));
    }
    const upstream = instrumentedStream(parts);
    const { server, base, metrics, drain } = await startProxy(t, { streamPassthrough: true,
      attemptTimeoutMs, attemptCeilingMs,
      fetchImpl: async (url, options) => {
        upstream.watch(options.signal);
        return sse(upstream.stream);
      } });
    // res.write() returning false is the proxy's backpressure signal; a registered 'drain'
    // listener is the wait it parks on until the socket moves or closes.
    let backpressured = false;
    let target = null;
    server.on('request', (req, res) => {
      target = res;
      const write = res.write.bind(res);
      res.write = (chunk, encoding, callback) => {
        const accepted = write(chunk, encoding, callback);
        if (!accepted) backpressured = true;
        return accepted;
      };
    });
    const client = new AbortController();
    const response = await fetch(base, { method: 'POST', headers: HEADERS,
      body: requestBody({ max_tokens: 64000 }), signal: client.signal });
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.ok(first.value?.byteLength > 0, 'the first frames must reach the client before the abort');
    await waitFor(() => backpressured, 'the socket buffer to fill');
    await waitFor(() => target && target.listenerCount('drain') >= 1, 'the proxy to wait for drain');
    client.abort();
    // The wait must resolve from the close event, not stay parked: its drain listener goes away.
    await waitFor(() => target.listenerCount('drain') === 0, 'the drain wait to resolve on close');
    await waitFor(() => upstream.record.cancels >= 1, 'the upstream body to be cancelled');
    await waitFor(() => attemptTimers().length > 0 && attemptTimers().every(entry => entry.cleared),
      'the attempt timers to be cleared');
    assert.equal(upstream.record.aborted, true);
    assert.equal(upstream.record.abortReason, 'client_close');
    assert.ok(attemptTimers().every(entry => !entry.fired), 'no attempt timeout may fire after the client is gone');
    const payload = await metrics();
    assert.equal(payload.requests, 1);
    assert.equal(payload.aborted, 1);
    assert.equal(payload.attempts, 1, 'a backpressured client that is gone must not trigger a retry');
    assert.equal(payload.routes.plan, 0);
    await reader.cancel().catch(() => {});
    await drain();
  });

test('closing the listener mid-stream leaves no unhandled rejection or stray timer',
  { timeout: 10000 }, async t => {
    const attemptTimeoutMs = 1000, attemptCeilingMs = 4000;
    const attemptTimers = trackTimers(t, [attemptTimeoutMs, attemptCeilingMs]);
    const problems = [];
    const onRejection = reason => problems.push(reason);
    const onException = error => problems.push(error);
    process.on('unhandledRejection', onRejection);
    process.on('uncaughtException', onException);
    t.after(() => {
      process.off('unhandledRejection', onRejection);
      process.off('uncaughtException', onException);
    });
    const upstream = instrumentedStream([
      frame('message_start', { message: { ...META, model: UPSTREAM_MODEL } }),
      frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
      frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'hello' } }),
    ]);
    const { server, base } = await startProxy(t, { streamPassthrough: true,
      attemptTimeoutMs, attemptCeilingMs,
      fetchImpl: async (url, options) => {
        upstream.watch(options.signal);
        return sse(upstream.stream);
      } });
    const client = new AbortController();
    const response = await fetch(base, { method: 'POST', headers: HEADERS, body: requestBody(), signal: client.signal });
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    await reader.read();
    // The listener goes away while the turn is still streaming: sockets are cut under the
    // proxy, which must still hand back its reader and timers instead of leaking them.
    const closed = new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    server.closeAllConnections();
    await withTimeout(closed, 2000, 'the listener to finish closing');
    await reader.cancel().catch(() => {});
    await waitFor(() => upstream.record.cancels >= 1, 'the upstream body to be cancelled');
    await waitFor(() => attemptTimers().length > 0 && attemptTimers().every(entry => entry.cleared),
      'the attempt timers to be cleared');
    assert.equal(upstream.record.aborted, true);
    assert.ok(attemptTimers().every(entry => !entry.fired), 'no attempt timeout may fire after the listener closes');
    // A stray rejection or socket error surfaces on a tick after the teardown, not during it.
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual(problems, []);
  });
