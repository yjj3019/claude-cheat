import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const LOCAL = 'http://127.0.0.1:11434/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const MODEL = 'observability-local';
const body = { model: 'claude-sonnet-5-5', max_tokens: 64, messages: [{ role: 'user', content: 'fixture' }] };
const headers = { 'content-type': 'application/json', 'x-local-route-token': 'observe-test',
  authorization: 'Bearer sk-ant-oat-observe-test', 'anthropic-beta': 'oauth-2025-04-20' };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const message = (model = MODEL, content = [{ type: 'text', text: 'ok' }]) => ({
  id: 'msg_fixture', type: 'message', role: 'assistant', model, content,
  stop_reason: 'end_turn', usage: { input_tokens: 3, output_tokens: 1 },
});
const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
async function waitFor(predicate) {
  const deadline = Date.now() + 3000;
  while (!await predicate()) { assert.ok(Date.now() < deadline, 'condition should become true'); await delay(5); }
}
async function fixture(t, fetchImpl, options = {}) {
  const server = createProxy({ nonce: 'observe-test', provider: 'local', localUrl: LOCAL,
    localModel: MODEL, retryDelayMs: 5, attemptTimeoutMs: 2000, localConcurrency: 1,
    localQueueLimit: 2, localQueueWaitMs: 1000, ...options, fetchImpl });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    send: (signal, request = body) => fetch(base + '/v1/messages', { method: 'POST', headers, body: JSON.stringify(request), signal }),
    metrics: async () => (await fetch(base + '/api/metrics', { headers })).json(),
  };
}
function captureEvents(t) {
  const events = [];
  t.mock.method(console, 'error', text => { try { events.push(JSON.parse(text)); } catch {} });
  return events;
}

test('CLI activity measures proxy-side in-flight requests and closes once after cancellation', async t => {
  const ready = deferred();
  const f = await fixture(t, async () => { ready.resolve(); return new Promise(() => {}); },
    {provider:'cli', localModel:'cli/codex', cliKind:'codex'});
  const controller = new AbortController();
  const pending = f.send(controller.signal).catch(() => null);
  await ready.promise;
  assert.deepEqual((await f.metrics()).cliActivity, {active:1,limit:4,queued:0,rejected:0});
  controller.abort(); await pending;
  await waitFor(async () => (await f.metrics()).cliActivity.active === 0);
  const result = await f.metrics();
  assert.equal(result.cliActivity.active, 0);
  assert.equal(result.aborted, 1);
  await delay(20);
  assert.deepEqual((await f.metrics()).cliActivity, result.cliActivity);
});

test('CLI process duration accepts only bounded numeric metadata on completed validated replies', async t => {
  let reported = '128.35';
  const f = await fixture(t, async () => Response.json(message('cli/codex'),
    {headers: {'x-cli-process-ms': reported}}), {provider:'cli', localModel:'cli/codex', cliKind:'codex'});
  assert.equal((await f.send()).status, 200);
  assert.deepEqual((await f.metrics()).cliProcessMs, {count:1,p50:128.35,p95:128.35,max:128.35});
  for (const value of ['-1', 'Infinity', '180001', '1e3', '1.234']) {
    reported = value;
    assert.equal((await f.send()).status, 200);
  }
  assert.deepEqual((await f.metrics()).cliProcessMs, {count:1,p50:128.35,p95:128.35,max:128.35});
});

test('CLI failure categories and 429 count distinguish backend responses from Plan fallback', async t => {
  const cases = [
    ['status429', () => new Response('', {status:429})],
    ['status4xx', () => new Response('', {status:400})],
    ['status5xx', () => new Response('', {status:503})],
    ['validation', () => Response.json(message('wrong-model'))],
    ['connection', () => { throw new Error('fixture'); }],
    ['timeout', () => new Promise(() => {})],
  ];
  for (const [category, fail] of cases) {
    await t.test(category, async sub => {
      const f = await fixture(sub, async url => url === PLAN ? Response.json(message(body.model)) : fail(),
        {provider:'cli', localModel:'cli/codex', cliKind:'codex', attemptTimeoutMs:30});
      assert.equal((await f.send()).status, 200);
      const m = await f.metrics();
      assert.equal(m.cliFailures[category], 1);
      assert.equal(Object.values(m.cliFailures).reduce((a,b) => a+b, 0), 1);
      assert.equal(m.cliActivity.rejected, category === 'status429' ? 1 : 0);
      assert.equal(m.cliActivity.active, 0);
      assert.equal(m.routes.plan, 1);
      assert.equal(m.cliUsage.responses, 0);
    });
  }
});

