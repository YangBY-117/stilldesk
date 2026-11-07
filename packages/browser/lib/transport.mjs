import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const MAX_MESSAGE = 12 * 1024 * 1024;
export const MAX_UPLOAD = 32 * 1024 * 1024;
export const CHUNK_BYTES = 192 * 1024;
export const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
export const proof = (token, text) => crypto.createHmac('sha256', token).update(text).digest('hex');
export function proofMatches(actual, expected) {
  return typeof actual==='string' && /^[a-f0-9]{64}$/.test(actual) && crypto.timingSafeEqual(Buffer.from(actual,'hex'),Buffer.from(expected,'hex'));
}

export function dataDirectory() { return path.resolve(process.env.STILLDESK_HOME || path.join(os.homedir(), '.stilldesk'), 'browser'); }

export function runtimePaths(directory = process.env.STILLDESK_BROWSER_RUNTIME || path.join(dataDirectory(), 'runtime')) {
  const dir = path.resolve(directory);
  const suffix = crypto.createHash('sha256').update(dir.toLowerCase()).digest('hex').slice(0, 24);
  return { dir, auth: path.join(dir, 'auth.json'), status: path.join(dir, 'status.json'),
    pipe: process.platform === 'win32' ? `\\\\.\\pipe\\stilldesk-browser-${suffix}` : path.join(os.tmpdir(), `stilldesk-browser-${suffix}.sock`) };
}

function protectWindowsPath(target, directory) {
  // Replace the DACL in one SetAccessControl call. /grant:r alone would preserve broad
  // pre-existing explicit ACEs, and resetting then tightening could expose a
  // previously existing credential during that interval.
  const script = `$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system=New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$isDirectory=$env:STILLDESK_ACL_KIND -eq 'directory'
if($isDirectory){$acl=New-Object System.Security.AccessControl.DirectorySecurity;$inherit=[System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit';$info=New-Object System.IO.DirectoryInfo($env:STILLDESK_ACL_PATH)}
else{$acl=New-Object System.Security.AccessControl.FileSecurity;$inherit=[System.Security.AccessControl.InheritanceFlags]::None;$info=New-Object System.IO.FileInfo($env:STILLDESK_ACL_PATH)}
$acl.SetAccessRuleProtection($true,$false)
$acl.SetOwner($identity)
foreach($sid in @($identity,$system)){
 $rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,[System.Security.AccessControl.FileSystemRights]::FullControl,$inherit,[System.Security.AccessControl.PropagationFlags]::None,[System.Security.AccessControl.AccessControlType]::Allow)
 [void]$acl.AddAccessRule($rule)
}
$info.SetAccessControl($acl)
$secured=$info.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
if(-not $secured.AreAccessRulesProtected){throw 'DACL inheritance was not protected'}
foreach($rule in $secured.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])){
 if($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and $rule.IdentityReference.Value -notin @($identity.Value,$system.Value)){throw 'An unexpected allow rule remains'}
}`;
  try {
    execFileSync('powershell.exe', ['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')],
      {windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,STILLDESK_ACL_PATH:target,STILLDESK_ACL_KIND:directory?'directory':'file'}});
  } catch { throw new Error('Could not apply an exact private Windows DACL; credentials were not read'); }
}

