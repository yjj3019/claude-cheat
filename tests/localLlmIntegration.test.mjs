// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import test from 'node:test';
import { listenSafe } from './listenSafe.mjs';
import { createProxy } from '../claudeRouteProxy.mjs';

// Synthetic credentials only; local transport is real HTTP and Pro never leaves this process.
const SONNET = 'claude-sonnet-4-6';
const MODEL = 'loopback-fixture';
const OAUTH = 'Bearer sk-ant-oat-loopback-fixture';
const body = extra => ({ model: SONNET, max_tokens: 64,
  messages: [{ role: 'user', content: '한글 fixture' }], ...extra });
const message = (model = MODEL, content = [{ type: 'text', text: 'local fixture answer' }]) => ({ type: 'message', model, content, stop_reason: 'end_turn' });
const frame = (type, extra = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...extra })}\n\n`;
const json = (res, value) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };

async function listen(t, server) {
  t.after(async () => {
    if (!server.listening) return;
    const closed = new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    server.closeAllConnections();
    await closed;
  });
  await listenSafe(server);
  return `http://127.0.0.1:${server.address().port}`;
}

async function fixture(t, handler, config = {}) {
  const localCalls = [], proCalls = [], attempts = [];
  const local = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const request = { method: req.method, path: req.url, headers: req.headers,
        body: JSON.parse(Buffer.concat(chunks).toString('utf8')) };
      localCalls.push(request);
      await handler(req, res, request);
    } catch (error) { res.destroy(error); }
  });
  const localUrl = await listen(t, local) + '/v1/messages';
  const proxy = createProxy({ provider: 'local', localUrl, localModel: MODEL,
    nonce: 'loopback-nonce', retryDelayMs: 0, attemptTimeoutMs: 500, ...config,
    fetchImpl: (url, options) => {
      if (url === 'https://api.anthropic.com/v1/messages') {
        proCalls.push({ ...options, body: JSON.parse(options.body) });
        return Promise.resolve(new Response(JSON.stringify(message(SONNET)), {
          headers: { 'content-type': 'application/json' },
        }));
      }
      assert.equal(url, localUrl, 'only the owned loopback upstream can receive a network request');
      attempts.push(url);
      return fetch(url, options);
    },
  });
  const proxyUrl = await listen(t, proxy) + '/v1/messages';
  const request = (extra, signal, beta = 'oauth-2025-04-20', sessionId) => fetch(proxyUrl, { method: 'POST', signal,
    headers: { 'content-type': 'application/json', 'x-local-route-token': 'loopback-nonce',
      authorization: OAUTH, 'anthropic-beta': beta, ...(sessionId ? {'x-claude-code-session-id':sessionId} : {}) }, body: JSON.stringify(body(extra)) });
  return { local, localUrl, proxyUrl, localCalls, proCalls, attempts, request };
}

test('opt-in beta diagnostics expose only fixed metadata and do not alter routing', async t => {
  const old = process.env.ROUTE_BETA_DIAGNOSTICS;
  process.env.ROUTE_BETA_DIAGNOSTICS = '1';
  t.after(() => { if (old === undefined) delete process.env.ROUTE_BETA_DIAGNOSTICS; else process.env.ROUTE_BETA_DIAGNOSTICS = old; });
  const log=t.mock.method(console,'error',()=>{});
  const f=await fixture(t,()=>{ throw new Error('unexpected local request'); });
  const extra={tools:[{name:'PRIVATE_TOOL',description:'PRIVATE_DESCRIPTION',defer_loading:true,input_schema:{type:'object'}}],
    messages:[{role:'user',content:[{type:'text',text:'PRIVATE_PROMPT'}]}]};
  const beta='oauth-2025-04-20,advisor-tool-2026-03-01,private-beta-2026-11-17';
  assert.equal((await f.request(extra,undefined,beta)).status,200);
  const entry=log.mock.calls.map(call=>JSON.parse(call.arguments[0])).find(item=>item.event==='local_beta_diagnostic');
  assert.ok(entry);assert.deepEqual(entry.known_betas,['advisor-tool-2026-03-01']);
  assert.equal(entry.unknown_beta_count,1);assert.equal(entry.tool_count,1);assert.equal(entry.deferred_count,1);
  assert.equal(f.proCalls.length,1);assert.deepEqual(f.proCalls[0].body,body(extra));
  for (const secret of ['PRIVATE_TOOL','PRIVATE_DESCRIPTION','PRIVATE_PROMPT','private-beta-2026-11-17',OAUTH])
    assert.equal(JSON.stringify(entry).includes(secret),false);
});

test('opt-in Auto correlation records only bounded request metadata and keeps routing', async t => {
  const old = process.env.ROUTE_AUTO_DIAGNOSTICS;
  process.env.ROUTE_AUTO_DIAGNOSTICS = '1';
  t.after(() => { if (old === undefined) delete process.env.ROUTE_AUTO_DIAGNOSTICS; else process.env.ROUTE_AUTO_DIAGNOSTICS = old; });
  const log = t.mock.method(console, 'error', () => {});
  const f = await fixture(t, (_req, res) => json(res, message()));
  const response = await f.request({messages:[{role:'user',content:'PRIVATE_PROMPT'}],
    tools:[{name:'PRIVATE_TOOL',input_schema:{type:'object'}}]});
  assert.equal(response.status, 200);
  await response.text();
  const entry = log.mock.calls.map(call => JSON.parse(call.arguments[0])).find(item => item.event === 'auto_request_diagnostic');
  assert.ok(entry);
  assert.equal(entry.max_tokens, 64);
  assert.equal(entry.message_count, 1);
  assert.equal(entry.tool_count, 1);
  assert.match(entry.at, /^\d{4}-\d\d-\d\dT/);
  assert.equal(f.proCalls.length, 0);
  for (const secret of ['PRIVATE_PROMPT', 'PRIVATE_TOOL', OAUTH]) assert.equal(JSON.stringify(entry).includes(secret), false);
});

for (const validJson of [true, false]) {
  test(`Local SSE model mismatch retries JSON with exact model validation (${validJson})`, async t => {
    const f = await fixture(t, (_req,res,request) => {
      if (request.body.stream) {
        res.writeHead(200, {'content-type':'text/event-stream'});
        res.end(frame('message_start',{message:{model:'different-model'}}));
      } else json(res,{...message(validJson ? MODEL : 'different-model'),id:'fixture-id',role:'assistant',usage:{input_tokens:1,output_tokens:1}});
    });
    const response=await f.request({stream:true});
    assert.equal(response.status,validJson ? 200 : 503);
    const output=await response.text();
    if (validJson) assert.match(output,/local fixture answer/);
    assert.equal(f.localCalls[0].body.stream,true);
    assert.ok(f.localCalls.slice(1).every(call=>call.body.stream===false));
    assert.equal(f.proCalls.length,0);
  });
}

