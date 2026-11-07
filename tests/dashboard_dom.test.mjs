// SPDX-License-Identifier: MIT
// Runs the dashboard page script against a minimal DOM stub so the rendering layer is
// covered without a browser: cards, alert de-duplication, summary and copy buttons.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import vm from 'node:vm';

// Run the exact scripts embedded in the served pages, including computed Python appends.
const ASSETS = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-c',
  'import json,re,routeDashboard as d; print(json.dumps({name:re.search(r"<script>(.*)</script>",page,re.S).group(1) for name,page in (("dashboard",d.PAGE),("settings",d.SETTINGS_PAGE),("blackbox",d.BLACKBOX_PAGE),("usage",d.USAGE_PAGE))} | {"settings_html":d.SETTINGS_PAGE}))'],
{ cwd: new URL('..', import.meta.url), encoding: 'utf-8' }));

function scriptBundle() {
  return ASSETS.dashboard;
}

function fakeElement(id = '') {
  const element = {
    id, innerHTML: '', textContent: '', className: '', title: '', value: '', checked: false,
    dataset: {}, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(child) { element.children.push(child); return child; },
    prepend(child) { element.children.unshift(child); return child; },
    remove() {}, addEventListener() {}, removeEventListener() {},
    querySelectorAll: () => [], querySelector: () => null, closest: () => null,
    contains: () => false, dispatchEvent() {}, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; }, getAttribute(name) { return this.attributes[name] ?? null; },
  };
  return element;
}

function buildContext(hash = '#token=test-token', fetchImpl = async () => ({ ok: true, json: async () => ({}) })) {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, fakeElement(id));
    return nodes.get(id);
  };
  const document = {
    hidden: false,
    getElementById: get,
    createElement: () => fakeElement(),
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: fakeElement('body'),
  };
  const sessionStorage = {
    _map: new Map(),
    getItem(key) { return this._map.has(key) ? this._map.get(key) : null; },
    setItem(key, value) { this._map.set(key, String(value)); },
    removeItem(key) { this._map.delete(key); },
    clear() { this._map.clear(); },
  };
  return {
    nodes, document, window: { addEventListener() {} },
    location: { hash, pathname: '/', search: '' },
    history: { replaceState() {} },
    sessionStorage,
    fetch: fetchImpl,
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    console, navigator: { clipboard: { writeText: async () => {} } }, Event: class {},
  };
}

function fixture(overrides = {}) {
  return {
    generated_at: '2026-10-25T08:00:00+00:00', model: 'vendor/model', configured: true,
    refresh_ms: 2000, proxy_stale: false, summary: 'OpenRouter로 정상 처리 중 · 오늘 1,000토큰 · $0.01',
    insights: [{ level: 'warn', title: '실행기 코드가 오래됨', detail: '재시작하면 반영됩니다',
      action: '.\\OpenClaude.ps1', evidence: ['재시작 필요'], score: 90, code: 'proxy_stale' }],
    alerts: [
      { level: 'warn', title: '실행기 재시작 필요', detail: '오래된 코드', code: 'proxy_stale',
        command: '.\\OpenClaude.ps1' },
      { level: 'critical', title: 'OpenRouter 잔액 부족', detail: '남은 금액 $1.00', code: 'low_balance' },
    ],
    routing: {
      state: 'external', last: { ts: '2026-10-25T08:00:00+00:00', provider: 'openrouter',
        reason: 'replacement', model: 'vendor/model', age_seconds: 12 },
      window_minutes: 60, counts: { external: 10, plan: 0 }, plan_reasons: [], failures: [],
      models: [['external:vendor/model', 10]], circuit_opens: 0, proxy_started_at: '2026-10-25T07:00:00+00:00',
      proxy_code: '2026-10-25T06:00:00.000Z',
      short: { minutes: 10, counts: { external: 4, plan: 0 }, plan_reasons: [], failures: [], circuit_opens: 0 },
      transitions: [], recent: [],
    },
    usage: { totals: { external: [1000, 100, 0, 0], plan: [0, 0, 0, 0] },
      today: { external: [1000, 100, 0, 0], plan: [0, 0, 0, 0] }, cost_usd: 0.01,
      current_hour: '2026-10-25T08', timeline: [], sessions: [], session_count: 0 },
    account: { ok: true, limit_usd: 50, spent_usd: 1, remaining_usd: 49, reset: 'monthly' },
    pricing: { ok: true }, proxy_metrics: null, proxy_modified_at: '2026-10-25T07:30:00+00:00',
    help: { reasons: {}, commands: [], endpoints: [], states: [], exit_codes: [], sources: [] },
    ...overrides,
  };
}

function render(data) {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  sandbox.render(data);
  return context.nodes;
}

test('usage statistics distinguish unreported tokens and show observed session scope', () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  vm.createContext(sandbox); vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  sandbox.renderUsage({status:'ready',report:{collection_started:'2026-10-29T00:00:00Z',rows:[
    {bucket:'2026-10-29',provider:'cli',requested_model:'model-a',reported_model:null,instance_id:'12345678-abcd',requests:2,failed:0,input_samples:1,input_tokens:0,output_samples:0,output_tokens:null,mean_elapsed_ms:120},
  ]},sessions:[{instance_id:'12345678-abcd',provider:'cli',status:'active',inflight:1,queued:0,requests:2,failed:0,p95_ms:120,recording:{enabled:true,dropped:0,errors:0,pending:0}}]});
  const html=context.nodes.get('usage-grid').innerHTML;
  assert.match(html,/model-a/); assert.match(html,/미보고/); assert.match(html,/0 <small>\(1\/2\)/);
  assert.match(html,/실행기별 현재 상태/); assert.match(html,/12345678/);
  assert.match(context.nodes.get('usage-status').textContent,/첫 관측/);
  sandbox.renderUsage({status:'ready',report:{coverage:'partial',rows:[]},sessions:[]});
  assert.match(context.nodes.get('usage-status').textContent,/누락/);
});

test('usage telemetry exposes host resources, complete-session admission reasons and coverage', () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  vm.createContext(sandbox); vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  sandbox.renderUsage({status:'ready',report:{rows:[]},sessions:[],aggregate_sessions:{active_instances:40,observed_instances:40,
    partial:false,requests:80,inflight:3,rss_bytes:4000000,admission_active:3,admission_max:128,
    admission_inflight_bytes:8192,admission_max_bytes:67108864,admission_rejected_concurrency:9,
    admission_rejected_bytes:2,local_rejected:4,cli_rejected:1,
    metric_coverage:{requests:{reported:40,active:40,observed:40},rss_bytes:{reported:40,active:40,observed:40}},by_provider:{}},
    host_resources:{sampled_at:new Date().toISOString(),source:'windows_api',cpu_percent:55,memory_percent:70,
      memory_used_bytes:700,memory_available_bytes:300,error:null,cpu_error:null,memory_error:null}});
  const html=context.nodes.get('usage-grid').innerHTML;
  assert.match(html,/40개 인증 실행기/);
  assert.match(html,/55%/); assert.match(html,/70%/);
  assert.match(html,/요청 동시 3\/128/);
  assert.match(html,/동시 429 9 · 본문 예산 503 2/);
  assert.match(html,/Local 대기 4 · CLI 429 1/);
  assert.match(html,/요청 40\/40 · RSS 40\/40/);
});

test('usage warns about legacy unverified model provenance only when present', () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  vm.createContext(sandbox); vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  sandbox.renderUsage({status:'ready',report:{rows:[],model_provenance_unverified:3},sessions:[]});
  assert.match(context.nodes.get('usage-status').textContent,/\uC774\uC804 \uC218\uC9D1 \uD615\uC2DD 3/);
  sandbox.renderUsage({status:'ready',report:{rows:[],model_provenance_unverified:0},sessions:[]});
  assert.doesNotMatch(context.nodes.get('usage-status').textContent,/\uC2E4\uC81C \uBAA8\uB378 \uCD9C\uCC98 \uBBF8\uD655\uC778/);
});

test('usage labels the fetch time without claiming it is collection time', async () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  sandbox.fetch=async()=>({ok:true,json:async()=>({status:'ready',report:{rows:[]},sessions:[]})});
  vm.createContext(sandbox); vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  await sandbox.loadUsage();
  assert.match(context.nodes.get('usage-status').textContent, /조회 시각 .*수집 완료 시각 아님/);
});

test('usage preserves same-period results when refresh fails but clears an old period', async () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  sandbox.fetch=async()=>({ok:true,json:async()=>({status:'ready',report:{rows:[],collection_started:'2026-10-29T00:00:00Z'},sessions:[]})});
  vm.createContext(sandbox); vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  await sandbox.loadUsage(); const grid=context.nodes.get('usage-grid'); const previous=grid.innerHTML;
  sandbox.fetch=async()=>{throw new Error('unavailable');};
  await sandbox.loadUsage();
  assert.equal(grid.innerHTML,previous);
  assert.match(context.nodes.get('usage-status').textContent,/새 조회 실패 · 마지막 조회/);
  context.nodes.get('usage-period').value='week';
  await sandbox.loadUsage();
  assert.equal(grid.innerHTML,'');
  assert.match(context.nodes.get('usage-status').textContent,/불러오지 못했습니다/);
});

test('usage polling is bounded and pauses while the page is hidden', async () => {
  const context=buildContext(), handlers={}, timers=[];
  context.document.addEventListener=(event,handler)=>{handlers[event]=handler;};
  context.setTimeout=(fn,delay)=>{timers.push({fn,delay});return timers.length;};
  const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  let calls=0;
  sandbox.fetch=async()=>{calls++;return {ok:true,json:async()=>({status:'ready',report:{rows:[]},sessions:[]})};};
  vm.createContext(sandbox);vm.runInContext(ASSETS.usage,sandbox,{filename:'usage.js'});
  await sandbox.pollUsage();
  assert.equal(timers.at(-1).delay,30000);
  context.document.hidden=true;handlers.visibilitychange();const before=calls;
  await sandbox.pollUsage();assert.equal(calls,before);
  context.document.hidden=false;handlers.visibilitychange();
  await Promise.resolve();await Promise.resolve();
  assert.equal(calls,before+1);
});

test('flow header and proxy share the observed state on the first snapshot', () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  vm.createContext(sandbox); vm.runInContext(scriptBundle(),sandbox);
  sandbox.render(fixture({active_session:{provider:'cli'},proxy_metrics:{startedAt:'session',requests:1,inflightRequests:1,configuration:{provider:'cli'}}}));
  const html=context.nodes.get('grid').innerHTML;
  assert.match(html, /class="flow-count">프록시 요청 진행 중/);
  assert.match(html, /Claude-Cheat 프록시<\/b><span>프록시 요청 진행 중<\/span>/);
});

test('settings describe next-session effects without inventing CLI actual model', () => {
  const context=buildContext(); const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;
  vm.createContext(sandbox); vm.runInContext(ASSETS.settings,sandbox,{filename:'settings.js'});
  context.nodes.get('default-provider').value='local';
  vm.runInContext("SESSION_DIAGNOSTICS = {session:{active:true,provider:'cli',requested_model:'model-a',loaded_model:null}}; DIRTY.add('local:LOCAL_LLM_MODEL');",sandbox);
  sandbox.renderSessionImpact();
  const message=context.nodes.get('default-session-impact').textContent;
  assert.match(message,/프록시 세션: CLI/); assert.match(message,/요청 모델 model-a/);
  assert.match(message,/실제 모델 미보고/); assert.match(message,/선택한 다음 실행: Local LLM/);
  assert.match(message,/실행 중인 세션의 모델은 바뀌지/); assert.match(message,/이어가면 모델별 도구·문맥 해석/);
  assert.match(context.nodes.get('impact-local').textContent,/저장하기 전/);
  vm.runInContext('SESSION_DIAGNOSTICS = null;',sandbox); sandbox.renderSessionImpact();
  assert.match(context.nodes.get('default-session-impact').textContent,/프록시 세션을 확인하지 못했습니다/);
  assert.match(context.nodes.get('default-session-impact').textContent,/직접 실행한 Sonnet/);
});

test('settings distinguish connection checks from tool and Claude Code capability', () => {
  assert.match(ASSETS.settings_html, /모델 확인과 단순 추론은 도구 왕복이나 Claude Code 기능을 검증하지 않습니다/);
});

