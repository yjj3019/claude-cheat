import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import { listenSafe } from './listenSafe.mjs';
import { createProxy, diagnoseLocal, DEEPSEEK } from '../claudeRouteProxy.mjs';

const diagnosticConfig = { localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'diagnostic-fixture', localKey: 'synthetic-local-key' };
const diagnosticMessage = extra => ({ type: 'message', model: diagnosticConfig.localModel,
  content: [{ type: 'text', text: 'LOCAL_DIAGNOSTIC_OK' }], stop_reason: 'end_turn', ...extra });

test('Local diagnostic sends one minimal request with isolated authentication and confirms inference', async () => {
  const calls = [];
  const result = await diagnoseLocal({ ...diagnosticConfig, fetchImpl: async (url, options) => {
    calls.push({ url, ...options });
    return Response.json(diagnosticMessage());
  } });
  assert.deepEqual(result, { local_diagnostic: true, inference_checked: true, model_confirmed: true,
    reply_confirmed: true, reason: 'ready_inference_only' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, diagnosticConfig.localUrl);
  assert.equal(calls[0].redirect, 'error');
  assert.deepEqual(Object.keys(calls[0].headers).sort(), ['anthropic-version', 'authorization', 'content-type']);
  assert.equal(calls[0].headers.authorization, 'Bearer synthetic-local-key');
  const body = JSON.parse(calls[0].body);
  assert.deepEqual(Object.keys(body).sort(), ['max_tokens', 'messages', 'model', 'stream']);
  assert.equal(body.stream, false);
  assert.equal(body.messages.length, 1);
  assert.equal(body.model, diagnosticConfig.localModel);
});

for (const [label, reply, reason] of [
  ['wrong model', diagnosticMessage({ model: 'other-model' }), 'invalid_response'],
  ['wrong reply', diagnosticMessage({ content: [{ type: 'text', text: 'private-answer-marker' }] }), 'reply_mismatch'],
  ['truncated reply', diagnosticMessage({ stop_reason: 'max_tokens' }), 'incomplete_response'],
  ['empty reply', diagnosticMessage({ content: [] }), 'invalid_response'],
]) {
  test(`Local diagnostic rejects ${label} without Plan fallback or leaking response content`, async () => {
    let calls = 0;
    const result = await diagnoseLocal({ ...diagnosticConfig, fetchImpl: async url => {
      assert.equal(url, diagnosticConfig.localUrl);
      calls++;
      return Response.json(reply);
    } });
    assert.equal(calls, 1);
    assert.equal(result.local_diagnostic, false);
    assert.equal(result.reason, reason);
    assert.equal(JSON.stringify(result).includes('private-answer-marker'), false);
  });
}

for (const status of [302, 401, 500]) {
  test(`Local diagnostic HTTP ${status} is reported once without retry or Plan fallback`, async () => {
    let calls = 0;
    const result = await diagnoseLocal({ ...diagnosticConfig, fetchImpl: async () => {
      calls++;
      return new Response('private-error-marker', { status });
    } });
    assert.equal(calls, 1);
    assert.deepEqual(result, { local_diagnostic: false, inference_checked: true, reason: 'http_error', status });
  });
}

test('Local diagnostic deadline covers a stalled response body', async () => {
  const result = await diagnoseLocal({ ...diagnosticConfig, timeoutMs: 20,
    fetchImpl: async () => new Response(new ReadableStream({ start() {} })) });
  assert.deepEqual(result, { local_diagnostic: false, inference_checked: true, reason: 'timeout' });
});

test('Local diagnostic connection errors are fixed categories without retry', async () => {
  let calls = 0;
  const result = await diagnoseLocal({ ...diagnosticConfig, fetchImpl: async () => { calls++; throw new Error('private-error-marker'); } });
  assert.equal(calls, 1);
  assert.deepEqual(result, { local_diagnostic: false, inference_checked: true, reason: 'connection_failed' });
});

for (const localUrl of ['http://127.0.0.1:0/v1/messages', 'http://127.0.0.1:11434/v1/mes\nsages', 'https://llm.example.invalid:0/v1/messages']) {
  test('Local URL rejects zero port and embedded control characters: ' + JSON.stringify(localUrl), () => {
    assert.throws(() => createProxy({ nonce: 'fixture-nonce', provider: 'local', localUrl,
      localModel: 'fixture-model', localAllowRemote: true }));
  });
}

// Deliberately synthetic values: these tests never read credentials or contact an upstream.
const KEY = 'test-openrouter-key';
const NONCE = 'test-local-nonce';
const OAUTH = 'Bearer sk-ant-oat-test-fixture';
const BETA = 'oauth-2025-04-20';
const OPUS = 'claude-opus-5-5';
const HAIKU = 'claude-haiku-5-5';
const FABLE = 'claude-fable-5-1';
const requestBody = (model = DEEPSEEK, extra = {}) => ({
  model, max_tokens: 64, messages: [{ role: 'user', content: 'fixture' }], ...extra,
});
const jsonResponse = (_url, options) => new Response(JSON.stringify({ type: 'message',
  model: options ? JSON.parse(options.body).model : DEEPSEEK, content: [{ type: 'text', text: 'fixture answer' }], stop_reason: 'end_turn' }), {
  headers: { 'content-type': 'application/json' },
});
const sseResponse = (parts) => new Response(new ReadableStream({
  start(controller) {
    for (const part of parts) controller.enqueue(new TextEncoder().encode(part));
    controller.close();
  },
}), { headers: { 'content-type': 'text/event-stream' } });
const frame = (type, extra = {}) => `event: ${type}\ndata: ${JSON.stringify({ type,
  ...(type === 'message_start' ? { message: { model: DEEPSEEK } } : {}),
  ...(type === 'message_delta' ? { delta: { stop_reason: 'end_turn' } } : {}), ...extra })}\n\n`;

async function fixture(t, upstream = jsonResponse, config = {}) {
  const calls = [];
  const server = createProxy({ key: KEY, nonce: NONCE, openrouterModel: DEEPSEEK, retryDelayMs: 0, attemptTimeoutMs: 2000, ...config, fetchImpl: async (url, options) => {
    calls.push({ url, ...options, parsedBody: JSON.parse(options.body) });
    return upstream(url, options);
  } });
  await listenSafe(server);
  t.after(async () => {
    const closed = new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    server.closeAllConnections();
    await closed;
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const request = (body = requestBody(), { path = '/v1/messages', headers = {}, method = 'POST', raw, signal } = {}) =>
    fetch(base + path, { method, signal, headers: { 'content-type': 'application/json',
      'x-local-route-token': NONCE, ...headers },
    ...(method === 'HEAD' || method === 'GET' ? {} : { body: raw ?? JSON.stringify(body) }) });
  return { calls, request };
}

async function consume(request) {
  try {
    const response = await request;
    try { return { status: response.status, text: await response.text() }; }
    catch (error) { return { status: response.status, error }; }
  } catch (error) { return { error }; }
}

const proHeaders = { authorization: OAUTH, 'anthropic-beta': BETA };
const SONNET = 'claude-sonnet-4-6';

test('subscription routing logs use the Plan label', async t => {
  const log = t.mock.method(console, 'error', () => {});
  const { request } = await fixture(t);
  const response = await request(requestBody(OPUS), { headers: proHeaders });
  assert.equal(response.status, 200);
  await response.text();
  assert.ok(log.mock.calls.some(call => {
    const entry = JSON.parse(call.arguments[0]);
    return entry.event === 'route_selected' && entry.provider === 'plan' && entry.reason === 'native_feature' && entry.detail === 'plan_model';
  }));
});

const exclusionCases = [
  ['unsupported_beta', {}, { ...proHeaders, 'anthropic-beta': BETA + ',private-beta-marker' }],
  ['unsupported_request_field', { private_field_marker: 'private-value-marker' }],
  ['unsupported_message_shape', { messages: [{ role: 'system', content: 'private-value-marker' }] }],
  ['unsupported_thinking_config', { thinking: { type: 'private-value-marker' } }],
  ['unsupported_output_config', { output_config: { format: 'private-value-marker' } }],
  ['unsupported_tool_choice', { tool_choice: { private_field_marker: true } }],
  ['native_thinking_history', { messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'private-value-marker', signature: 'private-signature-marker' }] }] }],
  ['unsupported_content_block', { messages: [{ role: 'user', content: [{ type: 'private-block-marker' }] }] }],
  ['unsupported_content_field', { messages: [{ role: 'user', content: [{ type: 'text', text: 'private-value-marker', private_field_marker: true }] }] }],
  ['unsupported_tool_caller', { messages: [{ role: 'assistant', content: [{ type: 'tool_use', id: 't1', name: 'Read', input: {}, caller: { type: 'private-caller-marker' } }] }] }],
  ['unsupported_content', { messages: [{ role: 'user', content: null }] }],
  ['unsupported_tool_definition', { tools: [{ type: 'advisor_20260301', name: 'private-tool-marker' }] }],
  ['token_count', {}, proHeaders, '/v1/messages/count_tokens'],
];
for (const [detail, extra, headers = proHeaders, path = '/v1/messages'] of exclusionCases) {
  test(`native exclusion reports ${detail} without logging request data`, async t => {
    const log = t.mock.method(console, 'error', () => {});
    const { calls, request } = await fixture(t);
    const body = requestBody(SONNET, extra);
    const response = await request(body, { headers, path });
    assert.equal(response.status, 200);
    await response.text();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.anthropic.com' + path);
    assert.deepEqual(calls[0].parsedBody, body);
    const entries = log.mock.calls.map(call => JSON.parse(call.arguments[0]));
    assert.deepEqual(entries, [{ event: 'route_selected', provider: 'plan', reason: 'native_feature', detail,
      ...(detail === 'unsupported_beta' ? { beta_features: ['[redacted]'], unsupported_beta_count: 1 } : {}) }]);
    assert.equal(JSON.stringify(entries).includes('private-'), false);
    assert.equal(JSON.stringify(entries).includes(OAUTH), false);
  });
}

