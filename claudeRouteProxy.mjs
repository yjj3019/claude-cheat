import http from 'node:http';
import { once } from 'node:events';
import { pathToFileURL } from 'node:url';

export const DEEPSEEK = 'deepseek/deepseek-v4.1-flash-1017';
const isPro = model => typeof model === 'string' && /^claude-(opus|haiku|fable)-[0-9][a-zA-Z0-9.-]*$/.test(model);
const isSonnet = model => typeof model === 'string' && /^claude-sonnet-[0-9][a-zA-Z0-9.-]*$/.test(model);
const SEGMENT = String.raw`(?:[A-Za-z0-9_\-~:@+][A-Za-z0-9_\-.~:@+]{0,199}|\.[A-Za-z0-9_\-~:@+][A-Za-z0-9_\-.~:@+]{0,198}|\.\.[A-Za-z0-9_\-.~:@+]{1,198})`;
const PATTERNS = new Map([
  [String.raw`^(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}$`, `^${SEGMENT}$`],
  [String.raw`^(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}(?:\/(?!\.\.?(?:\/|$))[A-Za-z0-9_\-.~:@+]{1,200}){0,14}$`, `^${SEGMENT}(?:/${SEGMENT}){0,14}$`],
]);
const SCHEMA_MAPS = new Set(['properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas']);
const SCHEMA_ARRAYS = new Set(['allOf', 'anyOf', 'oneOf', 'prefixItems']);
const SCHEMA_NODES = new Set(['additionalProperties', 'unevaluatedProperties', 'propertyNames', 'items',
  'additionalItems', 'unevaluatedItems', 'contains', 'if', 'then', 'else', 'not', 'contentSchema']);

function compatibleSchema(schema) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return schema;
  return Object.fromEntries(Object.entries(schema).map(([name, value]) => {
    if (name === 'pattern' && PATTERNS.has(value)) value = PATTERNS.get(value);
    else if (SCHEMA_MAPS.has(name) && value && typeof value === 'object' && !Array.isArray(value))
      value = Object.fromEntries(Object.entries(value).map(([key, child]) => [key, compatibleSchema(child)]));
    else if ((SCHEMA_ARRAYS.has(name) || name === 'items') && Array.isArray(value)) value = value.map(compatibleSchema);
    else if (SCHEMA_NODES.has(name)) value = compatibleSchema(value);
    return [name, value];
  }));
}

const STOP_REASONS = new Set(['end_turn', 'max_tokens', 'stop_sequence', 'tool_use', 'refusal', 'model_context_window_exceeded']);
function validContentBlock(block) {
  if (!block || typeof block !== 'object') return false;
  if (block.type === 'text') return typeof block.text === 'string';
  if (block.type === 'thinking') return typeof block.thinking === 'string';
  if (block.type === 'redacted_thinking') return typeof block.data === 'string';
  if (block.type === 'tool_use') return typeof block.id === 'string' && typeof block.name === 'string' &&
    block.input !== null && typeof block.input === 'object' && !Array.isArray(block.input);
  return false;
}

const MAX_RESPONSE = 32 * 1024 * 1024;
const BASIC_FIELDS = new Set(['model', 'messages', 'max_tokens', 'system', 'stream', 'temperature',
  'top_p', 'top_k', 'stop_sequences', 'tools', 'tool_choice', 'metadata', 'thinking', 'output_config', 'safeguards', 'cache_control']);
const CONTENT_FIELDS = {
  text: new Set(['type', 'text', 'cache_control']),
  image: new Set(['type', 'source', 'cache_control']),
  tool_use: new Set(['type', 'id', 'name', 'input', 'cache_control']),
  tool_result: new Set(['type', 'tool_use_id', 'content', 'is_error', 'cache_control']),
};
function externalCompatible(body, beta) {
  // Unknown betas/fields and native history belong to the subscription API. Never strip them.
  if ((beta || '').split(',').map(x => x.trim()).filter(Boolean).some(x =>
    !/^oauth-\d{4}-\d{2}-\d{2}$/.test(x) && !['claude-code-20250219', 'prompt-caching-2024-07-31'].includes(x))) return false;
  if (Object.keys(body).some(name => !BASIC_FIELDS.has(name))) return false;
  if (!Array.isArray(body.messages) || body.messages.some(message => !message ||
      !['user', 'assistant'].includes(message.role) || Object.keys(message).some(name => !['role', 'content'].includes(name)))) return false;
  if (body.thinking && (!['enabled', 'disabled', 'adaptive'].includes(body.thinking.type) || Object.keys(body.thinking).some(name => !['type', 'budget_tokens'].includes(name)))) return false;
  if (body.output_config && Object.keys(body.output_config).some(name => name !== 'effort')) return false;
  if (body.tool_choice && Object.keys(body.tool_choice).some(name => !['type', 'name', 'disable_parallel_tool_use'].includes(name))) return false;
  const contentOK = content => typeof content === 'string' || (Array.isArray(content) && content.every(block => {
    if (!block || !CONTENT_FIELDS[block.type] || Object.keys(block).some(key => !CONTENT_FIELDS[block.type].has(key))) return false;
    return block.type !== 'tool_result' || contentOK(block.content);
  }));
  if (!body.messages.every(message => contentOK(message.content)) ||
      (body.system !== undefined && !contentOK(body.system))) return false;
  if (body.tools !== undefined && (!Array.isArray(body.tools) || body.tools.some(tool =>
    !tool || typeof tool.name !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(tool.name) ||
    !tool.input_schema || typeof tool.input_schema !== 'object' || Array.isArray(tool.input_schema) ||
    Object.keys(tool).some(name => !['name', 'description', 'input_schema', 'cache_control', 'type'].includes(name)) ||
    (tool.type !== undefined && tool.type !== 'custom')))) return false;
  return true;
}