test('tool probe uses saved settings and separates tool success from model mismatch', async () => {
  const page = await settingsPage({config:{openrouter:READY_OPENROUTER, local:READY_LOCAL}});
  const calls=[];
  page.sandbox.fetch=async (url, options) => {
    calls.push({url, payload:JSON.parse(options.body)});
    return {json:async()=>({ok:true,reason:'passed',tool_proposal_valid:true,
      first_model_matches_requested:false,second_model_matches_requested:null,
      first_usage_reported:true,second_usage_reported:false})};
  };
  await vm.runInContext("runToolRoundtrip('local')", page.sandbox);
  assert.deepEqual(calls, [{url:'/api/test',payload:{provider:'local',mode:'tool_roundtrip'}}]);
  const html=page.nodes.get('res-local').innerHTML;
  assert.match(html,/도구 왕복 통과/); assert.match(html,/첫 응답 불일치/);
  assert.match(html,/두 번째 응답 미확인/); assert.match(html,/result-line warn/);
  assert.match(html,/현재 Claude Code 세션과 원본 Sonnet 동등성은 검증하지 않습니다/);
  vm.runInContext("renderResult('local')", page.sandbox);
  assert.match(page.nodes.get('res-local').innerHTML,/도구 왕복 통과/);
});

test('tool probe refuses unsaved changes without calling the API', async () => {
  const page = await settingsPage({config:{openrouter:READY_OPENROUTER, local:READY_LOCAL}});
  vm.runInContext("DIRTY.add('local:LOCAL_LLM_MODEL')", page.sandbox);
  const before=page.requests.length;
  await vm.runInContext("runToolRoundtrip('local')", page.sandbox);
  assert.equal(page.requests.length,before);
  assert.match(page.nodes.get('res-local').innerHTML,/저장한 뒤 검사/);
});

test('blackbox distinguishes waiting, connected and unavailable without showing content', () => {
  const context = buildContext();
  const sandbox = {...context, globalThis: null};
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(ASSETS.blackbox, sandbox, {filename:'blackbox.js'});
  const status = context.nodes.get('blackbox-status');
  sandbox.renderBlackbox({active:true, mode:'detailed', detail_status:'waiting', incidents:0});
  assert.match(status.textContent, /새 세션 연결 대기/);
  assert.match(status.textContent, /기록을 종료하고 다시 시작한 뒤 새 세션/);
  sandbox.renderBlackbox({active:true, mode:'detailed', detail_status:'connected', details_available:true, incidents:1, content:'PRIVATE'});
  assert.match(status.textContent, /새 세션 연결됨/);
  assert.match(status.textContent, /상세 내용 저장됨/);
  assert.doesNotMatch(status.textContent, /PRIVATE/);
  sandbox.renderBlackbox({active:true, mode:'detailed', detail_status:'unavailable', incidents:1});
  assert.match(status.textContent, /상세 수집 불가/);
  sandbox.renderBlackbox({active:true, mode:'metadata', incidents:2, discarded:3, started_at:'2026-10-25T07:00:00+00:00'});
  assert.match(status.textContent, /기록 중/);
  assert.match(status.textContent, /시작/);
  assert.match(status.textContent, /폐기 3건/);
});

test('separate blackbox page uses existing authenticated recording actions', async () => {
  const context = buildContext();
  const calls = [];
  context.window.confirm = () => true;
  context.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok:true, json:async () => ({active:true, mode:'detailed', incidents:0})};
  };
  const sandbox = {...context, globalThis:null};
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(ASSETS.blackbox, sandbox, {filename:'blackbox.js'});
  await Promise.resolve();
  context.document.getElementById('blackbox-detailed').checked = true;
  await sandbox.blackboxAction('start');
  await sandbox.blackboxAction('mark');
  await sandbox.blackboxAction('stop');
  const writes = calls.filter(call => call.options.method === 'POST');
  assert.deepEqual(writes.map(call => JSON.parse(call.options.body).action), ['start','mark','stop']);
  assert.equal(JSON.parse(writes[0].options.body).mode, 'detailed');
  assert.ok(writes.every(call => call.url === '/api/blackbox' && call.options.headers['x-dashboard-token']));
});

test('dashboard renders the insights card with its evidence and copy action', () => {
  const nodes = render(fixture());
  const grid = nodes.get('grid').innerHTML;
  assert.match(grid, /인사이트/);
  assert.match(grid, /실행기 코드가 오래됨/);
  assert.match(grid, /재시작 필요/);
  assert.match(grid, /data-copy="\.\\OpenClaude\.ps1"/);
  assert.match(grid, /지금 상태/);
});

test('performance card shows measured throughput and completed-request failure rate', () => {
  const data = fixture({proxy_metrics: {
    startedAt: '2026-10-25T07:00:00+00:00', uptimeMs: 3600000, requests: 60, ok: 45, failed: 15, aborted: 10,
    configuration: {provider: 'local'}, routes: {external: 40, plan: 20},
    latencyMs: {external: {p50: 100, p95: 500, count: 40}, plan: {p50: 200, p95: 600, count: 20}, ttfb: {p50: 80, p95: 400, count: 40}},
    modelPerformance: {local: {samples: 2, outputTokens: 40, elapsedMs: 4000}},
  }});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /1\.00 건\/분/);
  assert.match(html, /10\.0% \(5 \/ 50건 완료\)/);
  assert.match(html, /완료 50건 · 진행/);
  assert.match(html, /10\.00 토큰\/초 \(2건\)/);
  assert.match(html, /첫 바이트는 첫 토큰과 다릅니다/);
});

test('unreported model throughput is not presented as zero', () => {
  const html = render(fixture({proxy_metrics: {startedAt:'2026-10-25T07:00:00+00:00', uptimeMs:3600000,
    requests:0, ok:0, failed:0, configuration:{provider:'local'}}})).get('grid').innerHTML;
  assert.match(html, /미측정/);
});

test('overview presents live model and key metrics before detailed insights', () => {
  const data = fixture({ active_session: {provider: 'local'}, proxy_metrics: {
    stale: false, startedAt: '2026-10-25T07:00:00+00:00', uptimeMs: 3600000,
    requests: 60, ok: 45, failed: 5, configuration: {provider: 'local', model: 'fixture/model'},
    latencyMs: {external: {p50:1000,p95:2500,count:40}},
    modelPerformance: {local:{samples:2,outputTokens:40,elapsedMs:4000}},
  }});
  const html = render(data).get('grid').innerHTML;
  assert.ok(html.indexOf('한눈에 보기') < html.indexOf('인사이트'));
  assert.match(html, /로드된 외부 모델 · fixture\/model/);
  assert.match(html, /10\.0% \(5 \/ 50건 완료\)/);
  assert.match(html, /10\.00 토큰\/초 \(2건\)/);
  assert.match(html, /2\.50초/);
});

test('overview does not mistake historical metrics for a live session', () => {
  const html = render(fixture({active_session:null, proxy_metrics:{stale:true, requests:99,
    configuration:{provider:'local',model:'old/model'}}})).get('grid').innerHTML;
  const overview = html.slice(html.indexOf('한눈에 보기'), html.indexOf('지금 상태'));
  assert.match(overview, /현재 세션 데이터 없음/);
  assert.match(overview, /미측정/);
  assert.doesNotMatch(overview, /old\/model/);
});

test('routing share and time-series charts use actual data and expose accessible detail', () => {
  const base = fixture();
  base.routing.counts = {external:3, plan:1};
  base.usage.timeline = [
    {hour:'2026-10-25T07', external:{tokens:[100,20,0,0]}, plan:{tokens:[50,0,0,0]}},
    {hour:'2026-10-25T08', external:{tokens:[0,0,0,0]}, plan:{tokens:[10,0,0,0]}}
  ];
  const html = render(base).get('grid').innerHTML;
  assert.match(html, /aria-label="외부 모델 3건 75%, Plan 1건 25%"/);
  assert.match(html, /시간당 최대 170 토큰/);
  assert.match(html, /시간별 상세 보기/);
  assert.match(html, /2026-10-25T07/);
  assert.ok(html.indexOf('시간대별 토큰 사용량') < html.indexOf('지금 상태'));
});

test('help content remains untouched on unchanged snapshots and refreshes when help changes', () => {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, {filename:'dashboard.js'});
  const data = fixture();
  sandbox.render(data);
  const help = context.nodes.get('help');
  help.innerHTML = 'user-opened-detail';
  sandbox.render(data);
  assert.equal(help.innerHTML, 'user-opened-detail');
  data.help = {...data.help, commands:[['help command', 'changed']]};
  sandbox.render(data);
  assert.match(help.innerHTML, /help command/);
});

test('chart detail remains open when a live snapshot rerenders the dashboard', () => {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, {filename:'dashboard.js'});
  const data = fixture();
  data.usage.timeline = [{hour:'2026-10-25T07', external:{tokens:[5,0,0,0]}, plan:{tokens:[0,0,0,0]}}];
  sandbox.render(data);
  assert.match(context.nodes.get('grid').innerHTML, /id="chart-details" class="chart-details"><summary>/);
  context.nodes.get('chart-details').open = true;
  sandbox.render(data);
  assert.match(context.nodes.get('grid').innerHTML, /id="chart-details" class="chart-details" open><summary>/);
  context.nodes.get('chart-details').open = false;
  sandbox.render(data);
  assert.doesNotMatch(context.nodes.get('grid').innerHTML, /id="chart-details" class="chart-details" open>/);
});

test('charts do not fabricate data for empty or unmeasured sessions', () => {
  const base = fixture();
  base.routing.counts = {external:0, plan:0};
  let html = render(base).get('grid').innerHTML;
  assert.match(html, /최근 구간의 라우팅 로그가 없습니다/);
  assert.match(html, /최근 24시간에 표시할 토큰 사용 기록이 없습니다/);
  base.active_session = {provider:'cli'};
  html = render(base).get('grid').innerHTML;
  assert.match(html, /CLI 토큰 시계열 미보고/);
  assert.match(html, /입력 사용량 보고 <b>미보고/);
});

test('CLI live metrics show measured usage coverage and distinguish failures from Plan', () => {
  const history = [
    {minute:'2026-10-25T07:10:00Z',completed:2,ok:2,failed:0,aborted:0,latencyMsSum:2000,tokenSamples:1,outputTokens:0,tokenElapsedMs:1000,
      cliExternalSuccess:2,cliInputSamples:1,cliOutputSamples:1,cliInputTokens:40,cliOutputTokens:0},
    {minute:'2026-10-25T07:11:00Z',completed:1,ok:1,failed:0,aborted:0,latencyMsSum:1000,tokenSamples:0,outputTokens:0,tokenElapsedMs:0,
      cliExternalSuccess:1,cliInputSamples:0,cliOutputSamples:0,cliInputTokens:0,cliOutputTokens:0},
  ];
  const data = fixture({active_session:{provider:'cli'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',requests:3,ok:3,failed:0,
    configuration:{provider:'cli'},monitoringHistory:history,cliActivity:{active:1,queued:0,limit:4,rejected:2},
    cliFailures:{timeout:1,status429:2,status4xx:1,status5xx:0,validation:0,connection:0},
    cliUsage:{responses:3,inputSamples:1,inputTokens:40,outputSamples:1,outputTokens:0},
    cliProcessMs:{count:2,p50:410,p95:600,max:620}}});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /CLI 프록시 진행 1건 · 백엔드 동시 한도 4/);
  assert.match(html, /CLI 별도 대기열 없음/);
  assert.match(html, /CLI 시도 실패 횟수이며 Plan 우회 요청 수와 다릅니다/);
  assert.match(html, /CLI 토큰 사용량 추이/);
  assert.match(html, /1 \/ 3건 \(33\.3%\)/);
  assert.match(html, /0 · 1건/); // measured zero differs from unreported
  assert.match(html, /백엔드 내부 제한 시간 초과와 검증 오류는 4xx에 포함될 수 있습니다/);
  assert.match(html, /CLI 시도 실패 횟수이며 Plan 우회 요청 수와 다릅니다/);
  assert.match(html, /미보고 구간은 0으로 표시하지 않습니다/);
  assert.match(html, /CLI 프로세스 실행 · 성공 응답만/);
  assert.match(html, /410 \/ 600 \/ 620 ms \(2건\)/);
  assert.match(html, /순수 모델 추론 시간이 아닙니다/);
  delete data.proxy_metrics.cliProcessMs;
  assert.match(render(data).get('grid').innerHTML, /CLI 프로세스 실행 · 성공 응답만<\/span><span class="v">미보고/);
});