test('beta diagnostics show bounded feature identifiers and redact nonstandard or credential-like values', async t => {
  const log = t.mock.method(console, 'error', () => {});
  const features = ['test-feature-2026-10-21', 'private-value-marker', 'sk-secret-2026-10-21',
    ...Array.from({ length: 20 }, (_, i) => `test-feature-${i}-2026-10-21`)];
  const beta = [BETA, 'claude-code-20250219', ...features].join(',');
  const { calls, request } = await fixture(t);
  const response = await request(requestBody(SONNET), { headers: { ...proHeaders, 'anthropic-beta': beta } });
  assert.equal(response.status, 200);
  await response.text();
  const entry = JSON.parse(log.mock.calls[0].arguments[0]);
  assert.equal(entry.detail, 'unsupported_beta');
  assert.equal(entry.beta_features.length, 16);
  assert.equal(entry.unsupported_beta_count, 23);
  assert.deepEqual(entry.beta_features.slice(0, 3), ['test-feature-2026-10-21', '[redacted]', '[redacted]']);
  assert.equal(JSON.stringify(entry).includes('private-value-marker'), false);
  assert.equal(JSON.stringify(entry).includes('sk-secret'), false);
  assert.equal(JSON.stringify(entry).includes(OAUTH), false);
  assert.equal(calls[0].headers['anthropic-beta'], beta);
});

for (const provider of ['openrouter', 'local']) {
  test(`${provider} preserves direct caller through tool execution and the next turn`, async t => {
    const tool = { type: 'tool_use', id: 'history-1', name: 'ReadHistory', input: {}, caller: { type: 'direct' } };
    const config = provider === 'local' ? { provider, localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-model' } : {};
    const { calls, request } = await fixture(t, (_url, options) => {
      const body = JSON.parse(options.body);
      return Response.json({ type: 'message', model: body.model,
        content: body.messages.length === 1 ? [tool] : [{ type: 'text', text: 'history preserved' }],
        stop_reason: body.messages.length === 1 ? 'tool_use' : 'end_turn' });
    }, config);
    const body = requestBody(SONNET);
    const first = await (await request(body, { headers: proHeaders })).json();
    assert.deepEqual(first.content, [tool]);
    const followup = { ...body, messages: [...body.messages, { role: 'assistant', content: first.content },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: tool.id, content: 'synthetic commit 1234567' }] }] };
    const snapshot = structuredClone(followup);
    const second = await (await request(followup, { headers: proHeaders })).json();
    assert.equal(second.content[0].text, 'history preserved');
    assert.equal(calls.length, 2);
    assert.ok(calls.every(call => call.url === (provider === 'local' ? config.localUrl : 'https://openrouter.ai/api/v1/messages')));
    assert.deepEqual(calls[1].parsedBody.messages, snapshot.messages);
    assert.deepEqual(followup, snapshot);
  });
}

