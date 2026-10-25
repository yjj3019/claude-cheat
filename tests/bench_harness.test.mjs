// Regression tests for the performance harness itself (tests/bench_proxy.mjs).
//
// The harness is only trustworthy if its aggregation, correctness comparison, retry-count
// verification, budget/verdict and exit-code logic are themselves tested. Everything here
// is pure and synchronous: no worker is spawned, no port is bound, no network is touched.
// A "fake worker report" is a plain object shaped like the child's report line, which
// exercises exactly the comparison code the real run uses.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import {
  CLIENT_MODEL, EXIT, UsageError, UPSTREAM_MODEL, aggregateProcesses, describeExternalPayload,
  evaluateVerdict, expectedExternalBody, externalPayloadExpectation, fixtureMessage, fixtureSse,
  historyPayload, median, parseArgs, percentile, resolveConfig, sizePayload, streamPayload,
  summarizeSamples, summarizeSoak, validateHttpResponse, verifyMetrics, verifyUpstreamReport,
} from './bench_proxy.mjs';

const sha256 = text => createHash('sha256').update(text).digest('hex');
const processWith = (index, values, { medianMs = median(values), p90Ms = percentile(values, 0.9) } = {}) =>
  ({ index, samples: [...values], medianMs, p90Ms });

test('median handles odd, even, unsorted, empty and single values', () => {
  assert.equal(median([]), null);
  assert.equal(median([7]), 7);
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([10, 30, 20, 40]), 25);
  assert.equal(median([-1, 0, 1]), 0);
});

test('percentile is nearest-rank and rejects out-of-range fractions', () => {
  const values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
  assert.equal(percentile(values, 0), 10);
  assert.equal(percentile(values, 0.5), 50);
  assert.equal(percentile(values, 0.9), 90);
  assert.equal(percentile(values, 1), 100);
  assert.equal(percentile([], 0.9), null);
  assert.throws(() => percentile(values, 1.5), RangeError);
});

test('summarizeSamples reports count, median, p90, min and max', () => {
  assert.deepEqual(summarizeSamples([]), { count: 0, medianMs: null, p90Ms: null, minMs: null, maxMs: null });
  assert.deepEqual(summarizeSamples([1, 2, 3, 4, 5]),
    { count: 5, medianMs: 3, p90Ms: 5, minMs: 1, maxMs: 5 });
  assert.deepEqual(summarizeSamples([2, 4]), { count: 2, medianMs: 3, p90Ms: 4, minMs: 2, maxMs: 4 });
});

test('aggregateProcesses uses per-process medians, not pooled samples', () => {
  const aggregate = aggregateProcesses([processWith(0, [1, 2, 3]), processWith(1, [10, 20, 30])]);
  // median of [2, 20] is 11; pooling would average the six samples to 11 too here, so use
  // a case where the two disagree: one process is fast overall, the other has one outlier.
  assert.equal(aggregate.medianMs, 11);
  assert.equal(aggregate.minMs, 2);
  assert.equal(aggregate.maxMs, 20);
  assert.equal(aggregate.spreadRatio, 10);
  assert.deepEqual(aggregate.processMedians, [2, 20]);
  const skewed = aggregateProcesses([processWith(0, [1, 1, 1]), processWith(1, [9, 9, 100])]);
  assert.equal(skewed.medianMs, 5, 'median of process medians (1, 9), not pooled median');
  assert.equal(skewed.spreadRatio, 9);
});

test('aggregateProcesses keeps unmeasured processes and tolerates an empty run', () => {
  const aggregate = aggregateProcesses([{ index: 0, medianMs: 4, p90Ms: 4.5 },
    { index: 1, medianMs: null, p90Ms: null }]);
  assert.deepEqual(aggregate.processMedians, [4, null]);
  assert.equal(aggregate.medianMs, 4);
  const empty = aggregateProcesses([]);
  assert.equal(empty.medianMs, null);
  assert.equal(empty.spreadRatio, null);
  assert.deepEqual(empty.processMedians, []);
});

