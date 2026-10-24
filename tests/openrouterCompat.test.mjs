import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import { listenSafe } from './listenSafe.mjs';
import { createProxy, checkConnection } from '../claudeRouteProxy.mjs';

const MODEL = 'fixture-vendor/fixture-model', KEY = 'sk-or-synthetic-key', NONCE = 'synthetic-route-token';
const URL = 'https://openrouter.ai/api/v1/messages';
const reply = (extra = {}) => Response.json({ id: 'fixture-message', type: 'message', role: 'assistant', model: MODEL,
  content: [{ type: 'text', text: 'OK' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 }, ...extra });
async function session(t, upstream = () => reply(), options = {}) {
  const calls = [];
  const server = createProxy({ provider: 'openrouter', openrouterCompat: true, key: KEY, nonce: NONCE,
    openrouterModel: MODEL, ...options, fetchImpl: async (url, request) => {
      calls.push({ url, ...request, parsed: JSON.parse(request.body) }); return upstream(url, request);
    } });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { calls, send: (extra = {}, path = '', beta = 'interleaved-thinking-2025-05-14,effort-2025-11-24', signal) => fetch(
    `http://127.0.0.1:${server.address().port}/v1/messages${path}`, { method: 'POST', signal,
      headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE,
        authorization: 'Bearer sk-ant-oat-synthetic-plan-token', 'anthropic-beta': beta },
      body: JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 8192, messages: [{ role: 'user', content: 'fixture' }], ...extra }) }) };
}

for (const model of ['sonnet', 'opus', 'haiku', 'fable', 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-fable-5-1']) {
  test(`OpenRouter compatibility maps ${model} to the selected model without Plan authentication`, async t => {
    const s = await session(t); const r = await s.send({ model, thinking: { type: 'adaptive' }, output_config: { effort: 'high' }, stream: true });
    assert.equal(r.status, 200); assert.match(await r.text(), /message_stop/);
    assert.equal(s.calls.length, 1); const c = s.calls[0]; assert.equal(c.url, URL);
    assert.equal(c.headers.authorization, `Bearer ${KEY}`); assert.equal(c.headers['x-local-route-token'], undefined);
    assert.equal(c.headers['x-api-key'], undefined); assert.match(c.headers['anthropic-beta'], /interleaved-thinking/);
    assert.equal(c.parsed.model, MODEL); assert.equal(c.parsed.stream, false);
    assert.deepEqual(c.parsed.provider, { allow_fallbacks: false });
    assert.deepEqual(c.parsed.thinking, { type: 'adaptive' }); assert.deepEqual(c.parsed.output_config, { effort: 'high' });
  });
}
test('OpenRouter compatibility preserves native history, tool references and opaque metadata', async t => {
  const s = await session(t);
  const fields = { context_management: { edits: [{ type: 'clear_thinking_20251015', keep: 'all' }] },
    thinking: { type: 'adaptive', display: 'updates' }, output_config: { effort: 'low' },
    tools: [{ name: 'mcp__fixture__echo', input_schema: { type: 'object' }, defer_loading: true }],
    messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'fixture', signature: 'opaque-signature' },
      { type: 'tool_use', id: 'fixture-tool', name: 'mcp__fixture__echo', input: {}, provider_specific_fields: { state: 'opaque' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'fixture-tool', content: [{ type: 'tool_reference', tool_name: 'mcp__fixture__echo' }] }] },
    { role: 'system', content: [{ type: 'text', text: 'fixture' }], output_config: { effort: 'low' } }] };
  assert.equal((await s.send(fields)).status, 200);
  for (const field of Object.keys(fields)) assert.deepEqual(s.calls[0].parsed[field], fields[field]);
});
test('OpenRouter compatibility accepts provider citation fields on text blocks', async t => {
  const s = await session(t, () => reply({ content: [{ type: 'text', text: 'OK', citations: [{ cited_text: 'fixture' }] }] }));
  const r = await s.send({ stream: true });
  assert.equal(r.status, 200);
  const text = await r.text();
  assert.match(text, /message_stop/);
  assert.match(text, /citations/);
});
test('unsupported features and token counting never call OpenRouter or Plan', async t => {
  const s = await session(t);
  assert.equal((await s.send({}, '/count_tokens')).status, 400);
  assert.equal((await s.send({ unknown_feature: true })).status, 400);
  assert.equal((await s.send({}, '', 'unknown-feature-2099-01-01')).status, 400);
  assert.equal(s.calls.length, 0);
});
for (const [kind, failure, code] of [
  ['authentication', () => new Response('private-body', { status: 401 }), 'openrouter_authentication_failed'],
  ['upstream', () => new Response('private-body', { status: 503 }), 'openrouter_upstream_error'],
  ['empty', () => reply({ content: [] }), 'openrouter_empty_response'],
  ['wrong model', () => reply({ model: 'wrong-model' }), 'openrouter_upstream_error'],
  ['timeout', () => new Promise(() => {}), 'openrouter_timeout'],
]) {
  test(`OpenRouter ${kind} fails once, stays off Plan during cooldown and recovers`, async t => {
    let healthy = false, time = 1000;
    const s = await session(t, () => healthy ? reply() : failure(), { attemptTimeoutMs: 300, cooldownMs: 1000, now: () => time });
    const r = await s.send(); const body = await r.text(); assert.equal(r.status, 400); assert.match(body, new RegExp(code));
    assert.doesNotMatch(body, /private-body|sk-or-synthetic-key/); assert.equal(s.calls.length, 1);
    assert.equal((await s.send()).status, 400); assert.equal(s.calls.length, 1);
    time += 1001; healthy = true;
    assert.equal((await s.send()).status, 200); assert.equal(s.calls.length, 2);
    assert.ok(s.calls.every(call => call.url === URL));
  });
}
test('client cancellation stops a pending OpenRouter request without retry or Plan', async t => {
  let enter, upstreamSignal; const entered = new Promise(resolve => { enter = resolve; });
  const s = await session(t, (_url, request) => { upstreamSignal = request.signal; enter(); return new Promise(() => {}); });
  const client = new AbortController(); const pending = s.send({}, '', '', client.signal);
  await entered; client.abort(); await assert.rejects(pending);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(upstreamSignal.aborted, true); assert.equal(s.calls.length, 1); assert.equal(s.calls[0].url, URL);
});
test('OpenRouter compatibility rejects incompatible provider options', () => {
  for (const extra of [{ provider: 'local' }, { localOnly: true }, { localCompat: true }, { openrouterCompat: 'true' }])
    assert.throws(() => createProxy({ provider: 'openrouter', openrouterCompat: true, key: KEY, nonce: NONCE, openrouterModel: MODEL, ...extra }));
});

