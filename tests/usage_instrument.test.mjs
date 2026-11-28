// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import test from 'node:test';
import { randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const model = 'fixture-model';
const body = { model: 'claude-sonnet-5-5', max_tokens: 32, messages: [{ role: 'user', content: 'PRIVATE-PROMPT' }] };
const headers = { 'content-type': 'application/json', 'x-local-route-token': 'nonce-fixture',
  authorization: 'Bearer sk-ant-oat-fixture', 'anthropic-beta': 'oauth-2025-04-20' };
const answer = (m = model) => ({ id: 'msg_fixture', type: 'message', role: 'assistant', model: m,
  content: [{ type: 'text', text: 'PRIVATE-REPLY' }], stop_reason: 'end_turn', usage: { input_tokens: 5, output_tokens: 2 } });

async function waitRows(path, count) {
  const until = Date.now() + 4000;
  while (Date.now() < until) {
    try {
      const rows = (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
      if (rows.length >= count) return rows;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error('timed out awaiting usage events');
}

async function setup(t, impl, options = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'claude-cheat-usage-'));
  const events = join(dir, 'events.jsonl'), instanceId = randomUUID();
  const server = createProxy({ nonce: 'nonce-fixture', provider: 'local', localUrl: 'http://127.0.0.1:11434/v1/messages',
    localModel: model, retryDelayMs: 1, cooldownMs: 0, attemptTimeoutMs: 1000,
    instanceId, usageEventsPath: events, instanceMetricsPath: join(dir, 'metrics.json'), fetchImpl: impl, ...options });
  await listenSafe(server);
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 }); });
  const url = `http://127.0.0.1:${server.address().port}`;
  return { events, instanceId, send: (payload, signal) => fetch(url + '/v1/messages', { method: 'POST', headers, body: JSON.stringify(payload), signal }),
    metrics: () => fetch(url + '/api/metrics', { headers }).then(r => r.json()) };
}

test('per-instance events record verified usage without contents or credentials', async t => {
  const f = await setup(t, async () => Response.json(answer()));
  await (await f.send(body)).text(); await (await f.send(body)).text();
  const rows = await waitRows(f.events, 2);
  assert.deepEqual(rows.map(r => r.event_seq), [1, 2]);
  for (const row of rows) {
    assert.equal(row.schema_version, 2);
    assert.equal(row.instance_id, f.instanceId); assert.equal(row.provider, 'local');
    assert.equal(row.requested_model, model); assert.equal(row.reported_model, model);
    assert.equal(row.usage_measured, true); assert.equal(row.input_tokens, 5); assert.equal(row.output_tokens, 2);
    assert.equal(row.status, 'ok'); assert.ok(row.elapsed_ms >= 0);
  }
  assert.doesNotMatch(JSON.stringify(rows), /PRIVATE|sk-ant|nonce-fixture/);
  assert.equal((await f.metrics()).instanceId, f.instanceId);
});

test('provider failure is attributed without fabricating token usage or actual model', async t => {
  const f = await setup(t, async url => url.includes('api.anthropic.com') ? Response.json(answer(body.model)) : new Response('fail', { status: 500 }));
  await (await f.send(body)).text();
  const [row] = await waitRows(f.events, 1);
  assert.equal(row.provider, 'local'); assert.equal(row.status, 'failed');
  assert.equal(row.usage_measured, false); assert.equal(row.input_tokens, null); assert.equal(row.output_tokens, null);
  assert.equal(row.reported_model, null);
});

test('unroutable input is unknown rather than falsely attributed to a provider', async t => {
  const f = await setup(t, async () => { throw new Error('must not fetch'); });
  assert.equal((await f.send({ ...body, model: 'fixture-blocked' })).status, 403);
  const [row] = await waitRows(f.events, 1);
  assert.equal(row.provider, 'unknown'); assert.equal(row.status, 'failed');
  assert.equal(row.usage_measured, false);
});

