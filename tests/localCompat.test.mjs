import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import { listenSafe } from './listenSafe.mjs';
import { createProxy } from '../claudeRouteProxy.mjs';

const config = { nonce: 'fixture-route', provider: 'local', localOnly: true, localCompat: true,
  localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-model', localKey: 'fixture-key' };
const reply = content => Response.json({ id: 'fixture-message', type: 'message', role: 'assistant',
  model: config.localModel, content: content || [{ type: 'text', text: 'OK' }], stop_reason: 'end_turn',
  usage: { input_tokens: 2, output_tokens: 1 } });
async function session(t, fetchImpl, overrides = {}) {
  const calls = [];
  const server = createProxy({ ...config, ...overrides, fetchImpl: async (url, options) => {
    calls.push({ url, ...options });
    return fetchImpl(url, options);
  } });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { calls, send: (extra = {}, path = '', beta = 'interleaved-thinking-2025-05-14,effort-2025-11-24') => fetch(`http://127.0.0.1:${server.address().port}/v1/messages${path}`, {
    method: 'POST', headers: { 'x-local-route-token': config.nonce, 'content-type': 'application/json',
      authorization: 'Bearer synthetic-client-token', 'anthropic-beta': beta },
    body: JSON.stringify({ model: 'sonnet', max_tokens: 8192, messages: [{ role: 'user', content: 'fixture' }], ...extra }),
  }) };
}

for (const model of ['sonnet', 'opus', 'haiku', 'fable', 'claude-opus-5-5', 'claude-haiku-5-5']) {
  test(`compat routes ${model} to the same Local model without client credentials`, async t => {
    const s = await session(t, () => reply());
    const r = await s.send({ model, thinking: { type: 'adaptive' }, output_config: { effort: 'high' } });
    assert.equal(r.status, 200); assert.equal((await r.json()).model, model);
    assert.equal(s.calls.length, 1); const call = s.calls[0];
    assert.equal(call.url, config.localUrl); assert.equal(call.headers.authorization, 'Bearer fixture-key');
    assert.equal(call.headers['x-local-route-token'], undefined);
    assert.match(call.headers['anthropic-beta'], /effort/);
    const body = JSON.parse(call.body);
    assert.equal(body.model, config.localModel); assert.deepEqual(body.thinking, { type: 'adaptive' });
    assert.deepEqual(body.output_config, { effort: 'high' });
  });
}
test('compat drops signed thinking history and keeps opaque tool metadata', async t => {
  const s = await session(t, () => reply());
  const content = [{ type: 'thinking', thinking: 'fixture', signature: 'opaque-signature', provider_specific_fields: { opaque: 'keep-thinking' } },
    { type: 'tool_use', id: 'tool-fixture', name: 'mcp__fixture__echo', input: {}, provider_specific_fields: { opaque: 'keep' } }];
  assert.equal((await s.send({ messages: [{ role: 'assistant', content }] })).status, 200);
  const forwarded = JSON.parse(s.calls[0].body).messages[0].content;
  assert.deepEqual(forwarded, [content[1]]);
  assert.equal(JSON.stringify(forwarded).includes('opaque-signature'), false);
  assert.equal(JSON.stringify(forwarded).includes('opaque'), true);
});
test('compat synthesizes thinking/signature deltas only when upstream returns JSON', async t => {
  const s = await session(t, () => reply([{ type: 'thinking', thinking: 'fixture', signature: 'opaque-signature' },
    { type: 'text', text: 'OK' }]));
  const r = await s.send({ stream: true }); const body = await r.text();
  assert.equal(r.status, 200); assert.match(body, /thinking_delta/); assert.match(body, /signature_delta/);
  assert.match(body, /opaque-signature/); assert.match(body, /message_stop/);
});
test('count_tokens and unknown native fields fail explicitly without any upstream or Plan request', async t => {
  const s = await session(t, () => reply());
  assert.equal((await s.send({}, '/count_tokens')).status, 400);
  assert.equal((await s.send({ unknown_native_field: true })).status, 400);
  assert.equal((await s.send({}, '', 'unknown-feature-2099-01-01')).status, 400);
  assert.equal(s.calls.length, 0);
  assert.equal((await s.send()).status, 200); assert.equal(s.calls.length, 1);
});
for (const fields of [{ messages: [null] }, { thinking: { type: 'adaptive', display: 'unknown' } },
  { context_management: 'invalid' }, { output_config: { effort: 'unknown' } },
  { tools: [{ name: 'fixture', input_schema: {}, defer_loading: 'invalid' }] }]) {
  test('compat rejects malformed extension contracts without upstream calls', async t => {
    const s = await session(t, () => reply());
    assert.equal((await s.send(fields)).status, 400); assert.equal(s.calls.length, 0);
  });
}
test('compat failures have one attempt and no Plan fallback', async t => {
  const s = await session(t, () => new Response('private-body', { status: 401 }));
  const r = await s.send(); const text = await r.text();
  assert.equal(r.status, 400); assert.match(text, /local_authentication_failed/);
  assert.doesNotMatch(text, /private-body/); assert.equal(s.calls.length, 1);
});
test('compat cannot be enabled outside isolated Local-only routing', () => {
  assert.throws(() => createProxy({ ...config, localOnly: false }));
  assert.throws(() => createProxy({ ...config, provider: 'openrouter' }));
});
test('CLI per-turn settings, context management and deferred custom tools round-trip without stripping', async t => {
  const s = await session(t, () => reply());
  const fields = { context_management: { edits: [{ type: 'clear_thinking_20251015', keep: 'all' }] },
    thinking: { type: 'adaptive', display: 'updates' }, output_config: { effort: 'medium',
      format: { type: 'json_schema', schema: { type: 'object', properties: {} } } },
    messages: [{ role: 'user', content: 'fixture' }, { role: 'system', content: [{ type: 'text', text: 'fixture reminder' }], output_config: { effort: 'low' } }],
    tools: [{ name: 'mcp__fixture__echo', input_schema: { type: 'object' }, defer_loading: true, strict: true }] };
  assert.equal((await s.send(fields)).status, 200);
  const body = JSON.parse(s.calls[0].body);
  for (const name of Object.keys(fields)) assert.deepEqual(body[name], fields[name]);
  assert.equal(body.stream, false);
});
for (const content of [[{ type: 'thinking', thinking: 'fixture', signature: 42 }, { type: 'text', text: 'OK' }],
  [{ type: 'tool_use', id: 'fixture', name: 'echo', input: {}, provider_specific_fields: [] }]]) {
  test('compat rejects malformed opaque response fields before publishing any tool', async t => {
    const s = await session(t, () => reply(content)); const r = await s.send({ stream: true });
    assert.equal(r.status, 400); assert.equal(s.calls.length, 1); assert.doesNotMatch(await r.text(), /content_block_start/);
  });
}
test('compat preserves nested MCP tool references only for defined tools', async t => {
  const s = await session(t, () => reply());
  const fields = { tools: [{ name: 'mcp__fixture__echo', input_schema: { type: 'object' }, defer_loading: true }],
    messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'fixture-search',
      content: [{ type: 'tool_reference', tool_name: 'mcp__fixture__echo' }] }] }] };
  assert.equal((await s.send(fields)).status, 200);
  assert.deepEqual(JSON.parse(s.calls[0].body).messages, fields.messages);
  fields.messages[0].content[0].content[0].tool_name = 'unknown_tool';
  assert.equal((await s.send(fields)).status, 400); assert.equal(s.calls.length, 1);
});