test('diagnostics are grouped below live charts and disclosure choices survive polling', () => {
  const context = buildContext();
  const sandbox = {...context};
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox);
  const data = fixture();
  sandbox.render(data);
  let html = context.nodes.get('grid').innerHTML;
  assert.ok(html.indexOf('최근 요청 추이') < html.indexOf('id="runtime-details"'));
  assert.ok(html.indexOf('시간대별 토큰 사용량') < html.indexOf('id="runtime-details"'));
  for (const id of ['runtime-details', 'activity-details', 'flow-notes']) {
    assert.doesNotMatch(html, new RegExp(`id="${id}"[^>]* open>`));
    context.nodes.get(id).open = true;
  }
  let focused = false;
  const summary = context.document.getElementById('runtime-details-toggle');
  summary.focus = options => { focused = options.preventScroll === true; };
  context.document.activeElement = summary;
  sandbox.render(data);
  html = context.nodes.get('grid').innerHTML;
  for (const id of ['runtime-details', 'activity-details', 'flow-notes']) {
    assert.match(html, new RegExp(`id="${id}"[^>]* open>`));
    context.nodes.get(id).open = false;
  }
  assert.equal(focused, true, 'keyboard focus stays on the disclosure without scrolling');
  sandbox.render(data);
  html = context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html, /id="runtime-details"[^>]* open>/);
  assert.doesNotMatch(html, /id="activity-details"[^>]* open>/);
});

test('traffic light reflects current route and does not treat historical failures as live warnings', () => {
  const base = fixture({active_session:{provider:'local'}, proxy_metrics:{stale:false}, proxy_stale:false});
  base.routing.last = {provider:'local',reason:'replacement',age_seconds:1};
  let nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-green/);
  assert.match(nodes.get('live-pill').getAttribute('aria-label'), /라우팅 상태/);
  base.insights = [{code:'failed_requests',level:'warn'}];
  nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-green/);
  base.routing.state = 'plan'; base.routing.last.provider = 'plan';
  nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-red/);
  assert.equal(nodes.get('live-text').textContent, 'Plan 우회 중');
  base.routing.state = 'external'; base.routing.last.provider = 'local'; base.proxy_stale = true;
  nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-warn/);
  assert.match(nodes.get('live-text').textContent, /갱신 필요/);
  base.proxy_stale = false; base.routing.state = 'idle';
  nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-neutral/);
  base.routing.state = 'external'; base.active_session = null;
  nodes = render(base);
  assert.match(nodes.get('live-pill').className, /signal-neutral/);
  assert.equal(nodes.get('live-text').textContent, '상태 미확인');
});

test('request history plots observed values without connecting a missing minute', () => {
  const bucket = (minute, completed, ok, failed, aborted, latencyMsSum) => ({minute,completed,ok,failed,aborted,latencyMsSum,tokenSamples:0,outputTokens:0,tokenElapsedMs:0});
  const data = fixture({active_session:{provider:'local'}, proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',uptimeMs:3600000,requests:5,ok:4,failed:1,configuration:{provider:'local'},monitoringHistory:[
    bucket('2026-10-25T07:10:00Z',2,2,0,0,2000),bucket('2026-10-25T07:12:00Z',3,2,1,0,6000)]}});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /최근 요청 추이/);
  assert.match(html, /분당 완료 · 취소 포함/);
  assert.match(html, /2000\.00 ms/);
  assert.match(html, /33\.33 %/);
  assert.doesNotMatch(html, /<line x1=/);
  assert.match(html, /분별 측정값 보기/);
});

test('token speed history uses only buckets with measured usage', () => {
  const bucket = (minute, tokenSamples, outputTokens, tokenElapsedMs) => ({minute, completed:1,ok:1,failed:0,aborted:0,latencyMsSum:1000,tokenSamples,outputTokens,tokenElapsedMs});
  const data = fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z', requests:2,ok:2,failed:0,configuration:{provider:'local'},monitoringHistory:[
    bucket('2026-10-25T07:10:00Z',0,0,0),bucket('2026-10-25T07:11:00Z',1,40,2000)]}});
  const html=render(data).get('grid').innerHTML;
  assert.match(html, /관측 출력 속도/);
  assert.match(html, /20\.00 토큰\/초/);
  assert.match(html, /사용량이 보고된 성공 응답만/);
});

test('flow distinguishes measured token deltas from unavailable usage and preserves routing semantics', () => {
  const context = buildContext(); context.window.innerHeight = 900; context.window.matchMedia = () => ({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect = () => ({top:100,bottom:200});
  const sandbox = {...context, globalThis:null}; sandbox.globalThis = sandbox;
  vm.createContext(sandbox); vm.runInContext(scriptBundle(),sandbox);
  const stats = (requests, flow, started='2026-10-25T07:00:00Z') => fixture({active_session:{provider:'local'},
    proxy_metrics:{startedAt:started,requests,ok:0,failed:0,configuration:{provider:'local'},tokenFlow:{local:flow}}});
  const base={responses:2,inputSamples:2,inputTokens:100,outputSamples:1,outputTokens:20};
  sandbox.render(stats(5,base));
  let html=context.nodes.get('grid').innerHTML;
  assert.match(html, /<svg class="flow-illustration"[^>]*aria-hidden="true"[^>]*focusable="false"/);
  assert.match(html,/Claude-Cheat 프록시/); assert.match(html,/첫 관측 · 다음 갱신부터 비교/);
  assert.match(html,/Local LLM/); assert.match(html,/Claude Plan/);
  assert.match(html,/첫 관측 · 다음 갱신 대기/);
  sandbox.render(stats(7,{responses:3,inputSamples:3,inputTokens:100,outputSamples:2,outputTokens:520}));
  html=context.nodes.get('grid').innerHTML;
  assert.match(html,/관측 입력 토큰<\/span><strong>0 토큰 · 1건 보고/);
  assert.match(html,/관측 출력 토큰<\/span><strong>500 토큰 · 1건 보고/);
  assert.match(html,/width:5%/); assert.match(html,/입력은 별도 캐시 토큰을 더하지 않습니다/);
  assert.equal((html.match(/class="flow-particle"/g)||[]).length,0);
  assert.equal((html.match(/class="flow-particle flow-response"/g)||[]).length,18);
  sandbox.render(stats(8,{responses:4,inputSamples:3,inputTokens:100,outputSamples:2,outputTokens:520}));
  html=context.nodes.get('grid').innerHTML; assert.match(html,/미보고/);
  sandbox.render(stats(1,{responses:0,inputSamples:0,inputTokens:0,outputSamples:0,outputTokens:0},'2026-10-25T08:00:00Z'));
  html=context.nodes.get('grid').innerHTML; assert.match(html,/첫 관측 · 다음 갱신 대기/);
  assert.doesNotMatch(html,/class="flow-particle"/);
});

test('flow does not invent token amounts for old proxies or replay counts after reset', () => {
  const context = buildContext(); context.window.innerHeight=900; context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const stats=requests=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',requests,configuration:{provider:'local'}}});
  sandbox.render(stats(5));sandbox.render(stats(8));let html=context.nodes.get('grid').innerHTML;
  assert.match(html,/미확인/);assert.equal((html.match(/class="flow-particle"/g)||[]).length,0);
  sandbox.render(stats(2));html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/class="flow-particle"/);assert.match(html,/미확인/);
});

test('request flow animates only measured increments and respects visibility', () => {
  const context = buildContext();
  context.window.innerHeight = 900;
  context.window.matchMedia = () => ({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect = () => ({top:100,bottom:200});
  const sandbox = {...context, globalThis:null}; sandbox.globalThis = sandbox;
  vm.createContext(sandbox); vm.runInContext(scriptBundle(),sandbox);
  const metric = requests => ({active_session:{provider:'local'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',requests,ok:0,failed:0,configuration:{provider:'local'}}});
  sandbox.render(fixture(metric(5)));
  assert.doesNotMatch(context.nodes.get('grid').innerHTML, /class="flow-particle"/);
  sandbox.render(fixture(metric(25)));
  let html=context.nodes.get('grid').innerHTML;
  assert.equal((html.match(/class="flow-particle"/g)||[]).length,0);
  assert.match(html, /20건 요청 감지/);
  context.document.hidden=true;
  sandbox.render(fixture(metric(26)));
  assert.doesNotMatch(context.nodes.get('grid').innerHTML, /class="flow-particle"/);
  context.document.hidden=false;
  context.window.matchMedia=()=>({matches:true});
  sandbox.render(fixture(metric(27)));
  assert.doesNotMatch(context.nodes.get('grid').innerHTML, /class="flow-particle"/);
});

test('request flow does not replay arrivals accumulated while the tab was hidden', () => {
  const context = buildContext();
  const handlers = {};
  context.document.addEventListener = (type, handler) => { handlers[type] = handler; };
  context.window.innerHeight = 900;
  context.window.matchMedia = () => ({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect = () => ({top:100,bottom:200});
  const sandbox = {...context, globalThis:null}; sandbox.globalThis = sandbox;
  vm.createContext(sandbox); vm.runInContext(scriptBundle(),sandbox);
  const metric = requests => ({active_session:{provider:'local'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',requests,ok:0,failed:0,configuration:{provider:'local'}}});
  sandbox.render(fixture(metric(5)));
  context.document.hidden = true; handlers.visibilitychange();
  context.document.hidden = false; handlers.visibilitychange();
  sandbox.render(fixture(metric(25)));
  assert.doesNotMatch(context.nodes.get('grid').innerHTML, /class="flow-particle"/);
  sandbox.render(fixture(metric(26)));
  assert.equal((context.nodes.get('grid').innerHTML.match(/class="flow-particle"/g)||[]).length,0);
});

test('request flow establishes a fresh baseline after dashboard reconnection', async () => {
  const context=buildContext(); context.window.innerHeight=900; context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null}; sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  await new Promise(resolve=>setImmediate(resolve));
  const data=(requests,responses,tokens)=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'2026-10-25T07:00:00Z',requests,ok:0,failed:0,
    configuration:{provider:'local'},tokenFlow:{local:{responses,inputSamples:responses,inputTokens:tokens,outputSamples:responses,outputTokens:tokens}}}});
  sandbox.render(data(5,5,50));
  sandbox.fetch=async()=>{throw new Error('connection failed')};
  await sandbox.tick();
  sandbox.fetch=async()=>({ok:true,json:async()=>data(105,105,1050)});
  await sandbox.tick();
  let html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/class="flow-particle/);assert.match(html,/첫 관측 · 다음 갱신 대기/);
  sandbox.fetch=async()=>({ok:true,json:async()=>data(106,106,1060)});
  await sandbox.tick();html=context.nodes.get('grid').innerHTML;
  assert.equal((html.match(/class="flow-particle"/g)||[]).length,7);
  assert.match(html,/10 토큰 · 1건 보고/);
});

test('an alert that shares an insight code is not repeated', () => {
  const nodes = render(fixture());
  const alerts = nodes.get('alerts').innerHTML;
  assert.match(alerts, /OpenRouter 잔액 부족/);
  assert.doesNotMatch(alerts, /실행기 재시작 필요/);
});

test('the header shows the generated summary', () => {
  const nodes = render(fixture());
  assert.match(nodes.get('stamp').textContent, /OpenRouter로 정상 처리 중/);
  assert.match(nodes.get('stamp').title, /vendor\/model/);
});

test('an unconfigured install still offers the setup guide', () => {
  const nodes = render(fixture({ configured: false }));
  assert.match(nodes.get('alerts').innerHTML, /처음 설정하기/);
});

test('idle routing renders without an insight list', () => {
  const nodes = render(fixture({ insights: [], alerts: [], summary: '세션 대기 중' }));
  assert.match(nodes.get('grid').innerHTML, /조치할 항목 없음/);
  assert.equal(nodes.get('alerts').innerHTML, '');
});

// ---- settings page: one service form at a time, chosen from a select box ----

function settingsBundle() {
  return ASSETS.settings;
}

const READY_OPENROUTER = { fields: {
  OPENROUTER_MODEL: { value: 'vendor/model', set: true },
  ANTHROPIC_AUTH_TOKEN: { secret: true, set: true, preview: 'sk-or-…abcd', value: '' } } };
const READY_LOCAL = { fields: {
  LOCAL_LLM_URL: { value: 'http://127.0.0.1:11434/v1/messages', set: true },
  LOCAL_LLM_MODEL: { value: 'llama', set: true } } };

async function settingsPage({ config, store = new Map(), blockedStorage = false, configStatus = 200,
  cli = [], cliStatus = 200 } = {}) {
  const context = buildContext();
  const handlers = {};
  const select = context.document.getElementById('provider-select');
  select.addEventListener = (type, handler) => { handlers[type] = handler; };
  const requests = [];
  context.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/cli' && cliStatus !== 200) {
      return { ok: false, status: cliStatus,
        clone() { return { json: async () => ({ error: 'token_required' }) }; },
        json: async () => ({ error: 'token_required' }) };
    }
    if (url === '/api/cli') return { ok: true, status: 200, json: async () => ({ kinds: cli }) };
    return { ok: configStatus === 200, status: configStatus, json: async () => config };
  };
  context.localStorage = blockedStorage
    ? { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } }
    : { getItem: key => (store.has(key) ? store.get(key) : null), setItem: (key, value) => store.set(key, value) };
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(settingsBundle(), sandbox, { filename: 'settings.js' });
  await new Promise(resolve => setImmediate(resolve));
  return { nodes: context.nodes, select, handlers, store, sandbox, requests };
}

const visible = nodes => ['openrouter', 'local'].filter(name => !nodes.get(`card-${name}`).hidden);

const DETECTED_CLIS = [
  { kind: 'codex', found: true, name: 'codex.exe',
    dir: 'C:\\Users\\fixture\\AppData\\Local\\Programs\\OpenAI\\Codex-cli\\bin', via: 'PATH', version: '0.166.0', reason: null },
  { kind: 'claude', found: true, name: 'claude.exe', dir: 'C:\\Users\\fixture\\.local\\bin', via: 'PATH',
    version: '2.1.316', reason: null },
];

test('CLI auto-detect fills the kind and executable path', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL }, cli: DETECTED_CLIS });
  await vm.runInContext('autoDetectCli()', page.sandbox);
  assert.equal(page.nodes.get('cfg-cli-CLI_KIND').value, 'codex');
  assert.equal(page.nodes.get('cfg-cli-CLI_BINARY').value,
    'C:\\Users\\fixture\\AppData\\Local\\Programs\\OpenAI\\Codex-cli\\bin\\codex.exe');
  assert.match(page.nodes.get('cli-detect-status').textContent, /codex/);
  assert.match(page.nodes.get('cli-detect-status').textContent, /0\.166\.0/);
  const cliCall = page.requests.find(request => request.url === '/api/cli');
  assert.ok(cliCall, 'auto-detect must re-query the CLI endpoint');
  assert.ok(cliCall.options.headers['x-dashboard-token'], 'detection carries the write token');
  assert.ok(page.requests.filter(request => request.url === '/api/cli').length >= 2,
    'the button refreshes detection instead of reusing the page-load result');
});