test('fallback records only a bounded failure class without upstream content', async t => {
  const events = captureEvents(t);
  const secret = 'PRIVATE-UPSTREAM-RESPONSE';
  const f = await fixture(t, async url => url === PLAN ? Response.json(message(body.model)) :
    new Response(secret, { status: 415 }));
  assert.equal((await f.send()).status, 200);
  const m = await f.metrics();
  assert.equal(m.last.reason, 'fallback');
  assert.equal(m.last.fallbackFailure, 'request');
  assert.equal(events.find(e => e.event === 'route_selected' && e.provider === 'plan').detail, 'request');
  assert.ok(!JSON.stringify({ events, m }).includes(secret));
});

test('empty JSON diagnostics contain bounded enums/counts and safe usage, never content or arbitrary metadata', async t => {
  const events = captureEvents(t);
  const secret = 'PRIVATE-PAYLOAD-DO-NOT-LOG';
  const empty = message(MODEL, [{ type: 'text', text: '  ', arbitrary: secret },
    { type: 'thinking', thinking: secret }, { type: 'redacted_thinking', data: secret }]);
  empty.usage = { input_tokens: 0, output_tokens: 7, cache_read_input_tokens: -1,
    cache_creation_input_tokens: Number.MAX_SAFE_INTEGER + 1, secret };
  empty.extra = secret;
  const f = await fixture(t, async () => Response.json(empty), { localOnly: true });
  await (await f.send()).text();
  const failure = events.find(e => e.event === 'external_attempt_failed');
  assert.equal(failure.validation, 'empty_response');
  assert.deepEqual(failure.response_diagnostic, { format: 'json', stop_reason: 'end_turn', block_count: 3,
    block_types: { text: 1, thinking: 1, redacted_thinking: 1 }, usage: { input_tokens: 0, output_tokens: 7 } });
  assert.ok(!JSON.stringify(events).includes(secret));
});

test('empty SSE retains safe start/delta usage and block counts without token text', async t => {
  const events = captureEvents(t);
  const payload = frame('message_start', { message: { ...message(), content: [],
    usage: { input_tokens: 9, output_tokens: 'PRIVATE-USAGE' } } }) +
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: '  ' } }) +
    frame('content_block_stop', { index: 0 }) +
    frame('message_delta', { delta: { stop_reason: 'max_tokens' }, usage: { output_tokens: 0 } }) + frame('message_stop');
  const f = await fixture(t, async url => url === PLAN ? Response.json(message(body.model)) :
    new Response(payload, { headers: { 'content-type': 'text/event-stream' } }));
  await (await f.send(undefined, { ...body, stream: true })).text();
  const failures = events.filter(e => e.event === 'external_attempt_failed');
  assert.equal(failures.length, 4);
  assert.deepEqual(failures[0].response_diagnostic, { format: 'sse', stop_reason: 'max_tokens', block_count: 1,
    block_types: { text: 1 }, usage: { input_tokens: 9, output_tokens: 0 } });
  assert.ok(!JSON.stringify(events).includes('PRIVATE-USAGE'));
});

test('upstream thrown properties cannot spoof trusted response diagnostics', async t => {
  const events = captureEvents(t);
  const f = await fixture(t, async () => { throw Object.assign(new Error('Empty message response'), {
    responseDiagnostic: { format: 'PRIVATE-DATA', model: 'PRIVATE-DATA' },
  }); }, { localOnly: true });
  await (await f.send()).text();
  assert.equal(events.find(e => e.event === 'external_attempt_failed').response_diagnostic, undefined);
  assert.ok(!JSON.stringify(events).includes('PRIVATE-DATA'));
});

