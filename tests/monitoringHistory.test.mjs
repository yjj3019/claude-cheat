import assert from 'node:assert/strict';
import test from 'node:test';
import {createProxy} from '../claudeRouteProxy.mjs';
import {listenSafe} from './listenSafe.mjs';

const token = 'synthetic-token';
const sonnet = 'claude-sonnet-5-5';
const localUrl = 'http://127.0.0.1:49123/v1/messages';
const message = (model, usage = {input_tokens: 3, output_tokens: 7}) => ({
  id: 'msg_fixture', type: 'message', role: 'assistant', model,
  content: [{type:'text', text:'fixture'}], stop_reason:'end_turn', usage,
});
async function fixture(t, options = {}) {
  let ms = Date.UTC(2026, 9, 29, 0, 0);
  const server = createProxy({provider:'local', localUrl, localModel:'fixture/model',
    localKey:'fixture-key', nonce:token, retryDelayMs:0, now:()=>ms,
    fetchImpl:options.fetchImpl || (async target=>Response.json(message(target === localUrl ? 'fixture/model' : 'claude-haiku-4-5', options.usage))),
  });
  await listenSafe(server);
  t.after(()=>{server.closeAllConnections();server.close();});
  const base=`http://127.0.0.1:${server.address().port}`;
  const send = (model=sonnet, signal)=>fetch(base+'/v1/messages',{method:'POST', signal,
    headers:{'content-type':'application/json','x-local-route-token':token,
      authorization:'Bearer sk-ant-oat-synthetic','anthropic-beta':'oauth-2025-04-20'},
    body:JSON.stringify({model,max_tokens:256,messages:[{role:'user',content:'fixture'}]})});
  const metrics=async()=>await(await fetch(base+'/api/metrics',{headers:{'x-local-route-token':token}})).json();
  return {send, metrics, advance:(minutes)=>{ms += minutes*60000;}};
}

test('minute history counts concurrent outcomes, routes and reported tokens once',async t=>{
 const f=await fixture(t);
 await Promise.all([f.send(),f.send(),f.send('claude-haiku-4-5'),f.send('invalid-model')].map(async p=>(await p).text()));
 const history=(await f.metrics()).monitoringHistory;
 assert.equal(history.length,1);
 const h=history[0];
 assert.equal(h.minute,'2026-10-29T00:00:00.000Z');
 assert.deepEqual([h.completed,h.ok,h.failed,h.aborted],[4,3,1,0]);
 assert.deepEqual([h.external,h.plan],[2,1]);
 assert.equal(h.tokenSamples,2);assert.equal(h.outputTokens,14);
 assert.ok(h.latencyMsSum>0);assert.ok(h.tokenElapsedMs>0);
 assert.deepEqual(Object.keys(h).sort(),['aborted','completed','external','failed','latencyMsSum','minute','ok','outputTokens','plan','tokenElapsedMs','tokenSamples','cliExternalSuccess','cliInputSamples','cliOutputSamples','cliInputTokens','cliOutputTokens'].sort());
 const flow=(await f.metrics()).tokenFlow;
 assert.deepEqual(flow.local,{responses:2,inputSamples:2,inputTokens:6,outputSamples:2,outputTokens:14});
 assert.deepEqual(flow.openrouter,{responses:0,inputSamples:0,inputTokens:0,outputSamples:0,outputTokens:0});
});

test('history rolls at minute boundaries and expires older than 60 minutes',async t=>{
 const f=await fixture(t);await (await f.send()).text();f.advance(1);await (await f.send()).text();
 let h=(await f.metrics()).monitoringHistory;assert.equal(h.length,2);
 assert.deepEqual(h.map(x=>x.minute),['2026-10-29T00:00:00.000Z','2026-10-29T00:01:00.000Z']);
 f.advance(59);h=(await f.metrics()).monitoringHistory;
 assert.deepEqual(h.map(x=>x.minute),['2026-10-29T00:01:00.000Z']);
 f.advance(1);assert.deepEqual((await f.metrics()).monitoringHistory,[]);
});

test('missing provider output usage never fabricates token samples',async t=>{
 const f=await fixture(t,{usage:{}});await (await f.send()).text();
 const h=(await f.metrics()).monitoringHistory[0];
 assert.equal(h.completed,1);assert.equal(h.ok,1);assert.equal(h.tokenSamples,0);
 assert.equal(h.outputTokens,0);assert.equal(h.tokenElapsedMs,0);
 const flow=(await f.metrics()).tokenFlow.local;
 assert.deepEqual(flow,{responses:1,inputSamples:0,inputTokens:0,outputSamples:0,outputTokens:0});
});

