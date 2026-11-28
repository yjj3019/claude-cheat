// SPDX-License-Identifier: MIT
// LLAMA_CACHE_PROBE_URL must point to a dedicated, single-slot loopback llama-server.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';

const endpoint = process.env.LLAMA_CACHE_PROBE_URL;

test('real prompt-cache canary verifies token boundaries and distinct uncached answers',
  {skip: !endpoint && process.env.LLAMA_CACHE_PROBE_REQUIRED !== '1', timeout: 90000}, async t => {
    assert.ok(endpoint, 'LLAMA_CACHE_PROBE_URL is required for this run');
    const url = new URL(endpoint);
    assert.ok(['http:', 'https:'].includes(url.protocol) && ['127.0.0.1', '[::1]'].includes(url.hostname) &&
      !url.username && !url.password && !url.search && !url.hash, 'use an isolated loopback engine');

    async function post(path, payload) {
      const res = await fetch(new URL(path, url), {method: 'POST', redirect: 'error', signal: t.signal,
        headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});
      assert.equal(res.status, 200);
      return res.json();
    }

    async function tokenize(prompt) {
      // Match native /completion tokenization, including the model's beginning-of-sequence token.
      const {tokens} = await post('/tokenize', {content: prompt, add_special: true, parse_special: true});
      assert.ok(Array.isArray(tokens) && tokens.length > 1 && tokens.every(Number.isSafeInteger));
      return tokens;
    }

    async function complete(prompt, cached, tokenCount) {
      const {timings, tokens, content} = await post('/completion', {
        prompt, id_slot: 0, temperature: 0, seed: 17, n_predict: 8, cache_prompt: cached, return_tokens: true,
      });
      assert.equal(typeof content, 'string');
      assert.ok(Array.isArray(tokens) && tokens.length > 0 && tokens.every(Number.isSafeInteger));
      const {prompt_n: fresh, cache_n: read} = timings;
      assert.ok(Number.isSafeInteger(fresh) && fresh >= 0 && Number.isSafeInteger(read) && read >= 0);
      assert.equal(fresh + read, tokenCount, 'reported input length disagrees with the tokenizer');
      if (!cached) assert.equal(read, 0, 'the reference must bypass cache');
      return {answer: {tokens, content}, read};
    }

    for (const suffix of ['The children went back to their', 'The mother looked at the sky and',
      'The puppy was very hungry, so it', 'The queen took off her']) {
      const prefix = randomUUID() + '\n' + 'The blue bird flew back to the little house by the river. '.repeat(12);
      const a = prefix + 'Once upon a time, there was a', b = prefix + suffix;
      const left = await tokenize(a), right = await tokenize(b);
      let shared = 0;
      while (shared < left.length && shared < right.length && left[shared] === right[shared]) shared++;
      assert.ok(shared > 1 && shared < Math.min(left.length, right.length));

      // First-ever B for this prefix. A previous reference to B could otherwise seed the very cache we test.
      const prime = await complete(a, true, left.length);
      const warm = await complete(b, true, right.length);
      assert.ok(warm.read <= shared && warm.read >= shared - 1, 'count outside the reusable prefix');
      const coldA = await complete(a, false, left.length);
      const coldB = await complete(b, false, right.length);
      assert.deepEqual(prime.answer, coldA.answer);
      assert.deepEqual(warm.answer, coldB.answer, 'cached request returned the wrong continuation');
      if (coldA.answer.content !== coldB.answer.content) {
        assert.deepEqual((await complete(b, false, right.length)).answer, coldB.answer);
        return;
      }
    }
    assert.fail('inconclusive: all uncached probe pairs generated identical text');
  });