test('Local context errors expose a fixed diagnosis without leaking provider text', async t => {
  const f=await fixture(t,(_req,res)=>{
    res.writeHead(400,{'content-type':'application/json'});
    res.end(JSON.stringify({error:{message:'maximum context length exceeded, too many tokens PRIVATE_MARKER'}}));
  });
  const response=await f.request();
  const text=await response.text();
  assert.equal(response.status,400);
  assert.match(text,/local_context_limit/);
  assert.doesNotMatch(text,/Send a new request to check Local recovery/);
  assert.doesNotMatch(text,/PRIVATE_MARKER/);
  assert.equal(f.localCalls.length,1);
  assert.equal(f.proCalls.length,0);
});

test('ordinary Local preserves a valid tool result with omitted content', async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  const messages = [{role:'user', content:'Use the tool.'},
    {role:'assistant', content:[{type:'tool_use', id:'tool-1', name:'fixture', input:{}}]},
    {role:'user', content:[{type:'tool_result', tool_use_id:'tool-1', is_error:false}]}];
  const response = await f.request({messages, tools:[{name:'fixture', input_schema:{type:'object'}}]});
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(f.proCalls.length, 0);
  assert.deepEqual(f.localCalls[0].body.messages, messages);
});

test('ordinary Local returns after a Plan request when tool-change beta is unused', async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  const beta = 'oauth-2025-04-20,inline-tools-2026-09-15';
  // Unknown request content still needs Plan even with an otherwise removable header.
  const native = { messages: [{ role: 'user', content: [{ type: 'unknown_fixture', value: 'x' }] }] };
  assert.equal((await f.request(native, undefined, beta)).status, 200);
  assert.equal(f.proCalls.length, 1);
  assert.equal(f.localCalls.length, 0);
  const response = await f.request({}, undefined, beta);
  assert.equal(response.status, 200);
  await response.json();
  assert.equal(f.localCalls.length, 1);
  assert.equal(f.proCalls.length, 1);
  assert.equal(f.localCalls[0].headers['anthropic-beta'], undefined);
  // Unknown betas must not become eligible through the new exception.
  assert.equal((await f.request({}, undefined, beta + ',future-feature-2026-10-21')).status, 200);
  assert.equal(f.proCalls.length, 2);
  const removal = { messages: [{ role: 'system', content: [{ type: 'tool_removal', tool: { type: 'tool_reference', name: 'Read' } }] }] };
  assert.equal((await f.request(removal, undefined, beta)).status, 200);
  assert.equal(f.proCalls.length, 3);
  assert.deepEqual(f.proCalls[2].body.messages, removal.messages);
  const incomplete = {messages:[{role:'assistant',content:[{type:'tool_use',id:'a',name:'Read',input:{}}]},
    {role:'user',content:'continue'}]};
  assert.equal((await f.request(incomplete,undefined,beta)).status,200);
  assert.equal(f.proCalls.length,4);
  assert.deepEqual(f.proCalls[3].body.messages,incomplete.messages);
});

for (const localKey of [undefined, 'synthetic-local-key']) {
  test(`real local HTTP preserves request and isolates OAuth (local key ${Boolean(localKey)})`, { timeout: 5000 }, async t => {
    const content = [{ type: 'tool_use', id: 'fixture-tool', name: 'Read', input: { path: '한글.txt', nested: { safe: true } } }];
    const f = await fixture(t, (_req, res) => json(res, { ...message(MODEL, content), stop_reason: 'tool_use' }), { localKey });
    const tools = [{ name: 'Read', input_schema: { type: 'object', properties: { path: { type: 'string' } } } }];
    const response = await f.request({ tools });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ...message(SONNET, content), stop_reason: 'tool_use' });
    assert.equal(f.localCalls.length, 1);
    const received = f.localCalls[0];
    assert.equal(received.method, 'POST');
    assert.equal(received.path, '/v1/messages');
    const { system, ...receivedBody } = received.body;
    assert.match(system, /Host tool access:/);
    assert.deepEqual(receivedBody, { ...body({ tools }), thinking: { type: 'disabled' }, model: MODEL });
    assert.equal(received.headers.authorization, localKey ? `Bearer ${localKey}` : undefined);
    for (const name of ['anthropic-beta', 'x-api-key', 'x-local-route-token']) assert.equal(received.headers[name], undefined);
    assert.equal(JSON.stringify(received).includes(OAUTH), false);
    assert.equal(f.proCalls.length, 0);
  });
}

for (const format of ['json', 'sse']) {
  test(`empty local HTTP ${format} retries, reports failure, and recovers after cooldown`, { timeout: 5000 }, async t => {
    let time = 1000, healthy = false;
    const f = await fixture(t, (_req, res) => {
      if (healthy) {
        if (format === 'json') { json(res, message()); return; }
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end(frame('message_start', { message: { model: MODEL } }) +
          frame('content_block_start', { index: 0, content_block: { type: 'text', text: 'local fixture answer' } }) +
          frame('content_block_stop', { index: 0 }) +
          frame('message_delta', { delta: { stop_reason: 'end_turn' } }) + frame('message_stop'));
        return;
      }
      if (format === 'json') { json(res, message(MODEL, [])); return; }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.end(frame('message_start', { message: { model: MODEL } }) +
        frame('message_delta', { delta: { stop_reason: 'end_turn' } }) + frame('message_stop'));
    }, { now: () => time, cooldownMs: 30 });
    const extra = { stream: format === 'sse' };
    assert.equal((await f.request(extra)).status, 503);
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 0);
    healthy = true;
    assert.equal((await f.request(extra)).status, 503);
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 0);
    time += 31;
    for (let i = 0; i < 2; i++) {
      const response = await f.request(extra);
      if (format === 'json') assert.deepEqual(await response.json(), message(SONNET));
      else {
        const output = await response.text();
        assert.match(output, /local fixture answer/);
        assert.match(output, new RegExp(SONNET));
      }
    }
    assert.equal(f.localCalls.length, 6);
    assert.equal(f.proCalls.length, 0);
  });
}

