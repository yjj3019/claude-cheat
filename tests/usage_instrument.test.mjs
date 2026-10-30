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
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(dir, { recursive: true, force: true }); });
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

test('Plan fallback records its final route without fabricating token usage or actual model', async t => {
  const f = await setup(t, async url => url.includes('api.anthropic.com') ? Response.json(answer(body.model)) : new Response('fail', { status: 500 }));
  await (await f.send(body)).text();
  const [row] = await waitRows(f.events, 1);
  assert.equal(row.provider, 'plan'); assert.equal(row.status, 'ok');
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
  for (const [passthrough, outputs, expected] of [[false,[7],7],[true,[0],0],[true,[undefined],null],[true,[2.5],null],[true,[3,7],7],[true,[3,undefined],null]]) {
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
