import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const MODEL = 'shared-local';
const LOCAL = 'http://127.0.0.1:11434/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const headers = { 'content-type': 'application/json', 'x-local-route-token': 'admission-test',
  authorization: 'Bearer sk-ant-oat-admission-test', 'anthropic-beta': 'oauth-2025-04-20' };
const body = { model: 'claude-sonnet-5-5', max_tokens: 321, messages: [{ role: 'user', content: 'preserve this' }] };
const response = options => Response.json({ id: 'msg_test', type: 'message', role: 'assistant',
  model: JSON.parse(options.body).model, content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn',
  usage: { input_tokens: 5, output_tokens: 1 } });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
async function waitFor(predicate) {
  const deadline = Date.now() + 3000;
  while (!await predicate()) {
    assert.ok(Date.now() < deadline, 'condition should become true');
    await new Promise(r => setTimeout(r, 5));
  }
}
async function fixture(t, fetchImpl, options = {}) {
  const server = createProxy({ nonce: 'admission-test', provider: 'local', localUrl: LOCAL,
    localModel: MODEL, retryDelayMs: 0, attemptTimeoutMs: 2000, localConcurrency: 1,
    localQueueLimit: 1, localQueueWaitMs: 1000, ...options, fetchImpl });
  await listenSafe(server);
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); });
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    send: signal => fetch(base + '/v1/messages', { method: 'POST', headers, body: JSON.stringify(body), signal }),
    metrics: async () => (await fetch(base + '/api/metrics', { headers })).json(),
  };
}

for (const count of [4, 8]) {
  test(`${count} parallel Local turns keep two slots and complete without Plan fallback`, async t => {
    const gate = deferred(); let active = 0, peak = 0, calls = 0;
    const { send, metrics } = await fixture(t, async (url, options) => {
      assert.equal(url, LOCAL);
      calls++; active++; peak = Math.max(peak, active);
      await gate.promise;
      active--;
      return response(options);
    }, { localConcurrency: 2, localQueueLimit: 8, localQueueWaitMs: 60000, attemptTimeoutMs: 15000 });
    const pending = Array.from({ length: count }, () => send());
    try {
      await waitFor(async () => (await metrics()).localAdmission.queued === count - 2);
      if (count === 8) await new Promise(resolve => setTimeout(resolve, 5200));
      const waiting = await metrics();
      assert.equal(waiting.localAdmission.active, 2);
      assert.equal(waiting.localAdmission.queued, count - 2);
      assert.equal(waiting.routes.plan, 0);
    } finally { gate.resolve(); }
    for (const answer of await Promise.all(pending)) { assert.equal(answer.status, 200); await answer.text(); }
    const final = await metrics();
    assert.equal(calls, count); assert.equal(peak, 2);
    assert.equal(final.routes.external, count); assert.equal(final.routes.plan, 0);
    assert.equal(final.localAdmission.active, 0); assert.equal(final.localAdmission.queued, 0);
  });
}

test('Local queue wait defaults to 60 seconds and honors a bounded environment override', async t => {
  const previous = process.env.ROUTE_LOCAL_QUEUE_WAIT_MS;
  t.after(() => { if (previous === undefined) delete process.env.ROUTE_LOCAL_QUEUE_WAIT_MS;
    else process.env.ROUTE_LOCAL_QUEUE_WAIT_MS = previous; });
  delete process.env.ROUTE_LOCAL_QUEUE_WAIT_MS;
  const base = await fixture(t, async (_url, options) => response(options), { localQueueWaitMs: undefined });
  assert.equal((await base.metrics()).localAdmission.queueWaitMs, 60000);
  process.env.ROUTE_LOCAL_QUEUE_WAIT_MS = '15000';
  const custom = await fixture(t, async (_url, options) => response(options), { localQueueWaitMs: undefined });
  assert.equal((await custom.metrics()).localAdmission.queueWaitMs, 15000);
  for (const invalid of ['invalid', '-1', '60001', 'Infinity']) {
    process.env.ROUTE_LOCAL_QUEUE_WAIT_MS = invalid;
    assert.throws(() => createProxy({ nonce: 'admission-test', provider: 'local', localUrl: LOCAL,
      localModel: MODEL }), /Invalid Local admission limits/);
  }
});

