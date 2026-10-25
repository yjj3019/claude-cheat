// Request normalization: the Plan route only ever sees representable tool ids, while the
// external route is untouched; the opt-in cache stabilizer peels only a trailing footer.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const NONCE = 'request-rewrite-nonce';
const MODEL = 'claude-sonnet-5-5';
const PRO_MODEL = 'claude-opus-5-5';
const UPSTREAM_MODEL = 'rewrite/model';
const OAUTH = 'Bearer sk-ant-oat-rewrite';
const BETA = 'oauth-2025-04-20';
const EXTERNAL = 'https://openrouter.ai/api/v1/messages';
const PLAN = 'https://api.anthropic.com/v1/messages';
const AUTH = { 'x-local-route-token': NONCE };
const HEADERS = { ...AUTH, 'content-type': 'application/json', authorization: OAUTH, 'anthropic-beta': BETA };
const TOOL_ID = /^[a-zA-Z0-9_-]{1,64}$/;

const message = model => ({ id: 'msg_rewrite', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const planReply = () => Response.json(message(MODEL));
const externalReply = () => Response.json(message(UPSTREAM_MODEL));

async function startProxy(t, { respond, ...config } = {}) {
  const calls = [];
  const server = createProxy({ key: 'sk-or-rewrite', nonce: NONCE, openrouterModel: UPSTREAM_MODEL,
    retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 1000, onHandover: () => {},
    fetchImpl: async (url, options) => {
      const call = { url, body: options.body, parsed: JSON.parse(options.body) };
      calls.push(call);
      if (respond) return respond(call);
      return url === PLAN ? planReply() : externalReply();
    },
    ...config });
  t.after(async () => {
    server.closeAllConnections();
    if (!server.listening) return;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    calls,
    send: (body, raw) => fetch(`${base}/v1/messages`, { method: 'POST', headers: HEADERS,
      body: raw ?? JSON.stringify(body) }),
  };
}

const toolTurn = (id, extra = {}) => ({
  messages: [
    { role: 'user', content: 'run it' },
    { role: 'assistant', content: [{ type: 'tool_use', id, name: 'Bash', input: { command: 'ls' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content: 'ok' }] },
  ],
  ...extra,
});

const captureLogs = t => {
  const log = t.mock.method(console, 'error', () => {});
  return () => log.mock.calls.map(call => {
    try { return JSON.parse(call.arguments[0]); } catch { return null; }
  }).filter(Boolean);
};

test('non-conforming tool ids are rewritten consistently and deterministically on the Plan route',
  { timeout: 15000 }, async t => {
    const entries = captureLogs(t);
    const proxy = await startProxy(t);
    const body = { model: PRO_MODEL, max_tokens: 32, ...toolTurn('Bash:0#abc') };
    const response = await proxy.send(body);
    assert.equal(response.status, 200);
    await response.text();
    assert.equal(proxy.calls.length, 1);
    assert.equal(proxy.calls[0].url, PLAN);
    const [{ parsed }] = proxy.calls;
    const toolUse = parsed.messages[1].content[0];
    const toolResult = parsed.messages[2].content[0];
    assert.match(toolUse.id, TOOL_ID);
    assert.notEqual(toolUse.id, 'Bash:0#abc');
    assert.equal(toolResult.tool_use_id, toolUse.id, 'the tool_result pair follows the rewrite');
    assert.equal(toolUse.name, 'Bash');
    assert.deepEqual(toolUse.input, { command: 'ls' });
    assert.equal(parsed.messages[0].content, 'run it');
    // Only the ids changed: every other block field stays exactly as the client sent it.
    assert.deepEqual({ ...toolUse, id: 'Bash:0#abc' }, body.messages[1].content[0]);
    assert.deepEqual({ ...toolResult, tool_use_id: 'Bash:0#abc' }, body.messages[2].content[0]);
    // Deterministic per request: the same history yields the same replacement.
    await (await proxy.send(body)).text();
    assert.equal(proxy.calls[1].parsed.messages[1].content[0].id, toolUse.id);
    const normalized = entries().filter(entry => entry.event === 'tool_ids_normalized');
    assert.equal(normalized.length, 2);
    assert.deepEqual(normalized.map(entry => entry.count), [1, 1]);
    assert.equal(JSON.stringify(normalized).includes('Bash:0#abc'), false, 'logs carry the count only');
  });

test('conforming tool ids stay byte-identical and are not logged', { timeout: 15000 }, async t => {
  const entries = captureLogs(t);
  const proxy = await startProxy(t);
  const body = { model: PRO_MODEL, max_tokens: 32, ...toolTurn('call_abc-123') };
  const raw = JSON.stringify(body);
  const response = await proxy.send(body, raw);
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(proxy.calls.length, 1);
  assert.equal(proxy.calls[0].url, PLAN);
  assert.equal(Buffer.from(proxy.calls[0].body).toString(), raw, 'a clean body keeps its exact bytes');
  assert.deepEqual(proxy.calls[0].parsed.messages, body.messages);
  assert.equal(entries().some(entry => entry.event === 'tool_ids_normalized'), false);
});

test('generated ids avoid reserved ids and duplicate source ids map once',
  { timeout: 15000 }, async t => {
    const entries = captureLogs(t);
    const proxy = await startProxy(t);
    const body = { model: PRO_MODEL, max_tokens: 32,
      messages: [
        { role: 'assistant', content: [
          { type: 'tool_use', id: 'toolu_1', name: 'Read', input: {} },
          { type: 'tool_use', id: 'dup:id', name: 'Bash', input: {} },
        ] },
        { role: 'user', content: [
          { type: 'tool_result', tool_use_id: 'toolu_1', content: 'first' },
          { type: 'tool_result', tool_use_id: 'dup:id', content: 'second' },
        ] },
        { role: 'assistant', content: [{ type: 'tool_use', id: 'dup:id', name: 'Bash', input: {} }] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'dup:id', content: 'again' }] },
      ] };
    const response = await proxy.send(body);
    assert.equal(response.status, 200);
    await response.text();
    const [{ parsed }] = proxy.calls;
    const [conforming, rewritten] = parsed.messages[0].content;
    assert.equal(conforming.id, 'toolu_1', 'conforming id untouched');
    assert.equal(rewritten.id, 'toolu_2', 'allocator skips the reserved id');
    const [firstResult, secondResult] = parsed.messages[1].content;
    assert.equal(firstResult.tool_use_id, 'toolu_1');
    assert.equal(secondResult.tool_use_id, 'toolu_2');
    assert.notEqual(firstResult.tool_use_id, secondResult.tool_use_id);
    // A duplicate source id is one mapping, so every occurrence agrees with its pair.
    assert.equal(parsed.messages[2].content[0].id, 'toolu_2');
    assert.equal(parsed.messages[3].content[0].tool_use_id, 'toolu_2');
    const normalized = entries().filter(entry => entry.event === 'tool_ids_normalized');
    assert.deepEqual(normalized.map(entry => entry.count), [1]);
  });

test('the external route never rewrites tool ids', { timeout: 15000 }, async t => {
  const entries = captureLogs(t);
  const proxy = await startProxy(t);
  const body = { model: MODEL, max_tokens: 32, ...toolTurn('Bash:0#abc') };
  const response = await proxy.send(body);
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(proxy.calls.length, 1);
  assert.equal(proxy.calls[0].url, EXTERNAL);
  assert.deepEqual(proxy.calls[0].parsed.messages, body.messages, 'external ids are untouched');
  assert.equal(proxy.calls[0].body.includes('Bash:0#abc'), true);
  assert.equal(entries().some(entry => entry.event === 'tool_ids_normalized'), false);
});

test('an empty tool id is rejected locally with an Anthropic-shaped 400 and no upstream call',
  { timeout: 15000 }, async t => {
    const proxy = await startProxy(t);
    const body = { model: PRO_MODEL, max_tokens: 32, ...toolTurn('') };
    const response = await proxy.send(body);
    assert.equal(response.status, 400);
    assert.equal(proxy.calls.length, 0, 'the upstream is never contacted');
    const error = await response.json();
    assert.equal(error.type, 'error');
    assert.equal(typeof error.error.type, 'string');
    assert.match(error.error.message, /tool call id/i);
  });

test('a Plan fallback request carries normalized ids while the external attempts stay untouched',
  { timeout: 15000 }, async t => {
    const proxy = await startProxy(t, { respond: ({ url }) => url === PLAN ? planReply() : new Response('down', { status: 500 }) });
    const body = { model: MODEL, max_tokens: 32, ...toolTurn('Bash:0#abc') };
    const response = await proxy.send(body);
    assert.equal(response.status, 200);
    await response.text();
    const externalCalls = proxy.calls.filter(call => call.url === EXTERNAL);
    const planCalls = proxy.calls.filter(call => call.url === PLAN);
    assert.equal(externalCalls.length, 4);
    assert.equal(planCalls.length, 1);
    assert.equal(externalCalls[0].parsed.messages[1].content[0].id, 'Bash:0#abc', 'external attempts keep the raw id');
    const planToolUse = planCalls[0].parsed.messages[1].content[0];
    assert.match(planToolUse.id, TOOL_ID);
    assert.notEqual(planToolUse.id, 'Bash:0#abc');
    assert.equal(planCalls[0].parsed.messages[2].content[0].tool_use_id, planToolUse.id);
  });

const FOOTER = '<total_tokens>1234 tokens left</total_tokens>';

test('the opt-in stabilizer peels only a trailing system footer', { timeout: 15000 }, async t => {
  const system = `System guidance.\n${FOOTER}`;
  const body = { model: MODEL, max_tokens: 32, system,
    messages: [{ role: 'user', content: 'hi' }] };
  const disabled = await startProxy(t);
  await (await disabled.send(body)).text();
  const enabled = await startProxy(t, { stabilizePromptCache: true });
  await (await enabled.send(body)).text();
  const before = disabled.calls[0].parsed;
  const after = enabled.calls[0].parsed;
  assert.equal(before.system, system, 'off by default: the footer stays');
  assert.equal(after.system, 'System guidance.\n');
  assert.deepEqual({ ...after, system: before.system }, before, 'every other field is unchanged');
  // A body whose only system content is the footer becomes an empty string, no more.
  const onlyFooter = await startProxy(t, { stabilizePromptCache: true });
  await (await onlyFooter.send({ ...body, system: FOOTER })).text();
  assert.equal(onlyFooter.calls[0].parsed.system, '');
});

test('the stabilizer leaves non-matching and fenced footers byte-identically untouched',
  { timeout: 15000 }, async t => {
    const enabled = await startProxy(t, { stabilizePromptCache: true });
    const disabled = await startProxy(t);
    const cases = [
      `Intro ${FOOTER} then more`,
      `\`\`\`\n${FOOTER}`,
      `${FOOTER}\n`,
      'no footer here',
    ];
    for (const [index, system] of cases.entries()) {
      const body = { model: MODEL, max_tokens: 32, system, messages: [{ role: 'user', content: 'hi' }] };
      await (await enabled.send(body)).text();
      await (await disabled.send(body)).text();
      assert.equal(enabled.calls[index].parsed.system, system, `system must stay identical: ${JSON.stringify(system)}`);
      // Enabling the stabilizer must not change a single byte when no footer is peelable.
      assert.equal(Buffer.from(enabled.calls[index].body).toString(),
        Buffer.from(disabled.calls[index].body).toString(), `body must stay byte-identical: ${JSON.stringify(system)}`);
    }
  });

test('only the last system text block may be stabilized', { timeout: 15000 }, async t => {
  const proxy = await startProxy(t, { stabilizePromptCache: true });
  const system = [
    { type: 'text', text: `first block ${FOOTER}` },
    { type: 'text', text: `second block\n${FOOTER}`, cache_control: { type: 'ephemeral' } },
    { type: 'text', text: 'third block without footer' },
  ];
  await (await proxy.send({ model: MODEL, max_tokens: 32, system,
    messages: [{ role: 'user', content: 'hi' }] })).text();
  const captured = proxy.calls[0].parsed.system;
  assert.equal(captured.length, 3);
  assert.equal(captured[0].text, `first block ${FOOTER}`, 'earlier footer blocks are untouched');
  assert.equal(captured[1].text, `second block\n${FOOTER}`, 'only the last text block is considered');
  assert.deepEqual(captured[1].cache_control, { type: 'ephemeral' });
  assert.equal(captured[2].text, 'third block without footer');
});

test('the last system text block is peeled with its cache marker intact',
  { timeout: 15000 }, async t => {
    const proxy = await startProxy(t, { stabilizePromptCache: true });
    const system = [
      { type: 'text', text: 'earlier' },
      { type: 'text', text: `tracked\n${FOOTER}`, cache_control: { type: 'ephemeral' } },
    ];
    await (await proxy.send({ model: MODEL, max_tokens: 32, system,
      messages: [{ role: 'user', content: 'hi' }] })).text();
    const captured = proxy.calls[0].parsed.system;
    assert.equal(captured[0].text, 'earlier');
    assert.equal(captured[1].text, 'tracked\n');
    assert.deepEqual(captured[1].cache_control, { type: 'ephemeral' });
  });