export async function ensureRuntime(runtime = runtimePaths()) {
  await fsp.mkdir(runtime.dir, { recursive: true, mode: 0o700 });
  if ((await fsp.lstat(runtime.dir)).isSymbolicLink()) throw new Error('Runtime directory cannot be a symlink');
  if (process.platform === 'win32') {
    protectWindowsPath(runtime.dir,true);
  } else await fsp.chmod(runtime.dir, 0o700);
  // Publish a complete credential file atomically. Concurrent Chrome/MCP starts
  // must not read a newly created file before its first write has completed.
  if (!fs.existsSync(runtime.auth)) {
    const temporary = path.join(runtime.dir, `.auth-${crypto.randomUUID()}.tmp`);
    try {
      await fsp.writeFile(temporary, JSON.stringify({ version: 1, token: crypto.randomBytes(32).toString('hex') }), { flag: 'wx', mode: 0o600 });
      try { await fsp.link(temporary, runtime.auth); }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
    } finally { await fsp.rm(temporary, { force: true }); }
  }
  const authStat = await fsp.lstat(runtime.auth);
  if (!authStat.isFile() || authStat.isSymbolicLink()) throw new Error('Credential file is not a regular private file');
  if(process.platform==='win32')protectWindowsPath(runtime.auth,false);
  else await fsp.chmod(runtime.auth,0o600);
  const auth = JSON.parse(await fsp.readFile(runtime.auth, 'utf8'));
  if (auth.version !== 1 || !/^[a-f0-9]{64}$/.test(auth.token)) throw new Error('Invalid lab credentials');
  return auth.token;
}

export function messages(stream, onMessage, onError = () => {}) {
  let buffer = Buffer.alloc(0);
  const fail = error => { onError(error); stream.destroy?.(); };
  const receive = chunk => {
    buffer = Buffer.concat([buffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
    while (true) {
      const end = buffer.indexOf(10);
      if (end < 0) break;
      if (end > MAX_MESSAGE) return fail(new Error('IPC message too large'));
      const line = buffer.subarray(0, end); buffer = buffer.subarray(end + 1);
      if (!line.length) continue;
      try { onMessage(JSON.parse(line.toString('utf8'))); } catch (error) { return fail(error); }
    }
    if (buffer.length > MAX_MESSAGE) fail(new Error('IPC message too large'));
  };
  stream.on('data', receive);
  return () => stream.off('data', receive);
}

export function send(stream, message) {
  const json = JSON.stringify(message);
  if (Buffer.byteLength(json) > MAX_MESSAGE) throw new Error('IPC message too large');
  if (stream.destroyed || !stream.writable) throw new Error('Connection closed');
  stream.write(json + '\n');
}

export async function connect(runtime = runtimePaths(), role = 'client', { autoStart = true } = {}) {
  const token = await ensureRuntime(runtime);
  let spawned = false;
  const deadline = Date.now() + 8000;
  while (true) {
    try {
      return await new Promise((resolve, reject) => {
        const socket = net.createConnection(runtime.pipe);
        let done = false, stop, serverNonce, clientNonce;
        const finish = (error, value) => {
          if (done) return; done = true; clearTimeout(timer); stop?.(); socket.off('error', errorHandler);
          if (error) { socket.destroy(); reject(error); } else resolve(value);
        };
        const timer = setTimeout(() => finish(new Error('Broker connection timed out')), 2000);
        const errorHandler = error => finish(error);
        socket.on('error', errorHandler);
        socket.once('connect', () => {
          stop = messages(socket, msg => {
            if(!serverNonce){
              if(msg.type!=='challenge'||typeof msg.nonce!=='string'||!/^\w{64}$/.test(msg.nonce)||!proofMatches(msg.proof,proof(token,`server:${msg.nonce}`))) return finish(new Error('Untrusted or incompatible broker; no credential was sent'));
              serverNonce=msg.nonce;clientNonce=crypto.randomBytes(32).toString('hex');
              send(socket,{type:'hello',role,nonce:clientNonce,proof:proof(token,`client:${serverNonce}:${clientNonce}:${role}`)});
              return;
            }
            if(msg.type!=='ready'||!proofMatches(msg.proof,proof(token,`ready:${serverNonce}:${clientNonce}:${role}`))) return finish(new Error(msg.error||'Broker authentication refused'));
            finish(null,socket);
          }, finish);
        });
      });
    } catch (error) {
      if (!autoStart || Date.now() >= deadline || !['ENOENT', 'ECONNREFUSED', 'EPIPE'].includes(error.code)) throw error;
      if (!spawned) {
        spawned = true;
        const child = spawn(process.execPath, [path.join(ROOT, 'broker.mjs'), '--runtime', runtime.dir], { detached: true, stdio: 'ignore', windowsHide: true });
        child.on('error', () => {}); child.unref();
      }
      await delay(100);
    }
  }
}

export class LabClient {
  constructor(socket, { timeoutMs = 45000 } = {}) {
    this.socket = socket; this.timeoutMs = timeoutMs; this.pending = new Map(); this.closed = false;
    this.stop = messages(socket, message => {
      const item = this.pending.get(message.id);
      if (!item || message.type !== 'result') return;
      clearTimeout(item.timer); this.pending.delete(message.id);
      message.error ? item.reject(Object.assign(new Error(typeof message.error === 'object' ? message.error.message : String(message.error)),
        { code: message.error?.code || 'bridge_error' })) : item.resolve(message.result);
    }, error => this.fail(error));
    socket.on('error', error => this.fail(error));
    socket.on('close', () => this.fail(new Error('Broker disconnected. An action may have completed; inspect before repeating.')));
  }
  static async open(options = {}) {
    return new LabClient(await connect(options.runtime ?? runtimePaths(), 'client', options), options);
  }
  fail(error) {
    this.closed = true;
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(error); }
    this.pending.clear();
  }
  request(action, args = {}, { timeoutMs = this.timeoutMs, signal } = {}) {
    if (this.closed) return Promise.reject(new Error('Client is closed'));
    if (signal?.aborted) return Promise.reject(new Error('Request cancelled'));
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const cleanup = () => signal?.removeEventListener('abort', abort);
      const cancel = reason => {
        if (!this.pending.has(id)) return;
        try { send(this.socket, { type: 'cancel', id }); } catch {}
        const item = this.pending.get(id); clearTimeout(item.timer); this.pending.delete(id); cleanup(); reject(new Error(reason));
      };
      const abort = () => cancel('Request cancelled. An in-flight action may have completed; inspect before repeating.');
      const timer = setTimeout(() => cancel('Request timed out. An action may have completed; inspect before repeating.'), timeoutMs + 1000);
      this.pending.set(id, { resolve: value => { cleanup(); resolve(value); }, reject: error => { cleanup(); reject(error); }, timer });
      signal?.addEventListener('abort', abort, { once: true });
      try { send(this.socket, { type: 'request', id, action, args, timeoutMs }); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); cleanup(); reject(error); }
    });
  }
  close() { this.socket.destroy(); this.fail(new Error('Client closed')); }
}