for (const caller of [null, {}, [], 1, 'direct', { type: 'future' }, { type: 'code_execution_20250825', tool_id: 'x' }, { type: 'direct', extra: true }]) {
  test(`unsupported caller ${JSON.stringify(caller)} preserves native request`, async t => {
    const body = requestBody(SONNET, { messages: [{ role: 'assistant', content: [
      { type: 'tool_use', id: 'history-1', name: 'ReadHistory', input: {}, caller },
    ] }] });
    const { calls, request } = await fixture(t);
    assert.equal((await request(body, { headers: proHeaders })).status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.anthropic.com/v1/messages');
    assert.deepEqual(calls[0].parsedBody, body);
  });
}

for (const nativeFeature of ['beta', 'system role', 'nested caller']) {
  test(`direct caller does not bypass ${nativeFeature} preservation`, async t => {
    const tool = { type: 'tool_use', id: 't1', name: 'ReadHistory', input: {}, caller: { type: 'direct' } };
    const body = requestBody(SONNET, { messages: [{ role: 'assistant', content: [tool] }] });
    const headers = { ...proHeaders };
    if (nativeFeature === 'beta') headers['anthropic-beta'] += ',future-native-2026-10-20';
    if (nativeFeature === 'system role') body.messages.unshift({ role: 'system', content: 'synthetic instruction' });
    if (nativeFeature === 'nested caller') body.messages.push({ role: 'user', content: [
      { type: 'tool_result', tool_use_id: 't1', content: [{ ...tool, caller: { type: 'future' } }] },
    ] });
    const { calls, request } = await fixture(t);
    assert.equal((await request(body, { headers })).status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.anthropic.com/v1/messages');
    assert.deepEqual(calls[0].parsedBody, body);
    assert.equal(calls[0].headers['anthropic-beta'], headers['anthropic-beta']);
  });
}

test('native 429 does not stop external routing or subsequent native recovery', async t => {
  let limited = true;
  const { calls, request } = await fixture(t, (url, options) => {
    if (url.startsWith('https://api.anthropic.com') && limited) {
      limited = false;
      return Response.json({ type: 'error', error: { type: 'rate_limit_error', message: 'synthetic limit' } },
        { status: 429, headers: { 'retry-after': '7', 'anthropic-ratelimit-unified-5h-status': 'allowed' } });
    }
    return jsonResponse(url, options);
  });
  const original = requestBody(OPUS);
  const limitedResponse = await request(original, { headers: proHeaders });
  assert.equal(limitedResponse.status, 429);
  assert.equal(limitedResponse.headers.get('retry-after'), '7');
  assert.equal(limitedResponse.headers.get('anthropic-ratelimit-unified-5h-status'), 'allowed');
  assert.equal((await limitedResponse.json()).error.type, 'rate_limit_error');
  assert.deepEqual(calls[0].parsedBody, original);
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 200);
  assert.equal(calls[1].url, 'https://openrouter.ai/api/v1/messages');
  assert.equal((await request(original, { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 3);
});
const ARTIFACT_ID_PATTERN = String.raw`^(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}$`;
const ARTIFACT_COLLECTION_PATTERN = String.raw`^(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}(?:\/(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}){0,14}$`;

function regexCorpus() {
  const values = new Set(['', '.', '..', '...', '.a', '..a', 'a.', 'a..', '.a/..a', 'a/./b', 'a/../b',
    'A_z-0.~:@+', 'a b', '\n', 'a\nb']);
  let frontier = [''];
  for (let length = 1; length <= 5; length++) {
    frontier = frontier.flatMap(prefix => ['.', 'a', '/', '!'].map(char => prefix + char));
    for (const value of frontier) values.add(value);
  }
  for (const length of [199, 200, 201]) {
    for (const char of ['a', '.']) values.add(char.repeat(length));
    values.add('.' + 'a'.repeat(length - 1));
    values.add('a/' + 'a'.repeat(length));
  }
  for (const count of [1, 14, 15, 16]) {
    values.add(Array(count).fill('a').join('/'));
    values.add(Array(count).fill('..a').join('/'));
    for (const bad of ['.', '..', '']) {
      const parts = Array(count).fill('a');
      parts[Math.floor(count / 2)] = bad;
      values.add(parts.join('/'));
    }
  }
  return values;
}

function assertEquivalentPattern(original, converted) {
  assert.equal(typeof converted, 'string');
  assert.doesNotMatch(converted, /\(\?[=!]/, 'outbound ArtifactData patterns must not use lookahead');
  const before = new RegExp(original);
  const after = new RegExp(converted);
  for (const value of regexCorpus()) {
    assert.equal(after.test(value), before.test(value), `regex acceptance changed for ${JSON.stringify(value)}`);
  }
}

test('DeepSeek ArtifactData regex conversion preserves ID and collection acceptance at boundaries', async t => {
  const schema = { type: 'object', properties: { id: { type: 'string', pattern: ARTIFACT_ID_PATTERN },
    collection: { type: 'string', pattern: ARTIFACT_COLLECTION_PATTERN } }, additionalProperties: false };
  const body = requestBody(SONNET, { tools: [{ name: 'ArtifactData', input_schema: schema }] });
  const original = structuredClone(body);
  const { calls, request } = await fixture(t);
  const response = await request(body);
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(calls.length, 1);
  const converted = calls[0].parsedBody.tools[0].input_schema;
  assertEquivalentPattern(ARTIFACT_ID_PATTERN, converted.properties.id.pattern);
  assertEquivalentPattern(ARTIFACT_COLLECTION_PATTERN, converted.properties.collection.pattern);
  assert.equal(converted.additionalProperties, false);
  assert.deepEqual(body, original, 'the original fixture must remain unchanged');
});

test('DeepSeek schema compatibility traverses schema nodes only and retains all tools and data values', async t => {
  const patternNode = () => ({ type: 'string', pattern: ARTIFACT_ID_PATTERN });
  const data = { pattern: ARTIFACT_ID_PATTERN, propertyNames: { type: 'string' },
    properties: { nested: { pattern: ARTIFACT_COLLECTION_PATTERN } } };
  const schema = { type: 'object', propertyNames: { type: 'string' },
    properties: { id: patternNode(), payload: { default: structuredClone(data), examples: [structuredClone(data)],
      enum: [structuredClone(data)], const: structuredClone(data), description: ARTIFACT_ID_PATTERN },
      named: { type: 'object', propertyNames: { type: 'string', pattern: '^[a-z]+$', minLength: 1 },
        additionalProperties: false },
      names: { type: 'object', propertyNames: { pattern: ARTIFACT_ID_PATTERN } } },
    $defs: { id: patternNode() }, definitions: { id: patternNode() },
    patternProperties: { '^a': patternNode() }, items: patternNode(), prefixItems: [patternNode()],
    additionalProperties: { type: 'object', propertyNames: { type: 'string' }, properties: { id: patternNode() } },
    contains: patternNode(), if: patternNode(), then: patternNode(), else: patternNode(), not: patternNode(),
    oneOf: [patternNode()], anyOf: [patternNode()], allOf: [patternNode()],
    dependentSchemas: { id: patternNode() } };
  const tools = [{ name: 'ArtifactData', description: 'keep this tool', input_schema: schema },
    { name: 'OtherTool', input_schema: { type: 'object', additionalProperties: true } },
    { name: 'ThirdTool', input_schema: { type: 'object', additionalProperties: false } }];
  const original = structuredClone(tools);
  const { calls, request } = await fixture(t);
  const response = await request(requestBody(SONNET, { tools }));
  assert.equal(response.status, 200);
  await response.text();
  const outbound = calls[0].parsedBody.tools;
  assert.deepEqual(outbound.map(tool => tool.name), tools.map(tool => tool.name));
  assert.equal(outbound.length, 3);
  assert.deepEqual(outbound.slice(1), tools.slice(1));
  assert.equal(outbound[0].description, tools[0].description);
  const converted = outbound[0].input_schema;
  assert.deepEqual(converted.properties.payload, schema.properties.payload, 'instance data must not be treated as schemas');
  assert.deepEqual(converted.properties.named, schema.properties.named, 'nontrivial key restrictions must survive');
  for (const path of [['properties', 'id'], ['$defs', 'id'], ['definitions', 'id'],
    ['patternProperties', '^a'], ['items'], ['prefixItems', 0], ['additionalProperties', 'properties', 'id'],
    ['contains'], ['if'], ['then'], ['else'], ['not'], ['oneOf', 0], ['anyOf', 0], ['allOf', 0],
    ['dependentSchemas', 'id'], ['properties', 'names', 'propertyNames']]) {
    const node = path.reduce((value, key) => value[key], converted);
    assertEquivalentPattern(ARTIFACT_ID_PATTERN, node.pattern);
  }
  assert.deepEqual(tools, original);
});

test('Pro forwards ArtifactData patterns and propertyNames without compatibility rewriting', async t => {
  const schema = { type: 'object', propertyNames: { type: 'string' }, additionalProperties: false,
    properties: { id: { pattern: ARTIFACT_ID_PATTERN }, collection: { pattern: ARTIFACT_COLLECTION_PATTERN } } };
  const body = requestBody(OPUS, { tools: [{ name: 'ArtifactData', input_schema: schema }] });
  const original = structuredClone(body);
  const { calls, request } = await fixture(t);
  const response = await request(body, { headers: proHeaders });
  assert.equal(response.status, 200);
  await response.text();
  assert.deepEqual(calls[0].parsedBody, original);
  assert.deepEqual(body, original);
});


const proURL = 'https://api.anthropic.com/v1/messages';
const externalURL = 'https://openrouter.ai/api/v1/messages';
const contentStream = (content, stopReason = 'end_turn') => frame('message_start') +
  content.map((block, index) => frame('content_block_start', { index, content_block: block }) +
    frame('content_block_stop', { index })).join('') +
  frame('message_delta', { delta: { stop_reason: stopReason } }) + frame('message_stop');
const complete = model => frame('message_start', { message: { model } }) +
  frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
  frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'fixture answer' } }) +
  frame('content_block_stop', { index: 0 }) + frame('message_delta') + frame('message_stop');
const native = (url, options) => url.startsWith('https://api.anthropic.com') ? jsonResponse(url, options) : null;

for (const openrouterModel of ['vendor-a/model-a', 'vendor-b/model-b:free', '~vendor-c/latest']) {
  test(`explicit OpenRouter model routes and validates independently of DeepSeek: ${openrouterModel}`, async t => {
    const { calls, request } = await fixture(t, jsonResponse, { openrouterModel });
    for (const clientModel of [SONNET, openrouterModel, DEEPSEEK]) {
      const response = await request(requestBody(clientModel));
      assert.equal(response.status, 200);
      assert.equal((await response.json()).model, clientModel);
      assert.equal(calls.at(-1).parsedBody.model, openrouterModel);
      assert.deepEqual(calls.at(-1).parsedBody.provider, { allow_fallbacks: false });
    }
    assert.equal(calls.length, 3);
  });
}
for (const openrouterModel of [undefined, '', ' ', 'YOUR_MODEL', 'REPLACE_WITH_OPENROUTER_MODEL_ID', 'vendor/model\n', 'vendor/model name']) {
  test('OpenRouter model must be explicitly configured with no implicit default', () => {
    assert.throws(() => createProxy({ key: KEY, nonce: NONCE, openrouterModel }), /OpenRouter model required/);
  });
}
test('configured OpenRouter model failures rewrite direct model aliases to Plan Sonnet', async t => {
  const model = 'vendor-a/model-a';
  const { calls, request } = await fixture(t, (url, options) => native(url, options) || new Response('fixture-failure', { status: 500 }), { openrouterModel: model });
  assert.equal((await request(requestBody(model), { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 5);
  assert.equal(calls.at(-1).url, proURL);
  assert.equal(calls.at(-1).parsedBody.model, 'claude-sonnet-5-5');
});
test('response model mismatch uses configured identity rather than accepting the former fixed model', async t => {
  const model = 'vendor-a/model-a';
  const { calls, request } = await fixture(t, (url, options) => native(url, options) || Response.json({
    type: 'message', model: DEEPSEEK, content: [{ type: 'text', text: 'fixture' }], stop_reason: 'end_turn' }), { openrouterModel: model });
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 5);
  assert.ok(calls.slice(0, 4).every(call => call.parsedBody.model === model));
  assert.equal(calls.at(-1).parsedBody.model, SONNET);
});

for (const [label, content] of [
  ['no blocks', []],
  ['empty text', [{ type: 'text', text: '' }]],
  ['whitespace text', [{ type: 'text', text: ' \t\n' }]],
  ['thinking only', [{ type: 'thinking', thinking: 'synthetic reasoning' }]],
  ['redacted thinking only', [{ type: 'redacted_thinking', data: 'synthetic' }]],
  ['whitespace tool ID', [{ type: 'tool_use', id: ' ', name: 'Read', input: {} }]],
  ['whitespace tool name', [{ type: 'tool_use', id: 'fixture-tool', name: ' ', input: {} }]],
]) {
  for (const format of ['json', 'sse']) {
    test(`external ${format} ${label} retries three times then forwards Pro unchanged`, async t => {
      const { calls, request } = await fixture(t, url => url === proURL
        ? new Response('native result', { status: 429, headers: { 'retry-after': '60' } })
        : format === 'json'
          ? new Response(JSON.stringify({ type: 'message', model: DEEPSEEK, content, stop_reason: 'end_turn' }), { headers: { 'content-type': 'application/json' } })
          : sseResponse([contentStream(content)]));
      const original = requestBody(SONNET, { stream: format === 'sse' });
      const response = await request(original, { headers: proHeaders });
      assert.equal(response.status, 429);
      assert.equal(response.headers.get('retry-after'), '60');
      assert.equal(await response.text(), 'native result');
      assert.equal(calls.length, 5);
      assert.ok(calls.slice(0, 4).every(call => call.url === externalURL));
      assert.equal(calls.at(-1).url, proURL);
      assert.deepEqual(calls.at(-1).parsedBody, original);
      assert.equal(calls.at(-1).headers.authorization, OAUTH);
    });
  }
}

for (const text of ['', ' \t\n']) {
  test(`external SSE empty text delta ${JSON.stringify(text)} triggers fallback`, async t => {
    const events = frame('message_start') +
      frame('content_block_start', { index: 0, content_block: { type: 'text', text: '' } }) +
      frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text } }) +
      frame('content_block_stop', { index: 0 }) + frame('message_delta') + frame('message_stop');
    const { calls, request } = await fixture(t, url => url === proURL
      ? new Response('native delta fallback') : sseResponse([events]));
    const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
    assert.equal(await response.text(), 'native delta fallback');
    assert.equal(calls.length, 5);
    assert.equal(calls.at(-1).url, proURL);
  });
}

for (const stopReason of ['refusal', 'model_context_window_exceeded']) {
  for (const format of ['json', 'sse']) {
    test(`external ${format} explicit ${stopReason} is preserved without retry or fallback`, async t => {
      const { calls, request } = await fixture(t, () => format === 'json'
        ? new Response(JSON.stringify({ type: 'message', model: DEEPSEEK, content: [], stop_reason: stopReason }), { headers: { 'content-type': 'application/json' } })
        : sseResponse([contentStream([], stopReason)]));
      const response = await request(requestBody(SONNET, { stream: format === 'sse' }), { headers: proHeaders });
      assert.equal(response.status, 200);
      assert.match(await response.text(), new RegExp(stopReason));
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, externalURL);
    });
  }
}

for (const format of ['json', 'sse']) {
  test(`empty OpenRouter ${format} opens cooldown and a real output probe restores replacement`, async t => {
    let time = 1000, healthy = false;
    const { calls, request } = await fixture(t, (url, options) => {
      if (url === proURL) return jsonResponse(url, options);
      if (healthy) return format === 'sse' ? sseResponse([complete(DEEPSEEK)]) : jsonResponse(url, options);
      return format === 'json'
        ? new Response(JSON.stringify({ type: 'message', model: DEEPSEEK, content: [], stop_reason: 'end_turn' }), { headers: { 'content-type': 'application/json' } })
        : sseResponse([contentStream([])]);
    }, { now: () => time, cooldownMs: 30 });
    const original = requestBody(SONNET, { stream: format === 'sse' });
    await (await request(original, { headers: proHeaders })).text();
    assert.equal(calls.length, 5);
    healthy = true;
    await (await request(original, { headers: proHeaders })).text();
    assert.equal(calls.length, 6);
    assert.equal(calls.at(-1).url, proURL);
    time += 31;
    for (let i = 0; i < 2; i++) {
      const response = await request(original, { headers: proHeaders });
      assert.match(await response.text(), /fixture answer/);
      assert.equal(calls.at(-1).url, externalURL);
    }
    assert.equal(calls.length, 8);
  });
}

for (const model of [OPUS, HAIKU, FABLE]) {
  test(`native ${model} empty completion remains untouched`, async t => {
    const raw = JSON.stringify({ type: 'message', model, content: [], stop_reason: 'end_turn' });
    const { calls, request } = await fixture(t, () => new Response(raw, { headers: { 'content-type': 'application/json' } }));
    const response = await request(requestBody(model), { headers: proHeaders });
    assert.equal(await response.text(), raw);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, proURL);
  });
}

