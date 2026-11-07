// SPDX-License-Identifier: MIT
// Synthetic cross-provider routing + monitoring simulation. No provider/network calls.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'provider-monitor-sim', OAUTH = 'Bearer sk-ant-oat-provider-sim';
const CLIENT_MODEL = 'claude-sonnet-5-5';
const config = provider => provider === 'openrouter' ? {
  provider, key: 'sk-or-synthetic', openrouterModel: 'vendor/sim',
  openrouterUpstreamProvider: 'sim/fp4', openrouterReasoning: 'high',
} : provider === 'local' ? {
  provider, localModel: 'local/sim', localUrl: 'http://127.0.0.1:11434/v1/messages',
} : {
  provider, localModel: 'cli/codex', localUrl: 'http://127.0.0.1:4100/v1/messages',
  cliKind: 'codex', cliRequestedModel: 'gpt-sim',
};
const upstreamModel = { openrouter: 'vendor/sim', local: 'local/sim', cli: 'cli/codex' };
const success = provider => {
  const headers = { 'content-type': 'application/json' };
  if (provider === 'cli') Object.assign(headers, { 'x-cli-usage-measured': 'true',
    'x-cli-reported-model': 'gpt-sim', 'x-cli-process-ms': '8.5' });
  return new Response(JSON.stringify({ id: 'msg_sim', type: 'message', role: 'assistant',
    model: upstreamModel[provider], content: [{ type: 'text', text: 'OK' }],
    stop_reason: 'end_turn', usage: { input_tokens: 5, output_tokens: 2 } }), { headers });
};
const body = scenario => ({ model: CLIENT_MODEL, max_tokens: 32,
  messages: [{ role: 'user', content: `synthetic-${scenario}` }] });

async function runCase(t, provider, scenario, handler, options = {}) {
  const attempts = [];
  const server = createProxy({ ...config(provider), nonce: NONCE, retryDelayMs: 0, retryMaxWaitMs: 0,
    cooldownMs: 100, attemptTimeoutMs: 500, ...options,
    fetchImpl: async (url, request) => {
      attempts.push({ url, request: JSON.parse(request.body), headers: request.headers });
      return handler(attempts.length, url, request);
    } });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(base + '/v1/messages', { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': 'oauth-2025-04-20' },
    body: JSON.stringify(body(scenario)) });
  const text = await response.text();
  const metrics = await (await fetch(base + '/api/metrics', {
    headers: { 'x-local-route-token': NONCE } })).json();
  return { status: response.status, text, attempts, metrics };
}

for (const provider of ['openrouter', 'local', 'cli']) {
  test(`${provider}: success updates provider token/performance/history metrics`, async t => {
    const result = await runCase(t, provider, 'ok', async () => success(provider));
    assert.equal(result.status, 200);
    assert.equal(result.metrics.requests, 1);
    assert.equal(result.metrics.ok, 1);
    assert.equal(result.metrics.failed, 0);
    assert.equal(result.metrics.tokenFlow[provider].responses, 1);
    assert.equal(result.metrics.tokenFlow[provider].inputTokens, 5);
    assert.equal(result.metrics.tokenFlow[provider].outputTokens, 2);
    assert.equal(result.metrics.modelPerformance[provider].samples, 1);
    assert.equal(result.metrics.monitoringHistory.at(-1).completed, 1);
    assert.equal(result.metrics.monitoringHistory.at(-1).ok, 1);
    if (provider === 'openrouter') {
      assert.deepEqual(result.attempts[0].request.provider, { only: ['sim/fp4'], allow_fallbacks: false });
      assert.deepEqual(result.attempts[0].request.reasoning, { effort: 'high' });
    }
    if (provider === 'cli') {
      assert.equal(result.metrics.cliActivity.active, 0);
      assert.equal(result.metrics.cliFailures.status5xx, 0);
      assert.equal(result.metrics.monitoringHistory.at(-1).cliExternalSuccess, 1);
    }
  });

  test(`${provider}: retry/rate-limit policy is reflected in metrics`, async t => {
    let seen = 0;
    const result = await runCase(t, provider, 'rate-once', async () => {
      seen++;
      return provider === 'cli' || seen === 1
        ? new Response('limited', { status: 429, headers: { 'retry-after': '0' } })
        : success(provider);
    });
    if (provider === 'cli') {
      assert.equal(result.status, 429);
      assert.equal(result.attempts.length, 1);
      assert.equal(result.metrics.retries, 0);
      assert.equal(result.metrics.cliFailures.status429, 1);
    } else {
      assert.equal(result.status, 200);
      assert.equal(result.attempts.length, 2);
      assert.equal(result.metrics.retries, 1);
      assert.equal(result.metrics.tokenFlow[provider].responses, 1);
    }
    assert.equal(result.metrics.requests, 1);
    assert.equal(result.metrics.failed, provider === 'cli' ? 1 : 0);
    assert.equal(result.metrics.cliActivity?.active ?? 0, 0);
  });

  test(`${provider}: persistent 5xx is bounded and classified`, async t => {
    const result = await runCase(t, provider, 'fail', async () => new Response('down', { status: 503 }));
    assert.equal(result.status, 503);
    assert.equal(result.metrics.requests, 1);
    assert.equal(result.metrics.failed, 1);
    assert.equal(result.metrics.ok, 0);
    assert.equal(result.metrics.tokenFlow[provider].responses, 0);
    assert.equal(result.metrics.cliActivity?.active ?? 0, 0);
    if (provider === 'cli') {
      assert.equal(result.attempts.length, 1);
      assert.equal(result.metrics.cliFailures.status5xx, 1);
    } else {
      assert.equal(result.attempts.length, 4);
      assert.equal(result.metrics.retries, 3);
    }
    const history = result.metrics.monitoringHistory.at(-1);
    assert.equal(history.completed, 1);
    assert.equal(history.failed, 1);
  });
}

