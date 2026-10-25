// Streaming passthrough is opt-in: it trades guaranteed fallback for first-token latency.
// The default keeps whole-body validation, which is what the rest of the suite assumes.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const MODEL = 'claude-sonnet-5-5';
const NONCE = 'stream-nonce';
const BETA = 'oauth-2025-04-20';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const META = { id: 'msg_stream', role: 'assistant', usage: { input_tokens: 5, output_tokens: 2 } };
const FRAME_DELAY_MS = 60;

const frame = (type, fields) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;

function frames(model, { text = 'hello', extraFrames = [] } = {}) {
  return [
    frame('message_start', { message: { ...META, model } }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text } }),
    frame('content_block_stop', { index: 0 }),
    frame('message_delta', { delta: { stop_reason: 'end_turn' } }),
    frame('message_stop', {}),
    ...extraFrames,
  ];
}

async function start({ passthrough = false, chunks = null, planCalls = [] } = {}) {
  const server = createProxy({
    key: 'sk-or-stream', nonce: NONCE, openrouterModel: 'stream/model', retryDelayMs: 0,
    attemptTimeoutMs: 8000, streamPassthrough: passthrough,
    fetchImpl: async (url, request) => {
      if (url === PLAN) { planCalls.push(url); return jsonReply('plan'); }
      if (url !== EXTERNAL) throw new Error('unexpected url ' + url);
      const body = JSON.parse(request.body);
      const pieces = chunks || frames(body.model);
      const stream = new ReadableStream({
        async start(controller) {
          const encoder = new TextEncoder();
          for (const piece of pieces) {
            await new Promise(resolve => setTimeout(resolve, FRAME_DELAY_MS));
            controller.enqueue(encoder.encode(piece));
          }
          controller.close();
        },
      });
      return new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
    },
  });
  await listenSafe(server);
  return { server, base: `http://127.0.0.1:${server.address().port}/v1/messages` };
}

function jsonReply(model) {
  return Response.json({ ...META, type: 'message', model, content: [{ type: 'text', text: 'plan ok' }],
    stop_reason: 'end_turn' });
}

async function request(base) {
  const startedAt = performance.now();
  const response = await fetch(base, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: 'Bearer sk-ant-oat-stream', 'anthropic-beta': BETA },
    body: JSON.stringify({ model: MODEL, max_tokens: 64, stream: true,
      messages: [{ role: 'user', content: 'hello' }] }) });
  const reader = response.body.getReader();
  const first = await reader.read();
  const ttfb = performance.now() - startedAt;
  let body = new TextDecoder().decode(first.value || new Uint8Array());
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    body += new TextDecoder().decode(value);
  }
  return { ttfb, total: performance.now() - startedAt, status: response.status, body };
}

test('by default the proxy buffers the whole stream before answering', async () => {
  const { server, base } = await start();
  try {
    const result = await request(base);
    assert.equal(result.status, 200);
    assert.ok(result.ttfb > result.total * 0.85, `ttfb ${result.ttfb} of ${result.total}`);
    assert.match(result.body, /message_stop/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('passthrough delivers the first token while the provider is still generating', async () => {
  const { server, base } = await start({ passthrough: true });
  try {
    const result = await request(base);
    assert.equal(result.status, 200);
    assert.ok(result.ttfb < result.total * 0.6, `ttfb ${result.ttfb} of ${result.total}`);
    assert.match(result.body, /message_stop/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('passthrough keeps a recoverable failure recoverable before any token', async () => {
  // The wrong model is rejected on the first frame, so nothing has been sent and the
  // normal retry + Plan fallback still happens.
  const planCalls = [];
  const broken = [frame('message_start', { message: { ...META, model: 'other/model' } })];
  const { server, base } = await start({ passthrough: true, chunks: broken, planCalls });
  try {
    const result = await request(base);
    assert.equal(result.status, 200);
    assert.match(result.body, /plan ok/);
    assert.equal(planCalls.length, 1);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('passthrough still delivers a control-stop reply that has no content', async () => {
  // A refusal carries no usable text, so nothing may be forwarded early; the client must
  // still receive the whole stream instead of an empty 200.
  const refusal = [
    frame('message_start', { message: { ...META, model: 'stream/model' } }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_stop', { index: 0 }),
    frame('message_delta', { delta: { stop_reason: 'refusal' } }),
    frame('message_stop', {}),
  ];
  const { server, base } = await start({ passthrough: true, chunks: refusal });
  try {
    const result = await request(base);
    assert.equal(result.status, 200);
    assert.match(result.body, /message_stop/);
    assert.match(result.body, /refusal/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('a transport failure after the first frame ends the stream instead of retrying', async () => {
  // The client already has bytes, so retrying would duplicate them and falling back is
  // impossible; the proxy must finish with an error frame and make exactly one call.
  const planCalls = [];
  const pieces = [
    frame('message_start', { message: { ...META, model: 'stream/model' } }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'hello' } }),
  ];
  const server = createProxy({
    key: 'sk-or-stream', nonce: NONCE, openrouterModel: 'stream/model', retryDelayMs: 0,
    attemptTimeoutMs: 5000, streamPassthrough: true,
    fetchImpl: async (url, request) => {
      if (url === PLAN) { planCalls.push(url); return jsonReply('plan'); }
      const stream = new ReadableStream({
        async start(controller) {
          const encoder = new TextEncoder();
          for (const piece of pieces) {
            controller.enqueue(encoder.encode(piece));
            await new Promise(resolve => setTimeout(resolve, 40));
          }
          controller.error(new Error('socket reset'));
        },
      });
      return new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
    },
  });
  await listenSafe(server);
  try {
    const result = await request(`http://127.0.0.1:${server.address().port}/v1/messages`);
    assert.equal(result.status, 200);
    assert.match(result.body, /hello/);
    assert.match(result.body, /upstream stream failed/);
    assert.equal(planCalls.length, 0);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('passthrough closes the stream when a later frame is invalid', async () => {
  // Text arrives first (so the client has bytes), then the protocol breaks: the proxy
  // must not fall back, because that would duplicate output the client already saw.
  const planCalls = [];
  const broken = [
    frame('message_start', { message: { ...META, model: 'stream/model' } }),
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }),
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'hello' } }),
    // A delta for a block that never started: the client already received "hello".
    frame('content_block_delta', { index: 9, delta: { type: 'text_delta', text: 'x' } }),
    frame('content_block_stop', { index: 0 }),
    frame('message_delta', { delta: { stop_reason: 'end_turn' } }),
    frame('message_stop', {}),
  ];
  const { server, base } = await start({ passthrough: true, chunks: broken, planCalls });
  try {
    const result = await request(base);
    assert.equal(result.status, 200);
    assert.match(result.body, /hello/);
    assert.match(result.body, /upstream stream failed/);
    assert.equal(planCalls.length, 0);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});