test('real chunked SSE preserves split tool JSON and restores the client model', { timeout: 5000 }, async t => {
  const partials = ['{"path":', '"한글.txt","nested":{"safe":true}}'];
  const events = frame('message_start', { message: { model: MODEL } }) +
    frame('content_block_start', { index: 0, content_block: { type: 'tool_use', id: 'fixture-tool', name: 'Read', input: {} } }) +
    partials.map(partial_json => frame('content_block_delta', { index: 0, delta: { type: 'input_json_delta', partial_json } })).join('') +
    frame('content_block_stop', { index: 0 }) + frame('message_delta', { delta: { stop_reason: 'tool_use' } }) + frame('message_stop');
  const bytes = Buffer.from(events);
  const f = await fixture(t, async (_req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    // Splitting into tiny writes exercises event and UTF-8 boundaries over the HTTP reader.
    for (let offset = 0; offset < bytes.length; offset += 7) {
      res.write(bytes.subarray(offset, offset + 7));
      await new Promise(resolve => setImmediate(resolve));
    }
    res.end();
  });
  const response = await f.request({ stream: true, tools: [{ name: 'Read', input_schema: { type: 'object' } }] });
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/event-stream/);
  const text = await response.text();
  assert.equal(text, events.replace(MODEL, SONNET));
  const deltas = text.split('\n').filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)))
    .filter(event => event.type === 'content_block_delta');
  assert.deepEqual(JSON.parse(deltas.map(event => event.delta.partial_json).join('')), { path: '한글.txt', nested: { safe: true } });
  assert.equal(f.localCalls.length, 1);
  assert.equal(f.proCalls.length, 0);
});

test('connection refusal makes four local attempts without Plan fallback', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  await new Promise(resolve => f.local.close(resolve));
  const response = await f.request();
  assert.equal(response.status, 503);
  assert.match(await response.text(), /local_upstream_error/);
  assert.equal(f.attempts.length, 4);
  assert.equal(f.localCalls.length, 0);
  assert.equal(f.proCalls.length, 0);
});

test('restored local HTTP gets one half-open probe and subsequent requests recover locally', { timeout: 5000 }, async t => {
  let healthy = false, time = 1000, entered, release;
  const probeEntered = new Promise(resolve => { entered = resolve; });
  const probeReleased = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, async (_req, res) => {
    if (!healthy) { res.writeHead(503); res.end('unavailable'); return; }
    entered();
    await probeReleased;
    json(res, message());
  }, { now: () => time, cooldownMs: 100, attemptTimeoutMs: 2000 });
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 4);
  assert.equal(f.proCalls.length, 0);
  healthy = true;
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 4);
  assert.equal(f.proCalls.length, 0);
  time += 101;
  const probe = f.request();
  await probeEntered;
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 5, 'only one half-open local request');
  assert.equal(f.proCalls.length, 0);
  release();
  const recovered = await probe;
  assert.equal(recovered.status, 200);
  await recovered.text();
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 6);
  assert.equal(f.proCalls.length, 0);
});

for (const format of ['json', 'sse']) {
  test(`malformed local ${format} tool output never reaches the HTTP client`, { timeout: 5000 }, async t => {
    const f = await fixture(t, (_req, res) => {
      res.writeHead(200, { 'content-type': format === 'json' ? 'application/json' : 'text/event-stream' });
      res.end(format === 'json' ? '{"content":[{"name":"UNSAFE_TOOL"}]' :
        frame('message_start', { message: { model: MODEL } }) +
        frame('content_block_start', { index: 0, content_block: { type: 'tool_use', id: 'bad', name: 'UNSAFE_TOOL', input: {} } }));
    });
    const response = await f.request({ stream: format === 'sse', tools: [{ name: 'UNSAFE_TOOL', input_schema: { type: 'object' } }] });
    assert.equal(response.status, 503);
    const text = await response.text();
    assert.doesNotMatch(text, /UNSAFE_TOOL/);
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 0);
  });
}

test('real HTTP stalled response times out four times without Plan fallback', { timeout: 5000 }, async t => {
  const closed = [];
  const f = await fixture(t, (_req, res) => {
    closed.push(once(res, 'close'));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.write('{');
  }, { attemptTimeoutMs: 100 });
  const response = await f.request();
  assert.equal(response.status, 503);
  await response.text();
  assert.equal(f.localCalls.length, 4);
  assert.equal(f.proCalls.length, 0);
  await Promise.all(closed);
});

test('Local-only body timeout closes HTTP, blocks cooldown and concurrent probes, then preserves tool round trips without Plan', { timeout: 5000 }, async t => {
  let time = 1000, closed, entered, release, active = false, ticks = 0, work;
  const disconnected = new Promise(resolve => { closed = resolve; });
  const probeEntered = new Promise(resolve => { entered = resolve; });
  const probeReleased = new Promise(resolve => { release = resolve; });
  t.after(() => { clearInterval(work); release(); });
  const tool = { type: 'tool_use', id: 'local-recovery-read', name: 'Read', input: { path: 'fixture.txt' } };
  const f = await fixture(t, async (_req, res) => {
    if (f.localCalls.length === 1) {
      active = true;
      work = setInterval(() => { ticks++; }, 5);
      res.once('close', () => { clearInterval(work); active = false; closed(); });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.write('{"content":[{"type":"text","text":"PARTIAL_MUST_NOT_ESCAPE');
      return;
    }
    if (f.localCalls.length === 2) {
      entered();
      await probeReleased;
      json(res, { ...message(MODEL, [tool]), stop_reason: 'tool_use' });
      return;
    }
    json(res, { ...message(), id: 'fixture-recovery', role: 'assistant', usage: { input_tokens: 4, output_tokens: 3 } });
  }, { localOnly: true, now: () => time, cooldownMs: 30, attemptTimeoutMs: 500 });
  const failed = await f.request({ stream: true });
  assert.equal(failed.status, 400);
  const failureMessage = (await failed.json()).error.message;
  assert.match(failureMessage, /^\[local_timeout\].*Wait 1 second before a new request/);
  assert.doesNotMatch(failureMessage, /PARTIAL_MUST_NOT_ESCAPE/);
  await disconnected;
  assert.equal(active, false);
  const stoppedTicks = ticks;
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(ticks, stoppedTicks, 'fixture work stops when the response connection closes');
  assert.equal(f.localCalls.length, 1, 'Local-only performs no upstream retry');
  assert.equal(f.localCalls[0].body.stream, false);
  const cooldown = await f.request();
  assert.equal(cooldown.status, 400);
  assert.match((await cooldown.json()).error.message, /^\[local_cooldown\].*Wait 1 second/);
  assert.equal(f.localCalls.length, 1, 'cooldown must not reach the upstream');
  time += 31;
  const recovery = f.request({ tools: [{ name: 'Read', input_schema: { type: 'object', properties: { path: { type: 'string' } } } }] });
  await probeEntered;
  const concurrent = await f.request();
  assert.equal(concurrent.status, 400);
  const concurrentMessage = (await concurrent.json()).error.message;
  assert.match(concurrentMessage, /^\[local_recovery_in_progress\].*Wait for it to finish/);
  assert.doesNotMatch(concurrentMessage, /Wait 0|Send a new request to check/);
  assert.equal(f.localCalls.length, 2, 'only one recovery probe may reach Local');
  release();
  const recovered = await recovery;
  assert.equal(recovered.status, 200);
  assert.deepEqual(await recovered.json(), { ...message(SONNET, [tool]), stop_reason: 'tool_use' });
  const messages = [...body().messages, { role: 'assistant', content: [tool] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: tool.id, content: 'SYNTHETIC_FILE_CONTENT' }] }];
  const next = await f.request({ stream: true, messages });
  assert.equal(next.status, 200);
  assert.match(next.headers.get('content-type'), /text\/event-stream/);
  assert.match(await next.text(), /local fixture answer/);
  assert.deepEqual(f.localCalls[2].body.messages, messages);
  assert.equal(f.localCalls.length, 3);
  assert.equal(f.proCalls.length, 0, 'no failure, cooldown, probe or tool turn may invoke Plan');
});

