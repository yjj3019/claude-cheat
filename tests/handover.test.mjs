// Handover: a running stream survives, the listener stops accepting, and the caller gets a
// drained reply so a fresh proxy can bind the same port.
import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import { createProxy, DEEPSEEK } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'handover-token';
const MODEL = 'handover/model';
const META = { id: 'msg_handover', type: 'message', role: 'assistant', model: MODEL,
  usage: { input_tokens: 1, output_tokens: 1 } };
const frame = (type, body) => `event: ${type}\ndata: ${JSON.stringify({ type, ...body })}\n\n`;

function sse(frames, { delayMs = 0 } = {}) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    async start(controller) {
      for (const item of frames) {
        controller.enqueue(encoder.encode(item));
        if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs));
      }
      controller.close();
    },
  }), { headers: { 'content-type': 'text/event-stream' } });
}

test('a drain that hits the cap reverts and keeps serving instead of cutting the turn', async t => {
  // Cleanup runs even when an assertion fails: a leaked interval would pin the event loop.
  const disposables = [];
  t.after(() => {
    for (const dispose of disposables) dispose();
  });
  // A valid but endless stream: frames keep flowing so the turn never finishes.
  const encoder = new TextEncoder();
  let timer = null;
  const stalled = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frame('message_start', { message: META })));
      controller.enqueue(encoder.encode(frame('content_block_start', { index: 0,
        content_block: { type: 'text', text: '' } })));
      timer = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(frame('content_block_delta', { index: 0,
            delta: { type: 'text_delta', text: 'tick' } })));
        } catch { clearInterval(timer); }
      }, 150);
    },
    cancel() { clearInterval(timer); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const server = createProxy({ key: 'sk-or-handover', nonce: NONCE, openrouterModel: MODEL,
    streamPassthrough: true, handoverTimeoutMs: 300, fetchImpl: async () => stalled,
    onHandover: () => {} });
  await listenSafe(server);
  // Close the listener on the way out: a reverted drain leaves it open, and an open
  // listener pins the event loop, so the suite would hang instead of reporting.
  disposables.push(() => clearInterval(timer), () => server.closeAllConnections(),
                    () => { if (server.listening) server.close(); });
  const port = server.address().port;
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-handover', 'anthropic-beta': 'interleaved-thinking-2025-05-14' };
  const body = JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, stream: true,
    messages: [{ role: 'user', content: 'stall' }] });
  const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: 'POST', headers, body });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  await reader.read();
  const started = Date.now();
  const handover = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST',
    headers: { 'x-local-route-token': NONCE } });
  const report = await handover.json();
  assert.equal(handover.status, 409, 'a revert must be signalled, not a fake success');
  assert.equal(report.drained, false);
  assert.equal(report.reverted, true, 'the cap must revert the handover, not kill the turn');
  assert.ok(Date.now() - started >= 250, 'it waited for the cap, not forever');
  // The stream is still flowing untouched: frames keep arriving after the revert.
  const still = await Promise.race([
    reader.read().then(result => ({ value: result.value, timeout: false })),
    new Promise(resolve => setTimeout(() => resolve({ value: null, timeout: true }), 1000)),
  ]);
  assert.equal(still.timeout, false, 'the active stream must survive the reverted handover');
  // Service resumed: new requests are served again instead of 503.
  const resumed = await fetch(`http://127.0.0.1:${port}/api/metrics`, { method: 'GET',
    headers: { 'x-local-route-token': NONCE } });
  assert.equal(resumed.status, 200);
  await reader.cancel().catch(() => {});
});

