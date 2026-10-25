// Observability: one traceId correlates every diagnostic line of a turn, and one redaction
// sink guarantees a credential can never leave through a log line or an error body.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy, redactDiagnostic } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'observability-nonce';
const MODEL = 'claude-sonnet-5-5';
const UPSTREAM_MODEL = 'observability/model';
const OAUTH = 'Bearer sk-ant-oat-observability';
const BETA = 'oauth-2025-04-20';
const PLAN = 'https://api.anthropic.com/v1/messages';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json', authorization: OAUTH, 'anthropic-beta': BETA };
const TRACE_ID = /^[0-9a-f]{8}$/;

const textMessage = (model, text) => ({ id: 'msg_observability', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const externalReply = text => Response.json(textMessage(UPSTREAM_MODEL, text));
const planReply = text => Response.json(textMessage(MODEL, text));
const requestBody = (extra = {}) => JSON.stringify({ model: MODEL, max_tokens: 16, stream: false,
  messages: [{ role: 'user', content: 'hi' }], ...extra });

// Capture the single sink without weakening it: the proxy writes JSON through console.error.
function captureConsole(t) {
  const lines = [];
  const originalError = console.error, originalLog = console.log;
  const record = (...args) => { lines.push(args.map(value => typeof value === 'string' ? value : String(value)).join(' ')); };
  console.error = record;
  console.log = record;
  t.after(() => {
    console.error = originalError;
    console.log = originalLog;
  });
  const entries = () => lines.map(line => { try { return JSON.parse(line); } catch { return null; } }).filter(Boolean);
  return { lines, entries };
}

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-observability', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 500, onHandover: () => {}, ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    base,
    send: (extra = {}) => fetch(`${base}/v1/messages`, { method: 'POST', headers: HEADERS, body: requestBody(extra) }),
    metrics: async () => {
      const response = await fetch(`${base}/api/metrics`, { headers: AUTH });
      assert.equal(response.status, 200);
      return response.json();
    },
  };
}

test('one turn tags every diagnostic line and metrics.last with the same traceId',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return planReply('plan-ok');
        return new Response('upstream down', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 200);
    await response.text();
    const entries = logs.entries();
    const attempts = entries.filter(entry => entry.event === 'external_attempt_failed');
    assert.equal(attempts.length, 4, 'the failed stream is fully logged');
    // Existing keys keep their meaning and the correlation key is additive.
    assert.equal(attempts[0].status, 500);
    assert.equal(attempts[0].attempt, 1);
    assert.equal(attempts[0].category, 'upstream');
    assert.ok(entries.some(entry => entry.event === 'circuit_open'));
    assert.ok(entries.some(entry => entry.event === 'route_selected' && entry.provider === 'plan'));
    const traceIds = new Set(entries.map(entry => entry.traceId));
    assert.equal(traceIds.size, 1, 'every line of one turn shares one traceId');
    const [traceId] = traceIds;
    assert.match(traceId, TRACE_ID);
    const payload = await proxy.metrics();
    assert.equal(payload.last.traceId, traceId, 'metrics.last carries the turn traceId');
    assert.equal(payload.last.status, 200);
  });

test('a successful replacement turn logs its traceId and exposes it in metrics.last',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return planReply('plan-ok');
        return externalReply('external-ok');
      } });
    const response = await proxy.send();
    assert.equal(response.status, 200);
    await response.text();
    const entries = logs.entries();
    const selected = entries.filter(entry => entry.event === 'route_selected');
    assert.equal(selected.length, 1);
    assert.match(selected[0].traceId, TRACE_ID);
    assert.equal(selected[0].provider, 'openrouter');
    assert.equal(selected[0].model, UPSTREAM_MODEL);
    const payload = await proxy.metrics();
    assert.equal(payload.last.traceId, selected[0].traceId);
    assert.equal(payload.last.route, 'external');
  });

test('concurrent turns are not cross-correlated', { timeout: 15000 }, async t => {
  const logs = captureConsole(t);
  const releases = [];
  let bothEntered;
  const ready = new Promise(resolve => { bothEntered = resolve; });
  const proxy = await startProxy(t, {
    fetchImpl: async url => {
      if (url === PLAN) return planReply('plan-ok');
      return new Promise(resolve => {
        releases.push(() => resolve(externalReply('external-ok')));
        if (releases.length === 2) bothEntered();
      });
    } });
  const first = proxy.send();
  const second = proxy.send();
  await ready;
  for (const release of releases) release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.status, 200);
  assert.equal(b.status, 200);
  await a.text();
  await b.text();
  const selected = logs.entries().filter(entry => entry.event === 'route_selected');
  assert.equal(selected.length, 2);
  assert.equal(new Set(selected.map(entry => entry.traceId)).size, 2, 'each turn keeps its own traceId');
  for (const entry of selected) assert.match(entry.traceId, TRACE_ID);
});

