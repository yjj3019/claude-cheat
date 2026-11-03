// Dual provider: the alternate model id selects the second provider for that request only,
// the primary route is untouched, and each provider keeps its own breaker so one failing
// service never makes the other look down.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'dual-provider-nonce';
const MODEL = 'claude-sonnet-5-5';
const PRIMARY_MODEL = 'primary/model';
const ALT_MODEL = 'local/model';
const ALT_ALIAS = 'claude-route-local';
const OAUTH = 'Bearer sk-ant-oat-dual';
const BETA = 'oauth-2025-04-20';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const LOCAL_URL = 'http://127.0.0.1:11434/v1/messages';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json', authorization: OAUTH, 'anthropic-beta': BETA };

const reply = model => ({ id: 'msg_dual', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const planReply = () => ({ ...reply(MODEL), content: [{ type: 'text', text: 'plan-ok' }] });
const requestBody = extra => JSON.stringify({ model: MODEL, max_tokens: 16, stream: false,
  messages: [{ role: 'user', content: 'hi' }], ...extra });
const altConfig = { altProvider: 'local', altUrl: LOCAL_URL, altModel: ALT_MODEL,
  altKey: 'local-secret-key', altAlias: ALT_ALIAS };

async function startProxy(t, config = {}) {
  const server = createProxy({ key: 'sk-or-dual', nonce: NONCE, openrouterModel: PRIMARY_MODEL,
    retryDelayMs: 0, cooldownMs: 100, attemptTimeoutMs: 1000, onHandover: () => {}, ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  const send = (model, extra = {}, headers = HEADERS) => fetch(`${base}/v1/messages`,
    { method: 'POST', headers, body: requestBody({ model, ...extra }) });
  const sendOk = async (model, extra, headers) => {
    const response = await send(model, extra, headers);
    return { status: response.status, text: await response.text() };
  };
  return { send, sendOk };
}

test('alternate Local materializes compatible tools and leaves unknown native requests on Plan', async t => {
  const calls = [];
  const proxy = await startProxy(t, { ...altConfig, fetchImpl: async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body), headers: init.headers });
    return Response.json(url === LOCAL_URL ? reply(ALT_MODEL) : url === EXTERNAL ? reply(PRIMARY_MODEL) : planReply());
  } });
  const tool = { name: 'mcp__fixture__echo', input_schema: { type: 'object' } };
  const extra = { stream: true, metadata: { user_id: 'private-fixture' },
    messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' },
      { type: 'tool_addition', tool: { type: 'tool_definition', definition: tool } }] }] };
  const headers = { ...HEADERS, 'anthropic-beta': `${BETA},inline-tools-2026-09-15` };
  const result = await proxy.sendOk(ALT_ALIAS, extra, headers);
  assert.equal(result.status, 200);
  assert.match(result.text, /message_stop/);
  assert.equal(calls[0].url, LOCAL_URL);
  assert.deepEqual(calls[0].body.tools, [tool]);
  assert.deepEqual(calls[0].body.messages, [{ role: 'user', content: [{ type: 'text', text: 'hello' }] }]);
  assert.equal(calls[0].headers.authorization, 'Bearer local-secret-key');
  assert.equal(calls[0].headers['anthropic-beta'], headers['anthropic-beta']);
  assert.equal(calls[0].body.stream, false);
  assert.equal(calls[0].body.metadata, undefined);
  assert.equal((await proxy.sendOk(ALT_ALIAS, extra, { ...headers, 'anthropic-beta': `${BETA},unknown-feature-2099-01-01` })).status, 200);
  assert.equal(calls[1].url, PLAN);
  assert.deepEqual(calls[1].body.messages, extra.messages);
  assert.equal((await proxy.sendOk(MODEL, extra, headers)).status, 200);
  assert.equal(calls[2].url, EXTERNAL);
  assert.deepEqual(calls[2].body.tools, [tool]);
  assert.equal(calls[2].body.metadata, undefined);
  assert.equal(calls[2].headers['anthropic-beta'], undefined);
});

test('the alternate model id routes to the alternate provider with its own credential', { timeout: 15000 }, async t => {
  const calls = [];
  const proxy = await startProxy(t, { ...altConfig,
    fetchImpl: async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body), auth: init.headers.authorization });
      return Response.json(reply(url === LOCAL_URL ? ALT_MODEL : PRIMARY_MODEL));
    } });
  const alt = await proxy.sendOk(ALT_ALIAS);
  assert.equal(alt.status, 200);
  assert.match(alt.text, new RegExp(ALT_ALIAS), 'the answer carries the client model id');
  const local = calls[0];
  assert.equal(local.url, LOCAL_URL);
  assert.equal(local.body.model, ALT_MODEL);
  assert.equal(local.body.thinking.type, 'disabled');
  assert.equal(local.auth, 'Bearer local-secret-key');
  const primary = await proxy.sendOk(MODEL);
  assert.equal(primary.status, 200);
  assert.match(primary.text, new RegExp(`"${MODEL}"`));
  assert.equal(calls[1].url, EXTERNAL);
  assert.equal(calls[1].body.model, PRIMARY_MODEL);
  assert.equal(calls[1].auth, 'Bearer sk-or-dual');
});