test('actual attempts and phase distributions include all failures, retries, and Plan fallback', async t => {
  const calls = [];
  const f = await fixture(t, async (url, options) => {
    calls.push(url); await delay(8);
    return url === PLAN ? Response.json(message(JSON.parse(options.body).model)) : new Response('unavailable', { status: 500 });
  });
  const empty = await f.metrics();
  assert.deepEqual(empty.routingTimingMs.total, { count: 0, p50: null, p95: null, max: null });
  assert.equal((await f.send()).status, 200);
  const m = await f.metrics();
  assert.deepEqual(calls, [LOCAL, LOCAL, LOCAL, LOCAL, PLAN]);
  assert.deepEqual(m.attemptCounts, { external: 4, plan: 1 });
  assert.deepEqual([m.attempts, m.externalAttempts, m.retries], [5, 1, 3]);
  assert.deepEqual(Object.fromEntries(Object.entries(m.routingTimingMs).map(([key, value]) => [key, value.count])),
    { queueWait: 1, externalAttempt: 4, retryWait: 3, plan: 1, total: 1 });
  assert.ok(m.routingTimingMs.externalAttempt.p50 >= 5);
  assert.ok(m.routingTimingMs.retryWait.p50 >= 1);
  assert.ok(m.routingTimingMs.plan.max >= 5);
  assert.ok(m.routingTimingMs.total.max >= m.routingTimingMs.plan.max + m.routingTimingMs.externalAttempt.max);
  assert.equal(m.latencyMs.plan.count, 1); // legacy latency still measures the complete fallback lifecycle
  assert.equal(m.latencyMs.external.count, 0);
});

test('parallel queued and active cancellation records elapsed phases once, with no extra upstream calls', async t => {
  const gate = deferred(); let calls = 0;
  const f = await fixture(t, async () => { calls++; await gate.promise; return Response.json(message()); });
  const active = new AbortController(), queued = new AbortController();
  const first = f.send(active.signal).catch(error => error);
  await waitFor(() => calls === 1);
  const second = f.send(queued.signal).catch(error => error);
  await waitFor(async () => (await f.metrics()).localAdmission.queued === 1);
  await delay(15);
  queued.abort(); await second;
  // Client disconnects arrive independently: observe removal before releasing the active slot.
  await waitFor(async () => (await f.metrics()).localAdmission.queued === 0);
  active.abort(); await first;
  await waitFor(async () => (await f.metrics()).aborted === 2);
  const m = await f.metrics();
  assert.deepEqual(m.attemptCounts, { external: 1, plan: 0 });
  assert.equal(m.routingTimingMs.queueWait.count, 2);
  assert.ok(m.routingTimingMs.queueWait.max >= 10);
  assert.equal(m.routingTimingMs.externalAttempt.count, 1);
  assert.equal(m.routingTimingMs.total.count, 2);
  assert.equal(m.routingTimingMs.plan.count, 0);
  gate.resolve(); await delay(20);
  const settled = await f.metrics();
  assert.deepEqual(settled.routingTimingMs, m.routingTimingMs);
  assert.equal(calls, 1); assert.equal(settled.localAdmission.active, 0);
});

test('cancellation during retry delay measures actual wait and never starts the next attempt', async t => {
  const f = await fixture(t, async () => new Response('unavailable', { status: 500 }), { retryDelayMs: 1000 });
  const controller = new AbortController();
  const pending = f.send(controller.signal).catch(error => error);
  await waitFor(async () => (await f.metrics()).routingTimingMs.externalAttempt.count === 1);
  await delay(15); controller.abort(); await pending;
  await waitFor(async () => (await f.metrics()).aborted === 1);
  const m = await f.metrics();
  assert.deepEqual(m.attemptCounts, { external: 1, plan: 0 });
  assert.equal(m.routingTimingMs.retryWait.count, 1);
  assert.ok(m.routingTimingMs.retryWait.max >= 10 && m.routingTimingMs.retryWait.max < 1000);
  assert.equal(m.routingTimingMs.total.count, 1);
});

test('a committed stream failure records one attempt and cannot replay or fall back', async t => {
  let calls = 0;
  const payload = frame('message_start', { message: { ...message(), content: [] } }) +
    frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
    frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'visible once' } });
  const f = await fixture(t, async () => { calls++; return new Response(payload,
    { headers: { 'content-type': 'text/event-stream' } }); }, { streamPassthrough: true });
  const response = await f.send(undefined, { ...body, stream: true });
  const text = await response.text();
  assert.equal(text.match(/visible once/g).length, 1); assert.match(text, /upstream stream failed/);
  const m = await f.metrics();
  assert.equal(calls, 1); assert.equal(m.failed, 1);
  assert.deepEqual(m.attemptCounts, { external: 1, plan: 0 });
  assert.equal(m.routingTimingMs.externalAttempt.count, 1);
  assert.equal(m.routingTimingMs.total.count, 1);
  assert.equal(m.routingTimingMs.retryWait.count, 0);
});