test('zero input is reported and cache token fields are not invented or added',async t=>{
 const f=await fixture(t,{usage:{input_tokens:0,output_tokens:7,cache_read_input_tokens:100,cache_creation_input_tokens:200}});
 await (await f.send()).text();
 assert.deepEqual((await f.metrics()).tokenFlow.local,
   {responses:1,inputSamples:1,inputTokens:0,outputSamples:1,outputTokens:7});
});

test('CLI placeholder usage is unreported until bridge confirms measurement',async t=>{
 let measured=false;
 const server=createProxy({provider:'cli',localUrl,localModel:'cli/codex',cliKind:'codex',
   localKey:'fixture-key',nonce:token,retryDelayMs:0,
   fetchImpl:async()=>Response.json(message('cli/codex'),{headers:{'x-cli-usage-measured':String(measured)}})});
 await listenSafe(server);t.after(()=>{server.closeAllConnections();server.close();});
 const base=`http://127.0.0.1:${server.address().port}`;
 const send=()=>fetch(base+'/v1/messages',{method:'POST',
   headers:{'content-type':'application/json','x-local-route-token':token,
     authorization:'Bearer sk-ant-oat-synthetic','anthropic-beta':'oauth-2025-04-20'},
   body:JSON.stringify({model:sonnet,max_tokens:256,messages:[{role:'user',content:'fixture'}]})});
 const metrics=async()=>await(await fetch(base+'/api/metrics',{headers:{'x-local-route-token':token}})).json();
 await (await send()).text();measured=true;await (await send()).text();
 const result=await metrics();
 const usage={responses:2,inputSamples:1,inputTokens:3,outputSamples:1,outputTokens:7};
 assert.deepEqual(result.tokenFlow.cli,usage);
 assert.deepEqual(result.cliUsage,usage);
 const h=result.monitoringHistory[0];
 assert.deepEqual([h.cliExternalSuccess,h.cliInputSamples,h.cliInputTokens,h.cliOutputSamples,h.cliOutputTokens],[2,1,3,1,7]);
});

test('successful directly streamed external replies have unknown token usage',async t=>{
 const frame=(type,fields)=>`event: ${type}\ndata: ${JSON.stringify({type,...fields})}\n\n`;
 const payload=[frame('message_start',{message:{id:'msg_stream',role:'assistant',model:'fixture/model',usage:{input_tokens:5,output_tokens:2}}}),
   frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
   frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'fixture'}}),
   frame('content_block_stop',{index:0}),frame('message_delta',{delta:{stop_reason:'end_turn'}}),frame('message_stop',{})].join('');
 const server=createProxy({key:'sk-or-fixture',nonce:token,openrouterModel:'fixture/model',streamPassthrough:true,
   fetchImpl:async()=>new Response(payload,{headers:{'content-type':'text/event-stream'}})});
 await listenSafe(server);t.after(()=>{server.closeAllConnections();server.close();});
 const base=`http://127.0.0.1:${server.address().port}`;
 const response=await fetch(base+'/v1/messages',{method:'POST',headers:{'content-type':'application/json',
   'x-local-route-token':token,authorization:'Bearer sk-ant-oat-synthetic','anthropic-beta':'oauth-2025-04-20'},
   body:JSON.stringify({model:sonnet,max_tokens:256,stream:true,messages:[{role:'user',content:'fixture'}]})});
 assert.equal(response.status,200);await response.text();
 const metrics=await(await fetch(base+'/api/metrics',{headers:{'x-local-route-token':token}})).json();
 assert.deepEqual(metrics.tokenFlow.openrouter,
   {responses:1,inputSamples:0,inputTokens:0,outputSamples:0,outputTokens:0});
});

test('history has at most 60 buckets even when the proxy runs longer',async t=>{
 const f=await fixture(t);
 for(let i=0;i<61;i++){await (await f.send()).text();f.advance(1);}
 const h=(await f.metrics()).monitoringHistory;
 assert.equal(h.length,59); // At minute 61 only minutes 2 through 60 remain.
 assert.equal(h[0].minute,'2026-10-29T00:02:00.000Z');
});

test('cancelled client is recorded separately from completed failures',async t=>{
 let entered;
 const ready=new Promise(resolve=>{entered=resolve;});
 const f=await fixture(t,{fetchImpl:async()=>{entered();return new Promise(()=>{});}});
 const controller=new AbortController();
 const pending=f.send(sonnet,controller.signal).catch(()=>null);
 await ready;controller.abort();await pending;
 let history=[];
 for(let i=0;i<30;i++){
   history=(await f.metrics()).monitoringHistory;
   if(history.length && history[0].aborted)break;
   await new Promise(resolve=>setTimeout(resolve,10));
 }
 assert.equal(history.length,1);
 assert.equal(history[0].completed,1);assert.equal(history[0].aborted,1);
 assert.equal(history[0].ok,0);assert.equal(history[0].failed,0);
 assert.equal(history[0].tokenSamples,0);
});
