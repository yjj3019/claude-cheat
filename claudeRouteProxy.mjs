import http from 'node:http';
import { once } from 'node:events';
import { createHash, timingSafeEqual } from 'node:crypto';
import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// Diagnostic mirror: routing events also land in a file so a session can be explained afterwards.
// The launcher sets ROUTE_LOG_PATH for OpenRouter sessions; without it nothing is written.
const routeLogPath = process.env.ROUTE_LOG_PATH;
if (routeLogPath) {
  delete process.env.ROUTE_LOG_PATH;
  const emit = console.error.bind(console);
  console.error = (...args) => {
    emit(...args);
    try { appendFileSync(routeLogPath, `${new Date().toISOString()} ${args.join(' ')}\n`); } catch {}
  };
  try { appendFileSync(routeLogPath, `${new Date().toISOString()} --- proxy start ---\n`); } catch {}
}

// Historical client alias only; the upstream model must be configured explicitly.
export const DEEPSEEK = 'deepseek/deepseek-v4.1-flash-1017';
const isPro = model => typeof model === 'string' && /^claude-(opus|haiku|fable)-[0-9][a-zA-Z0-9.-]*$/.test(model);
const isSonnet = model => typeof model === 'string' && /^claude-sonnet-[0-9][a-zA-Z0-9.-]*$/.test(model);
const validOpenRouterModel = model => typeof model === 'string' && /^[A-Za-z0-9~@][A-Za-z0-9._:/@+~-]{0,255}$/.test(model) && !/^(REPLACE_WITH_|YOUR_)/.test(model);
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
// Read-only fields a provider may add to a response block; request blocks never carry them.
const RESPONSE_FIELDS = { text: new Set(['citations']) };
const responseBlockField = (type, name) => Boolean(CONTENT_FIELDS[type]?.has(name) || RESPONSE_FIELDS[type]?.has(name));
// Native reasoning stays on the subscription route: another provider cannot verify the Anthropic
// signature, and forwarding it would ship the Plan model's internal reasoning to a third party.
// Dropping it from the external copy also keeps a session routable after one Plan fallback.
const NATIVE_THINKING = new Set(['thinking', 'redacted_thinking']);
const OMITTED_REASONING = '(reasoning omitted)';
const isNativeThinking = block => NATIVE_THINKING.has(block?.type);
const nativeThinkingOK = block => block.type === 'thinking' ? typeof block.thinking === 'string' :
  typeof block.data === 'string';
const withoutNativeThinking = messages => messages.map(message => {
  if (!Array.isArray(message.content)) return message;
  const content = message.content.filter(block => !isNativeThinking(block));
  if (content.length === message.content.length) return message;
  // A turn that held only reasoning keeps its place so the conversation keeps valid turn order.
  return { ...message, content: content.length ? content : [{ type: 'text', text: OMITTED_REASONING }] };
});
const COMPAT_BETAS = new Set(['interleaved-thinking-2025-05-14', 'redact-thinking-2026-02-12',
  'context-management-2025-06-27', 'prompt-caching-scope-2026-01-05', 'per-turn-control-2026-07-01',
  'mid-conversation-tool-changes-2026-07-01', 'inline-tools-2026-09-15', 'advisor-tool-2026-03-01',
  'advanced-tool-use-2025-11-20', 'effort-2025-11-24', 'structured-outputs-2025-12-15',
  'dangerous-tool-use-2026-09-03', 'thinking-display-updates-2026-08-18', 'afk-mode-2026-01-31',
  'extended-cache-ttl-2025-04-11', 'fallback-credit-2026-06-01', 'thinking-token-count-2026-05-13']);
