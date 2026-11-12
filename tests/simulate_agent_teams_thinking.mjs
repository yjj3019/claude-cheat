// SPDX-License-Identifier: MIT
// Ten-round simulation of Agent Teams traffic and thinking (reasoning) handling
// through the routing proxy. Surfaces the practical limitations a user would hit:
// local admission pressure (local_busy), thinking forced off / rejected in ordinary
// modes, Plan-fallback dependency on a subscription OAuth, and the compat-mode
// thinking pipeline (forward, return, echo without pinning).
//
//   node tests/simulate_agent_teams_thinking.mjs            # 10 rounds, human report
//   node tests/simulate_agent_teams_thinking.mjs --json     # machine-readable report
//
// Exit code 0 = only the *known* limitations were observed (reported as findings).
// Exit code 1 = an unexpected failure (crash, 500, pinning, corruption).
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { listenSafe } from './listenSafe.mjs';
// The proxy reads these at module load; keep its route log off stderr of this harness.
process.env.ROUTE_LOG_PATH = join(tmpdir(), `sim-teams-thinking-${process.pid}.route.log`);
process.env.ROUTE_METRICS_PATH = '';
const { createProxy } = await import('../claudeRouteProxy.mjs');
// The in-process proxy writes its structured diagnostics to stderr (its one logging sink).
// The harness report is on stdout; keep real errors reachable through `fatal`.
const fatal = console.error.bind(console);
console.error = () => {};

const ARGS = process.argv.slice(2);
const ROUND_ARG = ARGS.find(arg => !arg.startsWith('--'));
const ROUNDS = Number.isFinite(Number(ROUND_ARG)) ? Number(ROUND_ARG) : 10;
const JSON_OUT = ARGS.includes('--json');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const BETA = 'interleaved-thinking-2025-05-14,effort-2025-11-24';
const LOCAL = 'http://127.0.0.1:11434/v1/messages';
const MODEL = 'fixture-local';
// Ordinary mode gates on full Claude model ids (isSonnet/isPro); bare aliases only resolve in
// compat mode (compatAlias). A real Claude Code client sends the full id, so mimic that.
const CLIENT_MODEL = 'claude-sonnet-5-5';
const NONCE = 'sim-nonce';

const TEAM_TOOLS = [
  { name: 'Agent', input_schema: { type: 'object', properties: { description: { type: 'string' }, prompt: { type: 'string' }, name: { type: 'string' }, subagent_type: { type: 'string' }, run_in_background: { type: 'boolean' } }, required: ['description', 'prompt'] } },
  { name: 'SendMessage', input_schema: { type: 'object', properties: { to: { type: 'string' }, message: { type: 'string' } }, required: ['to', 'message'] } },
  { name: 'TaskCreate', input_schema: { type: 'object', properties: { subject: { type: 'string' }, description: { type: 'string' } }, required: ['subject'] } },
  { name: 'TaskGet', input_schema: { type: 'object', properties: { task_id: { type: 'string' } }, required: ['task_id'] } },
  { name: 'TaskList', input_schema: { type: 'object', properties: {} } },
  { name: 'TaskUpdate', input_schema: { type: 'object', properties: { task_id: { type: 'string' }, action: { type: 'string' } }, required: ['task_id'] } },
  { name: 'Bash', input_schema: { type: 'object', properties: { command: { type: 'string' } }, required: ['command'] } },
];

function meta(model, extra = {}) {
  return { id: 'msg_sim', type: 'message', role: 'assistant', model, usage: { input_tokens: 3, output_tokens: 2 }, ...extra };
}

// Parse the proxy/Anthropic error body: {error:{type,message}} or {error:string}.
function parseError(text) {
  try { const j = JSON.parse(text); const e = j?.error; return typeof e === 'string' ? e : (e?.message || null); }
  catch { return null; }
}

async function withProxy(config, fetchImpl, work) {
  const server = createProxy({ metricsPath: null, retryDelayMs: 0, ...config, fetchImpl });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  try { return await work(base); }
  finally { server.closeAllConnections(); server.close(); }
}

const post = (base, body, auth = null) => fetch(`${base}/v1/messages`, {
  method: 'POST',
  headers: { 'x-local-route-token': NONCE, 'content-type': 'application/json', ...(auth ? { authorization: auth } : {}), 'anthropic-beta': BETA },
  body: JSON.stringify(body),
});

