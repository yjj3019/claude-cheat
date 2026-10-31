// Circuit breaker classification and fencing: only provider trouble may open the breaker,
// a 429 names its own cooldown, and a late completion from an older generation must never
// close, reopen, or otherwise overwrite the state a newer generation established.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'circuit-fencing-nonce';
const MODEL = 'claude-sonnet-5-5';
const UPSTREAM_MODEL = 'circuit/model';
const OAUTH = 'Bearer sk-ant-oat-circuit';
const BETA = 'oauth-2025-04-20';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const LOCAL_URL = 'http://127.0.0.1:11434/v1/messages';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json', authorization: OAUTH, 'anthropic-beta': BETA };

const message = model => ({ id: 'msg_circuit', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const textReply = (model, text) => ({ ...message(model), content: [{ type: 'text', text }] });
const externalReply = () => Response.json(textReply(UPSTREAM_MODEL, 'external-ok'));
const planReply = () => Response.json(textReply(MODEL, 'plan-ok'));
const requestBody = (extra = {}) => JSON.stringify({ model: MODEL, max_tokens: 16, stream: false,
  messages: [{ role: 'user', content: 'hi' }], ...extra });

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-circuit', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, cooldownMs: 100, attemptTimeoutMs: 1000, onHandover: () => {}, ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  const metrics = async () => {
    const response = await fetch(`${base}/api/metrics`, { headers: AUTH });
    assert.equal(response.status, 200);
    return response.json();
  };
  const send = extra => fetch(`${base}/v1/messages`, { method: 'POST', headers: HEADERS, body: requestBody(extra) });
  const sendOk = async extra => {
    const response = await send(extra);
    return { status: response.status, text: await response.text() };
  };
  return { send, sendOk, metrics };
}

for (const status of [400, 404, 405, 413, 415, 422]) {
  test(`request-level HTTP ${status} never opens the breaker`, { timeout: 15000 }, async t => {
    let time = 1000;
    const externalCalls = [];
    const proxy = await startProxy(t, { now: () => time,
      fetchImpl: async url => {
        if (url === PLAN) return planReply();
        assert.equal(url, EXTERNAL);
        externalCalls.push(status);
        return new Response('request rejected', { status });
      } });
    const first = await proxy.sendOk();
    assert.equal(first.status, 200);
    assert.match(first.text, /plan-ok/, 'the request falls back to Plan');
    assert.equal(externalCalls.length, 1, 'a request-level 4xx is not retried');
    const second = await proxy.sendOk();
    assert.equal(second.status, 200);
    assert.equal(externalCalls.length, 2, 'the next request must still reach the provider');
    const payload = await proxy.metrics();
    assert.equal(payload.circuitOpens, 0, 'the breaker must stay closed');
  });
}

test('a 409 conflict retains bounded retries without opening the breaker', async t => {
  let attempts = 0;
  const proxy = await startProxy(t, { fetchImpl: async url => {
    if (url === PLAN) return planReply();
    attempts++;
    return attempts === 1 ? new Response('conflict', { status: 409 }) : externalReply();
  } });
  assert.match((await proxy.sendOk()).text, /external-ok/);
  assert.equal(attempts, 2);
  assert.equal((await proxy.metrics()).circuitOpens, 0);
});

test('client-side request validation and a blocked model never open the provider circuit',
  { timeout: 15000 }, async t => {
    let time = 1000;
    const externalCalls = [], planCalls = [];
    const proxy = await startProxy(t, { now: () => time,
      fetchImpl: async url => {
        if (url === PLAN) { planCalls.push(url); return planReply(); }
        externalCalls.push(url);
        return externalReply();
      } });
    // An unsupported request field is validated locally and routed to Plan without a hop.
    const validated = await proxy.sendOk({ unknown_feature: true });
    assert.equal(validated.status, 200);
    assert.match(validated.text, /plan-ok/);
    assert.equal(externalCalls.length, 0);
    assert.equal(planCalls.length, 1);
    // A model outside the allowed set is rejected before any upstream call.
    const blocked = await proxy.send({ model: 'unknown-model' });
    assert.equal(blocked.status, 403);
    await blocked.text();
    assert.equal(externalCalls.length, 0);
    // Nothing opened the breaker: a normal request still reaches the provider.
    const normal = await proxy.sendOk();
    assert.equal(normal.status, 200);
    assert.match(normal.text, /external-ok/);
    assert.equal(externalCalls.length, 1);
    const payload = await proxy.metrics();
    assert.equal(payload.circuitOpens, 0);
  });