test('CLI auto-detect does not double a trailing directory separator', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL },
    cli: [{ kind: 'claude', found: true, name: 'claude.exe', dir: 'C:\\tools\\', via: 'PATH',
      version: '2.1.316', reason: null }] });
  await vm.runInContext('autoDetectCli()', page.sandbox);
  assert.equal(page.sandbox.document.getElementById('cfg-cli-CLI_BINARY').value, 'C:\\tools\\claude.exe');
});

test('CLI auto-detect keeps a user-pinned executable path', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL }, cli: DETECTED_CLIS });
  page.sandbox.document.getElementById('cfg-cli-CLI_BINARY').value = 'D:\\custom\\codex.exe';
  await vm.runInContext('autoDetectCli()', page.sandbox);
  assert.equal(page.nodes.get('cfg-cli-CLI_BINARY').value, 'D:\\custom\\codex.exe');
  assert.equal(page.nodes.get('cfg-cli-CLI_KIND').value, 'codex');
});

test('CLI auto-detect explains when no supported backend is installed', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL },
    cli: [{ kind: 'claude', found: false, name: null, dir: null, version: null, reason: 'not_found' }] });
  await vm.runInContext('autoDetectCli()', page.sandbox);
  assert.match(page.nodes.get('cli-detect-status').textContent, /감지된 CLI가 없습니다/);
  assert.equal(page.sandbox.document.getElementById('cfg-cli-CLI_KIND').value, '');
});

test('CLI detection explains a missing write token instead of an empty table', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL }, cliStatus: 403 });
  assert.match(page.nodes.get('cli-detect').innerHTML, /쓰기 토큰/);
  await vm.runInContext('autoDetectCli()', page.sandbox);
  assert.match(page.nodes.get('cli-detect').innerHTML, /쓰기 토큰/);
});

test('reset asks before posting and refreshes after a partial failure', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  assert.match(page.nodes.get('settings').innerHTML, /인증 정보 삭제/);
  assert.match(page.nodes.get('settings').innerHTML, /서비스 설정 초기화/);
  let confirmed = false;
  page.sandbox.window.confirm = text => { assert.match(text, /되돌릴 수 없습니다/); return confirmed; };
  const calls = [];
  page.sandbox.fetch = async (url, options = {}) => {
    calls.push({url, options});
    return {ok: true, json: async () => url === '/api/config-reset'
      ? {error:'reset_incomplete'} : {openrouter: READY_OPENROUTER, local: READY_LOCAL} };
  };
  await vm.runInContext("resetProvider('local','service')", page.sandbox);
  assert.equal(calls.length, 0);
  confirmed = true;
  await vm.runInContext("resetProvider('local','service')", page.sandbox);
  assert.deepEqual(calls.map(call => call.url), ['/api/config-reset', '/api/config']);
  assert.deepEqual(JSON.parse(calls[0].options.body), {provider:'local', scope:'service'});
  assert.ok(calls[0].options.headers['x-dashboard-token']);
  assert.ok(calls[1].options.headers['x-dashboard-token']);
  assert.match(page.nodes.get('res-local').innerHTML, /일부 파일만 변경/);
});

test('reset refuses while any setting has unsaved edits', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  vm.runInContext("DIRTY.add('local:LOCAL_LLM_MODEL')", page.sandbox);
  page.sandbox.window.confirm = () => { throw new Error('confirmation must not open'); };
  await vm.runInContext("resetProvider('openrouter','credential')", page.sandbox);
  assert.equal(page.requests.filter(request => request.url === '/api/config-reset').length, 0);
});

test('CLI settings explain backend support and the Grok limitation accurately', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const html = page.sandbox.cliExtra('cli');
  assert.match(html, /Claude Code에서 CLI 모델 백엔드를 사용할 수 있습니다/);
  assert.match(html, /Grok은 도구 격리가 검증되기 전까지/);
  assert.doesNotMatch(html, /요청 라우팅은 아직 지원하지 않습니다/);
});

test('settings show only the service that still needs configuration', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: { fields: {} } } });
  assert.deepEqual(visible(page.nodes), ['local']);
  assert.match(page.select.innerHTML, /value="local" selected/);
});

test('settings default to OpenRouter when every service is configured', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  assert.deepEqual(visible(page.nodes), ['openrouter']);
});

test('the last selection survives a reload', async () => {
  const store = new Map([['openclaude.provider', 'local']]);
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL }, store });
  assert.deepEqual(visible(page.nodes), ['local']);
});

test('changing the select switches the visible form without re-rendering it', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const before = page.nodes.get('settings').innerHTML;
  page.select.value = 'local';
  page.handlers.change();
  assert.deepEqual(visible(page.nodes), ['local']);
  assert.equal(page.store.get('openclaude.provider'), 'local');
  assert.equal(page.nodes.get('settings').innerHTML, before);
  // both forms stay in the DOM, so values typed into the hidden one are not lost
  assert.match(before, /id="card-openrouter"/);
  assert.match(before, /id="card-local"/);
});

test('the served settings script authenticates every config read', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const requests = page.requests.filter(request => request.url === '/api/config');
  assert.ok(requests.length > 0);
  for (const { options } of requests) assert.ok(options.headers['x-dashboard-token']);
});

test('a rejected config read is not treated as empty settings', async () => {
  const page = await settingsPage({ config: { error: 'token_required' }, configStatus: 403 });
  assert.equal(vm.runInContext('CONFIG', page.sandbox), null);
  await vm.runInContext('saveDefaultProvider()', page.sandbox);
  assert.equal(page.requests.filter(request => request.url === '/api/default-provider').length, 0);
  assert.match(page.nodes.get('default-provider-status').textContent, /먼저 읽어야/);
});

test('saving service settings keeps the current session and launch default', async () => {
  const config = { openrouter: READY_OPENROUTER, local: READY_LOCAL, default_provider: 'local',
    active_session: { provider: 'local', started: '2026-10-28T00:00:00Z' } };
  const page = await settingsPage({ config });
  page.sandbox.fetch = async () => ({ json: async () => ({ saved: true,
    config: { openrouter: READY_OPENROUTER, local: READY_LOCAL, default_provider: 'local' } }) });
  await vm.runInContext("saveSettings('openrouter')", page.sandbox);
  assert.equal(vm.runInContext('CONFIG.active_session.provider', page.sandbox), 'local');
  assert.equal(page.nodes.get('default-provider').value, 'local');
  assert.match(page.nodes.get('default-provider-status').textContent, /현재 세션: Local LLM/);
});

test('Local routing is labeled correctly and does not show OpenRouter account failures', () => {
  const base = fixture();
  const nodes = render(fixture({ active_session:{provider:'local'}, proxy_metrics:{stale:false}, routing: { ...base.routing, last: { ...base.routing.last, provider: 'local' } },
    account: { ok: false, error: 'not_applicable' }, summary: 'Local LLM으로 정상 처리 중' }));
  assert.equal(nodes.get('live-text').textContent, 'Local LLM 사용 중');
  assert.match(nodes.get('grid').innerHTML, /조회 대상이 아닙니다/);
  assert.doesNotMatch(nodes.get('grid').innerHTML, /not_applicable/);
});

test('live service and loaded model are separate from an unreported Plan model and log windows', () => {
  const base = fixture();
  const nodes = render(fixture({ active_session: {provider:'local'}, proxy_stale:false, insights:[], alerts:[],
    summary:'Plan으로 우회 중',
    routing: {...base.routing, state:'plan', counts:{external:0,plan:0},
      last:{provider:'plan', model:null, reason:'native_feature', age_seconds:2}},
    proxy_metrics:{requests:7, failed:1, routes:{external:0,plan:7}, uptimeMs:10000,
      stale:false, configuration:{provider:'local', model:'loaded-local-model'}} }));
  assert.equal(nodes.get('live-text').textContent, 'Plan 우회 중');
  const grid = nodes.get('grid').innerHTML;
  assert.match(grid, /실행 서비스/);
  assert.match(grid, /Local LLM/);
  assert.match(grid, /로드 모델/);
  assert.match(grid, /loaded-local-model/);
  assert.match(grid, /보고되지 않음/);
  assert.match(grid, /최근 구간의 라우팅 로그가 없습니다/);
  assert.match(grid, /최근 구간의 우회 사유 로그가 없습니다/);
  assert.match(grid, /새 Local·OpenRouter 세션에서 요청이 오가면 기록됩니다/);
  assert.doesNotMatch(grid, /Plan 우회 없음/);
  assert.doesNotMatch(grid, /실행기 코드가 오래됨/);
});

test('an external route with no provider evidence is not labeled as OpenRouter', () => {
  const base = fixture();
  const nodes = render(fixture({routing:{...base.routing,last:{provider:null,model:null}}}));
  assert.equal(nodes.get('live-text').textContent, '상태 미확인');
});

test('historical proxy counters are labeled as a previous run', () => {
  const nodes = render(fixture({ proxy_metrics: { requests: 4, stale: true, age_seconds: 120 } }));
  assert.match(nodes.get('grid').innerHTML, /지난 실행 기록/);
});

test('the saved launch default is separate from the settings editor selection', async () => {
  const page = await settingsPage({ config: {
    openrouter: READY_OPENROUTER, local: READY_LOCAL, default_provider: 'local' } });
  assert.equal(page.nodes.get('default-provider').value, 'local');
  assert.match(page.nodes.get('default-provider-status').textContent, /저장된 기본값: Local LLM/);
  page.select.value = 'openrouter';
  page.handlers.change();
  assert.equal(page.nodes.get('default-provider').value, 'local');
});

