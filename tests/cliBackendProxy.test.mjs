// SPDX-License-Identifier: MIT
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProxy } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const SONNET = 'claude-sonnet-5-5';
const BRIDGE = 'http://127.0.0.1:49123/v1/messages';
const NONCE = 'synthetic-front-token';
const BRIDGE_TOKEN = 'synthetic-backend-token';
const message = (model, content = [{type:'text',text:'fixture answer'}]) => ({
  id:'msg_fixture',type:'message',role:'assistant',model,content,
  stop_reason:content.some(b=>b.type==='tool_use')?'tool_use':'end_turn',
  usage:{input_tokens:5,output_tokens:2},
});

async function fixture(t, handler, extra = {}, planHandler = () => Response.json(message(SONNET))) {
  const calls = [];
  const server = createProxy({provider:'cli',cliKind:'codex',cliRequestedModel:'requested-model',
    localUrl:BRIDGE,localModel:'cli/codex',localKey:BRIDGE_TOKEN,nonce:NONCE,retryDelayMs:0,
    ...extra,fetchImpl:async(url,options)=>{
      calls.push({url,headers:options.headers,body:JSON.parse(options.body)});
      if (url === BRIDGE) return handler(options);
      assert.equal(url,'https://api.anthropic.com/v1/messages');
      return planHandler(options);
    }});
  await listenSafe(server);
  t.after(()=>{server.closeAllConnections();server.close();});
  const base=`http://127.0.0.1:${server.address().port}`;
  const request = (body, signal, beta='oauth-2025-04-20,inline-tools-2026-09-15')=>fetch(base+'/v1/messages',{method:'POST',signal,
    headers:{'content-type':'application/json','x-local-route-token':NONCE,
      authorization:'Bearer sk-ant-oat-synthetic','anthropic-beta':beta},
    body:JSON.stringify({model:SONNET,max_tokens:256,messages:[{role:'user',content:'fixture input'}],...body})});
  const metrics=async()=>await (await fetch(base+'/api/metrics',{headers:{'x-local-route-token':NONCE}})).json();
  return {calls,request,metrics};
}

for (const stream of [false,true]) {
  test(`CLI backend retains Claude tools and JSON/SSE transport (stream ${stream})`,async t=>{
    const tool={name:'mcp__mcp-ssh__checkConnectivity',input_schema:{type:'object',properties:{hostAlias:{type:'string'}}}};
    const toolUse={type:'tool_use',id:'toolu_fixture',name:tool.name,input:{hostAlias:'fixture-host'}};
    const f=await fixture(t,()=>Response.json(message('cli/codex',[toolUse]),
      {headers:{'x-cli-usage-measured':'false'}}));
    const reply=await f.request({stream,messages:[{role:'user',content:'fixture input'},
      {role:'system',content:[{type:'text',text:'fixture reminder'},
        {type:'tool_addition',tool:{type:'tool_definition',definition:tool}}]}]});
    assert.equal(reply.status,200);
    const text=await reply.text();
    assert.match(text,/mcp__mcp-ssh__checkConnectivity/);
    assert.equal(f.calls.length,1);
    assert.equal(f.calls[0].body.model,'cli/codex');
    assert.equal(f.calls[0].body.stream,false);
    assert.deepEqual(f.calls[0].body.tools,[tool]);
    assert.equal(f.calls[0].headers.authorization,`Bearer ${BRIDGE_TOKEN}`);
    assert.equal(JSON.stringify(f.calls[0]).includes('sk-ant-oat-synthetic'),false);
    const m=await f.metrics();
    assert.equal(m.configuration.provider,'cli');
    assert.equal(m.configuration.cliKind,'codex');
    assert.equal(m.configuration.requestedModel,'requested-model');
    assert.equal(m.configuration.model,null,'protocol identifier is not a reported model');
    assert.equal(m.configuration.usageMeasured,false);
    assert.equal(m.routes.external,1);
    assert.equal(m.routes.plan,0);
  });
}

