import { spawn } from 'node:child_process';
import { access, constants, realpath, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

export class PlatformError extends Error {
  constructor(code, message, details) { super(message); this.code = code; this.details = details; }
}
export const execution = Object.freeze({ mode: 'native-service', foregroundRequired: false,
  pointerMovement: false, globalKeyboardInjection: false, uiFallback: false });
export function text(value, name, { required = false, maximum = 8192, pattern } = {}) {
  if (value === undefined && !required) return undefined;
  if (typeof value !== 'string' || (required && !value.trim()) || value.length > maximum || /[\0\r\n]/.test(value) || (pattern && !pattern.test(value)))
    throw new PlatformError('invalid_argument', `${name} is invalid`);
  return value;
}
export function paragraph(value, name, maximum = 20000) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || value.includes('\0')) throw new PlatformError('invalid_argument', `${name} is invalid`);
  return value;
}
export function integer(value, name, low, high, defaultValue) {
  if (value === undefined && defaultValue !== undefined) return defaultValue;
  if (!Number.isSafeInteger(value) || value < low || value > high) throw new PlatformError('invalid_argument', `${name} must be an integer from ${low} to ${high}`);
  return value;
}
export function isoTime(value, name) {
  if (value === undefined || value === '') return undefined;
  text(value, name);
  if (!/(?:Z|[+-]\d\d:\d\d)$/.test(value) || Number.isNaN(Date.parse(value))) throw new PlatformError('invalid_time', `${name} needs an ISO 8601 timestamp with timezone`);
  return value;
}
export function object(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new PlatformError('invalid_argument', 'Arguments must be an object');
  for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new PlatformError('invalid_argument', `Unknown argument: ${key}`);
}
export function writePlan(action, args, preview) {
  if (args.dry_run !== undefined && typeof args.dry_run !== 'boolean') throw new PlatformError('invalid_argument', 'dry_run must be boolean');
  if (args.authorized !== undefined && typeof args.authorized !== 'boolean') throw new PlatformError('invalid_argument', 'authorized must be boolean');
  if (args.dry_run !== false) return { ok: true, dry_run: true, action, plan: preview, executed: false, execution };
  if (args.authorized !== true) throw new PlatformError('authorization_required', 'Execution requires the user’s specific authorization and authorized=true');
  text(args.request_id, 'request_id', { required: true, maximum: 80, pattern: /^[A-Za-z0-9_-]{8,80}$/ });
  return undefined;
}
export async function findExecutable(kind, env = process.env) {
  const override = env[kind === 'lark' ? 'STILLDESK_LARK_CLI' : 'STILLDESK_TMEET_CLI'];
  const suffix = process.platform === 'win32' ? '.exe' : '';
  const name = kind === 'lark' ? 'lark-cli' : 'tmeet';
  const candidates = [];
  if (override) {
    if (!path.isAbsolute(override)) throw new PlatformError('invalid_configuration', `${name} executable override must be absolute`);
    candidates.push(override);
  } else {
    for (const folder of (env.PATH || env.Path || '').split(path.delimiter).filter(Boolean)) candidates.push(path.join(folder, name + suffix));
    const npmRoots = [path.join(env.APPDATA || path.join(os.homedir(), '.config'), 'npm', 'node_modules'), path.join(process.cwd(), 'node_modules')];
    for (const root of npmRoots) {
      if (kind === 'lark') candidates.push(path.join(root, '@larksuite', 'cli', 'bin', name + suffix));
      else candidates.push(path.join(root, '@tencentcloud', 'tmeet', 'dist', `tmeet-${({ win32: 'Windows', darwin: 'Darwin', linux: 'Linux' })[process.platform]}-${process.arch === 'x64' ? 'x86_64' : 'arm64'}${suffix}`));
    }
  }
  for (const candidate of candidates) {
    // Never feed a .cmd/.bat script or caller arguments through a shell.
    if (/\.(?:bat|cmd|ps1)$/i.test(candidate)) continue;
    try {
      const resolved = await realpath(candidate);
      if (!(await stat(resolved)).isFile()) continue;
      await access(resolved, constants.X_OK);
      return resolved;
    } catch {}
  }
  throw new PlatformError('not_configured', `Install the official ${name} executable or set its STILLDESK_*_CLI path. No login or UI fallback was started.`);
}
export function runNative(executable, args, { timeoutMs = 45000, maxBytes = 8 * 1024 * 1024, cwd, env = process.env, signal } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new PlatformError('cancelled', 'Native request was cancelled before starting'));
    const child = spawn(executable, args, { cwd, env, shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const output = [], diagnostic = [];
    let bytes = 0, done = false;
    const finish = (error, result) => { if (done) return; done = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); error ? reject(error) : resolve(result); };
    const abort = () => { child.kill(); finish(new PlatformError('cancelled', 'Native request was cancelled; an in-flight write may have completed and was not replayed')); };
    signal?.addEventListener('abort', abort, { once: true });
    const collect = target => chunk => {
      bytes += chunk.length;
      if (bytes > maxBytes) { child.kill(); finish(new PlatformError('response_too_large', 'The native response exceeded the limit; request a smaller page')); }
      else target.push(chunk);
    };
    const timer = setTimeout(() => { child.kill(); finish(new PlatformError('upstream_timeout', 'Native command timed out. A write may have completed; do not retry with a new request ID.')); }, timeoutMs);
    child.stdout.on('data', collect(output)); child.stderr.on('data', collect(diagnostic));
    child.once('error', error => finish(new PlatformError('process_start_failed', `Could not start the configured native executable (${error.code || 'unknown'})`)));
    child.once('close', code => finish(null, { code, stdout: Buffer.concat(output).toString('utf8'), stderr: Buffer.concat(diagnostic).toString('utf8') }));
  });
}
export function parseNative(result) {
  if (result.code !== 0) {
    const diagnostic = (result.stdout + result.stderr).toLowerCase();
    const code = /auth|login|登录|token.*expir/.test(diagnostic) ? 'authentication_required' : /permission|forbidden|权限/.test(diagnostic) ? 'permission_denied' : 'upstream_error';
    throw new PlatformError(code, `The official native CLI failed (exit ${result.code}); no empty-result or completion assumption was made.`);
  }
  try {
    const value = JSON.parse(result.stdout.trim());
    if (value && typeof value === 'object' && !Array.isArray(value) && value.code !== undefined && ![0, '0', null].includes(value.code))
      throw new PlatformError('upstream_error', 'The service returned a nonzero result code');
    return value;
  } catch (error) {
    if (error instanceof PlatformError) throw error;
    throw new PlatformError('unexpected_response', 'The official native CLI did not return the expected JSON response');
  }
}