function compatExclusion(body, beta) {
  // Check extensions separately; the original request, not this validation copy, is forwarded.
  if (!Array.isArray(body.messages)) return 'unsupported_message_shape';
  if (unsupportedBetas(beta, true).length) return 'unsupported_beta';
  if (body.messages.some(message => !message || typeof message !== 'object')) return 'unsupported_message_shape';
  const copy = { ...body, messages: body.messages.map(message => {
    const result = { ...message, content: Array.isArray(message.content) ? message.content.filter(block =>
      !['thinking', 'redacted_thinking'].includes(block?.type)) : message.content };
    if (message.role === 'system') result.role = 'user';
    if (message.output_config !== undefined) delete result.output_config;
    return result;
  }) };
  for (const output of [body.output_config, ...body.messages.map(message => message.output_config)].filter(value => value !== undefined)) {
    if (!output || typeof output !== 'object' || Array.isArray(output) ||
        Object.keys(output).some(name => !['effort', 'format'].includes(name)) ||
        (output.effort !== undefined && !['low', 'medium', 'high', 'max', 'xhigh'].includes(output.effort)) ||
        (output.format !== undefined && (!output.format || output.format.type !== 'json_schema' ||
          Object.keys(output.format).some(name => !['type', 'schema'].includes(name)) ||
          !output.format.schema || typeof output.format.schema !== 'object' || Array.isArray(output.format.schema)))) return 'unsupported_output_config';
  }
  if (copy.output_config) copy.output_config = {};
  if (Array.isArray(body.tools)) {
    for (const tool of body.tools) for (const name of ['defer_loading', 'strict', 'eager_input_streaming'])
      if (tool?.[name] !== undefined && typeof tool[name] !== 'boolean') return 'unsupported_tool_definition';
    copy.tools = body.tools.map(tool => {
      if (!tool || typeof tool !== 'object') return tool;
      const result = { ...tool };
      for (const name of ['defer_loading', 'strict', 'eager_input_streaming']) delete result[name];
      return result;
    });
  }
  if (body.context_management !== undefined) {
    if (!body.context_management || typeof body.context_management !== 'object' || Array.isArray(body.context_management))
      return 'unsupported_context_management';
    delete copy.context_management;
  }
  if (body.thinking?.display !== undefined) {
    if (!['updates', 'omitted', 'summarized'].includes(body.thinking.display)) return 'unsupported_thinking_config';
    copy.thinking = { ...body.thinking }; delete copy.thinking.display;
  }
  return externalExclusion(copy, '', true, true);
}
// Claude Code 2.1.310 sends these on every request, even with experimental betas disabled. They only
// steer reasoning hints this proxy rewrites per request, so a name alone must not divert traffic to Plan.
const BASELINE_BETAS = new Set(['interleaved-thinking-2025-05-14', 'effort-2025-11-24']);
function unsupportedBetas(beta, localCompat = false) {
  return (beta || '').split(',').map(x => x.trim()).filter(x => x &&
    !BASELINE_BETAS.has(x) && !(localCompat && COMPAT_BETAS.has(x)) &&
    !/^oauth-\d{4}-\d{2}-\d{2}$/.test(x) && !['claude-code-20250219', 'prompt-caching-2024-07-31'].includes(x));
}
function externalExclusion(body, beta, localOnly = false, localCompat = false) {
  // Unknown betas/fields belong to the subscription API and are never stripped; native reasoning is
  // the one exception, dropped from the external copy because no other provider can verify it.
  if (unsupportedBetas(beta).length) return 'unsupported_beta';
  if (Object.keys(body).some(name => !BASIC_FIELDS.has(name))) return 'unsupported_request_field';
  // Claude Code sends a context turn with role "system" in messages; it carries text only.
  const systemContentOK = content => typeof content === 'string' ? content.trim().length > 0 :
    Array.isArray(content) && content.length > 0 && content.every(block => block?.type === 'text');
  if (!Array.isArray(body.messages) || body.messages.some(message => !message ||
      !['user', 'assistant', 'system'].includes(message.role) ||
      (message.role === 'system' && !systemContentOK(message.content)) ||
      Object.keys(message).some(name => !['role', 'content'].includes(name)))) return 'unsupported_message_shape';
  if (body.thinking && (!['enabled', 'disabled', 'adaptive'].includes(body.thinking.type) || Object.keys(body.thinking).some(name => !['type', 'budget_tokens'].includes(name)))) return 'unsupported_thinking_config';
  if (body.output_config && Object.keys(body.output_config).some(name => name !== 'effort')) return 'unsupported_output_config';
  if (body.tool_choice && Object.keys(body.tool_choice).some(name => !['type', 'name', 'disable_parallel_tool_use'].includes(name))) return 'unsupported_tool_choice';
  let contentDetail = 'unsupported_content';
  const contentOK = (content, toolResult = false) => typeof content === 'string' || (Array.isArray(content) && content.every(block => {
    const fields = localCompat && toolResult && block?.type === 'tool_reference' ? new Set(['type', 'tool_name', 'cache_control']) : CONTENT_FIELDS[block?.type];
    if (isNativeThinking(block)) return nativeThinkingOK(block);
    if (!block || !fields) {
      contentDetail = 'unsupported_content_block';
      return false;
    }
    if ((block.type === 'tool_reference' && (typeof block.tool_name !== 'string' ||
        !Array.isArray(body.tools) || !body.tools.some(tool => tool?.name === block.tool_name))) || Object.keys(block).some(key => !fields.has(key) &&
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
    return block.type !== 'tool_result' || contentOK(block.content, true);
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

// Verified live against OpenRouter: every message carries id, role=assistant and token usage.
const validMessageMeta = message => typeof message?.id === 'string' && message.id && message.role === 'assistant' &&
  Number.isFinite(message.usage?.input_tokens) && message.usage.input_tokens >= 0 &&
  Number.isFinite(message.usage?.output_tokens) && message.usage.output_tokens >= 0;

async function bufferedResponse(upstream, model, clientModel, stream, signal, onProgress, strictMeta = false) {
  if (!upstream.ok) throw new Error('External request failed');
  if (!upstream.body) throw new Error('Empty message response');
  const sse = (upstream.headers.get('content-type') || '').includes('text/event-stream');
  if (stream === true && !sse) throw new Error('Invalid response');
  if (stream !== true && sse) throw new Error('Unexpected SSE response');
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
      onProgress?.();
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
        !response.content.every(validContentBlock) || !STOP_REASONS.has(response.stop_reason) ||
        (strictMeta && !validMessageMeta(response))) throw new Error('Invalid message response');
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
    const named = frame.split(/\r?\n/).find(line => line.startsWith('event:'))?.slice(6).trim();
    if (named !== undefined && named !== event.type) throw new Error('Unexpected stream event');
    if (!['message_start', 'content_block_start', 'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop', 'ping'].includes(event.type)) throw new Error('Unexpected stream event');
    if (stopped) { if (event.type === 'ping') continue; throw new Error('Event after message stop'); }
    if (event.type === 'message_start') {
      if (started || event.message?.model !== model) throw new Error('Wrong response model');
      if (strictMeta && !validMessageMeta(event.message)) throw new Error('Invalid stream metadata');
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
      if (block.hasJson && block.json.trim()) {
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
  if (!validMessageMeta(message)) throw new Error('Invalid stream metadata');
  const frame = (type, fields = {}) => `event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
  let output = frame('message_start', { message: { ...message, content: [], stop_reason: null, stop_sequence: null,
    usage: { ...message.usage, output_tokens: 0 } } });
  message.content.forEach((block, index) => {
    const initial = block.type === 'text' ? { ...block, text: '' } : block.type === 'thinking' ?
      { ...block, thinking: '', ...(block.signature !== undefined ? { signature: '' } : {}) } : block.type === 'tool_use' ? { ...block, input: {} } : block;
    output += frame('content_block_start', { index, content_block: initial });
    if (block.type === 'text') output += frame('content_block_delta', { index, delta: { type: 'text_delta', text: block.text } });
    if (block.type === 'tool_use') output += frame('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } });
    if (block.type === 'thinking') {
      output += frame('content_block_delta', { index, delta: { type: 'thinking_delta', thinking: block.thinking } });
      if (block.signature) output += frame('content_block_delta', { index, delta: { type: 'signature_delta', signature: block.signature } });
    }
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
  return diagnoseEndpoint({ url, model: localModel, key: localKey, fetchImpl, timeoutMs });
}

async function diagnoseEndpoint({ url, model, key, fetchImpl, timeoutMs, openrouter = false }) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60000) throw new Error('Invalid diagnostic timeout');
  const headers = { 'content-type': 'application/json', 'anthropic-version': '2023-06-01' };
  if (key) headers.authorization = `Bearer ${key}`;
  const controller = new AbortController();
  const { signal } = controller;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let received = false;
  try {
    const response = await abortable(fetchImpl(url, { method: 'POST', headers, redirect: 'error', signal,
      body: JSON.stringify({ model, max_tokens: 512, stream: false, ...(openrouter ? { provider: { allow_fallbacks: false } } : {}),
        messages: [{ role: 'user', content: 'Reply with exactly LOCAL_DIAGNOSTIC_OK. Do not use tools.' }] }) }), signal);
    received = true;
    if (!response.ok) {
      await response.body?.cancel();
      return { local_diagnostic: false, inference_checked: true, reason: 'http_error', status: response.status };
    }
    const validated = await abortable(bufferedResponse(response, model, model, false, signal), signal);
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

export async function checkConnection({ provider, key, openrouterModel, fetchImpl = fetch, timeoutMs = 15000, ...config }) {
  if (!['local', 'openrouter'].includes(provider)) throw new Error('Unsupported connection provider');
  if (provider === 'openrouter' && (!validOpenRouterModel(openrouterModel) || typeof key !== 'string' || !/^sk-or-[A-Za-z0-9_-]+$/.test(key)))
    throw new Error('Invalid OpenRouter connection configuration');
  const result = await (provider === 'local' ? diagnoseLocal({ ...config, timeoutMs, fetchImpl }) :
    diagnoseEndpoint({ url: 'https://openrouter.ai/api/v1/messages', model: openrouterModel, key, fetchImpl, timeoutMs, openrouter: true }));
  return { connection_check: result.local_diagnostic, provider, inference_checked: result.inference_checked,
    model_confirmed: result.model_confirmed === true, reply_confirmed: result.reply_confirmed === true,
    reason: result.local_diagnostic ? 'ready' : [401, 403].includes(result.status) ? 'authentication_failed' : result.reason,
    ...(Number.isInteger(result.status) ? { status: result.status } : {}) };
}

export function createProxy({ key, nonce, fetchImpl = fetch, provider = 'openrouter', openrouterModel, localUrl,
  localModel, localKey, localAllowRemote = false, localHttpUrl, localOnly = false, localCompat = false, openrouterCompat = false, fallbackModel = 'claude-sonnet-5-5', retryDelayMs = 1000,
  cooldownMs = 30000, attemptTimeoutMs = localOnly || openrouterCompat ? 120000 : 30000, planHeaderMs = 120000, planIdleMs = 300000, attemptCeilingMs = 300000, now = Date.now }) {
  if (typeof localOnly !== 'boolean' || (localOnly && provider !== 'local')) throw new Error('Local-only mode requires local provider');
  if (typeof localCompat !== 'boolean' || (localCompat && (!localOnly || provider !== 'local')))
    throw new Error('Local compatibility requires Local-only mode');
  if (typeof openrouterCompat !== 'boolean' || (openrouterCompat && (provider !== 'openrouter' || localOnly || localCompat)))
    throw new Error('OpenRouter compatibility requires the OpenRouter provider');
  const noPlan = localOnly || openrouterCompat, compatProfile = localCompat || openrouterCompat;
  const label = openrouterCompat ? 'OpenRouter' : 'Local', errorPrefix = openrouterCompat ? 'openrouter' : 'local';
  if (!nonce || (provider === 'openrouter' && !key)) throw new Error('Proxy credentials required');
  const nonceDigest = createHash('sha256').update(nonce).digest();
  const tokenMatches = value => typeof value === 'string' && timingSafeEqual(createHash('sha256').update(value).digest(), nonceDigest);
  if (!['openrouter', 'local'].includes(provider)) throw new Error('Unsupported Sonnet provider');
  if (provider === 'openrouter' && !validOpenRouterModel(openrouterModel))
    throw new Error('OpenRouter model required; no default model is selected');
  if (typeof localAllowRemote !== 'boolean') throw new Error('Remote Local setting must be boolean');
  if (localKey !== undefined && (typeof localKey !== 'string' || /[\x00-\x1f\x7f]/.test(localKey))) throw new Error('Invalid Local credential');
  const externalUrl = provider === 'local' ? localEndpoint(localUrl, localAllowRemote, localHttpUrl) : 'https://openrouter.ai/api/v1/messages';
  if (provider === 'local' && (typeof localModel !== 'string' || !localModel.trim())) throw new Error('Local model required');
  if (!isSonnet(fallbackModel)) throw new Error('Fallback must be a native Sonnet model');
  for (const value of [retryDelayMs, cooldownMs, attemptTimeoutMs, planHeaderMs, planIdleMs, attemptCeilingMs])
    if (!Number.isFinite(value) || value < 0) throw new Error('Invalid routing timeout');
  let openUntil = 0, probing = false, generation = 0, lastLocalFailure = 'upstream_error';
  const server = http.createServer(async (req, res) => {
    const reject = (status, message) => {
      if (res.destroyed) return;
      if (res.headersSent) return res.destroy();
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message } }));
    };
    if (!tokenMatches(req.headers['x-local-route-token'])) return reject(403, 'Local authorization required');
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
        if (size > MAX_RESPONSE) return reject(413, 'Request too large');
        chunks.push(chunk);
      }
      const raw = Buffer.concat(chunks);
      let body;
      try { body = JSON.parse(raw.toString('utf8')); } catch { return reject(400, 'Invalid JSON'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return reject(403, 'Unsupported request fields');
      const model = body.model;
      const compatAlias = compatProfile && ['sonnet', 'opus', 'haiku', 'fable'].includes(model);
      const configuredAlias = provider === 'openrouter' && model === openrouterModel;
      if (!compatAlias && !configuredAlias && !isPro(model) && !isSonnet(model) && model !== DEEPSEEK) return reject(403, 'Model blocked');
      const oauthOK = /^Bearer\s+sk-ant-oat/.test(req.headers.authorization || '') && !req.headers['x-api-key'] &&
        (req.headers['anthropic-beta'] || '').includes('oauth-');
      const sendPro = async (reason = 'native_feature', detail) => {
        const betas = detail === 'unsupported_beta' ? unsupportedBetas(req.headers['anthropic-beta'], compatProfile) : [];
        const betaFeatures = betas.slice(0, 16).map(value =>
          /^[a-z][a-z0-9-]{0,63}-20\d{2}-[01]\d-[0-3]\d$/.test(value) &&
          !/(?:^|-)(?:sk|key|token|secret|password|bearer)(?:-|$)/.test(value) ? value : '[redacted]');
        if (noPlan) {
          let message = `${label}-only mode cannot route this request; Plan fallback disabled`;
          let code, retryAfterSeconds, previousErrorCode;
          if (reason === 'fallback' || reason === 'circuit_open') {
            const remaining = Math.max(0, Math.ceil((openUntil - now()) / 1000));
            const recoveryInProgress = reason === 'circuit_open' && probing && remaining === 0;
            const descriptions = {
              timeout: [`${errorPrefix}_timeout`, `${label} request timed out.`],
              authentication_failed: [`${errorPrefix}_authentication_failed`, `${label} server authentication failed. Check the ${label} API key and access permissions; restart the ${label} session after changing credentials.`],
              payment_required: [`${errorPrefix}_payment_required`, `${label} server rejected the request for billing or credit reasons. Check the ${label} account balance and limits; restart the ${label} session after changing them.`],
              empty_response: [`${errorPrefix}_empty_response`, `${label} server returned an empty response.`],
              upstream_error: [`${errorPrefix}_upstream_error`, `${label} server request failed. Check the ${errorPrefix} routing log.`],
            };
            let description;
            [code, description] = reason === 'fallback' ? descriptions[detail] || descriptions.upstream_error :
              recoveryInProgress ? [`${errorPrefix}_recovery_in_progress`, `${label} recovery check is in progress. Wait for it to finish before sending a new request.`] :
                [`${errorPrefix}_cooldown`, `${label} retry cooldown is active. Previous failure: ${(descriptions[lastLocalFailure] || descriptions.upstream_error)[1]}`];
            if (reason === 'circuit_open') previousErrorCode = (descriptions[lastLocalFailure] || descriptions.upstream_error)[0];
            if (!recoveryInProgress) retryAfterSeconds = remaining;
            const retryHint = recoveryInProgress ? '' : remaining > 0 ?
              ` Wait ${remaining} second${remaining === 1 ? '' : 's'} before a new request.` :
              ` Send a new request to check ${label} recovery.`;
            message = `[${code}] ${description}${retryHint} Plan fallback disabled.`;
          }
          console.error(JSON.stringify({ event: `${errorPrefix}_mode_rejected`, provider, reason, ...(detail ? { detail } : {}),
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
        const proBody = model === DEEPSEEK || (configuredAlias && !isPro(model) && !isSonnet(model)) ? JSON.stringify({ ...body, model: fallbackModel }) : raw;
        // Bound a stalled Plan upstream; user cancellation still flows through client.signal.
        const plan = new AbortController();
        const cancelPlan = () => plan.abort('client_close');
        client.signal.addEventListener('abort', cancelPlan, { once: true });
        let stall;
        const arm = ms => { clearTimeout(stall); stall = setTimeout(() => plan.abort('plan_timeout'), ms); };
        if (body.stream === true) arm(planHeaderMs);
        try {
        const upstream = await abortable(fetchImpl('https://api.anthropic.com' + req.url,
          { method: 'POST', headers, body: proBody, redirect: 'error', signal: plan.signal }), plan.signal);
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
              arm(planIdleMs);
              const { done, value } = await abortable(reader.read(), plan.signal);
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
        } finally {
          clearTimeout(stall);
          client.signal.removeEventListener('abort', cancelPlan);
        }
      };
      const nativeDetail = req.url.startsWith('/v1/messages/count_tokens') ? 'token_count' : compatProfile ?
        compatExclusion(body, req.headers['anthropic-beta']) : isPro(model) ? 'plan_model' : externalExclusion(body, req.headers['anthropic-beta'], localOnly);
      if (nativeDetail) return await sendPro('native_feature', nativeDetail);
      if (openUntil && (now() < openUntil || probing)) return await sendPro('circuit_open');
      const probe = Boolean(openUntil);
      if (probe) probing = true;
      const claimedGeneration = generation;
      let succeeded = false, localFailure = 'upstream_error';
      try {
        const externalModel = provider === 'local' ? localModel : openrouterModel;
        const externalBody = { ...body, model: externalModel };
        if (Array.isArray(externalBody.messages)) externalBody.messages = withoutNativeThinking(externalBody.messages);
        delete externalBody.metadata; // client account/session identifiers stay off third-party providers
        if (noPlan) {
          externalBody.stream = false;
        }
        // Ordinary mode: external thinking blocks would pin the next turn to Plan (unsigned history), so never request them.
        if (!compatProfile) externalBody.thinking = { type: 'disabled' };
        if (body.tools) externalBody.tools = body.tools.map(tool => ({ ...tool, input_schema: compatibleSchema(tool.input_schema) }));
        if (provider === 'openrouter') externalBody.provider = { allow_fallbacks: false };
        const headers = { 'content-type': 'application/json', 'anthropic-version': req.headers['anthropic-version'] || '2023-06-01' };
        if (compatProfile && req.headers['anthropic-beta']) headers['anthropic-beta'] = req.headers['anthropic-beta'];
        const credential = provider === 'local' ? localKey : key;
        if (credential) headers.authorization = `Bearer ${credential}`;
        const attempts = noPlan ? 1 : 4;
        // Permanent 4xx will not change on retry; request-level ones must not open the provider-wide circuit.
        let requestRejected = false;
        for (let attempt = 0; attempt < attempts; attempt++) {
          client.signal.throwIfAborted();
          const controller = new AbortController();
          const cancel = () => controller.abort('client_close');
          client.signal.addEventListener('abort', cancel, { once: true });
          // Ordinary mode: attemptTimeoutMs bounds silence (headers and gaps between chunks), so long but live
          // generations survive; attemptCeilingMs bounds the total. Local/compat modes keep the fixed total limit.
          let timer;
          const arm = () => { clearTimeout(timer); timer = setTimeout(() => controller.abort('attempt_timeout'), attemptTimeoutMs); };
          arm();
          const ceiling = noPlan ? undefined : setTimeout(() => controller.abort('attempt_timeout'), attemptCeilingMs);
          let status, retryAfterMs = 0;
          try {
            const operation = (async () => {
              const response = await fetchImpl(externalUrl, { method: 'POST', headers,
                body: JSON.stringify(externalBody), redirect: 'error', signal: controller.signal });
              status = response.status;
              if (status === 429) { const seconds = Number(response.headers.get('retry-after')); if (seconds > 0) retryAfterMs = seconds * 1000; }
              if (!response.ok) {
                await response.body?.cancel();
                throw new Error('External request failed');
              }
              const validated = await bufferedResponse(response, externalModel, model, noPlan ? false : body.stream, controller.signal, noPlan ? undefined : arm, provider === 'openrouter');
              if (compatProfile) {
                const content = JSON.parse(validated.text).content;
                if (content.some(block => Object.keys(block).some(name =>
                    !(responseBlockField(block.type, name) ||
                      (['thinking', 'redacted_thinking'].includes(block.type) && ['type', 'thinking', 'signature', 'data', 'cache_control'].includes(name)) ||
                      (['thinking', 'redacted_thinking', 'tool_use'].includes(block.type) && name === 'provider_specific_fields'))) ||
                    (block.signature !== undefined && typeof block.signature !== 'string') ||
                    (block.provider_specific_fields !== undefined && block.provider_specific_fields !== null &&
                      (typeof block.provider_specific_fields !== 'object' || Array.isArray(block.provider_specific_fields)))))
                  throw new Error('Unsupported Local response fields');
              }
              if (localOnly && !localCompat) {
                if (validated.contentType !== 'application/json') throw new Error('Expected JSON Local response');
                const content = JSON.parse(validated.text).content;
                if (content.some(block => ['thinking', 'redacted_thinking'].includes(block.type)))
                  throw new Error('Local thinking response disabled');
                if (content.some(block => !['text', 'tool_use'].includes(block.type)))
                  throw new Error('Unsupported Local response content');
                const extraFields = content.flatMap(block => Object.keys(block).filter(name => !responseBlockField(block.type, name) &&
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
              // shortcut: textual match can false-positive on a tool_use input object containing this literal; refine if seen.
              if (!compatProfile && !localOnly && /"type"\s*:\s*"(?:redacted_thinking|thinking|thinking_delta|signature_delta)"/.test(validated.text))
                throw new Error('Thinking response in ordinary mode');
              return noPlan && body.stream === true ? jsonMessageStream(validated.text) : validated;
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
              'Incomplete message': 'incomplete_message', 'Invalid response': 'expected_sse', 'Unexpected SSE response': 'unexpected_sse',
              'Invalid content delta': 'invalid_delta', 'Truncated stream': 'truncated_stream',
              'Empty message response': 'empty_response', 'Invalid message response': 'invalid_message',
              'Invalid stream metadata': 'invalid_stream_metadata', 'Expected JSON Local response': 'expected_json', 'Unsupported Local response content': 'unsupported_local_content',
              'Local thinking response disabled': 'thinking_response', 'Thinking response in ordinary mode': 'thinking_response', 'Unsupported Local response fields': 'unsupported_content_fields' }[error?.message];
            if (noPlan) localFailure = controller.signal.reason === 'attempt_timeout' ? 'timeout' :
              status === 401 || status === 403 ? 'authentication_failed' : status === 402 ? 'payment_required' : validation === 'empty_response' ? 'empty_response' : 'upstream_error';
            console.error(JSON.stringify({ event: 'external_attempt_failed', provider, attempt: attempt + 1,
              ...(Number.isInteger(status) ? { status } : {}), ...(validation ? { validation } : {}),
              category: controller.signal.reason === 'attempt_timeout' ? 'timeout' : 'upstream' }));
            controller.abort('attempt_failed');
            if ([400, 413, 422].includes(status) || error?.message === 'Thinking response in ordinary mode') requestRejected = true;
            if (requestRejected || [401, 402, 403, 404].includes(status)) break;
          } finally {
            clearTimeout(timer);
            clearTimeout(ceiling);
            client.signal.removeEventListener('abort', cancel);
          }
          if (attempt + 1 < attempts && retryDelayMs) await abortable(new Promise(resolve => {
            const timeout = setTimeout(resolve, Math.min(Math.max(retryDelayMs, retryAfterMs), 10000));
            client.signal.addEventListener('abort', () => { clearTimeout(timeout); resolve(); }, { once: true });
          }), client.signal);
        }
        if (!requestRejected && claimedGeneration === generation) {
          generation++;
          if (noPlan) lastLocalFailure = localFailure;
          openUntil = now() + cooldownMs;
          console.error(JSON.stringify({ event: 'circuit_open', provider }));
        }
      } finally {
        if (probe) probing = false;
      }
      if (!succeeded) await sendPro('fallback', noPlan ? localFailure : undefined);
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
  const config = { key: process.env.OPENROUTER_API_KEY, openrouterModel: process.env.OPENROUTER_MODEL, nonce: process.env.LOCAL_ROUTE_TOKEN,
    provider: process.env.SONNET_PROVIDER || 'openrouter', localUrl: process.env.LOCAL_LLM_URL,
    localModel: process.env.LOCAL_LLM_MODEL, localKey: process.env.LOCAL_LLM_API_KEY,
    localOnly: process.env.LOCAL_ROUTE_ONLY === '1', localCompat: process.env.LOCAL_ROUTE_COMPAT === '1', openrouterCompat: process.env.OPENROUTER_ROUTE_COMPAT === '1', localHttpUrl: process.env.LOCAL_LLM_HTTP_URL, localAllowRemote: process.env.LOCAL_LLM_ALLOW_REMOTE === 'true' };
  for (const name of ['OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'LOCAL_ROUTE_TOKEN', 'LOCAL_ROUTE_ONLY', 'LOCAL_ROUTE_COMPAT', 'OPENROUTER_ROUTE_COMPAT', 'SONNET_PROVIDER', 'LOCAL_LLM_URL',
    'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY', 'LOCAL_LLM_ALLOW_REMOTE', 'LOCAL_LLM_HTTP_URL', 'LOCAL_LLM_MODELS_URL']) delete process.env[name];
  if (process.argv.slice(2).includes('--check-connection')) {
    try {
      const result = await checkConnection(config);
      process.stdout.write(JSON.stringify(result) + '\n');
      process.exitCode = result.connection_check ? 0 : 1;
    } catch {
      process.stdout.write(JSON.stringify({ connection_check: false, provider: ['local', 'openrouter'].includes(config.provider) ? config.provider : 'unknown',
        inference_checked: false, model_confirmed: false, reply_confirmed: false, reason: 'invalid_configuration' }) + '\n');
      process.exitCode = 1;
    }
  } else if (process.argv.slice(2).includes('--diagnose-local')) {
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
    if (process.argv.slice(2).includes('--parent-pipe')) {
      const parentClosed = () => {
        console.error(JSON.stringify({ event: config.openrouterCompat ? 'openrouter_parent_disconnected' : config.localOnly ? 'local_parent_disconnected' : 'parent_disconnected', provider: config.provider }));
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
