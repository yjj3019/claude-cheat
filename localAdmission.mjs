// SPDX-License-Identifier: MIT
// Same-host admission coordinator. A TCP connection owns exactly one permit;
// disconnecting releases it. No prompts, credentials, or endpoint URLs cross it.
import net from 'node:net';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const endpointKey = url => createHash('sha256').update(new URL(url).origin).digest('hex');
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const validState = data => isRecord(data) && data.event === 'state' &&
  Number.isInteger(data.concurrency) && data.concurrency >= 1 && data.concurrency <= 64 &&
  Number.isInteger(data.queueLimit) && data.queueLimit >= 0 && data.queueLimit <= 256 &&
  Number.isInteger(data.active) && data.active >= 0 && data.active <= data.concurrency &&
  Number.isInteger(data.queued) && data.queued >= 0 && data.queued <= data.queueLimit;
const stateFields = data => ({active:data.active, queued:data.queued, concurrency:data.concurrency, queueLimit:data.queueLimit});
export function coordinator(port = 18788) {
  const groups = new Map();
  let clients = 0, lastUsed = Date.now();
  const server = net.createServer(socket => {
    clients++; lastUsed = Date.now();
    let buffer = '', entry, group, timer, received = false;
    const send = data => { if (!socket.destroyed) socket.write(JSON.stringify(data) + '\n'); };
    const publish = () => {
      if (!group) return;
      const state = { active: group.active.size, queued: group.queue.length, concurrency: group.limit, queueLimit: group.queueLimit };
      for (const item of [...group.active, ...group.queue]) item.send({ event: 'state', ...state });
    };
    const grant = item => {
      clearTimeout(item.timer); group.active.add(item);
      item.send({ event: 'granted', protocol: 1 });
    };
    socket.setTimeout(5000, () => socket.destroy());
    socket.on('data', chunk => {
      if (received) return; // one request per connection, including status/rejections
      buffer += chunk.toString();
      if (buffer.length > 1024) return socket.destroy();
      if (!buffer.includes('\n')) return;
      received = true;
      let request;
      try { request = JSON.parse(buffer.split('\n')[0]); } catch { return socket.destroy(); }
      if (!isRecord(request) || ![undefined, 'acquire', 'status'].includes(request.action)) return socket.destroy();
      const { key, concurrency, queueLimit, waitMs } = request;
      if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key) || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 64 ||
          !Number.isInteger(queueLimit) || queueLimit < 0 || queueLimit > 256 || !Number.isInteger(waitMs) || waitMs < 0 || waitMs > 600000)
        return socket.destroy();
      group = groups.get(key);
      if (request.action === 'status') {
        send({ event: 'state', active: group?.active.size || 0, queued: group?.queue.length || 0,
          concurrency: group?.limit ?? concurrency, queueLimit: group?.queueLimit ?? queueLimit });
        group = null;
        return socket.end();
      }
      if (!group) { group = { active: new Set(), queue: [], limit: concurrency, queueLimit }; groups.set(key, group); }
      // Never silently give two sessions different interpretations of a shared cap.
      if (group.limit !== concurrency || group.queueLimit !== queueLimit) {
        send({ event: 'rejected', code: 'shared_limit_mismatch', protocol: 1 }); return socket.end();
      }
      entry = { send, socket }; socket.setTimeout(0); socket.setKeepAlive(true, 1000);
      if (group.active.size < group.limit) grant(entry);
      else if (group.queue.length >= group.queueLimit || waitMs === 0) {
        send({ event: 'rejected', code: 'local_busy', protocol: 1 }); return socket.end();
      } else {
        group.queue.push(entry);
        timer = entry.timer = setTimeout(() => { send({ event: 'rejected', code: 'local_busy', protocol: 1 }); socket.end(); }, waitMs);
        send({ event: 'queued', protocol: 1 });
      }
      publish();
    });
    socket.on('error', () => {});
    socket.on('close', () => {
      clients--; lastUsed = Date.now(); clearTimeout(timer);
      if (!group || !entry) return;
      group.active.delete(entry);
      group.queue = group.queue.filter(item => item !== entry);
      while (group.active.size < group.limit && group.queue.length) {
        const next = group.queue.shift();
        if (!next.socket.destroyed && !next.socket.writableEnded) grant(next);
      }
      publish();
      if (!group.active.size && !group.queue.length) for (const [key, value] of groups) if (value === group) groups.delete(key);
    });
  });
  server.maxConnections = 1024;
  const idle = setInterval(() => { if (!clients && Date.now() - lastUsed > 60000) server.close(); }, 5000);
  idle.unref(); server.on('close', () => clearInterval(idle));
  server.listen(port, '127.0.0.1');
  return server;
}

