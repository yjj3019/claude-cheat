import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { listenSafe } from './listenSafe.mjs';

// The route log path is read at import time, so set it before loading the proxy.
const logFile = join(mkdtempSync(join(tmpdir(), 'shape-')), 'route.log');
process.env.ROUTE_LOG_PATH = logFile;
const { createProxy, DEEPSEEK } = await import('../claudeRouteProxy.mjs');

const NONCE = 'test-local-nonce';
const SECRET_PROMPT = 'unique-prompt-marker-7731';
const SECRET_TOOL = 'unique-tool-marker-4419';
const response = () => new Response(JSON.stringify({ id: 'm', type: 'message', role: 'assistant', model: DEEPSEEK,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } }),
{ headers: { 'content-type': 'application/json' } });

async function send(t, body) {
  const server = createProxy({ key: 'k', nonce: NONCE, openrouterModel: DEEPSEEK, retryDelayMs: 0, attemptTimeoutMs: 2000,
    fetchImpl: async () => response() });
  await listenSafe(server);
  t.after(() => { server.closeAllConnections(); server.close(); });
  const r = await fetch(`http://127.0.0.1:${server.address().port}/v1/messages`, { method: 'POST',
    headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE },
    body: typeof body === 'string' ? body : JSON.stringify(body) });
  await r.text();
}
const shapes = () => readFileSync(logFile, 'utf8').split('\n').filter(l => l.includes('"request_shape"'))
  .map(l => JSON.parse(l.slice(l.indexOf('{'))));
async function shapeOf(t, body) {
  const before = shapes().length;
  await send(t, body);
  const all = shapes();
  assert.equal(all.length, before + 1);
  const { traceId, ...rest } = all.at(-1);
  assert.match(traceId, /^[0-9a-f]{8,12}$/);
  return rest;
}
const base = (model, extra = {}) => ({ model, max_tokens: 8, messages: [{ role: 'user', content: SECRET_PROMPT }], ...extra });
const flags = { tools: false, tool_blocks: false, media: false, stream: false };

for (const [model, family] of [['claude-sonnet-4-6', 'sonnet'], ['claude-opus-5-5', 'opus'], ['claude-haiku-5-5', 'haiku'],
  ['claude-fable-5-1', 'fable'], [DEEPSEEK, 'other']]) {
  test(`request_shape family ${family}`, async t => {
    assert.deepEqual(await shapeOf(t, base(model)), { event: 'request_shape', family, ...flags });
  });
}

test('request_shape tools flag and stream flag', async t => {
  const tools = [{ name: SECRET_TOOL, description: 'd', input_schema: { type: 'object' } }];
  assert.equal((await shapeOf(t, base(DEEPSEEK, { tools }))).tools, true);
  assert.equal((await shapeOf(t, base(DEEPSEEK, { tools: [] }))).tools, false);
  assert.equal((await shapeOf(t, base(DEEPSEEK, { stream: true }))).stream, true);
});

test('request_shape tool_result and image blocks', async t => {
  const toolResult = [{ role: 'assistant', content: [{ type: 'tool_use', id: 'x', name: SECRET_TOOL, input: {} }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'x', content: SECRET_PROMPT }] }];
  const s1 = await shapeOf(t, { ...base(DEEPSEEK), messages: toolResult });
  assert.deepEqual([s1.tool_blocks, s1.media], [true, false]);
  const image = [{ role: 'user', content: [{ type: 'text', text: SECRET_PROMPT },
    { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAAA' } }] }];
  const s2 = await shapeOf(t, { ...base(DEEPSEEK), messages: image });
  assert.deepEqual([s2.tool_blocks, s2.media], [false, true]);
  const plain = [{ role: 'user', content: [{ type: 'text', text: 'a' }, { type: 'thinking', thinking: 'b' }] }];
  const s3 = await shapeOf(t, { ...base(DEEPSEEK), messages: plain });
  assert.deepEqual([s3.tool_blocks, s3.media], [false, false]);
});

test('malformed bodies never throw and requests are still answered', async t => {
  await send(t, { model: DEEPSEEK, max_tokens: 8, messages: 'not-an-array' });
  await send(t, { model: DEEPSEEK, max_tokens: 8, messages: [null, 7, { role: 'user', content: [null, 3] }] });
  await send(t, '{not json');
});

test('log never contains prompt text or tool names', () => {
  const log = readFileSync(logFile, 'utf8');
  assert.ok(log.includes('request_shape'));
  assert.equal(log.includes(SECRET_PROMPT), false);
  assert.equal(log.includes(SECRET_TOOL), false);
});
