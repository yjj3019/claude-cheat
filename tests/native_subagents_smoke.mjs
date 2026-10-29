// Opt-in installed-Claude check; all model responses are synthetic loopback fixtures.
// node tests/native_subagents_smoke.mjs [2|4]
// This checks frontend orchestration, not a Local model's reasoning or Agent Teams.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { listenSafe } from './listenSafe.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
delete process.env.ROUTE_LOG_PATH;
delete process.env.ROUTE_METRICS_PATH;
const { createProxy } = await import('../claudeRouteProxy.mjs');
const count = Number(process.argv[2] || 2);
assert.ok([2, 4].includes(count), 'choose 2 or 4 workers');
const resolve = spawnSync('python', ['-c', 'import shutil,routeCli,json; print(json.dumps(routeCli.resolve_command(shutil.which("claude"))))'],
  { cwd: root, encoding: 'utf8', timeout: 15000, windowsHide: true });
assert.equal(resolve.status, 0, 'installed Claude command must resolve');
const [command, ...prefix] = JSON.parse(resolve.stdout);
const version = spawnSync(command, [...prefix, '--version'], { encoding: 'utf8', timeout: 15000, windowsHide: true }).stdout.trim();

function sse(message) {
  const frame = (type, data = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;
  let out = frame('message_start', { message: { ...message, content: [], stop_reason: null, usage: { input_tokens: 1, output_tokens: 0 } } });
  message.content.forEach((block, index) => {
    out += frame('content_block_start', { index, content_block: block.type === 'tool_use' ? { ...block, input: {} } : { type: 'text', text: '' } });
    out += frame('content_block_delta', { index, delta: block.type === 'tool_use'
      ? { type: 'input_json_delta', partial_json: JSON.stringify(block.input) }
      : { type: 'text_delta', text: block.text } });
    out += frame('content_block_stop', { index });
  });
  return out + frame('message_delta', { delta: { stop_reason: message.stop_reason, stop_sequence: null }, usage: { output_tokens: 1 } }) + frame('message_stop');
}

async function run(mode) {
  const dir = await mkdtemp(join(tmpdir(), `claude-subagent-${mode}-`));
  const settings = join(dir, 'settings.json');
  await writeFile(settings, JSON.stringify({ disableAllHooks: true, autoMemoryEnabled: false,
    env: { CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: '0' } }));
  const observed = { requests: 0, childCalls: 0, active: 0, maxActive: 0, returned: 0 };
  const fixture = async (_url, options) => {
    const body = JSON.parse(options.body);
    observed.requests++;
    const messages = body.messages || [];
    const last = messages.findLast(message => message.role === 'user');
    const lastText = JSON.stringify(last?.content || '');
    let content, stop = 'end_turn';
    // Only delivered user/system messages can acknowledge completion; never count
    // the assistant's own generated text or tool inputs as returned worker results.
    const completions = new Set(JSON.stringify(messages.filter(message => ['user', 'system'].includes(message.role)))
      .match(/NATIVE_CHILD_DONE_\d+/g) || []);
    if (/NATIVE_CHILD_\d+:/.test(lastText) && !lastText.includes('tool_result')) {
      observed.childCalls++; observed.active++;
      observed.maxActive = Math.max(observed.maxActive, observed.active);
      await new Promise(r => setTimeout(r, 300)); observed.active--;
      const index = lastText.match(/NATIVE_CHILD_(\d+)/)?.[1];
      assert.notEqual(index, undefined);
      content = [{ type: 'text', text: `NATIVE_CHILD_DONE_${index}` }];
    } else if (Array.isArray(last?.content) && last.content.some(b => b.type === 'tool_result')) {
      observed.returned = completions.size;
      observed.completedIds = [...completions].sort();
      content = [{ type: 'text', text: completions.size === count ? 'NATIVE_ROOT_DONE' : 'Waiting for worker completion.' }];
    } else if (lastText.includes('NATIVE_ROOT')) {
      const agent = body.tools?.find(tool => tool.name === 'Agent');
      assert.ok(agent, 'Agent tool is advertised');
      observed.agentFields = Object.keys(agent.input_schema?.properties || {});
      content = Array.from({ length: count }, (_, i) => ({ type: 'tool_use', id: `call_fixture_${i}`, name: 'Agent',
        input: { description: `Fixture worker ${i}`, prompt: `NATIVE_CHILD_${i}: return the fixture response`, subagent_type: 'fixture-worker',
          ...(observed.agentFields.includes('run_in_background') ? { run_in_background: false } : {}) } }));
      stop = 'tool_use';
    } else content = [{ type: 'text', text: 'OK' }];
    const message = { id: 'msg_fixture', type: 'message', role: 'assistant', model: body.model,
      content, stop_reason: stop, stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 } };
    return body.stream ? new Response(sse(message), { headers: { 'content-type': 'text/event-stream' } }) : Response.json(message);
  };
  const nonce = 'synthetic-route-token';
  const server = mode === 'proxy' ? createProxy({ provider: 'local', nonce, localUrl: 'http://127.0.0.1:11434/v1/messages',
    localModel: 'fixture-local', fetchImpl: fixture, metricsPath: null, retryDelayMs: 0 }) : createServer(async (req, res) => {
      if (req.method !== 'POST' || !req.url.startsWith('/v1/messages')) { res.writeHead(404); res.end(); return; }
      let raw = ''; for await (const chunk of req) raw += chunk;
      if (req.url.includes('count_tokens')) { res.setHeader('content-type', 'application/json'); res.end('{"input_tokens":1}'); return; }
      const reply = await fixture(req.url, { body: raw });
      res.writeHead(reply.status, Object.fromEntries(reply.headers)); res.end(await reply.text());
    });
  await listenSafe(server);
  const base = `http://127.0.0.1:${server.address().port}`;
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(CLAUDE|ANTHROPIC|OPENROUTER|LOCAL_LLM|ROUTE_)/i.test(key)));
  Object.assign(env, { CLAUDE_CONFIG_DIR: dir, ANTHROPIC_BASE_URL: base, ANTHROPIC_AUTH_TOKEN: 'sk-ant-oat-synthetic',
    ANTHROPIC_CUSTOM_HEADERS: `X-Local-Route-Token: ${nonce}\nanthropic-beta: oauth-2025-04-20`,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: '0' });
  const args = [...prefix, '-p', '--output-format', 'json', '--model', 'sonnet', '--max-turns', '4',
    '--settings', settings, '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
    '--tools', 'Agent', '--allowedTools', 'Agent', '--agents', JSON.stringify({ 'fixture-worker': {
      description: 'Synthetic test worker', prompt: 'Return the synthetic response.', tools: [], model: 'sonnet' } }),
    '--', 'NATIVE_ROOT: run the fixture workers'];
  let child;
  try {
    const result = await new Promise(resolveResult => {
      child = spawn(command, args, { cwd: dir, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = '', stderr = '', exceeded = false;
      const timer = setTimeout(() => { exceeded = true; child.kill(); }, 60000);
      const collect = target => chunk => {
        if (stdout.length + stderr.length > 1024 * 1024) { exceeded = true; child.kill(); return; }
        if (target === 'out') stdout += chunk; else stderr += chunk;
      };
      child.stdout.on('data', collect('out')); child.stderr.on('data', collect('err'));
      child.on('error', () => { clearTimeout(timer); resolveResult({ code: null, exceeded: true, stdout: '' }); });
      child.on('close', code => { clearTimeout(timer); resolveResult({ code, exceeded, stdout }); });
    });
    let parsed; try { parsed = JSON.parse(result.stdout.trim().split('\n').at(-1)); } catch {}
    const report = { mode, version, count, ...observed, exit: result.code, timedOut: result.exceeded,
      resultType: parsed?.subtype || 'missing', rootCompleted: parsed?.result?.includes('NATIVE_ROOT_DONE') === true };
    if (mode === 'proxy') {
      const metrics = await (await fetch(base + '/api/metrics', { headers: { 'x-local-route-token': nonce } })).json();
      report.routes = metrics.routes; report.reasons = metrics.reasons;
    }
    console.log(JSON.stringify(report));
    assert.equal(result.code, 0); assert.equal(result.exceeded, false); assert.equal(report.rootCompleted, true);
    assert.equal(parsed?.subtype, 'success'); assert.equal(parsed?.is_error, false);
    assert.equal(observed.childCalls, count); assert.equal(observed.returned, count);
    assert.deepEqual(observed.completedIds, Array.from({ length: count }, (_, index) => `NATIVE_CHILD_DONE_${index}`));
    assert.ok(observed.maxActive >= 2, 'at least two worker requests must overlap');
    if (mode === 'proxy') {
      assert.equal(report.routes.plan, 0, 'synthetic workers must not silently fall back to Plan');
      assert.equal(report.routes.external, observed.requests);
    }
  } finally {
    if (child && child.exitCode === null) child.kill();
    server.closeAllConnections(); await new Promise(r => server.close(r));
    // Keep the owned temporary fixture directory for debugging; it has no real credentials.
  }
}

await run('direct');
await run('proxy');