const mimeTypes = { '.txt': 'text/plain', '.csv': 'text/csv', '.json': 'application/json', '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.zip': 'application/zip', '.html': 'text/html' };
const within = (root, file) => { const rel = path.relative(root, file); return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)); };

export async function prepareUploads(filePaths, allowedRoots = []) {
  if (!Array.isArray(filePaths) || filePaths.length < 1 || filePaths.length > 16) throw new Error('Provide 1 to 16 files');
  if (!allowedRoots.length) throw new Error('No upload roots are authorized; use --allow-upload-root PATH');
  const roots = await Promise.all(allowedRoots.map(async root => {
    if (!path.isAbsolute(root)) throw new Error('Upload roots must be absolute');
    const real = await fsp.realpath(root);
    if (!(await fsp.stat(real)).isDirectory()) throw new Error('An upload root must be a directory');
    return real;
  }));
  const files = []; let total = 0;
  for (const name of filePaths) {
    if (typeof name !== 'string' || !path.isAbsolute(name)) throw new Error('Upload file paths must be absolute');
    const real = await fsp.realpath(name);
    if (!roots.some(root => within(root, real))) throw new Error('Upload path is outside the authorized roots');
    const handle = await fsp.open(real, 'r');
    try {
      const stat = await handle.stat();
      if (!stat.isFile()) throw new Error('Only regular files can be uploaded');
      if (total + stat.size > MAX_UPLOAD) throw new Error('Upload exceeds the 32 MiB limit');
      const storage = Buffer.alloc(stat.size + 1);
      let count = 0;
      while (count < storage.length) {
        const { bytesRead } = await handle.read(storage, count, storage.length - count, null);
        if (!bytesRead) break;
        count += bytesRead;
      }
      const bytes = storage.subarray(0, count);
      total += bytes.length;
      if (total > MAX_UPLOAD || bytes.length !== stat.size) throw new Error('File changed while being read or exceeds the upload limit');
      const after = await handle.stat();
      if (after.mtimeMs !== stat.mtimeMs || after.size !== stat.size || after.ino !== stat.ino) throw new Error('File changed while being read');
      const nameNow = await fsp.stat(real);
      const realNow = await fsp.realpath(real);
      if (nameNow.ino !== stat.ino || nameNow.dev !== stat.dev || !roots.some(root => within(root, realNow))) throw new Error('File path changed while being read');
      files.push({ bytes, metadata: { name: path.basename(real), type: mimeTypes[path.extname(real).toLowerCase()] || 'application/octet-stream', size: bytes.length,
        lastModified: Math.trunc(stat.mtimeMs), sha256: crypto.createHash('sha256').update(bytes).digest('hex') } });
    } finally { await handle.close(); }
  }
  return files;
}