test('parseArgs returns documented defaults', () => {
  const options = parseArgs([]);
  assert.equal(options.quick, false);
  assert.equal(options.help, false);
  assert.equal(options.workers, 3);
  assert.equal(options.rounds, 15);
  assert.equal(options.warmup, 2);
  assert.equal(options.retryProcesses, 2);
  assert.equal(options.soakSeconds, 0);
  assert.equal(options.soakConcurrency, 8);
  assert.equal(options.maxRssGrowthMb, 192);
  assert.equal(options.maxHeapGrowthMb, 12);
  assert.equal(options.maxMedianMs, null);
  assert.equal(options.maxSpreadRatio, null);
  assert.equal(options.sizes, null);
  assert.deepEqual([...options.provided], []);
});

test('parseArgs accepts values, lists and flags', () => {
  const options = parseArgs(['--quick', '--workers', '4', '--sizes', '16,256,1024',
    '--soak-seconds', '3', '--max-median-ms', '25.5', '--max-spread-ratio', '2']);
  assert.equal(options.quick, true);
  assert.equal(options.workers, 4);
  assert.deepEqual(options.sizes, [16, 256, 1024]);
  assert.equal(options.soakSeconds, 3);
  assert.equal(options.maxMedianMs, 25.5);
  assert.equal(options.maxSpreadRatio, 2);
  assert.ok(options.provided.has('workers'));
  assert.ok(options.provided.has('sizes'));
});

test('parseArgs rejects usage errors with exit code 2', () => {
  const cases = [
    ['--nope'], ['--workers'], ['--workers', '0'], ['--workers', '17'], ['--workers', '2.5'],
    ['--rounds', '0'], ['--sizes', 'abc'], ['--sizes', '16,0'], ['--soak-seconds', '-1'],
    ['--max-spread-ratio', '0.5'], ['--max-median-ms', 'no'],
  ];
  for (const argv of cases) {
    assert.throws(() => parseArgs(argv), (error) => {
      assert.ok(error instanceof UsageError, `expected UsageError for ${argv.join(' ')}`);
      assert.equal(error.exitCode, EXIT.usage);
      return true;
    }, `expected a usage error for ${argv.join(' ')}`);
  }
});

test('resolveConfig applies the quick profile without overriding explicit flags', () => {
  const quick = resolveConfig(parseArgs(['--quick']));
  assert.equal(quick.workers, 2);
  assert.equal(quick.rounds, 6);
  assert.equal(quick.warmup, 1);
  assert.equal(quick.retryProcesses, 1);
  assert.deepEqual(quick.sizes, [16, 1024]);
  const overridden = resolveConfig(parseArgs(['--quick', '--workers', '5', '--rounds', '20']));
  assert.equal(overridden.workers, 5);
  assert.equal(overridden.rounds, 20);
  assert.equal(overridden.warmup, 1, 'unspecified quick defaults still apply');
  const standard = resolveConfig(parseArgs([]));
  assert.equal(standard.workers, 3);
  assert.equal(standard.rounds, 15);
  assert.deepEqual(standard.sizes, [16, 64, 256, 1024]);
  assert.equal(standard.maxMedianMs, null, 'latency budget is opt-in');
});

test('payload builders produce the fixed workload shapes', () => {
  const size = JSON.parse(sizePayload(16));
  assert.equal(size.model, CLIENT_MODEL);
  assert.equal(size.messages.length, 1);
  assert.equal(size.tools[0].name, 'Read');
  assert.ok(sizePayload(16).length > 15 * 1024 && sizePayload(16).length < 17 * 1024,
    'size payload stays within its KB budget');
  assert.equal(historyPayload(4).length > 0, true);
  const history = JSON.parse(historyPayload(4, { kbEach: 1 }));
  assert.equal(history.messages.length, 4);
  assert.deepEqual(history.messages.map(message => message.role), ['user', 'assistant', 'user', 'assistant']);
  const stream = JSON.parse(streamPayload());
  assert.equal(stream.stream, true);
});