for (const provider of ['openrouter', 'local']) {
  test(`${provider} connection check confirms inference and outputs only safe fields`, async () => {
    const calls = [];
    const result = await checkConnection({ provider, key: KEY, openrouterModel: MODEL,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: MODEL, localKey: KEY,
      fetchImpl: async (url, request) => { calls.push({ url, ...request }); return reply({ content: [{ type: 'text', text: 'LOCAL_DIAGNOSTIC_OK' }] }); } });
    assert.deepEqual(result, { connection_check: true, provider, inference_checked: true, model_confirmed: true, reply_confirmed: true, reason: 'ready' });
    assert.equal(calls.length, 1); assert.equal(calls[0].url, provider === 'openrouter' ? URL : 'http://127.0.0.1:11434/v1/messages');
    assert.equal(calls[0].headers.authorization, `Bearer ${KEY}`);
    assert.doesNotMatch(JSON.stringify(result), /fixture-vendor|sk-or|127\.0/);
  });
}
for (const [failure, reason] of [[() => new Response('private-body', { status: 401 }), 'authentication_failed'],
  [() => reply({ content: [{ type: 'text', text: 'extra reply' }] }), 'reply_mismatch'],
  [() => new Promise(() => {}), 'timeout']]) {
  test(`connection check reports ${reason} without retries or Plan calls`, async () => {
    const calls = []; const r = await checkConnection({ provider: 'openrouter', key: KEY, openrouterModel: MODEL, timeoutMs: 20,
      fetchImpl: async url => { calls.push(url); return failure(); } });
    assert.equal(r.connection_check, false); assert.equal(r.reason, reason); assert.deepEqual(calls, [URL]);
    assert.doesNotMatch(JSON.stringify(r), /private-body|extra reply|sk-or/);
  });
}
