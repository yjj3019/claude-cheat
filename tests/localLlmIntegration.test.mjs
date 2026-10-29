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
  const request = (extra, signal, beta = 'oauth-2025-04-20') => fetch(proxyUrl, { method: 'POST', signal,
    headers: { 'content-type': 'application/json', 'x-local-route-token': 'loopback-nonce',
      authorization: OAUTH, 'anthropic-beta': beta }, body: JSON.stringify(body(extra)) });
  return { local, localUrl, localCalls, proCalls, attempts, request };
}

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
  test(`empty local HTTP ${format} retries, falls back, and recovers after cooldown`, { timeout: 5000 }, async t => {
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
    await (await f.request(extra)).text();
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 1);
    assert.deepEqual(f.proCalls[0].body, body(extra));
    healthy = true;
    await (await f.request(extra)).text();
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 2);
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
    assert.equal(f.proCalls.length, 2);
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
  const response = await f.request({ stream: true });
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

test('connection refusal makes four local attempts then one native fallback', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => json(res, message()));
  await new Promise(resolve => f.local.close(resolve));
  const response = await f.request();
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), message(SONNET));
  assert.equal(f.attempts.length, 4);
  assert.equal(f.localCalls.length, 0);
  assert.equal(f.proCalls.length, 1);
  assert.deepEqual(f.proCalls[0].body, body());
  assert.equal(f.proCalls[0].headers.authorization, OAUTH);
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
  assert.equal(f.proCalls.length, 1);
  healthy = true;
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 4);
  assert.equal(f.proCalls.length, 2);
  time += 101;
  const probe = f.request();
  await probeEntered;
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 5, 'only one half-open local request');
  assert.equal(f.proCalls.length, 3);
  release();
  const recovered = await probe;
  assert.equal(recovered.status, 200);
  await recovered.text();
  await (await f.request()).text();
  assert.equal(f.localCalls.length, 6);
  assert.equal(f.proCalls.length, 3);
});

for (const format of ['json', 'sse']) {
  test(`malformed local ${format} tool output never reaches the HTTP client`, { timeout: 5000 }, async t => {
    const f = await fixture(t, (_req, res) => {
      res.writeHead(200, { 'content-type': format === 'json' ? 'application/json' : 'text/event-stream' });
      res.end(format === 'json' ? '{"content":[{"name":"UNSAFE_TOOL"}]' :
        frame('message_start', { message: { model: MODEL } }) +
        frame('content_block_start', { index: 0, content_block: { type: 'tool_use', id: 'bad', name: 'UNSAFE_TOOL', input: {} } }));
    });
    const response = await f.request(format === 'sse' ? { stream: true } : undefined);
    assert.equal(response.status, 200);
    const text = await response.text();
    assert.doesNotMatch(text, /UNSAFE_TOOL/);
    assert.deepEqual(JSON.parse(text), message(SONNET));
    assert.equal(f.localCalls.length, 4);
    assert.equal(f.proCalls.length, 1);
  });
}

test('real HTTP stalled response times out four times and falls back', { timeout: 5000 }, async t => {
  const closed = [];
  const f = await fixture(t, (_req, res) => {
    closed.push(once(res, 'close'));
    res.writeHead(200, { 'content-type': 'application/json' });
    res.write('{');
  }, { attemptTimeoutMs: 100 });
  const response = await f.request();
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(f.localCalls.length, 4);
  assert.equal(f.proCalls.length, 1);
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

test('ordinary Local compatibility keeps failed requests recoverable on Plan without changing the native body', async t => {
  const f = await fixture(t, (_req,res) => {res.writeHead(400);res.end('{}');});
  const extra = {tools:[{name:'mcp__fixture__echo',defer_loading:true,input_schema:{type:'object'}}]};
  const response = await f.request(extra,undefined,'oauth-2025-04-20,advanced-tool-use-2025-11-20');
  assert.equal(response.status,200);
  await response.text();
  assert.equal(f.localCalls.length,1);
  assert.equal(f.proCalls.length,1);
  assert.deepEqual(f.proCalls[0].body,body(extra));
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
    assert.equal(response.status,200);
    await response.text();
    assert.equal(f.localCalls.length,1);
    assert.deepEqual(f.localCalls[0].body.messages,[{role:'user',content:[{type:'text',text:'fixture input'}]}]);
    assert.deepEqual(f.localCalls[0].body.tools,[{...loadedTool,cache_control:{type:'ephemeral'}}]);
    assert.equal(f.proCalls.length,fail?1:0);
    if (fail) assert.deepEqual(f.proCalls[0].body,body(extra));
  });
}

test('unknown inline tool declaration fields stay on Plan unchanged', async t => {
  const f = await fixture(t,()=>{throw new Error('no Local call expected');});
  const extra = {messages:[{role:'user',content:[{type:'tool_addition',tool:{name:'Read',input_schema:{type:'object'}},unknown_flag:true}]}]};
  await (await f.request(extra,undefined,'oauth-2025-04-20,inline-tools-2026-09-15')).text();
  assert.equal(f.localCalls.length,0);
  assert.deepEqual(f.proCalls[0].body,body(extra));
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
    assert.equal(response.status,200);await response.text();
    assert.equal(f.localCalls.length,1);
    assert.equal(Object.hasOwn(f.localCalls[0].body.messages[1].content[0],'provider_specific_fields'),false);
    assert.equal(f.proCalls.length,fail?1:0);
    if (fail) {
      const expected=body(extra);
      expected.messages=extra.messages.map((m,index)=> index===1?{...m,content:[{type:'tool_use',id:'t1',name:'mcp__fixture__echo',input:{}}]}:m);
      assert.deepEqual(f.proCalls[0].body,expected);
    }
    assert.equal(Object.hasOwn(tool,'provider_specific_fields'),true);
    assert.equal(tool.provider_specific_fields,null);
  });
}
