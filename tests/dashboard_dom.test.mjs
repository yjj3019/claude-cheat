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
    contains: () => false, dispatchEvent() {}, setAttribute() {}, getAttribute: () => null,
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
    addEventListener() {},
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
  const nodes = render(fixture({ routing: { ...base.routing, last: { ...base.routing.last, provider: 'local' } },
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
  assert.equal(nodes.get('live-text').textContent, '외부 서비스 사용 중');
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
test('the CLI card explains it is diagnosis only and lists the detected clients', async () => {
  const kinds = [
    { kind: 'codex', found: true, name: 'codex.cmd', dir: 'C:\\npm', version: '0.164.0' },
    { kind: 'grok', found: false, reason: 'not_found' },
  ];
  const page = await settingsPage({ config: { openrouter: READY_OPENROUTER, local: READY_LOCAL } });
  const sandboxEval = code => vm.runInContext(code, page.sandbox);
  sandboxEval(`CLI_DETECT = ${JSON.stringify(kinds)}; renderSettings(true)`);
  const html = page.nodes.get('settings').innerHTML;
  assert.match(html, /id="card-cli"/);
  assert.match(html, /요청 라우팅은 아직 지원하지 않습니다/);
  assert.match(html, /Reply with exactly OK/);
  assert.match(html, /<select class="input" id="cfg-cli-CLI_KIND"/);
  assert.match(html, /<option value="codex">codex<\/option>/);
  assert.match(html, /감지됨/);
  assert.match(html, /0\.164\.0/);
  assert.match(html, /설치된 CLI를 찾지 못했습니다/);
  assert.match(html, />연결 테스트 \(외부 호출\)</);
  assert.doesNotMatch(html, /data-picker="cli"/, 'a CLI has no model list');
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



test('CLI launch default saves its nested engine before selecting the service', async () => {
  const page = await settingsPage({config:{openrouter:READY_OPENROUTER,local:READY_LOCAL,default_provider:'local',
    cli:{fields:{CLI_KIND:{value:'codex'},CLI_MODEL:{value:'old-model'},CLI_BINARY:{value:'old-binary'},CLI_TIMEOUT_S:{value:'45'}}}}});
  page.nodes.get('default-provider').value = 'cli';
  vm.runInContext('renderDefaultCli()', page.sandbox);
  assert.equal(page.nodes.get('default-cli-options').hidden, false);
  page.nodes.get('default-cli-kind').value = 'claude';
  const calls = [];
  page.sandbox.fetch = async (url, options) => {
    const payload = JSON.parse(options.body); calls.push({url,payload});
    return {json:async()=>url === '/api/config' ? {saved:true,config:{cli:{fields:{CLI_KIND:{value:'claude'}}}}} : {saved:true,default_provider:'cli'}};
  };
  await vm.runInContext('saveDefaultProvider()', page.sandbox);
  assert.deepEqual(calls.map(call=>call.url), ['/api/config','/api/default-provider']);
  assert.deepEqual(calls[0].payload, {provider:'cli',values:{CLI_KIND:'claude',CLI_MODEL:'',CLI_BINARY:'',CLI_TIMEOUT_S:'45'}});
  assert.deepEqual(calls[1].payload, {provider:'cli'});
  assert.equal(vm.runInContext('CONFIG.default_provider', page.sandbox), 'cli');
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


test('served settings offer CLI as the third backend with three nested engines', () => {
  const html = ASSETS.settings_html;
  assert.match(html, /id="default-provider"[^>]*>[\s\S]*?<option value="cli">CLI<\/option>/);
  assert.match(html, /id="default-cli-kind">[\s\S]*?<option value="claude">Claude Code<\/option>[\s\S]*?<option value="codex">Codex<\/option>[\s\S]*?<option value="grok">Grok<\/option>/);
  assert.doesNotMatch(html, /CLI는 감지·연결 테스트 전용/);
});
