import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import net from 'node:net';
import { coordinator, acquireShared, sharedStatus } from '../localAdmission.mjs';

test('already cancelled admission does not open a connection or leave a timeout', async t => {
  const connect = t.mock.method(net, 'connect', () => { throw new Error('unexpected connection'); });
  const timeout = t.mock.method(globalThis, 'setTimeout', () => { throw new Error('unexpected timer'); });
  const abort = new AbortController(); abort.abort();
  await assert.rejects(acquireShared({url:'http://127.0.0.1:12345/v1/messages',
    concurrency:1,queueLimit:0,waitMs:0,signal:abort.signal}), {code:'ABORT_ERR'});
  assert.equal(connect.mock.callCount(), 0);
  assert.equal(timeout.mock.callCount(), 0);
});

test('shared permits bound concurrency, queue overflow, cancellation and mismatched limits', async t => {
  const server = coordinator(0); await once(server, 'listening');
  t.after(() => server.close());
  const options = { port: server.address().port, url:'http://127.0.0.1:12345/v1/messages', concurrency:1, queueLimit:1, waitMs:2000 };
  const release = await acquireShared(options);
  let queued;
  const waiting = new Promise(resolve => { queued = resolve; });
  const abort = new AbortController();
  const next = acquireShared({...options, signal:abort.signal, onState: state => { if (state.queued === 1) queued(); }});
  // Attach the rejection observer before aborting.
  const rejected = assert.rejects(next, /cancelled/);
  await waiting;
  assert.equal(await acquireShared(options), null);
  await assert.rejects(acquireShared({...options,concurrency:2}), /limits differ/);
  abort.abort(); await rejected; release();
  const again = await acquireShared(options); assert.equal(typeof again, 'function'); again();
  let state;
  for(let i=0;i<20;i++) {
    state=await sharedStatus(options);
    if(state.active===0 && state.queued===0)break;
    await new Promise(resolve=>setTimeout(resolve,10));
  }
  assert.equal(state.active,0); assert.equal(state.queued,0);
});

test('coordinator disconnect invalidates an active permit', async t => {
  const server = coordinator(0); await once(server, 'listening');
  const sockets = []; server.on('connection', socket => sockets.push(socket));
  t.after(() => server.close());
  let lost; const failure = new Promise(resolve => { lost=resolve; });
  await acquireShared({port:server.address().port,url:'http://127.0.0.1:12346/v1/messages',concurrency:1,queueLimit:0,waitMs:0,onLost:lost});
  sockets.forEach(socket => socket.destroy()); await failure;
});

test('malformed requests cannot terminate the coordinator or release an existing permit', async t => {
  const server = coordinator(0); await once(server, 'listening');
  t.after(() => server.close());
  const options = {port:server.address().port, url:'http://127.0.0.1:12345/v1/messages', concurrency:1,queueLimit:0,waitMs:0};
  let lost = false;
  const release = await acquireShared({...options,onLost:()=>{lost=true;}});
  t.after(release);
  for (const input of ['null', '[]', 'true', '42', '"text"', '{}', '{"key":{"toString":0}}', '{']) {
    const socket = net.connect(options.port, '127.0.0.1');
    const closed = new Promise(resolve => socket.once('close', resolve));
    socket.on('error', () => {});
    socket.on('connect', () => socket.write(input + '\n'));
    await closed;
    assert.equal((await sharedStatus(options)).active, 1, input);
    assert.equal(lost, false, input);
  }
  assert.equal(await acquireShared(options), null);
});

test('malformed coordinator replies reject cleanly without crashing the proxy client', async t => {
  let reply = 'null';
  const server = net.createServer(socket => {
    socket.on('error', () => {});
    socket.once('data', () => socket.end(reply + '\n'));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => server.close());
  const options = {port:server.address().port,url:'http://127.0.0.1:12345/v1/messages',concurrency:1,queueLimit:0,waitMs:0};
  for (const malformed of ['null', '[]', '42', '{}', '{"event":"state","active":-1}',
    '{"event":"granted","protocol":2}', '{"event":"rejected","protocol":1,"code":"unknown"}']) {
    reply = malformed;
    await assert.rejects(acquireShared(options), /Invalid admission response/);
    await assert.rejects(sharedStatus(options), /Invalid admission status/);
  }
});