for (const model of [DEEPSEEK, SONNET]) {
  test(`${model} external JSON drops client metadata and isolates OAuth`, async t => {
    const body = requestBody(model, { metadata: { user_id: 'fixture' }, tools: [{ name: 'Read', input_schema: { type: 'object' } }] });
    const { calls, request } = await fixture(t);
    const response = await request(body, { headers: proHeaders });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).model, model);
    const { metadata, ...expected } = body;
    assert.equal(calls[0].parsedBody.metadata, undefined);
    assert.deepEqual(calls[0].parsedBody, { ...expected, thinking: { type: 'disabled' }, model: DEEPSEEK, provider: { allow_fallbacks: false } });
    assert.equal(calls[0].headers.authorization, `Bearer ${KEY}`);
    assert.equal(JSON.stringify(calls[0].headers).includes(OAUTH), false);
    assert.equal(calls[0].headers['anthropic-beta'], undefined);
  });
  test(`${model} validated SSE retains content and restores model`, async t => {
    const data = complete(DEEPSEEK);
    const { calls, request } = await fixture(t, () => sseResponse([data.slice(0, 31), data.slice(31)]));
    const response = await request(requestBody(model, { stream: true }), { headers: proHeaders });
    assert.equal(response.status, 200);
    const text = await response.text();
    assert.match(text, new RegExp(model.replaceAll('.', '\\.')));
    assert.match(text, /message_stop/);
    assert.equal(calls.length, 1);
    if (model === DEEPSEEK) assert.equal(text, data);
  });
}

for (const model of [OPUS, HAIKU, FABLE, 'claude-opus-4-7', 'claude-haiku-4-5-20251001']) {
  test(`${model} original Pro body bytes and headers are transparent`, async t => {
    const raw = JSON.stringify(requestBody(model, { future_native_field: { enabled: true } }), null, 2);
    const { calls, request } = await fixture(t, () => new Response('native response', { status: 201, headers: { 'x-native-feature': 'preserved' } }));
    const response = await request(undefined, { raw, headers: { ...proHeaders, 'x-native-feature': 'request' } });
    assert.equal(response.status, 201);
    assert.equal(await response.text(), 'native response');
    assert.equal(response.headers.get('x-native-feature'), 'preserved');
    assert.equal(calls[0].url, proURL);
    assert.equal(Buffer.from(calls[0].body).toString(), raw);
    assert.equal(calls[0].headers.authorization, OAUTH);
    assert.equal(calls[0].headers['anthropic-beta'], BETA);
    assert.equal(calls[0].headers['x-native-feature'], 'request');
    for (const key of ['host', 'content-length', 'connection', 'transfer-encoding', 'x-local-route-token', 'x-api-key']) assert.equal(calls[0].headers[key], undefined);
  });
}

const nativeFeatures = [
  ['advisor model', { tools: [{ type: 'advisor_20260301', name: 'advisor', model: OPUS }] }],
  ['advisor advisor_model', { tools: [{ type: 'advisor_20260301', name: 'advisor', advisor_model: FABLE }] }],
  ['multiple advisor fields', { tools: [{ type: 'advisor_20260301', name: 'advisor', model: OPUS, advisor_model: HAIKU }] }],
  ['web search forced', { tools: [{ type: 'web_search_20250305', name: 'web_search' }], tool_choice: { type: 'tool', name: 'web_search' } }],
  ['web search auto', { tools: [{ type: 'web_search_20250305', name: 'web_search' }], tool_choice: { type: 'auto' } }],
  ['mixed server client tools', { tools: [{ type: 'web_search_20260301', name: 'web_search' }, { name: 'Read', input_schema: { type: 'object' } }] }],
  ['deferred tool', { tools: [{ name: 'Read', input_schema: { type: 'object' }, defer_loading: true }] }],
  ['future tool field', { tools: [{ name: 'Read', input_schema: { type: 'object' }, future_tool: true }] }],
  ['context management', { context_management: { edits: [] } }],
  ['unknown top field', { future_native: true }],
  ['provider override', { provider: { allow_fallbacks: true } }],
  ['endpoint override', { base_url: 'https://example.invalid' }],
  ['signed thinking history', { messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'native', signature: 'fixture' }] }] }],
  ['server tool history', { messages: [{ role: 'assistant', content: [{ type: 'server_tool_use', id: 'tool', name: 'web_search', input: {} }] }] }],
  ['nested server result', { messages: [{ role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool', content: [{ type: 'web_search_tool_result', content: [] }] }] }] }],
  ['inline tool addition', { messages: [{ role: 'user', content: [{ type: 'tool_addition', tools: [] }] }] }],
  ['inline tool reference', { messages: [{ role: 'user', content: [{ type: 'tool_reference', tool_name: 'Read' }] }] }],
  ['inline system reminder', { messages: [{ role: 'user', content: [{ type: 'system_reminder', text: 'native' }] }] }],
  ['block future field', { messages: [{ role: 'user', content: [{ type: 'text', text: 'x', native_flag: true }] }] }],
];
for (const [label, extra] of nativeFeatures) {
  test(`Sonnet ${label} bypasses external without mutating original`, async t => {
    const body = requestBody(SONNET, extra);
    const { calls, request } = await fixture(t);
    const response = await request(body, { headers: proHeaders });
    assert.equal(response.status, 200);
    await response.text();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, proURL);
    assert.deepEqual(calls[0].parsedBody, body);
  });
}

for (const [label, body, path = '/v1/messages', headers = proHeaders] of [
  ['Plan model', requestBody(OPUS)],
  ['token count', requestBody(SONNET), '/v1/messages/count_tokens'],
  ['native beta', requestBody(SONNET), '/v1/messages', { ...proHeaders, 'anthropic-beta': BETA + ',future-native-2026-10-20' }],
  ['native tool', requestBody(SONNET, { tools: [{ type: 'advisor_20260301', name: 'advisor' }] })],
  ['native history', requestBody(SONNET, { messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'fixture' }] }] })],
]) {
  test(`Local-only mode rejects ${label} even with valid OAuth and never calls Plan`, async t => {
    const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly: true,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    const response = await request(body, { path, headers });
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { type: 'error', error: { type: 'api_error',
      message: 'Local-only mode cannot route this request; Plan fallback disabled' } });
    assert.equal(calls.length, 0);
  });
}

test('Local-only mode keeps standard file tool calls and direct caller history on Local', async t => {
  const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly: true,
    localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
  const response = await request(requestBody(SONNET, { tools: [{ name: 'Read', input_schema: { type: 'object' } }] }), { headers: proHeaders });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:11434/v1/messages');
  assert.equal(calls[0].headers.authorization, undefined);
});

