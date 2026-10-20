import assert from 'node:assert/strict';
import { once } from 'node:events';
import test from 'node:test';
import { createProxy, DEEPSEEK } from '../claudeRouteProxy.mjs';

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
  model: options ? JSON.parse(options.body).model : DEEPSEEK, content: [], stop_reason: 'end_turn' }), {
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
  const server = createProxy({ key: KEY, nonce: NONCE, retryDelayMs: 0, attemptTimeoutMs: 200, ...config, fetchImpl: async (url, options) => {
    calls.push({ url, ...options, parsedBody: JSON.parse(options.body) });
    return upstream(url, options);
  } });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
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
const complete = model => frame('message_start', { message: { model } }) + frame('message_delta') + frame('message_stop');
const native = (url, options) => url.startsWith('https://api.anthropic.com') ? jsonResponse(url, options) : null;

for (const model of [DEEPSEEK, SONNET]) {
  test(`${model} external JSON keeps metadata and isolates OAuth`, async t => {
    const body = requestBody(model, { metadata: { user_id: 'fixture' }, tools: [{ name: 'Read', input_schema: { type: 'object' } }] });
    const { calls, request } = await fixture(t);
    const response = await request(body, { headers: proHeaders });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).model, model);
    assert.deepEqual(calls[0].parsedBody, { ...body, model: DEEPSEEK, provider: { allow_fallbacks: false } });
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
    assert.deepEqual(calls[0].parsedBody, { ...body, model: DEEPSEEK, provider: { allow_fallbacks: false } });
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
  assert.deepEqual(calls[0].parsedBody, { ...requestBody(SONNET), model: localModel });
  assert.equal(calls[0].headers.authorization, `Bearer ${localKey}`);
  assert.equal(calls[0].headers['anthropic-beta'], undefined);
  assert.equal(calls[0].headers['x-api-key'], undefined);
  assert.equal(JSON.stringify(calls[0].headers).includes(OAUTH), false);
  assert.equal(calls[0].redirect, 'error');
});

test('remote opt-in never allows plaintext HTTP', () => {
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