// ---------------------------------------------------------------------------
// Agent Teams: one round = every teammate turn issued as one burst (worst case:
// all teammates go idle at the same instant). Admission cap is localConcurrency=2
// plus a queue of localQueueLimit=8, so >10 concurrent turns start to 503.
// ---------------------------------------------------------------------------
async function teamsRound(round) {
  const mode = round % 2 === 0 ? 'ordinary-local' : 'local-compat';
  const teammates = [2, 3, 4, 5, 6][round % 5];
  const turns = 2;
  const burst = teammates * turns;
  const delay = 90 + ((round * 37) % 130); // synthetic GPU latency per turn
  const upstream = { calls: 0, active: 0, maxActive: 0 };
  const fixture = async (url, options) => {
    upstream.calls++;
    upstream.active++; upstream.maxActive = Math.max(upstream.maxActive, upstream.active);
    await sleep(delay);
    upstream.active--;
    const marker = JSON.stringify(JSON.parse(options.body).messages).match(/TURN:(\d+)/)?.[1];
    if (marker === '1') {
      return Response.json(meta(MODEL, { content: [{ type: 'tool_use', id: `toolu_r${round}_${upstream.calls}`, name: 'SendMessage',
        input: { to: 'teammate-2', message: 'status update' } }], stop_reason: 'tool_use' }));
    }
    return Response.json(meta(MODEL, { content: [{ type: 'text', text: 'done' }], stop_reason: 'end_turn' }));
  };
  const config = { nonce: NONCE, provider: 'local', localUrl: LOCAL, localModel: MODEL, localKey: 'fixture-key',
    ...(mode === 'local-compat' ? { localOnly: true, localCompat: true } : {}) };
  return withProxy(config, fixture, async base => {
    const jobs = [];
    for (let i = 0; i < teammates; i++) for (let turn = 1; turn <= turns; turn++) jobs.push([i, turn]);
    const all = await Promise.all(jobs.map(async ([i, turn]) => {
      const t0 = performance.now();
      const response = await post(base, {
        model: CLIENT_MODEL, max_tokens: 4096, stream: false,
        system: [{ type: 'text', text: `teammate-${i + 1} system prompt`, cache_control: { type: 'ephemeral' } }],
        tools: TEAM_TOOLS,
        messages: [{ role: 'user', content: `teammate-${i + 1} TURN:${turn} work item` }],
      });
      const text = await response.text();
      return { status: response.status, code: parseError(text), ms: Math.round(performance.now() - t0) };
    }));
    const ok = all.filter(r => r.status === 200).length;
    const busy = all.filter(r => r.status === 503 && /local_busy/.test(r.code || '')).length;
    const unexpected = all.filter(r => r.status !== 200 && !(r.status === 503 && /local_busy/.test(r.code || '')));
    // Queue wait ~ client time beyond the upstream delay the turn spent upstream.
    const waits = all.filter(r => r.status === 200).map(r => Math.max(0, r.ms - delay));
    const maxWait = waits.length ? Math.max(...waits) : 0;
    return { mode, teammates, turns, burst, delayMs: delay, totalRequests: all.length, ok, localBusy: busy,
      maxUpstreamActive: upstream.maxActive, maxQueueWaitMs: maxWait, unexpected,
      issue: unexpected.length ? `unexpected failures in ${mode}: ${JSON.stringify(unexpected.slice(0, 3))}`
        : busy ? `${mode}: ${busy}/${all.length} teammate turns REJECTED local_busy (admission cap 2 + queue 8); max queue wait ${maxWait}ms`
        : maxWait > delay ? `${mode}: ${all.length} concurrent turns absorbed, max queue wait ${maxWait}ms (no rejection yet)`
        : `${mode}: ${all.length} concurrent turns, no queuing observed` };
  });
}

