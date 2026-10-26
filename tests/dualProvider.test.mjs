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
  const send = (model, extra = {}) => fetch(`${base}/v1/messages`,
    { method: 'POST', headers: HEADERS, body: requestBody({ model, ...extra }) });
  const sendOk = async (model, extra) => {
    const response = await send(model, extra);
    return { status: response.status, text: await response.text() };
  };
  return { send, sendOk };
}

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
  const base = { key: 'sk-or-dual', nonce: NONCE, openrouterModel: PRIMARY_MODEL, provider: 'openrouter' };
  assert.throws(() => createProxy({ ...base, localOnly: true, provider: 'local',
    localUrl: LOCAL_URL, localModel: 'm', altProvider: 'openrouter', altModel: 'a/b', altKey: 'sk-or-x', altAlias: ALT_ALIAS }));
  assert.throws(() => createProxy({ ...base, altProvider: 'openrouter', altModel: 'a/b', altKey: 'wrong-prefix', altAlias: ALT_ALIAS }));
  assert.throws(() => createProxy({ ...base, altProvider: 'openrouter', altModel: 'bad model', altKey: 'sk-or-x', altAlias: ALT_ALIAS }));
  assert.throws(() => createProxy({ ...base, altProvider: 'local', altModel: ALT_MODEL, altAlias: ALT_ALIAS }));
  assert.throws(() => createProxy({ ...base, altProvider: 'openrouter', altModel: PRIMARY_MODEL, altKey: 'sk-or-x', altAlias: ALT_ALIAS }));
  assert.throws(() => createProxy({ ...base, altProvider: 'openrouter', altModel: 'a/b', altKey: 'sk-or-x', altAlias: 'sonnet' }));
  assert.throws(() => createProxy({ ...base, altProvider: 'openrouter', altModel: 'a/b', altKey: 'sk-or-x', altAlias: 'bad id' }));
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
  assert.equal(failed.status, 200);
  assert.match(failed.text, /plan-ok/, 'the alias request falls back to Plan');
  assert.equal(urls.filter(url => url === LOCAL_URL).length, 4, 'the transient failure is retried');
  assert.equal(planBodies.at(-1).model, 'claude-sonnet-5-5', 'the alias id never reaches Plan');
  // The primary provider is untouched by the alternate breaker.
  const primary = await proxy.sendOk(MODEL);
  assert.equal(primary.status, 200);
  assert.match(primary.text, new RegExp(`"${MODEL}"`));
  // While the alternate breaker is open the alias goes straight to Plan.
  const during = await proxy.sendOk(ALT_ALIAS);
  assert.equal(during.status, 200);
  assert.match(during.text, /plan-ok/);
  assert.equal(urls.filter(url => url === LOCAL_URL).length, 4, 'the open alternate breaker is not hammered');
});