for (const [localOnly, override, expected] of [[false, undefined, 30000], [true, undefined, 120000], [true, 500, 500]]) {
  test(`Local compaction timeout preserves ordinary defaults and overrides (${localOnly}, ${override})`, async t => {
    const timer = t.mock.method(globalThis, 'setTimeout');
    const { request } = await fixture(t, jsonResponse, { provider: 'local', localOnly, attemptTimeoutMs: override,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    const response = await request(requestBody(SONNET), { headers: proHeaders });
    assert.equal(response.status, 200);
    await response.text();
    assert.ok(timer.mock.calls.some(call => call.arguments[1] === expected));
  });
}

for (const failure of ['HTTP error', 'timeout']) {
  test(`Local-only ${failure} fails once and circuit-open never invokes Plan`, async t => {
    const upstream = failure === 'timeout' ? () => new Promise(() => {}) : () => new Response('failure', { status: 500 });
    const { calls, request } = await fixture(t, upstream, { provider: 'local', localOnly: true, attemptTimeoutMs: 10,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 400);
    assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 400);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'http://127.0.0.1:11434/v1/messages');
  });
}

for (const [label, upstream, code, description] of [
  ['HTTP 401', () => new Response('private-response-marker', { status: 401 }), 'local_authentication_failed', /API key and access permissions/],
  ['HTTP 403', () => new Response('private-response-marker', { status: 403 }), 'local_authentication_failed', /API key and access permissions/],
  ['empty HTTP body', () => new Response(''), 'local_empty_response', /empty response/],
  ['blank HTTP body', () => new Response(' \r\n '), 'local_empty_response', /empty response/],
  ['HTTP 204', () => new Response(null, { status: 204 }), 'local_empty_response', /empty response/],
  ['empty content', () => Response.json({ type: 'message', model: 'private-model-fixture', content: [], stop_reason: 'end_turn' }), 'local_empty_response', /empty response/],
  ['blank text', () => Response.json({ type: 'message', model: 'private-model-fixture', content: [{ type: 'text', text: '  ' }], stop_reason: 'end_turn' }), 'local_empty_response', /empty response/],
  ['HTTP 500', () => new Response('private-response-marker', { status: 500 }), 'local_upstream_error', /request failed/],
  ['connection failure', () => { throw new Error('private-response-marker'); }, 'local_upstream_error', /request failed/],
  ['invalid JSON', () => new Response('private-response-marker'), 'local_upstream_error', /request failed/],
  ['fetch timeout', () => new Promise(() => {}), 'local_timeout', /timed out/],
  ['body timeout', () => new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{')); } })), 'local_timeout', /timed out/],
]) {
  test(`Local-only ${label} reports a safe cause and rounded cooldown without changing retry policy`, async t => {
    const log = t.mock.method(console, 'error', () => {});
    let time = 1000;
    const { calls, request } = await fixture(t, upstream, { provider: 'local', localOnly: true,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'private-model-fixture', localKey: KEY,
      now: () => time, cooldownMs: 30000, attemptTimeoutMs: 10 });
    const failed = await request(requestBody(SONNET), { headers: proHeaders });
    assert.equal(failed.status, 400);
    const error = await failed.json();
    assert.deepEqual(Object.keys(error).sort(), ['error', 'type']);
    assert.deepEqual(Object.keys(error.error).sort(), ['message', 'type']);
    assert.equal(error.type, 'error');
    assert.equal(error.error.type, 'api_error');
    assert.match(error.error.message, new RegExp(`^\\[${code}\\]`));
    assert.match(error.error.message, description);
    assert.match(error.error.message, /Wait 30 seconds before a new request/);
    assert.match(error.error.message, /Plan fallback disabled/);
    assert.equal(failed.headers.get('retry-after'), null, 'SDK retry headers are unchanged');
    time += 12345;
    const waiting = await request(requestBody(SONNET), { headers: proHeaders });
    assert.equal(waiting.status, 400);
    const waitingMessage = (await waiting.json()).error.message;
    assert.match(waitingMessage, /^\[local_cooldown\].*Wait 18 seconds/);
    assert.match(waitingMessage, description, 'CLI follow-up requests must not hide the original failure cause');
    time += 17654;
    const almostReady = await request(requestBody(SONNET), { headers: proHeaders });
    assert.equal(almostReady.status, 400);
    assert.match((await almostReady.json()).error.message, /Wait 1 second before a new request/);
    assert.equal(calls.length, 1, 'failure and cooldown must never retry upstream or call Plan');
    const entries = log.mock.calls.map(call => JSON.parse(call.arguments[0]));
    assert.ok(entries.some(e => e.event === 'local_mode_rejected' && e.code === code && e.retry_after_seconds === 30));
    assert.ok(entries.some(e => e.event === 'local_mode_rejected' && e.code === 'local_cooldown' && e.retry_after_seconds === 18 && e.previous_error_code === code));
    const output = JSON.stringify({ error, entries });
    for (const value of [KEY, NONCE, OAUTH, 'private-response-marker', 'private-model-fixture', '127.0.0.1'])
      assert.equal(output.includes(value), false, 'response and diagnostics must not expose configured or upstream data');
  });
}

test('Local-only zero cooldown gives a new-request hint without inventing a wait', async t => {
  const { calls, request } = await fixture(t, () => new Response('failure', { status: 503 }), {
    provider: 'local', localOnly: true, localUrl: 'http://127.0.0.1:11434/v1/messages',
    localModel: 'fixture-local', now: () => 1000, cooldownMs: 0 });
  const failed = await request(requestBody(SONNET), { headers: proHeaders });
  assert.equal(failed.status, 400);
  const message = (await failed.json()).error.message;
  assert.match(message, /Send a new request to check Local recovery/);
  assert.doesNotMatch(message, /Wait 0/);
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 400);
  assert.equal(calls.length, 2);
});

test('Local-only cooldown tracks the latest failure after a failed probe and later recovery', async t => {
  let time = 1000, status = 500;
  const { calls, request } = await fixture(t, (url, options) => status === 200 ? jsonResponse(url, options) : new Response('private-error-marker', { status }), {
    provider: 'local', localOnly: true, localUrl: 'http://127.0.0.1:11434/v1/messages',
    localModel: 'fixture-local', now: () => time, cooldownMs: 10 });
  const send = () => request(requestBody(SONNET), { headers: proHeaders });
  assert.match((await (await send()).json()).error.message, /^\[local_upstream_error\]/);
  time += 11; status = 401;
  assert.match((await (await send()).json()).error.message, /^\[local_authentication_failed\]/);
  assert.match((await (await send()).json()).error.message, /^\[local_cooldown\].*authentication failed/);
  time += 11; status = 200;
  assert.equal((await send()).status, 200);
  status = 500;
  assert.match((await (await send()).json()).error.message, /^\[local_upstream_error\]/);
  const waiting = (await (await send()).json()).error.message;
  assert.match(waiting, /^\[local_cooldown\].*request failed/);
  assert.doesNotMatch(waiting, /authentication failed/);
  assert.equal(calls.length, 4);
});

for (const localOnly of [false, true]) {
  test(`thinking/effort beta compatibility is limited to Local-only mode (${localOnly})`, async t => {
    const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    const body = requestBody(SONNET, { thinking: { type: 'adaptive' }, output_config: { effort: 'medium' } });
    const beta = BETA + ',interleaved-thinking-2025-05-14,effort-2025-11-24';
    assert.equal((await request(body, { headers: { ...proHeaders, 'anthropic-beta': beta } })).status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, localOnly ? 'http://127.0.0.1:11434/v1/messages' : proURL);
    assert.deepEqual(calls[0].parsedBody.thinking, localOnly ? { type: 'disabled' } : body.thinking);
    assert.deepEqual(calls[0].parsedBody.output_config, body.output_config);
    if (localOnly) assert.equal(calls[0].headers['anthropic-beta'], undefined);
  });
}

test('Local-only mode still rejects unknown beta mixed with compatible thinking/effort', async t => {
  const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly: true,
    localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
  const beta = BETA + ',interleaved-thinking-2025-05-14,effort-2025-11-24,future-feature-2026-10-21';
  assert.equal((await request(requestBody(SONNET), { headers: { ...proHeaders, 'anthropic-beta': beta } })).status, 400);
  assert.equal(calls.length, 0);
});

test('Local-only mode validates JSON and converts ordered text/tool blocks to CLI SSE without changing usage', async t => {
  const message = { type: 'message', id: 'msg-local', role: 'assistant', model: 'fixture-local',
    content: [{ type: 'text', text: 'fixture text' }, { type: 'tool_use', id: 'tool-1', name: 'Read',
      input: { file_path: 'fixture.txt' }, caller: { type: 'direct' }, provider_specific_fields: { state: 'opaque-fixture' } }], stop_reason: 'tool_use', stop_sequence: null,
    usage: { input_tokens: 12, output_tokens: 8 } };
  const { calls, request } = await fixture(t, () => Response.json(message), { provider: 'local', localOnly: true,
    localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
  const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
  assert.equal(response.status, 200);
  assert.equal(calls[0].parsedBody.stream, false);
  const events = (await response.text()).split('\n\n').filter(Boolean).map(frame => JSON.parse(frame.split('\n').find(line => line.startsWith('data: ')).slice(6)));
  assert.equal(events[0].message.model, SONNET);
  assert.equal(events[0].message.id, message.id);
  const starts = events.filter(event => event.type === 'content_block_start');
  assert.deepEqual(starts.map(event => event.index), [0, 1]);
  assert.deepEqual(starts[1].content_block.caller, { type: 'direct' });
  assert.deepEqual(starts[1].content_block.provider_specific_fields, message.content[1].provider_specific_fields);
  const deltas = events.filter(event => event.type === 'content_block_delta');
  assert.equal(deltas[0].delta.text, message.content[0].text);
  assert.deepEqual(JSON.parse(deltas[1].delta.partial_json), message.content[1].input);
  const end = events.find(event => event.type === 'message_delta');
  assert.equal(end.delta.stop_reason, message.stop_reason);
  assert.deepEqual(end.usage, message.usage);
  assert.equal(events.at(-1).type, 'message_stop');
});

for (const [label, extra] of [
  ['wrong model', { model: 'wrong-model' }],
  ['missing id', { id: undefined }],
  ['missing usage', { usage: undefined }],
  ['negative usage', { usage: { input_tokens: -1, output_tokens: 2 } }],
  ['thinking history', { content: [{ type: 'thinking', thinking: 'native' }] }],
  ['unrecognized content fields', { content: [{ type: 'text', text: 'x', citations: [] }] }],
  ['invalid provider metadata', { content: [{ type: 'tool_use', id: 't1', name: 'Read', input: {}, provider_specific_fields: [] }] }],
]) {
  test(`Local-only CLI rejects ${label} without silently changing content or routing to Plan`, async t => {
    const message = { type: 'message', id: 'msg-local', role: 'assistant', model: 'fixture-local',
      content: [{ type: 'text', text: 'fixture' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 }, ...extra };
    const { calls, request } = await fixture(t, () => Response.json(message), { provider: 'local', localOnly: true,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    assert.equal((await request(requestBody(SONNET, { stream: true }), { headers: proHeaders })).status, 400);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'http://127.0.0.1:11434/v1/messages');
  });
}

test('Local-only non-streaming requests retain validated JSON', async t => {
  const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly: true,
    localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
  const response = await request(requestBody(SONNET, { stream: false }), { headers: proHeaders });
  assert.equal(response.headers.get('content-type'), 'application/json');
  assert.equal((await response.json()).model, SONNET);
  assert.equal(calls[0].parsedBody.stream, false);
});

for (const localOnly of [false, true]) {
  for (const metadata of [null, { state: { signature: 'opaque-fixture', counter: 1 } }]) {
    test(`provider tool metadata is preserved only in Local mode (${localOnly}, ${metadata === null ? 'null' : 'object'})`, async t => {
      const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly,
        localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
      const tool = { type: 'tool_use', id: 't1', name: 'Read', input: {}, provider_specific_fields: metadata };
      const body = requestBody(SONNET, { messages: [{ role: 'user', content: 'fixture' }, { role: 'assistant', content: [tool] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'fixture result' }] }] });
      assert.equal((await request(body, { headers: proHeaders })).status, 200);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, localOnly ? 'http://127.0.0.1:11434/v1/messages' : proURL);
      assert.deepEqual(calls[0].parsedBody.messages[1].content[0].provider_specific_fields, metadata);
    });
  }
}

for (const block of [
  ...[[], 'invalid', 42, true].map(provider_specific_fields => ({ type: 'tool_use', id: 't1', name: 'Read', input: {}, provider_specific_fields })),
  { type: 'text', text: 'fixture', provider_specific_fields: null },
  { type: 'tool_result', tool_use_id: 't1', content: 'fixture', provider_specific_fields: {} },
]) {
  test(`Local metadata is limited to object/null on tool_use (${block.type}, ${typeof block.provider_specific_fields})`, async t => {
    const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', localOnly: true,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'fixture-local' });
    assert.equal((await request(requestBody(SONNET, { messages: [{ role: 'assistant', content: [block] }] }), { headers: proHeaders })).status, 400);
    assert.equal(calls.length, 0);
  });
}

test('Local-only mode requires a boolean and Local provider', () => {
  assert.throws(() => createProxy({ key: KEY, nonce: NONCE, localOnly: true }));
  assert.throws(() => createProxy({ key: KEY, nonce: NONCE, localOnly: 'true' }));
});

test('unknown native beta remains intact on original Sonnet Pro', async t => {
  const { calls, request } = await fixture(t);
  const beta = BETA + ',future-native-2026-10-20';
  assert.equal((await request(requestBody(SONNET), { headers: { ...proHeaders, 'anthropic-beta': beta } })).status, 200);
  assert.equal(calls[0].url, proURL);
  assert.equal(calls[0].headers['anthropic-beta'], beta);
});

for (const status of [400, 401, 402, 403, 404, 408, 422, 429, 500, 503]) {
  test(`Pro HTTP ${status} preserves status/body/headers and remains request scoped`, async t => {
    let failed = false;
    const { calls, request } = await fixture(t, (url, options) => {
      if (!failed) { failed = true; return new Response('native raw error', { status, headers: { 'retry-after': 'native-value', 'x-trace-id': 'fixture' } }); }
      return jsonResponse(url, options);
    });
    const response = await request(requestBody(FABLE), { headers: proHeaders });
    assert.equal(response.status, status);
    assert.equal(await response.text(), 'native raw error');
    assert.equal(response.headers.get('retry-after'), 'native-value');
    assert.equal(response.headers.get('x-trace-id'), 'fixture');
    assert.equal((await request(requestBody(OPUS), { headers: proHeaders })).status, 200);
    assert.equal((await request(requestBody(DEEPSEEK))).status, 200);
    assert.equal(calls.length, 3);
  });
}

for (const failure of ['http', 'network', 'json model', 'invalid json', 'stream json', 'timeout']) {
  test(`external ${failure} gets initial plus three retries then original Sonnet Pro once`, async t => {
    const { calls, request } = await fixture(t, (url, options) => {
      if (url === proURL) return jsonResponse(url, options);
      if (failure === 'http') return new Response('fixture secret provider text', { status: 429 });
      if (failure === 'network') throw new Error('fixture secret transport');
      if (failure === 'invalid json') return new Response('invalid', { headers: { 'content-type': 'application/json' } });
      if (failure === 'json model') return new Response(JSON.stringify({ type: 'message', model: OPUS }), { headers: { 'content-type': 'application/json' } });
      if (failure === 'timeout') return new Promise(() => {});
      return jsonResponse(url, options);
    }, { attemptTimeoutMs: 10 });
    const body = requestBody(SONNET, { stream: failure === 'stream json' });
    const response = await request(body, { headers: proHeaders });
    assert.equal(response.status, 200);
    await response.text();
    assert.equal(calls.length, 5);
    assert.ok(calls.slice(0, 4).every(call => call.url === externalURL && call.headers.authorization === `Bearer ${KEY}`));
    assert.equal(calls[4].url, proURL);
    assert.deepEqual(calls[4].parsedBody, body);
    assert.equal(calls[4].headers.authorization, OAUTH);
  });
}

for (const [name, events] of [
  ['model mismatch', frame('message_start', { message: { model: OPUS } }) + frame('message_delta') + frame('message_stop')],
  ['missing start', frame('message_delta') + frame('message_stop')],
  ['missing delta', frame('message_start') + frame('message_stop')],
  ['missing stop', frame('message_start') + frame('message_delta')],
  ['error', frame('message_start') + frame('error', { error: { type: 'api_error' } })],
  ['malformed', 'data: {invalid}\n\n'],
  ['premature done', 'data: [DONE]\n\n'],
  ['event after stop', complete(DEEPSEEK) + frame('content_block_delta')],
  ['partial event after stop', complete(DEEPSEEK) + 'data: {'],
  ['duplicate start', frame('message_start') + complete(DEEPSEEK)],
]) {
  test(`SSE ${name} buffers all failed content and falls back to raw Pro`, async t => {
    const { calls, request } = await fixture(t, (url) => url === proURL ? new Response('raw pro fallback', { status: 429 }) : sseResponse([events]));
    const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
    assert.equal(response.status, 429);
    assert.equal(await response.text(), 'raw pro fallback');
    assert.equal(calls.length, 5);
  });
}

for (const trailer of ['', 'data: [DONE]\n\n', 'data:[DONE]', frame('ping')]) {
  test(`valid SSE completion accepts trailer ${JSON.stringify(trailer)}`, async t => {
    const { calls, request } = await fixture(t, () => sseResponse([complete(DEEPSEEK), trailer]));
    const response = await request(requestBody(DEEPSEEK, { stream: true }));
    assert.equal(response.status, 200);
    assert.match(await response.text(), /message_stop/);
    assert.equal(calls.length, 1);
  });
}

for (const headers of [{}, { authorization: OAUTH }, { authorization: 'Bearer api-key', 'anthropic-beta': BETA }, { ...proHeaders, 'x-api-key': 'synthetic-api-key' }]) {
  test(`native OAuth validation denies ${JSON.stringify(Object.keys(headers))}`, async t => {
    const { calls, request } = await fixture(t);
    assert.equal((await request(requestBody(OPUS), { headers })).status, 403);
    assert.equal(calls.length, 0);
  });
}

test('external exhaustion without valid saved OAuth never forwards credentials to Pro', async t => {
  const { calls, request } = await fixture(t, () => new Response('fail', { status: 500 }));
  assert.equal((await request(requestBody(SONNET))).status, 403);
  assert.equal(calls.length, 4);
  assert.ok(calls.every(call => call.url === externalURL));
});

test('explicit DeepSeek failure uses configured native Sonnet fallback ID', async t => {
  const fallbackModel = 'claude-sonnet-4-6';
  const { calls, request } = await fixture(t, (url, options) => native(url, options) || new Response('fail', { status: 500 }), { fallbackModel });
  assert.equal((await request(requestBody(DEEPSEEK), { headers: proHeaders })).status, 200);
  assert.equal(calls.at(-1).parsedBody.model, fallbackModel);
});

test('failed Pro fallback does not loop or touch external again during cooldown', async t => {
  const { calls, request } = await fixture(t, () => new Response('raw failure', { status: 503 }));
  const response = await request(requestBody(SONNET), { headers: proHeaders });
  assert.equal(response.status, 503);
  assert.equal(await response.text(), 'raw failure');
  assert.equal(calls.length, 5);
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 503);
  assert.equal(calls.length, 6);
  assert.equal(calls.at(-1).url, proURL);
});

test('cooldown expires into one real half-open probe and valid completion restores external', async t => {
  let healthy = false, releaseProbe, probeEntered, time = 1000;
  const entered = new Promise(resolve => { probeEntered = resolve; });
  const { calls, request } = await fixture(t, async (url, options) => {
    if (url === proURL) return jsonResponse(url, options);
    if (!healthy) return new Response('fail', { status: 500 });
    probeEntered();
    await new Promise(resolve => { releaseProbe = resolve; });
    return jsonResponse(url, options);
  }, { cooldownMs: 15, attemptTimeoutMs: 2000, now: () => time });
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.length, 5);
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.length, 6);
  time += 30;
  healthy = true;
  const probe = request(requestBody(SONNET), { headers: proHeaders });
  await entered;
  const concurrent = await request(requestBody(SONNET), { headers: proHeaders });
  assert.equal(concurrent.status, 200);
  await concurrent.text();
  assert.equal(calls.at(-1).url, proURL);
  releaseProbe();
  assert.equal((await probe).status, 200);
  healthy = false;
  // Next request really reaches external; a temporary success removes cooldown.
  const next = await request(requestBody(SONNET), { headers: proHeaders });
  await next.text();
  assert.equal(calls.filter(call => call.url === externalURL).length, 9);
});

