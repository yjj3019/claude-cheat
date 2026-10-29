import assert from 'node:assert/strict';
import test from 'node:test';
import {createProxy} from '../claudeRouteProxy.mjs';
import {listenSafe} from './listenSafe.mjs';

const token = 'synthetic-token';
const sonnet = 'claude-sonnet-5-5';
const message = (model, usage = {input_tokens: 3, output_tokens: 7}) => ({
  id: 'msg_fixture', type: 'message', role: 'assistant', model,
  content: [{type:'text', text:'fixture'}], stop_reason:'end_turn', usage,
});
async function fixture(t, {provider='local', response, fallback=false}) {
  const isCli = provider === 'cli';
  const url = 'http://127.0.0.1:49123/v1/messages';
  const server = createProxy({provider, localUrl:url, localModel:isCli?'cli/codex':'fixture/model',
    localKey:'fixture-key', nonce:token, retryDelayMs:0, ...(isCli?{cliKind:'codex'}:{}),
    fetchImpl:async (target)=>target === url ? response() : Response.json(message(sonnet)),
  });
  await listenSafe(server);
  t.after(()=>{server.closeAllConnections();server.close();});
  const base=`http://127.0.0.1:${server.address().port}`;
  const send = (stream=false)=>fetch(base+'/v1/messages',{method:'POST',
    headers:{'content-type':'application/json','x-local-route-token':token,
      authorization:'Bearer sk-ant-oat-synthetic','anthropic-beta':'oauth-2025-04-20'},
    body:JSON.stringify({model:sonnet,max_tokens:256,stream,messages:[{role:'user',content:'fixture'}]})});
  const metrics=async()=>await(await fetch(base+'/api/metrics',{headers:{'x-local-route-token':token}})).json();
  return {send,metrics};
}

test('local valid JSON reports bounded session totals',async t=>{
 const f=await fixture(t,{response:()=>Response.json(message('fixture/model'))});
 await (await f.send()).text();
 await (await f.send()).text();
 const m=(await f.metrics()).modelPerformance;
 assert.equal(m.local.samples,2);assert.equal(m.local.outputTokens,14);
 assert.ok(m.local.elapsedMs>0);
 assert.deepEqual(m.openrouter,{samples:0,outputTokens:0,elapsedMs:0});
});

test('CLI unreported usage is excluded, reported usage is included',async t=>{
 let reported=false;
 const f=await fixture(t,{provider:'cli',response:()=>Response.json(message('cli/codex'),
   {headers:{'x-cli-usage-measured':String(reported)}})});
 await (await f.send()).text();
 assert.equal((await f.metrics()).modelPerformance.cli.samples,0);
 reported=true;
 await (await f.send(true)).text();
 const value=(await f.metrics()).modelPerformance.cli;
 assert.equal(value.samples,1);assert.equal(value.outputTokens,7);assert.ok(value.elapsedMs>0);
});

test('missing or invalid usage and Plan fallback are excluded',async t=>{
 let mode='missing';
 const f=await fixture(t,{response:()=>mode==='fail'?new Response('no',{status:400}):
   Response.json(message('fixture/model',mode==='missing'?{}:{output_tokens:2.5}))});
 await (await f.send()).text();mode='invalid';await (await f.send()).text();
 mode='fail';await (await f.send()).text();
 assert.deepEqual((await f.metrics()).modelPerformance.local,{samples:0,outputTokens:0,elapsedMs:0});
});
