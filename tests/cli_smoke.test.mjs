import assert from 'node:assert/strict';
import test from 'node:test';
import { access, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { runSmoke } from './cli_smoke.mjs';

const MODEL = 'fixture/model';
const KEY = 'sk-or-fixture';
const fakeUpstream = async () => Response.json({ id: 'msg_fixture', type: 'message', role: 'assistant', model: MODEL,
  content: [{ type: 'text', text: 'fixture answer' }], stop_reason: 'end_turn', usage: { input_tokens: 1, output_tokens: 1 } });
const requestCli = `
const base = process.env.ANTHROPIC_BASE_URL;
const token = process.env.ANTHROPIC_CUSTOM_HEADERS.split(': ')[1];
fetch(base + '/v1/messages', { method: 'POST', headers: { 'content-type': 'application/json',
  'x-local-route-token': token, authorization: 'Bearer sk-ant-oat-fixture', 'anthropic-beta': 'oauth-2025-04-20' },
  body: JSON.stringify({ model: 'claude-sonnet-5-5', max_tokens: 16, stream: false,
    messages: [{ role: 'user', content: 'fixture' }] }) })
  .then(async response => { if (!response.ok) throw Error('request failed');
    const body = await response.json(); console.log(JSON.stringify({ type: 'result', subtype: 'success', is_error: false, result: body.content[0].text })); })
  .catch(() => { process.exitCode = 1; });`;

function options(script) {
  return { key: KEY, model: MODEL, command: process.execPath, args: ['-e', script],
    fetchImpl: fakeUpstream, env: process.env, timeoutMs: 20000 };
}

test('smoke serves a child request and checks current route metrics', { timeout: 40000 }, async () => {
  const report = await runSmoke(options(requestCli));
  assert.equal(report.ok, true);
  assert.equal(report.requests, 1);
  assert.equal(report.routes.external, 1);
  assert.equal(report.reply, 'fixture answer');
});

test('smoke never writes an inherited proxy metrics file', { timeout: 40000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'smoke-isolation-'));
  const metricsPath = join(dir, 'metrics.json');
  const oldMetrics = process.env.ROUTE_METRICS_PATH;
  try {
    await writeFile(metricsPath, 'metrics-sentinel');
    process.env.ROUTE_METRICS_PATH = metricsPath;
    const report = await runSmoke(options(requestCli));
    assert.equal(report.ok, true);
    assert.equal(await readFile(metricsPath, 'utf8'), 'metrics-sentinel');
  } finally {
    if (oldMetrics === undefined) delete process.env.ROUTE_METRICS_PATH; else process.env.ROUTE_METRICS_PATH = oldMetrics;
    await rm(dir, { recursive: true, force: true });
  }
});