test('a broken passthrough stream keeps one traceId across its lines', { timeout: 15000 }, async t => {
  const logs = captureConsole(t);
  const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
  const proxy = await startProxy(t, { streamPassthrough: true,
    fetchImpl: async url => {
      if (url === PLAN) return planReply('plan-ok');
      const encoder = new TextEncoder();
      return new Response(new ReadableStream({
        async start(controller) {
          controller.enqueue(encoder.encode(frame('message_start', { message: { id: 'msg_stream', type: 'message',
            role: 'assistant', model: UPSTREAM_MODEL, usage: { input_tokens: 1, output_tokens: 1 } } })));
          controller.enqueue(encoder.encode(frame('content_block_start', { index: 0,
            content_block: { type: 'text', text: '' } })));
          await new Promise(resolve => setTimeout(resolve, 20));
          controller.enqueue(encoder.encode(frame('content_block_delta', { index: 0,
            delta: { type: 'text_delta', text: 'hello' } })));
          await new Promise(resolve => setTimeout(resolve, 20));
          controller.error(new Error('socket reset'));
        },
      }), { headers: { 'content-type': 'text/event-stream' } });
    } });
  const response = await proxy.send({ stream: true });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /hello/);
  assert.match(body, /upstream stream failed/);
  const entries = logs.entries();
  const selected = entries.filter(entry => entry.event === 'route_selected');
  const broken = entries.filter(entry => entry.event === 'stream_broken');
  assert.equal(selected.length, 1);
  assert.equal(broken.length, 1);
  assert.equal(selected[0].traceId, broken[0].traceId, 'a failed stream is reconstructable from one traceId');
  assert.match(selected[0].traceId, TRACE_ID);
});

test('a credential in an upstream error body is redacted in the client body and the logs',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const CONFIGURED_KEY = 'sk-or-v1-configured-secret-key';
    const bearerSecret = 'sk-ant-oat-SECRET-123';
    const bodySecret = 'sk-or-v1-SECRET456';
    const upstreamBody = JSON.stringify({ error: { type: 'rate_limit_error',
      message: `quota reached for Bearer ${bearerSecret} and x-api-key: ${bodySecret}`,
      hint: `configured credential ${CONFIGURED_KEY}`,
      tagged: 'Bearer [redacted:bearer-token]' } });
    const proxy = await startProxy(t, { key: CONFIGURED_KEY,
      fetchImpl: async url => {
        if (url === PLAN) return new Response(upstreamBody, { status: 429, headers: { 'content-type': 'application/json' } });
        return new Response('external failed', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 429, 'the Plan status is preserved');
    const clientBody = await response.text();
    for (const secret of [bearerSecret, bodySecret, CONFIGURED_KEY])
      assert.equal(clientBody.includes(secret), false, `the client response leaked ${secret}`);
    assert.match(clientBody, /\[redacted:bearer-token\]/);
    assert.match(clientBody, /\[redacted:api-key\]/);
    assert.match(clientBody, /\[redacted:provider-key\]/);
    const allLogs = logs.lines.join('\n');
    for (const secret of [bearerSecret, bodySecret, CONFIGURED_KEY])
      assert.equal(allLogs.includes(secret), false, `a log line leaked ${secret}`);
    assert.match(allLogs, /\[redacted:(?:bearer-token|api-key|provider-key)\]/);
    // The scrubbed excerpt passes the sink again; a tag already present must not be masked twice.
    assert.equal(allLogs.includes('[redacted:bearer-token]]'), false, 'no double-masking artifact in logs');
    assert.equal(allLogs.includes('[[redacted'), false);
    assert.ok(logs.entries().some(entry => entry.event === 'upstream_error_redacted'),
      'the scrubbed excerpt is logged for triage');
    // Non-secret context survives so the log stays useful.
    assert.match(clientBody, /rate_limit_error/);
  });

test('already-clean and already-tagged error text is relayed without double masking',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const clean = '{"error":{"message":"rate limited","tag":"[redacted:api-key]",' +
      '"bearer":"Bearer [redacted:bearer-token]"}}';
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return new Response(clean, { status: 429, headers: { 'content-type': 'application/json' } });
        return new Response('external failed', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 429);
    const text = await response.text();
    assert.equal(text, clean, 'clean, pre-tagged text is relayed byte-identically');
    assert.equal((text.match(/\[redacted:api-key\]/g) || []).length, 1);
    assert.equal((text.match(/\[redacted:bearer-token\]/g) || []).length, 1, 'the existing tag is not duplicated');
    assert.equal(text.includes('[[redacted'), false);
    assert.equal(text.includes('[redacted:bearer-token]]'), false);
    assert.equal(logs.entries().some(entry => entry.event === 'upstream_error_redacted'), false,
      'clean text does not trigger a scrubbing event');
  });

test('mixed secret and pre-tagged text keeps a single stable tag per value',
  { timeout: 15000 }, async t => {
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return new Response(`Bearer sk-ant-oat-XYZZY and sk-or-v1-ABCDEF and [redacted:api-key]`,
          { status: 400, headers: { 'content-type': 'text/plain' } });
        return new Response('external failed', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 400);
    const text = await response.text();
    assert.equal((text.match(/\[redacted:bearer-token\]/g) || []).length, 1);
    assert.equal((text.match(/\[redacted:api-key\]/g) || []).length, 2);
    assert.equal(text.includes('XYZZY'), false);
    assert.equal(text.includes('ABCDEF'), false);
    assert.equal(text.includes('[[redacted'), false);
  });

test('the logging sink redacts an unscrubbed excerpt before it is written',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const secret = 'sk-or-v1-SINK-SECRET';
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return new Response(JSON.stringify({ error: { message: `quota ${secret}` } }),
          { status: 429, headers: { 'content-type': 'application/json' } });
        return new Response('external failed', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 429);
    await response.text();
    // The proxy hands the raw excerpt to the sink; only the sink may scrub the written line.
    const line = logs.lines.find(text => text.includes('"upstream_error_redacted"'));
    assert.ok(line, 'the scrubbed excerpt is logged');
    assert.equal(line.includes(secret), false, 'the sink removed the raw credential');
    assert.match(line, /\[redacted:api-key\]/);
    assert.equal(line.includes('[[redacted'), false);
    const entry = JSON.parse(line);
    assert.match(entry.traceId, TRACE_ID);
    assert.equal(entry.excerpt.includes(secret), false);
  });