test('an unreadable default is shown as an error rather than automatic selection', async () => {
  const page = await settingsPage({ config: {
    openrouter: READY_OPENROUTER, local: READY_LOCAL,
    default_provider: null, default_provider_error: 'invalid_default_provider' } });
  assert.equal(page.nodes.get('default-provider').value, '');
  assert.match(page.nodes.get('default-provider-status').textContent, /읽지 못했습니다/);
});

test('saving a default persists the choice and explains when it applies', async () => {
  const page = await settingsPage({ config: {
    openrouter: READY_OPENROUTER, local: READY_LOCAL, default_provider: 'auto' } });
  const calls = [];
  page.sandbox.fetch = async (url, options) => {
    calls.push({ url, payload: JSON.parse(options.body), headers: options.headers });
    return { json: async () => ({ saved: true, default_provider: 'local' }) };
  };
  page.nodes.get('default-provider').value = 'local';
  await vm.runInContext('saveDefaultProvider()', page.sandbox);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/default-provider');
  assert.deepEqual(calls[0].payload, { provider: 'local' });
  assert.ok(calls[0].headers['x-dashboard-token']);
  assert.match(page.nodes.get('default-provider-status').textContent, /다음 새 세션/);
  assert.equal(page.nodes.get('save-default-provider').disabled, false);
});

test('a failed default save preserves the saved choice and shows a useful action', async () => {
  const page = await settingsPage({ config: {
    openrouter: READY_OPENROUTER, local: READY_LOCAL, default_provider: 'openrouter' } });
  page.sandbox.fetch = async () => ({ json: async () => ({ error: 'default_provider_not_configured' }) });
  page.nodes.get('default-provider').value = 'local';
  await vm.runInContext('saveDefaultProvider()', page.sandbox);
  assert.equal(vm.runInContext('CONFIG.default_provider', page.sandbox), 'openrouter');
  assert.match(page.nodes.get('default-provider-status').textContent, /필수 설정을 먼저 저장/);
  assert.equal(page.nodes.get('save-default-provider').disabled, false);
});

test('an unknown select value is ignored', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  page.select.value = 'nope';
  page.handlers.change();
  assert.deepEqual(visible(page.nodes), ['openrouter']);
});

test('options show whether each service is configured, and blocked storage is tolerated', async () => {
  const page = await settingsPage({
    config: { openrouter: READY_OPENROUTER, local: { fields: {} } }, blockedStorage: true });
  assert.match(page.select.innerHTML, /OpenRouter · 설정됨/);
  assert.match(page.select.innerHTML, /Local LLM · 설정 필요/);
  assert.deepEqual(visible(page.nodes), ['local']);
  page.select.value = 'openrouter';
  assert.doesNotThrow(() => page.handlers.change());
  assert.deepEqual(visible(page.nodes), ['openrouter']);
});

test('a stored server address is shown masked and can be cleared only when optional', async () => {
  const masked = (preview, clearable) => ({ secret: true, set: true, preview, value: '', clearable });
  const page = await settingsPage({ config: {
    openrouter: READY_OPENROUTER,
    local: { fields: {
      LOCAL_LLM_URL: masked('http://12•••••.1:11434/v1/messages', false),
      LOCAL_LLM_MODEL: { value: 'llama', set: true },
      LOCAL_LLM_MODELS_URL: masked('http://12•••••.1:11434/v1/models', true),
      LOCAL_LLM_HTTP_URL: { secret: true, set: false, preview: '', value: '', clearable: true } } } } });
  const html = page.nodes.get('settings').innerHTML;
  assert.doesNotMatch(html, /127\.0\.0\.1:11434\/v1\/messages/);
  assert.match(html, /저장됨 \(http:\/\/12•••••\.1:11434\/v1\/messages\)/);
  // a masked address is not a password field: no reveal toggle for it
  assert.doesNotMatch(html, /data-reveal="cfg-local-LOCAL_LLM_URL"/);
  // required address cannot be cleared; a stored optional one can; an unset optional one has nothing to clear
  assert.doesNotMatch(html, /data-clear="local:LOCAL_LLM_URL"/);
  assert.match(html, /data-clear="local:LOCAL_LLM_MODELS_URL"/);
  assert.doesNotMatch(html, /data-clear="local:LOCAL_LLM_HTTP_URL"/);
});

test('fields marked for clearing are sent as an explicit clear list', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const sandboxEval = code => vm.runInContext(code, page.sandbox);
  // arrays built inside the vm have another realm's prototype, so compare them as JSON
  const clearList = provider => JSON.parse(sandboxEval(`JSON.stringify(clearList("${provider}"))`));
  assert.deepEqual(clearList('local'), []);
  sandboxEval('CLEAR.add("local:LOCAL_LLM_MODELS_URL"); CLEAR.add("openrouter:OPENROUTER_MODEL")');
  assert.deepEqual(clearList('local'), ['LOCAL_LLM_MODELS_URL']);
  // a cleared field renders disabled with a cancel button so the pending erase is visible
  sandboxEval('CONFIG.local = {fields:{LOCAL_LLM_URL:{secret:true,set:true,preview:"x",value:"",clearable:false},' +
    'LOCAL_LLM_MODEL:{value:"m",set:true},LOCAL_LLM_MODELS_URL:{secret:true,set:true,preview:"y",value:"",clearable:true}}}; renderSettings(true)');
  const html = page.nodes.get('settings').innerHTML;
  assert.match(html, /id="cfg-local-LOCAL_LLM_MODELS_URL"[^>]*disabled/);
  assert.match(html, /data-clear="local:LOCAL_LLM_MODELS_URL">취소</);
});

test('the request-shape card shows an empty state and then the text-only candidates', () => {
  const empty = render(fixture()).get('grid').innerHTML;
  assert.match(empty, /요청 구성/);
  assert.match(empty, /데이터 없음/);
  const base = fixture();
  const shapes = { total: 10, by_family: { sonnet: { no_tools: 2, tools: 3 }, haiku: { no_tools: 5, tools: 0 } },
    text_only: { sonnet: 2, haiku: 4 } };
  const html = render(fixture({ routing: { ...base.routing, shapes } })).get('grid').innerHTML;
  assert.match(html, /10건/);
  assert.match(html, /텍스트만 Sonnet 2건, 텍스트만 Haiku 4건/);
  assert.match(html, /<td>Sonnet<\/td><td class="num">2<\/td><td class="num">3<\/td>/);
  assert.doesNotMatch(html, /데이터 없음. OpenRouter 세션/);
});
﻿
test('the CLI card explains backend support and lists the detected clients', async () => {
  const kinds = [
    { kind: 'codex', found: true, name: 'codex.cmd', dir: 'C:\\npm', version: '0.164.0' },
    { kind: 'grok', found: false, reason: 'not_found' },
  ];
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const sandboxEval = code => vm.runInContext(code, page.sandbox);
  sandboxEval(`CLI_DETECT = ${JSON.stringify(kinds)}; renderSettings(true)`);
  const html = page.nodes.get('settings').innerHTML;
  assert.match(html, /id="card-cli"/);
  assert.match(html, /Claude Code에서 CLI 모델 백엔드를 사용할 수 있습니다/);
  assert.match(html, /Reply with exactly OK/);
  assert.match(html, /<select class="input" id="cfg-cli-CLI_KIND"/);
  assert.match(html, /<option value="codex">codex<\/option>/);
  assert.match(html, /감지됨/);
  assert.match(html, /0\.164\.0/);
  assert.match(html, /설치된 CLI를 찾지 못했습니다/);
  assert.match(html, />연결 테스트 \(외부 호출\)</);
  assert.match(html, /data-picker="cli"/, 'CLI models can be listed');
});

test('CLI catalog explains cache uncertainty and handles unsupported kinds safely', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const run = code => vm.runInContext(code, page.sandbox);
  assert.match(run(`cliCatalogNote({source:'codex_local_cache',age_seconds:10}, 3)`), /Codex.*10.*\uacc4\uc815.*\ucd5c\uc2e0\uc131/);
  assert.match(run(`cliCatalogNote({source:'PRIVATE'}, 0)`), /\ucd9c\ucc98 \ubbf8\ud655\uc778/);
  assert.doesNotMatch(run(`cliCatalogNote({source:'PRIVATE'}, 0)`), /PRIVATE/);
  assert.match(run(`cliCatalogError('not_supported')`), /\uc9c0\uc6d0\ud558\uc9c0/);
  assert.match(run(`cliCatalogError('invalid_cli_kind')`), /\uba3c\uc800/);
  assert.doesNotMatch(run(`cliCatalogError('PRIVATE')`), /PRIVATE/);
});

test('CLI model picker passes the selected kind and discards a stale response', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const kind = page.sandbox.document.getElementById('cfg-cli-CLI_KIND');
  const target = page.sandbox.document.getElementById('cfg-cli-CLI_MODEL');
  kind.value = 'codex'; target.value = 'manual-model';
  let complete, calledUrl;
  page.sandbox.fetch = url => { calledUrl = url; return new Promise(resolve => { complete = resolve; }); };
  const pending = page.sandbox.openModelPicker('cli');
  assert.equal(calledUrl, '/api/models?provider=cli&kind=codex');
  kind.value = 'claude';
  complete({json: async () => ({ok:true, source:'codex_local_cache', models:[{id:'stale',name:'stale'}]})});
  await pending;
  assert.equal(target.value, 'manual-model');
  assert.equal(page.sandbox.document.getElementById('picker-list').innerHTML, '');
});

test('an unconfigured CLI card is never preselected over the routing services', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  assert.deepEqual(visible(page.nodes), ['openrouter']);
  assert.equal(page.nodes.get('card-cli').hidden, true);
  assert.match(page.select.innerHTML, /CLI 모델 백엔드 · 설정 필요/);
});

test('a CLI test result shows the version, exit code and a firewall hint on timeout', async () => {
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const sandboxEval = code => vm.runInContext(code, page.sandbox);
  sandboxEval(`RESULTS.cli = {provider:'cli', mode:'inference', ok:false, reason:'timeout', version:'0.164.0', exit_code:null, elapsed_ms:60000}; renderSettings(true)`);
  const result = page.nodes.get('res-cli');
  assert.match(result.innerHTML, /연결 테스트 · 시간 초과 — 방화벽/);
  assert.match(result.innerHTML, /v0\.164\.0/);
  assert.match(result.innerHTML, /60초/);
  assert.doesNotMatch(result.innerHTML, /종료 코드/, 'a null exit code is not shown');
});



test('CLI launch default saves only the service and preserves the existing CLI configuration', async () => {
  const page = await settingsPage({config:{openrouter:READY_OPENROUTER,local:READY_LOCAL,default_provider:'local',
    cli:{fields:{CLI_KIND:{value:'codex'},CLI_MODEL:{value:'old-model'},CLI_BINARY:{value:'old-binary'},CLI_TIMEOUT_S:{value:'45'}}}}});
  page.nodes.get('default-provider').value = 'cli';
  const calls = [];
  page.sandbox.fetch = async (url, options) => {
    calls.push({url,payload:JSON.parse(options.body)});
    return {json:async()=>({saved:true,default_provider:'cli'})};
  };
  await vm.runInContext('saveDefaultProvider()', page.sandbox);
  assert.deepEqual(calls, [{url:'/api/default-provider',payload:{provider:'cli'}}]);
  assert.equal(vm.runInContext('CONFIG.cli.fields.CLI_MODEL.value', page.sandbox), 'old-model');
  assert.equal(vm.runInContext('CONFIG.cli.fields.CLI_BINARY.value', page.sandbox), 'old-binary');
});

test('CLI live status shows recorded usage with a truthful provenance note', () => {
  const base = fixture();
  const nodes = render(fixture({active_session:{provider:'cli'},account:{ok:false,error:'not_applicable'},
    routing:{...base.routing,last:{provider:'cli',model:null}},
    proxy_metrics:{configuration:{provider:'cli',cliKind:'codex',model:null,requestedModel:'requested-x',usageMeasured:false}}}));
  const html = nodes.get('grid').innerHTML;
  assert.match(html, /CLI/);
  // The card still reports zero/unreported tokens instead of hiding the panel, and says why.
  assert.match(html, /토큰과 추정 비용/);
  assert.match(html, /CLI 응답의 토큰은 CLI가 보고한 경우에만/);
  assert.match(html, /미보고 값은 0으로 추정하지 않으며/);
  assert.doesNotMatch(html, /CLI 보고 여부/);
});


