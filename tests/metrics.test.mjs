// Metrics endpoint and counters: the dashboard reads these to show routing load.
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'metrics-nonce';
const MODEL = 'claude-sonnet-5-5';
const BETA = 'oauth-2025-04-20';
const OAUTH = 'Bearer sk-ant-oat-metrics';

function message(model) {
  return { id: 'msg_metrics', type: 'message', role: 'assistant', model,
    content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn',
    usage: { input_tokens: 5, output_tokens: 2 } };
}

function sse(model) {
  return [
    `event: message_start\ndata: ${JSON.stringify({ type: 'message_start',
      message: { id: 'msg_metrics', type: 'message', role: 'assistant', model,
        usage: { input_tokens: 5, output_tokens: 2 } } })}\n\n`,
    `event: content_block_start\ndata: ${JSON.stringify({ type: 'content_block_start', index: 0,
      content_block: { type: 'text', text: '' } })}\n\n`,
    `event: content_block_delta\ndata: ${JSON.stringify({ type: 'content_block_delta', index: 0,
      delta: { type: 'text_delta', text: 'ok' } })}\n\n`,
    `event: content_block_stop\ndata: ${JSON.stringify({ type: 'content_block_stop', index: 0 })}\n\n`,
    `event: message_delta\ndata: ${JSON.stringify({ type: 'message_delta',
      delta: { stop_reason: 'end_turn' } })}\n\n`,
    `event: message_stop\ndata: ${JSON.stringify({ type: 'message_stop' })}\n\n`,
  ].join('');
}

async function start(options = {}) {
  const failures = new Set();
  const server = createProxy({
    key: 'sk-or-metrics', nonce: NONCE, openrouterModel: 'metrics/model', retryDelayMs: 0,
    attemptTimeoutMs: 4000, cooldownMs: 0,
    fetchImpl: async (url, request) => {
      const body = JSON.parse(request.body);
      if (options.failAlways && url.includes('openrouter.ai')) {
        return new Response('down', { status: 503 });
      }
      if (options.failOnce && url.includes('openrouter.ai') && body.messages?.[0]?.content === 'retry-me') {
        if (!failures.has('retry-me')) {
          failures.add('retry-me');
          return new Response('busy', { status: 500 });
        }
      }
      if (body.stream === true) {
        return new Response(sse(body.model), { headers: { 'content-type': 'text/event-stream' } });
      }
      return new Response(JSON.stringify(message(body.model)),
        { headers: { 'content-type': 'application/json' } });
    },
    ...options,
  });
  await listenSafe(server);
  const port = server.address().port;
  return { server, port, base: `http://127.0.0.1:${port}/v1/messages`,
    metrics: `http://127.0.0.1:${port}/api/metrics` };
}

async function send(base, { beta = BETA, model = MODEL, content = 'hello', stream = false } = {}) {
  const response = await fetch(base, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': beta },
    body: JSON.stringify({ model, max_tokens: 32, stream, messages: [{ role: 'user', content }] }) });
  await response.text();
  return response.status;
}

