// Retry policy: a provider wait is parsed once, honored briefly inside the proxy, and handed
// to the client when the turn cannot be served — without inventing delays for a bare 429.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy, parseRetryAfter } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'retry-policy-nonce';
const MODEL = 'claude-sonnet-5-5';
const UPSTREAM_MODEL = 'retry/model';
const OAUTH = 'Bearer sk-ant-oat-retry-policy';
const BETA = 'oauth-2025-04-20';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json', authorization: OAUTH, 'anthropic-beta': BETA };
const NOW = Date.UTC(2026, 0, 1, 0, 0, 0);

const message = model => ({ id: 'msg_retry', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const requestBody = () => JSON.stringify({ model: MODEL, max_tokens: 16, stream: false,
  messages: [{ role: 'user', content: 'hi' }] });

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-retry-policy', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 1000, onHandover: () => {}, ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await listenSafe(server);
  const port = server.address().port;
  return {
    base: `http://127.0.0.1:${port}/v1/messages`,
    metrics: async () => {
      const response = await fetch(`http://127.0.0.1:${port}/api/metrics`, { headers: AUTH });
      assert.equal(response.status, 200);
      return response.json();
    },
  };
}

async function waitFor(predicate, label, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await predicate()) return;
    if (Date.now() >= deadline) throw new Error(`timed out after ${timeoutMs}ms waiting for ${label}`);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

const send = (base, headers = HEADERS) =>
  fetch(base, { method: 'POST', headers, body: requestBody() });

test('parseRetryAfter accepts seconds and HTTP-dates, clamps them, and rejects garbage', () => {
  const future = ms => new Date(NOW + ms).toUTCString();
  const cases = [
    ['120', 120000], ['0', 0], ['7', 7000], ['  90  ', 90000], ['0.15', 150],
    [future(120000), 120000], [future(0), 0], [future(-3600000), 0],
    ['Sunday, 06-Nov-94 08:49:37 GMT', 0], ['Sun Nov  6 08:49:37 1994', 0],
    ['600', 600000], ['7200', 600000], [future(24 * 3600 * 1000), 600000],
    ['soon', null], ['', null], ['   ', null], [null, null], [undefined, null],
    ['-5', null], ['1e3', null], ['garbage 120', null], [new Date(NOW).toUTCString() + 'x', null],
    ['Wed, 32 Oct 2015 07:28:00 GMT', null],
  ];
  for (const [value, expected] of cases) {
    assert.equal(parseRetryAfter(value, NOW), expected, `parseRetryAfter(${JSON.stringify(value)})`);
  }
  // asctime carries no zone marker but is GMT. Read as local time (KST here) a future wait
  // would silently become a past date, so the future form is pinned against a fixed now.
  const asctimeNow = Date.UTC(2026, 9, 25, 12, 0, 0);
  assert.equal(parseRetryAfter('Sun Oct 25 12:05:00 2026', asctimeNow), 300000);
});

test('a long Retry-After is clamped to the proxy retry cap instead of freezing the turn',
  { timeout: 15000 }, async t => {
    const externalStamps = [], planCalls = [];
    const { base, metrics } = await startProxy(t, { retryMaxWaitMs: 40,
      fetchImpl: async (url, options) => {
        if (url === PLAN) { planCalls.push(url); return Response.json(message(JSON.parse(options.body).model)); }
        assert.equal(url, EXTERNAL);
        externalStamps.push(Date.now());
        return new Response('rate limited', { status: 429, headers: { 'retry-after': '120' } });
      } });
    const started = Date.now();
    const response = await send(base);
    assert.equal(response.status, 200, 'the route falls back to Plan after the retries');
    await response.text();
    const elapsed = Date.now() - started;
    assert.equal(externalStamps.length, 4, 'all four external attempts happen');
    assert.equal(planCalls.length, 1);
    assert.ok(elapsed >= 100, `the retries must actually wait the capped delay (${elapsed}ms)`);
    assert.ok(elapsed < 5000, `a 120s hint must not freeze the turn (${elapsed}ms)`);
    for (let index = 1; index < externalStamps.length; index += 1) {
      const gap = externalStamps[index] - externalStamps[index - 1];
      assert.ok(gap >= 30 && gap <= 1000, `retry gap ${gap}ms should be capped at ~40ms`);
    }
    // The parsed hint is kept for the client/circuit view, not silently dropped.
    const payload = await metrics();
    assert.equal(payload.rateLimit.status, 429);
    assert.equal(payload.rateLimit.retryAfterMs, 120000);
    assert.match(payload.rateLimit.at, /^\d{4}-\d{2}-\d{2}T/);
    assert.equal(payload.last.retryAfterMs, 120000);
  });

test('a rate-limited provider keeps its status and wait on the client-facing failure',
  { timeout: 15000 }, async t => {
    const { base } = await startProxy(t, { retryMaxWaitMs: 20,
      fetchImpl: async url => {
        if (url === PLAN) throw new Error('Plan route unavailable');
        return new Response('provider detail must not leak', { status: 429, headers: { 'retry-after': '60' } });
      } });
    const response = await send(base);
    assert.equal(response.status, 429, 'the provider status must not collapse into a generic 502');
    assert.equal(response.headers.get('retry-after'), '60', 'the client must learn how long to wait');
    const body = await response.json();
    assert.equal(body.type, 'error');
    assert.equal(body.error.type, 'rate_limit_error');
    assert.equal(JSON.stringify(body).includes('provider detail'), false, 'provider text must not leak');
  });

test('an overloaded provider keeps 529 and an HTTP-date Retry-After reaches the client',
  { timeout: 15000 }, async t => {
    const headers = new Date(NOW + 90000).toUTCString();
    const { base } = await startProxy(t, { retryMaxWaitMs: 20, now: () => NOW,
      fetchImpl: async url => {
        if (url === PLAN) throw new Error('Plan route unavailable');
        return new Response('overloaded', { status: 529, headers: { 'retry-after': headers } });
      } });
    const response = await send(base);
    assert.equal(response.status, 529);
    assert.equal(response.headers.get('retry-after'), '90', 'an HTTP-date hint becomes delta seconds');
    const body = await response.json();
    assert.equal(body.error.type, 'overloaded_error');
  });

test('a 4xx request-level rejection is not retried', { timeout: 15000 }, async t => {
  const externalCalls = [];
  const { base } = await startProxy(t, {
    fetchImpl: async (url, options) => {
      if (url === PLAN) return Response.json(message(JSON.parse(options.body).model));
      externalCalls.push(url);
      return new Response('bad request', { status: 400, headers: { 'retry-after': '5' } });
    } });
  const response = await send(base);
  assert.equal(response.status, 200, 'the route falls back to Plan');
  await response.text();
  assert.equal(externalCalls.length, 1, 'a 400 must not be retried');
});

test('a 429 without Retry-After adds no invented wait', { timeout: 15000 }, async t => {
  const externalStamps = [];
  const { base, metrics } = await startProxy(t, {
    fetchImpl: async (url, options) => {
      if (url === PLAN) return Response.json(message(JSON.parse(options.body).model));
      externalStamps.push(Date.now());
      return new Response('quota exhausted', { status: 429 });
    } });
  const started = Date.now();
  const response = await send(base);
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(externalStamps.length, 4);
  assert.ok(Date.now() - started < 1000, 'no magic delay may be invented for a bare 429');
  const payload = await metrics();
  assert.equal(payload.rateLimit.status, 429);
  assert.equal(payload.rateLimit.retryAfterMs, null, 'no hint means no parsed wait');
  assert.equal(payload.last.retryAfterMs, null);
});

test('a Retry-After on a successful answer never reaches the metrics', { timeout: 15000 }, async t => {
  const { base, metrics } = await startProxy(t, {
    fetchImpl: async () => Response.json(message(UPSTREAM_MODEL), { headers: { 'retry-after': '30' } }) });
  const response = await send(base);
  assert.equal(response.status, 200);
  await response.text();
  await waitFor(async () => {
    const payload = await metrics();
    return payload.last && payload.last.status === 200 ? payload : null;
  }, 'the successful request to be recorded');
  const payload = await metrics();
  assert.equal(payload.requests, 1);
  assert.equal(payload.last.status, 200);
  assert.equal(payload.last.retryAfterMs, null, 'a successful answer must not carry a retry hint');
  assert.equal(payload.rateLimit, null, 'a success is not a rate-limit observation');
});
