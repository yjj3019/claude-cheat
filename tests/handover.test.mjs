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

test('a stream in flight finishes while the listener drains and stops accepting', async () => {
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
