# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cliBackend
from usage_ledger import UsageLedger


NODE_FIXTURE = r"""
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { mkdir, readFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createProxy } from './claudeRouteProxy.mjs';
import { listenSafe } from './tests/listenSafe.mjs';
const root = process.argv[1];
const bridge = process.argv[2] === 'cli' ? JSON.parse(readFileSync(0, 'utf8')) : null;
const model = 'fixture-model';
const answer = (m, usage) => ({ id: 'fixture', type: 'message', role: 'assistant', model: m,
  content: [{ type: 'text', text: 'fixture reply' }], stop_reason: 'end_turn', usage });
const servers = [];
async function instance(fallback, stream = false) {
  const id = randomUUID();
  const directory = join(root, '.usage-stats', 'instances', id);
  await mkdir(directory, { recursive: true });
  const events = join(directory, 'events.jsonl');
  const config = bridge
    ? { provider: 'cli', localModel: 'cli/grok', cliKind: 'grok', localKey: bridge.token, localUrl: bridge.url }
    : { provider: (stream || fallback) ? 'openrouter' : 'local', localModel: model, localUrl: 'http://127.0.0.1:11434/v1/messages' };
  const server = createProxy({ ...config, nonce: 'test-nonce',
    key: 'sk-or-fixture', openrouterModel: model, streamPassthrough: stream,
    retryDelayMs: 1, cooldownMs: 0,
    instanceId: id, usageEventsPath: events,
    fetchImpl: async (url, options) => {
      if (bridge) {
        if (url !== bridge.url) throw Error('unexpected fixture destination');
        return fetch(url, options);
      }
      // fallback machine: the openrouter external leg fails (500), then the plan leg answers.
      if (fallback && !url.includes('api.anthropic.com')) return new Response('unavailable', {status: 500});
      if (fallback) return Response.json(answer('claude-sonnet-5-5', {input_tokens: 11, output_tokens: 7}));
      if (!stream) return Response.json(answer(model, {input_tokens: 11, output_tokens: 7}));
      const frame = (type, values) => `event: ${type}\ndata: ${JSON.stringify({type,...values})}\n\n`;
      const frames=[frame('message_start',{message:{id:'fixture',role:'assistant',model,usage:{input_tokens:11,output_tokens:0}}}),
        frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
        frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'fixture reply'}}),
        frame('content_block_stop',{index:0}),frame('message_delta',{delta:{stop_reason:'end_turn'},usage:{output_tokens:7}}),
        frame('message_stop',{})].join('');
      return new Response(frames,{headers:{'content-type':'text/event-stream'}});
    } });
  servers.push(server); await listenSafe(server);
  return {id, events, stream, fallback, url: `http://127.0.0.1:${server.address().port}/v1/messages`};
}
try {
  const sessions = bridge ? [await instance(false)] : await Promise.all([instance(false), instance(true), ...(process.argv[2] === 'stream' ? [instance(false,true)] : [])]);
  const request = {model: 'claude-sonnet-5-5', max_tokens: 32, messages: [{role:'user',content:bridge ? 'x'.repeat(40000) : 'private fixture'}]};
  const results = await Promise.all(sessions.map(s => fetch(s.url, {method:'POST',
    headers: {'content-type':'application/json','x-local-route-token':'test-nonce',
      authorization:'Bearer sk-ant-oat-fixture','anthropic-beta':'oauth-2025-04-20'},
    body: JSON.stringify({...request,stream:s.stream})})));
  for (let i = 0; i < results.length; i++) {
    const r = results[i]; const expected = sessions[i].fallback || bridge?.expectedError ? 503 : 200;
    if (r.status !== expected) throw Error(`status ${r.status} (expected ${expected})`);
    if (bridge) {
      const body = await r.json();
      const metricsUrl = sessions[i].url.replace('/v1/messages', '/api/metrics');
      const metrics = await (await fetch(metricsUrl, {headers:{'x-local-route-token':'test-nonce'}})).json();
      if (bridge.expectedError) {
        assert.ok(JSON.stringify(body).includes(bridge.expectedError));
        assert.equal(metrics.efficiency.measured_calls, 0);
      } else {
        for (const direction of ['input', 'output']) {
          const tokens = bridge.usage[direction + '_tokens'] ?? 0;
          assert.equal(body.usage[direction + '_tokens'], tokens);
          assert.equal(metrics.efficiency['observed_' + direction], tokens);
        }
      }
    } else {
      await r.text();
    }
  }
  for (const s of sessions) {
    let found = false;
    for (let i = 0; i < 200; i++) {
      try { if ((await readFile(s.events, 'utf8')).endsWith('\n')) {found = true; break;} }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    if (!found) throw Error('event not persisted');
  }
  console.log(JSON.stringify(sessions.map(s => s.id)));
} finally {
  await Promise.all(servers.map(s => new Promise(resolve => {s.closeAllConnections(); s.close(resolve);} )));
}
"""


