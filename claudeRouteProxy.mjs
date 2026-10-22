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
const CONTROL_STOPS = new Set(['refusal', 'model_context_window_exceeded']);
function usableContent(block) {
  return block.type === 'text' ? block.text.trim().length > 0 :
    block.type === 'tool_use' && block.id.trim().length > 0 && block.name.trim().length > 0;
}
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
  tool_use: new Set(['type', 'id', 'name', 'input', 'cache_control', 'caller']),
  tool_result: new Set(['type', 'tool_use_id', 'content', 'is_error', 'cache_control']),
};
function unsupportedBetas(beta, localOnly = false) {
  return (beta || '').split(',').map(x => x.trim()).filter(x => x &&
    !/^oauth-\d{4}-\d{2}-\d{2}$/.test(x) && !['claude-code-20250219', 'prompt-caching-2024-07-31'].includes(x) &&
    !(localOnly && ['interleaved-thinking-2025-05-14', 'effort-2025-11-24'].includes(x)));
}
function externalExclusion(body, beta, localOnly = false) {
  // Unknown betas/fields and native history belong to the subscription API. Never strip them.
  if (unsupportedBetas(beta, localOnly).length) return 'unsupported_beta';
  if (Object.keys(body).some(name => !BASIC_FIELDS.has(name))) return 'unsupported_request_field';
  // Local CLI adds plain system reminders; preserve their role and text without stripping.
  if (!Array.isArray(body.messages) || body.messages.some(message => !message ||
      (!['user', 'assistant'].includes(message.role) && !(localOnly && message.role === 'system' &&
        typeof message.content === 'string' && message.content.trim())) ||
      Object.keys(message).some(name => !['role', 'content'].includes(name)))) return 'unsupported_message_shape';
  if (body.thinking && (!['enabled', 'disabled', 'adaptive'].includes(body.thinking.type) || Object.keys(body.thinking).some(name => !['type', 'budget_tokens'].includes(name)))) return 'unsupported_thinking_config';
  if (body.output_config && Object.keys(body.output_config).some(name => name !== 'effort')) return 'unsupported_output_config';
  if (body.tool_choice && Object.keys(body.tool_choice).some(name => !['type', 'name', 'disable_parallel_tool_use'].includes(name))) return 'unsupported_tool_choice';
  let contentDetail = 'unsupported_content';
  const contentOK = content => typeof content === 'string' || (Array.isArray(content) && content.every(block => {
    if (!block || !CONTENT_FIELDS[block.type]) {
      contentDetail = block?.type === 'thinking' || block?.type === 'redacted_thinking' ? 'native_thinking_history' : 'unsupported_content_block';
      return false;
    }
    if (Object.keys(block).some(key => !CONTENT_FIELDS[block.type].has(key) &&
        !(localOnly && block.type === 'tool_use' && key === 'provider_specific_fields')) ||
        (localOnly && Object.hasOwn(block, 'provider_specific_fields') && block.provider_specific_fields !== null &&
          (typeof block.provider_specific_fields !== 'object' || Array.isArray(block.provider_specific_fields)))) {
      contentDetail = 'unsupported_content_field';
      return false;
    }
    if (block.type === 'tool_use' && Object.hasOwn(block, 'caller') &&
        (!block.caller || typeof block.caller !== 'object' || Array.isArray(block.caller) ||
          block.caller.type !== 'direct' || Object.keys(block.caller).length !== 1)) {
      contentDetail = 'unsupported_tool_caller';
      return false;
    }
    return block.type !== 'tool_result' || contentOK(block.content);
  }));
  if (!body.messages.every(message => contentOK(message.content)) ||
      (body.system !== undefined && !contentOK(body.system))) return contentDetail;
  if (body.tools !== undefined && (!Array.isArray(body.tools) || body.tools.some(tool =>
    !tool || typeof tool.name !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(tool.name) ||
    !tool.input_schema || typeof tool.input_schema !== 'object' || Array.isArray(tool.input_schema) ||
    Object.keys(tool).some(name => !['name', 'description', 'input_schema', 'cache_control', 'type'].includes(name)) ||
    (tool.type !== undefined && tool.type !== 'custom')))) return 'unsupported_tool_definition';
  return null;
}