test('one failed request does not abort another external attempt', async t => {
  let pending, entered;
  const ready = new Promise(resolve => { entered = resolve; });
  const { calls, request } = await fixture(t, (url, options) => {
    if (url === proURL) return jsonResponse(url, options);
    if (JSON.parse(options.body).messages[0].content === 'hold') {
      entered();
      return new Promise(resolve => { pending = () => { assert.equal(options.signal.aborted, false); resolve(jsonResponse(url, options)); }; });
    }
    return new Response('fail', { status: 500 });
  }, { attemptTimeoutMs: 2000 });
  const held = request(requestBody(SONNET, { messages: [{ role: 'user', content: 'hold' }] }), { headers: proHeaders });
  await ready;
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  pending();
  assert.equal((await held).status, 200);
  assert.equal(calls.length, 6);
});

for (const phase of ['fetch', 'body', 'retry delay']) {
  test(`client cancellation in ${phase} stops retries and fallback`, async t => {
    let entered, capturedSignal;
    const ready = new Promise(resolve => { entered = resolve; });
    const { calls, request } = await fixture(t, (_url, options) => {
      capturedSignal = options.signal;
      entered();
      if (phase === 'fetch') return new Promise(() => {});
      if (phase === 'retry delay') return new Response('fail', { status: 503 });
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode(frame('message_start')));
        options.signal.addEventListener('abort', () => controller.error(new Error('cancelled')), { once: true });
      } }), { headers: { 'content-type': 'text/event-stream' } });
    }, { retryDelayMs: 100, attemptTimeoutMs: 1000 });
    const controller = new AbortController();
    const pending = consume(request(requestBody(SONNET, { stream: true }), { headers: proHeaders, signal: controller.signal }));
    await ready;
    controller.abort();
    await pending;
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(calls.length, 1);
    assert.ok(capturedSignal.aborted);
  });
}

test('attempt body timeout aborts reader even when mocked fetch ignores signal', async t => {
  let cancelled = 0;
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) :
    new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(frame('message_start'))); }, cancel() { cancelled++; } }),
      { headers: { 'content-type': 'text/event-stream' } }), { attemptTimeoutMs: 10 });
  assert.equal((await request(requestBody(SONNET, { stream: true }), { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 5);
  assert.equal(cancelled, 4);
});

for (const localKey of [undefined, 'test-local-key']) {
  test(`local Messages needs no OpenRouter key and uses only its configured key ${Boolean(localKey)}`, async t => {
    const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', key: undefined,
      localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'local-fixture', localKey });
    assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 200);
    assert.equal(calls[0].url, 'http://127.0.0.1:11434/v1/messages');
    assert.equal(calls[0].parsedBody.model, 'local-fixture');
    assert.equal(calls[0].parsedBody.provider, undefined);
    assert.equal(calls[0].headers.authorization, localKey ? `Bearer ${localKey}` : undefined);
    assert.equal(JSON.stringify(calls[0].headers).includes(OAUTH), false);
    assert.equal(calls[0].redirect, 'error');
  });
}
for (const localUrl of ['https://example.invalid/v1/messages', 'http://127.0.0.1:11434/v1/messages?q=x',
  'http://user:pass@127.0.0.1/v1/messages', 'http://127.0.0.1/v1/messages#x', 'file:///v1/messages', 'http://127.0.0.1/v1/chat/completions']) {
  test(`local endpoint rejects ${localUrl}`, () => {
    assert.throws(() => createProxy({ nonce: NONCE, provider: 'local', localUrl, localModel: 'fixture' }));
  });
}