test('fixtureMessage and fixtureSse carry the metadata the proxy validates', () => {
  const message = fixtureMessage(CLIENT_MODEL);
  assert.equal(message.type, 'message');
  assert.equal(message.model, CLIENT_MODEL);
  assert.equal(message.id, 'msg_bench');
  assert.ok(Number.isFinite(message.usage.input_tokens));
  const sse = fixtureSse(CLIENT_MODEL);
  const frames = sse.split('\n\n').filter(Boolean);
  assert.equal(frames.length, 6);
  assert.deepEqual(frames.map(frame => /^event: (.+)$/m.exec(frame)[1]),
    ['message_start', 'content_block_start', 'content_block_delta', 'content_block_stop',
      'message_delta', 'message_stop']);
  const start = JSON.parse(/^data: (.+)$/m.exec(frames[0])[1]);
  assert.equal(start.message.model, CLIENT_MODEL);
});

test('expectedExternalBody mirrors the proxy rewrite without mutating the client body', () => {
  const client = { model: CLIENT_MODEL, max_tokens: 16, messages: [{ role: 'user', content: 'hi' }],
    metadata: { user_id: 'x' }, stream: true };
  const snapshot = JSON.parse(JSON.stringify(client));
  const external = expectedExternalBody(client);
  assert.deepEqual(client, snapshot, 'client body must not be mutated');
  assert.equal(external.model, UPSTREAM_MODEL);
  assert.equal(external.metadata, undefined);
  assert.deepEqual(external.thinking, { type: 'disabled' });
  assert.deepEqual(external.provider, { allow_fallbacks: false });
  assert.equal(external.stream, true);
  assert.equal(expectedExternalBody(JSON.stringify(client)).model, UPSTREAM_MODEL, 'accepts a JSON string');

  const withTools = expectedExternalBody({ model: CLIENT_MODEL, max_tokens: 8,
    messages: [{ role: 'user', content: 'hi' }], tools: [{ name: 'Read', input_schema: { type: 'object' } }] });
  assert.equal(withTools.tools.length, 1);
  assert.equal(withTools.tools[0].name, 'Read');

  const custom = expectedExternalBody({ model: CLIENT_MODEL, max_tokens: 8, messages: [] },
    { upstreamModel: 'other/model' });
  assert.equal(custom.model, 'other/model');
});

test('external payload expectation hashes the exact rewritten body', () => {
  const body = JSON.stringify({ model: CLIENT_MODEL, max_tokens: 16,
    messages: [{ role: 'user', content: 'hi' }] });
  const expectation = externalPayloadExpectation(body);
  const expectedText = JSON.stringify(expectedExternalBody(body));
  assert.equal(expectation.hash, sha256(expectedText));
  assert.equal(expectation.bytes, Buffer.byteLength(expectedText));
  assert.equal(expectation.summary.model, UPSTREAM_MODEL);
  assert.equal(expectation.summary.stream, false);
  assert.equal(expectation.summary.thinkingType, 'disabled');
  assert.equal(expectation.summary.providerAllowFallbacks, false);
  assert.equal(expectation.summary.metadataPresent, false);
  assert.equal(expectation.summary.messageCount, 1);
  // Same client body always hashes the same: reproducibility across processes.
  assert.equal(externalPayloadExpectation(body).hash, expectation.hash);

  const stream = externalPayloadExpectation(streamPayload());
  assert.equal(stream.summary.stream, true);
});

test('describeExternalPayload records the summary fields the child must report', () => {
  const description = describeExternalPayload(expectedExternalBody(sizePayload(4)));
  assert.equal(description.summary.toolCount, 1);
  assert.equal(description.summary.messageCount, 1);
  assert.equal(description.summary.model, UPSTREAM_MODEL);
  assert.equal(description.summary.bytes, description.bytes);
});