test('served settings explain safe switching without a duplicate CLI engine selector', () => {
  const html = ASSETS.settings_html;
  assert.match(html, /id="default-provider"[^>]*>[\s\S]*?<option value="cli">CLI<\/option>/);
  assert.doesNotMatch(html, /id="default-cli-kind"/);
  assert.match(html, /CLI 종류와 모델은 아래 서비스 설정에서/);
  assert.match(html, /원본 Sonnet은 프록시 없이 직접 실행/);
  assert.match(html, /<option value="sonnet">원본 Claude Sonnet<\/option>/);
  assert.match(html, /진행 중인 요청을 마치고/);
});

test('Local admission metrics show this-proxy capacity and omit stale or unavailable counters', () => {
  const localAdmission = {active: 2, queued: 3, rejected: 4, concurrency: 2, queueLimit: 8, queueWaitMs: 5000};
  const grid = render(fixture({proxy_metrics: {localAdmission}})).get('grid').innerHTML;
  assert.match(grid, /Local 실행 슬롯 · 이 프록시/);
  assert.match(grid, /2 \/ 2/);
  assert.match(grid, /3 \/ 8/);
  assert.match(grid, /Local 대기 한도 초과 · 누적/);
  assert.match(grid, /5,000 ms/);
  assert.match(grid, /공유 서버 전체 부하·GPU 사용률을 뜻하지 않습니다/);
  for (const proxy_metrics of [{localAdmission, stale: true}, {}, null]) {
    assert.doesNotMatch(render(fixture({proxy_metrics})).get('grid').innerHTML, /Local 실행 슬롯 · 이 프록시/);
  }
});


test('token flow scales bounded particles and exposes Local occupancy without inventing unreported traffic', () => {
  const context=buildContext(); context.window.innerHeight=900; context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=(tokens,responses,active=2,queued=3)=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'same-session',requests:5,
    configuration:{provider:'local'},localAdmission:{active,queued,concurrency:2},
    tokenFlow:{local:{responses,inputSamples:responses,inputTokens:tokens,outputSamples:responses,outputTokens:tokens}}}});
  sandbox.render(data(0,0));sandbox.render(data(1000000,1));
  let html=context.nodes.get('grid').innerHTML;
  assert.equal((html.match(/class="flow-particle"/g)||[]).length,24);
  assert.equal((html.match(/class="flow-particle flow-response"/g)||[]).length,24);
  assert.match(html,/class="flow-proxy flow-reacting flow-intensity-3"/);
  assert.match(html,/Local 실행 슬롯 2 \/ 2/);assert.match(html,/Local 대기 3건/);
  assert.match(html,/class="flow-turbine unknown spinning batch-3 batch-heat"/);
  assert.match(html,/flow-return-track flow-completed/);
  assert.match(html,/--turbine-speed:3600ms/); assert.match(html,/--turbine-turns:3turn/);
  assert.match(html,/Local 작업 진행 중/);
  sandbox.render(data(1000000,1,0,0));html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/class="flow-particle/);assert.match(html,/새 응답 없음/);
  assert.doesNotMatch(html,/flow-reacting/);
  assert.doesNotMatch(html,/batch-heat|flow-completed/);
  assert.match(html,/class="flow-turbine unknown"/);
  assert.doesNotMatch(html,/flow-turbine unknown spinning/);
  context.window.matchMedia=()=>({matches:true});sandbox.render(data(2000000,2));
  assert.match(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  assert.match(context.nodes.get('grid').innerHTML,/1,000,000 토큰/);
});

test('turbine uses common in-flight evidence, stops when idle or hidden, and never invents activity', () => {
  for (const provider of ['local','openrouter','cli','plan']) {
    const context=buildContext();context.window.innerHeight=900;context.window.matchMedia=()=>({matches:false});
    context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
    const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
    const data=inflight=>fixture({active_session:{provider},proxy_metrics:{startedAt:'turbine',requests:1,inflightRequests:inflight,configuration:{provider}}});
    sandbox.render(data(2));let html=context.nodes.get('grid').innerHTML;
    assert.match(html,/class="flow-turbine active spinning"/,provider);
    assert.match(html,/class="flow-track flow-live"/,provider);
    assert.match(html,/프록시 진행 요청 2건/,provider);
    sandbox.render(data(0));html=context.nodes.get('grid').innerHTML;
    assert.match(html,/class="flow-turbine idle"/,provider);assert.doesNotMatch(html,/flow-turbine idle spinning/,provider);
    assert.doesNotMatch(html,/flow-track flow-live/,provider);
    sandbox.render(data(null));html=context.nodes.get('grid').innerHTML;
    assert.match(html,/class="flow-turbine unknown"/,provider);assert.doesNotMatch(html,/flow-turbine unknown spinning/,provider);
    context.document.hidden=true;sandbox.render(data(2));html=context.nodes.get('grid').innerHTML;
    assert.match(html,/class="flow-turbine active"/,provider);assert.doesNotMatch(html,/flow-turbine active spinning/,provider);
    assert.doesNotMatch(html,/flow-track flow-live/,provider);
  }
});

test('in-flight turbine keeps its rotation phase across dashboard refreshes', () => {
  let now=10000;
  const context=buildContext(); context.Date=class extends Date { static now(){return now;} };
  context.window.innerHeight=900;context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'phase',requests:1,inflightRequests:1,configuration:{provider:'local'}}});
  const phase=()=>Number(context.nodes.get('grid').innerHTML.match(/--turbine-offset:(-?\d+)ms/)[1]);
  sandbox.render(data);const first=phase();now+=800;sandbox.render(data);const second=phase();
  assert.equal((-second + first + 3600) % 3600,800);
});

test('processing bar pulses once when a new request is observed', () => {
  const context=buildContext(); context.window.innerHeight=900; context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=requests=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'bar-pulse',requests,inflightRequests:1,configuration:{provider:'local'}}});
  sandbox.render(data(1));assert.doesNotMatch(context.nodes.get('grid').innerHTML,/request-pulse/);
  sandbox.render(data(2));assert.match(context.nodes.get('grid').innerHTML,/flow-turbine active spinning request-pulse/);
  sandbox.render(data(2));assert.doesNotMatch(context.nodes.get('grid').innerHTML,/request-pulse/);
  context.document.hidden=true;sandbox.render(data(3));assert.doesNotMatch(context.nodes.get('grid').innerHTML,/request-pulse/);
});


test('flow motion defaults on even with reduced motion and can be paused for this page', () => {
  const context=buildContext(), clicks=[];
  context.document.addEventListener=(type,handler)=>{if(type==='click')clicks.push(handler)};
  context.window.innerHeight=900;context.window.matchMedia=()=>({matches:true});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=n=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'motion-test',requests:n,configuration:{provider:'local'},
    tokenFlow:{local:{responses:n,inputSamples:n,inputTokens:n*10,outputSamples:n,outputTokens:n}}}});
  sandbox.render(data(1));sandbox.render(data(2));
  assert.equal(sandbox.flowMotionAllowed(),true);
  assert.match(context.nodes.get('grid').innerHTML,/움직임 끄기/);
  assert.match(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  assert.match(context.nodes.get('grid').innerHTML,/flow-reacting flow-intensity-1/);
  const click=()=>clicks.forEach(fn=>fn({target:{closest:selector=>selector==='[data-flow-motion]'?{}:null}}));
  click();
  assert.equal(sandbox.flowMotionAllowed(),false);
  assert.doesNotMatch(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  assert.doesNotMatch(context.nodes.get('grid').innerHTML,/flow-turbine [^" ]* spinning/);
  sandbox.render(data(3));let html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/class="flow-particle/);assert.doesNotMatch(html,/flow-reacting/);assert.match(html,/움직임 켜기/);
  click();sandbox.render(data(4));html=context.nodes.get('grid').innerHTML;
  assert.equal(sandbox.flowMotionAllowed(),true);assert.match(html,/class="flow-particle/);
  assert.equal(context.window.matchMedia().matches,true);
});

test('gateway reacts only to observed token deltas equally for all external providers', () => {
  for (const provider of ['local', 'openrouter', 'cli']) {
    const context = buildContext();
    context.window.innerHeight = 900;
    context.window.matchMedia = () => ({matches:true});
    context.document.getElementById('gateway-motion').getBoundingClientRect = () => ({top:100,bottom:200});
    const sandbox = {...context, globalThis:null}; sandbox.globalThis = sandbox;
    vm.createContext(sandbox); vm.runInContext(scriptBundle(), sandbox);
    const data = (responses, samples, input, output, tokenFlow = true) => fixture({
      active_session: {provider}, proxy_metrics: {startedAt:'same', requests:responses,
        configuration:{provider}, ...(tokenFlow ? {tokenFlow:{[provider]:{responses,inputSamples:samples,
          inputTokens:input,outputSamples:samples,outputTokens:output}}} : {})},
    });
    sandbox.render(data(0,0,0,0));
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /flow-reacting/);
    sandbox.render(data(1,1,10,1));
    assert.match(context.nodes.get('grid').innerHTML, /flow-reacting flow-intensity-1/, provider);
    assert.match(context.nodes.get('grid').innerHTML, /flow-return-track flow-completed/, provider);
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /batch-heat/, provider);
    assert.match(context.nodes.get('grid').innerHTML, /--turbine-turns:1turn/, provider);
    sandbox.render(data(1,1,10,1));
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /flow-reacting/, provider);
    sandbox.render(data(2,1,10,1)); // successful response without reported usage
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /flow-reacting/, provider);
    assert.match(context.nodes.get('grid').innerHTML, /flow-return-track flow-completed/, provider);
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /class="flow-particle/, provider);
    context.document.hidden = true;
    sandbox.render(data(3,2,20,2));
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /flow-reacting/, provider);
    context.document.hidden = false;
    sandbox.render(data(4,2,20,2,false)); // older proxy: usage unavailable
    assert.doesNotMatch(context.nodes.get('grid').innerHTML, /flow-reacting/, provider);
  }
});


test('runtime timing breakdown distinguishes stage samples and actual attempts', () => {
  const row = {count: 2, p50: 10, p95: 20, max: 30};
  const data = fixture({proxy_metrics: {requests: 3, externalAttempts: 2, retries: 1,
    attemptCounts: {external: 4, plan: 1}, routingTimingMs: {queueWait: row, externalAttempt: row,
      retryWait: {count: 0, p50: null, p95: null, max: null}, plan: row, total: row}}});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /라우팅 단계별 시간/);
  assert.match(html, /10 \/ 20 \/ 30 ms \(2건\)/);
  assert.match(html, /측정 없음 \(0건\)/);
  assert.match(html, /외부 모델 실제 시도/);
  assert.match(html, /Plan 실제 시도/);
  assert.match(html, /백분위는 단계별로 계산하므로 합산할 수 없습니다/);
  const old = render(fixture({proxy_metrics:{requests:3}})).get('grid').innerHTML;
  assert.match(old, /Local 슬롯 대기/);
  assert.match(old, /미보고/);
});


