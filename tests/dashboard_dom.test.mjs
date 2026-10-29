// Runs the dashboard page script against a minimal DOM stub so the rendering layer is
// covered without a browser: cards, alert de-duplication, summary and copy buttons.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import vm from 'node:vm';

// Run the exact scripts embedded in the served pages, including computed Python appends.
const ASSETS = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-c',
  'import json,re,routeDashboard as d; print(json.dumps({name:re.search(r"<script>(.*)</script>",page,re.S).group(1) for name,page in (("dashboard",d.PAGE),("settings",d.SETTINGS_PAGE))} | {"settings_html":d.SETTINGS_PAGE}))'],
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
    remove() {}, addEventListener() {}, removeEventListener() {},
    querySelectorAll: () => [], querySelector: () => null, closest: () => null,
    contains: () => false, dispatchEvent() {}, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; }, getAttribute(name) { return this.attributes[name] ?? null; },
  };
  return element;
}

function buildContext() {
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
  return {
    nodes, document, window: { addEventListener() {} },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
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
    startedAt: '2026-10-25T07:00:00+00:00', uptimeMs: 3600000, requests: 60, ok: 45, failed: 5, aborted: 10,
    configuration: {provider: 'local'}, routes: {external: 40, plan: 20},
    latencyMs: {external: {p50: 100, p95: 500, count: 40}, plan: {p50: 200, p95: 600, count: 20}, ttfb: {p50: 80, p95: 400, count: 40}},
    modelPerformance: {local: {samples: 2, outputTokens: 40, elapsedMs: 4000}},
  }});
  const html = render(data).get('grid').innerHTML;
  assert.match(html, /1\.00 건\/분/);
  assert.match(html, /10\.0% \(5 \/ 50건\)/);
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
  assert.match(html, /10\.0% \(5 \/ 50건\)/);
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
  assert.match(html, /aria-label="외부 서비스 3건 75%, Plan 1건 25%"/);
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
  assert.match(html, /CLI 토큰 사용량은 이 그래프에서 집계하지 않습니다/);
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
  assert.match(html,/Claude-Cheat 프록시/); assert.match(html,/경로 선택 · 요청 처리/);
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

async function settingsPage({ config, store = new Map(), blockedStorage = false, configStatus = 200 } = {}) {
  const context = buildContext();
  const handlers = {};
  const select = context.document.getElementById('provider-select');
  select.addEventListener = (type, handler) => { handlers[type] = handler; };
  const requests = [];
  context.fetch = async (url, options) => {
    requests.push({ url, options });
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

test('CLI live status does not invent a model or token cost', () => {
  const base = fixture();
  const nodes = render(fixture({active_session:{provider:'cli'},account:{ok:false,error:'not_applicable'},
    routing:{...base.routing,last:{provider:'cli',model:null}},
    proxy_metrics:{configuration:{provider:'cli',cliKind:'codex',model:null,requestedModel:'requested-x',usageMeasured:false}}}));
  const html = nodes.get('grid').innerHTML;
  assert.match(html, /CLI/);
  assert.match(html, /CLI 토큰 사용량과 비용의 일일 합계는 집계되지 않습니다/);
  assert.doesNotMatch(html, /토큰과 추정 비용/);
});


test('served settings explain safe switching without a duplicate CLI engine selector', () => {
  const html = ASSETS.settings_html;
  assert.match(html, /id="default-provider"[^>]*>[\s\S]*?<option value="cli">CLI<\/option>/);
  assert.doesNotMatch(html, /id="default-cli-kind"/);
  assert.match(html, /CLI 종류와 모델은 아래 서비스 설정에서/);
  assert.match(html, /Plan은 별도 실행 선택지가 아니라/);
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
  assert.match(html,/Local 실행 슬롯 2 \/ 2/);assert.match(html,/Local 대기 3건/);
  assert.equal((html.match(/class="flow-ring occupied"/g)||[]).length,20);
  assert.match(html,/Local 작업 진행 중/);
  sandbox.render(data(1000000,1,0,0));html=context.nodes.get('grid').innerHTML;
  assert.doesNotMatch(html,/class="flow-particle/);assert.match(html,/새 응답 없음/);
  assert.equal((html.match(/class="flow-ring occupied"/g)||[]).length,0);
  context.window.matchMedia=()=>({matches:true});sandbox.render(data(2000000,2));
  assert.doesNotMatch(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  assert.match(context.nodes.get('grid').innerHTML,/1,000,000 토큰/);
});


test('flow motion respects system preference until explicit page-only opt-in, and can be paused', () => {
  const context=buildContext(), clicks=[];
  context.document.addEventListener=(type,handler)=>{if(type==='click')clicks.push(handler)};
  context.window.innerHeight=900;context.window.matchMedia=()=>({matches:true});
  context.document.getElementById('gateway-motion').getBoundingClientRect=()=>({top:100,bottom:200});
  const sandbox={...context,globalThis:null};sandbox.globalThis=sandbox;vm.createContext(sandbox);vm.runInContext(scriptBundle(),sandbox);
  const data=n=>fixture({active_session:{provider:'local'},proxy_metrics:{startedAt:'motion-test',requests:n,configuration:{provider:'local'},
    tokenFlow:{local:{responses:n,inputSamples:n,inputTokens:n*100,outputSamples:n,outputTokens:n*10}}}});
  sandbox.render(data(1));sandbox.render(data(2));
  assert.match(context.nodes.get('grid').innerHTML,/움직임 줄이기 적용 중/);
  assert.doesNotMatch(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  const click=()=>clicks.forEach(fn=>fn({target:{closest:selector=>selector==='[data-flow-motion]'?{}:null}}));
  click();
  assert.equal(sandbox.flowMotionAllowed(),true);
  assert.doesNotMatch(context.nodes.get('grid').innerHTML,/class="flow-particle/);
  sandbox.render(data(3));let html=context.nodes.get('grid').innerHTML;
  assert.match(html,/flow-motion-enabled/);assert.match(html,/움직임 끄기/);assert.match(html,/class="flow-particle/);
  click();sandbox.render(data(4));html=context.nodes.get('grid').innerHTML;
  assert.equal(sandbox.flowMotionAllowed(),false);assert.doesNotMatch(html,/class="flow-particle/);
  assert.equal(context.window.matchMedia().matches,true);
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
  assert.match(html, /외부 실제 시도/);
  assert.match(html, /Plan 실제 시도/);
  assert.match(html, /백분위는 단계별로 계산하므로 합산할 수 없습니다/);
  const old = render(fixture({proxy_metrics:{requests:3}})).get('grid').innerHTML;
  assert.match(old, /Local 슬롯 대기/);
  assert.match(old, /미보고/);
});
