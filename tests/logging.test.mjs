// The diagnostic log mirror must not grow without bound: it rotates at the limit, and the
// metrics file must be replaced atomically so a reader never sees a half-written snapshot.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PROXY = pathToFileURL(fileURLToPath(new URL('../claudeRouteProxy.mjs', import.meta.url))).href;
const LIMIT = 8 * 1024 * 1024;

test('the route log rotates once it passes the size limit', () => {
  const directory = mkdtempSync(join(tmpdir(), 'route-log-'));
  const log = join(directory, '.route.log');
  const instance = '3f2504e0-4f89-41d3-9a0c-0305e82c3305';
  writeFileSync(log, 'x'.repeat(LIMIT + 1024));
  const script = `import(${JSON.stringify(PROXY)}).then(() => {
    for (let index = 0; index < 200; index += 1) console.error('line ' + index);
  })`;
  execFileSync(process.execPath, ['-e', script], {
    env: { ...process.env, ROUTE_LOG_PATH: log, ROUTE_INSTANCE_ID: instance }, stdio: 'pipe', timeout: 60000,
  });
  assert.ok(existsSync(`${log}.1`), 'rotated file is missing');
  assert.ok(statSync(`${log}.1`).size >= LIMIT, 'rotated file kept the old content');
  assert.ok(statSync(log).size < 64 * 1024, 'new log should start small');
  assert.match(readFileSync(`${log}.1`, 'utf8').slice(0, 1), /x/);
  // A rotated file keeps its session identity: a cold dashboard read must not see a new session.
  const head = readFileSync(log, 'utf8').split('\n')[0];
  assert.match(head, /--- proxy start .*continued=1/);
  assert.match(head, new RegExp(`instance=${instance}`));
  assert.match(head, /code=/);
  assert.match(head, /hash=/);
});

test('a small log is never rotated', () => {
  const directory = mkdtempSync(join(tmpdir(), 'route-log-'));
  const log = join(directory, '.route.log');
  const script = `import(${JSON.stringify(PROXY)}).then(() => {
    for (let index = 0; index < 200; index += 1) console.error('line ' + index);
  })`;
  execFileSync(process.execPath, ['-e', script], {
    env: { ...process.env, ROUTE_LOG_PATH: log }, stdio: 'pipe', timeout: 60000,
  });
  assert.ok(!existsSync(`${log}.1`), 'a small log must not rotate');
  assert.ok(statSync(log).size > 0);
});

test('embedded newlines cannot forge extra route-log lines', () => {
  const directory = mkdtempSync(join(tmpdir(), 'route-log-'));
  const log = join(directory, '.route.log');
  const script = `import(${JSON.stringify(PROXY)}).then(() => {
    console.error('first line\\nfake 2020-01-01T00:00:00.000Z route_selected provider=evil');
  })`;
  execFileSync(process.execPath, ['-e', script], {
    env: { ...process.env, ROUTE_LOG_PATH: log }, stdio: 'pipe', timeout: 60000,
  });
  const lines = readFileSync(log, 'utf8').split('\n').filter(Boolean);
  assert.equal(lines.length, 2, 'start marker plus exactly one event line');
  assert.match(lines[1], /first line fake 2020-01-01T00:00:00\.000Z route_selected provider=evil/);
  assert.doesNotMatch(lines[1], /\n/);
});