test('requires credentials and nonempty local model at creation', () => {
  assert.throws(() => createProxy({ key: KEY }));
  assert.throws(() => createProxy({ nonce: NONCE }));
  assert.throws(() => createProxy({ nonce: NONCE, provider: 'local', localUrl: 'http://127.0.0.1/v1/messages', localModel: ' ' }));
  assert.throws(() => createProxy({ nonce: NONCE, provider: 'invalid' }));
});

for (const model of ['fable', 'opus', 'haiku', 'unknown', null, {}, '']) {
  test(`unresolved model ${JSON.stringify(model)} stays local`, async t => {
    const { calls, request } = await fixture(t);
    assert.equal((await request(requestBody(model))).status, 403);
    assert.equal(calls.length, 0);
  });
}

test('nonce, endpoint, malformed JSON and nonobject checks fail before upstream', async t => {
  const { calls, request } = await fixture(t);
  for (const token of ['', 'wrong']) assert.equal((await request(undefined, { headers: { 'x-local-route-token': token } })).status, 403);
  for (const path of ['/v1/chat/completions', '/v1/messages?beta=false', '/v1/messages/extra', '/api/hello']) assert.equal((await request(undefined, { path })).status, 403);
  assert.equal((await request(undefined, { raw: '{' })).status, 400);
  for (const body of [null, [], 'text', 1]) assert.equal((await request(body)).status, 403);
  assert.equal((await request(undefined, { path: '/api/hello', method: 'HEAD' })).status, 200);
  assert.equal(calls.length, 0);
});

test('Pro SSE streams immediately without model validation or rewrite', { timeout: 2000 }, async t => {
  let finish;
  const { calls, request } = await fixture(t, () => new Response(new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('data: native arbitrary event\n\n'));
    finish = () => controller.close();
  } }), { headers: { 'content-type': 'text/event-stream' } }));
  const response = await request(requestBody(OPUS, { stream: true }), { headers: proHeaders });
  const reader = response.body.getReader();
  assert.equal(new TextDecoder().decode((await reader.read()).value), 'data: native arbitrary event\n\n');
  finish();
  assert.equal((await reader.read()).done, true);
  assert.equal(calls.length, 1);
});

for (const thinking of [{ type: 'disabled' }, { type: 'enabled', budget_tokens: 1024 }, { type: 'adaptive' }]) {
  test(`previously supported ${thinking.type} thinking and normal settings still use external`, async t => {
    const body = requestBody(SONNET, { thinking, output_config: { effort: 'high' }, safeguards: { allow_overrides: false },
      cache_control: { type: 'ephemeral' }, system: [{ type: 'text', text: 'fixture', cache_control: { type: 'ephemeral' } }] });
    const { calls, request } = await fixture(t);
    assert.equal((await request(body, { headers: { ...proHeaders, 'anthropic-beta': BETA + ',claude-code-20250219,prompt-caching-2024-07-31' } })).status, 200);
    assert.equal(calls[0].url, externalURL);
    assert.deepEqual(calls[0].parsedBody, { ...body, thinking: { type: 'disabled' }, model: DEEPSEEK, provider: { allow_fallbacks: false } });
    assert.equal(calls[0].headers['anthropic-beta'], undefined);
  });
}

test('complete external SSE finishes at message_stop without upstream EOF', { timeout: 2000 }, async t => {
  let cancelled = false;
  const { calls, request } = await fixture(t, () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(complete(DEEPSEEK))); },
    cancel() { cancelled = true; },
  }), { headers: { 'content-type': 'text/event-stream' } }));
  const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
  assert.equal(response.status, 200);
  assert.match(await response.text(), /message_stop/);
  assert.equal(calls.length, 1);
  assert.equal(cancelled, true);
});

test('aggregate response buffer is bounded at 32 MiB before fallback', async t => {
  let cancelled = 0;
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) :
    new Response(new ReadableStream({ start(controller) {
      const chunk = new Uint8Array(8 * 1024 * 1024).fill(32);
      for (let i = 0; i < 5; i++) controller.enqueue(chunk);
    }, cancel() { cancelled++; } }), { headers: { 'content-type': 'application/json' } }), { attemptTimeoutMs: 2000 });
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 5);
  assert.equal(cancelled, 4);
});

test('count_tokens preserves original body and uses native OAuth only', async t => {
  const { calls, request } = await fixture(t, () => new Response('{"input_tokens":12}', { headers: { 'content-type': 'application/json' } }));
  const body = requestBody(SONNET, { tools: [{ type: 'advisor_20260301', name: 'advisor', model: FABLE }] });
  const response = await request(body, { path: '/v1/messages/count_tokens?beta=true', headers: proHeaders });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { input_tokens: 12 });
  assert.equal(calls[0].url, proURL + '/count_tokens?beta=true');
  assert.deepEqual(calls[0].parsedBody, body);
  assert.equal(calls[0].headers.authorization, OAUTH);
});

test('functional native bypass during expired cooldown does not consume the health probe', async t => {
  let healthy = false, time = 1000;
  const { calls, request } = await fixture(t, (url, options) => url === proURL || healthy ? jsonResponse(url, options) : new Response('fail', { status: 500 }), { cooldownMs: 1, now: () => time });
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  time += 10;
  healthy = true;
  await (await request(requestBody(SONNET, { tools: [{ type: 'advisor_20260301', name: 'advisor', model: OPUS }] }), { headers: proHeaders })).text();
  assert.equal(calls.at(-1).url, proURL);
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.at(-1).url, externalURL);
  assert.equal(calls.length, 7);
});

test('failed half-open request renews cooldown and later real request retries recovery', async t => {
  let healthy = false, time = 1000;
  const { calls, request } = await fixture(t, (url, options) => url === proURL || healthy ? jsonResponse(url, options) : new Response('fail', { status: 500 }), { cooldownMs: 10, now: () => time });
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  time += 20;
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.length, 10);
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.length, 11);
  healthy = true;
  time += 20;
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.at(-1).url, externalURL);
  assert.equal(calls.length, 12);
});

test('local provider failures retry locally then forward exact native Sonnet once', async t => {
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : new Response('local unavailable', { status: 500 }),
    { provider: 'local', key: undefined, localUrl: 'http://[::1]:8080/v1/messages', localModel: 'local-fixture' });
  assert.equal((await request(requestBody(SONNET), { headers: proHeaders })).status, 200);
  assert.equal(calls.length, 5);
  assert.ok(calls.slice(0, 4).every(call => call.url === 'http://[::1]:8080/v1/messages' && !call.headers.authorization));
  assert.equal(calls.at(-1).url, proURL);
  assert.equal(calls.at(-1).parsedBody.model, SONNET);
});

const toolStart = () => frame('content_block_start', { index: 0, content_block: { type: 'tool_use', id: 'tool_fixture', name: 'Read', input: {} } });
const toolDelta = text => frame('content_block_delta', { index: 0, delta: { type: 'input_json_delta', partial_json: text } });
const toolStop = () => frame('content_block_stop', { index: 0 });
for (const [label, middle] of [
  ['tool missing block stop', toolStart() + toolDelta('{"path":')],
  ['tool invalid JSON', toolStart() + toolDelta('{"path":') + toolStop()],
  ['tool primitive JSON', toolStart() + toolDelta('1') + toolStop()],
  ['tool array JSON', toolStart() + toolDelta('[]') + toolStop()],
  ['delta unknown index', toolDelta('{}')],
  ['stop unknown index', toolStop()],
  ['duplicate block index', toolStart() + toolStop() + toolStart() + toolStop()],
  ['server block', frame('content_block_start', { index: 0, content_block: { type: 'server_tool_use', name: 'web_search', input: {} } })],
  ['wrong block delta type', toolStart() + frame('content_block_delta', { index: 0, delta: { type: 'text_delta', text: 'x' } }) + toolStop()],
]) {
  test(`external SSE rejects ${label} before sending any failed tools`, async t => {
    const events = frame('message_start') + middle + frame('message_delta') + frame('message_stop');
    const { calls, request } = await fixture(t, url => url === proURL ? new Response('native rejection', { status: 401 }) : sseResponse([events]));
    const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
    assert.equal(response.status, 401);
    assert.equal(await response.text(), 'native rejection');
    assert.equal(calls.length, 5);
  });
}

test('complete SSE tool input JSON across deltas succeeds and restores native client model', async t => {
  const events = frame('message_start') + toolStart() + toolDelta('{"path":') + toolDelta('"fixture"}') + toolStop() +
    frame('message_delta', { delta: { stop_reason: 'tool_use' } }) + frame('message_stop');
  const { calls, request } = await fixture(t, () => sseResponse([events.slice(0, 200), events.slice(200)]));
  const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.match(text, /claude-sonnet-4-6/);
  assert.match(text, /tool_fixture/);
  assert.equal(calls.length, 1);
});

for (const [label, responseBody] of [
  ['missing content', { type: 'message', model: DEEPSEEK, stop_reason: 'end_turn' }],
  ['missing stop reason', { type: 'message', model: DEEPSEEK, content: [] }],
  ['null stop reason', { type: 'message', model: DEEPSEEK, content: [], stop_reason: null }],
  ['incomplete tool input', { type: 'message', model: DEEPSEEK, content: [{ type: 'tool_use', id: 'x', name: 'Read', input: '{"path":' }], stop_reason: 'tool_use' }],
  ['external server tool', { type: 'message', model: DEEPSEEK, content: [{ type: 'server_tool_use', id: 'x', name: 'web_search', input: {} }], stop_reason: 'end_turn' }],
]) {
  test(`external JSON rejects ${label} before fallback`, async t => {
    const { calls, request } = await fixture(t, url => url === proURL ? new Response('raw native', { status: 429 }) : new Response(JSON.stringify(responseBody), { headers: { 'content-type': 'application/json' } }));
    const response = await request(requestBody(SONNET), { headers: proHeaders });
    assert.equal(response.status, 429);
    assert.equal(await response.text(), 'raw native');
    assert.equal(calls.length, 5);
  });
}

test('native role system and unknown thinking configuration preserve original Pro', async t => {
  const { calls, request } = await fixture(t);
  for (const extra of [{ messages: [{ role: 'system', content: 'native inline system' }] }, { thinking: { type: 'adaptive', future_feature: true } }, { output_config: { format: { type: 'json_schema' } } }]) {
    const body = requestBody(SONNET, extra);
    assert.equal((await request(body, { headers: proHeaders })).status, 200);
    assert.equal(calls.at(-1).url, proURL);
    assert.deepEqual(calls.at(-1).parsedBody, body);
  }
});

