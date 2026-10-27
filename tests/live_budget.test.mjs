import assert from 'node:assert/strict';
import test from 'node:test';
import { singleOpenRouterRequest } from './live_budget.mjs';
import { runSmoke } from './cli_smoke.mjs';

const url = 'https://openrouter.ai/api/v1/messages';
test('one outbound request is reserved synchronously and redirects are forbidden', async () => {
  const calls = [];
  const guard = singleOpenRouterRequest((target, options) => {
    calls.push({ target, options });
    return Promise.resolve('fixture');
  });
  const first = guard.fetchImpl(url, { method: 'POST', redirect: 'follow' });
  assert.throws(() => guard.fetchImpl(url, { method: 'POST' }), /blocked/);
  assert.throws(() => guard.fetchImpl('https://api.anthropic.com/v1/messages', { method: 'POST' }), /blocked/);
  assert.equal(await first, 'fixture');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.redirect, 'error');
  assert.deepEqual(guard.counts(), { sent: 1, blocked: 2 });
});

test('bad destinations and methods do not consume the slot', () => {
  const guard = singleOpenRouterRequest(() => 'fixture');
  for (const [target, method] of [[url + '?x=1', 'POST'], [url, 'GET'], ['https://api.anthropic.com/v1/messages', 'POST']])
    assert.throws(() => guard.fetchImpl(target, { method }), /blocked/);
  assert.equal(guard.fetchImpl(url, { method: 'POST' }), 'fixture');
  assert.deepEqual(guard.counts(), { sent: 1, blocked: 3 });
});

test('failed first send cannot reopen the budget', async () => {
  const guard = singleOpenRouterRequest(() => Promise.reject(new Error('fixture')));
  await assert.rejects(guard.fetchImpl(url, { method: 'POST' }), /fixture/);
  assert.throws(() => guard.fetchImpl(url, { method: 'POST' }), /blocked/);
  assert.deepEqual(guard.counts(), { sent: 1, blocked: 1 });
});

test('a synchronously thrown send error also consumes the budget', () => {
  const guard = singleOpenRouterRequest(() => { throw new Error('fixture sync failure'); });
  assert.throws(() => guard.fetchImpl(url, { method: 'POST' }), /fixture sync failure/);
  assert.throws(() => guard.fetchImpl(url, { method: 'POST' }), /blocked/);
  assert.deepEqual(guard.counts(), { sent: 1, blocked: 1 });
});

test('proxy retries and Plan fallback cannot bypass the guard', { timeout: 40000 }, async () => {
  const guard = singleOpenRouterRequest(async () => new Response('fixture outage', { status: 503 }));
  const child = `const token=process.env.ANTHROPIC_CUSTOM_HEADERS.split(': ')[1];
    fetch(process.env.ANTHROPIC_BASE_URL+'/v1/messages',{method:'POST',headers:{'content-type':'application/json',
      'x-local-route-token':token,authorization:'Bearer sk-ant-oat-fixture','anthropic-beta':'oauth-2025-04-20'},
      body:JSON.stringify({model:'claude-sonnet-5-5',max_tokens:16,messages:[{role:'user',content:'fixture'}]})})
      .then(r=>r.text()).then(()=>console.log(JSON.stringify({type:'result',subtype:'success',is_error:false,result:'fixture'})))`;
  const report = await runSmoke({ key: 'sk-or-fixture', model: 'fixture/model', command: process.execPath,
    args: ['-e', child], fetchImpl: guard.fetchImpl, timeoutMs: 25000 });
  assert.equal(report.ok, false);
  assert.equal(guard.counts().sent, 1);
  assert.ok(guard.counts().blocked >= 1);
});