test('validateHttpResponse deep-compares JSON bodies and exact streaming text', () => {
  const expectedBody = fixtureMessage(CLIENT_MODEL);
  const ok = validateHttpResponse({ status: 200, contentType: 'application/json',
    text: JSON.stringify(expectedBody) }, { status: 200, contentTypeIncludes: 'application/json',
    body: expectedBody });
  assert.deepEqual(ok, { ok: true, failures: [] });

  const wrongBody = validateHttpResponse({ status: 200, contentType: 'application/json',
    text: JSON.stringify({ ...expectedBody, content: [{ type: 'text', text: 'changed' }] }) },
    { status: 200, body: expectedBody });
  assert.equal(wrongBody.ok, false);
  assert.match(wrongBody.failures.join(' '), /body differs/);

  const extraField = validateHttpResponse({ status: 200, contentType: 'application/json',
    text: JSON.stringify({ ...expectedBody, extra: true }) }, { status: 200, body: expectedBody });
  assert.equal(extraField.ok, false, 'deep equality must reject extra fields');

  const wrongStatus = validateHttpResponse({ status: 502, contentType: 'application/json', text: '{}' },
    { status: 200, body: expectedBody });
  assert.match(wrongStatus.failures.join(' '), /status 502/);

  const wrongType = validateHttpResponse({ status: 200, contentType: 'text/plain', text: 'x' },
    { status: 200, contentTypeIncludes: 'application/json', text: 'x' });
  assert.equal(wrongType.ok, false);

  const invalidJson = validateHttpResponse({ status: 200, contentType: 'application/json', text: 'not json' },
    { status: 200, body: expectedBody });
  assert.match(invalidJson.failures.join(' '), /not JSON/);

  const sse = fixtureSse(CLIENT_MODEL);
  assert.equal(validateHttpResponse({ status: 200, contentType: 'text/event-stream', text: sse },
    { status: 200, contentTypeIncludes: 'text/event-stream', text: sse }).ok, true);
  assert.equal(validateHttpResponse({ status: 200, contentType: 'text/event-stream', text: `${sse}x` },
    { status: 200, text: sse }).ok, false);
});

test('verifyUpstreamReport accepts exact counts, hashes and summaries', () => {
  const body = sizePayload(16);
  const expectation = externalPayloadExpectation(body);
  const report = { calls: 7, planCalls: 0,
    bodies: [{ hash: expectation.hash, count: 7, ...expectation.summary }] };
  assert.deepEqual(verifyUpstreamReport(report, { expectedCalls: [{ ...expectation, count: 7 }],
    planCalls: 0 }), { ok: true, failures: [] });
});

test('verifyUpstreamReport fails on wrong retry counts, unexpected payloads or Plan fallback', () => {
  const body = sizePayload(16);
  const expectation = externalPayloadExpectation(body);
  const summary = expectation.summary;

  const wrongCount = verifyUpstreamReport({ calls: 2, planCalls: 0,
    bodies: [{ hash: expectation.hash, count: 2, ...summary }] },
  { expectedCalls: [{ ...expectation, count: 4 }] });
  assert.equal(wrongCount.ok, false);
  assert.match(wrongCount.failures.join(' '), /seen 2x, expected 4x/);

  const missing = verifyUpstreamReport({ calls: 4, planCalls: 0, bodies: [] },
    { expectedCalls: [{ ...expectation, count: 4 }] });
  assert.equal(missing.ok, false);
  assert.match(missing.failures.join(' '), /never saw expected payload/);

  const unexpected = verifyUpstreamReport({ calls: 5, planCalls: 0,
    bodies: [{ hash: expectation.hash, count: 4, ...summary }, { hash: 'f'.repeat(64), count: 1 }] },
  { expectedCalls: [{ ...expectation, count: 4 }] });
  assert.equal(unexpected.ok, false);
  assert.match(unexpected.failures.join(' '), /unexpected rewritten payload/);

  const plan = verifyUpstreamReport({ calls: 4, planCalls: 1,
    bodies: [{ hash: expectation.hash, count: 4, ...summary }] },
  { expectedCalls: [{ ...expectation, count: 4 }], planCalls: 0 });
  assert.equal(plan.ok, false);
  assert.match(plan.failures.join(' '), /Plan fallback attempts 1/);
});