test('stable Sonnet request goes to CLI; an unknown beta remains native-only',async t=>{
  const f=await fixture(t,()=>Response.json(message('cli/codex')));
  let reply=await f.request({},undefined,'oauth-2025-04-20,interleaved-thinking-2025-05-14');
  assert.equal(reply.status,200); await reply.text();
  assert.equal(f.calls.length,1); assert.equal(f.calls[0].url,BRIDGE);
  reply=await f.request({},undefined,'oauth-2025-04-20,future-native-2026-10-20');
  assert.equal(reply.status,200); await reply.text();
  assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].url,'https://api.anthropic.com/v1/messages');
  assert.equal(f.calls[1].headers['anthropic-beta'],'oauth-2025-04-20,future-native-2026-10-20');
  const m=await f.metrics();
  assert.equal(m.routes.external,1); assert.equal(m.routes.plan,1);
});

test('stable CLI routing preserves host tool definitions and tool history',async t=>{
  const tool={name:'Read',description:'Read a file',input_schema:{type:'object',properties:{path:{type:'string'}}}};
  const messages=[{role:'user',content:'inspect'},
    {role:'assistant',content:[{type:'tool_use',id:'toolu_fixture',name:'Read',input:{path:'fixture'}}]},
    {role:'user',content:[{type:'tool_result',tool_use_id:'toolu_fixture',content:'fixture result'}]}];
  const f=await fixture(t,()=>Response.json(message('cli/codex')));
  const reply=await f.request({tools:[tool],messages},undefined,'oauth-2025-04-20,effort-2025-11-24');
  assert.equal(reply.status,200); await reply.text();
  assert.equal(f.calls.length,1); assert.equal(f.calls[0].url,BRIDGE);
  assert.deepEqual(f.calls[0].body.tools,[tool]);
  assert.deepEqual(f.calls[0].body.messages,messages);
  const m=await f.metrics();
  assert.equal(m.routes.external,1); assert.equal(m.routes.plan,0);
});

test('CLI provider exposes only backend-reported model and measured usage',async t=>{
  const f=await fixture(t,()=>Response.json(message('cli/codex'),{
    headers:{'x-cli-reported-model':'reported-model','x-cli-usage-measured':'true'}}));
  assert.equal((await f.metrics()).configuration.model,null);
  await (await f.request({})).text();
  const m=await f.metrics();
  assert.equal(m.configuration.model,'reported-model');
  assert.equal(m.configuration.requestedModel,'requested-model');
  assert.equal(m.configuration.usageMeasured,true);
});

test('CLI failures attempt once and never send the conversation to Plan',async t=>{
  const f=await fixture(t,()=>new Response('{"error":"not_logged_in"}',{status:400}));
  const body={model:SONNET,max_tokens:256,messages:[{role:'user',content:'fixture input'}],
    tools:[{name:'Skill',input_schema:{type:'object',properties:{skill:{type:'string'}}},defer_loading:true}]};
  const reply=await f.request(body);
  assert.equal(reply.status,400);assert.match(await reply.text(),/cli_invalid_request/);
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,BRIDGE);
  const m=await f.metrics();
  assert.equal(m.externalAttempts,1);
  assert.equal(m.attempts,1);
  assert.equal(m.retries,0);
  assert.equal(m.routes.plan,0);
});

test('an Advisor native tool stays on the Plan path and is never sent to the CLI bridge',async t=>{
  const f=await fixture(t,()=>{throw new Error('CLI must not receive an Advisor native tool');});
  const body={tools:[{type:'advisor_20260301',name:'advisor',model:'claude-opus-5-5'}]};
  const reply=await f.request(body,undefined,'oauth-2025-04-20,advisor-tool-2026-03-01');
  assert.equal(reply.status,200); await reply.text();
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,'https://api.anthropic.com/v1/messages');
  assert.deepEqual(f.calls[0].body.tools,body.tools);
  assert.equal((await f.metrics()).routes.plan,1);
});

test('an Advisor Plan rate limit is returned once without retrying or using the CLI',async t=>{
  const f=await fixture(t,()=>{throw new Error('CLI must not receive an Advisor native tool');}, {},
    ()=>Response.json({type:'error',error:{type:'rate_limit_error',message:'rate limited'}},
      {status:429,headers:{'retry-after':'60'}}));
  const reply=await f.request({tools:[{type:'advisor_20260301',name:'advisor',model:'claude-opus-5-5'}]},
    undefined,'oauth-2025-04-20,advisor-tool-2026-03-01');
  assert.equal(reply.status,429);
  assert.equal(reply.headers.get('retry-after'),'60');
  await reply.text();
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,'https://api.anthropic.com/v1/messages');
  const metrics=await f.metrics();
  assert.equal(metrics.routes.plan,1);
  assert.equal(metrics.routes.external,0);
});