test('metrics endpoint requires the session token', async () => {
  const { server, metrics } = await start();
  try {
    const denied = await fetch(metrics);
    assert.equal(denied.status, 403);
    await denied.text();
    const allowed = await fetch(metrics, { headers: { 'x-local-route-token': NONCE } });
    assert.equal(allowed.status, 200);
    const payload = await allowed.json();
    assert.equal(payload.requests, 0);
    assert.deepEqual(payload.routes, { external: 0, plan: 0 });
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('metrics identify the loaded routing configuration without credentials', async () => {
  const { server, metrics } = await start();
  try {
    const response = await fetch(metrics, { headers: { 'x-local-route-token': NONCE } });
    const payload = await response.json();
    assert.deepEqual(payload.configuration, { provider: 'openrouter', model: 'metrics/model',
      modelRedacted: false, alternate: null, planFallback: true });
    assert.equal(payload.requests, 0, 'reading diagnostics must not perform inference');
    assert.doesNotMatch(JSON.stringify(payload), /metrics-nonce|sk-or-metrics|sk-ant-oat/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('metrics redact paths and URLs even when they were configured as model ids', async () => {
  const { server, metrics } = await start({ provider: 'local', localModel: 'C:/private/model.gguf',
    localUrl: 'http://127.0.0.1:11434/v1/messages', localKey: 'local-private-key',
    altProvider: 'openrouter', altModel: 'https://private.example/model', altKey: 'sk-or-alt-private',
    altAlias: 'claude-route-openrouter' });
  try {
    const response = await fetch(metrics, { headers: { 'x-local-route-token': NONCE } });
    const payload = await response.json();
    assert.equal(payload.configuration.model, null);
    assert.equal(payload.configuration.modelRedacted, true);
    assert.deepEqual(payload.configuration.alternate, { provider: 'openrouter', model: null, modelRedacted: true });
    assert.doesNotMatch(JSON.stringify(payload), /private|127\.0\.0\.1|11434|https:|sk-or-|metrics-nonce/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('a session token accidentally used as a model is not exposed in metrics', async () => {
  const { server, metrics } = await start({ provider: 'local', localModel: NONCE,
    localUrl: 'http://127.0.0.1:11434/v1/messages' });
  try {
    const response = await fetch(metrics, { headers: { 'x-local-route-token': NONCE } });
    const payload = await response.json();
    assert.equal(payload.configuration.model, null);
    assert.equal(payload.configuration.modelRedacted, true);
    assert.ok(!JSON.stringify(payload).includes(NONCE));
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('counters follow the routes the proxy takes', async () => {
  const { server, base, metrics } = await start({ failOnce: true });
  try {
    assert.equal(await send(base, { content: 'retry-me' }), 200);
    assert.equal(await send(base, { content: 'second', stream: true }), 200);
    // An unknown native beta keeps this turn on Plan.
    assert.equal(await send(base, { content: 'third', beta: `${BETA},future-native-2099-01-01` }), 200);
    const payload = await (await fetch(metrics, { headers: { 'x-local-route-token': NONCE } })).json();
    assert.equal(payload.requests, 3);
    assert.equal(payload.ok, 3);
    assert.equal(payload.routes.external, 2);
    assert.equal(payload.routes.plan, 1);
    assert.ok(payload.reasons.replacement >= 2);
    assert.equal(payload.attempts, 4); // retry (2) + stream (1) + Plan (1)
    assert.equal(payload.retries, 1);
    assert.equal(payload.streams, 1);
    assert.ok(payload.bytesIn > 0 && payload.bytesOut > 0);
    assert.equal(payload.latencyMs.external.count, 2);
    assert.ok(payload.latencyMs.ttfb.count >= 2, 'first-byte timing missing');
    assert.ok(payload.latencyMs.ttfb.p95 >= 0);
    assert.equal(payload.last.route, 'plan');
    assert.equal(payload.last.status, 200);
    assert.ok(payload.uptimeMs >= 0);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('a request that ends on Plan still shows the external attempt, without a first byte', async () => {
  const { server, base, metrics } = await start({ failAlways: true });
  try {
    assert.equal(await send(base, { content: 'fallback' }), 200);
    const payload = await (await fetch(metrics, { headers: { 'x-local-route-token': NONCE } })).json();
    assert.equal(payload.routes.external, 0, 'served by Plan');
    assert.equal(payload.routes.plan, 1);
    assert.equal(payload.externalAttempts, 1, 'the OpenRouter attempt must be visible');
    assert.equal(payload.latencyMs.ttfb.count, 0, 'a discarded attempt must not report a first byte');
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('a metrics file is written for the dashboard', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'route-metrics-'));
  const path = join(directory, 'metrics.json');
  const { server, base } = await start({ metricsPath: path });
  try {
    assert.equal(await send(base, { content: 'file' }), 200);
    let snapshot = null;
    for (let attempt = 0; attempt < 40 && !snapshot; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 100));
      try { snapshot = JSON.parse(await readFile(path, 'utf8')); } catch { /* not flushed yet */ }
    }
    assert.ok(snapshot, 'metrics file was not written');
    assert.equal(snapshot.requests, 1);
    assert.equal(snapshot.routes.external, 1);
    assert.ok(snapshot.startedAt);
  } finally {
    server.closeAllConnections();
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
});