test('verifyUpstreamReport compares every rewritten-payload summary field', () => {
  const expectation = externalPayloadExpectation(streamPayload());
  const report = { calls: 1, planCalls: 0,
    bodies: [{ hash: expectation.hash, count: 1, ...expectation.summary, stream: false }] };
  const check = verifyUpstreamReport(report, { expectedCalls: [{ ...expectation, count: 1 }] });
  assert.equal(check.ok, false);
  assert.match(check.failures.join(' '), /stream = false != true/);
});

test('verifyMetrics compares numeric counters exactly', () => {
  assert.deepEqual(verifyMetrics({ attempts: 8, retries: 4, failed: 0 }, { attempts: 8, retries: 4, failed: 0 }),
    { ok: true, failures: [] });
  const mismatch = verifyMetrics({ attempts: 8, retries: 3 }, { attempts: 8, retries: 4 });
  assert.equal(mismatch.ok, false);
  assert.match(mismatch.failures.join(' '), /metrics.retries = 3 != expected 4/);
  assert.equal(verifyMetrics({ ok: 0 }, { ok: 0 }).ok, true);
});

test('summarizeSoak computes start/mid/end medians and growth', () => {
  const memory = {
    start: { gc: true, rss: 100 * 1024 * 1024, heapUsed: 10 * 1024 * 1024 },
    mid: { gc: true, rss: 101 * 1024 * 1024, heapUsed: 10.5 * 1024 * 1024 },
    end: { gc: true, rss: 102 * 1024 * 1024, heapUsed: 11 * 1024 * 1024 },
  };
  const samples = [
    ...Array.from({ length: 3 }, () => ({ wallMs: 2, phase: 0 })),
    ...Array.from({ length: 3 }, () => ({ wallMs: 3, phase: 1 })),
    ...Array.from({ length: 3 }, () => ({ wallMs: 4, phase: 2 })),
  ];
  const soak = summarizeSoak({ samples, memory, thresholds: { maxRssGrowthMb: 24, maxHeapGrowthMb: 12 } });
  assert.equal(soak.pass, true);
  assert.deepEqual(soak.buckets.map(bucket => bucket.medianMs), [2, 3, 4]);
  assert.equal(soak.growth.rssGrowthMb, 2);
  assert.equal(soak.growth.heapGrowthMb, 1);
  assert.equal(soak.growth.midRssGrowthMb, 1);
  assert.equal(soak.growth.midHeapGrowthMb, 0.5);
  assert.equal(soak.growth.gcAvailable, true);
});

test('summarizeSoak fails only on configured growth thresholds', () => {
  const memory = {
    start: { gc: true, rss: 100 * 1024 * 1024, heapUsed: 10 * 1024 * 1024 },
    end: { gc: true, rss: 130 * 1024 * 1024, heapUsed: 30 * 1024 * 1024 },
  };
  const rss = summarizeSoak({ samples: [], memory, thresholds: { maxRssGrowthMb: 20, maxHeapGrowthMb: 100 } });
  assert.equal(rss.pass, false);
  assert.match(rss.failures.join(' '), /RSS growth 30MB > 20MB/);
  const heap = summarizeSoak({ samples: [], memory, thresholds: { maxRssGrowthMb: 100, maxHeapGrowthMb: 12 } });
  assert.equal(heap.pass, false);
  assert.match(heap.failures.join(' '), /heap growth 20MB > 12MB/);
  const relaxed = summarizeSoak({ samples: [], memory, thresholds: { maxRssGrowthMb: 100, maxHeapGrowthMb: 100 } });
  assert.equal(relaxed.pass, true);
  const shrinking = summarizeSoak({ samples: [], thresholds: { maxRssGrowthMb: 0, maxHeapGrowthMb: 0 },
    memory: { start: { gc: true, rss: 200, heapUsed: 100 }, end: { gc: true, rss: 100, heapUsed: 50 } } });
  assert.equal(shrinking.pass, true, 'negative growth never fails');
});