test('Local-only preserves plain system text between user and tool turns', { timeout: 5000 }, async t => {
  const messages = [
    { role: 'user', content: '한글 자료/검색 결과.txt를 확인하세요.' },
    { role: 'system', content: 'Keep the existing permission checks and use only the offered tools.' },
    { role: 'assistant', content: [{ type: 'tool_use', id: 'fixture-search', name: 'Grep', input: { path: '한글 자료/검색 결과.txt', pattern: '검색표식=' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'fixture-search', content: '검색표식=fixture' }] },
  ];
  const f = await fixture(t, (_req, res) => json(res, message()), { localOnly: true });
  const response = await f.request({ messages });
  assert.equal(response.status, 200);
  await response.text();
  assert.deepEqual(f.localCalls[0].body.messages, messages, 'do not strip, relocate, or downgrade system messages');
  assert.equal(f.proCalls.length, 0);
});

test('text-only system context turns are forwarded without rewriting', { timeout: 5000 }, async t => {
  for (const localOnly of [false, true]) {
    const messages = [{ role: 'user', content: 'fixture' },
      { role: 'system', content: [{ type: 'text', text: 'fixture tool guidance' }] }];
    const f = await fixture(t, (_req, res) => json(res, message()), { localOnly });
    const response = await f.request({ messages });
    assert.equal(response.status, 200);
    await response.text();
    assert.equal(f.localCalls.length, 1);
    assert.deepEqual(f.localCalls[0].body.messages, messages);
    assert.equal(f.proCalls.length, 0);
  }
});

for (const [label, system] of [
  ['tool blocks', { role: 'system', content: [{ type: 'tool_use', id: 'fixture', name: 'Write', input: {} }] }],
  ['thinking blocks', { role: 'system', content: [{ type: 'thinking', thinking: 'fixture' }] }],
  ['extra fields', { role: 'system', content: 'fixture', metadata: {} }],
  ['blank text', { role: 'system', content: ' \n ' }],
  ['null content', { role: 'system', content: null }],
  ['other role', { role: 'developer', content: 'fixture' }],
]) {
  test(`Local-only rejects non-plain system messages: ${label}`, { timeout: 5000 }, async t => {
    const f = await fixture(t, (_req, res) => json(res, message()), { localOnly: true });
    const response = await f.request({ messages: [{ role: 'user', content: 'fixture' }, system] });
    assert.equal(response.status, 400);
    assert.match(await response.text(), /Plan fallback disabled/);
    assert.equal(f.localCalls.length, 0);
    assert.equal(f.proCalls.length, 0);
  });
}

test('ordinary routing forwards plain system messages to the replacement route', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  const messages = [{ role: 'user', content: 'fixture' }, { role: 'system', content: 'fixture' }];
  const response = await f.request({ messages });
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(f.localCalls.length, 1);
  assert.deepEqual(f.localCalls[0].body.messages, messages);
  assert.equal(f.proCalls.length, 0);
});

for (const localOnly of [false, true]) {
  test(`client cancellation closes the local HTTP request without retry or fallback (Local-only ${localOnly})`, { timeout: 5000 }, async t => {
    let entered, closed;
    const started = new Promise(resolve => { entered = resolve; });
    const disconnected = new Promise(resolve => { closed = resolve; });
    const f = await fixture(t, (_req, res) => {
      if (f.localCalls.length > 1) { json(res, message()); return; }
      res.once('close', closed);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.write('{');
      entered();
    }, { localOnly, attemptTimeoutMs: 2000 });
    const controller = new AbortController();
    const pending = f.request(undefined, controller.signal);
    const rejected = assert.rejects(pending, { name: 'AbortError' });
    await started;
    controller.abort();
    await rejected;
    await disconnected;
    assert.equal(f.attempts.length, 1);
    assert.equal(f.proCalls.length, 0);
    const next = await f.request();
    assert.equal(next.status, 200);
    await next.text();
    assert.equal(f.localCalls.length, 2);
    assert.equal(f.proCalls.length, 0);
  });
}

for (const stream of [false, true]) {
  test(`ordinary Local preserves deferred MCP tools and Plan fallback (stream ${stream})`, async t => {
    const f = await fixture(t, (_req, res) => json(res, { ...message(), id:'fixture-message', role:'assistant',
      usage:{input_tokens:4,output_tokens:1} }));
    const tools = [{ name:'mcp__mcp-ssh__checkConnectivity', defer_loading:true,
      input_schema:{type:'object',properties:{hostAlias:{type:'string'}}} }];
    const beta = 'oauth-2025-04-20,advanced-tool-use-2025-11-20';
    const response = await f.request({stream,tools}, undefined, beta);
    assert.equal(response.status, 200);
    const text = await response.text();
    assert.match(text, /local fixture answer/);
    assert.equal(f.localCalls.length, 1);
    assert.deepEqual(f.localCalls[0].body.tools, tools);
    assert.deepEqual(f.localCalls[0].body.thinking, {type:'disabled'});
    assert.equal(f.localCalls[0].body.stream, false);
    assert.equal(f.localCalls[0].headers['anthropic-beta'], beta);
    assert.equal(f.proCalls.length, 0);
    const native = {model:'claude-opus-5-5',tools,stream:false};
    await (await f.request(native,undefined,beta)).text();
    assert.equal(f.localCalls.length, 1);
    assert.equal(f.proCalls.length, 1);
    assert.deepEqual(f.proCalls[0].body, body(native));
  });
}

test('ordinary Local compatibility reports failed requests without Plan fallback', async t => {
  const f = await fixture(t, (_req,res) => {res.writeHead(400);res.end('{}');});
  const extra = {tools:[{name:'mcp__fixture__echo',defer_loading:true,input_schema:{type:'object'}}]};
  const response = await f.request(extra,undefined,'oauth-2025-04-20,advanced-tool-use-2025-11-20');
  assert.equal(response.status,400);
  await response.text();
  assert.equal(f.localCalls.length,1);
  assert.equal(f.proCalls.length,0);
});