function localEndpoint(value, allowRemote, httpUrl) {
  if (typeof value !== 'string' || /[\x00-\x20\x7f]/.test(value)) throw new Error('Invalid Local Messages URL');
  let url;
  try { url = new URL(value); } catch { throw new Error('Local Messages URL required'); }
  const loopback = ['127.0.0.1', '[::1]'].includes(url.hostname);
  const internalHttp = url.protocol === 'http:' && value === httpUrl;
  if (!['http:', 'https:'].includes(url.protocol) || url.port === '0' || (!loopback && !internalHttp && (!allowRemote || url.protocol !== 'https:')) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/v1/messages')
    throw new Error('Local Messages URL requires loopback, approved internal HTTP, or explicitly allowed remote HTTPS');
  return url.href;
}

async function bufferedResponse(upstream, model, clientModel, stream, signal) {
  if (!upstream.ok) throw new Error('External request failed');
  if (!upstream.body) throw new Error('Empty message response');
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
  if (!text.trim()) throw new Error('Empty message response');
  if (!sse) {
    const response = JSON.parse(text);
    if (response.type !== 'message' || response.model !== model || !Array.isArray(response.content) ||
        !response.content.every(validContentBlock) || !STOP_REASONS.has(response.stop_reason)) throw new Error('Invalid message response');
    if (!response.content.some(usableContent) && !CONTROL_STOPS.has(response.stop_reason)) throw new Error('Empty message response');
    response.model = clientModel;
    return { contentType: 'application/json', text: JSON.stringify(response) };
  }
  let started = false, delta = false, stopped = false;
  let usable = false, stopReason;
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
      usable ||= usableContent(event.content_block);
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
      if (event.delta.type === 'text_delta') usable ||= event.delta.text.trim().length > 0;
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
      stopReason = event.delta.stop_reason;
    }
    if (event.type === 'message_stop') { if (!started || !delta || blocks.size) throw new Error('Missing start or delta'); stopped = true; }

    output.push(model === clientModel || event.type !== 'message_start' ? frame + '\n\n' :
      frame.split(/\r?\n/).filter(line => !line.startsWith('data:')).concat(`data: ${JSON.stringify(event)}`).join('\n') + '\n\n');
  }
  if (!started || !stopped) throw new Error('Truncated stream');
  if (!usable && !CONTROL_STOPS.has(stopReason)) throw new Error('Empty message response');
  return { contentType: 'text/event-stream', text: output.join('') };
}