export async function callAction(client, action, args = {}, { allowedRoots = [], downloadRoots = [], signal } = {}) {
  if (action === 'download_file' || action === 'get_download') {
    if(args.expectedSha256!==undefined && !downloadRoots.length) throw Object.assign(new Error('Configure --allow-download-root before requesting expectedSha256 verification'),{code:'download_verification_not_configured'});
    const result = await client.request(action, args, { signal });
    if (!result.complete || !result.filename) return result;
    if (!downloadRoots.length) return { ...result, fileHashVerified: false, verification: 'No --allow-download-root was configured; local bytes were not opened' };
    const [file] = await prepareUploads([result.filename], downloadRoots);
    const expectedMatches = args.expectedSha256 === undefined || args.expectedSha256 === file.metadata.sha256;
    if (!expectedMatches) throw new Error('Downloaded file SHA-256 differs from expectedSha256');
    return { ...result, fileHashVerified: true, sha256: file.metadata.sha256, bytes: file.metadata.size };
  }
  if (action !== 'upload_file') return client.request(action, args, { signal });
  if (!Number.isInteger(args.pageId) || typeof args.uid !== 'string') throw new Error('upload_file requires pageId and uid');
  const files = await prepareUploads(args.filePaths, allowedRoots);
  let uploadId, committed = false;
  try {
    ({ uploadId } = await client.request('upload_begin', { pageId: args.pageId, uid: args.uid, files: files.map(file => file.metadata) }, { signal }));
    if (typeof uploadId !== 'string') throw new Error('Extension returned an invalid upload transaction');
    for (let fileIndex = 0; fileIndex < files.length; fileIndex++) {
      const { bytes } = files[fileIndex];
      for (let offset = 0; offset < bytes.length; offset += CHUNK_BYTES) {
        await client.request('upload_chunk', { pageId: args.pageId, uploadId, fileIndex, offset, base64: bytes.subarray(offset, offset + CHUNK_BYTES).toString('base64') }, { signal });
      }
    }
    const result = await client.request('upload_commit', { pageId: args.pageId, uploadId }, { signal });
    committed = true; return result;
  } finally {
    if (uploadId && !committed && !client.closed) {
      try { await client.request('upload_abort', { pageId: args.pageId, uploadId }, { timeoutMs: 3000 }); } catch {}
    }
  }
}

export function parseArgs(argv) {
  const options = { allowedRoots: [], downloadRoots: [], positionals: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--runtime') options.runtime = runtimePaths(argv[++i]);
    else if (arg === '--allow-upload-root') options.allowedRoots.push(argv[++i]);
    else if (arg === '--allow-download-root') options.downloadRoots.push(argv[++i]);
    else if (arg === '--timeout') options.timeoutMs = Number(argv[++i]);
    else options.positionals.push(arg);
  }
  if (options.timeoutMs !== undefined && (!Number.isInteger(options.timeoutMs) || options.timeoutMs < 1000 || options.timeoutMs > 120000)) throw new Error('Timeout must be 1000 to 120000 milliseconds');
  return options;
}