test('a 5xx opens the breaker and a later 4xx neither extends nor reopens it',
  { timeout: 15000 }, async t => {
    let time = 1000, phase = 'down';
    const externalCalls = [];
    const proxy = await startProxy(t, { cooldownMs: 100, now: () => time,
      fetchImpl: async url => {
        if (url === PLAN) return planReply();
        externalCalls.push(phase);
        if (phase === 'down') return new Response('down', { status: 500 });
        if (phase === 'request-level') return new Response('renamed', { status: 400 });
        return externalReply();
      } });
    const failed = await proxy.sendOk();
    assert.equal(failed.status, 200);
    assert.match(failed.text, /plan-ok/);
    assert.equal(externalCalls.length, 4, 'the transient failure is retried');
    // During the cooldown the provider is bypassed.
    const cooling = await proxy.sendOk();
    assert.match(cooling.text, /plan-ok/);
    assert.equal(externalCalls.length, 4);
    time += 101; // cooldown expired
    phase = 'request-level';
    const probe = await proxy.sendOk();
    assert.equal(probe.status, 200);
    assert.match(probe.text, /plan-ok/);
    assert.equal(externalCalls.length, 5, 'the half-open probe reaches the provider once');
    // A 4xx probe result must not extend the old open timer: the next request may probe again.
    phase = 'healthy';
    const recovered = await proxy.sendOk();
    assert.match(recovered.text, /external-ok/);
    assert.equal(externalCalls.length, 6);
    const after = await proxy.sendOk();
    assert.match(after.text, /external-ok/);
    assert.equal(externalCalls.length, 7);
    const payload = await proxy.metrics();
    assert.equal(payload.circuitOpens, 1, 'the 4xx must not open the breaker a second time');
  });

test('a 408 remains transient and is retried', { timeout: 15000 }, async t => {
  let externalCalls = 0;
  const proxy = await startProxy(t, { fetchImpl: async url => {
    if (url === PLAN) return planReply();
    assert.equal(url, EXTERNAL);
    externalCalls++;
    return new Response('timeout', { status: 408 });
  } });
  const first = await proxy.sendOk();
  assert.match(first.text, /plan-ok/);
  assert.equal(externalCalls, 4);
  const payload = await proxy.metrics();
  assert.equal(payload.circuitOpens, 1);
});

test('a 429 honors its Retry-After window for the cooldown', { timeout: 15000 }, async t => {
  let time = 1000, healthy = false;
  const externalCalls = [];
  const proxy = await startProxy(t, { cooldownMs: 100, retryMaxWaitMs: 1, now: () => time,
    fetchImpl: async url => {
      if (url === PLAN) return planReply();
      externalCalls.push(time);
      if (healthy) return externalReply();
      return new Response('slow down', { status: 429, headers: { 'retry-after': '2' } });
    } });
  const first = await proxy.sendOk();
  assert.equal(first.status, 200);
  assert.match(first.text, /plan-ok/);
  assert.equal(externalCalls.length, 4, 'the 429 keeps the usual retry count');
  const payload = await proxy.metrics();
  assert.equal(payload.circuitOpens, 1);
  assert.equal(payload.rateLimit.status, 429);
  assert.equal(payload.rateLimit.retryAfterMs, 2000);
  // The operator's 100ms cooldown has elapsed, but the provider asked for 2s.
  time += 101;
  const during = await proxy.sendOk();
  assert.match(during.text, /plan-ok/);
  assert.equal(externalCalls.length, 4, 'the breaker must wait the Retry-After window');
  time += 1898; // 2999ms: still inside the 2000ms window opened at now=1000
  const borderline = await proxy.sendOk();
  assert.match(borderline.text, /plan-ok/);
  assert.equal(externalCalls.length, 4);
  time += 2; // 3001ms: window over
  healthy = true;
  const probe = await proxy.sendOk();
  assert.match(probe.text, /external-ok/);
  assert.equal(externalCalls.length, 5);
  const after = await proxy.sendOk();
  assert.match(after.text, /external-ok/);
  assert.equal(externalCalls.length, 6);
});

test('a huge Retry-After is bounded to ten minutes of cooldown', { timeout: 15000 }, async t => {
  let time = 1000, healthy = false;
  const externalCalls = [];
  const proxy = await startProxy(t, { cooldownMs: 100, retryMaxWaitMs: 1, now: () => time,
    fetchImpl: async url => {
      if (url === PLAN) return planReply();
      externalCalls.push(url);
      if (healthy) return externalReply();
      return new Response('slow down', { status: 429, headers: { 'retry-after': '7200' } });
    } });
  await proxy.sendOk();
  assert.equal(externalCalls.length, 4);
  const payload = await proxy.metrics();
  assert.equal(payload.rateLimit.retryAfterMs, 600000, 'the parsed hint is clamped');
  time = 600999; // just inside the 600s cap
  const during = await proxy.sendOk();
  assert.match(during.text, /plan-ok/);
  assert.equal(externalCalls.length, 4);
  time = 601001; // past the cap
  healthy = true;
  const probe = await proxy.sendOk();
  assert.match(probe.text, /external-ok/);
  assert.equal(externalCalls.length, 5);
});

