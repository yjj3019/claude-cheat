import http from 'node:http';
import { once } from 'node:events';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { appendFileSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Diagnostics leave the process through one redaction pass: an upstream error body echoed
// into a log line or an error response must never carry a credential. The tags stay stable
// and greppable; model names, status codes and trace ids never match any rule.
const REDACTED_BEARER = '[redacted:bearer-token]';
const REDACTED_API_KEY = '[redacted:api-key]';
const REDACTED_PROVIDER_KEY = '[redacted:provider-key]';
// A provider error body is relayed only up to this many bytes after scrubbing.
const MAX_ERROR_BODY = 64 * 1024;
export function redactDiagnostic(text, secrets = []) {
  if (typeof text !== 'string' || !text) return text;
  let output = text;
  // The configured credential is the most specific secret: replace it verbatim first so a
  // provider key can never be split by the generic patterns.
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret.length >= 4 && output.includes(secret))
      output = output.split(secret).join(REDACTED_PROVIDER_KEY);
  }
  output = output.replace(/Bearer[ \t]+(?!\[redacted:)[^\s"',}\]]+/gi, `Bearer ${REDACTED_BEARER}`);
  output = output.replace(/\bsk-(?:ant-|or-)?[A-Za-z0-9._-]{4,}/g, REDACTED_API_KEY);
  output = output.replace(/("x-api-key"\s*:\s*")(?!\[redacted:)[^"]*(")/gi, `$1${REDACTED_API_KEY}$2`);
  output = output.replace(/(x-api-key\s*[:=]\s*)(?!\[redacted:)[^\s"',}]+/gi, `$1${REDACTED_API_KEY}`);
  return output;
}
// The one logging sink: every structured line of this proxy goes through it.
function emitDiagnostic(text, secrets = []) {
  console.error(redactDiagnostic(text, secrets));
}
// Reads at most `limit` bytes from a web stream and cancels the rest, so an oversized
// provider error body is never fully buffered. One extra byte is read to detect truncation.
async function readBoundedBody(body, limit, signal, onProgress) {
  const reader = body.getReader();
  const chunks = [];
  let total = 0, truncated = false;
  try {
    while (total <= limit) {
      const { done, value } = await abortable(reader.read(), signal);
      if (done) break;
      onProgress?.();
      const allowed = limit + 1 - total;
      if (value.byteLength >= allowed) {
        if (allowed > 0) chunks.push(value.subarray(0, allowed));
        total += Math.max(0, allowed);
        truncated = true;
        break;
      }
      chunks.push(value);
      total += value.byteLength;
    }
  } finally {
    // The source may never settle cancellation; do not let cleanup defeat the deadline.
    reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  return { text: Buffer.concat(chunks).subarray(0, limit).toString('utf8'), truncated };
}

// Anthropic accepts only ^[a-zA-Z0-9_-]{1,64}$ tool ids. Third-party providers mint ids like
// `Bash:0#abc`; when such history meets the native Plan route it is rejected, so only the
// non-conforming ids are remapped (conforming ids stay byte-identical for prompt caching).
const PLAN_TOOL_ID = /^[a-zA-Z0-9_-]{1,64}$/;
function normalizeToolCallIds(body) {
  if (!Array.isArray(body?.messages)) return { body, changed: false, count: 0, empty: false };
  const reserved = new Set();
  const nonConforming = new Set();
  for (const message of body.messages) {
    if (!Array.isArray(message?.content)) continue;
    for (const block of message.content) {
      const id = block?.type === 'tool_use' ? block.id : block?.type === 'tool_result' ? block.tool_use_id : undefined;
      if (id === undefined) continue;
      // An empty id has no representable form on the native contract: reject the request.
      if (typeof id !== 'string' || id.length === 0) return { body, changed: false, count: 0, empty: true };
      if (PLAN_TOOL_ID.test(id)) reserved.add(id);
      else nonConforming.add(id);
    }
  }
  if (!nonConforming.size) return { body, changed: false, count: 0, empty: false };
  // Deterministic per-request allocator: encounter order and a counter skipping every id the
  // history already reserves, so generated ids are collision-free and stable across retries.
  const replacements = new Map();
  let counter = 0;
  for (const id of nonConforming) {
    let candidate;
    do { counter += 1; candidate = `toolu_${counter.toString(36)}`; } while (reserved.has(candidate));
    reserved.add(candidate);
    replacements.set(id, candidate);
  }
  const messages = body.messages.map(message => {
    if (!Array.isArray(message?.content)) return message;
    let touched = false;
    const content = message.content.map(block => {
      if (block?.type === 'tool_use' && replacements.has(block.id)) {
        touched = true;
        return { ...block, id: replacements.get(block.id) };
      }
      if (block?.type === 'tool_result' && replacements.has(block.tool_use_id)) {
        touched = true;
        return { ...block, tool_use_id: replacements.get(block.tool_use_id) };
      }
      return block;
    });
    return touched ? { ...message, content } : message;
  });
  return { body: { ...body, messages }, changed: true, count: replacements.size, empty: false };
}

// Opt-in external-path stabilization: the CLI appends a volatile token-count footer to the
// last system text block, which invalidates the provider's prefix cache. ROUTE_STABILIZE_PROMPT_CACHE=1
// peels only that unfenced, very-last fragment; every other byte stays identical. Off by default.
const PROMPT_CACHE_FOOTER = /<total_tokens>\d+ tokens left<\/total_tokens>$/;
function peelPromptCacheFooter(text) {
  const match = PROMPT_CACHE_FOOTER.exec(text);
  if (!match || match.index + match[0].length !== text.length) return text;
  // An odd number of fences before the footer means it is code, not a client tracker line.
  if ((text.slice(0, match.index).split('```').length - 1) % 2 === 1) return text;
  return text.slice(0, match.index);
}
function stabilizeSystem(system) {
  if (typeof system === 'string') return peelPromptCacheFooter(system);
  if (!Array.isArray(system)) return system;
  // Only the last text block may change; earlier blocks (and their cache_control markers)
  // stay identical, and user/assistant content is never touched.
  let index = -1;
  for (let position = system.length - 1; position >= 0; position -= 1) {
    if (system[position]?.type === 'text' && typeof system[position].text === 'string') { index = position; break; }
  }
  if (index < 0) return system;
  const text = system[index].text;
  const stripped = peelPromptCacheFooter(text);
  if (stripped === text) return system;
  const blocks = system.slice();
  blocks[index] = { ...system[index], text: stripped };
  return blocks;
}

// Diagnostic mirror: routing events also land in a file so a session can be explained afterwards.
// The launcher sets ROUTE_LOG_PATH for Local and OpenRouter sessions; without it nothing is written.
const routeLogPath = process.env.ROUTE_LOG_PATH;
const ROUTE_LOG_LIMIT = 8 * 1024 * 1024;
if (routeLogPath) {
  delete process.env.ROUTE_LOG_PATH;
  const emit = console.error.bind(console);
  let writes = 0;
  // The log is diagnostic, not a journal: rotate it so a long-running session cannot
  // fill the disk. The dashboard notices the new file through its prefix fingerprint.
  const rotate = () => {
    try {
      if (statSync(routeLogPath).size < ROUTE_LOG_LIMIT) return;
      renameSync(routeLogPath, `${routeLogPath}.1`);
    } catch {}
  };
  console.error = (...args) => {
    emit(...args);
    try {
      // Check before writing so the line that triggers the rotation lands in the new file.
      if (writes % 200 === 0) rotate();
      appendFileSync(routeLogPath, `${new Date().toISOString()} ${args.join(' ')}\n`);
      writes += 1;
    } catch {}
  };
  // Record which source revision this process loaded so a stale run is visible afterwards.
  const codeStamp = (() => {
    try { return statSync(fileURLToPath(import.meta.url)).mtime.toISOString(); } catch { return null; }
  })();
  // A content digest tells the dashboard whether the code really differs, not just the mtime.
  const codeHash = (() => {
    try {
      return createHash('sha256').update(readFileSync(fileURLToPath(import.meta.url))).digest('hex').slice(0, 12);
    } catch { return null; }
  })();
  try {
    appendFileSync(routeLogPath, `${new Date().toISOString()} --- proxy start` +
      `${codeStamp ? ` code=${codeStamp}` : ''}${codeHash ? ` hash=${codeHash}` : ''} ---\n`);
  } catch {}
}

// Historical client alias only; the upstream model must be configured explicitly.
export const DEEPSEEK = 'deepseek/deepseek-v4.1-flash-1017';
const isPro = model => typeof model === 'string' && /^claude-(opus|haiku|fable)-[0-9][a-zA-Z0-9.-]*$/.test(model);
const isSonnet = model => typeof model === 'string' && /^claude-sonnet-[0-9][a-zA-Z0-9.-]*$/.test(model);
// Measurement only: coarse request shape (booleans/enums, never content or identifiers).
const modelFamily = model => isSonnet(model) ? 'sonnet' : isPro(model) ? model.split('-')[1] : 'other';
function requestShape(body) {
  let tool_blocks = false, media = false;
  for (const message of body.messages) {
    if (!Array.isArray(message && message.content)) continue;
    for (const block of message.content) {
      const type = block && block.type;
      if (type === 'tool_use' || type === 'tool_result') tool_blocks = true;
      else if (type !== 'text' && type !== 'thinking' && type !== 'redacted_thinking') media = true;
      if (tool_blocks && media) return { tool_blocks, media };
    }
  }
  return { tool_blocks, media };
}
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
const DIAGNOSTIC_BLOCK_TYPES = ['text', 'tool_use', 'thinking', 'redacted_thinking'];
const DIAGNOSTIC_USAGE_FIELDS = ['input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'];
// Keep diagnostics separate from throwable provider objects: an upstream cannot inject
// arbitrary error properties into the log. Only bounded enums/counts leave this boundary.
const emptyResponseDiagnostics = new WeakMap();
function safeResponseUsage(usage) {
  return Object.fromEntries(DIAGNOSTIC_USAGE_FIELDS.filter(name =>
    Number.isSafeInteger(usage?.[name]) && usage[name] >= 0).map(name => [name, usage[name]]));
}
function emptyResponseError(detail, format, stopReason, counts = {}, usage = {}) {
  const error = Object.assign(new Error('Empty message response'), { validationDetail: detail });
  const blockTypes = Object.fromEntries(DIAGNOSTIC_BLOCK_TYPES.filter(type => counts[type] > 0)
    .map(type => [type, Math.min(65535, counts[type])]));
  emptyResponseDiagnostics.set(error, {
    format, stop_reason: STOP_REASONS.has(stopReason) ? stopReason : null,
    block_count: Math.min(65535, Object.values(blockTypes).reduce((sum, count) => sum + count, 0)),
    block_types: blockTypes, usage: safeResponseUsage(usage),
  });
  return error;
}
function usableContent(block) {
  return block.type === 'text' ? block.text.trim().length > 0 :
    block.type === 'tool_use' && block.id.trim().length > 0 && block.name.trim().length > 0;
}
function validContentBlock(block) {
  if (!block || typeof block !== 'object') return false;
  if (block.type === 'text') return typeof block.text === 'string';
  if (block.type === 'thinking') return typeof block.thinking === 'string';
  if (block.type === 'redacted_thinking') return typeof block.data === 'string';
  if (block.type === 'tool_use') return typeof block.id === 'string' && block.id.trim().length > 0 &&
    typeof block.name === 'string' && block.name.trim().length > 0 && block.input !== null && typeof block.input === 'object' && !Array.isArray(block.input);
  return false;
}

const MAX_RESPONSE = 32 * 1024 * 1024;
const BASIC_FIELDS = new Set(['model', 'messages', 'max_tokens', 'system', 'stream', 'temperature',
  'top_p', 'top_k', 'stop_sequences', 'tools', 'tool_choice', 'metadata', 'thinking', 'output_config', 'safeguards', 'cache_control']);
const CONTENT_FIELDS = {
  // citations is emitted by providers and echoed back in history by the CLI, so it must be accepted.
  text: new Set(['type', 'text', 'cache_control', 'citations']),
  image: new Set(['type', 'source', 'cache_control']),
  tool_use: new Set(['type', 'id', 'name', 'input', 'cache_control', 'caller']),
  tool_result: new Set(['type', 'tool_use_id', 'content', 'is_error', 'cache_control']),
};
// Fields only a provider may add to a response block; citations is also a request field today.
const RESPONSE_FIELDS = {};
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
// Scanning first avoids rebuilding (and re-allocating) a long history on every turn; most
// sessions never carry signed reasoning because ordinary mode disables it upstream.
const hasNativeThinking = messages => messages.some(message =>
  Array.isArray(message.content) && message.content.some(isNativeThinking));
const COMPAT_BETAS = new Set(['interleaved-thinking-2025-05-14', 'redact-thinking-2026-02-12',
  'context-management-2025-06-27', 'prompt-caching-scope-2026-01-05', 'per-turn-control-2026-07-01',
  'mid-conversation-tool-changes-2026-07-01', 'inline-tools-2026-09-15', 'advisor-tool-2026-03-01',
  'advanced-tool-use-2025-11-20', 'effort-2025-11-24', 'structured-outputs-2025-12-15',
  'dangerous-tool-use-2026-09-03', 'thinking-display-updates-2026-08-18', 'afk-mode-2026-01-31',
  'extended-cache-ttl-2025-04-11', 'fallback-credit-2026-06-01', 'thinking-token-count-2026-05-13']);
function withoutNullToolMetadata(body) {
  if (!Array.isArray(body.messages)) return body;
  let changed = false;
  const messages = body.messages.map(message => {
    if (!Array.isArray(message?.content)) return message;
    const content = message.content.map(block => {
      if (block?.type !== 'tool_use' || block.provider_specific_fields !== null) return block;
      changed = true;
      const copy = { ...block }; delete copy.provider_specific_fields;
      return copy;
    });
    return { ...message, content };
  });
  return changed ? { ...body, messages } : body;
}

function localInlineTools(body) {
  body = withoutNullToolMetadata(body);
  if (!Array.isArray(body.messages) || (body.tools !== undefined && !Array.isArray(body.tools))) return null;
  const tools = [...(body.tools || [])], messages = [];
  const sameDefinition = (first, second) => {
    const a = { ...first }, b = { ...second };
    delete a.cache_control; delete b.cache_control;
    return JSON.stringify(a) === JSON.stringify(b);
  };
  for (const message of body.messages) {
    if (!message || typeof message !== 'object') return null;
    if (!Array.isArray(message.content)) { messages.push(message); continue; }
    const content = [];
    for (const block of message.content) {
      if (block?.type !== 'tool_addition') { content.push(block); continue; }
      if (!['user', 'system'].includes(message.role) || Object.keys(block).some(key => !['type', 'tool', 'cache_control'].includes(key)) ||
          !block.tool || typeof block.tool !== 'object' || Array.isArray(block.tool)) return null;
      if (block.tool.type !== 'tool_definition' || Object.keys(block.tool).some(key => !['type', 'definition'].includes(key))) return null;
      const definition = block.tool.definition;
      if (!definition || typeof definition !== 'object' || Array.isArray(definition) || typeof definition.name !== 'string') return null;
      const tool = { ...definition };
      if (block.cache_control !== undefined) {
        const cache = block.cache_control;
        if (!cache || cache.type !== 'ephemeral' || Object.keys(cache).some(key => !['type', 'ttl'].includes(key)) ||
            (cache.ttl !== undefined && !['5m', '1h'].includes(cache.ttl)) ||
            (tool.cache_control !== undefined && JSON.stringify(tool.cache_control) !== JSON.stringify(cache))) return null;
        tool.cache_control = cache;
      }
      const index = tools.findIndex(previous => previous?.name === tool.name);
      if (index < 0) tools.push(tool);
      else if (tools[index].defer_loading === true || sameDefinition(tools[index], tool)) tools[index] = tool;
      else return null;
    }
    if (!content.length) return null;
    messages.push({ ...message, content });
  }
  if (!messages.length) return null;
  const cacheCount = tools.filter(tool => tool?.cache_control).length +
    messages.reduce((count, message) => count + (Array.isArray(message.content) ? message.content.filter(block => block?.cache_control).length : 0), 0) +
    (Array.isArray(body.system) ? body.system.filter(block => block?.cache_control).length : 0);
  if (cacheCount > 4) return null;
  return { ...body, tools, messages };
}

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
// Claude Code sends these on every request, even with experimental betas disabled. They only
// steer reasoning hints or the client's context budget, so a name alone must not divert traffic to Plan.
// context-1m only raises the client budget: it adds no request field and providers ignore the header.
// fallback-credit is client bookkeeping for the Plan fallback model; it changes no request field either.
const BASELINE_BETAS = new Set(['interleaved-thinking-2025-05-14', 'effort-2025-11-24',
  'context-1m-2025-08-07', 'fallback-credit-2026-06-01']);
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

// Frames a provider may send once a message has started.
const STREAM_EVENTS = new Set(['message_start', 'content_block_start', 'content_block_delta',
  'content_block_stop', 'message_delta', 'message_stop', 'ping']);

// Validates one SSE frame and returns the text to forward. Throws on anything the
// client could not accept, which is what keeps a bad reply from reaching the session.
function streamValidator(model, clientModel, strictMeta, rejectThinking = false) {
  let started = false, delta = false, stopped = false, usable = false, stopReason;
  // Frames may only leave the proxy once the reply provably has usable content: until
  // then a failure must stay recoverable (retry, then Plan fallback).
  let publishable = false, sawThinking = false;
  const blocks = new Map();
  const indices = new Set();
  const diagnosticCounts = {};
  let diagnosticUsage = {};
  const thinking = block => ['thinking', 'redacted_thinking'].includes(block?.type);
  return {
    get started() { return started; },
    get stopped() { return stopped; },
    get publishable() { return publishable && started; },
    validate(frame) {
      const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
      if (!data) return frame + '\n\n';
      if (data === '[DONE]') { if (!stopped) throw new Error('Premature stream terminator'); return ''; }
      const event = JSON.parse(data);
      const named = frame.split(/\r?\n/).find(line => line.startsWith('event:'))?.slice(6).trim();
      if (named !== undefined && named !== event.type) throw new Error('Unexpected stream event');
      if (!STREAM_EVENTS.has(event.type)) throw new Error('Unexpected stream event');
      if (stopped) { if (event.type === 'ping') return ''; throw new Error('Event after message stop'); }
      if (event.type === 'message_start') {
        if (started || event.message?.model !== model) throw new Error('Wrong response model');
        if (strictMeta && !validMessageMeta(event.message)) throw new Error('Invalid stream metadata');
        if (event.message.content !== undefined && (!Array.isArray(event.message.content) || event.message.content.length))
          throw new Error('Nonempty stream start content');
        started = true;
        diagnosticUsage = safeResponseUsage(event.message.usage);
        event.message.model = clientModel;
      } else if (event.type !== 'ping' && !started) throw new Error('Missing start');
      if (event.type === 'content_block_start') {
        if (!Number.isInteger(event.index) || event.index < 0 || indices.has(event.index) || delta ||
            !validContentBlock(event.content_block)) throw new Error('Invalid content block');
        // Ordinary mode never accepts reasoning blocks from the external provider; the
        // buffered path caught these with a text scan, which a stream cannot rely on.
        if (thinking(event.content_block)) {
          if (rejectThinking) throw new Error('Thinking response in ordinary mode');
          sawThinking = true;
        }
        indices.add(event.index);
        const type = event.content_block.type;
        if (DIAGNOSTIC_BLOCK_TYPES.includes(type)) diagnosticCounts[type] = Math.min(65535, (diagnosticCounts[type] || 0) + 1);
        usable ||= usableContent(event.content_block);
        blocks.set(event.index, { type: event.content_block.type, json: '', hasJson: false });
      }
      if (event.type === 'content_block_delta') {
        const block = blocks.get(event.index);
        if (!block || !event.delta) throw new Error('Unknown content block');
        if (['thinking_delta', 'signature_delta'].includes(event.delta.type)) {
          if (rejectThinking) throw new Error('Thinking response in ordinary mode');
          sawThinking = true;
        }
        if (event.delta.type === 'input_json_delta' && block.type === 'tool_use' && typeof event.delta.partial_json === 'string') {
          block.hasJson = true;
          block.json += event.delta.partial_json;
        } else if (!((event.delta.type === 'text_delta' && block.type === 'text' && typeof event.delta.text === 'string') ||
            (event.delta.type === 'thinking_delta' && block.type === 'thinking' && typeof event.delta.thinking === 'string') ||
            (event.delta.type === 'signature_delta' && block.type === 'thinking' && typeof event.delta.signature === 'string')))
          throw new Error('Invalid content delta');
        if (event.delta.type === 'text_delta' && event.delta.text.trim().length > 0) { usable = true; publishable = true; }
      }
      if (event.type === 'content_block_stop') {
        const block = blocks.get(event.index);
        if (!block) throw new Error('Unknown content block');
        if (block.type === 'tool_use' && !block.hasJson) publishable = true;
        if (block.hasJson && block.json.trim()) {
          const input = JSON.parse(block.json);
          if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Invalid tool input');
          if (block.type === 'tool_use') publishable = true;
        }
        blocks.delete(event.index);
      }
      if (event.type === 'message_delta') {
        if (blocks.size || !STOP_REASONS.has(event.delta?.stop_reason)) throw new Error('Incomplete message');
        delta = true;
        stopReason = event.delta.stop_reason;
        diagnosticUsage = { ...diagnosticUsage, ...safeResponseUsage(event.usage) };
      }
      if (event.type === 'message_stop') {
        if (!started || !delta || blocks.size) throw new Error('Missing start or delta');
        stopped = true;
      }
      return model === clientModel || event.type !== 'message_start' ? frame + '\n\n' :
        frame.split(/\r?\n/).filter(line => !line.startsWith('data:')).concat(`data: ${JSON.stringify(event)}`).join('\n') + '\n\n';
    },
    finish() {
      if (!started || !stopped) throw new Error('Truncated stream');
      if (!usable && !CONTROL_STOPS.has(stopReason)) throw emptyResponseError(
        'stream_without_usable_content', 'sse', stopReason, diagnosticCounts, diagnosticUsage);
      if (rejectThinking && sawThinking) throw new Error('Thinking response in ordinary mode');
    },
  };
}

async function bufferedResponse(upstream, model, clientModel, stream, signal, onProgress, strictMeta = false, onFrame = null, rejectThinking = false, onDrain = null) {
  if (!upstream.ok) throw new Error('External request failed');
  const sse = (upstream.headers.get('content-type') || '').includes('text/event-stream');
  if (!upstream.body) throw emptyResponseError('missing_body', sse ? 'sse' : 'json');
  if (stream === true && !sse) throw new Error('Invalid response');
  if (stream !== true && sse) throw new Error('Unexpected SSE response');
  const reader = upstream.body.getReader();
  const chunks = [];
  const output = [];
  let size = 0, eventPending = '', forwarded = false;
  const eventDecoder = new TextDecoder();
  const validator = sse ? streamValidator(model, clientModel, strictMeta, rejectThinking) : null;
  const cancel = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  const drainIfNeeded = async () => {
    if (!backpressured) return;
    backpressured = false;
    if (onDrain) await onDrain();
  };
  // A frame is forwarded as soon as it validates, so the client sees tokens while the
  // provider is still generating. An invalid frame after that point can no longer fall
  // back, so it is reported with `streamed` for the caller to close the response.
  const pending = [];
  let committed = false;
  let backpressured = false;
  const forward = text => {
    if (!onFrame || !text) return;
    try {
      // write() === false means the socket buffer is full; the caller must wait for drain.
      backpressured = onFrame(text) === false;
      forwarded = true;
    } catch { /* client went away; the close handler aborts */ }
  };
  const emit = text => {
    if (!onFrame || !text) return;
    if (committed) { forward(text); return; }
    pending.push(text);
    if (!validator.publishable) return;
    committed = true;
    for (const item of pending.splice(0)) forward(item);
  };
  const guarded = operation => {
    try { return operation(); } catch (error) {
      if (forwarded) Object.assign(error, { streamed: true });
      throw error;
    }
  };
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      onProgress?.();
      size += value.byteLength;
      if (size > MAX_RESPONSE) throw new Error('Response too large');
      if (!sse) chunks.push(Buffer.from(value));
      await drainIfNeeded();
      if (sse) {
        eventPending += eventDecoder.decode(value, { stream: true });
        let boundary, terminal = false;
        while ((boundary = /\r?\n\r?\n/.exec(eventPending))) {
          const frame = eventPending.slice(0, boundary.index);
          eventPending = eventPending.slice(boundary.index + boundary[0].length);
          const text = guarded(() => validator.validate(frame));
          if (onFrame) emit(text); else if (text) output.push(text);
          // Keep draining buffered frames after the stop so a trailing protocol error is
          // still caught, then stop reading; the provider may leave the stream open.
          if (validator.stopped) terminal = true;
        }
        if (terminal) break;
      }
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (!sse) {
    const text = (chunks.length === 1 ? chunks[0] : Buffer.concat(chunks)).toString('utf8');
    if (!text.trim()) throw emptyResponseError('empty_body', 'json');
    const response = JSON.parse(text);
    if (response.type !== 'message' || response.model !== model || !Array.isArray(response.content) ||
        !response.content.every(validContentBlock) || !STOP_REASONS.has(response.stop_reason) ||
        (strictMeta && !validMessageMeta(response))) throw new Error('Invalid message response');
    if (!response.content.some(usableContent) && !CONTROL_STOPS.has(response.stop_reason)) {
      const counts = {};
      for (const block of response.content) counts[block.type] = Math.min(65535, (counts[block.type] || 0) + 1);
      throw emptyResponseError(response.content.length ? 'content_without_usable_output' : 'empty_content',
        'json', response.stop_reason, counts, response.usage);
    }
    response.model = clientModel;
    return { contentType: 'application/json', text: JSON.stringify(response), streamed: false };
  }
  const tail = eventPending;
  // A split [DONE] or ping after message_stop must not turn a valid answer into a retry,
  // while a truncated real event still does.
  const tailText = tail.trim();
  const benignTail = !tailText || tailText === 'event: ping' || /^data:\s*\[D?O?N?E?\]?$/.test(tailText);
  if (!benignTail) throw guarded(() => { throw new Error('Trailing incomplete event'); });
  guarded(() => validator.finish());
  if (onFrame) return { contentType: 'text/event-stream', text: committed ? '' : pending.join(''), streamed: forwarded };
  return { contentType: 'text/event-stream', text: output.join(''), streamed: false };
}

function jsonMessageStream(text, requireMeta = true) {
  const message = JSON.parse(text);
  // Local compatibility servers return Messages-shaped bodies without OpenRouter metadata;
  // local-only (and OpenRouter) still require it.
  if (requireMeta && !validMessageMeta(message)) throw new Error('Invalid stream metadata');
  if (!message || typeof message !== 'object' || !Array.isArray(message.content))
    throw new Error('Invalid message response');
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

// Retry-After is delta-seconds or an HTTP-date (RFC 7231); the proxy works in milliseconds.
// A value beyond 10 minutes is clamped: a client may be told to wait, but the proxy never
// trusts an unbounded number from a provider.
const RETRY_AFTER_MAX_MS = 10 * 60 * 1000;
// A 429 may name its own recovery window; the breaker trusts it only up to the same cap.
const RATE_LIMIT_COOLDOWN_MAX_MS = 10 * 60 * 1000;
// Date.parse is lenient enough to read arbitrary text as a year, so dates are shape-checked
// first against the three HTTP-date forms (IMF-fixdate, RFC 850, asctime).
const MONTH = 'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec';
const SHORT_DAY = 'Mon|Tue|Wed|Thu|Fri|Sat|Sun';
const LONG_DAY = 'Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday';
const HTTP_DATE = new RegExp(`^(?:(?:${SHORT_DAY}), \\d{2} (?:${MONTH}) \\d{4} \\d{2}:\\d{2}:\\d{2} GMT|` +
  `(?:${LONG_DAY}), \\d{2}-(?:${MONTH})-\\d{2} \\d{2}:\\d{2}:\\d{2} GMT|` +
  `(?:${SHORT_DAY}) (?:${MONTH}) [ \\d]\\d \\d{2}:\\d{2}:\\d{2} \\d{4})$`);
export function parseRetryAfter(value, nowMs = Date.now()) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text) return null;
  // Tolerate fractional seconds: providers send them even though the RFC does not.
  if (/^\d+(?:\.\d+)?$/.test(text)) return Math.min(Number(text) * 1000, RETRY_AFTER_MAX_MS);
  if (!HTTP_DATE.test(text)) return null;
  // The asctime form carries no zone marker but is defined as GMT; Date.parse would read it
  // as local time and turn a future wait into a past date outside a UTC host.
  const at = Date.parse(/ GMT$/.test(text) ? text : `${text} GMT`);
  if (!Number.isFinite(at)) return null;
  const reference = Number.isFinite(nowMs) ? nowMs : Date.now();
  return Math.min(Math.max(at - reference, 0), RETRY_AFTER_MAX_MS);
}

export function createProxy({ key, nonce, fetchImpl = fetch, provider = 'openrouter', openrouterModel, localUrl,
  localModel, localKey, localAllowRemote = false, localHttpUrl, cliKind = null, cliRequestedModel = null, localOnly = false, localCompat = false, openrouterCompat = false, fallbackModel = 'claude-sonnet-5-5', retryDelayMs = 1000,
  cooldownMs = 30000, attemptTimeoutMs = localOnly || openrouterCompat ? 120000 : 30000, planHeaderMs = 120000, planIdleMs = 300000, attemptCeilingMs = 300000, now = Date.now,
  metricsPath = process.env.ROUTE_METRICS_PATH,
  streamPassthrough = process.env.ROUTE_STREAM_PASSTHROUGH === '1',
  handoverTimeoutMs = Number(process.env.ROUTE_HANDOVER_TIMEOUT_MS) || 120000,
  // Idle, not total: a client may send for minutes as long as it keeps sending. 0 disables it.
  requestIdleTimeoutMs = Number.isFinite(Number(process.env.ROUTE_REQUEST_IDLE_TIMEOUT_MS))
    ? Number(process.env.ROUTE_REQUEST_IDLE_TIMEOUT_MS) : 30000,
  // A provider may ask for a long wait, but a CLI turn must not freeze for minutes: retries
  // wait at most this long and the full hint reaches the client as Retry-After instead.
  retryMaxWaitMs = Number.isFinite(Number(process.env.ROUTE_RETRY_MAX_WAIT_MS))
    ? Number(process.env.ROUTE_RETRY_MAX_WAIT_MS) : 10000,
  localConcurrency = 2, localQueueLimit = 8,
  localQueueWaitMs = process.env.ROUTE_LOCAL_QUEUE_WAIT_MS === undefined
    ? 60000 : Number(process.env.ROUTE_LOCAL_QUEUE_WAIT_MS),
  // Opt-in: peel the CLI's trailing token-count footer from system text before the external
  // request so the provider's prefix cache survives between turns. Documented in the README.
  stabilizePromptCache = process.env.ROUTE_STABILIZE_PROMPT_CACHE === '1',
  onHandover = null,
  // Optional second provider for ordinary sessions: a request whose model equals altAlias
  // goes to that provider instead of the session's primary one. Absent = single provider.
  altProvider = null, altModel, altUrl, altKey, altAllowRemote = false, altHttpUrl = '', altAlias }) {
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
  if (!['openrouter', 'local', 'cli'].includes(provider)) throw new Error('Unsupported Sonnet provider');
  if (provider === 'cli' && (!['claude', 'codex', 'grok'].includes(cliKind) || localModel !== 'cli/' + cliKind || altProvider !== null)) throw new Error('Invalid CLI backend');
  if (provider === 'openrouter' && !validOpenRouterModel(openrouterModel))
    throw new Error('OpenRouter model required; no default model is selected');
  if (typeof localAllowRemote !== 'boolean') throw new Error('Remote Local setting must be boolean');
  if (localKey !== undefined && (typeof localKey !== 'string' || /[\x00-\x1f\x7f]/.test(localKey))) throw new Error('Invalid Local credential');
  const externalUrl = provider !== 'openrouter' ? localEndpoint(localUrl, provider === 'cli' ? false : localAllowRemote, provider === 'cli' ? '' : localHttpUrl) : 'https://openrouter.ai/api/v1/messages';
  if (provider !== 'openrouter' && (typeof localModel !== 'string' || !localModel.trim())) throw new Error('Local model required');
  // Dual provider is an ordinary-session feature only: local-only and compatibility modes
  // rewrite requests and disable Plan fallback, so a second provider there would bypass
  // those guarantees. The alias is the exact model id the CLI must send to select it.
  let altExternalUrl = null;
  if (altProvider !== null) {
    if (localOnly || localCompat || openrouterCompat) throw new Error('Alternate provider requires an ordinary session');
    if (!['openrouter', 'local'].includes(altProvider) || altProvider === provider) throw new Error('Invalid alternate provider');
    if (typeof altAlias !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(altAlias)) throw new Error('Invalid alternate model alias');
    if (altAlias === openrouterModel || altAlias === localModel || isPro(altAlias) || isSonnet(altAlias))
      throw new Error('Alternate model alias collides with a routed model');
    if (typeof altAllowRemote !== 'boolean') throw new Error('Remote Local setting must be boolean');
    if (altProvider === 'openrouter') {
      if (!validOpenRouterModel(altModel) || typeof altKey !== 'string' || !/^sk-or-[A-Za-z0-9_-]+$/.test(altKey))
        throw new Error('Alternate OpenRouter configuration required');
      altExternalUrl = 'https://openrouter.ai/api/v1/messages';
    } else {
      if (typeof altModel !== 'string' || !altModel.trim()) throw new Error('Alternate Local model required');
      if (altKey !== undefined && (typeof altKey !== 'string' || /[\x00-\x1f\x7f]/.test(altKey)))
        throw new Error('Invalid Local credential');
      altExternalUrl = localEndpoint(altUrl, altAllowRemote, altHttpUrl);
    }
  }
  if (!isSonnet(fallbackModel)) throw new Error('Fallback must be a native Sonnet model');
  for (const value of [retryDelayMs, cooldownMs, attemptTimeoutMs, planHeaderMs, planIdleMs, attemptCeilingMs])
    if (!Number.isFinite(value) || value < 0) throw new Error('Invalid routing timeout');
  if (!Number.isFinite(requestIdleTimeoutMs) || requestIdleTimeoutMs < 0) throw new Error('Invalid request idle timeout');
  if (!Number.isFinite(retryMaxWaitMs) || retryMaxWaitMs < 0) throw new Error('Invalid retry wait');
  if (!Number.isSafeInteger(localConcurrency) || localConcurrency < 1 || localConcurrency > 64 ||
      !Number.isSafeInteger(localQueueLimit) || localQueueLimit < 0 || localQueueLimit > 256 ||
      !Number.isFinite(localQueueWaitMs) || localQueueWaitMs < 0 || localQueueWaitMs > 60000)
    throw new Error('Invalid Local admission limits');
  // This bounds this proxy's contribution, not other users of the shared server.
  const localQueue = [];
  let localActive = 0, localQueueRejected = 0;
  const localPermit = () => {
    localActive++;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      localActive--;
      localQueue[0]?.(true);
    };
  };
  const acquireLocal = signal => {
    signal.throwIfAborted();
    if (localActive < localConcurrency) return Promise.resolve(localPermit());
    if (localQueue.length >= localQueueLimit || localQueueWaitMs === 0) {
      localQueueRejected++;
      return Promise.resolve(null);
    }
    return new Promise((resolve, reject) => {
      const finish = admitted => {
        clearTimeout(timer);
        signal.removeEventListener('abort', cancel);
        const index = localQueue.indexOf(finish);
        if (index < 0) return;
        localQueue.splice(index, 1);
        if (signal.aborted) return reject(new Error('Request aborted'));
        if (!admitted) localQueueRejected++;
        resolve(admitted ? localPermit() : null);
      };
      const cancel = () => finish(false);
      const timer = setTimeout(() => finish(false), localQueueWaitMs);
      localQueue.push(finish);
      signal.addEventListener('abort', cancel, { once: true });
    });
  };
  // Breaker state is per provider: one failing provider must not make the other look down.
  const newCircuit = () => ({ openUntil: 0, probing: false, generation: 0, lastLocalFailure: 'upstream_error' });
  const circuits = { openrouter: newCircuit(), local: newCircuit(), cli: newCircuit() };
  const primaryCircuit = circuits[provider];
  let cliReportedModel = null, cliUsageMeasured = false;
  // The last rate-limit answer a provider gave; kept for exposure and the breaker decision.
  let lastRateLimit = null;
  // Breaker classification. Transport/timeout, 5xx and unusable 2xx answers mean the provider
  // itself is unhealthy; 401/402/403 are provider-wide configuration failures that keep their
  // existing cooldown; a 429 names its own wait. A request-level 4xx answer must never open the
  // breaker: the request was wrong, the provider was not.
  const failureClassOf = (status, timedOut) => {
    if (timedOut || !Number.isInteger(status)) return 'transient';
    if (status === 429) return 'rate_limit';
    if (status === 401 || status === 402 || status === 403) return 'provider';
    if (status >= 500 || status === 408) return 'transient';
    if (status >= 200 && status < 300) return 'transient';
    return 'request';
  };
  // Diagnostics are redacted once, at the sink: the configured credentials are the most
  // specific secrets and the generic rules catch any provider text that echoed a token.
  const secrets = [key, localKey, altKey].filter(secret => typeof secret === 'string' && secret.length >= 4);
  const diagnostics = text => emitDiagnostic(text, secrets);
  // Counters stay in memory and are written to a small file at most once a second, so the
  // dashboard can read routing load without talking to this process.
  const metrics = { startedAt: Date.now(), requests: 0, ok: 0, failed: 0, streams: 0, attempts: 0,
    retries: 0, externalAttempts: 0, aborted: 0, bytesIn: 0, bytesOut: 0, circuitOpens: 0,
    routes: { external: 0, plan: 0 },
    reasons: {}, latency: { external: [], plan: [] }, ttfb: [], last: null };
  // Bounded lifetime aggregates for validated, usage-reporting external JSON responses.
  const modelPerformance = Object.fromEntries(['local', 'openrouter', 'cli'].map(name =>
    [name, { samples: 0, outputTokens: 0, elapsedMs: 0 }]));
  // Observed provider usage only; response count lets clients distinguish missing usage from zero.
  const tokenFlow = Object.fromEntries(['local', 'openrouter', 'cli'].map(name =>
    [name, { responses: 0, inputSamples: 0, inputTokens: 0, outputSamples: 0, outputTokens: 0 }]));
  // Only completed request aggregates, never content or individual request records.
  const monitoringHistory = new Map();
  const currentMinute = () => Math.floor(now() / 60000);
  const pruneHistory = minute => {
    for (const key of monitoringHistory.keys()) {
      if (key < minute - 59 || key > minute) monitoringHistory.delete(key);
    }
  };
  const historySnapshot = () => {
    pruneHistory(currentMinute());
    return [...monitoringHistory.entries()].sort(([a], [b]) => a - b)
      .map(([minute, counts]) => ({ minute: new Date(minute * 60000).toISOString(), ...counts }));
  };
  const LATENCY_WINDOW = 512;
  const routingTiming = { queueWait: [], externalAttempt: [], retryWait: [], plan: [], total: [] };
  const attemptCounts = { external: 0, plan: 0 };
  const recordTiming = (phase, elapsed) => {
    const samples = routingTiming[phase];
    samples.push(Math.max(0, elapsed));
    if (samples.length > LATENCY_WINDOW) samples.shift();
  };
  const latencySummary = values => {
    if (!values.length) return { count: 0, p50: null, p95: null, max: null };
    const sorted = [...values].sort((a, b) => a - b);
    const at = fraction => Number(sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))].toFixed(2));
    return { count: sorted.length, p50: at(0.5), p95: at(0.95), max: at(1) };
  };
  const publicMetricModel = value => validOpenRouterModel(value) &&
    !/^(?:sk-|xai-|Bearer\s)/i.test(value) &&
    !/^[A-Za-z]:|:\/\/|\\/.test(value) &&
    ![nonce, key, localKey, altKey].some(secret => typeof secret === 'string' && secret.length > 0 && value.includes(secret)) ? value : null;
  const metricsSnapshot = () => ({
    configuration: {
      provider,
      model: provider === 'cli' ? cliReportedModel : publicMetricModel(provider === 'local' ? localModel : openrouterModel),
      modelRedacted: provider === 'cli' ? false : publicMetricModel(provider === 'local' ? localModel : openrouterModel) === null,
      ...(provider === 'cli' ? {cliKind, requestedModel: publicMetricModel(cliRequestedModel), usageMeasured: cliUsageMeasured} : {}),
      alternate: altExternalUrl === null ? null : {
        provider: altProvider, model: publicMetricModel(altModel), modelRedacted: publicMetricModel(altModel) === null,
      },
      planFallback: !noPlan,
    },
    startedAt: new Date(metrics.startedAt).toISOString(),
    uptimeMs: Date.now() - metrics.startedAt,
    requests: metrics.requests, ok: metrics.ok, failed: metrics.failed,
    streams: metrics.streams, attempts: metrics.attempts, retries: metrics.retries,
    routes: { ...metrics.routes }, externalAttempts: metrics.externalAttempts, aborted: metrics.aborted,
    reasons: { ...metrics.reasons },
    bytesIn: metrics.bytesIn, bytesOut: metrics.bytesOut, circuitOpens: metrics.circuitOpens,
    latencyMs: { external: latencySummary(metrics.latency.external), plan: latencySummary(metrics.latency.plan),
      ttfb: latencySummary(metrics.ttfb) },
    // Independent phase distributions, not additive percentiles. Total excludes inbound body parsing.
    routingTimingMs: Object.fromEntries(Object.entries(routingTiming).map(([name, values]) => [name, latencySummary(values)])),
    attemptCounts: { ...attemptCounts },
    modelPerformance: Object.fromEntries(Object.entries(modelPerformance).map(([name, value]) => [name, { ...value }])),
    tokenFlow: Object.fromEntries(Object.entries(tokenFlow).map(([name, value]) => [name, { ...value }])),
    monitoringHistory: historySnapshot(),
    localAdmission: { active: localActive, queued: localQueue.length, rejected: localQueueRejected,
      concurrency: localConcurrency, queueLimit: localQueueLimit, queueWaitMs: localQueueWaitMs },
    last: metrics.last,
    rateLimit: lastRateLimit ? { ...lastRateLimit } : null,
  });
  let flushTimer = null, lastFlushAt = 0, metricsDirty = false;
  const flushMetrics = () => {
    flushTimer = null;
    lastFlushAt = Date.now();
    if (!metricsDirty || !metricsPath) return;
    metricsDirty = false;
    try {
      // Replace atomically: a reader must never see a half-written snapshot.
      const temporary = `${metricsPath}.tmp`;
      // The minute history is live-session only; keep the existing disk snapshot compact.
      const { monitoringHistory: _liveHistory, tokenFlow: _liveTokenFlow, ...persisted } = metricsSnapshot();
      writeFileSync(temporary, JSON.stringify(persisted));
      renameSync(temporary, metricsPath);
    } catch {}
  };
  const scheduleMetricsFlush = () => {
    metricsDirty = true;
    if (!metricsPath || flushTimer) return;
    flushTimer = setTimeout(flushMetrics, Math.max(0, 1000 - (Date.now() - lastFlushAt)));
    flushTimer.unref?.();
  };
  if (metricsPath) process.once('exit', flushMetrics);
  let inflight = 0;
  let drainState = 'serving'; // 'serving' → 'draining' → 'closed', or back to 'serving' on a revert
  let handoverRun = null;
  // Drain in-flight turns, then close the listener so a fresh proxy can bind the same
  // port. If the cap expires with turns still active, revert instead of cutting them:
  // resuming service costs the operator a retry, while killing the process would drop
  // every CLI mid-turn and leave nothing listening until a replacement starts.
  const runHandover = async () => {
    if (drainState === 'closed') {
      return { status: 503, report: { type: 'error', error: { type: 'api_error', message: 'Proxy is handing over' } } };
    }
    drainState = 'draining';
    const started = Date.now();
    const deadline = started + handoverTimeoutMs;
    while (inflight > 0 && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
    if (inflight > 0) {
      drainState = 'serving';
      return { status: 409, report: { drained: false, reverted: true, inflight,
                                      waitedMs: Date.now() - started, forced: false,
                                      reason: 'drain_timeout' } };
    }
    // server.close() on Windows tears down sockets that are still streaming, which would
    // truncate a reply the client is reading right now; draining first prevents that.
    drainState = 'closed';
    server.close();
    const report = { drained: true, inflight: 0, waitedMs: Date.now() - started, forced: false };
    // The exit side effect belongs to the drain, not to a caller: concurrent POSTs join
    // one run and must not stack exit timers or callbacks.
    if (typeof onHandover === 'function') {
      onHandover();
      return { status: 200, report };
    }
    // Report first, leave second: a launcher that reads the 200 must not see a reset because
    // the process exited before its body was on the wire. The caller invokes `reported()`
    // once the handover response has flushed; the linger bounds a write that never does.
    let exited = false, reportFlushed = false;
    const exitLater = () => {
      if (exited) return;
      exited = true;
      setTimeout(() => process.exit(0), 50);
    };
    const linger = setTimeout(() => { if (!reportFlushed) exitLater(); }, 250);
    linger.unref?.();
    return { status: 200, report,
      reported: () => {
        if (reportFlushed) return;
        reportFlushed = true;
        clearTimeout(linger);
        exitLater();
      } };
  };
  const server = http.createServer(async (req, res) => {
    const reject = (status, message, headers) => {
      if (res.destroyed) return;
      if (res.headersSent) return res.destroy();
      res.writeHead(status, { 'content-type': 'application/json', ...headers });
      // Keep Anthropic's error vocabulary: a rate limit is not a generic API failure.
      const type = status === 429 ? 'rate_limit_error' : status === 529 ? 'overloaded_error' : 'api_error';
      // A message assembled from upstream text passes the same redaction as the logs.
      res.end(JSON.stringify({ type: 'error', error: { type, message: redactDiagnostic(String(message), secrets) } }));
    };
    if (!tokenMatches(req.headers['x-local-route-token'])) return reject(403, 'Local authorization required');
    if (req.method === 'HEAD' && req.url === '/api/hello') { res.writeHead(200); res.end(); return; }
    if (req.method === 'POST' && req.url === '/api/handover') {
      // Older launchers do not preserve isolated-session flags. Never let them replace
      // a no-Plan proxy with an ordinary proxy that could route to Plan.
      if (noPlan) return reject(409, 'Isolated session cannot be taken over; restart the session');
      // One drain run serves every concurrent request: a duplicate POST joins the same
      // wait instead of starting a second countdown. A run that hit the cap reverts, so
      // the next POST may try again while the proxy keeps serving.
      if (!handoverRun) handoverRun = runHandover();
      const outcome = await handoverRun.finally(() => { handoverRun = null; });
      if (res.destroyed) return;
      res.writeHead(outcome.status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      res.end(JSON.stringify(outcome.report));
      if (outcome.reported) {
        // Exit only once this very response (or its socket) has flushed; the drain keeps its
        // own bounded linger, so a write that never completes still cannot pin the process.
        const reported = () => {
          res.off('finish', reported);
          res.off('close', reported);
          outcome.reported();
        };
        res.once('finish', reported);
        res.once('close', reported);
        if (res.writableFinished || res.destroyed) reported();
      }
      return;
    }
    if (drainState !== 'serving') {
      // Tell the client to retry shortly: the replacement proxy is binding the same port,
      // or a drain is still waiting for an active turn to finish.
      res.writeHead(503, { 'content-type': 'application/json', 'retry-after': '1', 'cache-control': 'no-store' });
      return res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message: 'Proxy is handing over' } }));
    }
    if (req.method === 'GET' && /^\/api\/metrics(?:\?.*)?$/.test(req.url)) {
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      res.end(JSON.stringify(metricsSnapshot()));
      return;
    }
    if (req.method !== 'POST' || !/^\/v1\/messages(?:\/count_tokens)?(?:\?beta=true)?$/.test(req.url)) return reject(403, 'Endpoint blocked');
    const client = new AbortController();
    let recordRequest = () => {}, responseFailed = false;
    // A request owns one inflight slot until it is released. An early rejection (bad JSON,
    // oversized body) never reaches recordRequest, and a client that vanishes mid-body never
    // finishes its response, so release on 'close' too: a leaked slot makes every later
    // handover wait out its cap and revert instead of draining.
    let ownsTurn = false, released = false;
    const releaseTurn = () => {
      if (!ownsTurn || released) return;
      released = true;
      if (inflight > 0) inflight -= 1;
    };
    res.on('close', () => {
      if (!res.writableFinished) {
        recordRequest(true);
        client.abort('client_close');
      }
      releaseTurn();
    });
    req.on('aborted', () => client.abort('client_close'));
    // A failed external attempt can still decide the client-facing result: if the route
    // downstream fails too, the provider's own status and wait are more useful than a 502.
    let failureStatus = null, retryAfterHintMs = null;
    inflight += 1;
    ownsTurn = true;
    // A short correlation id ties every diagnostic line of this turn together. It is an
    // additive correlation key, not request data, so logs stay parseable and greppable.
    const traceId = randomBytes(4).toString('hex');
    const logEvent = fields => diagnostics(JSON.stringify({ traceId, ...fields }));
    try {
      const chunks = [];
      let size = 0;
      // A client that stalls mid-body must not hold its turn: Node's default requestTimeout
      // (300s) is longer than the handover cap, so a drain could never finish. The timer is
      // idle-based and re-armed per chunk, so a slow but live upload is never cut short.
      let idleTimer = null, bodyTimedOut = false;
      const clearIdle = () => { if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; } };
      const armIdle = () => {
        if (!requestIdleTimeoutMs) return;
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          idleTimer = null;
          bodyTimedOut = true;
          // A self-timeout is not peer interference, so it gets its own event and is
          // counted as an aborted request instead of vanishing with the socket.
          metrics.aborted += 1;
          metrics.failed += 1;
          scheduleMetricsFlush();
          logEvent({ event: 'client_body_timeout', provider, idle_ms: requestIdleTimeoutMs });
          client.abort('client_body_timeout');
          req.destroy();
        }, requestIdleTimeoutMs);
        idleTimer.unref?.();
      };
      armIdle();
      try {
        for await (const chunk of req) {
          armIdle();
          size += chunk.length;
          if (size > MAX_RESPONSE) return reject(413, 'Request too large');
          chunks.push(chunk);
        }
      } finally {
        clearIdle();
      }
      // A destroyed request may still end the iterator cleanly; never route a partial body.
      if (bodyTimedOut) return;
      const raw = chunks.length === 1 ? chunks[0] : Buffer.concat(chunks);
      let body;
      try { body = JSON.parse(raw.toString('utf8')); } catch { return reject(400, 'Invalid JSON'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return reject(403, 'Unsupported request fields');
      const model = body.model;
      // Only sessions with a route log record the shape; it is a measurement, not routing input.
      if (routeLogPath) {
        try {
          logEvent({ event: 'request_shape', family: modelFamily(model), tools: Array.isArray(body.tools) && body.tools.length > 0,
            ...requestShape(body), stream: body.stream === true });
        } catch {}
      }
      // Everything from here to the response is a routed request; count it once.
      const startedAt = performance.now();
      let firstByteMs = null;
      const noteFirstByte = () => { if (firstByteMs === null) firstByteMs = performance.now() - startedAt; };
      metrics.requests += 1;
      metrics.bytesIn += raw.length;
      if (body.stream === true) metrics.streams += 1;
      // Which provider this request is aimed at; the alias check below may switch it.
      let circuitState = primaryCircuit;
      const originalWrite = res.write.bind(res), originalEnd = res.end.bind(res);
      res.write = chunk => {
        if (chunk) metrics.bytesOut += Buffer.byteLength(chunk);
        return originalWrite(chunk);
      };
      res.end = (chunk, encoding, callback) => {
        if (chunk) metrics.bytesOut += Buffer.byteLength(chunk);
        return originalEnd(chunk, encoding, callback);
      };
      let routeKind = null, routeReason = null, recorded = false, performanceSample = null;
      const openPhases = new Set();
      const startPhase = phase => {
        const start = performance.now();
        const finish = (end = performance.now()) => {
          if (!openPhases.delete(finish)) return;
          recordTiming(phase, end - start);
        };
        openPhases.add(finish);
        return finish;
      };
      const noteRoute = (kind, reason) => {
        if (routeKind) return;
        routeKind = kind;
        routeReason = reason;
        metrics.routes[kind] += 1;
        metrics.reasons[reason] = (metrics.reasons[reason] || 0) + 1;
      };
      recordRequest = aborted => {
        if (recorded) return;
        recorded = true;
        releaseTurn();
        const completedAt = performance.now();
        // finish/close can run before an attempt's finally, especially on client cancellation.
        // Close active timers now; later finally calls become no-ops.
        for (const finish of openPhases) finish(completedAt);
        recordTiming('total', completedAt - startedAt);
        const elapsed = Number((completedAt - startedAt).toFixed(2));
        if (aborted) { metrics.aborted += 1; metrics.failed += 1; }
        else if (!responseFailed && res.statusCode < 400) metrics.ok += 1;
        else metrics.failed += 1;
        const minute = currentMinute();
        pruneHistory(minute);
        let history = monitoringHistory.get(minute);
        if (!history) {
          history = { completed: 0, ok: 0, failed: 0, aborted: 0, external: 0, plan: 0,
            latencyMsSum: 0, tokenSamples: 0, outputTokens: 0, tokenElapsedMs: 0 };
          monitoringHistory.set(minute, history);
        }
        history.completed += 1;
        if (aborted) history.aborted += 1;
        else if (!responseFailed && res.statusCode < 400) history.ok += 1;
        else history.failed += 1;
        if (routeKind) history[routeKind] += 1;
        history.latencyMsSum += elapsed;
        if (!aborted && !responseFailed && res.statusCode < 400 && routeKind === 'external' && performanceSample) {
          const flow = tokenFlow[performanceSample.provider];
          flow.responses += 1;
          if (performanceSample.inputTokens !== null) {
            flow.inputSamples += 1;
            flow.inputTokens += performanceSample.inputTokens;
          }
          if (performanceSample.outputTokens !== null) {
            flow.outputSamples += 1;
            flow.outputTokens += performanceSample.outputTokens;
          }
        }
        if (!aborted && !responseFailed && res.statusCode < 400 && routeKind === 'external' &&
            performanceSample && performanceSample.outputTokens !== null && elapsed > 0) {
          const aggregate = modelPerformance[performanceSample.provider];
          aggregate.samples += 1;
          aggregate.outputTokens += performanceSample.outputTokens;
          aggregate.elapsedMs += elapsed;
          history.tokenSamples += 1;
          history.outputTokens += performanceSample.outputTokens;
          history.tokenElapsedMs += elapsed;
        }
        if (routeKind) {
          const bucket = metrics.latency[routeKind];
          bucket.push(elapsed);
          if (bucket.length > LATENCY_WINDOW) bucket.shift();
        }
        if (firstByteMs !== null) {
          metrics.ttfb.push(Number(firstByteMs.toFixed(2)));
          if (metrics.ttfb.length > LATENCY_WINDOW) metrics.ttfb.shift();
        }
        metrics.last = { ts: new Date().toISOString(), traceId, route: routeKind || 'none', reason: routeReason,
          status: aborted ? 499 : res.statusCode, ms: elapsed,
          outcome: aborted ? 'aborted' : responseFailed || res.statusCode >= 400 ? 'failed' : 'ok',
          // Exposed so a rate-limit answer stays visible to the dashboard (and to the circuit
          // work that will consume it); null means no usable Retry-After was seen.
          retryAfterMs: retryAfterHintMs };
        scheduleMetricsFlush();
      };
      res.on('finish', () => recordRequest(false));
      const compatAlias = compatProfile && ['sonnet', 'opus', 'haiku', 'fable'].includes(model);
      const configuredAlias = provider === 'openrouter' && model === openrouterModel;
      const altRequested = altExternalUrl !== null && model === altAlias;
      if (!compatAlias && !configuredAlias && !altRequested && !isPro(model) && !isSonnet(model) && model !== DEEPSEEK) return reject(403, 'Model blocked');
      const oauthOK = /^Bearer\s+sk-ant-oat/.test(req.headers.authorization || '') && !req.headers['x-api-key'] &&
        (req.headers['anthropic-beta'] || '').includes('oauth-');
      const sendPro = async (reason = 'native_feature', detail) => {
        const betas = detail === 'unsupported_beta' ? unsupportedBetas(req.headers['anthropic-beta'], compatProfile) : [];
        const betaFeatures = betas.slice(0, 16).map(value =>
          /^[a-z][a-z0-9-]{0,63}-20\d{2}-[01]\d-[0-3]\d$/.test(value) &&
          !/(?:^|-)(?:sk|key|token|secret|password|bearer)(?:-|$)/.test(value) ? value : '[redacted]');
        if (noPlan) {
          if (reason === 'local_busy') {
            logEvent({ event: 'local_mode_rejected', provider: 'local', reason, code: 'local_busy' });
            return reject(503, '[local_busy] Local request queue is full or its wait expired. Retry shortly. Plan fallback disabled.',
              { 'retry-after': '1' });
          }
          let message = `${label}-only mode cannot route this request; Plan fallback disabled`;
          let code, retryAfterSeconds, previousErrorCode;
          if (reason === 'fallback' || reason === 'circuit_open') {
            const remaining = Math.max(0, Math.ceil((circuitState.openUntil - now()) / 1000));
            const recoveryInProgress = reason === 'circuit_open' && circuitState.probing && remaining === 0;
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
                [`${errorPrefix}_cooldown`, `${label} retry cooldown is active. Previous failure: ${(descriptions[circuitState.lastLocalFailure] || descriptions.upstream_error)[1]}`];
            if (reason === 'circuit_open') previousErrorCode = (descriptions[circuitState.lastLocalFailure] || descriptions.upstream_error)[0];
            if (!recoveryInProgress) retryAfterSeconds = remaining;
            const retryHint = recoveryInProgress ? '' : remaining > 0 ?
              ` Wait ${remaining} second${remaining === 1 ? '' : 's'} before a new request.` :
              ` Send a new request to check ${label} recovery.`;
            message = `[${code}] ${description}${retryHint} Plan fallback disabled.`;
          }
          logEvent({ event: `${errorPrefix}_mode_rejected`, provider, reason, ...(detail ? { detail } : {}),
            ...(code ? { code } : {}), ...(retryAfterSeconds !== undefined ? { retry_after_seconds: retryAfterSeconds } : {}),
            ...(previousErrorCode ? { previous_error_code: previousErrorCode } : {}),
            ...(betas.length ? { beta_features: betaFeatures, unsupported_beta_count: betas.length } : {}) });
          return reject(400, message);
        }
        if (!oauthOK) return reject(403, 'Saved subscription OAuth required');
        client.signal.throwIfAborted();
        // Native Plan has a stricter tool-id contract than third-party providers. Normalize
        // only unrepresentable ids here; the external route is never rewritten (it translates
        // its own ids), and conforming ids stay byte-identical for the prompt cache.
        const planInput = provider !== 'openrouter' ? withoutNullToolMetadata(body) : body;
        const normalizedIds = normalizeToolCallIds(planInput);
        if (normalizedIds.empty) return reject(400, 'Invalid tool call id');
        if (normalizedIds.changed) logEvent({ event: 'tool_ids_normalized', provider: 'plan', count: normalizedIds.count });
        // The model that actually serves a Plan request may differ from the client alias.
        const planModel = model === DEEPSEEK || altRequested
          || (configuredAlias && !isPro(model) && !isSonnet(model)) ? fallbackModel : model;
        noteRoute('plan', reason);
        logEvent({ event: 'route_selected', provider: 'plan', reason, ...(detail ? { detail } : {}),
          model: planModel,
          ...(betas.length ? { beta_features: betaFeatures, unsupported_beta_count: betas.length } : {}) });
        const headers = {};
        for (const [name, value] of Object.entries(req.headers))
          if (typeof value === 'string' && !['host', 'content-length', 'accept-encoding', 'connection',
            'transfer-encoding', 'x-local-route-token', 'x-api-key'].includes(name)) headers[name] = value;
        const planBody = normalizedIds.changed ? normalizedIds.body : planInput;
        const proBody = planModel === model && planInput === body && !normalizedIds.changed ? raw
          : JSON.stringify({ ...planBody, model: planModel });
        // Bound a stalled Plan upstream; user cancellation still flows through client.signal.
        const plan = new AbortController();
        const cancelPlan = () => plan.abort('client_close');
        client.signal.addEventListener('abort', cancelPlan, { once: true });
        let stall;
        const arm = ms => { clearTimeout(stall); stall = setTimeout(() => plan.abort('plan_timeout'), ms); };
        arm(planHeaderMs);
        const finishPlan = startPhase('plan');
        try {
        metrics.attempts += 1;
        attemptCounts.plan += 1;
        const upstream = await abortable(fetchImpl('https://api.anthropic.com' + req.url,
          { method: 'POST', headers, body: proBody, redirect: 'error', signal: plan.signal }), plan.signal);
        const responseHeaders = {};
        for (const [name, value] of upstream.headers)
          if (!['connection', 'transfer-encoding', 'content-length', 'content-encoding'].includes(name)) responseHeaders[name] = value;
        // Claude's retry contract expects seconds, even when an upstream supplies a date.
        const retryHint = responseHeaders['retry-after'];
        if (retryHint && !/^\d+(?:\.\d+)?$/.test(retryHint.trim())) {
          const retryAt = Date.parse(retryHint);
          if (Number.isFinite(retryAt)) responseHeaders['retry-after'] = String(Math.max(0, Math.ceil((retryAt - now()) / 1000)));
        }
        // An upstream error body is relayed only after the same redaction pass used for
        // logs; a successful response stays a stream so latency and framing are unchanged.
        // This is the one path where provider error text reaches the client verbatim.
        if (upstream.status >= 400 && upstream.body) {
          // The body is read with a hard cap: an error page cannot be buffered in full just
          // to relay a diagnostic excerpt, and the remainder is cancelled.
          arm(planIdleMs);
          const { text: errorBody, truncated } = await readBoundedBody(upstream.body, MAX_ERROR_BODY, plan.signal, () => arm(planIdleMs));
          const scrubbed = redactDiagnostic(errorBody, secrets);
          const redacted = scrubbed !== errorBody;
          if (truncated || redacted) {
            logEvent({ event: truncated ? 'upstream_error_truncated' : 'upstream_error_redacted',
              provider: 'plan', status: upstream.status,
              ...(truncated ? { truncated: true, bytes: MAX_ERROR_BODY } : {}),
              // The excerpt goes to the logging sink raw; the sink performs the redaction.
              ...(redacted ? { excerpt: errorBody.slice(0, 512) } : {}) });
          }
          res.writeHead(upstream.status, responseHeaders);
          res.end(scrubbed);
          return;
        }
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
              if (!res.write(value)) {
                try {
                  await once(res, 'drain', { signal: plan.signal });
                } catch (error) {
                  if (plan.signal.aborted) res.destroy();
                  throw error;
                }
              }
            }
          } finally {
            client.signal.removeEventListener('abort', cancel);
            reader.cancel().catch(() => {});
            reader.releaseLock();
          }
        }
        res.end();
        } finally {
          finishPlan();
          clearTimeout(stall);
          client.signal.removeEventListener('abort', cancelPlan);
        }
      };
      let nativeDetail = req.url.startsWith('/v1/messages/count_tokens') ? 'token_count' : compatProfile ?
        compatExclusion(body, req.headers['anthropic-beta']) : isPro(model) ? 'plan_model' : externalExclusion(body, req.headers['anthropic-beta'], localOnly);
      // Preserve MCP/tool-search request shapes without disabling client integrations.
      // Ordinary thinking stays disabled so Local signatures never contaminate Plan fallback.
      // Eligible requests already use the original body; only build a compatibility copy
      // when it could rescue a native-feature exclusion.
      const localBody = provider !== 'openrouter' && !localOnly && !altRequested &&
        nativeDetail && !['token_count', 'plan_model'].includes(nativeDetail) ? localInlineTools(body) : null;
      const localRequestCompat = localBody && provider !== 'openrouter' && !localOnly && !altRequested &&
        nativeDetail && !['token_count', 'plan_model'].includes(nativeDetail) &&
        !body.messages.some(message => Array.isArray(message.content) && message.content.some(block =>
          block?.type === 'tool_use' && Object.hasOwn(block, 'provider_specific_fields') && block.provider_specific_fields !== null)) &&
        !compatExclusion(localBody, req.headers['anthropic-beta']);
      if (localRequestCompat) nativeDetail = null;
      if (nativeDetail) return await sendPro('native_feature', nativeDetail);
      const forceJson = noPlan || localRequestCompat || provider === 'cli';
      // The alternate model id selects the second provider; anything else keeps the session's
      // primary provider, so a single-provider session behaves exactly as before.
      const target = altRequested
        ? { name: altProvider, model: altModel, url: altExternalUrl, key: altKey, openrouter: altProvider === 'openrouter' }
        : { name: provider, model: provider !== 'openrouter' ? localModel : openrouterModel, url: externalUrl,
            key: provider !== 'openrouter' ? localKey : key, openrouter: provider === 'openrouter' };
      circuitState = circuits[target.name];
      // Native-only requests never consume Local capacity. Queued turns recheck the breaker
      // after admission, then claim the single recovery probe without an intervening await.
      if (circuitState.openUntil && (now() < circuitState.openUntil || circuitState.probing)) return await sendPro('circuit_open');
      let releaseLocal = () => {};
      if (target.name === 'local') {
        const finishQueue = startPhase('queueWait');
        try { releaseLocal = await acquireLocal(client.signal); }
        finally { finishQueue(); }
      }
      if (!releaseLocal) {
        logEvent({ event: 'local_admission_rejected', provider: 'local', reason: 'local_busy' });
        return await sendPro('local_busy');
      }
      // An expired breaker admits exactly one half-open probe: the first request takes the
      // lease synchronously, everyone else falls back to Plan (or is told recovery is in
      // progress) until that probe settles. The generation snapshot fences late completions.
      if (circuitState.openUntil && (now() < circuitState.openUntil || circuitState.probing)) {
        releaseLocal();
        return await sendPro('circuit_open');
      }
      const probe = Boolean(circuitState.openUntil);
      if (probe) circuitState.probing = true;
      const claimedGeneration = circuitState.generation;
      let succeeded = false, localFailure = 'upstream_error', failureClass = null;
      try {
        const externalModel = target.model;
        const externalBody = { ...(localRequestCompat ? localBody : body), model: externalModel };
        if (Array.isArray(externalBody.messages) && hasNativeThinking(externalBody.messages)) {
          externalBody.messages = withoutNativeThinking(externalBody.messages);
        }
        delete externalBody.metadata; // client account/session identifiers stay off third-party providers
        if (forceJson) {
          externalBody.stream = false;
        }
        // Ordinary mode: external thinking blocks would pin the next turn to Plan (unsigned history), so never request them.
        if (!compatProfile) externalBody.thinking = { type: 'disabled' };
        if (externalBody.tools) externalBody.tools = externalBody.tools.map(tool => ({ ...tool, input_schema: compatibleSchema(tool.input_schema) }));
        if (target.openrouter) externalBody.provider = { allow_fallbacks: false };
        // Opt-in prefix-cache stabilization: only the very last system text block may lose a
        // trailing, unfenced token-count footer. User/assistant content is never touched.
        if (stabilizePromptCache && externalBody.system !== undefined) {
          externalBody.system = stabilizeSystem(externalBody.system);
        }
        // Serialize once: the retry loop reuses the same payload, and a large history would
        // otherwise be stringified up to four times.
        const externalPayload = JSON.stringify(externalBody);
        let routeLogged = false;
        const logReplacement = () => {
          if (routeLogged) return;
          routeLogged = true;
          logEvent({ event: 'route_selected', provider: target.name, reason: 'replacement', model: provider === 'cli' ? cliReportedModel : externalModel });
          noteRoute('external', 'replacement');
        };
        const headers = { 'content-type': 'application/json', 'anthropic-version': req.headers['anthropic-version'] || '2023-06-01' };
        if ((compatProfile || localRequestCompat) && req.headers['anthropic-beta']) headers['anthropic-beta'] = req.headers['anthropic-beta'];
        const credential = target.key;
        if (credential) headers.authorization = `Bearer ${credential}`;
        const attempts = noPlan || provider === 'cli' ? 1 : 4;
        // Permanent 4xx will not change on retry; request-level ones must not open the provider-wide circuit.
        let requestRejected = false;
        for (let attempt = 0; attempt < attempts; attempt++) {
          if (target.name === 'local') { failureStatus = null; retryAfterHintMs = null; }
          metrics.attempts += 1;
          if (attempt === 0) metrics.externalAttempts += 1;
          if (attempt > 0) metrics.retries += 1;
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
          let status, retryAfterMs = 0, congested = false;
          const finishAttempt = startPhase('externalAttempt');
          try {
            const operation = (async () => {
              attemptCounts.external += 1;
              const response = await fetchImpl(target.url, { method: 'POST', headers,
                body: externalPayload, redirect: 'error', signal: controller.signal });
              status = response.status;
              if (!response.ok) {
                // Only a failed answer carries a wait for the client; a Retry-After on a
                // successful response is not part of the result and must not taint metrics.
                const hint = parseRetryAfter(response.headers.get('retry-after'), now());
                if (hint !== null) retryAfterHintMs = hint;
                // Only a rate-limit answer changes the retry delay, as before; other failures
                // keep their fixed delay even when the provider sends a Retry-After.
                if (status === 429 || status === 503 || (target.name === 'local' && status === 529)) {
                  if (hint !== null) retryAfterMs = hint;
                  // Fence the exposed observation: a stale request must not overwrite the
                  // rate-limit state a newer generation already established.
                  if (claimedGeneration === circuitState.generation) {
                    lastRateLimit = { status, retryAfterMs: hint, at: new Date().toISOString() };
                  }
                }
                await response.body?.cancel();
                throw new Error('External request failed');
              }
              // Ordinary-mode streams are forwarded frame by frame; anything that needs a
              // whole body (compat/local modes, non-stream clients) keeps the buffered path.
              let streamStarted = false;
              const onFrame = text => {
                if (!res.headersSent) {
                  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
                  logReplacement();
                }
                return res.write(text);
              };
              // A slow client must slow the upstream read instead of buffering the reply here.
              const onDrain = () => new Promise(resolve => {
                const done = () => { res.off('drain', done); res.off('close', done); resolve(); };
                res.once('drain', done);
                res.once('close', done);
              });
              const streaming = streamPassthrough && !forceJson && body.stream === true;
              const validated = await bufferedResponse(response, externalModel, model, forceJson ? false : body.stream,
                controller.signal, () => { noteFirstByte(); if (!noPlan) arm(); }, target.openrouter,
                streaming ? onFrame : null, !compatProfile && !localOnly, streaming ? onDrain : null);
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
                  logEvent({ event: 'local_response_unsupported_fields', fields: extraFields.slice(0, 8).map(name =>
                    ['signature', 'thought_signature', 'extra_content', 'provider_specific_fields', 'index'].includes(name) ? name : '[redacted]') });
                  throw new Error('Unsupported Local response fields');
                }
              }
              // shortcut: textual match can false-positive on a tool_use input object containing this literal; refine if seen.
              if (!validated.streamed && !compatProfile && !localOnly && /"type"\s*:\s*"(?:redacted_thinking|thinking|thinking_delta|signature_delta)"/.test(validated.text))
                throw new Error('Thinking response in ordinary mode');
              // Only complete JSON replies carry directly verifiable usage here; SSE and
              // CLI bridge placeholders do not imply measured output tokens.
              let inputTokens = null, outputTokens = null;
              if (validated.contentType === 'application/json' &&
                  (target.name !== 'cli' || response.headers.get('x-cli-usage-measured') === 'true')) {
                const usage = JSON.parse(validated.text).usage;
                if (Number.isSafeInteger(usage?.input_tokens) && usage.input_tokens >= 0) inputTokens = usage.input_tokens;
                if (Number.isSafeInteger(usage?.output_tokens) && usage.output_tokens >= 0) outputTokens = usage.output_tokens;
              }
              const result = forceJson && body.stream === true ? jsonMessageStream(validated.text, !compatProfile) : validated;
              result.outputTokens = outputTokens;
              result.inputTokens = inputTokens;
              if (provider === 'cli') {
                result.cliReportedModel = response.headers.get('x-cli-reported-model');
                result.cliUsageMeasured = response.headers.get('x-cli-usage-measured') === 'true';
              }
              return result;
            })();
            const response = await abortable(operation, controller.signal);
            client.signal.throwIfAborted();
            succeeded = true;
            if (claimedGeneration === circuitState.generation) {
              if (circuitState.openUntil) logEvent({ event: 'circuit_recovered', provider: target.name });
              circuitState.openUntil = 0;
            }
            if (provider === 'cli') {
              cliReportedModel = publicMetricModel(response.cliReportedModel);
              cliUsageMeasured = response.cliUsageMeasured === true;
            }
            logReplacement();
            performanceSample = { provider: target.name, inputTokens: response.inputTokens ?? null,
              outputTokens: response.outputTokens ?? null };
            if (response.streamed) { res.end(); return; }
            res.writeHead(200, { 'content-type': response.contentType, 'cache-control': 'no-store' });
            res.end(response.text);
            return;
          } catch (error) {
            client.signal.throwIfAborted();
            // Headers on the wire means the client already has part of the answer: retrying
            // would duplicate it and falling back is impossible, so finish the stream.
            if (error?.streamed || res.headersSent) {
              // Headers are already on the wire, so retrying or falling back would corrupt
              // the response; report the failure inside the stream and stop here.
              responseFailed = true;
              logEvent({ event: 'stream_broken', provider: target.name, category: 'upstream' });
              try {
                res.write(`event: error\ndata: ${JSON.stringify({ type: 'error',
                  error: { type: 'api_error', message: 'upstream stream failed' } })}\n\n`);
              } catch {}
              res.end();
              return;
            }
            const validation = { 'Wrong response model': 'wrong_model', 'Invalid content block': 'invalid_content_block',
              'Incomplete message': 'incomplete_message', 'Invalid response': 'expected_sse', 'Unexpected SSE response': 'unexpected_sse',
              'Invalid content delta': 'invalid_delta', 'Truncated stream': 'truncated_stream',
              'Empty message response': 'empty_response', 'Invalid message response': 'invalid_message',
              'Invalid stream metadata': 'invalid_stream_metadata', 'Expected JSON Local response': 'expected_json', 'Unsupported Local response content': 'unsupported_local_content',
              'Local thinking response disabled': 'thinking_response', 'Thinking response in ordinary mode': 'thinking_response', 'Unsupported Local response fields': 'unsupported_content_fields' }[error?.message];
            if (noPlan) localFailure = controller.signal.reason === 'attempt_timeout' ? 'timeout' :
              status === 401 || status === 403 ? 'authentication_failed' : status === 402 ? 'payment_required' : validation === 'empty_response' ? 'empty_response' : 'upstream_error';
            if (Number.isInteger(status)) failureStatus = status;
            failureClass = failureClassOf(status, controller.signal.reason === 'attempt_timeout');
            congested = target.name === 'local' &&
              ([429, 503, 529].includes(status) || controller.signal.reason === 'attempt_timeout');
            logEvent({ event: 'external_attempt_failed', provider: target.name, attempt: attempt + 1,
              ...(Number.isInteger(status) ? { status } : {}), ...(validation ? { validation } : {}),
              ...(['missing_body', 'empty_body', 'empty_content', 'content_without_usable_output',
                'stream_without_usable_content'].includes(error?.validationDetail) ? { validation_detail: error.validationDetail } : {}),
              ...(emptyResponseDiagnostics.has(error) ? { response_diagnostic: emptyResponseDiagnostics.get(error) } : {}),
              ...(retryAfterMs ? { retry_after_ms: retryAfterMs } : {}),
              failure: failureClass,
              category: controller.signal.reason === 'attempt_timeout' ? 'timeout' : 'upstream' });
            controller.abort('attempt_failed');
            // This attempt is discarded, so its first byte is not the response's.
            firstByteMs = null;
            // 404 is request-level too: a renamed model must not open the provider circuit.
            if ([400, 404, 413, 422].includes(status) || error?.message === 'Thinking response in ordinary mode') requestRejected = true;
            if (requestRejected || [401, 402, 403, 404].includes(status)) break;
          } finally {
            finishAttempt();
            clearTimeout(timer);
            clearTimeout(ceiling);
            client.signal.removeEventListener('abort', cancel);
          }
          // Do not hammer a shared Local server before its recovery hint expires. Long
          // waits go directly to the existing breaker/fallback instead of freezing a turn.
          if (target.name === 'local' && retryAfterMs > retryMaxWaitMs) break;
          if (attempt + 1 < attempts) {
            const backoff = congested ? retryDelayMs * (2 ** attempt) * (1 + Math.random() * 0.25) : retryDelayMs;
            const waitMs = Math.min(Math.max(backoff, retryAfterMs), retryMaxWaitMs);
            const finishWait = startPhase('retryWait');
            try {
              await abortable(new Promise(resolve => {
                const done = () => { clearTimeout(timeout); client.signal.removeEventListener('abort', done); resolve(); };
                const timeout = setTimeout(done, waitMs);
                client.signal.addEventListener('abort', done, { once: true });
              }), client.signal);
            } finally { finishWait(); }
          }
        }
        if (!requestRejected && failureClass !== 'request' && claimedGeneration === circuitState.generation) {
          circuitState.generation++;
          if (noPlan) circuitState.lastLocalFailure = localFailure;
          // A 429 states its own recovery window: honor the parsed Retry-After (bounded)
          // instead of the generic cooldown, so the provider is not hammered while it asked
          // to be left alone. Without a hint, the operator's cooldown still applies.
          const cooldown = (failureClass === 'rate_limit' || (target.name === 'local' && [503, 529].includes(failureStatus))) && retryAfterHintMs !== null
            ? Math.min(RATE_LIMIT_COOLDOWN_MAX_MS, retryAfterHintMs)
            : cooldownMs;
          circuitState.openUntil = now() + cooldown;
          metrics.circuitOpens += 1;
          logEvent({ event: 'circuit_open', provider: target.name, failure: failureClass,
            ...(failureClass === 'rate_limit' || (target.name === 'local' && [503, 529].includes(failureStatus))
              ? { retry_after_ms: retryAfterHintMs, cooldown_ms: cooldown } : {}) });
        }
      } finally {
        if (probe) circuitState.probing = false;
        releaseLocal();
      }
      if (!succeeded) await sendPro('fallback', noPlan ? localFailure : undefined);
    } catch {
      if (!client.signal.aborted) {
        responseFailed = true;
        logEvent({ event: 'route_failed', category: 'upstream' });
        if (res.headersSent) {
          // Part of the answer is already on the wire: report the failure inside the stream.
          try {
            res.write(`event: error\ndata: ${JSON.stringify({ type: 'error',
              error: { type: 'api_error', message: 'upstream stream failed' } })}\n\n`);
          } catch {}
          res.end();
        } else if (failureStatus === 429 || failureStatus === 529) {
          // The provider already explained the refusal. A generic 502 would hide both the
          // cause and the wait the client has to observe, so surface its status and hint.
          const headers = retryAfterHintMs === null ? {}
            : { 'retry-after': String(Math.ceil(retryAfterHintMs / 1000)) };
          reject(failureStatus, failureStatus === 429 ? 'Upstream rate limited' : 'Upstream overloaded', headers);
        } else {
          reject(502, 'Upstream request failed');
        }
      }
    }
  });
  // A stalled header never reaches the request handler, so it is bounded by the server's
  // own header timeout; Node checks it on a coarse interval, which must shrink with the cap.
  if (requestIdleTimeoutMs > 0) {
    server.headersTimeout = requestIdleTimeoutMs;
    server.connectionsCheckingInterval = Math.min(requestIdleTimeoutMs, 1000);
  }
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const config = { key: process.env.OPENROUTER_API_KEY, openrouterModel: process.env.OPENROUTER_MODEL, nonce: process.env.LOCAL_ROUTE_TOKEN,
    provider: process.env.SONNET_PROVIDER || 'openrouter', localUrl: process.env.LOCAL_LLM_URL,
    localModel: process.env.LOCAL_LLM_MODEL, localKey: process.env.LOCAL_LLM_API_KEY,
    cliKind: process.env.CLI_KIND, cliRequestedModel: process.env.CLI_MODEL,
    attemptTimeoutMs: process.env.SONNET_PROVIDER === 'cli' ? (Number(process.env.CLI_TIMEOUT_S) || 60) * 1000 : undefined,
    localOnly: process.env.LOCAL_ROUTE_ONLY === '1', localCompat: process.env.LOCAL_ROUTE_COMPAT === '1', openrouterCompat: process.env.OPENROUTER_ROUTE_COMPAT === '1', localHttpUrl: process.env.LOCAL_LLM_HTTP_URL, localAllowRemote: process.env.LOCAL_LLM_ALLOW_REMOTE === 'true',
    metricsPath: process.env.ROUTE_METRICS_PATH,
    altProvider: process.env.ROUTE_ALT_PROVIDER || null, altModel: process.env.ROUTE_ALT_MODEL, altUrl: process.env.ROUTE_ALT_URL,
    altKey: process.env.ROUTE_ALT_KEY, altAllowRemote: process.env.ROUTE_ALT_ALLOW_REMOTE === 'true', altHttpUrl: process.env.ROUTE_ALT_HTTP_URL,
    altAlias: process.env.ROUTE_ALT_ALIAS };
  for (const name of ['OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'LOCAL_ROUTE_TOKEN', 'LOCAL_ROUTE_ONLY', 'LOCAL_ROUTE_COMPAT', 'OPENROUTER_ROUTE_COMPAT', 'SONNET_PROVIDER', 'LOCAL_LLM_URL',
    'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY', 'LOCAL_LLM_ALLOW_REMOTE', 'LOCAL_LLM_HTTP_URL', 'LOCAL_LLM_MODELS_URL', 'ROUTE_METRICS_PATH', 'ROUTE_ALT_PROVIDER', 'ROUTE_ALT_MODEL', 'ROUTE_ALT_URL',
    'ROUTE_ALT_KEY', 'ROUTE_ALT_ALLOW_REMOTE', 'ROUTE_ALT_HTTP_URL', 'ROUTE_ALT_ALIAS', 'CLI_KIND', 'CLI_MODEL', 'CLI_TIMEOUT_S']) delete process.env[name];
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
        emitDiagnostic(JSON.stringify({ event: config.openrouterCompat ? 'openrouter_parent_disconnected' : config.localOnly ? 'local_parent_disconnected' : 'parent_disconnected', provider: config.provider }));
        process.exit(0);
      };
      process.stdin.once('end', parentClosed);
      process.stdin.once('error', parentClosed);
      process.stdin.once('close', parentClosed);
      process.stdin.resume();
    }
    const portArg = process.argv.indexOf('--port');
    const requestedPort = portArg > 0 ? Number(process.argv[portArg + 1]) : 0;
    server.listen(Number.isInteger(requestedPort) ? requestedPort : 0, '127.0.0.1',
      () => process.stdout.write(JSON.stringify({ port: server.address().port }) + '\n'));
  }
}