test('concurrent handover requests share one drain run and one report', async t => {
  const disposables = [];
  t.after(() => {
    for (const dispose of disposables) dispose();
  });
  let handedOver = 0;
  const slowStream = [
    frame('message_start', { message: META }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'part-one ' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'part-two' } }),
    frame('content_block_stop', { index: 0 }),
    frame('message_delta', { delta: { stop_reason: 'end_turn' } }),
    frame('message_stop', {}),
  ];
  const server = createProxy({ key: 'sk-or-handover', nonce: NONCE, openrouterModel: MODEL,
    streamPassthrough: true,
    fetchImpl: async () => sse(slowStream, { delayMs: 60 }),
    onHandover: () => { handedOver += 1; } });
  await listenSafe(server);
  disposables.push(() => server.closeAllConnections(),
                    () => { if (server.listening) server.close(); });
  const port = server.address().port;
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-handover', 'anthropic-beta': 'interleaved-thinking-2025-05-14' };
  const body = JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, stream: true,
    messages: [{ role: 'user', content: 'stream while handing over' }] });
  const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: 'POST', headers, body });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  await reader.read();
  // Fire both before either resolves: they must join the same drain, not race.
  const [first, second] = await Promise.all([
    fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: { 'x-local-route-token': NONCE } }),
    fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: { 'x-local-route-token': NONCE } }),
  ]);
  while (!(await reader.read()).done);
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  const one = await first.json();
  const two = await second.json();
  assert.deepEqual(two, one, 'duplicate requests must get the same report');
  assert.equal(one.drained, true);
  assert.equal(handedOver, 1, 'the exit hook must run exactly once');
});

test('concurrent handovers during a stalled turn share one revert and keep serving', async t => {
  const disposables = [];
  t.after(() => { for (const dispose of disposables) dispose(); });
  const encoder = new TextEncoder();
  let timer = null;
  let handedOver = 0;
  const stalled = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frame('message_start', { message: META })));
      controller.enqueue(encoder.encode(frame('content_block_start', { index: 0,
        content_block: { type: 'text', text: '' } })));
      timer = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(frame('content_block_delta', { index: 0,
            delta: { type: 'text_delta', text: 'tick' } })));
        } catch { clearInterval(timer); }
      }, 50);
    },
    cancel() { clearInterval(timer); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const server = createProxy({ key: 'sk-or-handover', nonce: NONCE, openrouterModel: MODEL,
    streamPassthrough: true, handoverTimeoutMs: 250, fetchImpl: async () => stalled,
    onHandover: () => { handedOver += 1; } });
  await listenSafe(server);
  disposables.push(() => clearInterval(timer), () => server.closeAllConnections(),
                    () => { if (server.listening) server.close(); });
  const port = server.address().port;
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-handover', 'anthropic-beta': 'interleaved-thinking-2025-05-14' };
  const body = JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, stream: true,
    messages: [{ role: 'user', content: 'stall through a double handover' }] });
  const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: 'POST', headers, body });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  await reader.read();
  const [first, second] = await Promise.all([
    fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: { 'x-local-route-token': NONCE } }),
    fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST', headers: { 'x-local-route-token': NONCE } }),
  ]);
  assert.equal(first.status, 409);
  assert.equal(second.status, 409);
  const one = await first.json();
  const two = await second.json();
  assert.deepEqual(two, one, 'both callers must see the same revert report');
  assert.equal(one.reverted, true);
  assert.equal(handedOver, 0, 'a revert must not run the exit hook');
  // The proxy is serving again, not stuck draining or closed.
  const metrics = await fetch(`http://127.0.0.1:${port}/api/metrics`, { headers: { 'x-local-route-token': NONCE } });
  assert.equal(metrics.status, 200);
  await reader.cancel().catch(() => {});
});
test('a handover retried after a revert drains normally once the turn is gone', async t => {
  const disposables = [];
  t.after(() => { for (const dispose of disposables) dispose(); });
  // The same endless stream as the revert case: only the client can end it.
  const encoder = new TextEncoder();
  let timer = null;
  const stalled = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frame('message_start', { message: META })));
      controller.enqueue(encoder.encode(frame('content_block_start', { index: 0,
        content_block: { type: 'text', text: '' } })));
      timer = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(frame('content_block_delta', { index: 0,
            delta: { type: 'text_delta', text: 'tick' } })));
        } catch { clearInterval(timer); }
      }, 50);
    },
    cancel() { clearInterval(timer); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const server = createProxy({ key: 'sk-or-handover', nonce: NONCE, openrouterModel: MODEL,
    streamPassthrough: true, handoverTimeoutMs: 200, fetchImpl: async () => stalled,
    onHandover: () => {} });
  await listenSafe(server);
  disposables.push(() => clearInterval(timer), () => server.closeAllConnections(),
                    () => { if (server.listening) server.close(); });
  const port = server.address().port;
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-handover', 'anthropic-beta': 'interleaved-thinking-2025-05-14' };
  const response = await fetch(`http://127.0.0.1:${port}/v1/messages`, { method: 'POST', headers,
    body: JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, stream: true,
      messages: [{ role: 'user', content: 'stall then leave' }] }) });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  await reader.read();
  const reverted = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST',
    headers: { 'x-local-route-token': NONCE } });
  assert.equal(reverted.status, 409);
  // The client gives up: its turn is over, so the proxy is free to drain next time.
  await reader.cancel().catch(() => {});
  let drained = null;
  for (let attempt = 0; attempt < 40 && !drained; attempt++) {
    const retry = await fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST',
      headers: { 'x-local-route-token': NONCE } });
    if (retry.status === 200) drained = await retry.json();
    else await new Promise(resolve => setTimeout(resolve, 25));
  }
  assert.ok(drained, 'the retry after the turn ended must drain');
  assert.equal(drained.drained, true);
});

