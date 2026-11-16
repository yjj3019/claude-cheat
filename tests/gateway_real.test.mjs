// Actual child proxies and TCP upstream: no injected fetch implementation.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { coordinator } from '../localAdmission.mjs';

async function proxy(t, env, args=[]) {
  const child = spawn(process.execPath, ['claudeRouteProxy.mjs','--parent-pipe',...args], {
    cwd: new URL('..',import.meta.url), windowsHide:true,
    env:{...process.env, ROUTE_PREWARM:'0', SONNET_PROVIDER:'local', LOCAL_ROUTE_TOKEN:'real-test-token',
      LOCAL_ROUTE_ONLY:'1', LOCAL_LLM_MODEL:'fixture-model', LOCAL_LLM_API_KEY:'fixture-secret',...env},
    stdio:['pipe','pipe','pipe'],
  });
  let errors=''; child.stderr.on('data', chunk => { errors += chunk; });
  const exit = once(child,'exit');
  t.after(async () => { if(child.exitCode===null) child.kill(); await exit; });
  const lines=createInterface({input:child.stdout});
  const result=await Promise.race([once(lines,'line').then(([line])=>JSON.parse(line)),exit.then(()=>({error:errors}))]);
  return {child,result,exit};
}
test('two real proxy processes share one upstream slot and preserve responses', {timeout:20000}, async t => {
  let active=0,peak=0,calls=0;
  const upstream=http.createServer(async(req,res)=> {
    let text=''; for await(const chunk of req)text+=chunk;
    const body=JSON.parse(text); active++;peak=Math.max(peak,active);calls++;
    await new Promise(resolve=>setTimeout(resolve,100)); active--;
    res.writeHead(200,{'content-type':'application/json'});
    res.end(JSON.stringify({id:'msg_test',type:'message',role:'assistant',model:body.model,
      content:[{type:'text',text:'ok'}],stop_reason:'end_turn',usage:{input_tokens:1,output_tokens:1}}));
  });
  upstream.listen(0,'127.0.0.1');await once(upstream,'listening');t.after(()=>upstream.close());
  const admission=coordinator(0);await once(admission,'listening');t.after(()=>admission.close());
  const url=`http://127.0.0.1:${upstream.address().port}/v1/messages`;
  const env={LOCAL_LLM_URL:url,ROUTE_LOCAL_CONCURRENCY:'1',ROUTE_LOCAL_QUEUE_LIMIT:'8',ROUTE_ADMISSION_PORT:String(admission.address().port)};
  const a=await proxy(t,env),b=await proxy(t,env);
  assert.ok(a.result.port);assert.ok(b.result.port);
  const send=p=>fetch(`http://127.0.0.1:${p.result.port}/v1/messages`,{method:'POST',
    headers:{'content-type':'application/json','x-local-route-token':'real-test-token'},
    body:JSON.stringify({model:'claude-sonnet-5-5',max_tokens:64,messages:[{role:'user',content:'hello'}]})});
  const responses=await Promise.all([send(a),send(b),send(a),send(b)]);
  for(const response of responses){assert.equal(response.status,200,await response.clone().text());assert.equal((await response.json()).content[0].text,'ok');}
  assert.equal(calls,4);assert.equal(peak,1);
});

test('real startup distinguishes rejected configuration from occupied port without secrets', async t => {
  const bad=await proxy(t,{LOCAL_LLM_URL:'http://192.0.2.10/v1/messages'});
  assert.match(bad.result.error,/configuration_invalid/); assert.doesNotMatch(bad.result.error,/fixture-secret|192\.0\.2/);
  const busy=http.createServer();busy.listen(0,'127.0.0.1');await once(busy,'listening');t.after(()=>busy.close());
  const failed=await proxy(t,{LOCAL_LLM_URL:'http://127.0.0.1/v1/messages'},['--port',String(busy.address().port)]);
  assert.match(failed.result.error,/port_in_use/);
});

for(const config of [
  {LOCAL_LLM_URL:'http://192.0.2.10:4000/v1/messages',LOCAL_LLM_HTTP_URL:'http://192.0.2.10:4000/v1/messages'},
  {LOCAL_LLM_URL:'https://example.invalid/v1/messages',LOCAL_LLM_ALLOW_REMOTE:'true'},
])test('real proxy startup accepts '+new URL(config.LOCAL_LLM_URL).protocol,async t=>{
  const p=await proxy(t,config);assert.ok(p.result.port);
});