for (const fail of [false,true]) {
  test(`ordinary Local expands inline MCP tool declarations without changing Plan history (failure ${fail})`, async t => {
    const f = await fixture(t, (_req,res) => {
      if (fail) {res.writeHead(400);res.end('{}');return;}
      json(res, {...message(),id:'fixture-message',role:'assistant',usage:{input_tokens:4,output_tokens:1}});
    });
    const oldTool = {name:'mcp__fixture__echo',defer_loading:true,input_schema:{type:'object'}};
    const loadedTool = {...oldTool,description:'loaded fixture tool'};
    const extra = {tools:[oldTool],messages:[{role:'user',content:[{type:'text',text:'fixture input'},
      {type:'tool_addition',tool:{type:'tool_definition',definition:loadedTool},cache_control:{type:'ephemeral'}}]}],stream:true};
    const response = await f.request(extra,undefined,'oauth-2025-04-20,inline-tools-2026-09-15');
    assert.equal(response.status,fail ? 400 : 200);
    await response.text();
    assert.equal(f.localCalls.length,1);
    assert.deepEqual(f.localCalls[0].body.messages,[{role:'user',content:[{type:'text',text:'fixture input'}]}]);
    assert.deepEqual(f.localCalls[0].body.tools,[{...loadedTool,cache_control:{type:'ephemeral'}}]);
    assert.equal(f.proCalls.length,0);
  });
}

test('unknown inline tool declaration fields stay on Plan unchanged', async t => {
  const f = await fixture(t,()=>{throw new Error('no Local call expected');});
  const extra = {messages:[{role:'user',content:[{type:'tool_addition',tool:{name:'Read',input_schema:{type:'object'}},unknown_flag:true}]}]};
  await (await f.request(extra,undefined,'oauth-2025-04-20,inline-tools-2026-09-15')).text();
  assert.equal(f.localCalls.length,0);
  assert.deepEqual(f.proCalls[0].body,body(extra));
});

test('final system tool reference resolves uniquely and preserves the rest of the request', async t => {
  const f=await fixture(t,(_req,res)=>json(res,message()));
  const declared={name:'Read',defer_loading:true,input_schema:{type:'object'}};
  const reference={type:'tool_addition',tool:{type:'tool_reference',name:'Read'},cache_control:{type:'ephemeral'}};
  const beta='oauth-2025-04-20,inline-tools-2026-09-15';
  const messages=[{role:'user',content:'fixture'}, {role:'system',content:[{type:'text',text:'continue'},reference]}];
  assert.equal((await f.request({tools:[declared],messages},undefined,beta)).status,200);
  assert.equal(f.localCalls.length,1);
  assert.deepEqual(f.localCalls[0].body.tools,[{...declared,defer_loading:false,cache_control:{type:'ephemeral'}}]);
  assert.deepEqual(f.localCalls[0].body.messages,[messages[0],{role:'system',content:[{type:'text',text:'continue'}]}]);
  for (const invalid of [
    {tools:[],messages},
    {tools:[declared,declared],messages},
    {tools:[{...declared,cache_control:{type:'ephemeral',ttl:'1h'}}],messages},
    {tools:[declared],messages:[messages[0],messages[1],{role:'user',content:'later'}]},
    {tools:[declared],messages:[messages[0],{role:'assistant',content:'previous turn'},messages[1]]},
  ]) {
    assert.equal((await f.request(invalid,undefined,beta)).status,200);
    assert.deepEqual(f.proCalls.at(-1).body,body(invalid));
  }
  assert.equal(f.proCalls.length,5);
});

test('compatibility conversion preserves completed parallel tool history and refuses incomplete results', async t => {
  const f = await fixture(t, (_req,res) => json(res,message()));
  const beta='oauth-2025-04-20,inline-tools-2026-09-15';
  const definition={name:'Read',input_schema:{type:'object'}};
  const initial={role:'user',content:[{type:'text',text:'fixture'},
    {type:'tool_addition',tool:{type:'tool_definition',definition}}]};
  const calls={role:'assistant',content:[{type:'tool_use',id:'a',name:'Read',input:{}},
    {type:'tool_use',id:'b',name:'Read',input:{}}]};
  const result=id=>({type:'tool_result',tool_use_id:id,content:'fixture result'});
  for (const results of [[result('a')],[result('a'),result('a')]]) {
    const extra={messages:[initial,calls,{role:'user',content:results}]};
    assert.equal((await f.request(extra,undefined,beta)).status,200);
    assert.deepEqual(f.proCalls.at(-1).body.messages,extra.messages);
  }
  assert.equal(f.localCalls.length,0);
  const messages=[initial,calls,{role:'user',content:[result('a'),result('b')]}];
  assert.equal((await f.request({messages},undefined,beta)).status,200);
  assert.equal(f.localCalls.length,1);
  assert.deepEqual(f.localCalls[0].body.messages.slice(1),messages.slice(1));
  assert.equal(f.proCalls.length,2);
});

test('a later complex inline tool definition stays native and a later compatible request returns to Local', async t => {
  const f = await fixture(t, (_req,res) => json(res,message()));
  const definition = {name:'mcp__fixture__echo',input_schema:{type:'object',oneOf:[{required:['key']}]}};
  const beta = 'oauth-2025-04-20,inline-tools-2026-09-15';
  const prior = [{role:'user',content:'first'}, {role:'assistant',content:'previous response'}];
  const native = {messages:[...prior,{role:'user',content:[{type:'text',text:'load'},
    {type:'tool_addition',tool:{type:'tool_definition',definition}}]}]};
  assert.equal((await f.request(native,undefined,beta)).status,200);
  assert.deepEqual(f.proCalls[0].body,body(native));
  assert.equal(f.localCalls.length,0);
  const continued = {messages:[...native.messages,{role:'assistant',content:'loaded'},
    {role:'user',content:'continue'}]};
  assert.equal((await f.request(continued,undefined,beta)).status,200);
  assert.deepEqual(f.proCalls[1].body,body(continued));
  assert.equal(f.localCalls.length,0);
  const next = {messages:[...prior,{role:'user',content:'next'}],tools:[definition]};
  assert.equal((await f.request(next,undefined,beta)).status,200);
  assert.deepEqual(f.localCalls[0].body.messages,next.messages);
  assert.equal(f.proCalls.length,2);
});

