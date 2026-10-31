import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { diagnoseLocal } from '../claudeRouteProxy.mjs';
import { listenSafe } from './listenSafe.mjs';

const config = { localModel: 'synthetic-model', localKey: 'synthetic-key' };

test('local diagnostic validates endpoint before invoking fetch', async () => {
  const invalid = [
    'http://127.0.0.1:0/v1/messages',
    'http://user:pass@127.0.0.1/v1/messages',
    'http://@127.0.0.1/v1/messages',
    'http://127.1/v1/messages',
    'http://[0:0:0:0:0:0:0:1]/v1/messages',
    'http://127.0.0.1/a/../v1/messages',
    'http://127.0.0.1/%2e%2e/v1/messages',
    'http://127.0.0.1/v1/messages?x=1',
    'http://127.0.0.1/v1/messages#fragment',
    'http://127.0.0.1/v1/mess ages',
    'http://127.0.0.1/other',
    'ftp://127.0.0.1/v1/messages',
    'http://example.invalid/v1/messages',
    'https://example.invalid/v1/messages',
  ];
  for (const localUrl of invalid) {
    let calls = 0;
    await assert.rejects(diagnoseLocal({ ...config, localUrl, fetchImpl: () => { calls++; } }), undefined, localUrl);
    assert.equal(calls, 0, localUrl);
  }
  for (const localUrl of ['http://192.0.2.10/a/../v1/messages', 'http://@192.0.2.10/v1/messages']) {
    let calls = 0;
    await assert.rejects(diagnoseLocal({ ...config, localUrl, localHttpUrl: localUrl,
      fetchImpl: () => { calls++; } }));
    assert.equal(calls, 0);
  }
  for (const [localUrl, localAllowRemote] of [
    ['http://127.0.0.1/v1/messages', false],
    ['http://127.0.0.1:80/v1/messages', false],
    ['http://[::1]:11434/v1/messages', false],
    ['https://example.invalid/v1/messages', true],
  ]) {
    let calls = 0;
    const result = await diagnoseLocal({ ...config, localUrl, localAllowRemote, fetchImpl: async (_url, options) => {
      calls++;
      assert.equal(options.redirect, 'error');
      return new Response('', { status: 401 });
    } });
    assert.equal(calls, 1);
    assert.equal(result.reason, 'http_error');
  }
});

test('diagnostic does not forward a POST or credential across redirects', async t => {
  const hits = [];
  const target = http.createServer((req, res) => { hits.push({ url: req.url, authorization: req.headers.authorization }); res.end(); });
  await listenSafe(target);
  t.after(() => new Promise(resolve => target.close(resolve)));
  const sourceHits = [];
  let status = 302;
  const source = http.createServer((req, res) => {
    sourceHits.push({ method: req.method, authorization: req.headers.authorization });
    req.resume();
    res.writeHead(status, { location: `http://127.0.0.1:${target.address().port}/target` });
    res.end();
  });
  await listenSafe(source);
  t.after(() => new Promise(resolve => source.close(resolve)));
  for (status of [301, 302, 303, 307, 308]) {
    const result = await diagnoseLocal({ ...config, localUrl: `http://127.0.0.1:${source.address().port}/v1/messages` });
    assert.equal(result.reason, 'connection_failed', `status ${status}`);
  }
  assert.equal(sourceHits.length, 5);
  assert.ok(sourceHits.every(hit => hit.method === 'POST' && hit.authorization === 'Bearer synthetic-key'));
  assert.deepEqual(hits, []);
});