test('half-open admits exactly one probe and only a real success recovers the circuit',
  { timeout: 15000 }, async t => {
    let time = 1000, phase = 'down', releaseProbe, probeEntered;
    const probeStarted = new Promise(resolve => { probeEntered = resolve; });
    const probeGate = new Promise(resolve => { releaseProbe = resolve; });
    const externalCalls = [];
    const proxy = await startProxy(t, { cooldownMs: 100, now: () => time,
      fetchImpl: async url => {
        if (url === PLAN) return planReply();
        externalCalls.push(phase);
        if (phase === 'down') return new Response('down', { status: 500 });
        if (phase === 'probe') { probeEntered(); await probeGate; return externalReply(); }
        return externalReply();
      } });
    const failed = await proxy.sendOk();
    assert.match(failed.text, /plan-ok/);
    assert.equal(externalCalls.length, 4);
    time += 101;
    phase = 'probe';
    const probe = proxy.send();
    await probeStarted;
    assert.equal(externalCalls.length, 5, 'the half-open probe reaches the provider');
    // A concurrent request must not start a second probe; it takes the fallback path.
    const concurrent = await proxy.sendOk();
    assert.equal(concurrent.status, 200);
    assert.match(concurrent.text, /plan-ok/, 'the Plan answer must not recover the provider circuit');
    assert.equal(externalCalls.length, 5, 'only one half-open probe may reach the provider');
    releaseProbe();
    const probeResponse = await probe;
    assert.equal(probeResponse.status, 200);
    assert.match(await probeResponse.text(), /external-ok/);
    const after = await proxy.sendOk();
    assert.match(after.text, /external-ok/);
    assert.equal(externalCalls.length, 6);
  });

test('a late stale failure from an older generation cannot reopen the breaker',
  { timeout: 15000 }, async t => {
    let time = 1000, healthy = false, releaseStale, staleEntered;
    const staleStarted = new Promise(resolve => { staleEntered = resolve; });
    const staleGate = new Promise(resolve => { releaseStale = resolve; });
    const proxy = await startProxy(t, { provider: 'local', localOnly: true, localUrl: LOCAL_URL,
      localModel: 'circuit-local', cooldownMs: 100, now: () => time,
      fetchImpl: async (url, options) => {
        assert.equal(url, LOCAL_URL);
        const content = JSON.parse(options.body).messages[0].content;
        if (content === 'stale') {
          staleEntered();
          await staleGate;
          return new Response('late', { status: 500 });
        }
        return healthy ? Response.json(textReply('circuit-local', 'local-ok')) : new Response('down', { status: 500 });
      } });
    const stale = proxy.send({ messages: [{ role: 'user', content: 'stale' }] });
    await staleStarted;
    // A normal request fails once (Local-only) and opens the breaker.
    const failed = await proxy.sendOk();
    assert.equal(failed.status, 400);
    assert.match(failed.text, /local_upstream_error/);
    time += 101;
    healthy = true;
    const recovered = await proxy.sendOk();
    assert.equal(recovered.status, 200, 'a real success recovers the circuit');
    releaseStale();
    const staleResponse = await stale;
    assert.equal(staleResponse.status, 400);
    await staleResponse.text();
    const after = await proxy.sendOk();
    assert.equal(after.status, 200, 'the stale failure must not re-open the breaker');
    const payload = await proxy.metrics();
    assert.equal(payload.circuitOpens, 1);
  });

test('a late stale success cannot close a breaker opened after it started',
  { timeout: 15000 }, async t => {
    let time = 1000, releaseStale, staleEntered;
    const staleStarted = new Promise(resolve => { staleEntered = resolve; });
    const staleGate = new Promise(resolve => { releaseStale = resolve; });
    let phase = 'down', externalCalls = 0;
    const proxy = await startProxy(t, { cooldownMs: 100, now: () => time,
      fetchImpl: async url => {
        if (url === PLAN) return planReply();
        externalCalls += 1;
        if (externalCalls === 1) { staleEntered(); await staleGate; return externalReply(); }
        if (phase === 'down') return new Response('down', { status: 500 });
        return externalReply();
      } });
    const stale = proxy.send({ messages: [{ role: 'user', content: 'stale' }] });
    await staleStarted;
    // Another request opens the breaker while the stale request is still in flight.
    const failed = await proxy.sendOk();
    assert.equal(failed.status, 200);
    assert.match(failed.text, /plan-ok/);
    assert.equal(externalCalls, 5, 'the stale request plus four failed attempts');
    releaseStale();
    const staleResponse = await stale;
    assert.equal(staleResponse.status, 200);
    assert.match(await staleResponse.text(), /external-ok/);
    // The stale success belongs to the previous generation, so the breaker stays open.
    const cooling = await proxy.sendOk();
    assert.match(cooling.text, /plan-ok/, 'the stale success must not close the new breaker');
    assert.equal(externalCalls, 5);
    time += 101;
    phase = 'healthy';
    const probe = await proxy.sendOk();
    assert.match(probe.text, /external-ok/);
    assert.equal(externalCalls, 6, 'recovery needs a real success from the current generation');
    const after = await proxy.sendOk();
    assert.match(after.text, /external-ok/);
    assert.equal(externalCalls, 7);
    const payload = await proxy.metrics();
    assert.equal(payload.circuitOpens, 1);
  });