test('two simultaneous proxy instances keep separate usage files and IDs', async t => {
  const a = await setup(t, async () => Response.json(answer()));
  const b = await setup(t, async () => Response.json(answer()));
  await Promise.all([(await a.send(body)).text(), (await b.send(body)).text()]);
  const [ar] = await waitRows(a.events, 1), [br] = await waitRows(b.events, 1);
  assert.notEqual(ar.instance_id, br.instance_id);
  assert.equal(ar.instance_id, a.instanceId); assert.equal(br.instance_id, b.instanceId);
  assert.equal(ar.event_seq, 1); assert.equal(br.event_seq, 1);
});

test('client cancellation records exactly one unmeasured event', async t => {
  let started;
  const entered = new Promise(resolve => { started = resolve; });
  const f = await setup(t, async (_url, opts) => {
    started();
    return new Promise((_, reject) => opts.signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true }));
  });
  const abort = new AbortController();
  const pending = f.send(body, abort.signal).catch(() => null);
  await entered; abort.abort(); await pending;
  const rows = await waitRows(f.events, 1);
  assert.equal(rows[0].status, 'aborted'); assert.equal(rows[0].usage_measured, false);
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal((await waitRows(f.events, 1)).length, 1);
});


test('partial verified usage retains the available side', async t => {
  const f = await setup(t, async () => Response.json({ ...answer(), usage: { input_tokens: 8 } }), { localOnly: true });
  assert.equal((await f.send(body)).status, 200);
  const [row] = await waitRows(f.events, 1);
  assert.equal(row.reported_model, model);
  assert.equal(row.usage_measured, true);
  assert.equal(row.input_tokens, 8);
  assert.equal(row.output_tokens, null);
});

test('validated streams record reported usage once without inventing missing output', async t => {
  const frame = (type, fields) => `event: ${type}\ndata: ${JSON.stringify({type,...fields})}\n\n`;
  for (const [passthrough, outputs, expected] of [[false,[7],7],[true,[0],0],[true,[undefined],null],[true,[3,7],7],[true,[3,undefined],null]]) {
    const frames = [frame('message_start',{message:{id:'msg_sse',role:'assistant',model,usage:{input_tokens:5,output_tokens:0}}}),
      frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
      frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'PRIVATE-REPLY'}}),
      frame('content_block_stop',{index:0}),
      ...outputs.map(value=>frame('message_delta',{delta:{stop_reason:'end_turn'},...(value === undefined ? {} : {usage:{output_tokens:value}})})),
      frame('message_stop',{})].join('');
    const f = await setup(t, async()=>new Response(frames,{headers:{'content-type':'text/event-stream'}}),
      {provider:'openrouter',key:'sk-or-fixture',openrouterModel:model,streamPassthrough:passthrough});
    const response = await f.send({...body,stream:true});
    assert.equal(response.status,200); await response.text();
    const rows=await waitRows(f.events,1);
    assert.equal(rows.length,1);assert.equal(rows[0].status,'ok');
    assert.equal(rows[0].provider,'openrouter');assert.equal(rows[0].input_tokens,5);
    assert.equal(rows[0].output_tokens,expected);assert.equal(rows[0].usage_measured,true);
    assert.doesNotMatch(JSON.stringify(rows),/PRIVATE|sk-ant|nonce-fixture/);
  }
});

test('fractional streamed usage marks the response failed without recording partial usage', async t => {
  const frame = (type, fields) => `event: ${type}\ndata: ${JSON.stringify({type,...fields})}\n\n`;
  const frames = [frame('message_start',{message:{id:'msg_sse',role:'assistant',model,usage:{input_tokens:5,output_tokens:0}}}),
    frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
    frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'PRIVATE-REPLY'}}),
    frame('content_block_stop',{index:0}),
    frame('message_delta',{delta:{stop_reason:'end_turn'},usage:{output_tokens:2.5}}),frame('message_stop',{})].join('');
  const f = await setup(t, async()=>new Response(frames,{headers:{'content-type':'text/event-stream'}}),
    {provider:'openrouter',key:'sk-or-fixture',openrouterModel:model,streamPassthrough:true});
  const response = await f.send({...body,stream:true});
  assert.equal(response.status, 200);
  assert.match(await response.text(), /upstream stream failed/);
  const [row] = await waitRows(f.events,1);
  assert.equal(row.status,'failed');
  assert.equal(row.usage_measured,false);
  assert.equal(row.input_tokens,null);
  assert.equal(row.output_tokens,null);
});