for (const localAllowRemote of [undefined, false]) {
  test(`remote HTTPS local endpoint requires explicit opt-in (${localAllowRemote})`, () => {
    assert.throws(() => createProxy({ nonce: NONCE, provider: 'local',
      localUrl: 'https://llm.example.invalid/v1/messages', localModel: 'configured-model', localAllowRemote }));
  });
}

test('explicit remote HTTPS uses exactly the configured endpoint, model and isolated credential', async t => {
  const localUrl = 'https://llm.example.invalid:8443/v1/messages';
  const localKey = 'synthetic-remote-local-key';
  const localModel = 'configured-model';
  const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', key: undefined,
    localUrl, localModel, localKey, localAllowRemote: true });
  const response = await request(requestBody(SONNET), { headers: proHeaders });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).model, SONNET);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, localUrl);
  assert.deepEqual(calls[0].parsedBody, { ...requestBody(SONNET), thinking: { type: 'disabled' }, model: localModel });
  assert.equal(calls[0].headers.authorization, `Bearer ${localKey}`);
  assert.equal(calls[0].headers['anthropic-beta'], undefined);
  assert.equal(calls[0].headers['x-api-key'], undefined);
  assert.equal(JSON.stringify(calls[0].headers).includes(OAUTH), false);
  assert.equal(calls[0].redirect, 'error');
});

test('approved internal HTTP uses only the configured endpoint and isolated credential', async t => {
  const localUrl = 'http://192.0.2.10:4000/v1/messages';
  const localKey = 'synthetic-internal-key';
  const { calls, request } = await fixture(t, jsonResponse, { provider: 'local', key: undefined,
    localUrl, localModel: 'configured-model', localKey, localHttpUrl: localUrl });
  const response = await request(requestBody(SONNET), { headers: proHeaders });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, localUrl);
  assert.equal(calls[0].headers.authorization, `Bearer ${localKey}`);
  assert.equal(JSON.stringify(calls[0].headers).includes(OAUTH), false);
  assert.equal(calls[0].redirect, 'error');
});

test('explicit HTTP exception rejects other origins and invalid URL components', () => {
  const url = 'http://192.0.2.10:4000/v1/messages';
  for (const localUrl of [url.replace('.10', '.11'), url.replace(':4000', ':4001'),
    url.replace(':4000', ''), url.replace('http://', 'http://user:password@'),
    url + '?q=1', url + '#fragment', url.replace('/v1/messages', '/other'),
    url.replace('192.0.2.10', '0xc000020a'), url.replace('/v1/messages', '/other/../v1/messages')]) {
    for (const localAllowRemote of [false, true]) {
      assert.throws(() => createProxy({ nonce: NONCE, provider: 'local', localUrl,
        localModel: 'configured-model', localHttpUrl: url, localAllowRemote }));
    }
  }
});

test('HTTP exceptions require a matching pin and never bypass HTTPS opt-in', () => {
  const http = 'http://192.0.2.10:4000/v1/messages';
  const https = 'https://llm.example.invalid/v1/messages';
  for (const [localUrl, localHttpUrl] of [[http, undefined], [http, http.replace(':4000', ':4001')], [https, https]]) {
    assert.throws(() => createProxy({ nonce: NONCE, provider: 'local', localUrl, localHttpUrl,
      localModel: 'configured-model' }));
  }
});

test('remote opt-in still rejects other plaintext HTTP servers', () => {
  assert.throws(() => createProxy({ nonce: NONCE, provider: 'local',
    localUrl: 'http://llm.example.invalid/v1/messages', localModel: 'configured-model', localAllowRemote: true }));
});

test('remote opt-in requires a boolean rather than a truthy string', () => {
  assert.throws(() => createProxy({ nonce: NONCE, provider: 'local',
    localUrl: 'https://llm.example.invalid/v1/messages', localModel: 'configured-model', localAllowRemote: 'true' }));
});

test('local credentials reject CRLF before constructing request headers', () => {
  assert.throws(() => createProxy({ nonce: NONCE, provider: 'local',
    localUrl: 'http://127.0.0.1:11434/v1/messages', localModel: 'configured-model', localKey: 'synthetic\r\ninjected' }));
});

// Sonnet 5.5 rejects disabled thinking; never synthesize it during fallback.
for (const thinking of [undefined, { type: 'adaptive' }]) {
  test(`Sonnet 5.5 fallback preserves ${thinking ? 'adaptive' : 'omitted'} thinking`, async t => {
    const body = requestBody('claude-sonnet-5-5', { max_tokens: 256,
      ...(thinking ? { thinking } : {}) });
    const raw = JSON.stringify(body, null, 2);
    const { calls, request } = await fixture(t, (url, options) =>
      native(url, options) || new Response('{}', { status: 503 }));
    const response = await request(undefined, { raw, headers: proHeaders });
    assert.equal(response.status, 200);
    assert.equal(calls.length, 5);
    assert.equal(calls.filter(call => call.url === externalURL).length, 4);
    assert.equal(calls.at(-1).url, proURL);
    assert.equal(Buffer.from(calls.at(-1).body).toString(), raw);
    assert.deepEqual(calls.at(-1).parsedBody, body);
    assert.equal(Object.hasOwn(calls.at(-1).parsedBody, 'thinking'), Boolean(thinking));
  });
}

test('request-level 4xx is not retried and does not open the provider circuit', async t => {
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : new Response('bad', { status: 400 }));
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.deepEqual(calls.map(call => call.url), [externalURL, proURL]);
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls[2].url, externalURL);
});

for (const status of [401, 402, 403, 404]) {
  test(`provider-level HTTP ${status} is not retried and opens the circuit`, async t => {
    const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : new Response('no', { status }), { now: () => 1000 });
    await (await request(requestBody(SONNET), { headers: proHeaders })).text();
    await (await request(requestBody(SONNET), { headers: proHeaders })).text();
    assert.deepEqual(calls.map(call => call.url), [externalURL, proURL, proURL]);
  });
}

test('SSE response to a non-stream request is rejected and retried', async t => {
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : sseResponse([complete(DEEPSEEK)]));
  await (await request(requestBody(SONNET, { stream: false }), { headers: proHeaders })).text();
  assert.equal(calls.filter(call => call.url === externalURL).length, 4);
  assert.equal(calls.at(-1).url, proURL);
});

test('stale in-flight failure does not reopen a recovered circuit', async t => {
  let healthy = false, time = 1000, release;
  const gate = new Promise(resolve => { release = resolve; });
  const { calls, request } = await fixture(t, async (url, options) => {
    if (url === proURL) return jsonResponse(url, options);
    if (JSON.parse(options.body).system === 'A') { await gate; return new Response('fail', { status: 500 }); }
    return healthy ? jsonResponse(url, options) : new Response('fail', { status: 500 });
  }, { cooldownMs: 10, attemptTimeoutMs: 5000, now: () => time });
  const stale = request(requestBody(SONNET, { system: 'A' }), { headers: proHeaders });
  await new Promise(resolve => setTimeout(resolve, 50));
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  time += 20;
  healthy = true;
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  release();
  await (await stale).text();
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls.at(-1).url, externalURL);
});

test('ordinary mode never asks external for thinking and rejects a thinking reply for that request only', async t => {
  const withThinking = () => Response.json({ type: 'message', model: DEEPSEEK, stop_reason: 'end_turn',
    content: [{ type: 'thinking', thinking: 'x', signature: 'external' }, { type: 'text', text: 'ok' }] });
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : withThinking());
  await (await request(requestBody(SONNET, { thinking: { type: 'adaptive' } }), { headers: proHeaders })).text();
  assert.deepEqual(calls.map(call => call.url), [externalURL, proURL]);
  assert.deepEqual(calls[0].parsedBody.thinking, { type: 'disabled' });
  await (await request(requestBody(SONNET), { headers: proHeaders })).text();
  assert.equal(calls[2].url, externalURL);
});

test('stalled Plan stream is cut after the idle timeout', async t => {
  const stalled = new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('event: ping\n\n')); } }),
    { headers: { 'content-type': 'text/event-stream' } });
  const { request } = await fixture(t, () => stalled, { planIdleMs: 100, planHeaderMs: 100 });
  const response = await request(requestBody(OPUS, { stream: true }), { headers: proHeaders });
  await assert.rejects(async () => { await response.text(); });
});

test('request body up to 32 MiB reaches routing instead of being rejected at 16 MiB', async t => {
  const { calls, request } = await fixture(t, (url, options) => jsonResponse(url, options));
  const big = requestBody(SONNET, { system: 'x'.repeat(17 * 1024 * 1024) });
  assert.equal((await request(big, { headers: proHeaders })).status, 200);
  assert.ok(calls.length >= 1);
});

const trickleSse = (gapMs, count) => {
  const data = complete(DEEPSEEK);
  const size = Math.ceil(data.length / count);
  let index = 0;
  return new Response(new ReadableStream({
    async pull(controller) {
      if (index >= data.length) return controller.close();
      await new Promise(resolve => setTimeout(resolve, gapMs));
      controller.enqueue(new TextEncoder().encode(data.slice(index, index += size)));
    },
  }), { headers: { 'content-type': 'text/event-stream' } });
};

test('ordinary mode: a slow but live stream outlasts the silence limit', async t => {
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : trickleSse(40, 8),
    { attemptTimeoutMs: 150, attemptCeilingMs: 5000 });
  const response = await request(requestBody(SONNET, { stream: true }), { headers: proHeaders });
  assert.equal(response.status, 200);
  await response.text();
  assert.deepEqual(calls.map(call => call.url), [externalURL]);
});

test('ordinary mode: the total ceiling still ends an endless trickle and falls back to Plan', async t => {
  const { calls, request } = await fixture(t, (url, options) => url === proURL ? jsonResponse(url, options) : trickleSse(40, 400),
    { attemptTimeoutMs: 150, attemptCeilingMs: 200, retryDelayMs: 0 });
  await (await request(requestBody(SONNET, { stream: true }), { headers: proHeaders })).text();
  assert.equal(calls.at(-1).url, proURL);
  assert.equal(calls.filter(call => call.url === externalURL).length, 4);
});