test('Local queue bounds concurrency, rejects overflow to Plan and preserves token payload', async t => {
  const gate = deferred(); let localCalls = 0, planCalls = 0;
  const { send, metrics } = await fixture(t, async (url, options) => {
    if (url === PLAN) { planCalls++; return response(options); }
    localCalls++;
    assert.equal(JSON.parse(options.body).max_tokens, 321);
    assert.deepEqual(JSON.parse(options.body).messages, body.messages);
    if (localCalls === 1) await gate.promise;
    return response(options);
  });
  const first = send(); await waitFor(() => localCalls === 1);
  const second = send(); await waitFor(async () => (await metrics()).localAdmission.queued === 1);
  assert.equal((await send()).status, 200);
  assert.equal(planCalls, 1); assert.equal(localCalls, 1);
  gate.resolve(); await (await first).text(); await (await second).text();
  const m = await metrics();
  assert.deepEqual(m.localAdmission, { active: 0, queued: 0, rejected: 1, concurrency: 1, queueLimit: 1, queueWaitMs: 1000 });
  assert.equal(m.reasons.local_busy, 1); assert.equal(m.circuitOpens, 0); assert.equal(localCalls, 2);
});

test('Local queued cancellation removes its entry and releases active permits', async t => {
  const gate = deferred(); let calls = 0;
  const { send, metrics } = await fixture(t, async (_url, options) => {
    if (++calls === 1) await gate.promise;
    return response(options);
  });
  const first = send(); await waitFor(() => calls === 1);
  const controller = new AbortController();
  const cancelled = send(controller.signal).catch(error => error);
  await waitFor(async () => (await metrics()).localAdmission.queued === 1);
  controller.abort(); await cancelled;
  await waitFor(async () => (await metrics()).localAdmission.queued === 0);
  gate.resolve(); await (await first).text(); await (await send()).text();
  const m = await metrics(); assert.equal(calls, 2); assert.equal(m.localAdmission.active, 0);
  assert.equal(m.localAdmission.rejected, 0);
});

test('Local-only expired queue returns 503 without Plan or circuit changes', async t => {
  const gate = deferred(); let calls = 0;
  const { send, metrics } = await fixture(t, async (url, options) => {
    assert.equal(url, LOCAL); calls++; await gate.promise; return response(options);
  }, { localOnly: true, localQueueWaitMs: 20 });
  const first = send(); await waitFor(() => calls === 1);
  const busy = await send(); assert.equal(busy.status, 503);
  assert.equal(busy.headers.get('retry-after'), '1'); assert.match(await busy.text(), /local_busy/);
  const m = await metrics(); assert.equal(m.externalAttempts, 1); assert.equal(m.circuitOpens, 0);
  gate.resolve(); await (await first).text();
});

for (const status of [429, 503, 529]) {
  test(`Local ${status} long Retry-After skips early retries and keeps cooldown`, async t => {
    let calls = 0, plan = 0;
    const { send, metrics } = await fixture(t, async (url, options) => {
      if (url === PLAN) { plan++; return response(options); }
      calls++; return new Response('busy', { status, headers: { 'retry-after': '120' } });
    }, { retryMaxWaitMs: 10, cooldownMs: 1 });
    await (await send()).text(); await (await send()).text();
    assert.equal(calls, 1); assert.equal(plan, 2);
    const m = await metrics(); assert.equal(m.retries, 0); assert.equal(m.reasons.circuit_open, 1);
    assert.equal(m.localAdmission.active, 0); assert.equal(m.rateLimit.retryAfterMs, 120000);
  });
}

test('Queued Local request rechecks a newly opened breaker', async t => {
  const gate = deferred(); let calls = 0;
  const { send, metrics } = await fixture(t, async (url, options) => {
    if (url === PLAN) return response(options);
    calls++; await gate.promise;
    return new Response('busy', { status: 503, headers: { 'retry-after': '120' } });
  }, { retryMaxWaitMs: 10 });
  const first = send(); await waitFor(() => calls === 1);
  const second = send(); await waitFor(async () => (await metrics()).localAdmission.queued === 1);
  gate.resolve(); await (await first).text(); await (await second).text();
  assert.equal(calls, 1); assert.equal((await metrics()).localAdmission.active, 0);
  assert.equal((await metrics()).reasons.circuit_open, 1);
});