test('truncated streamed replies do not record partial usage', async t => {
  const frame=(type, fields)=>`event: ${type}\ndata: ${JSON.stringify({type,...fields})}\n\n`;
  const frames=[frame('message_start',{message:{id:'msg_sse',role:'assistant',model,usage:{input_tokens:5,output_tokens:0}}}),
    frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
    frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'PRIVATE-REPLY'}})].join('');
  const f=await setup(t,async()=>new Response(frames,{headers:{'content-type':'text/event-stream'}}),
    {provider:'openrouter',key:'sk-or-fixture',openrouterModel:model,streamPassthrough:true});
  const response=await f.send({...body,stream:true});await response.text();
  const [row]=await waitRows(f.events,1);
  assert.equal(row.status,'failed');assert.equal(row.usage_measured,false);
  assert.equal(row.input_tokens,null);assert.equal(row.output_tokens,null);
});

test('missing or mismatched provider model is never reported as the configured model', async t => {
  for (const returnedModel of [undefined, 'different-model']) {
    const f = await setup(t, async () => Response.json({ ...answer(), model: returnedModel }), { localOnly: true });
    assert.notEqual((await f.send(body)).status, 200);
    const [row] = await waitRows(f.events, 1);
    assert.equal(row.reported_model, null);
    assert.equal(row.status, 'failed');
  }
});

test('credential-shaped model identifiers never reach usage files or metrics', async t => {
  for (const name of ['gsk_' + 'x'.repeat(32), 'provider/AIza' + 'y'.repeat(35)]) {
    const f = await setup(t, async () => Response.json(answer(name)), {localOnly:true,localModel:name});
    assert.equal((await f.send(body)).status,200);
    const [row]=await waitRows(f.events,1);
    assert.equal(row.requested_model,null);assert.equal(row.reported_model,null);
    assert.doesNotMatch(JSON.stringify(row),new RegExp(name));
    assert.equal((await f.metrics()).configuration.model,null);
  }
});

const cacheTrace = [
  // System, tool revision, virtual time, expected cache read and checkpoint.
  ['A', 'v1', 0, 0, 'cold start'],
  ['A', 'v1', 1, 32, 'repeated prefix'],
  ['A', 'v2', 2, 0, 'changed tool'],
  ['B', 'v2', 3, 0, 'changed system'],
  ['B', 'v2', 4, 32, 'inside TTL'],
  ['B', 'v2', 14, 0, 'expired prefix'],
  ['B', 'v2', 15, 0, 'after reset'],
];

const cacheFaultCases = [
  ['healthy'],
  ['dirty start', 'cold start: unexpected read'],
  ['disabled reads', 'repeated prefix: missed read', 'inside TTL: missed read'],
  ['tool omitted from key', 'changed tool: unexpected read'],
  ['system omitted from key', 'changed system: unexpected read'],
  ['expiry ignored', 'expired prefix: unexpected read'],
  ['reset ignored', 'after reset: unexpected read'],
  ['previous answer replayed', 'repeated prefix: stale answer'],
  ['double-counted input', 'repeated prefix: token total'],
  ['read not reported', 'repeated prefix: missing read'],
  ['negative read', 'repeated prefix: invalid read'],
];