test('smoke module import does not touch an inherited route log', { timeout: 30000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'smoke-route-'));
  const routePath = join(dir, 'route.log');
  try {
    await writeFile(routePath, 'log-sentinel');
    await promisify(execFile)(process.execPath, ['--input-type=module', '-e',
      `await import(${JSON.stringify(new URL('./cli_smoke.mjs', import.meta.url).href)})`], { cwd: fileURLToPath(new URL('..', import.meta.url)),
      env: { ...process.env, ROUTE_LOG_PATH: routePath }, timeout: 20000 });
    assert.equal(await readFile(routePath, 'utf8'), 'log-sentinel');
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('smoke refuses a successful child that never routes a request', { timeout: 40000 }, async () => {
  const report = await runSmoke(options("console.log(JSON.stringify({type:'result',subtype:'success',is_error:false,result:'fixture'}))"));
  assert.equal(report.ok, false);
  assert.equal(report.requests, 0);
});

test('smoke reports timeout even if descendant keeps output handles open', { timeout: 25000 }, async () => {
  // Wait for a spawned child that inherits the output handles and exits on a release file.
  const dir = await mkdtemp(join(tmpdir(), 'smoke-descendant-'));
  const release = join(dir, 'release');
  const descendant = `const fs=require('fs');const p=${JSON.stringify(release)};const t=setInterval(()=>{if(fs.existsSync(p)){fs.writeFileSync(p+'.done','');clearInterval(t);clearTimeout(w)}},50);const w=setTimeout(()=>{clearInterval(t)},10000)`;
  const script = `require('child_process').spawn(process.execPath, ['-e', ${JSON.stringify(descendant)}],
    {stdio:'inherit', detached:true}).unref(); setInterval(()=>{},1000)`;
  try {
    const report = await runSmoke({ ...options(script), timeoutMs: 4000 });
    assert.equal(report.ok, false);
    assert.equal(report.timedOut, true);
    assert.equal(report.requests, 0);
  } finally {
    await writeFile(release, 'release');
    // Wait for acknowledgement, bounded by the descendant's independent watchdog.
    const deadline = Date.now() + 11000;
    while (Date.now() < deadline) {
      try { await access(release + '.done'); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    await rm(dir, { recursive: true, force: true });
  }
});

test('smoke rejects a successful child after a failed routed response', { timeout: 30000 }, async () => {
  const fakeFailedUpstream = async () => new Response('fixture failure', { status: 503 });
  const script = `fetch(process.env.ANTHROPIC_BASE_URL + '/v1/messages', {method:'POST',
    headers:{'content-type':'application/json','x-local-route-token':process.env.ANTHROPIC_CUSTOM_HEADERS.split(': ')[1],
      authorization:'Bearer sk-ant-oat-fixture','anthropic-beta':'oauth-2025-04-20'},
    body:JSON.stringify({model:'claude-opus-5-5', max_tokens:16, messages:[{role:'user',content:'fixture'}]})})
    .then(() => console.log(JSON.stringify({type:'result',subtype:'success',is_error:false,result:'pretend success'})))`;
  const report = await runSmoke({ ...options(script), fetchImpl: fakeFailedUpstream });
  assert.equal(report.ok, false);
  assert.equal(report.routes.plan, 1);
  assert.equal(report.failed, 1);
});

test('smoke rejects CLI error result after successful routing', { timeout: 30000 }, async () => {
  const script = requestCli.replace("type: 'result', subtype: 'success', is_error: false", "type: 'result', subtype: 'error_during_execution', is_error: true");
  const report = await runSmoke(options(script));
  assert.equal(report.completed, 1);
  assert.equal(report.ok, false);
});

test('smoke preserves split UTF-8 in CLI JSON results', { timeout: 30000 }, async () => {
  const script = requestCli.replace("console.log(JSON.stringify({ type: 'result', subtype: 'success', is_error: false, result: body.content[0].text }));",
    "const b=Buffer.from(JSON.stringify({type:'result',subtype:'success',is_error:false,result:'한글'}));const n=b.indexOf(Buffer.from('한'));process.stdout.write(b.subarray(0,n+1));setTimeout(()=>process.stdout.write(b.subarray(n+1)),30);");
  const report = await runSmoke(options(script));
  assert.equal(report.ok, true);
  assert.equal(report.reply, '한글');
});

test('smoke rejects excessive child output', { timeout: 30000 }, async () => {
  const report = await runSmoke(options("process.stdout.write('x'.repeat(2 * 1024 * 1024)); setInterval(()=>{},1000)"));
  assert.equal(report.ok, false);
  assert.equal(report.outputExceeded, true);
});

test('smoke handles a missing executable', { timeout: 15000 }, async () => {
  const report = await runSmoke({ ...options(''), command: 'definitely-missing-cli-smoke-fixture' });
  assert.equal(report.ok, false);
  assert.ok(report.spawnError);
});

test('smoke redacts configured credentials from child output', { timeout: 40000 }, async () => {
  const report = await runSmoke(options(`console.error('Bearer ${KEY}'); process.exit(1)`));
  assert.equal(report.ok, false);
  assert.doesNotMatch(report.stderr, /sk-or-fixture/);
});
