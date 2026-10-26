// Runs the dashboard page script against a minimal DOM stub so the rendering layer is
// covered without a browser: cards, alert de-duplication, summary and copy buttons.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../routeDashboard.py', import.meta.url), 'utf-8');

function scriptBundle() {
  const grab = name => {
    // A shared bundle may be assembled from a base assignment plus later `+=` appends
    // (cost provenance labels are appended), so collect every assignment in order.
    const matches = [...SOURCE.matchAll(new RegExp(`^${name}\\s*(?:\\+=|=)\\s*(".*")$`, 'gm'))];
    assert.ok(matches.length, `${name} not found in routeDashboard.py`);
    return matches.map(match => JSON.parse(match[1])).join('\n');
  };
  return grab('JS_SHARED') + grab('JS_DASHBOARD');
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
    fetch: async () => ({ json: async () => ({}) }),
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
  const grab = name => {
    const matches = [...SOURCE.matchAll(new RegExp(`^${name}\\s*(?:\\+=|=)\\s*(".*")$`, 'gm'))];
    assert.ok(matches.length, `${name} not found in routeDashboard.py`);
    return matches.map(match => JSON.parse(match[1])).join('\n');
  };
  return grab('JS_SHARED') + '\n' + grab('JS_SETTINGS');
}

const READY_OPENROUTER = { fields: {
  OPENROUTER_MODEL: { value: 'vendor/model', set: true },
  ANTHROPIC_AUTH_TOKEN: { secret: true, set: true, preview: 'sk-or-…abcd', value: '' } } };
const READY_LOCAL = { fields: {
  LOCAL_LLM_URL: { value: 'http://127.0.0.1:11434/v1/messages', set: true },
  LOCAL_LLM_MODEL: { value: 'llama', set: true } } };

async function settingsPage({ config, store = new Map(), blockedStorage = false } = {}) {
  const context = buildContext();
  const handlers = {};
  const select = context.document.getElementById('provider-select');
  select.addEventListener = (type, handler) => { handlers[type] = handler; };
  context.fetch = async () => ({ json: async () => config });
  context.localStorage = blockedStorage
    ? { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } }
    : { getItem: key => (store.has(key) ? store.get(key) : null), setItem: (key, value) => store.set(key, value) };
  const sandbox = { ...context, globalThis: null };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(settingsBundle(), sandbox, { filename: 'settings.js' });
  await new Promise(resolve => setImmediate(resolve));
  return { nodes: context.nodes, select, handlers, store, sandbox };
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
  assert.match(page.select.innerHTML, /CLI \(연결 테스트 전용\) · 설정 필요/);
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