test('without an alternate provider the same model id stays blocked', { timeout: 15000 }, async t => {
  const proxy = await startProxy(t, { fetchImpl: async () => { throw new Error('no call expected'); } });
  const blocked = await proxy.send(ALT_ALIAS);
  assert.equal(blocked.status, 403);
  await blocked.text();
});

test('an alternate provider is refused outside ordinary mode and with invalid settings', () => {
  // Local-primary fixtures: with an OpenRouter base the same-provider guard would fire
  // before the key/model/alias validations, so each refusal below must be the specific one.
  const localBase = { nonce: NONCE, provider: 'local', localUrl: LOCAL_URL,
    localModel: 'primary-local-model', localKey: 'primary-local-key' };
  const altOpenrouter = { altProvider: 'openrouter', altModel: 'alt/or-model',
    altKey: 'sk-or-alt0000000000000000000000000000000000000', altAlias: ALT_ALIAS };
  assert.throws(() => createProxy({ ...localBase, localOnly: true, ...altOpenrouter }), /ordinary session/);
  assert.throws(() => createProxy({ ...localBase, ...altOpenrouter, altKey: 'wrong-prefix' }),
    /Alternate OpenRouter configuration required/);
  assert.throws(() => createProxy({ ...localBase, ...altOpenrouter, altModel: 'bad model' }),
    /Alternate OpenRouter configuration required/);
  assert.throws(() => createProxy({ ...localBase, ...altOpenrouter, altAlias: 'claude-sonnet-5' }), /collides/);
  assert.throws(() => createProxy({ ...localBase, ...altOpenrouter, altAlias: 'bad id' }),
    /Invalid alternate model alias/);
  assert.throws(() => createProxy({ ...localBase, altProvider: 'local', altModel: ALT_MODEL, altAlias: ALT_ALIAS }),
    /Invalid alternate provider/);
  // The symmetric cases: an OpenRouter session cannot take itself as the alternate, and an
  // alternate Local service still has to pass the endpoint policy.
  assert.throws(() => createProxy({ key: 'sk-or-dual', nonce: NONCE, openrouterModel: PRIMARY_MODEL,
    altProvider: 'openrouter', altModel: 'a/b', altKey: 'sk-or-x', altAlias: ALT_ALIAS }),
    /Invalid alternate provider/);
  assert.throws(() => createProxy({ key: 'sk-or-dual', nonce: NONCE, openrouterModel: PRIMARY_MODEL,
    altProvider: 'local', altModel: 'm', altAlias: ALT_ALIAS }), /Invalid Local/);
});

test('a local-primary session dispatches the OpenRouter alias to OpenRouter', { timeout: 15000 }, async t => {
  const calls = [];
  const ALT_KEY = 'sk-or-alt0000000000000000000000000000000000000';
  const proxy = await startProxy(t, { provider: 'local', localUrl: LOCAL_URL,
    localModel: 'primary-local-model', localKey: 'primary-local-key',
    altProvider: 'openrouter', altModel: 'alt/or-model', altKey: ALT_KEY, altAlias: 'claude-route-openrouter',
    fetchImpl: async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body), auth: init.headers.authorization });
      return Response.json(reply(url === EXTERNAL ? 'alt/or-model' : 'primary-local-model'));
    } });
  const alias = await proxy.sendOk('claude-route-openrouter');
  assert.equal(alias.status, 200);
  assert.equal(calls[0].url, EXTERNAL);
  assert.equal(calls[0].body.model, 'alt/or-model');
  assert.equal(calls[0].body.provider.allow_fallbacks, false);
  assert.equal(calls[0].auth, `Bearer ${ALT_KEY}`);
  const primary = await proxy.sendOk(MODEL);
  assert.equal(primary.status, 200);
  assert.equal(calls[1].url, LOCAL_URL);
  assert.equal(calls[1].body.model, 'primary-local-model');
});

test('alternate OpenRouter receives validated inline tools without switching to Plan', async t => {
  const calls = [];
  const proxy = await startProxy(t, { provider: 'local', localUrl: LOCAL_URL,
    localModel: 'primary-local-model', localKey: 'primary-local-key',
    altProvider: 'openrouter', altModel: 'alt/or-model',
    altKey: 'sk-or-alt0000000000000000000000000000000000000', altAlias: 'claude-route-openrouter',
    fetchImpl: async (url, init) => { calls.push({ url, body: JSON.parse(init.body), headers: init.headers });
      return Response.json(url === EXTERNAL ? reply('alt/or-model') : planReply()); } });
  const tool = { name: 'fixture', input_schema: { type: 'object' } };
  const extra = { messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' },
    { type: 'tool_addition', tool: { type: 'tool_definition', definition: tool } }] }] };
  const headers = { ...HEADERS, 'anthropic-beta': `${BETA},inline-tools-2026-09-15` };
  assert.equal((await proxy.sendOk('claude-route-openrouter', extra, headers)).status, 200);
  assert.deepEqual(calls.map(call => call.url), [EXTERNAL]);
  assert.deepEqual(calls[0].body.tools, [tool]);
  assert.deepEqual(calls[0].body.messages, [{ role: 'user', content: [{ type: 'text', text: 'hello' }] }]);
  assert.equal(calls[0].headers['anthropic-beta'], undefined);
});