test('an oversized upstream error body is truncated and cancelled, never buffered in full',
  { timeout: 15000 }, async t => {
    const logs = captureConsole(t);
    const limit = 64 * 1024;
    const tail = 'TAIL_MARKER_MUST_NOT_ESCAPE';
    const upstreamBody = 'A'.repeat(limit + 1024) + tail;
    const proxy = await startProxy(t, {
      fetchImpl: async url => {
        if (url === PLAN) return new Response(upstreamBody, { status: 500, headers: { 'content-type': 'text/plain' } });
        return new Response('external failed', { status: 500 });
      } });
    const response = await proxy.send();
    assert.equal(response.status, 500);
    const text = await response.text();
    assert.equal(text.length, limit, 'only the bounded prefix is relayed');
    assert.equal(text.includes(tail), false, 'bytes past the cap are cancelled, not relayed');
    const entry = logs.entries().find(item => item.event === 'upstream_error_truncated');
    assert.ok(entry, 'truncation is logged');
    assert.equal(entry.truncated, true);
    assert.equal(entry.bytes, limit);
    assert.match(entry.traceId, TRACE_ID);
  });

test('redactDiagnostic is idempotent and never touches model, trace or status context', () => {
  const raw = 'Bearer sk-ant-oat-ABC123 model claude-sonnet-5-5 trace deadbeef status 429 ' +
    'sk-or-v1-DEF456 x-api-key: GHI789';
  const once = redactDiagnostic(raw);
  assert.equal(redactDiagnostic(once), once, 'a second pass changes nothing');
  assert.match(once, /Bearer \[redacted:bearer-token\]/);
  assert.equal(once.includes('ABC123'), false);
  assert.equal(once.includes('DEF456'), false);
  assert.equal(once.includes('GHI789'), false);
  for (const keep of ['claude-sonnet-5-5', 'deadbeef', '429']) assert.ok(once.includes(keep), `${keep} was redacted`);
  assert.equal(once.includes('[[redacted'), false);
  // A configured secret without a recognizable prefix still gets the provider tag.
  assert.equal(redactDiagnostic('hint abcdef123456', ['abcdef123456']), 'hint [redacted:provider-key]');
});