async function runCacheFault(t, fault) {
  const cache = new Map();
  let sent = 0, previousAnswer;
  const proxy = await setup(t, async (_url, {body: raw}) => {
    const req = JSON.parse(raw);
    const [, , now] = cacheTrace[sent];
    assert.deepEqual(req.cache_control, {type: 'ephemeral'});
    const key = JSON.stringify([req.model, fault === 'tool omitted from key' ? null : req.tools,
      fault === 'system omitted from key' ? null : req.system]);
    if (sent === 0 && fault === 'dirty start') cache.set(key, now + 10);
    if (sent === 6 && fault !== 'reset ignored') cache.clear();
    const hit = fault !== 'disabled reads' && cache.has(key) && (fault === 'expiry ignored' || now < cache.get(key));
    cache.set(key, now + 10);

    const reply = answer();
    const currentAnswer = 'result ' + req.messages.at(-1).content;
    reply.content[0].text = sent === 1 && fault === 'previous answer replayed' ? previousAnswer : currentAnswer;
    previousAnswer = currentAnswer;
    // The fixture's prefix costs 32 tokens and its uncached suffix costs 8 on every turn.
    reply.usage = {input_tokens: 8, output_tokens: 2, cache_read_input_tokens: hit ? 32 : 0,
      cache_creation_input_tokens: hit ? 0 : 32};
    if (sent === 1) {
      if (fault === 'double-counted input') reply.usage.input_tokens += 32;
      if (fault === 'read not reported') delete reply.usage.cache_read_input_tokens;
      if (fault === 'negative read') reply.usage.cache_read_input_tokens = -1;
    }
    sent++;
    return Response.json(reply);
  }, {localOnly: true, localPromptCache: true});

  const findings = [], clientReads = [];
  for (const [index, [system, revision, , expectedRead, checkpoint]] of cacheTrace.entries()) {
    const content = 'question ' + index;
    const tracker = '<total_tokens>' + (8000 - index) + ' tokens left</total_tokens>';
    const r = await proxy.send({...body, system: system + '\n' + tracker,
      tools: [{name: 'Read', description: revision, input_schema: {type: 'object'}}],
      messages: [{role: 'user', content}]});
    if (fault === 'negative read' && index === 1) {
      assert.equal(r.status, 502);
      assert.match(await r.text(), /invalid_usage/);
      findings.push(checkpoint + ': invalid read');
      clientReads.push(null);
      continue;
    }
    assert.equal(r.status, 200, checkpoint);
    const reply = await r.json(), usage = reply.usage;
    const read = usage.cache_read_input_tokens;
    const validRead = Number.isSafeInteger(read) && read >= 0;
    clientReads.push(validRead ? read : null);
    if (reply.content[0].text !== 'result ' + content) findings.push(checkpoint + ': stale answer');
    if (read === undefined) findings.push(checkpoint + ': missing read');
    else if (!validRead) findings.push(checkpoint + ': invalid read');
    else {
      if (read !== expectedRead) findings.push(checkpoint + (expectedRead ? ': missed read' : ': unexpected read'));
      if (usage.input_tokens + read + usage.cache_creation_input_tokens !== 40) findings.push(checkpoint + ': token total');
    }
  }
  assert.equal(sent, cacheTrace.length);
  const rows = await waitRows(proxy.events, sent);
  assert.deepEqual(rows.map(row => row.cache_read_tokens ?? null), clientReads,
    'persist valid reads once and keep missing or invalid measurements unknown');
  return findings;
}

test('differential prompt-cache fault simulations', async t => {
  for (const [fault, ...expected] of cacheFaultCases) {
    await t.test(fault, async t => {
      const findings = await runCacheFault(t, fault);
      assert.deepEqual(findings, expected);
    });
  }
});