test('OpenRouter inline conversion preserves strict validation and never retries failures on Plan', async t => {
  const calls = [];
  const proxy = await startProxy(t, { ...altConfig, fetchImpl: async url => {
    calls.push(url);
    return Response.json({ type: 'message', model: PRIMARY_MODEL, content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' });
  } });
  const tool = { name: 'fixture', input_schema: { type: 'object' } };
  const extra = { messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' },
    { type: 'tool_addition', tool: { type: 'tool_definition', definition: tool } }] }] };
  const headers = { ...HEADERS, 'anthropic-beta': `${BETA},inline-tools-2026-09-15` };
  const result = await proxy.sendOk(MODEL, extra, headers);
  assert.equal(result.status, 503);
  assert.deepEqual(calls, [EXTERNAL, EXTERNAL, EXTERNAL, EXTERNAL]);
});

test('OpenRouter inline conversion rejects unsupported extensions and conflicting definitions', async t => {
  const calls = [];
  const proxy = await startProxy(t, { ...altConfig, fetchImpl: async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return Response.json(planReply());
  } });
  const tool = { name: 'fixture', input_schema: { type: 'object' } };
  const messages = [{ role: 'user', content: [{ type: 'text', text: 'hello' },
    { type: 'tool_addition', tool: { type: 'tool_definition', definition: tool } }] }];
  const headers = { ...HEADERS, 'anthropic-beta': `${BETA},inline-tools-2026-09-15` };
  for (const extra of [
    { messages, output_config: { format: { type: 'json_schema', schema: { type: 'object' } } } },
    { messages, tools: [{ ...tool, description: 'conflicting definition' }] },
    { messages: [{ role: 'assistant', content: [{ type: 'tool_use', id: 'call_1', name: 'fixture', input: {},
      provider_specific_fields: { opaque: true } }] }, ...messages] },
  ]) {
    assert.equal((await proxy.sendOk(MODEL, extra, headers)).status, 200);
    assert.equal(calls.at(-1).url, PLAN);
    assert.deepEqual(calls.at(-1).body.messages, extra.messages);
  }
});

test('an alternate provider failure opens only its own breaker', { timeout: 20000 }, async t => {
  const urls = [], planBodies = [];
  const proxy = await startProxy(t, { ...altConfig,
    fetchImpl: async (url, init) => {
      urls.push(url);
      if (url === PLAN) { planBodies.push(JSON.parse(init.body)); return Response.json(planReply()); }
      if (url === LOCAL_URL) return new Response('down', { status: 500 });
      return Response.json(reply(PRIMARY_MODEL));
    } });
  const failed = await proxy.sendOk(ALT_ALIAS);
  assert.equal(failed.status, 503);
  assert.match(failed.text, /local_upstream_error/);
  assert.equal(urls.filter(url => url === LOCAL_URL).length, 4, 'the transient failure is retried');
  assert.equal(planBodies.length, 0);
  // The primary provider is untouched by the alternate breaker.
  const primary = await proxy.sendOk(MODEL);
  assert.equal(primary.status, 200);
  assert.match(primary.text, new RegExp(`"${MODEL}"`));
  // While the alternate breaker is open the alias reports its Local cooldown.
  const during = await proxy.sendOk(ALT_ALIAS);
  assert.equal(during.status, 503);
  assert.match(during.text, /local_cooldown/);
  assert.equal(urls.filter(url => url === LOCAL_URL).length, 4, 'the open alternate breaker is not hammered');
});

test('an alternate answer naming the wrong model is validated and never passed through', { timeout: 20000 }, async t => {
  const urls = [];
  const proxy = await startProxy(t, { ...altConfig,
    fetchImpl: async (url, init) => {
      urls.push(url);
      if (url === PLAN) return Response.json(planReply());
      if (url === LOCAL_URL) return Response.json(reply('other/model'));
      return Response.json(reply(PRIMARY_MODEL));
    } });
  const result = await proxy.sendOk(ALT_ALIAS);
  assert.equal(result.status, 503);
  assert.doesNotMatch(result.text, /plan-ok/);
  assert.equal(urls.filter(url => url === LOCAL_URL).length, 4, 'the mismatch is retried before the breaker opens');
});
