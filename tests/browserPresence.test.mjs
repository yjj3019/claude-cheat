import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../browserPresence.js', import.meta.url), 'utf8');

test('tab lifecycle events keep one poll loop and stop it while the page is hidden away', async () => {
  const events = {}, timers = new Set();
  let resolveRequest, calls = 0;
  const listener = (name, fn) => { events[name] = fn; };
  const context = vm.createContext({
    TOKEN:'fixture', location:{pathname:'/'},
    document:{addEventListener:listener, hidden:false},
    window:{addEventListener:listener},
    fetch:(_url, options) => {
      if (JSON.parse(options.body).action === 'close') return Promise.resolve({ok:true});
      calls++;
      return new Promise(resolve => { resolveRequest = () => resolve({ok:true,json:async()=>({})}); });
    },
    setTimeout:fn => { timers.add(fn); return fn; },
    clearTimeout:fn => timers.delete(fn),
  });
  vm.runInContext(source, context);
  events.pageshow(); events.visibilitychange(); events.visibilitychange();
  assert.equal(calls, 1);
  resolveRequest(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 1);
  events.visibilitychange();
  assert.equal(calls, 2);
  events.pagehide();
  resolveRequest(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 0, 'an in-flight heartbeat must not restart polling after pagehide');
  events.pageshow();
  assert.equal(calls, 3);
  resolveRequest(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.size, 1, 'restoring from the back/forward cache resumes exactly one loop');
});

for (const accepted of [false, true]) test(`settings navigation requires a live acknowledgement (${accepted})`, async () => {
  const navigations = [], requests = [];
  vm.runInNewContext(source, {
    TOKEN:'fixture', location:{pathname:'/',assign:path=>navigations.push(path)},
    document:{addEventListener(){},hidden:false},window:{addEventListener(){}},
    fetch:async (_url,options) => {
      const request=JSON.parse(options.body); requests.push(request);
      return {ok:true,json:async()=>request.ack ? {acknowledged:accepted,command:null}
        : {command:{id:'fixture-command',page:'settings'}}};
    },
    setTimeout(){},clearTimeout(){},
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.length,2,'acknowledgement is sent immediately');
  assert.equal(requests[1].ack,'fixture-command');
  assert.deepEqual(navigations,accepted ? ['/settings'] : []);
});

test('a lost acknowledgement response retries the navigation intent', async () => {
  const navigations=[],events={}; let acknowledgements=0;
  const listener=(name,fn)=>{events[name]=fn;};
  vm.runInNewContext(source, {
    TOKEN:'fixture', location:{pathname:'/',assign:path=>navigations.push(path)},
    document:{addEventListener:listener,hidden:false},window:{addEventListener:listener},
    fetch:async (_url,options)=>{
      const request=JSON.parse(options.body);
      if (request.ack && ++acknowledgements===1) throw new Error('response lost');
      return {ok:true,json:async()=>request.ack ? {acknowledged:true,command:null}
        : {command:{id:'fixture-command',page:'settings'}}};
    },
    setTimeout(){},clearTimeout(){},
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(navigations,[]);
  events.visibilitychange();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(acknowledgements,2);
  assert.deepEqual(navigations,['/settings']);
});
