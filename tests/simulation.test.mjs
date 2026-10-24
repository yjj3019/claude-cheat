// Smoke test for the traffic simulator: a small mixed load must route every
// scenario as expected, so routing regressions fail the ordinary suite too.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

test('simulator routes a small mixed load without violations', () => {
  const result = spawnSync(process.execPath,
    ['tests/simulate.mjs', '--requests', '120', '--concurrency', '8', '--turns', '10', '--soak', '200',
      '--json'],
    { encoding: 'utf8', timeout: 180000 });
  assert.equal(result.status, 0, (result.stderr || result.stdout).slice(0, 800));
  const summary = JSON.parse(result.stdout);
  assert.deepEqual(summary.violations, []);
  assert.ok(summary.requests >= 120);
  assert.equal(summary.session.turns, 10);
  assert.equal(summary.soak.requests, 200);
  assert.ok(summary.soak.peakRssMb > 0);
  assert.ok(summary.routes.replacement > 0);
  assert.ok(summary.latencyMs.p50 > 0);
  assert.equal(summary.circuit.bypass, 200);
  assert.equal(summary.circuit.fallback, 200);
  assert.equal(summary.circuit.cooldown, 200);
});