test('in-flight byte budget rejections are counted in requestAdmission.rejected', async t => {
  let calls = 0;
  const server = createProxy({ ...config('openrouter'), nonce: NONCE, retryDelayMs: 0,
    maxConcurrentRequests: 64, maxInflightBytes: 1024,
    fetchImpl: async () => { calls++; return success('openrouter'); } });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const big = JSON.stringify({ model: CLIENT_MODEL, max_tokens: 8,
    messages: [{ role: 'user', content: 'y'.repeat(4096) }] });
  const response = await fetch(base + '/v1/messages', { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': 'oauth-2025-04-20' }, body: big });
  await response.text();
  const metrics = await (await fetch(base + '/api/metrics', {
    headers: { 'x-local-route-token': NONCE } })).json();
  assert.equal(response.status, 503);
  assert.equal(calls, 0, 'byte-budget rejections must not reach the upstream');
  assert.equal(metrics.requestAdmission.rejected, 1);
  assert.equal(metrics.requestAdmission.rejectedBytes, 1);
  assert.equal(metrics.requestAdmission.rejectedConcurrency, 0);
  assert.equal(metrics.requestAdmission.active, 0);
});

test('Local admission pressure matches rejected responses and drains permits', async t => {
  let calls = 0;
  const server = createProxy({ ...config('local'), nonce: NONCE, retryDelayMs: 0,
    localConcurrency: 1, localQueueLimit: 1, localQueueWaitMs: 0,
    fetchImpl: async () => { calls++; await new Promise(resolve => setTimeout(resolve, 50)); return success('local'); } });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const send = i => fetch(base + '/v1/messages', { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
      authorization: OAUTH, 'anthropic-beta': 'oauth-2025-04-20' }, body: JSON.stringify(body(`burst-${i}`)) });
  const responses = await Promise.all(Array.from({ length: 12 }, (_, i) => send(i)));
  const statuses = responses.map(response => response.status);
  const metrics = await (await fetch(base + '/api/metrics', {
    headers: { 'x-local-route-token': NONCE } })).json();
  assert.equal(statuses.filter(status => status === 200).length, 1);
  assert.equal(statuses.filter(status => status === 503).length, 11);
  assert.equal(calls, 1, 'rejected Local requests must not reach the upstream');
  assert.equal(metrics.localAdmission.rejected, 11);
  assert.equal(metrics.localAdmission.active, 0, 'all permits must be released');
  assert.equal(metrics.requests, 12);
  assert.equal(metrics.ok, 1);
  assert.equal(metrics.failed, 11);
  assert.equal(metrics.monitoringHistory.at(-1).completed, 12);
  assert.equal(metrics.monitoringHistory.at(-1).failed, 11);
});