test('flow combines independently observed providers and reports incomplete comparisons', () => {
  const context=buildContext(); context.window.innerHeight=900; context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const row=(n,input,output)=>({responses:n,inputSamples:n,inputTokens:input,outputSamples:n,outputTokens:output});
  const data=(tokenFlow,requests=3,inflightRequests=2,startedAt='session')=>fixture({active_session:{provider:'local'},
    proxy_metrics:{startedAt,requests,inflightRequests,failed:2,configuration:{provider:'local'},tokenFlow}});
  sandbox.render(data({local:row(0,0,0),cli:row(0,0,0),openrouter:row(0,0,0)}));
  sandbox.render(data({local:row(1,10,1),cli:row(1,30,3),openrouter:row(0,0,0)},4));
  let html=context.nodes.get('grid').innerHTML;
  assert.match(html,/프록시 진행 요청 2건/);
  assert.match(html,/최근 갱신 사이 성공한 외부 응답<\/span><strong>2건<\/strong><small>프록시 관측 기준 · 누적 실패\(취소 제외\) 2건/);
  assert.match(html,/관측 입력 토큰<\/span><strong>40 토큰 · 2건 보고/);
  assert.match(html,/Local LLM · 입력 10 토큰 \/ 출력 1 토큰/);
  assert.match(html,/CLI · 입력 30 토큰 \/ 출력 3 토큰/);
  assert.match(html,/OpenRouter · 입력 새 응답 없음/);
  assert.match(html,/flow-reacting/);
  sandbox.render(data({local:row(2,20,2),cli:{responses:2},openrouter:row(0,0,0)},5,0));
  html=context.nodes.get('grid').innerHTML;
  assert.match(html,/프록시 진행 요청 0건/);assert.match(html,/비교 가능한 제공자만 합산/);
  assert.match(html,/성공한 외부 응답<\/span><strong>1건<\/strong><small>일부 제공자만 관측/);
  assert.match(html,/CLI · 입력 미확인/);assert.match(html,/관측 입력 토큰<\/span><strong>10 토큰 · 1건 보고/);
  sandbox.render(data({local:row(2,20,2),cli:row(3,90,9),openrouter:row(0,0,0)},6));
  html=context.nodes.get('grid').innerHTML;assert.match(html,/CLI · 입력 비교 대기/);assert.doesNotMatch(html,/flow-reacting/);
  sandbox.render(data({local:row(3,30,3),cli:row(4,100,10),openrouter:row(0,0,0)},7,undefined,'new-session'));
  html=context.nodes.get('grid').innerHTML;assert.doesNotMatch(html,/flow-reacting/);
  assert.match(html,/첫 관측/);
});

test('flow shows common in-flight count for each provider and handles old or stale metrics', () => {
  for (const provider of ['local','openrouter','cli']) {
    const make=(inflightRequests,stale=false)=>fixture({active_session:{provider},proxy_metrics:{startedAt:'s',requests:1,
      configuration:{provider},...(inflightRequests === undefined ? {} : {inflightRequests}),stale}});
    assert.match(render(make(3)).get('grid').innerHTML,/프록시 진행 요청 3건/,provider);
    assert.match(render(make(undefined)).get('grid').innerHTML,/프록시 진행 요청 미보고/,provider);
    assert.match(render(make(3,true)).get('grid').innerHTML,/프록시 진행 요청 미보고/,provider);
  }
});

test('flow statistics label missing counters rather than inventing zero', () => {
  const html=render(fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'old',requests:1,configuration:{provider:'local'}}})).get('grid').innerHTML;
  assert.match(html,/성공한 외부 응답<\/span><strong>미보고<\/strong>/);
  assert.match(html,/누적 실패\(취소 제외\) 미보고/);
});


test('cancelled requests are excluded from the failure rate and each connection is listed', () => {
  const data = fixture({provider: 'cli', active_session: {provider: 'cli'},
    proxy_metrics: {startedAt: 's', requests: 6, ok: 3, failed: 3, aborted: 1,
      routes: {external: 4, plan: 2}}});
  data.routing.providers = {
    local: {requests: 2, failures: 1, last_model: 'qwen3', last_ts: '2026-10-25T07:50:00+00:00'},
    openrouter: {requests: 1, failures: 0, last_model: null, last_ts: null},
    cli: {requests: 3, failures: 0, last_model: null, last_ts: null},
    plan: {requests: 2, failures: 0, last_model: null, last_ts: null},
  };
  const html = render(data).get('grid').innerHTML;
  // 3 successes, 3 failures of which 1 was cancelled: completed = 3 + 2, rate = 2/5.
  assert.match(html, /실패율 · 완료 응답 기준\(취소 제외\)/);
  assert.match(html, /40\.0% \(2 \/ 5건 완료\)/);
  assert.match(html, /연결점별 결정 · 최근 60분/);
  assert.match(html, /Local 2건\(시도 실패 1\) qwen3 · OpenRouter 1건 · CLI 3건 · Plan 2건/);
  // A CLI display provider must not hide token usage or the normal usage card.
  assert.match(html, /토큰과 추정 비용/);
  assert.match(html, /CLI 응답의 토큰은/);
  assert.doesNotMatch(html, /CLI 보고 여부/);
});

test('session monitor states name the evidence instead of implying activity', () => {
  // The session monitor card is inserted next to the live chart, so the DOM stub does not
  // render it; assert the served script carries every evidence-based label instead.
  const page = ASSETS.dashboard;
  assert.match(page, /대기 중 \$\{count\(queued\)\}/);
  assert.match(page, /모델 응답 대기 중/);
  assert.match(page, /상태 확인 불가/);
  assert.match(page, /진행 상태 미보고/);
  assert.match(page, /연결 종료는 서버 계산 중단을 뜻하지 않습니다/);
  const source = execFileSync(process.env.PYTHON || 'python', ['-X', 'utf8', '-c',
    'import routeDashboard as d; print(d.PAGE)'], { cwd: new URL('..', import.meta.url), encoding: 'utf-8' });
  assert.match(source, /제공자 속도 제한/);
  assert.match(source, /프록시가 보고한 값만 사용합니다/);
});

test('connection summary says no events were recorded instead of printing zeros', () => {
  const data = fixture({active_session: {provider: 'local'},
    proxy_metrics: {startedAt: 's', requests: 1, ok: 1, failed: 0, aborted: 0, configuration: {provider: 'local'}}});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /연결점별 결정 · 최근 60분/);
  assert.match(html, /최근 60분 이벤트 없음/);
  assert.doesNotMatch(html, /Local 0건/);
});

