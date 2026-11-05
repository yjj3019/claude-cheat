// SPDX-License-Identifier: MIT
import test from 'node:test';
import assert from 'node:assert/strict';
import http2 from 'node:http2';
import { listenSafe } from './listenSafe.mjs';
import { createProxy, DEEPSEEK } from '../claudeRouteProxy.mjs';

const NONCE = 'h2-test-nonce';
const PREV = process.env.ROUTE_UPSTREAM_H2;
process.env.ROUTE_UPSTREAM_H2 = '1';
test.after(() => { if (PREV === undefined) delete process.env.ROUTE_UPSTREAM_H2; else process.env.ROUTE_UPSTREAM_H2 = PREV; });

async function startH2Upstream(t) {
  const sessions = [];
  const streams = [];
  const server = http2.createServer();
  server.on('error', () => {});
  server.on('session', session => sessions.push(session));
  server.on('stream', stream => {
    stream.on('error', () => {});
    streams.push(stream);
    stream.respond({ ':status': 200, 'content-type': 'application/json' });
    stream.end(JSON.stringify({ type: 'message', model: DEEPSEEK,
      content: [{ type: 'text', text: 'pong' }], stop_reason: 'end_turn' }));
  });
  await listenSafe(server);
  t.after(() => new Promise(resolve => { server.closeAllSessions?.(); server.close(() => resolve()); }));
  return { server, streams, sessions };
}

async function startProxy(t, upstream) {
  const proxy = createProxy({ key: 'never-used', nonce: NONCE, provider: 'local', localModel: DEEPSEEK,
    localUrl: `http://127.0.0.1:${upstream.address().port}/v1/messages`, retryDelayMs: 0, cooldownMs: 0, attemptTimeoutMs: 2000 });
  await listenSafe(proxy);
  t.after(() => new Promise(resolve => { proxy.closeAllConnections(); proxy.close(() => resolve()); }));
  const base = `http://127.0.0.1:${proxy.address().port}`;
  return {
    ask: content => fetch(base + '/v1/messages', { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-local-route-token': NONCE },
      body: JSON.stringify({ model: DEEPSEEK, max_tokens: 32, messages: [{ role: 'user', content }] }) }),
  };
}

test('ROUTE_UPSTREAM_H2 multiplexes concurrent requests over a single session', async t => {
  const upstream = await startH2Upstream(t);
  const proxy = await startProxy(t, upstream.server);
  const [first, second] = await Promise.all([proxy.ask('first'), proxy.ask('second')]);
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal((await first.json()).content[0].text, 'pong');
  assert.equal((await second.json()).content[0].text, 'pong');
  assert.ok(upstream.streams.length >= 2, `routed calls reached the h2 upstream (${upstream.streams.length})`);
  assert.equal(upstream.sessions.length, 1, 'one multiplexed session shared by prewarm and both calls');
});

test('ROUTE_UPSTREAM_H2 surfaces a redirect as a failure instead of following it', async t => {
  const upstream = await startH2Upstream(t);
  const redirectServer = await startH2Upstream(t);
  redirectServer.server.removeAllListeners('stream');
  redirectServer.server.on('stream', stream => { stream.respond({ ':status': 302, location: 'https://example.invalid/elsewhere' }); stream.end(); });
  const proxy = await startProxy(t, redirectServer.server);
  const response = await proxy.ask('x');
  assert.ok(response.status >= 400, `redirect must fail closed (got ${response.status})`);
});