test('ordinary Local materializes native system tool declarations and retains user text', async t => {
  const f = await fixture(t,(_req,res)=>json(res,message()));
  const definition = {name:'mcp__fixture__echo',input_schema:{type:'object'}};
  const extra = {messages:[{role:'user',content:'fixture input'},
    {role:'system',output_config:{effort:'medium'},content:[{type:'text',text:'fixture reminder'},{type:'tool_addition',tool:{type:'tool_definition',definition}}]}]};
  const response = await f.request(extra,undefined,'oauth-2025-04-20,inline-tools-2026-09-15');
  assert.equal(response.status,200);await response.text();
  assert.equal(f.localCalls.length,1);
  assert.deepEqual(f.localCalls[0].body.tools,[definition]);
  assert.deepEqual(f.localCalls[0].body.messages,[{role:'user',content:'fixture input'},
    {role:'system',output_config:{effort:'medium'},content:[{type:'text',text:'fixture reminder'}]}]);
  assert.equal(f.proCalls.length,0);
});

for (const fail of [false,true]) {
  test(`ordinary Local tool-result history treats null metadata as absent (failure ${fail})`, async t => {
    const f = await fixture(t,(_req,res)=>{
      if (fail) {res.writeHead(400);res.end('{}');return;}
      json(res,message());
    });
    const tool = {type:'tool_use',id:'t1',name:'mcp__fixture__echo',input:{},provider_specific_fields:null};
    const extra = {messages:[{role:'user',content:'fixture input'},
      {role:'assistant',content:[tool]}, {role:'user',content:[{type:'tool_result',tool_use_id:'t1',content:'fixture output'}]}]};
    const response = await f.request(extra);
    assert.equal(response.status,fail ? 400 : 200);await response.text();
    assert.equal(f.localCalls.length,1);
    assert.equal(Object.hasOwn(f.localCalls[0].body.messages[1].content[0],'provider_specific_fields'),false);
    assert.equal(f.proCalls.length,0);
    assert.equal(Object.hasOwn(tool,'provider_specific_fields'),true);
    assert.equal(tool.provider_specific_fields,null);
  });
}

test('merge inline definitions independently of property order and preserve cache breakpoints', async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  const cache = {type:'ephemeral', ttl:'1h'};
  const tools = [{name:'Read', input_schema:{type:'object', properties:{}}, cache_control:cache}];
  const definition = {input_schema:{properties:{}, type:'object'}, name:'Read'};
  const request = def => f.request({tools, messages:[{role:'user',content:[{type:'text',text:'test'},
    {type:'tool_addition',tool:{type:'tool_definition',definition:def}}]}]}, undefined, 'oauth-2025-04-20,inline-tools-2026-09-15');
  await (await request(definition)).text();
  assert.deepEqual(f.localCalls[0].body.tools, tools);
  await (await request({...definition,cache_control:{type:'ephemeral',ttl:'5m'}})).text();
  assert.equal(f.localCalls.length, 1);
  assert.equal(f.proCalls.length, 1);
});


test('one completed inference accounts for usage on both the empty attempt and the retry', async t => {
  const f = await fixture(t, (_req,res) => {
    if (f.localCalls.length === 1) return json(res,{...message(MODEL,[]),stop_reason:'max_tokens',
      usage:{input_tokens:11,output_tokens:5}});
    json(res,{...message(),usage:{input_tokens:12,output_tokens:3}});
  });
  const reply = await f.request();
  assert.equal(reply.status,200);
  await reply.text();
  const metrics = await fetch(new URL('/api/metrics',f.proxyUrl),{headers:{'x-local-route-token':'loopback-nonce'}});
  const {efficiency:e} = await metrics.json();
  assert.equal(e.logical_messages,1);
  assert.equal(e.upstream_calls,2);
  assert.equal(e.repeat_calls,1);
  assert.equal(e.measured_calls,2);
  assert.equal(e.observed_input,23);
  assert.equal(e.observed_output,8);
  assert.equal(e.repeat_tokens,15);
});

test('Local retains Plan tool history when a later turn repeats an already active definition', async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  const beta = 'oauth-2025-04-20,inline-tools-2026-09-15';
  const definition = {name:'fixture',input_schema:{type:'object'}};
  const call = {type:'tool_use',id:'plan-call',name:'fixture',input:{}};
  const history = [{role:'user',content:'run fixture'}, {role:'assistant',content:[call]},
    {role:'user',content:[{type:'tool_result',tool_use_id:call.id,content:'marker'}]}];
  const extra = {tools:[definition],messages:[...history,{role:'system',content:[
    {type:'text',text:'keep working'},{type:'tool_addition',tool:{type:'tool_definition',definition}}]}]};
  await (await f.request(extra,undefined,beta)).text();
  assert.deepEqual(f.localCalls[0].body.messages.slice(0,3),history);
  const changed = {...extra,tools:[{...definition,description:'different contract'}]};
  await (await f.request(changed,undefined,beta)).text();
  assert.deepEqual(f.proCalls[0].body, body(changed));
  const metrics = await fetch(new URL('/api/metrics',f.proxyUrl),{headers:{'x-local-route-token':'loopback-nonce'}});
  assert.equal((await metrics.json()).planBlockers.mid_history_tool_change,1);
});

test('repeated declarations retain the turn and count only effective tool changes', async t => {
  const [a,b,c] = ['a','b','c'].map(id => ({name:'mcp__fixture__'+id,input_schema:{type:'object'}}));
  const prior = [{role:'user',content:'start'},{role:'assistant',content:'ready'}];
  const beta = 'oauth-2025-04-20,inline-tools-2026-09-15';
  for (const [definitions,local] of [[[a],true],[[a,b],true],[[b,a],true],[[b,c],false]]) {
    const f = await fixture(t, (_req,res) => json(res,message()));
    const content = definitions.map(definition => ({type:'tool_addition',tool:{type:'tool_definition',definition}}));
    const extra = {tools:[a],messages:[...prior,{role:'user',content}]};
    assert.equal((await f.request(extra,undefined,beta)).status,200);
    assert.equal(f.proCalls.length,local ? 0 : 1);
    assert.equal(f.localCalls.length,local ? 1 : 0);
    if (local) {
      const sent = f.localCalls[0].body;
      assert.deepEqual(sent.tools,definitions.includes(b) ? [a,b] : [a]);
      assert.deepEqual(sent.messages.slice(0,-1),prior);
      assert.equal(sent.messages.at(-1).role,'user');
      assert.equal(sent.messages.at(-1).content.length,1);
      assert.match(sent.messages.at(-1).content[0].text,definitions.includes(b) ? /mcp__fixture__b/ : /remain available/);
    } else assert.deepEqual(f.proCalls[0].body,body(extra));
  }
});