test('shared error and test text cover backend failures and store rate limits', () => {
  assert.match(ASSETS.settings, /rate_limited:'저장 요청이 많습니다/);
  assert.match(ASSETS.settings, /backend_version_unverified:'CLI 버전이 검증 목록에 없어/);
  assert.match(ASSETS.settings, /grok \(1\.0\.77 검증 · 격리 제한\)/);
});


test('flow sequences each observed batch from input through proxy to output without fabricated particles', () => {
  const context=buildContext();context.window.innerHeight=900;context.window.matchMedia=()=>({matches:false});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=(n,input,output)=>fixture({refresh_ms:5000,active_session:{provider:'local'},proxy_metrics:{startedAt:'s',requests:n,
    configuration:{provider:'local'},tokenFlow:{local:{responses:n,inputSamples:n,inputTokens:input,outputSamples:n,outputTokens:output}}}});
  sandbox.render(data(0,0,0));sandbox.render(data(1,10,10));
  let html=context.nodes.get('grid').innerHTML;
  assert.match(html,/--pulse-delay:1600ms;--pulse-duration:900ms/);
  assert.match(html,/--turbine-delay:1600ms;--turbine-duration:900ms;--turbine-turns:1turn/);
  const input=[...html.matchAll(/class="flow-particle" style="--lane:\d+;--delay:(\d+)ms;--flight:(\d+)ms"/g)];
  const output=[...html.matchAll(/class="flow-particle flow-response" style="--lane:\d+;--delay:(\d+)ms;--flight:(\d+)ms"/g)];
  assert.ok(input.length && output.length);
  assert.ok(Math.max(...input.map(x=>+x[1]+ +x[2]))<=1600);
  assert.ok(Math.min(...output.map(x=>+x[1]))>=2500);
  assert.ok(Math.max(...output.map(x=>+x[1]+ +x[2]))<=5000);
  sandbox.render(data(2,10,20));html=context.nodes.get('grid').innerHTML;
  assert.equal((html.match(/class="flow-particle"/g)||[]).length,0);
  assert.match(html,/--pulse-delay:0ms;--pulse-duration:900ms/);
  assert.match(html,/--turbine-delay:0ms;--turbine-duration:900ms/);
  assert.match(html,/class="flow-particle flow-response" style="--lane:0;--delay:900ms/);
  sandbox.render(data(3,10,20));html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/flow-reacting/);assert.doesNotMatch(html,/class="flow-particle/);
});

test('Plan status occupies existing overview instead of inserting an alert banner', () => {
  const plan={state:'plan',last:{provider:'plan',reason:'backend_unavailable'}};
  const base={active_session:{provider:'local'},proxy_metrics:{startedAt:'s',requests:1,configuration:{provider:'local'}},
    routing:plan,alerts:[{code:'plan_state',level:'info',title:'현재 Plan 경로',detail:'last reason'}]};
  let nodes=render(fixture(base));
  assert.doesNotMatch(nodes.get('alerts').innerHTML,/현재 Plan 경로/);
  let html=nodes.get('grid').innerHTML;
  assert.match(html,/class="overview-state" role="status" aria-live="polite"/);
  assert.match(html,/마지막 Plan 경로 사유 · backend_unavailable/);
  nodes=render(fixture({...base,routing:{state:'external',last:{provider:'local'}} ,alerts:[]}));
  assert.doesNotMatch(nodes.get('grid').innerHTML,/마지막 Plan 경로 사유/);
  nodes=render(fixture({...base,proxy_metrics:{...base.proxy_metrics,stale:true}}));
  html=nodes.get('grid').innerHTML;
  assert.match(html,/현재 상태 미확인/);assert.doesNotMatch(html,/마지막 Plan 경로 사유/);
});


test('usage chart places dates proportionally and breaks missing periods', () => {
  const context=buildContext(), sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;
  vm.createContext(sandbox);vm.runInContext(ASSETS.usage,sandbox);
  const row=bucket=>({bucket,input_tokens:10,input_samples:1,output_tokens:10,output_samples:1});
  let html=sandbox.usageChart([row('2026-10-01'),row('2026-10-02'),row('2026-10-05')],'day');
  assert.match(html,/M3.00 5.00 L26.50 5.00 M97.00 5.00/);
  assert.match(html,/circle cx="97.00"/);
  html=sandbox.usageChart([row('2026-10-05'),row('2026-10-12'),row('2026-10-26')],'week');
  assert.match(html,/M3.00 5.00 L34.33 5.00 M97.00 5.00/);
  html=sandbox.usageChart([row('2026-01'),row('2026-02'),row('2026-04')],'month');
  assert.match(html,/M3.00 5.00 L34.33 5.00 M97.00 5.00/);
});

test('recording loss and unknown recording status are visible', () => {
  const context=buildContext(), sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;
  vm.createContext(sandbox);vm.runInContext(ASSETS.usage,sandbox);
  const payload=recording=>({status:'ready',report:{rows:[]},sessions:[{provider:'cli',status:'active',recording}]});
  sandbox.renderUsage(payload({enabled:true,dropped:2,errors:1,pending:3}));
  assert.match(context.nodes.get('usage-status').textContent,/누락/);
  assert.match(context.nodes.get('usage-grid').innerHTML,/누락 2 · 오류 1 · 대기 3/);
  sandbox.renderUsage(payload(null));assert.match(context.nodes.get('usage-status').textContent,/누락/);
  assert.match(context.nodes.get('usage-grid').innerHTML,/기록 상태 미보고/);
});

test('write token is read from the URL fragment and removed from the address bar', () => {
  const context = buildContext('#token=abc123_-X');
  let replaced = 0;
  context.history.replaceState = () => { replaced += 1; };
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  assert.equal(vm.runInContext('TOKEN', sandbox), 'abc123_-X');
  assert.equal(replaced, 1);
  assert.equal(context.sessionStorage.getItem('claude-cheat-write-token'), 'abc123_-X');

  // In-page navigation and refreshes drop the fragment; the same tab must keep its write token.
  const next = buildContext('');
  next.sessionStorage = context.sessionStorage;
  const nextSandbox = { ...next, globalThis: null };
  nextSandbox.globalThis = nextSandbox;
  vm.createContext(nextSandbox);
  vm.runInContext(scriptBundle(), nextSandbox, { filename: 'dashboard.js' });
  assert.equal(vm.runInContext('TOKEN', nextSandbox), 'abc123_-X');

  // A new tab starts with fresh sessionStorage and stays read-only.
  const bare = buildContext('');
  const bareSandbox = { ...bare, globalThis: null };
  bareSandbox.globalThis = bareSandbox;
  vm.createContext(bareSandbox);
  vm.runInContext(scriptBundle(), bareSandbox, { filename: 'dashboard.js' });
  assert.equal(vm.runInContext('TOKEN', bareSandbox), '');
});

test('a stale write token is dropped when the server answers token_required', async () => {
  const fetchImpl = async () => ({ status: 403, ok: false,
    clone() { return { json: async () => ({ error: 'token_required' }) }; } });
  const context = buildContext('#token=stale-token', fetchImpl);
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  assert.equal(context.sessionStorage.getItem('claude-cheat-write-token'), 'stale-token');
  await vm.runInContext('fetch("/api/config")', sandbox);
  assert.equal(context.sessionStorage.getItem('claude-cheat-write-token'), null);
  assert.equal(context.document.body.children.length, 1);
  assert.equal(context.document.body.children[0].className, 'token-notice');
});

test('a route-log model id is escaped before it reaches the connection summary', () => {
  const nodes = render(fixture({
    active_session: { provider: 'local', started: '2026-10-25T07:00:00+00:00' },
    proxy_metrics: { startedAt: '2026-10-25T07:00:00+00:00', requests: 1, ok: 1, failed: 0,
      routes: { external: 1, plan: 0 }, configuration: { provider: 'local' } },
    routing: {
      ...fixture().routing,
      providers: { local: { requests: 1, failures: 0, last_model: '<img src=x onerror=alert(1)>' } },
    },
  }));
  const html = nodes.get('grid').innerHTML;
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img src=x onerror/);
});

// ---- proactive reactions: attention badge, desktop notifications, recovery ----

function loadDashboard() {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  return { context, sandbox };
}

test('the tab title carries a proactive attention badge', () => {
  const { context, sandbox } = loadDashboard();
  sandbox.render(fixture({ alerts: [], insights: [] }));
  assert.equal(context.document.title, 'Claude-Cheat 라우팅 콘솔');
  sandbox.render(fixture({ alerts: [{ level: 'warn', title: '주의', detail: 'd', code: 'w1' }], insights: [] }));
  assert.match(context.document.title, /^⚠ 1 · /);
  sandbox.render(fixture({ alerts: [{ level: 'critical', title: '위험', detail: 'd', code: 'c1' }], insights: [] }));
  assert.match(context.document.title, /^⛔ 1 · /);
  assert.equal(context.nodes.get('attention-pill').textContent, '확인 필요 1');
});

test('critical alerts notify once per condition when notifications are enabled', () => {
  const context = buildContext();
  context.localStorage = { getItem: () => '1', setItem() {} };
  const sent = [];
  context.window = {
    addEventListener() {}, focus() {},
    Notification: class {
      static permission = 'granted';
      static async requestPermission() { return 'granted'; }
      constructor(title, options) { sent.push({ title, options }); }
      close() {}
    },
  };
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  assert.equal(context.nodes.get('notify-toggle').hidden, false, 'a supported notification button is visible');
  const critical = { level: 'critical', title: '한도 소진', detail: '남은 금액 $1', code: 'low_balance' };
  sandbox.render(fixture({ alerts: [critical], insights: [] }));
  assert.equal(sent.length, 1);
  assert.match(sent[0].title, /한도 소진/);
  sandbox.render(fixture({ alerts: [critical], insights: [] }));
  assert.equal(sent.length, 1, 'the same alert must not re-notify');
  sandbox.render(fixture({ alerts: [], insights: [] }));
  sandbox.render(fixture({ alerts: [critical], insights: [] }));
  assert.equal(sent.length, 2, 'a condition that clears and returns may notify again');
});

test('a dropped dashboard connection backs off and reports the recovery', async () => {
  const { context, sandbox } = loadDashboard();
  sandbox.fetch = async () => { throw new Error('down'); };
  await vm.runInContext('tick()', sandbox);
  assert.equal(vm.runInContext('tickFailures', sandbox), 1);
  sandbox.fetch = async () => ({ ok: true, json: async () => fixture({ alerts: [], insights: [] }) });
  await vm.runInContext('tick()', sandbox);
  assert.equal(vm.runInContext('tickFailures', sandbox), 0);
  assert.match(context.nodes.get('toasts').children.at(-1).innerHTML, /복구/);
});

test('process health is rendered separately from upstream performance', () => {
  const { context, sandbox } = loadDashboard();
  const grid = context.document.getElementById('grid');
  let inserted = '';
  grid.insertAdjacentHTML = (position, html) => { inserted = html; };
  sandbox.render(fixture({ proxy_metrics: { stale: false, process: {
    pid: 1234, rssBytes: 120 * 1048576, heapUsedBytes: 40 * 1048576,
    eventLoopLagMs: { samples: 20, p50Ms: 2, p95Ms: 12.5, maxMs: 30 },
    eventLoopSpikes: [{ at: '2026-11-01T09:05:00.000Z', ms: 626.5 }] },
    routeLog: { enabled: true, written: 42, dropped: 0, queuedLines: 0, queuedBytes: 0, writeMsMax: 1.25 },
    gc: { count: 3, maxMs: 7.5 }, blocking: [{ name: 'body_parse', count: 2, maxMs: 4.5 }] } }));
  assert.match(inserted, /프로세스 상태/);
  assert.match(inserted, /120\.0 MB/);
  assert.match(inserted, /40\.0 MB/);
  assert.match(inserted, /12\.5 ms/);
  assert.match(inserted, /최근 100ms 이상 지연/);
  assert.match(inserted, /627ms/);
  assert.match(inserted, /라우트 로그 쓰기 max/);
  assert.match(inserted, /1\.3 ms/);
  assert.match(inserted, /GC 3회/);
  assert.match(inserted, /body_parse/);
  assert.doesNotMatch(inserted, /undefined/);
});

test('process health is not invented when the proxy reports none', () => {
  const { context, sandbox } = loadDashboard();
  const grid = context.document.getElementById('grid');
  let calls = 0;
  grid.insertAdjacentHTML = () => { calls += 1; };
  sandbox.render(fixture({ proxy_metrics: { stale: true } }));
  assert.equal(calls, 0);
});

test('an all-zero metric chart reports no observation instead of a fake peak', () => {
  const { sandbox } = loadDashboard();
  const zero = vm.runInContext(
    "historyLine([{minute:'2026-11-01T09:24:00Z'},{minute:'2026-11-01T09:25:00Z'}], () => 0, '%', 'line-coral')",
    sandbox);
  assert.match(zero, /관측값 없음\(0\)/);
  assert.doesNotMatch(zero, /최대 1\.00/);
  const measured = vm.runInContext(
    "historyLine([{minute:'2026-11-01T09:24:00Z'},{minute:'2026-11-01T09:25:00Z'}], () => 5, '%', 'line-coral')",
    sandbox);
  assert.match(measured, /최대 5\.00 %/);
});

test('the notification button stays hidden without a Notification API', () => {
  const { context } = loadDashboard();
  assert.equal(context.nodes.get('notify-toggle').hidden, true);
});

test('attention queue renders only actionable items with direct actions', () => {
  const { context, sandbox } = loadDashboard();
  sandbox.render(fixture({
    alerts: [{ level: 'warn', title: '호스트 CPU 사용률 높음', detail: '호스트 CPU가 최근 표본에서 97.6%로 높게 지속됐습니다.', code: 'host_cpu_pressure' }],
    attention: {
      counts: { open: 1, critical: 0, warn: 1, acknowledged: 0, snoozed: 0, history: 0 },
      items: [{ id: '0123456789ab', level: 'warn', title: '호스트 CPU 사용률 높음',
        detail: '호스트 CPU가 최근 표본에서 97.6%로 높게 지속됐습니다.', project: 'enjoy-tesla', state: 'open', command: null, age_seconds: 9 * 3600 }],
      acknowledged: [], snoozed: [], history: [],
    },
  }));
  const html = context.nodes.get('alerts').innerHTML;
  assert.match(html, /지금 필요한 것/);
  assert.match(html, /호스트 CPU 사용률 높음/);
  assert.match(html, /97\.6%/);
  assert.match(html, /9시간 전/);
  assert.match(html, /data-attention-action="ack"/);
  assert.match(html, /data-attention-action="resolve"/);
  assert.match(html, /1시간 미루기/);
});

test('closed attention items move to activity history without occupying the main queue', () => {
  const { context, sandbox } = loadDashboard();
  sandbox.render(fixture({
    alerts: [], insights: [],
    attention: { counts: { open: 0, critical: 0, warn: 0, acknowledged: 1, snoozed: 0, history: 1 },
      items: [], acknowledged: [{ id: 'ack', title: '확인한 항목', state: 'acknowledged' }], snoozed: [],
      history: [{ id: 'done', title: '해결된 항목', state: 'resolved' }] },
  }));
  const html = context.nodes.get('alerts').innerHTML;
  assert.equal(html, '');
  const grid = context.nodes.get('grid').innerHTML;
  assert.match(grid, /주의 항목 이력/);
  assert.match(grid, /해결된 항목/);
  assert.match(grid, /data-attention-action="reopen"/);
  assert.doesNotMatch(grid, /data-attention-id="done"/);
});

function versionFixture(update = {}, local = {}) {
  return {
    local: { label: 'v1.0.0 (4148f2f)', version: '1.0.0', branch: 'main',
      commit_date: '2026-11-02T07:29:54+09:00', commit: '4148f2f819775b0c3e6fb178ea901851f32b5df0',
      dirty: false, ...local },
    update: { state: 'current', behind_by: 0, ahead_by: 0, checked_at: '2026-11-02T02:00:00+00:00',
      remote_commit: null, error: null, ...update },
  };
}

function loadVersionPage(payloads) {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  const fetched = [];
  const timers = [];
  sandbox.fetch = async url => {
    fetched.push(url);
    // Only the version endpoint is under test; the page's own bootstrap calls stay inert.
    if (!String(url).startsWith('/api/version')) return { ok: true, json: async () => ({}) };
    const payload = payloads.length > 1 ? payloads.shift() : payloads[0];
    return { ok: true, json: async () => payload };
  };
  sandbox.setTimeout = (fn, delay) => { timers.push(delay); return timers.length; };
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  return { context, sandbox, fetched, timers };
}

test('the version badge names the build and flags an available update', async () => {
  const { context, sandbox, fetched } = loadVersionPage([
    versionFixture({ state: 'behind', behind_by: 2 }, { dirty: true })]);
  await sandbox.loadVersion();
  const pill = context.nodes.get('version-pill');
  assert.equal(pill.textContent, 'v1.0.0 (4148f2f) · 업데이트');
  assert.match(pill.className, /pill-warn/);
  assert.equal(pill.dataset.state, 'behind');
  assert.match(pill.title, /2개 커밋 뒤짐 → git pull/);
  assert.match(pill.title, /커밋되지 않은 변경 있음/);
  // The page also bootstraps config and its snapshot poll; only one version request is ours.
  assert.deepEqual(fetched.filter(url => url.startsWith('/api/version')), ['/api/version']);
});

test('the version badge stays quiet when the checkout is current', async () => {
  const { context, sandbox } = loadVersionPage([versionFixture()]);
  await sandbox.loadVersion();
  const pill = context.nodes.get('version-pill');
  assert.equal(pill.textContent, 'v1.0.0 (4148f2f)');
  assert.match(pill.className, /pill-ok/);
  assert.equal(pill.dataset.state, 'current');
});

test('the version badge polls again while the answer is still pending', async () => {
  const { context, sandbox, timers } = loadVersionPage([
    versionFixture({ state: 'checking', checked_at: null }), versionFixture()]);
  await sandbox.loadVersion();
  assert.ok(timers.includes(15000), 'a pending answer must schedule another poll');
  await sandbox.loadVersion();
  assert.equal(context.nodes.get('version-pill').dataset.state, 'current');
  assert.equal(timers.filter(delay => delay === 15000).length, 1, 'a known answer must stop polling');
});

test('clicking the version badge re-checks instead of trusting the cache', async () => {
  const { context, sandbox, fetched } = loadVersionPage([versionFixture()]);
  await sandbox.checkVersionNow();
  assert.deepEqual(fetched.filter(url => url.startsWith('/api/version')), ['/api/version?refresh=1']);
  assert.equal(context.nodes.get('version-pill').dataset.state, 'current');
});

test('the version badge never breaks the page when the endpoint is unreachable', async () => {
  const context = buildContext();
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  sandbox.fetch = async () => { throw new Error('offline'); };
  vm.createContext(sandbox);
  vm.runInContext(scriptBundle(), sandbox, { filename: 'dashboard.js' });
  await sandbox.loadVersion();
  await sandbox.checkVersionNow();
  assert.equal(context.nodes.get('version-pill').textContent, '확인 실패');
});
