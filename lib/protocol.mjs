import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

export const MAX_BYTES = 12 * 1024 * 1024;
export const PROTOCOL_VERSIONS = ['2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'];
export function readLines(stream, onMessage, onError) {
  let buffer = Buffer.alloc(0);
  let failed = false;
  const stop = () => { failed = true; buffer = Buffer.alloc(0); stream.off('data', receive); };
  const fail = error => { if (failed) return; stop(); stream.destroy?.(); onError(error); };
  const receive = chunk => {
    if (failed) return;
    buffer = Buffer.concat([buffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
    while (true) {
      const i = buffer.indexOf(10);
      if (i < 0) break;
      const line = buffer.subarray(0, i); buffer = buffer.subarray(i + 1);
      if (line.length > MAX_BYTES) { fail(new Error('Protocol message exceeds size limit')); return; }
      if (!line.length) continue;
      try { onMessage(JSON.parse(line.toString('utf8'))); }
      catch (error) { fail(new Error('Invalid JSON protocol message: ' + error.message)); return; }
    }
    if (buffer.length > MAX_BYTES) fail(new Error('Protocol message exceeds size limit'));
  };
  stream.on('data', receive);
  return stop;
}
export function redact(text) {
  return String(text).replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, '$1[redacted]')
    .replace(/((?:token|secret|password|api[_-]?key)\s*[=:]\s*)[^\s,;]+/gi, '$1[redacted]');
}
export class MCPProcess {
  constructor({ command, args = [], cwd, env, timeoutMs = 30000 }) {
    this.pending = new Map(); this.timeoutMs = timeoutMs; this.closed = false;
    this.child = spawn(command, args, { cwd, env: { ...process.env, ...env }, windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], shell: false });
    this.errors = '';
    this.child.stderr.on('data', chunk => { this.errors = redact(this.errors + chunk).slice(-2000); });
    this.detach = readLines(this.child.stdout, message => {
      const slot = this.pending.get(message.id);
      if (slot) {
        this.pending.delete(message.id); clearTimeout(slot.timer); slot.cleanup();
        if (message.error) slot.reject(new Error(message.error.message || 'Backend request failed'));
        else slot.resolve(message.result);
      } else if (message.method && Object.hasOwn(message, 'id')) {
        // Root requests and sampling are deliberately unsupported by this router.
        this.write({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Client callback is unavailable' } });
      }
    }, error => this.fail(error));
    this.child.once('error', error => this.fail(error));
    this.child.once('exit', code => this.fail(new Error(`Provider exited (${code}). ${this.errors}`)));
    this.child.stdin.on('error', error => this.fail(error));
  }
  write(message) {
    const data = JSON.stringify(message);
    if (Buffer.byteLength(data) > MAX_BYTES) throw new Error('Protocol message exceeds size limit');
    if (this.closed) throw new Error('Provider connection is closed');
    this.child.stdin.write(data + '\n');
  }
  request(method, params = {}, { signal, timeoutMs = this.timeoutMs } = {}) {
    if (signal?.aborted) return Promise.reject(new Error('Request cancelled'));
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const cancel = message => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id); clearTimeout(timer); cleanup();
        try { this.write({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: id, reason: message } }); } catch {}
        reject(new Error(message + '; an in-flight action may already have completed and will not be replayed'));
      };
      const abort = () => cancel('Request cancelled');
      const cleanup = () => signal?.removeEventListener('abort', abort);
      const timer = setTimeout(() => cancel('Provider request timed out'), timeoutMs);
      this.pending.set(id, { resolve, reject, timer, cleanup });
      signal?.addEventListener('abort', abort, { once: true });
      try { this.write({ jsonrpc: '2.0', id, method, params }); }
      catch (error) { this.pending.delete(id); clearTimeout(timer); cleanup(); reject(error); }
    });
  }
  async initialize() {
    const info = await this.request('initialize', { protocolVersion: '2025-03-26', capabilities: {},
      clientInfo: { name: 'stilldesk', version: '0.1.0' } });
    this.write({ jsonrpc: '2.0', method: 'notifications/initialized' });
    return info;
  }
  fail(error) {
    if (this.failed) return;
    this.failed = true;
    this.closed = true;
    this.detach?.();
    this.child.stdin.end();
    if (this.child.exitCode === null && !this.child.killed) this.child.kill();
    for (const slot of this.pending.values()) { clearTimeout(slot.timer); slot.cleanup(); slot.reject(error); }
    this.pending.clear();
  }
  close() {
    if (this.child.exitCode === null && !this.child.killed) this.child.kill();
    this.fail(new Error('Provider closed'));
  }
}