test('initial tool availability is independent of declaration position', async t => {
  const [a,b,c] = ['a','b','c'].map(id => ({name:'mcp__fixture__'+id,input_schema:{type:'object'}}));
  const add = definition => ({type:'tool_addition',tool:{type:'tool_definition',definition}});
  const turn = (...content) => ({role:'user',content});
  const assistant = {role:'assistant',content:'ready'};
  const beta = 'oauth-2025-04-20,inline-tools-2026-09-15';
  for (const inline of [true,false]) {
    const first = inline ? turn(add(a),{type:'text',text:'start'}) : turn(add(a));
    const extra = {tools:inline ? [] : [a], messages:[first,assistant,turn(add(b))]};
    const f = await fixture(t, (_req,res) => json(res,message()));
    assert.equal((await f.request(extra,undefined,beta)).status,200);
    assert.deepEqual(f.localCalls[0].body.tools,[a,b]);
    const sent = f.localCalls[0].body.messages;
    assert.equal(sent[0].role,'user');
    assert.equal(sent[0].content[0].text,inline ? 'start' : 'Previously declared tools remain available.');
    assert.deepEqual(sent[1],assistant);
    assert.equal(f.proCalls.length,0);
    const next = {...extra,messages:[...extra.messages,assistant,turn(add(c))]};
    assert.equal((await f.request(next,undefined,beta)).status,200);
    assert.deepEqual(f.proCalls[0].body,body(next));
    assert.equal(f.localCalls.length,1);
  }
});

test('cache boundary and malformed cache on redeclaration retain native routing diagnostics', async t => {
  const a = {name:'mcp__fixture__a',input_schema:{type:'object'}};
  for (const cache of [null,{type:'ephemeral'}]) {
    const f = await fixture(t, (_req,res) => json(res,message()));
    const extra = {tools:[a],messages:[{role:'user',content:'start'},{role:'assistant',content:'ready'},
      {role:'user',content:[{type:'tool_addition',tool:{type:'tool_definition',definition:a},cache_control:cache}]}]};
    await (await f.request(extra,undefined,'oauth-2025-04-20,inline-tools-2026-09-15')).text();
    assert.equal(f.localCalls.length,0);
    assert.deepEqual(f.proCalls[0].body,body(extra));
    const metrics = await (await fetch(new URL('/api/metrics',f.proxyUrl),
      {headers:{'x-local-route-token':'loopback-nonce'}})).json();
    if (cache) assert.equal(metrics.planBlockerDetails.cache_boundary,1);
    else assert.equal(metrics.planBlockers.cache_conflict,1);
  }
});

test('single new MCP definition preserves availability and validates generated arguments', async t => {
  const name='mcp__fixture__echo';
  const definition={name,input_schema:{type:'object',properties:{key:{type:'string'}},required:['key'],additionalProperties:false}};
  const extra={messages:[{role:'user',content:'hello'},{role:'assistant',content:'hello'},
    {role:'system',content:[{type:'tool_addition',tool:{type:'tool_definition',definition}}]}]};
  const beta='oauth-2025-04-20,inline-tools-2026-09-15';
  for(const [toolName,input,valid] of [[name,{key:'value'},true],[name,{key:42},false],['unknown',{},false]]) {
    const call={type:'tool_use',id:'call',name:toolName,input};
    const f=await fixture(t,(_req,res)=>json(res,{...message(MODEL,[call]),stop_reason:'tool_use'}));
    const reply=await f.request(extra,undefined,beta);
    assert.equal(reply.ok,valid);
    assert.deepEqual(f.localCalls[0].body.tools,[definition]);
    assert.deepEqual(f.localCalls[0].body.messages.slice(0,2),extra.messages.slice(0,2));
    assert.match(f.localCalls[0].body.messages[2].content[0].text,/became available/);
  }
});

for (const recovered of [true,false]) {
  test(`Local corrects undeclared automatic tool selection at most once (recovered=${recovered})`, async t => {
    const tools = [{name:'Read',input_schema:{type:'object'}}];
    const f = await fixture(t,(_req,res) => {
      const bad = {type:'tool_use',name:'Write',id:'bad',input:{}};
      const good = {type:'tool_use',name:'Read',id:'good',input:{}};
      const block = recovered && f.localCalls.length === 2 ? good : bad;
      json(res,{...message(MODEL,[block]),stop_reason:'tool_use',usage:{input_tokens:17,output_tokens:3}});
    });
    const r = await f.request({tools});
    const text = await r.text();
    assert.equal(r.status,recovered ? 200 : 422);
    assert.doesNotMatch(text,/"Write"/);
    assert.match(text,recovered ? /"Read"/ : /local_undeclared_tool/);
    assert.equal(f.localCalls.length,2);
    assert.equal(f.proCalls.length,0);
    for (const call of f.localCalls) assert.deepEqual(call.body.tools,tools);
    assert.match(JSON.stringify(f.localCalls[1].body.system),/previous generation/);
    const metrics = await (await fetch(new URL('/api/metrics',f.proxyUrl),
      {headers:{'x-local-route-token':'loopback-nonce'}})).json();
    assert.equal(metrics.efficiency.measured_calls,2);
    assert.equal(metrics.efficiency.observed_input,34);
    assert.equal(metrics.efficiency.observed_output,6);
    assert.equal(metrics.efficiency.repeat_tokens,20);
    assert.equal(metrics.efficiency.upstream_calls,2);
    assert.deepEqual(metrics.routes,{external:1,plan:0});
    assert.equal(metrics.failed,Number(!recovered));
    assert.equal(metrics.last.route,'external');
    assert.equal(metrics.circuitOpens,0);
  });
}

test('ordinary Local allows cold startup time before receiving the first body', async t => {
  const f = await fixture(t,async (_req,res) => {
    await new Promise(resolve => setTimeout(resolve,150));
    json(res,message());
  },{attemptTimeoutMs:50,localFirstByteTimeoutMs:1000});
  const r = await f.request();
  assert.equal(r.status,200);
  assert.deepEqual((await r.json()).content,message().content);
  assert.equal(f.localCalls.length,1);
});

test('a failed first Local turn leaves recovery available to concurrent successful turns', async t => {
  const session = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
  let arrivals = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t,async (_req,res) => {
    if (++arrivals === 1) {
      json(res,{...message(MODEL,[{type:'tool_use',id:'x',name:'unknown',input:{}}]),stop_reason:'tool_use'});
      return;
    }
    if (arrivals === 3) release();
    await gate;
    json(res,message());
  },{attemptTimeoutMs:3000,localConcurrency:2});
  const send = extra => f.request(extra,undefined,undefined,session);
  await (await send({model:'claude-opus-5-5'})).text();
  assert.equal((await send({})).status,422);
  const replies = await Promise.all([send({}),send({})]);
  assert.deepEqual(replies.map(r => r.status),[200,200]);
  const metrics = await (await fetch(new URL('/api/metrics',f.proxyUrl),
    {headers:{'x-local-route-token':'loopback-nonce'}})).json();
  assert.equal(metrics.efficiency.eligible_after_plan,1);
  assert.equal(metrics.efficiency.first_local_returns,0);
  assert.equal(metrics.efficiency.local_returns,1);
  assert.equal(f.proCalls.length,1);
});

