import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from usage_ledger import UsageLedger


NODE_FIXTURE = r"""
import { randomUUID } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createProxy } from './claudeRouteProxy.mjs';
import { listenSafe } from './tests/listenSafe.mjs';
const root = process.argv[1];
const model = 'fixture-model';
const answer = (m, usage) => ({ id: 'fixture', type: 'message', role: 'assistant', model: m,
  content: [{ type: 'text', text: 'fixture reply' }], stop_reason: 'end_turn', usage });
const servers = [];
async function instance(fallback, stream = false) {
  const id = randomUUID();
  const directory = join(root, '.usage-stats', 'instances', id);
  await mkdir(directory, { recursive: true });
  const events = join(directory, 'events.jsonl');
  const server = createProxy({ nonce: 'test-nonce', provider: stream ? 'openrouter' : 'local', localModel: model,
    key: 'sk-or-fixture', openrouterModel: model, streamPassthrough: stream,
    localUrl: 'http://127.0.0.1:11434/v1/messages', retryDelayMs: 1, cooldownMs: 0,
    instanceId: id, usageEventsPath: events,
    fetchImpl: async url => {
      if (fallback && !url.includes('api.anthropic.com')) return new Response('unavailable', {status: 500});
      if (!stream) return Response.json(answer(fallback ? 'claude-sonnet-5-5' : model, {input_tokens: 11, output_tokens: 7}));
      const frame = (type, values) => `event: ${type}\ndata: ${JSON.stringify({type,...values})}\n\n`;
      const frames=[frame('message_start',{message:{id:'fixture',role:'assistant',model,usage:{input_tokens:11,output_tokens:0}}}),
        frame('content_block_start',{index:0,content_block:{type:'text',text:''}}),
        frame('content_block_delta',{index:0,delta:{type:'text_delta',text:'fixture reply'}}),
        frame('content_block_stop',{index:0}),frame('message_delta',{delta:{stop_reason:'end_turn'},usage:{output_tokens:7}}),
        frame('message_stop',{})].join('');
      return new Response(frames,{headers:{'content-type':'text/event-stream'}});
    } });
  servers.push(server); await listenSafe(server);
  return {id, events, stream, url: `http://127.0.0.1:${server.address().port}/v1/messages`};
}
try {
  const sessions = await Promise.all([instance(false), instance(true), ...(process.argv[2] === 'stream' ? [instance(false,true)] : [])]);
  const request = {model: 'claude-sonnet-5-5', max_tokens: 32, messages: [{role:'user',content:'private fixture'}]};
  const results = await Promise.all(sessions.map(s => fetch(s.url, {method:'POST',
    headers: {'content-type':'application/json','x-local-route-token':'test-nonce',
      authorization:'Bearer sk-ant-oat-fixture','anthropic-beta':'oauth-2025-04-20'},
    body: JSON.stringify({...request,stream:s.stream})})));
  for (const r of results) { if (r.status !== 200) throw Error(`status ${r.status}`); await r.text(); }
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
                plan = rows[plan_id.replace('-', '')]
                self.assertEqual((local["provider"], local["requested_model"], local["reported_model"]),
                                 ("local", "fixture-model", "fixture-model"))
                self.assertEqual((local["requests"], local["input_tokens"], local["output_tokens"]), (1, 11, 7))
                self.assertEqual((plan["provider"], plan["requests"], plan["reported_model"]), ("plan", 1, None))
                self.assertIsNone(plan["input_tokens"])
                self.assertIsNone(plan["output_tokens"])

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