function localEndpoint(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('Local Messages URL required'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/v1/messages')
    throw new Error('Local Messages URL must be a loopback Messages endpoint');
  return url.href;
}

async function bufferedResponse(upstream, model, clientModel, stream, signal) {
  if (!upstream.ok || !upstream.body) throw new Error('External request failed');
  const sse = (upstream.headers.get('content-type') || '').includes('text/event-stream');
  if (stream === true && !sse) throw new Error('Invalid response');
  const reader = upstream.body.getReader();
  const chunks = [];
  let size = 0, eventPending = '';
  const eventDecoder = new TextDecoder();
  const cancel = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE) throw new Error('Response too large');
      chunks.push(Buffer.from(value));
      if (sse) {
        eventPending += eventDecoder.decode(value, { stream: true });
        let boundary, terminal = false;
        while ((boundary = /\r?\n\r?\n/.exec(eventPending))) {
          const frame = eventPending.slice(0, boundary.index);
          eventPending = eventPending.slice(boundary.index + boundary[0].length);
          const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
          if (data && data !== '[DONE]' && JSON.parse(data).type === 'message_stop') terminal = true;
        }
        // Messages completion is sufficient even if the provider leaves the HTTP stream open.
        // Validate every frame already received before publishing any part of the response.
        if (terminal) break;
      }
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (!sse) {
    const response = JSON.parse(text);
    if (response.type !== 'message' || response.model !== model || !Array.isArray(response.content) ||
        !response.content.every(validContentBlock) || !STOP_REASONS.has(response.stop_reason)) throw new Error('Invalid message response');
    response.model = clientModel;
    return { contentType: 'application/json', text: JSON.stringify(response) };
  }
  let started = false, delta = false, stopped = false;
  const blocks = new Map();
  const indices = new Set();
  const frames = text.split(/\r?\n\r?\n/);
  const tail = frames.pop();
  if (tail.trim() && tail.trim() !== 'data: [DONE]' && tail.trim() !== 'data:[DONE]')
    throw new Error('Trailing incomplete event');
  const output = [];
  for (const frame of frames) {
    const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
    if (!data) { output.push(frame + '\n\n'); continue; }
    if (data === '[DONE]') { if (!stopped) throw new Error('Premature stream terminator'); continue; }
    const event = JSON.parse(data);
    if (!['message_start', 'content_block_start', 'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop', 'ping'].includes(event.type)) throw new Error('Unexpected stream event');
    if (stopped) { if (event.type === 'ping') continue; throw new Error('Event after message stop'); }
    if (event.type === 'message_start') {
      if (started || event.message?.model !== model) throw new Error('Wrong response model');
      started = true;
      event.message.model = clientModel;
    } else if (event.type !== 'ping' && !started) throw new Error('Missing start');
    if (event.type === 'content_block_start') {
      if (!Number.isInteger(event.index) || event.index < 0 || indices.has(event.index) || delta ||
          !validContentBlock(event.content_block)) throw new Error('Invalid content block');
      indices.add(event.index);
      blocks.set(event.index, { type: event.content_block.type, json: '', hasJson: false });
    }
    if (event.type === 'content_block_delta') {
      const block = blocks.get(event.index);
      if (!block || !event.delta) throw new Error('Unknown content block');
      if (event.delta.type === 'input_json_delta' && block.type === 'tool_use' && typeof event.delta.partial_json === 'string') {
        block.hasJson = true;
        block.json += event.delta.partial_json;
      } else if (!((event.delta.type === 'text_delta' && block.type === 'text' && typeof event.delta.text === 'string') ||
          (event.delta.type === 'thinking_delta' && block.type === 'thinking' && typeof event.delta.thinking === 'string') ||
          (event.delta.type === 'signature_delta' && block.type === 'thinking' && typeof event.delta.signature === 'string')))
        throw new Error('Invalid content delta');
    }
    if (event.type === 'content_block_stop') {
      const block = blocks.get(event.index);
      if (!block) throw new Error('Unknown content block');
      if (block.hasJson) {
        const input = JSON.parse(block.json);
        if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid tool input');
      }
      blocks.delete(event.index);
    }
    if (event.type === 'message_delta') {
      if (blocks.size || !STOP_REASONS.has(event.delta?.stop_reason)) throw new Error('Incomplete message');
      delta = true;
    }
    if (event.type === 'message_stop') { if (!started || !delta || blocks.size) throw new Error('Missing start or delta'); stopped = true; }

    output.push(model === clientModel || event.type !== 'message_start' ? frame + '\n\n' :
      frame.split(/\r?\n/).filter(line => !line.startsWith('data:')).concat(`data: ${JSON.stringify(event)}`).join('\n') + '\n\n');
  }
  if (!started || !stopped) throw new Error('Truncated stream');
  return { contentType: 'text/event-stream', text: output.join('') };
}