test('CLI bridge diagnostic is allowlisted and does not change routing or retry behavior',async t=>{
  const f=await fixture(t,()=>Response.json({error:'unsupported_request'},
    {status:400,headers:{'x-cli-error-code':'unsupported_request'}}));
  const reply=await f.request({},undefined,'oauth-2025-04-20');
  const text=await reply.text();
  assert.equal(reply.status,400);
  assert.match(text,/cli_invalid_request/);
  assert.match(text,/CLI diagnostic: unsupported_request/);
  assert.equal(f.calls.length,1);
  assert.equal((await f.metrics()).routes.plan,0);
});

test('CLI process and turn failure stages are forwarded as fixed diagnostic codes',async t=>{
  for (const diagnostic of ['cli_process_failed','cli_turn_failed','cli_empty_output']) {
    const f=await fixture(t,()=>Response.json({error:'cli_failed'},
      {status:400,headers:{'x-cli-error-code':diagnostic}}));
    const reply=await f.request({},undefined,'oauth-2025-04-20');
    assert.equal(reply.status,400);
    assert.match(await reply.text(),new RegExp(`CLI diagnostic: ${diagnostic}`));
    assert.equal(f.calls.length,1);
    assert.equal((await f.metrics()).routes.plan,0);
  }
});

test('untrusted CLI diagnostic headers are not reflected',async t=>{
  const secret='synthetic-secret-must-not-leak';
  const f=await fixture(t,()=>Response.json({error:secret},
    {status:400,headers:{'x-cli-error-code':secret}}));
  const reply=await f.request({},undefined,'oauth-2025-04-20');
  assert.equal(reply.status,400);
  assert.doesNotMatch(await reply.text(),/synthetic-secret-must-not-leak/);
  assert.equal(f.calls.length,1);
  assert.equal((await f.metrics()).routes.plan,0);
});

test('native Plan models bypass the CLI backend',async t=>{
  const f=await fixture(t,()=>{throw new Error('CLI must not receive native Opus');});
  await (await f.request({model:'claude-opus-5-5'})).text();
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,'https://api.anthropic.com/v1/messages');
});

test('CLI provider refuses malformed engine configuration and remote bridges',()=>{
  const config={provider:'cli',cliKind:'codex',localModel:'cli/codex',localUrl:BRIDGE,
    localKey:BRIDGE_TOKEN,nonce:NONCE};
  assert.throws(()=>createProxy({...config,cliKind:'other'}));
  assert.throws(()=>createProxy({...config,localModel:'fake-model'}));
  assert.throws(()=>createProxy({...config,localUrl:'https://remote.invalid/v1/messages',localAllowRemote:true}));
});


test('credential-looking reported model metadata is not exposed',async t=>{
  const f=await fixture(t,()=>Response.json(message('cli/codex'),{
    headers:{'x-cli-reported-model':'sk-ant-oat-private-fixture','x-cli-usage-measured':'false'}}));
  await (await f.request({})).text();
  const m=await f.metrics();
  assert.equal(m.configuration.model,null);
  assert.equal(JSON.stringify(m).includes('sk-ant-oat-private-fixture'),false);
});

test('front client cancellation aborts the backend and never falls back to Plan',async t=>{
  let entered, stopped;
  const started=new Promise(resolve=>{entered=resolve;});
  const aborted=new Promise(resolve=>{stopped=resolve;});
  const f=await fixture(t,options=>new Promise((resolve,reject)=>{
    entered();
    options.signal.addEventListener('abort',()=>{stopped();reject(new DOMException('aborted','AbortError'));},{once:true});
  }));
  const controller=new AbortController();
  const pending=f.request({},controller.signal).catch(error=>error);
  await started;controller.abort();
  await pending;
  await Promise.race([aborted,new Promise((_,reject)=>setTimeout(()=>reject(new Error('backend was not aborted')),1500))]);
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].url,BRIDGE);
});