@unittest.skipUnless(shutil.which("node"), "node is required")
class UsageEndToEndTests(unittest.TestCase):
    def test_long_grok_prompt_from_bridge_through_proxy_to_usage_ledger(self):
        root_dir = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "fake_grok.py"
            fake.write_text("""import json, sys
from pathlib import Path
args = sys.argv[1:]
def flag(name): return args[args.index(name) + 1]
assert '-p' not in args and all('x' * 40 not in arg for arg in args)
assert sys.stdin.buffer.read() == b''
assert flag('--output-format') == 'json' and 'properties' in json.loads(flag('--json-schema'))
assert '--disable-web-search' in args and '--no-subagents' in args and flag('--disallowed-tools')
source = Path(flag('--prompt-file'))
assert source.parent == Path.cwd()
request = json.loads(source.read_text(encoding='utf-8').split('REQUEST_JSON:\\n', 1)[1])
assert request['messages'][0]['content'] == 'x' * 40000
usage = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8'))
print(json.dumps({'stopReason': 'end_turn', 'model': 'fixture-native',
                  'text': json.dumps({'text': 'ok', 'tool_calls': []}),
                  'usage': usage}))
""", encoding="utf-8")
            with patch.multiple(cliBackend.routeCli, find_binary=lambda *_: {"path": str(fake)},
                                resolve_command=lambda _: [sys.executable, str(fake)],
                                probe_version=lambda *_: {"ok": True, "version": "1.0.77"}), \
                 cliBackend.serve({"CLI_KIND": "grok"}) as (_, url, token):
                for name, usage in (
                    ("input", {"input_tokens": 7, "cache_read_input_tokens": 0}),
                    ("output", {"output_tokens": 0}),
                    ("cache", {"cache_creation_input_tokens": 2}),
                    ("unknown", {}),
                    ("complete", {"input_tokens": 0, "output_tokens": 9}),
                    ("maximum", {"input_tokens": 2**31 - 1, "output_tokens": 3}),
                    ("overflow", {"input_tokens": 2**31, "output_tokens": 3}),
                ):
                    with self.subTest(case=name):
                        fake.with_suffix(".json").write_text(json.dumps(usage), encoding="utf-8")
                        workspace = Path(temp) / name
                        bridge = {"url": url, "token": token, "usage": usage}
                        if name == "overflow":
                            bridge["expectedError"] = "invalid_usage"
                        result = subprocess.run(["node", "--input-type=module", "-e", NODE_FIXTURE, str(workspace), "cli"],
                                                input=json.dumps(bridge), cwd=root_dir,
                                                text=True, capture_output=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        ledger = UsageLedger(workspace)
                        self.assertEqual(ledger.collect()["events_inserted"], 1)
                        row, = ledger.report("day")["rows"]
                        if name == "overflow":
                            self.assertEqual((row["ok"], row["input_samples"], row["output_samples"]), (0, 0, 0))
                            self.assertIsNone(row["input_tokens"])
                            self.assertIsNone(row["output_tokens"])
                            continue
                        self.assertEqual((row["provider"], row["reported_model"], row["ok"]), ("cli", "fixture-native", 1))
                        for field in ("input", "output"):
                            key = field + "_tokens"
                            self.assertEqual((row[key], row[field + "_samples"]), (usage.get(key), int(key in usage)))
                        self.assertEqual(row["cache_read_tokens"], usage.get("cache_read_input_tokens"))
                        self.assertEqual(row["cache_creation_tokens"], usage.get("cache_creation_input_tokens"))

    def test_two_proxy_sessions_to_ledger_and_repeat_collection(self):
        root_dir = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                ["node", "--input-type=module", "-e", NODE_FIXTURE, temp],
                cwd=root_dir, text=True, capture_output=True, timeout=20, check=True,
            )
            local_id, plan_id = json.loads(result.stdout.strip())
            ledger = UsageLedger(temp)
            first = ledger.collect()
            self.assertEqual(first["events_inserted"], 2)
            self.assertEqual(first["errors"], [])
            second = ledger.collect()
            self.assertEqual(second["events_inserted"], 0)
            self.assertEqual(second["errors"], [])
            for period in ("day", "week", "month"):
                report = ledger.report(period)
                rows = {row["instance_id"]: row for row in report["rows"]}
                self.assertEqual(set(rows), {local_id.replace('-', ''), plan_id.replace('-', '')})
                local = rows[local_id.replace('-', '')]
                failed = rows[plan_id.replace('-', '')]
                self.assertEqual((local["provider"], local["requested_model"], local["reported_model"]),
                                 ("local", "fixture-model", "fixture-model"))
                self.assertEqual((local["requests"], local["input_tokens"], local["output_tokens"]), (1, 11, 7))
                # The restored-tree policy: an upstream openrouter 500 exhausts retries and is
                # answered 503 (Prioritized no-Plan passthrough); its outcome is still recorded.
                self.assertEqual((failed["provider"], failed["requests"], failed["reported_model"]), ("openrouter", 1, None))
                self.assertIsNone(failed["input_tokens"])
                self.assertIsNone(failed["output_tokens"])

    def test_streamed_usage_reaches_day_week_and_month_reports(self):
        root_dir = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(["node", "--input-type=module", "-e", NODE_FIXTURE, temp, "stream"],
                                    cwd=root_dir, text=True, capture_output=True, timeout=20, check=True)
            _, _, stream_id = json.loads(result.stdout.strip())
            ledger = UsageLedger(temp)
            self.assertEqual(ledger.collect()["events_inserted"], 3)
            self.assertEqual(ledger.collect()["events_inserted"], 0)
            for period in ("day", "week", "month"):
                rows = ledger.report(period)["rows"]
                stream = next(row for row in rows if row["instance_id"] == stream_id.replace('-', ''))
                self.assertEqual((stream["provider"], stream["requests"], stream["input_tokens"], stream["output_tokens"]),
                                 ("openrouter", 1, 11, 7))


if __name__ == "__main__":
    unittest.main()