test('summarizeSoak skips the heap threshold when post-GC sampling is unavailable', () => {
  const soak = summarizeSoak({ samples: [], thresholds: { maxRssGrowthMb: 24, maxHeapGrowthMb: 1 },
    memory: { start: { gc: false, rss: 100, heapUsed: 0 }, end: { gc: false, rss: 101, heapUsed: 1e9 } } });
  assert.equal(soak.growth.gcAvailable, false);
  assert.equal(soak.growth.heapGrowthMb, null);
  assert.equal(soak.pass, true, 'without --expose-gc a heap growth verdict would be meaningless');
});

test('evaluateVerdict passes a clean run with exit code 0', () => {
  const verdict = evaluateVerdict({ workloads: [
    { name: 'size 16KB', kind: 'size', bodyKb: 16, medianMs: 1, spreadRatio: 1.1 },
    { name: 'size 1024KB', kind: 'size', bodyKb: 1024, medianMs: 9, spreadRatio: 1.2 },
  ], thresholds: {} });
  assert.deepEqual(verdict, { pass: true, exitCode: EXIT.pass, failures: [] });
});

test('evaluateVerdict fails correctness failures with exit code 1', () => {
  const verdict = evaluateVerdict({ correctnessFailures: ['[streaming] body differs'], workloads: [] });
  assert.equal(verdict.pass, false);
  assert.equal(verdict.exitCode, EXIT.fail);
  assert.deepEqual(verdict.failures, ['[streaming] body differs']);
  const objectForm = evaluateVerdict({ correctnessFailures: [{ message: '[perf p0] metrics.retries = 1' }] });
  assert.deepEqual(objectForm.failures, ['[perf p0] metrics.retries = 1']);
});

test('evaluateVerdict applies the latency budget only when opted in, to the largest payload', () => {
  const workloads = [
    { name: 'size 16KB', kind: 'size', bodyKb: 16, medianMs: 1, spreadRatio: 1 },
    { name: 'size 1024KB', kind: 'size', bodyKb: 1024, medianMs: 9, spreadRatio: 1 },
    { name: 'history 120 turns', kind: 'history', bodyKb: 240, medianMs: 99, spreadRatio: 1 },
  ];
  assert.equal(evaluateVerdict({ workloads, thresholds: {} }).pass, true, 'no budget by default');
  const under = evaluateVerdict({ workloads, thresholds: { maxMedianMs: 10 } });
  assert.equal(under.pass, true, 'budget only constrains the largest size workload');
  const over = evaluateVerdict({ workloads, thresholds: { maxMedianMs: 5 } });
  assert.equal(over.pass, false);
  assert.match(over.failures.join(' '), /size 1024KB > budget 5ms/);
  assert.equal(over.exitCode, EXIT.fail);
});

test('evaluateVerdict applies the spread budget to perf and retry workloads', () => {
  const workloads = [{ name: 'size 16KB', kind: 'size', bodyKb: 16, medianMs: 1, spreadRatio: 1.2 }];
  const retry = [{ name: 'retry fail-first', kind: 'retry', medianMs: 5, spreadRatio: 3 }];
  assert.equal(evaluateVerdict({ workloads, retry, thresholds: { maxSpreadRatio: 2 } }).pass, false);
  assert.equal(evaluateVerdict({ workloads, retry, thresholds: { maxSpreadRatio: 3.5 } }).pass, true);
});

test('evaluateVerdict folds soak growth failures into the verdict', () => {
  const soak = { pass: false, failures: ['soak RSS growth 30MB > 24MB'] };
  const verdict = evaluateVerdict({ soak });
  assert.equal(verdict.pass, false);
  assert.deepEqual(verdict.failures, ['soak RSS growth 30MB > 24MB']);
  assert.equal(evaluateVerdict({ soak: { pass: true, failures: [] } }).pass, true);
});

test('exit codes are stable contract values', () => {
  assert.deepEqual(EXIT, { pass: 0, fail: 1, usage: 2 });
  assert.equal(new UsageError('bad').exitCode, EXIT.usage);
});