test('a stream in flight finishes while the listener drains and stops accepting', async t => {
  let handedOver = false;
  const slowStream = [
    frame('message_start', { message: META }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'part-one ' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'part-two' } }),
    frame('content_block_stop', { index: 0 }),
    frame('message_delta', { delta: { stop_reason: 'end_turn' } }),
    frame('message_stop', {}),
  ];
  const server = createProxy({ key: 'sk-or-handover', nonce: NONCE, openrouterModel: MODEL,
    streamPassthrough: true,
    fetchImpl: async () => sse(slowStream, { delayMs: 60 }),
    onHandover: () => { handedOver = true; } });
  await listenSafe(server);
  t.after(() => {
    server.closeAllConnections();
    if (server.listening) server.close();
  });
  const port = server.address().port;
  const base = `http://127.0.0.1:${port}/v1/messages`;
  const headers = { 'content-type': 'application/json', 'x-local-route-token': NONCE,
    authorization: 'Bearer sk-ant-oat-handover', 'anthropic-beta': 'interleaved-thinking-2025-05-14' };
  const body = JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 1024, stream: true,
    messages: [{ role: 'user', content: 'stream while handing over' }] });

  const response = await fetch(base, { method: 'POST', headers, body });
  assert.equal(response.status, 200);

  const reader = response.body.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /part-one/);

  // Ask for the handover while still consuming the stream: a real client keeps reading, and
  // the drain wait can only finish if the socket keeps moving.
  const handoverPromise = fetch(`http://127.0.0.1:${port}/api/handover`, { method: 'POST',
    headers: { 'x-local-route-token': NONCE } });
  let rest = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    rest += new TextDecoder().decode(value);
  }
  assert.match(rest, /part-two/);
  assert.match(rest, /message_stop/);
  const handover = await handoverPromise;
  assert.equal(handover.status, 200);
  const report = await handover.json();
  assert.equal(report.drained, true);
  assert.equal(handedOver, true, 'the injected exit hook must run');

  // The listener is closed: a new connection is refused rather than queued.
  await assert.rejects(async () => {
    const probe = await fetch(`http://127.0.0.1:${port}/api/metrics`, { headers: { 'x-local-route-token': NONCE } });
    assert.equal(probe.status, 200);
  });
  server.closeAllConnections();
});