test('invalid provider cache counters fail before publication and are never billed twice by retries', async t => {
  const invalid = [
    {cache_read_input_tokens: -1}, {cache_creation_input_tokens: '5'},
    {cache_read_input_tokens: false}, {cache_creation_input_tokens: 1.5},
    {cache_read_input_tokens: 2 ** 31}, {cache_read_input_tokens: 40, prompt_tokens: 44},
    {cache_read_input_tokens: 40, prompt_tokens_details: {cached_tokens: 23}},
    {cache_read_input_tokens: 40, prompt_tokens_details: []},
    {cache_creation_input_tokens: 0, prompt_tokens_details: {cached_tokens: true}},
    {cache_creation_input_tokens: 0, prompt_tokens_details: {cached_tokens: 40}, prompt_tokens: 44},
    {cache_creation_input_tokens: 5, cache_creation: {ephemeral_5m_input_tokens: 3, ephemeral_1h_input_tokens: 3}},
    {input_tokens: undefined, cache_read_input_tokens: 50, prompt_tokens: 49},
    {cache_read_input_tokens: 40, cache_creation_input_tokens: 0, prompt_tokens: 46},
  ];
  for (const provider of ['local', 'openrouter']) {
    let calls = 0, sample;
    const f = await setup(t, async () => {
      calls++;
      const reply = answer();
      Object.assign(reply.usage, sample);
      return Response.json(reply);
    }, {provider, key: 'fixture', openrouterModel: model});
    for (const fields of invalid) {
      sample = fields;
      const r = await f.send(body);
      assert.equal(r.status, 502, JSON.stringify(fields));
      assert.match(await r.text(), /invalid_usage/);
    }
    assert.equal(calls, invalid.length);
    const rows = await waitRows(f.events, invalid.length);
    assert.ok(rows.every(row => row.status === 'failed' && !row.usage_measured && row.cache_read_tokens == null));
    assert.equal((await f.metrics()).efficiency.cache_read, 0);
  }
});

test('nullable and missing cache counters stay unknown and explicitly measured zero is preserved', async t => {
  let counts;
  const f = await setup(t, async () => {
    const reply = answer();
    Object.assign(reply.usage, counts);
    return Response.json(reply);
  });
  const cases = [{}, {cache_read_input_tokens: null, cache_creation_input_tokens: null},
    {cache_read_input_tokens: 0}, {cache_read_input_tokens: 40, prompt_tokens: 45},
    {cache_read_input_tokens: 40, cache_creation_input_tokens: 3, prompt_tokens: 48,
      prompt_tokens_details: {cached_tokens: 40}, cache_creation: {ephemeral_5m_input_tokens: 1, ephemeral_1h_input_tokens: 2}}];
  for (const fields of cases) {
    counts = fields;
    const r = await f.send(body);
    assert.equal(r.status, 200);
    const {usage} = await r.json();
    for (const key of ['cache_read_input_tokens', 'cache_creation_input_tokens']) assert.equal(usage[key], fields[key]);
  }
  const rows = await waitRows(f.events, cases.length);
  assert.deepEqual(rows.map(row => row.cache_read_tokens), [null, null, 0, 40, 40]);
});

test('bad cache usage is stopped in buffered and passthrough SSE at start or delta', async t => {
  const frame = (type, values) => 'event: ' + type + '\ndata: ' + JSON.stringify({type, ...values}) + '\n\n';
  for (const passthrough of [false, true]) {
    let payload;
    const f = await setup(t, async () => new Response(payload, {headers: {'content-type': 'text/event-stream'}}),
      {provider: 'openrouter', key: 'fixture', openrouterModel: model, streamPassthrough: passthrough});
    for (const [index, [start, delta]] of [
      [{cache_read_input_tokens: -2}, {}],
      [{}, {cache_creation_input_tokens: -2}],
      [{cache_read_input_tokens: 40, prompt_tokens: 45}, {cache_read_input_tokens: 57}],
    ].entries()) {
      payload = [frame('message_start', {message: {id: 'fixture', role: 'assistant', model, usage: {input_tokens: 5, output_tokens: 0, ...start}}}),
        frame('content_block_start', {index: 0, content_block: {type: 'text', text: ''}}),
        frame('content_block_delta', {index: 0, delta: {type: 'text_delta', text: 'fixture'}}),
        frame('content_block_stop', {index: 0}),
        frame('message_delta', {delta: {stop_reason: 'end_turn'}, usage: {output_tokens: 2, ...delta}}),
        frame('message_stop', {})].join('');
      const r = await f.send({...body, stream: true});
      const wire = await r.text();
      assert.equal(r.status, passthrough && index > 0 ? 200 : 502);
      assert.match(wire, /invalid_usage/);
      assert.doesNotMatch(wire, /"cache_(?:read|creation)_input_tokens":(?:-2|57)|event: message_stop/);
    }
    const rows = await waitRows(f.events, 3);
    assert.ok(rows.every(row => row.status === 'failed' && row.cache_read_tokens == null));
  }
});