function abortable(promise, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error('Request aborted'));
    if (signal.aborted) return abort();
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

export function createProxy({ key, nonce, fetchImpl = fetch, provider = 'openrouter', localUrl,
  localModel, localKey, fallbackModel = 'claude-sonnet-5-5', retryDelayMs = 1000,
  cooldownMs = 30000, attemptTimeoutMs = 30000, now = Date.now }) {
  if (!nonce || (provider === 'openrouter' && !key)) throw new Error('Proxy credentials required');
  if (!['openrouter', 'local'].includes(provider)) throw new Error('Unsupported Sonnet provider');
  const externalUrl = provider === 'local' ? localEndpoint(localUrl) : 'https://openrouter.ai/api/v1/messages';
  if (provider === 'local' && (typeof localModel !== 'string' || !localModel.trim())) throw new Error('Local model required');
  if (!isSonnet(fallbackModel)) throw new Error('Fallback must be a native Sonnet model');
  for (const value of [retryDelayMs, cooldownMs, attemptTimeoutMs])
    if (!Number.isFinite(value) || value < 0) throw new Error('Invalid routing timeout');
  let openUntil = 0, probing = false, generation = 0;
  const server = http.createServer(async (req, res) => {
    const reject = (status, message) => {
      if (res.destroyed) return;
      if (res.headersSent) return res.destroy();
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message } }));
    };
    if (req.headers['x-local-route-token'] !== nonce) return reject(403, 'Local authorization required');
    if (req.method === 'HEAD' && req.url === '/api/hello') { res.writeHead(200); res.end(); return; }
    if (req.method !== 'POST' || !/^\/v1\/messages(?:\/count_tokens)?(?:\?beta=true)?$/.test(req.url)) return reject(403, 'Endpoint blocked');
    const client = new AbortController();
    res.on('close', () => { if (!res.writableFinished) client.abort('client_close'); });
    req.on('aborted', () => client.abort('client_close'));
    try {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 16 * 1024 * 1024) return reject(413, 'Request too large');
        chunks.push(chunk);
      }
      const raw = Buffer.concat(chunks);
      let body;
      try { body = JSON.parse(raw.toString('utf8')); } catch { return reject(400, 'Invalid JSON'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return reject(403, 'Unsupported request fields');
      const model = body.model;
      if (!isPro(model) && !isSonnet(model) && model !== DEEPSEEK) return reject(403, 'Model blocked');
      const oauthOK = /^Bearer\s+sk-ant-oat/.test(req.headers.authorization || '') && !req.headers['x-api-key'] &&
        (req.headers['anthropic-beta'] || '').includes('oauth-');
      const sendPro = async (reason = 'native_feature') => {
        if (!oauthOK) return reject(403, 'Saved subscription OAuth required');
        client.signal.throwIfAborted();
        console.error(JSON.stringify({ event: 'route_selected', provider: 'pro', reason }));
        const headers = {};
        for (const [name, value] of Object.entries(req.headers))
          if (typeof value === 'string' && !['host', 'content-length', 'accept-encoding', 'connection',
            'transfer-encoding', 'x-local-route-token', 'x-api-key'].includes(name)) headers[name] = value;
        const proBody = model === DEEPSEEK ? JSON.stringify({ ...body, model: fallbackModel }) : raw;
        const upstream = await abortable(fetchImpl('https://api.anthropic.com' + req.url,
          { method: 'POST', headers, body: proBody, redirect: 'error', signal: client.signal }), client.signal);
        const responseHeaders = {};
        for (const [name, value] of upstream.headers)
          if (!['connection', 'transfer-encoding', 'content-length', 'content-encoding'].includes(name)) responseHeaders[name] = value;
        res.writeHead(upstream.status, responseHeaders);
        if (upstream.body) {
          const reader = upstream.body.getReader();
          const cancel = () => { reader.cancel().catch(() => {}); };
          client.signal.addEventListener('abort', cancel, { once: true });
          try {
            while (true) {
              const { done, value } = await abortable(reader.read(), client.signal);
              client.signal.throwIfAborted();
              if (done) break;
              if (!res.write(value)) await once(res, 'drain', { signal: client.signal });
            }
          } finally {
            client.signal.removeEventListener('abort', cancel);
            await reader.cancel().catch(() => {});
            reader.releaseLock();
          }
        }
        res.end();
      };
      if (req.url.startsWith('/v1/messages/count_tokens') || isPro(model) || !externalCompatible(body, req.headers['anthropic-beta'])) return await sendPro();
      if (openUntil && (now() < openUntil || probing)) return await sendPro('circuit_open');
      const probe = Boolean(openUntil);
      if (probe) probing = true;
      const claimedGeneration = generation;
      let succeeded = false;
      try {
        const externalModel = provider === 'local' ? localModel : DEEPSEEK;
        const externalBody = { ...body, model: externalModel };
        if (body.tools) externalBody.tools = body.tools.map(tool => ({ ...tool, input_schema: compatibleSchema(tool.input_schema) }));
        if (provider === 'openrouter') externalBody.provider = { allow_fallbacks: false };
        const headers = { 'content-type': 'application/json', 'anthropic-version': req.headers['anthropic-version'] || '2023-06-01' };
        const credential = provider === 'local' ? localKey : key;
        if (credential) headers.authorization = `Bearer ${credential}`;
        for (let attempt = 0; attempt < 4; attempt++) {
          client.signal.throwIfAborted();
          const controller = new AbortController();
          const cancel = () => controller.abort('client_close');
          client.signal.addEventListener('abort', cancel, { once: true });
          const timer = setTimeout(() => controller.abort('attempt_timeout'), attemptTimeoutMs);
          let status;
          try {
            const operation = (async () => {
              const response = await fetchImpl(externalUrl, { method: 'POST', headers,
                body: JSON.stringify(externalBody), redirect: 'error', signal: controller.signal });
              status = response.status;
              if (!response.ok) {
                await response.body?.cancel();
                throw new Error('External request failed');
              }
              return bufferedResponse(response, externalModel, model, body.stream, controller.signal);
            })();
            const response = await abortable(operation, controller.signal);
            client.signal.throwIfAborted();
            succeeded = true;
            if (claimedGeneration === generation) {
              if (openUntil) console.error(JSON.stringify({ event: 'circuit_recovered', provider }));
              openUntil = 0;
            }
            console.error(JSON.stringify({ event: 'route_selected', provider, reason: 'replacement' }));
            res.writeHead(200, { 'content-type': response.contentType, 'cache-control': 'no-store' });
            res.end(response.text);
            return;
          } catch {
            client.signal.throwIfAborted();
            console.error(JSON.stringify({ event: 'external_attempt_failed', provider, attempt: attempt + 1,
              ...(Number.isInteger(status) ? { status } : {}),
              category: controller.signal.reason === 'attempt_timeout' ? 'timeout' : 'upstream' }));
            controller.abort('attempt_failed');
          } finally {
            clearTimeout(timer);
            client.signal.removeEventListener('abort', cancel);
          }
          if (attempt < 3 && retryDelayMs) await abortable(new Promise(resolve => {
            const timeout = setTimeout(resolve, retryDelayMs);
            client.signal.addEventListener('abort', () => { clearTimeout(timeout); resolve(); }, { once: true });
          }), client.signal);
        }
        generation++;
        openUntil = now() + cooldownMs;
        console.error(JSON.stringify({ event: 'circuit_open', provider }));
      } finally {
        if (probe) probing = false;
      }
      if (!succeeded) await sendPro('fallback');
    } catch {
      if (!client.signal.aborted) {
        console.error(JSON.stringify({ event: 'route_failed', category: 'upstream' }));
        reject(502, 'Upstream request failed');
      }
    }
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = createProxy({ key: process.env.OPENROUTER_API_KEY, nonce: process.env.LOCAL_ROUTE_TOKEN,
    provider: process.env.SONNET_PROVIDER || 'openrouter', localUrl: process.env.LOCAL_LLM_URL,
    localModel: process.env.LOCAL_LLM_MODEL, localKey: process.env.LOCAL_LLM_API_KEY });
  for (const name of ['OPENROUTER_API_KEY', 'LOCAL_ROUTE_TOKEN', 'SONNET_PROVIDER', 'LOCAL_LLM_URL',
    'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY']) delete process.env[name];
  server.listen(0, '127.0.0.1', () => process.stdout.write(JSON.stringify({ port: server.address().port }) + '\n'));
}