function jsonMessageStream(text) {
  const message = JSON.parse(text);
  if (typeof message.id !== 'string' || !message.id || message.role !== 'assistant' ||
      !Number.isFinite(message.usage?.input_tokens) || message.usage.input_tokens < 0 ||
      !Number.isFinite(message.usage?.output_tokens) || message.usage.output_tokens < 0)
    throw new Error('Invalid stream metadata');
  const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
  let output = frame('message_start', { message: { ...message, content: [], stop_reason: null, stop_sequence: null,
    usage: { ...message.usage, output_tokens: 0 } } });
  message.content.forEach((block, index) => {
    const initial = block.type === 'text' ? { ...block, text: '' } : { ...block, input: {} };
    output += frame('content_block_start', { index, content_block: initial });
    if (block.type === 'text') output += frame('content_block_delta', { index, delta: { type: 'text_delta', text: block.text } });
    if (block.type === 'tool_use') output += frame('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } });
    output += frame('content_block_stop', { index });
  });
  output += frame('message_delta', { delta: { stop_reason: message.stop_reason, stop_sequence: message.stop_sequence ?? null }, usage: message.usage });
  output += frame('message_stop');
  return { contentType: 'text/event-stream', text: output };
}

function abortable(promise, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error('Request aborted'));
    if (signal.aborted) return abort();
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

export async function diagnoseLocal({ localUrl, localModel, localKey, localHttpUrl,
  localAllowRemote = false, fetchImpl = fetch, timeoutMs = 60000 }) {
  const url = localEndpoint(localUrl, localAllowRemote, localHttpUrl);
  if (typeof localAllowRemote !== 'boolean' || typeof localModel !== 'string' || !localModel.trim() ||
      !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60000)
    throw new Error('Invalid Local diagnostic configuration');
  if (localKey !== undefined && (typeof localKey !== 'string' || /[\x00-\x1f\x7f]/.test(localKey)))
    throw new Error('Invalid Local credential');
  const headers = { 'content-type': 'application/json', 'anthropic-version': '2023-06-01' };
  if (localKey) headers.authorization = `Bearer ${localKey}`;
  const controller = new AbortController();
  const { signal } = controller;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let received = false;
  try {
    const response = await abortable(fetchImpl(url, { method: 'POST', headers, redirect: 'error', signal,
      body: JSON.stringify({ model: localModel, max_tokens: 512, stream: false,
        messages: [{ role: 'user', content: 'Reply with exactly LOCAL_DIAGNOSTIC_OK. Do not use tools.' }] }) }), signal);
    received = true;
    if (!response.ok) {
      await response.body?.cancel();
      return { local_diagnostic: false, inference_checked: true, reason: 'http_error', status: response.status };
    }
    const validated = await abortable(bufferedResponse(response, localModel, localModel, false, signal), signal);
    if (validated.contentType !== 'application/json') throw new Error('Expected JSON diagnostic response');
    const message = JSON.parse(validated.text);
    const reply = message.content.filter(block => block.type === 'text').map(block => block.text).join('').trim();
    const passed = reply === 'LOCAL_DIAGNOSTIC_OK' && message.stop_reason === 'end_turn';
    return { local_diagnostic: passed, inference_checked: true, model_confirmed: true, reply_confirmed: passed,
      reason: message.stop_reason !== 'end_turn' ? 'incomplete_response' : passed ? 'ready_inference_only' : 'reply_mismatch' };
  } catch {
    return { local_diagnostic: false, inference_checked: true,
      reason: signal.aborted ? 'timeout' : received ? 'invalid_response' : 'connection_failed' };
  } finally {
    clearTimeout(timer);
  }
}

export function createProxy({ key, nonce, fetchImpl = fetch, provider = 'openrouter', localUrl,
  localModel, localKey, localAllowRemote = false, localHttpUrl, localOnly = false, fallbackModel = 'claude-sonnet-5-5', retryDelayMs = 1000,
  cooldownMs = 30000, attemptTimeoutMs = localOnly ? 120000 : 30000, now = Date.now }) {
  if (typeof localOnly !== 'boolean' || (localOnly && provider !== 'local')) throw new Error('Local-only mode requires local provider');
  if (!nonce || (provider === 'openrouter' && !key)) throw new Error('Proxy credentials required');
  if (!['openrouter', 'local'].includes(provider)) throw new Error('Unsupported Sonnet provider');
  if (typeof localAllowRemote !== 'boolean') throw new Error('Remote Local setting must be boolean');
  if (localKey !== undefined && (typeof localKey !== 'string' || /[\x00-\x1f\x7f]/.test(localKey))) throw new Error('Invalid Local credential');
  const externalUrl = provider === 'local' ? localEndpoint(localUrl, localAllowRemote, localHttpUrl) : 'https://openrouter.ai/api/v1/messages';
  if (provider === 'local' && (typeof localModel !== 'string' || !localModel.trim())) throw new Error('Local model required');
  if (!isSonnet(fallbackModel)) throw new Error('Fallback must be a native Sonnet model');
  for (const value of [retryDelayMs, cooldownMs, attemptTimeoutMs])
    if (!Number.isFinite(value) || value < 0) throw new Error('Invalid routing timeout');
  let openUntil = 0, probing = false, generation = 0, lastLocalFailure = 'upstream_error';
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
      const sendPro = async (reason = 'native_feature', detail) => {
        const betas = detail === 'unsupported_beta' ? unsupportedBetas(req.headers['anthropic-beta'], localOnly) : [];
        const betaFeatures = betas.slice(0, 16).map(value =>
          /^[a-z][a-z0-9-]{0,63}-20\d{2}-[01]\d-[0-3]\d$/.test(value) &&
          !/(?:^|-)(?:sk|key|token|secret|password|bearer)(?:-|$)/.test(value) ? value : '[redacted]');
        if (localOnly) {
          let message = 'Local-only mode cannot route this request; Plan fallback disabled';
          let code, retryAfterSeconds, previousErrorCode;
          if (reason === 'fallback' || reason === 'circuit_open') {
            const remaining = Math.max(0, Math.ceil((openUntil - now()) / 1000));
            const recoveryInProgress = reason === 'circuit_open' && probing && remaining === 0;
            const descriptions = {
              timeout: ['local_timeout', 'Local request timed out.'],
              authentication_failed: ['local_authentication_failed', 'Local server authentication failed. Check the Local API key and access permissions; restart the Local session after changing credentials.'],
              empty_response: ['local_empty_response', 'Local server returned an empty response.'],
              upstream_error: ['local_upstream_error', 'Local server request failed. Check the local routing log.'],
            };
            let description;
            [code, description] = reason === 'fallback' ? descriptions[detail] || descriptions.upstream_error :
              recoveryInProgress ? ['local_recovery_in_progress', 'Local recovery check is in progress. Wait for it to finish before sending a new request.'] :
                ['local_cooldown', `Local retry cooldown is active. Previous failure: ${(descriptions[lastLocalFailure] || descriptions.upstream_error)[1]}`];
            if (reason === 'circuit_open') previousErrorCode = (descriptions[lastLocalFailure] || descriptions.upstream_error)[0];
            if (!recoveryInProgress) retryAfterSeconds = remaining;
            const retryHint = recoveryInProgress ? '' : remaining > 0 ?
              ` Wait ${remaining} second${remaining === 1 ? '' : 's'} before a new request.` :
              ' Send a new request to check Local recovery.';
            message = `[${code}] ${description}${retryHint} Plan fallback disabled.`;
          }
          console.error(JSON.stringify({ event: 'local_mode_rejected', provider: 'local', reason, ...(detail ? { detail } : {}),
            ...(code ? { code } : {}), ...(retryAfterSeconds !== undefined ? { retry_after_seconds: retryAfterSeconds } : {}),
            ...(previousErrorCode ? { previous_error_code: previousErrorCode } : {}),
            ...(betas.length ? { beta_features: betaFeatures, unsupported_beta_count: betas.length } : {}) }));
          return reject(400, message);
        }
        if (!oauthOK) return reject(403, 'Saved subscription OAuth required');
        client.signal.throwIfAborted();
        console.error(JSON.stringify({ event: 'route_selected', provider: 'plan', reason, ...(detail ? { detail } : {}),
          ...(betas.length ? { beta_features: betaFeatures, unsupported_beta_count: betas.length } : {}) }));
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
      const nativeDetail = req.url.startsWith('/v1/messages/count_tokens') ? 'token_count' :
        isPro(model) ? 'plan_model' : externalExclusion(body, req.headers['anthropic-beta'], localOnly);
      if (nativeDetail) return await sendPro('native_feature', nativeDetail);
      if (openUntil && (now() < openUntil || probing)) return await sendPro('circuit_open');
      const probe = Boolean(openUntil);
      if (probe) probing = true;
      const claimedGeneration = generation;
      let succeeded = false, localFailure = 'upstream_error';
      try {
        const externalModel = provider === 'local' ? localModel : DEEPSEEK;
        const externalBody = { ...body, model: externalModel };
        if (localOnly) {
          externalBody.stream = false;
          externalBody.thinking = { type: 'disabled' };
        }
        if (body.tools) externalBody.tools = body.tools.map(tool => ({ ...tool, input_schema: compatibleSchema(tool.input_schema) }));
        if (provider === 'openrouter') externalBody.provider = { allow_fallbacks: false };
        const headers = { 'content-type': 'application/json', 'anthropic-version': req.headers['anthropic-version'] || '2023-06-01' };
        const credential = provider === 'local' ? localKey : key;
        if (credential) headers.authorization = `Bearer ${credential}`;
        const attempts = localOnly ? 1 : 4;
        for (let attempt = 0; attempt < attempts; attempt++) {
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
              const validated = await bufferedResponse(response, externalModel, model, localOnly ? false : body.stream, controller.signal);
              if (localOnly) {
                if (validated.contentType !== 'application/json') throw new Error('Expected JSON Local response');
                const content = JSON.parse(validated.text).content;
                if (content.some(block => ['thinking', 'redacted_thinking'].includes(block.type)))
                  throw new Error('Local thinking response disabled');
                if (content.some(block => !['text', 'tool_use'].includes(block.type)))
                  throw new Error('Unsupported Local response content');
                const extraFields = content.flatMap(block => Object.keys(block).filter(name => !CONTENT_FIELDS[block.type].has(name) &&
                  !(block.type === 'tool_use' && name === 'provider_specific_fields')));
                if (content.some(block => Object.hasOwn(block, 'provider_specific_fields') && block.provider_specific_fields !== null &&
                    (typeof block.provider_specific_fields !== 'object' || Array.isArray(block.provider_specific_fields))))
                  throw new Error('Unsupported Local response fields');
                if (extraFields.length) {
                  console.error(JSON.stringify({ event: 'local_response_unsupported_fields', fields: extraFields.slice(0, 8).map(name =>
                    ['signature', 'thought_signature', 'extra_content', 'provider_specific_fields', 'index'].includes(name) ? name : '[redacted]') }));
                  throw new Error('Unsupported Local response fields');
                }
              }
              return localOnly && body.stream === true ? jsonMessageStream(validated.text) : validated;
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
          } catch (error) {
            client.signal.throwIfAborted();
            const validation = { 'Wrong response model': 'wrong_model', 'Invalid content block': 'invalid_content_block',
              'Incomplete message': 'incomplete_message', 'Invalid response': 'expected_sse',
              'Invalid content delta': 'invalid_delta', 'Truncated stream': 'truncated_stream',
              'Empty message response': 'empty_response', 'Invalid message response': 'invalid_message',
              'Invalid stream metadata': 'invalid_stream_metadata', 'Expected JSON Local response': 'expected_json', 'Unsupported Local response content': 'unsupported_local_content',
              'Local thinking response disabled': 'thinking_response', 'Unsupported Local response fields': 'unsupported_content_fields' }[error?.message];
            if (localOnly) localFailure = controller.signal.reason === 'attempt_timeout' ? 'timeout' :
              status === 401 || status === 403 ? 'authentication_failed' : validation === 'empty_response' ? 'empty_response' : 'upstream_error';
            console.error(JSON.stringify({ event: 'external_attempt_failed', provider, attempt: attempt + 1,
              ...(Number.isInteger(status) ? { status } : {}), ...(validation ? { validation } : {}),
              category: controller.signal.reason === 'attempt_timeout' ? 'timeout' : 'upstream' }));
            controller.abort('attempt_failed');
          } finally {
            clearTimeout(timer);
            client.signal.removeEventListener('abort', cancel);
          }
          if (attempt + 1 < attempts && retryDelayMs) await abortable(new Promise(resolve => {
            const timeout = setTimeout(resolve, retryDelayMs);
            client.signal.addEventListener('abort', () => { clearTimeout(timeout); resolve(); }, { once: true });
          }), client.signal);
        }
        generation++;
        if (localOnly) lastLocalFailure = localFailure;
        openUntil = now() + cooldownMs;
        console.error(JSON.stringify({ event: 'circuit_open', provider }));
      } finally {
        if (probe) probing = false;
      }
      if (!succeeded) await sendPro('fallback', localOnly ? localFailure : undefined);
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
  const config = { key: process.env.OPENROUTER_API_KEY, nonce: process.env.LOCAL_ROUTE_TOKEN,
    provider: process.env.SONNET_PROVIDER || 'openrouter', localUrl: process.env.LOCAL_LLM_URL,
    localModel: process.env.LOCAL_LLM_MODEL, localKey: process.env.LOCAL_LLM_API_KEY,
    localOnly: process.env.LOCAL_ROUTE_ONLY === '1', localHttpUrl: process.env.LOCAL_LLM_HTTP_URL, localAllowRemote: process.env.LOCAL_LLM_ALLOW_REMOTE === 'true' };
  for (const name of ['OPENROUTER_API_KEY', 'LOCAL_ROUTE_TOKEN', 'LOCAL_ROUTE_ONLY', 'SONNET_PROVIDER', 'LOCAL_LLM_URL',
    'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY', 'LOCAL_LLM_ALLOW_REMOTE', 'LOCAL_LLM_HTTP_URL', 'LOCAL_LLM_MODELS_URL']) delete process.env[name];
  if (process.argv.slice(2).includes('--diagnose-local')) {
    try {
      const result = await diagnoseLocal(config);
      process.stdout.write(JSON.stringify(result) + '\n');
      process.exitCode = result.local_diagnostic ? 0 : 1;
    } catch {
      process.stdout.write(JSON.stringify({ local_diagnostic: false, inference_checked: false, reason: 'invalid_configuration' }) + '\n');
      process.exitCode = 1;
    }
  } else {
    const server = createProxy(config);
    if (config.localOnly && process.argv.slice(2).includes('--parent-pipe')) {
      const parentClosed = () => {
        console.error(JSON.stringify({ event: 'local_parent_disconnected', provider: 'local' }));
        process.exit(0);
      };
      process.stdin.once('end', parentClosed);
      process.stdin.once('error', parentClosed);
      process.stdin.once('close', parentClosed);
      process.stdin.resume();
    }
    server.listen(0, '127.0.0.1', () => process.stdout.write(JSON.stringify({ port: server.address().port }) + '\n'));
  }
}