// ---------------------------------------------------------------------------
// Thinking: six scenario probes, ten rounds.
// ---------------------------------------------------------------------------
async function thinkingRound(round) {
  const findings = [];
  const problems = [];
  const note = (severity, text) => findings.push({ severity, text });
  const baseBody = { model: CLIENT_MODEL, max_tokens: 1024, stream: false,
    messages: [{ role: 'user', content: 'reason step by step' }] };

  // S1 ordinary local: thinking request is silently force-disabled upstream.
  {
    const seen = [];
    const r = await withProxy({ nonce: NONCE, provider: 'local', localUrl: LOCAL, localModel: MODEL, localKey: 'k' },
      async (url, options) => { seen.push(JSON.parse(options.body)); return Response.json(meta(MODEL, { content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' })); },
      async base => (await post(base, { ...baseBody, thinking: { type: 'enabled', budget_tokens: 4096 } })).status);
    if (r !== 200) problems.push(`S1 unexpected status ${r}`);
    const forwarded = seen[0]?.thinking;
    note(forwarded?.type === 'disabled' ? 'known-limitation' : 'unexpected',
      `S1 ordinary-local: client requested thinking=enabled, upstream received thinking=${JSON.stringify(forwarded)} ` +
      `(model never reasons in ordinary mode; silent capability loss)`);
  }

  // S2 ordinary local: a local server that emits thinking blocks breaks the turn.
  {
    const response = await withProxy({ nonce: NONCE, provider: 'local', localUrl: LOCAL, localModel: MODEL, localKey: 'k' },
      async () => Response.json(meta(MODEL, { content: [{ type: 'thinking', thinking: 'local-reasoning' }, { type: 'text', text: 'ok' }], stop_reason: 'end_turn' })),
      async base => post(base, baseBody));
    const text = await response.text();
    if (response.status === 200) problems.push('S2 thinking block accepted in ordinary local (should be rejected)');
    else note('known-limitation', `S2 ordinary-local: local server returning thinking blocks -> HTTP ${response.status} code=${parseError(text)} (turn fails; requires --local-compat)`);
  }

  // S3 local-compat: thinking is forwarded and a returned (unsigned) thinking block reaches the client.
  {
    const seen = [];
    const response = await withProxy({ nonce: NONCE, provider: 'local', localOnly: true, localCompat: true, localUrl: LOCAL, localModel: MODEL, localKey: 'k' },
      async (url, options) => { seen.push(JSON.parse(options.body)); return Response.json(meta(MODEL,
        { content: [{ type: 'thinking', thinking: 'compat-reasoning' }, { type: 'text', text: 'ok' }], stop_reason: 'end_turn' })); },
      async base => post(base, { ...baseBody, thinking: { type: 'adaptive' } }));
    const body = JSON.parse(await response.text());
    const hasThinking = Array.isArray(body.content) && body.content.some(b => b.type === 'thinking');
    if (response.status !== 200 || !hasThinking) problems.push(`S3 compat thinking pipeline broken: HTTP ${response.status} hasThinking=${hasThinking}`);
    else note('verified-working', `S3 local-compat: thinking forwarded=${JSON.stringify(seen[0]?.thinking)}, unsigned thinking block returned to client OK`);
  }

  // S4 local-compat: echoing the thinking block back must not pin the session (stripped upstream).
  {
    const seen = [];
    const response = await withProxy({ nonce: NONCE, provider: 'local', localOnly: true, localCompat: true, localUrl: LOCAL, localModel: MODEL, localKey: 'k' },
      async (url, options) => { seen.push(JSON.parse(options.body)); return Response.json(meta(MODEL, { content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' })); },
      async base => post(base, { model: CLIENT_MODEL, max_tokens: 1024, stream: false,
        messages: [{ role: 'user', content: 'first' },
          { role: 'assistant', content: [{ type: 'thinking', thinking: 'echoed-reasoning' }, { type: 'text', text: 'mid' }] },
          { role: 'user', content: 'next' }] }));
    const forwarded = JSON.stringify(seen[0]?.messages || []);
    if (response.status !== 200) problems.push(`S4 compat echo failed: HTTP ${response.status}`);
    else if (forwarded.includes('echoed-reasoning')) problems.push('S4 thinking history leaked upstream in compat mode (pinning risk)');
    else note('verified-working', 'S4 local-compat: echoed thinking block stripped before upstream (no pinning loop)');
  }

  // S5 ordinary openrouter: an unsupported thinking config (display) diverts to Plan and
  // hard-fails without a saved subscription OAuth.
  {
    const calls = [];
    const response = await withProxy({ nonce: NONCE, provider: 'openrouter', key: 'sk-or-fixture', openrouterModel: 'fixture/model',
      fallbackModel: 'claude-sonnet-5-5' },
      async (url) => { calls.push(url); return Response.json(meta('fixture/model', { content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' })); },
      async base => post(base, { ...baseBody, thinking: { type: 'enabled', budget_tokens: 1024, display: 'updates' } }));
    const text = await response.text();
    if (response.status === 403 && /subscription OAuth/i.test(text)) {
      note('known-limitation', `S5 ordinary-openrouter: thinking.display -> Plan fallback -> HTTP 403 without saved subscription OAuth (upstream calls=${calls.length})`);
    } else if (response.status === 200) {
      problems.push(`S5 expected Plan 403 but got 200 (upstream calls: ${JSON.stringify(calls)})`);
    } else {
      note('observed', `S5 ordinary-openrouter: thinking.display -> HTTP ${response.status} code=${parseError(text)}`);
    }
  }

  // S6 ordinary local: signed thinking history (after a Plan turn) stays routable to local.
  {
    const seen = [];
    const response = await withProxy({ nonce: NONCE, provider: 'local', localUrl: LOCAL, localModel: MODEL, localKey: 'k' },
      async (url, options) => { seen.push(JSON.parse(options.body)); return Response.json(meta(MODEL, { content: [{ type: 'text', text: 'ok' }], stop_reason: 'end_turn' })); },
      async base => post(base, { model: CLIENT_MODEL, max_tokens: 1024, stream: false,
        messages: [{ role: 'user', content: 'plan turn' },
          { role: 'assistant', content: [{ type: 'thinking', thinking: 'signed-plan-reasoning', signature: 'opaque-sig' }, { type: 'text', text: 'plan' }] },
          { role: 'user', content: 'local turn' }] }));
    const forwarded = JSON.stringify(seen[0]?.messages || []);
    if (response.status !== 200) problems.push(`S6 signed thinking history broke ordinary local: HTTP ${response.status}`);
    else if (forwarded.includes('signed-plan-reasoning')) problems.push('S6 signed thinking history leaked to local upstream (privacy/routing risk)');
    else note('verified-working', 'S6 ordinary-local: signed thinking history stripped before local upstream (session stays routable after a Plan turn)');
  }

  return { findings, problems };
}

// ---------------------------------------------------------------------------
async function main() {
  const report = { rounds: ROUNDS, agent_teams: [], thinking: { rounds: [], problems: [] }, unexpected: [] };
  for (let round = 0; round < ROUNDS; round++) {
    const t0 = Date.now();
    let teams;
    try { teams = await teamsRound(round); }
    catch (error) { teams = { error: String(error?.message || error) }; report.unexpected.push(`teams round ${round} crashed: ${teams.error}`); }
    teams.ms = Date.now() - t0;
    report.agent_teams.push(teams);
    let thinking;
    try { thinking = await thinkingRound(round); }
    catch (error) { thinking = { findings: [], problems: [`thinking round ${round} crashed: ${String(error?.message || error)}`] }; report.unexpected.push(...thinking.problems); }
    report.thinking.rounds.push(thinking);
    report.thinking.problems.push(...thinking.problems);
  }
  const busyTotal = report.agent_teams.reduce((s, r) => s + (r.localBusy || 0), 0);
  const okTotal = report.agent_teams.reduce((s, r) => s + (r.ok || 0), 0);
  const reqTotal = report.agent_teams.reduce((s, r) => s + (r.totalRequests || 0), 0);
  const maxWaitTotal = Math.max(0, ...report.agent_teams.map(r => r.maxQueueWaitMs || 0));
  report.summary = {
    agent_teams: { rounds: ROUNDS, requests: reqTotal, ok: okTotal, localBusy: busyTotal,
      maxQueueWaitMs: maxWaitTotal,
      headline: busyTotal
        ? `${busyTotal}/${reqTotal} teammate turns were REJECTED local_busy across ${ROUNDS} burst rounds (admission cap 2 + queue 8); max queue wait ${maxWaitTotal}ms`
        : `no local_busy; ${reqTotal} teammate turns absorbed, max queue wait ${maxWaitTotal}ms` },
    thinking: { scenariosPerRound: 6,
      problems: report.thinking.problems },
    unexpected: report.unexpected,
  };
  if (JSON_OUT) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`\n=== Agent Teams x ${ROUNDS} rounds (burst) ===`);
    for (const r of report.agent_teams) console.log(`  ${r.mode.padEnd(14)} teammates=${r.teammates} burst=${r.burst} ok=${r.ok}/${r.totalRequests} local_busy=${r.localBusy} maxWait=${r.maxQueueWaitMs}ms`);
    console.log(`\n=== Thinking x ${ROUNDS} rounds (6 scenarios each) ===`);
    const byScenario = {};
    for (const r of report.thinking.rounds) for (const f of r.findings) {
      const key = f.text.split(':')[0];
      (byScenario[key] = byScenario[key] || new Set()).add(`${f.severity}: ${f.text}`);
    }
    for (const set of Object.values(byScenario)) for (const line of set) console.log(`  ${line}`);
    const problems = report.unexpected.concat(report.thinking.problems);
    console.log(`\n=== Problems (unexpected) ===\n${problems.map(p => `  - ${p}`).join('\n') || '  (none)'}`);
    console.log(`\n${report.summary.agent_teams.headline}`);
  }
  const failed = report.unexpected.length > 0 || report.thinking.problems.length > 0;
  process.exitCode = failed ? 1 : 0;
}

main().catch(error => { fatal(error); process.exitCode = 1; });
