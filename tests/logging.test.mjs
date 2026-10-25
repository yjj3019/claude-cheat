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
  writeFileSync(log, 'x'.repeat(LIMIT + 1024));
  const script = `import(${JSON.stringify(PROXY)}).then(() => {
    for (let index = 0; index < 200; index += 1) console.error('line ' + index);
  })`;
  execFileSync(process.execPath, ['-e', script], {
    env: { ...process.env, ROUTE_LOG_PATH: log }, stdio: 'pipe', timeout: 60000,
  });
  assert.ok(existsSync(`${log}.1`), 'rotated file is missing');
  assert.ok(statSync(`${log}.1`).size >= LIMIT, 'rotated file kept the old content');
  assert.ok(statSync(log).size < 64 * 1024, 'new log should start small');
  assert.match(readFileSync(`${log}.1`, 'utf8').slice(0, 1), /x/);
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