test('tool id and input roundtrip remain exact with observability enabled', async t => {
  const tool = { type: 'tool_use', id: 'call_fixture-123', name: 'ssh_exec',
    input: { command: 'echo fixture', nested: { values: [0, false, 'value'] } } };
  let incoming;
  const f = await fixture(t, async (_url, options) => {
    incoming = JSON.parse(options.body);
    return Response.json({ ...message(MODEL, [tool]), stop_reason: 'tool_use' });
  });
  const request = { ...body, tools: [{ name: 'ssh_exec', input_schema: { type: 'object', properties: {} } }],
    messages: [...body.messages, { role: 'assistant', content: [tool] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: tool.id, content: 'previous result' }] }] };
  const result = await (await f.send(undefined, request)).json();
  assert.deepEqual(result.content, [tool]); assert.deepEqual(incoming.messages, request.messages);
  const m = await f.metrics();
  assert.equal(m.ok, 1); assert.equal(m.routingTimingMs.total.count, 1);
  assert.deepEqual(m.attemptCounts, { external: 1, plan: 0 });
});

test('phase samples stay bounded at 512 while actual attempt counters remain cumulative', async t => {
  t.mock.method(console, 'error', () => {});
  const f = await fixture(t, async () => Response.json(message()));
  for (let index = 0; index < 515; index++) await (await f.send()).text();
  const m = await f.metrics();
  assert.deepEqual(m.attemptCounts, { external: 515, plan: 0 });
  for (const phase of ['queueWait', 'externalAttempt', 'total']) assert.equal(m.routingTimingMs[phase].count, 512);
  assert.equal(m.requests, 515);
});

test('Plan cancellation and failure keep their own durations without external samples', async t => {
  const gate = deferred(); let calls = 0;
  const f = await fixture(t, async url => {
    assert.equal(url, PLAN);
    if (++calls === 1) await gate.promise;
    return new Response('unavailable', { status: 503 });
  });
  const controller = new AbortController();
  const request = { ...body, model: 'claude-haiku-4-5' };
  const pending = f.send(controller.signal, request).catch(error => error);
  await waitFor(() => calls === 1); await delay(15); controller.abort(); await pending;
  await waitFor(async () => (await f.metrics()).aborted === 1);
  const cancelled = await f.metrics();
  assert.equal(cancelled.routingTimingMs.plan.count, 1);
  assert.ok(cancelled.routingTimingMs.plan.max >= 10);
  assert.equal(cancelled.routingTimingMs.total.count, 1);
  gate.resolve(); await delay(10);
  assert.deepEqual((await f.metrics()).routingTimingMs, cancelled.routingTimingMs);
  assert.equal((await f.send(undefined, request)).status, 503);
  const m = await f.metrics();
  assert.deepEqual(m.attemptCounts, { external: 0, plan: 2 });
  assert.equal(m.routingTimingMs.plan.count, 2);
  assert.equal(m.routingTimingMs.total.count, 2);
  assert.equal(m.routingTimingMs.externalAttempt.count, 0);
  assert.equal(m.failed, 2);
});

test('a timed out external fetch records elapsed failure once even if fetch ignores abort', async t => {
  const gate = deferred();
  const f = await fixture(t, async () => { await gate.promise; return Response.json(message()); },
    { localOnly: true, attemptTimeoutMs: 20 });
  assert.equal((await f.send()).status, 400);
  const m = await f.metrics();
  assert.deepEqual(m.attemptCounts, { external: 1, plan: 0 });
  assert.equal(m.routingTimingMs.externalAttempt.count, 1);
  assert.ok(m.routingTimingMs.externalAttempt.max >= 10);
  assert.equal(m.routingTimingMs.total.count, 1);
  gate.resolve(); await delay(10);
  assert.deepEqual((await f.metrics()).routingTimingMs, m.routingTimingMs);
});