test('Local congestion retries back off while successful replies release capacity', async t => {
  const stamps = [];
  const { send, metrics } = await fixture(t, async (_url, options) => {
    stamps.push(Date.now());
    return stamps.length < 4 ? new Response('busy', { status: 503 }) : response(options);
  }, { retryDelayMs: 30, retryMaxWaitMs: 1000 });
  await (await send()).text();
  assert.equal(stamps.length, 4);
  for (let i = 1; i < stamps.length; i++) assert.ok(stamps[i] - stamps[i - 1] >= 30 * (2 ** (i - 1)) - 5);
  assert.equal((await metrics()).localAdmission.active, 0);
});

test('Aborting an active Local turn releases capacity to the queued turn', async t => {
  let calls = 0;
  const { send, metrics } = await fixture(t, async (_url, options) => {
    if (++calls === 1) return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true });
    });
    return response(options);
  });
  const controller = new AbortController();
  const first = send(controller.signal).catch(error => error);
  await waitFor(() => calls === 1);
  const second = send(); await waitFor(async () => (await metrics()).localAdmission.queued === 1);
  controller.abort(); await first;
  assert.equal((await second).status, 200);
  await waitFor(async () => (await metrics()).localAdmission.active === 0);
  assert.equal(calls, 2); assert.equal((await metrics()).circuitOpens, 0);
});

test('Empty Local responses retain validation retry policy without congestion jitter', async t => {
  let randomCalls = 0;
  t.mock.method(Math, 'random', () => { randomCalls++; return 0; });
  let calls = 0;
  const { send, metrics } = await fixture(t, async (url, options) => {
    if (url === PLAN) return response(options);
    calls++; return Response.json({ id: 'msg_empty', type: 'message', role: 'assistant', model: MODEL,
      content: [], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 0 } });
  });
  await (await send()).text();
  assert.equal(calls, 4); assert.equal(randomCalls, 0); assert.equal((await metrics()).localAdmission.active, 0);
});

for (const [detail, upstream] of [
  ['missing_body', () => new Response(null)],
  ['empty_body', () => new Response('   ', { headers: { 'content-type': 'application/json' } })],
  ['empty_content', () => Response.json({ type: 'message', model: MODEL, content: [], stop_reason: 'end_turn' })],
  ['content_without_usable_output', () => Response.json({ type: 'message', model: MODEL,
    content: [{ type: 'text', text: '   ' }], stop_reason: 'end_turn' })],
]) {
  test(`Empty Local diagnostic distinguishes ${detail} without exposing a body`, async t => {
    const logs = t.mock.method(console, 'error', () => {});
    const { send } = await fixture(t, async (url, options) => url === PLAN ? response(options) : upstream());
    await (await send()).text();
    const failures = logs.mock.calls.map(call => JSON.parse(call.arguments[0]))
      .filter(entry => entry.event === 'external_attempt_failed');
    assert.equal(failures.length, 4);
    for (const failure of failures) {
      assert.equal(failure.validation, 'empty_response');
      assert.equal(failure.validation_detail, detail);
      assert.equal(failure.body, undefined);
      assert.equal(failure.content, undefined);
    }
  });
}

test('A later transport failure does not reuse an earlier Local Retry-After hint', async t => {
  let calls = 0, clock = 100000;
  const { send, metrics } = await fixture(t, async (url, options) => {
    if (url === PLAN) return response(options);
    if (++calls === 1) return new Response('busy', { status: 503, headers: { 'retry-after': '0.05' } });
    throw new Error('transport');
  }, { retryMaxWaitMs: 100, cooldownMs: 10, now: () => clock });
  await (await send()).text();
  assert.equal((await metrics()).last.retryAfterMs, null);
  clock += 20;
  await (await send()).text();
  assert.equal(calls, 8, 'short generic cooldown expired; earlier 50ms hint must not be reused');
});