test('overlapping Plan completions preserve each Local recovery exactly once', async t => {
  let entered, release;
  const ready = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t,async (_req,res) => {
    entered();
    await gate;
    json(res,message());
  });
  const send = async extra => {
    const reply = await f.request(extra,undefined,undefined,'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
    assert.equal(reply.status,200);
    await reply.text();
  };
  await send({model:'claude-opus-5-5'});
  const localA = send({});
  try {
    await ready;
    await send({model:'claude-opus-5-5'});
  } finally { release(); }
  await localA;
  const stats = async () => (await (await fetch(new URL('/api/metrics',f.proxyUrl),
    {headers:{'x-local-route-token':'loopback-nonce'}})).json()).efficiency;
  assert.equal((await stats()).local_returns,1);
  await send({});
  await send({});
  const final = await stats();
  assert.equal(final.local_returns,2);
  assert.equal(final.first_local_returns,2);
  assert.equal(final.eligible_after_plan,2);
});

test('Plan recovery belongs to the matching conversation', async t => {
  const f=await fixture(t,(_req,res)=>json(res,message()));
  const a='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', b='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
  const stats=async()=> (await (await fetch(new URL('/api/metrics',f.proxyUrl),
    {headers:{'x-local-route-token':'loopback-nonce'}})).json()).efficiency;
  await (await f.request({model:'claude-opus-5-5'},undefined,undefined,a)).text();
  await (await f.request({},undefined,undefined,b)).text();
  assert.equal((await stats()).eligible_after_plan,0);
  await (await f.request({},undefined,undefined,a)).text();
  assert.equal((await stats()).first_local_returns,1);
  assert.equal((await stats()).local_returns,1);
  await (await f.request({model:'claude-opus-5-5'})).text();
  await (await f.request({})).text();
  assert.equal((await stats()).instance_followups,1);
  assert.equal((await stats()).local_returns,1);
});

test('single addition checks selection across the entire response with mixed existing schemas', async t=>{
  const name='mcp__fixture__new', beta='oauth-2025-04-20,inline-tools-2026-09-15';
  const added={name,input_schema:{type:'object',properties:{n:{type:'integer',minimum:1},mode:{enum:['ok']},free:{},flag:{const:false}}}};
  const tools=[{name:'existing',input_schema:{type:'object',properties:{...added.input_schema.properties,tags:{type:'array',items:{type:'string'}},s:{type:'string',pattern:'x'}}}},
    {name:'hidden',defer_loading:true,input_schema:{type:'object'}}];
  const messages=[{role:'user',content:'hello'},{role:'assistant',content:'hello'},
    {role:'system',content:[{type:'tool_addition',tool:{type:'tool_definition',definition:added}}]}];
  for(const [choice,names,input,ok] of [
    [{type:'auto'},[name],{n:1,mode:'ok',free:[42],flag:false},true],
    [{type:'auto'},[name],{n:0},false],[{type:'auto'},[name],{mode:'bad'},false],
    [{type:'auto'},[name],{flag:true},false],[{type:'none'},[name],{},false],[{type:'any'},[],{},false],
    [{type:'tool',name},['existing'],{},false],[{type:'auto',disable_parallel_tool_use:true},[name,name],{},false],
    [{type:'auto'},['hidden'],{},false],[{type:'auto'},['existing'],{tags:[7]},false],
    [{type:'auto'},['existing'],{n:0},false],[{type:'auto'},['existing'],{s:'x'},false],
    [{type:'tool',name:'existing'},['existing'],{tags:['a'],n:1,mode:'ok',free:42,flag:false},true],[{type:'any'},[name],{},true]]) {
    const calls = names.map((toolName, i) => ({type:'tool_use',name:toolName,id:'call'+i,input}));
    const toolReply = calls.length ? {...message(MODEL,calls),stop_reason:'tool_use'} : message();
    const f = await fixture(t, (_req,res,request) => {
      json(res, request.body.messages.length === 1 ? message() : toolReply);
    });
    const r=await f.request({tools,messages,tool_choice:choice},undefined,beta);
    assert.equal(r.ok,ok,JSON.stringify(choice));
    assert.equal(f.proCalls.length,0);
    assert.equal(f.localCalls.length,1);
    const next = await f.request({tools});
    assert.equal(next.status,200);
    assert.deepEqual((await next.json()).content,message().content);
    assert.equal(f.proCalls.length,0);
    assert.equal(f.localCalls.length,2);
  }
});

test('ordinary JSON rejects forbidden calls once but forwards control stops unchanged', async t => {
  const call={type:'tool_use',id:'x',name:'demo',input:{}};
  for(const [stop,blocks,choice,ok] of [
    ['tool_use',[call],{type:'none'},false],
    ['refusal',[],{type:'any'},true],
    ['model_context_window_exceeded',[],{type:'tool',name:'demo'},true]
  ]) {
    const f=await fixture(t,(_q,r)=>json(r,{...message(MODEL,blocks),stop_reason:stop}));
    const response=await f.request({tools:[{name:'demo',input_schema:{type:'object'}}],tool_choice:choice});
    const data=await response.json();
    assert.equal(response.ok,ok);
    if(ok) assert.equal(data.stop_reason,stop);
    else assert.equal(data.content,undefined);
    assert.equal(f.localCalls.length,1);
  }
});

test('constrained SSE checks the whole reply before delivery and retains reported cache usage', async t=>{
  for(const forbidden of [false,true]) {
    const f=await fixture(t,(_q,r)=>{
      r.writeHead(200,{'content-type':'text/event-stream'});
      const block=forbidden?{type:'tool_use',id:'x',name:'demo',input:{}}:{type:'text',text:'ok'};
      r.end(frame('message_start',{message:{id:'m',role:'assistant',type:'message',model:MODEL,content:[],
        usage:{input_tokens:10,output_tokens:0,cache_read_input_tokens:40,cache_creation_input_tokens:5}}})+
        frame('content_block_start',{index:0,content_block:block})+frame('content_block_stop',{index:0})+
        frame('message_delta',{delta:{stop_reason:forbidden?'tool_use':'end_turn'},usage:{output_tokens:1}})+frame('message_stop'));
    },{streamPassthrough:true});
    const response=await f.request({stream:true,tool_choice:{type:'none'}});
    const output=await response.text();
    assert.equal(response.ok,!forbidden);
    if(forbidden) assert.doesNotMatch(output,/content_block_start/);
    else {
      const metrics=await (await fetch(new URL('/api/metrics',f.proxyUrl),{headers:{'x-local-route-token':'loopback-nonce'}})).json();
      assert.equal(metrics.efficiency.cache_read,40);
      assert.equal(metrics.efficiency.cache_write,5);
    }
    assert.equal(f.localCalls.length,1);
  }
});