export function sharedStatus({url, concurrency, queueLimit, port = Number(process.env.ROUTE_ADMISSION_PORT || 18788)}) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1'); let buffer = '';
    const timer = setTimeout(() => { socket.destroy(); reject(new Error('Admission status timeout')); }, 500);
    socket.on('connect', () => socket.write(JSON.stringify({action:'status', key:endpointKey(url), concurrency, queueLimit, waitMs:0}) + '\n'));
    socket.on('error', reject);
    socket.on('close', () => { clearTimeout(timer); reject(new Error('Admission status disconnected')); });
    socket.on('data', chunk => {
      buffer += chunk.toString();
      if (buffer.length > 2048) { socket.destroy(); reject(new Error('Invalid admission status')); return; }
      if (!buffer.includes('\n')) return;
      try {
        const data = JSON.parse(buffer.split('\n')[0]);
        if (!validState(data)) throw new Error('Invalid admission status');
        resolve(stateFields(data));
      } catch (error) { reject(error); }
      socket.destroy();
    });
  });
}

function acquireOnce({ port, key, concurrency, queueLimit, waitMs, signal, onState, onLost }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(Object.assign(new Error('Admission cancelled'), { code: 'ABORT_ERR' }));
      return;
    }
    let granted = false, settled = false, released = false, buffer = '';
    const socket = net.connect(port, '127.0.0.1');
    const fail = error => { if (!settled) { settled = true; reject(error); } socket.destroy(); };
    const cancel = () => fail(Object.assign(new Error('Admission cancelled'), { code: 'ABORT_ERR' }));
    const timeout = setTimeout(() => fail(Object.assign(new Error('Admission unavailable'), { code: 'admission_unavailable' })), waitMs + 5000);
    const release = () => { released = true; socket.destroy(); };
    signal?.addEventListener('abort', cancel, { once: true });
    socket.on('connect', () => socket.write(JSON.stringify({ key, concurrency, queueLimit, waitMs }) + '\n'));
    socket.on('data', chunk => {
      buffer += chunk.toString();
      if (buffer.length > 8192) return fail(new Error('Invalid admission response'));
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'), line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
        let data;
        try { data = JSON.parse(line); } catch { return fail(new Error('Invalid admission response')); }
        if (!isRecord(data) || (data.event === 'state' ? !validState(data) :
            data.protocol !== 1 || !['queued', 'granted', 'rejected'].includes(data.event)) ||
            (data.event === 'rejected' && !['local_busy', 'shared_limit_mismatch'].includes(data.code)))
          return fail(new Error('Invalid admission response'));
        if (data.event === 'state') onState?.(stateFields(data));
        if (data.event === 'granted' && data.protocol === 1 && !settled) { settled = granted = true; clearTimeout(timeout); resolve(release); }
        if (data.event === 'rejected' && !settled) {
          settled = true;
          if (data.code === 'local_busy') resolve(null);
          else reject(Object.assign(new Error('Shared admission limits differ'), { code: 'shared_limit_mismatch' }));
          socket.destroy();
        }
      }
    });
    socket.on('error', fail);
    socket.on('close', () => {
      clearTimeout(timeout); signal?.removeEventListener('abort', cancel);
      if (granted && !released && !signal?.aborted) onLost?.();
      if (!settled) { settled = true; reject(Object.assign(new Error('Admission disconnected'), { code: 'admission_unavailable' })); }
    });
  });
}

export async function acquireShared(options) {
  const port = options.port ?? Number(process.env.ROUTE_ADMISSION_PORT || 18788);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid admission port');
  const args = { ...options, port, key: endpointKey(options.url) };
  try { return await acquireOnce(args); }
  catch (error) { if (error.code !== 'ECONNREFUSED' || options.signal?.aborted) throw error; }
  const child = spawn(process.execPath, [fileURLToPath(import.meta.url), '--serve', String(port)], {
    detached: true, windowsHide: true, stdio: 'ignore', env: Object.fromEntries(Object.entries(process.env).filter(([key]) => ['SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'HOME', 'USERPROFILE'].includes(key.toUpperCase()))),
  });
  child.on('error', () => {}); child.unref();
  for (let n = 0; n < 30; n++) {
    await new Promise(resolve => setTimeout(resolve, 50));
    options.signal?.throwIfAborted();
    try { return await acquireOnce(args); }
    catch (error) { if (error.code !== 'ECONNREFUSED') throw error; }
  }
  throw new Error('Admission coordinator did not start');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv[2] === '--serve') {
  coordinator(Number(process.argv[3])).on('error', () => { process.exitCode = 1; });
}
