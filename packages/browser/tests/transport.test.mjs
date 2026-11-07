import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { startBroker } from '../broker.mjs';
import { LabClient, runtimePaths, connect, messages, send, prepareUploads, callAction, MAX_UPLOAD, CHUNK_BYTES, delay, ROOT, ensureRuntime } from '../lib/transport.mjs';
import { extensionIdFromKey } from '../scripts/prepare.mjs';
import { nativeHostDirectory } from '../scripts/register-native-host.mjs';

async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'stilldesk-browser-test-'));
  const runtime = runtimePaths(path.join(dir, 'runtime'));
  const broker = await startBroker(runtime);
  const sockets = [];
  t.after(async () => { for (const socket of sockets) socket.destroy(); await broker.close(); await fs.rm(dir, { recursive: true, force: true }); });
  return { dir, runtime, broker, sockets };
}

test('a fake pipe server cannot receive the runtime secret or a client proof',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'stilldesk-auth-'));
  const runtime=runtimePaths(dir);
  await ensureRuntime(runtime);
  let received='';
  const sockets=[];
  const fake=net.createServer(socket=>{sockets.push(socket);socket.on('data',chunk=>received+=chunk);send(socket,{type:'challenge',nonce:'1'.repeat(64),proof:'0'.repeat(64)});});
  await new Promise(resolve=>fake.listen(runtime.pipe,resolve));
  t.after(async()=>{for(const socket of sockets)socket.destroy();await new Promise(resolve=>fake.close(resolve));await fs.rm(dir,{recursive:true,force:true});});
  await assert.rejects(connect(runtime,'client',{autoStart:false}),/Untrusted/);
  await delay(20);
  assert.equal(received,'');
});

test('shared existing runtime and credential explicit grants are replaced by exact private DACLs', {skip:process.platform!=='win32'},async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'stilldesk-shared-acl-'));
  const runtime=runtimePaths(dir);
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const old=crypto.randomBytes(32).toString('hex');
  await fs.writeFile(runtime.auth,JSON.stringify({version:1,token:old}));
  execFileSync('icacls.exe',[dir,'/grant','*S-1-1-0:(OI)(CI)F'],{windowsHide:true,stdio:'pipe'});
  execFileSync('icacls.exe',[runtime.auth,'/grant','*S-1-1-0:F'],{windowsHide:true,stdio:'pipe'});
  assert.equal(await ensureRuntime(runtime),old);
  const script=`$ErrorActionPreference='Stop';$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$result=@();foreach($p in @($env:TEST_ACL_DIR,$env:TEST_ACL_FILE)){if([System.IO.Directory]::Exists($p)){$info=New-Object System.IO.DirectoryInfo($p)}else{$info=New-Object System.IO.FileInfo($p)};$a=$info.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access);$ids=@($a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | Where-Object AccessControlType -eq 'Allow' | ForEach-Object {$_.IdentityReference.Value});$result+=@{protected=$a.AreAccessRulesProtected;otherAllow=@($ids | Where-Object {$_ -notin @($identity,'S-1-5-18')}).Count}};$result | ConvertTo-Json -Compress`;
  const result=JSON.parse(execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')],{encoding:'utf8',windowsHide:true,env:{...process.env,TEST_ACL_DIR:dir,TEST_ACL_FILE:runtime.auth}}));
  assert.equal(result.length,2);
  for(const item of result){assert.equal(item.protected,true);assert.equal(item.otherAllow,0);}
});

test('named pipe requires authentication and reports extension state', async t => {
  const { runtime, sockets } = await fixture(t);
  const unauth = net.createConnection(runtime.pipe); sockets.push(unauth);
  unauth.resume(); // Consume the server challenge before the rejected peer closes.
  await new Promise(resolve => unauth.once('connect', resolve));
  const closed = new Promise(resolve => unauth.once('close', resolve));
  send(unauth, { type: 'hello', role: 'client', token: '0'.repeat(64) }); await closed;
  const c = await LabClient.open({ runtime, autoStart: false }); sockets.push(c.socket);
  assert.equal((await c.request('get_status')).nativeConnected, false);
  await assert.rejects(c.request('click', { pageId: 1, uid: 'stale' }), /not connected/);
});

test('two clients serialize commands and preserve response ownership', async t => {
  const { runtime, sockets } = await fixture(t);
  const native = await connect(runtime, 'native', { autoStart: false }); sockets.push(native);
  let concurrent = 0, maximum = 0; const received = [];
  messages(native, message => {
    concurrent++; maximum = Math.max(maximum, concurrent); received.push(message.args.label);
    setTimeout(() => { concurrent--; send(native, { type: 'result', id: message.id, result: { label: message.args.label } }); }, 30);
  });
  const a = await LabClient.open({ runtime, autoStart: false }), b = await LabClient.open({ runtime, autoStart: false }); sockets.push(a.socket, b.socket);
  const result = await Promise.all([a.request('fill', { label: 'a' }), b.request('click', { label: 'b' }), a.request('click', { label: 'c' })]);
  assert.deepEqual(result.map(r => r.label), ['a', 'b', 'c']); assert.equal(maximum, 1); assert.deepEqual(received, ['a', 'b', 'c']);
});

test('cancelled queued actions are never dispatched and disconnection does not replay', async t => {
  const { runtime, sockets } = await fixture(t);
  const native = await connect(runtime, 'native', { autoStart: false }); sockets.push(native);
  const received = []; messages(native, message => received.push(message));
  const a = await LabClient.open({ runtime, autoStart: false }), b = await LabClient.open({ runtime, autoStart: false }); sockets.push(a.socket, b.socket);
  const first = a.request('click', { label: 'first' });
  const controller = new AbortController();
  const second = b.request('click', { label: 'second' }, { signal: controller.signal });
  await delay(30); controller.abort(); await assert.rejects(second, /cancelled/);
  await delay(20); assert.equal(received.length, 1);
  send(native, { type: 'result', id: received[0].id, result: 'first' }); assert.equal(await first, 'first');
  const lost = a.request('click', { label: 'lost' }); await delay(30); native.destroy();
  await assert.rejects(lost, /disconnected/);
  const native2 = await connect(runtime, 'native', { autoStart: false }); sockets.push(native2);
  const replay = []; messages(native2, message => replay.push(message)); await delay(60); assert.equal(replay.length, 0);
});

test('upload roots, traversal, symlink escape, size and regular-file boundaries', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'stilldesk-browser-upload-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const allowed = path.join(dir, 'allowed'), outside = path.join(dir, 'outside');
  await fs.mkdir(allowed); await fs.mkdir(outside);
  const good = path.join(allowed, '中文.txt'), secret = path.join(outside, 'secret.txt');
  await fs.writeFile(good, '上传测试'); await fs.writeFile(secret, 'outside');
  await assert.rejects(prepareUploads([good]), /No upload roots/);
  await assert.rejects(prepareUploads(['relative.txt'], [allowed]), /absolute/);
  await assert.rejects(prepareUploads([path.join(allowed, '..', 'outside', 'secret.txt')], [allowed]), /outside/);
  await assert.rejects(prepareUploads([allowed], [allowed]), /regular files/);
  const link = path.join(allowed, 'escape'); await fs.symlink(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(prepareUploads([path.join(link, 'secret.txt')], [allowed]), /outside/);
  const large = path.join(allowed, 'large.bin'); const handle = await fs.open(large, 'w'); await handle.truncate(MAX_UPLOAD + 1); await handle.close();
  await assert.rejects(prepareUploads([large], [allowed]), /32 MiB/);
  const [file] = await prepareUploads([good], [allowed]); assert.equal(file.metadata.name, '中文.txt'); assert.equal(file.metadata.sha256, crypto.createHash('sha256').update('上传测试').digest('hex'));
});

test('uploads are chunked below native limit, include hashes, support empty files and abort after failure', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'stilldesk-browser-chunks-')); t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const name = path.join(dir, 'binary.bin'), empty = path.join(dir, 'empty.txt');
  const bytes = crypto.randomBytes(CHUNK_BYTES * 6 + 13); await fs.writeFile(name, bytes); await fs.writeFile(empty, '');
  const calls = [];
  const fake = { closed: false, request: async (action, args) => { calls.push({ action, args }); if (action === 'upload_begin') return { uploadId: 'test' }; if (action === 'upload_commit') return { assigned: true }; return {}; } };
  assert.deepEqual(await callAction(fake, 'upload_file', { pageId: 3, uid: 'uid', filePaths: [name, empty] }, { allowedRoots: [dir] }), { assigned: true });
  const chunks = calls.filter(c => c.action === 'upload_chunk'); assert.equal(chunks.length, 7);
  assert.deepEqual(Buffer.concat(chunks.map(c => Buffer.from(c.args.base64, 'base64'))), bytes);
  for (const call of calls) assert.ok(Buffer.byteLength(JSON.stringify(call)) < 1024 * 1024);
  assert.equal(calls[0].args.files[1].size, 0); assert.equal(calls[0].args.files[0].sha256, crypto.createHash('sha256').update(bytes).digest('hex'));
  fake.request = async (action, args) => { calls.push({ action, args }); if (action === 'upload_begin') return { uploadId: 'failed' }; if (action === 'upload_chunk') throw new Error('stale document'); return {}; };
  await assert.rejects(callAction(fake, 'upload_file', { pageId: 3, uid: 'uid', filePaths: [name] }, { allowedRoots: [dir] }), /stale document/);
  assert.equal(calls.at(-1).action, 'upload_abort');
});

test('MCP initializes, lists explicit-page tools, rejects invalid args and reports errors', async t => {
  const child = spawn(process.execPath, [path.join(ROOT, 'mcp-server.mjs')], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  t.after(() => child.kill());
  let buffer = ''; const waiters = new Map();
  child.stdout.on('data', data => { buffer += data; let end; while ((end = buffer.indexOf('\n')) >= 0) { const msg = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1); waiters.get(msg.id)?.(msg); waiters.delete(msg.id); } });
  const rpc = (id, method, params) => new Promise(resolve => { waiters.set(id, resolve); child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });
  assert.equal((await rpc(1, 'initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'test', version: '1' } })).result.serverInfo.name, 'stilldesk-browser');
  const tools = (await rpc(2, 'tools/list', {})).result.tools;
  assert.equal(tools.length, 24); assert.ok(!tools.some(t => /eval/.test(t.name)));
  assert.ok(tools.find(t => t.name === 'upload_file').inputSchema.required.includes('pageId'));
  assert.equal((await rpc(3, 'tools/call', { name: 'click', arguments: { uid: 'missing-page' } })).error.code, -32602);
  assert.equal((await rpc(4, 'tools/call', { name: 'fill', arguments: { pageId: 1, uid: 'x', value: 'text', extra: true } })).error.code, -32602);
  assert.equal((await rpc(5, 'tools/call', { name: 'new_page', arguments: { url: 'file:///C:/test' } })).result.isError, true);
  child.stdin.end(); await new Promise(resolve => child.once('exit', resolve));
});

test('real native host validates origin and transports fragmented uint32LE JSON frames', async t => {
  const { runtime, sockets } = await fixture(t);
  const manifest = JSON.parse(await fs.readFile(path.join(ROOT, 'extension', 'manifest.json'), 'utf8'));
  const origin = `chrome-extension://${extensionIdFromKey(manifest.key)}/`;
  const denied = spawn(process.execPath, [path.join(ROOT, 'native-host.mjs'), 'chrome-extension://' + 'a'.repeat(32) + '/'], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let diagnostic = ''; denied.stderr.on('data', data => diagnostic += data);
  assert.equal(await new Promise(resolve => denied.once('exit', resolve)), 1); assert.match(diagnostic, /not authorized/);
  const host = spawn(process.execPath, [path.join(ROOT, 'native-host.mjs'), origin, '--parent-window=0'],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, env: { ...process.env, STILLDESK_BROWSER_RUNTIME: runtime.dir } });
  t.after(() => host.kill());
  let nativeBytes = Buffer.alloc(0), commandResolve;
  const command = new Promise(resolve => { commandResolve = resolve; });
  host.stdout.on('data', data => {
    nativeBytes = Buffer.concat([nativeBytes, data]);
    if (nativeBytes.length < 4 || nativeBytes.length < nativeBytes.readUInt32LE() + 4) return;
    commandResolve(JSON.parse(nativeBytes.subarray(4, nativeBytes.readUInt32LE() + 4).toString('utf8')));
  });
  const c = await LabClient.open({ runtime, autoStart: false }); sockets.push(c.socket);
  const deadline = Date.now() + 5000;
  while (!(await c.request('get_status')).nativeConnected) { assert.ok(Date.now() < deadline, 'native host connected'); await delay(30); }
  const result = c.request('take_snapshot', { pageId: 4 });
  const message = await command; assert.equal(message.action, 'take_snapshot'); assert.equal(message.args.pageId, 4);
  const json = Buffer.from(JSON.stringify({ type: 'result', id: message.id, result: { text: '中文 response' } }));
  const header = Buffer.alloc(4); header.writeUInt32LE(json.length);
  host.stdin.write(header.subarray(0, 2)); await delay(5); host.stdin.write(Buffer.concat([header.subarray(2), json.subarray(0, 3)])); await delay(5); host.stdin.write(json.subarray(3));
  assert.deepEqual(await result, { text: '中文 response' });
  const exited = new Promise(resolve => host.once('exit', resolve)); host.stdin.end(); assert.equal(await exited, 0);
});

test('native host locations cover Chrome, Edge and Chromium without embedding user paths', () => {
  assert.match(nativeHostDirectory('edge','linux','/test-user'), /microsoft-edge/);
  assert.match(nativeHostDirectory('chrome','darwin','/test-user'), /Google[\\/]Chrome/);
  assert.match(nativeHostDirectory('chromium','linux','/test-user'), /chromium/);
  assert.throws(()=>nativeHostDirectory('unknown','linux','/test-user'),/browser must/);
});

test('download metadata never grants arbitrary local file reads', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(),'stilldesk-download-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const downloads=path.join(dir,'downloads'),outside=path.join(dir,'outside');
  await fs.mkdir(downloads);await fs.mkdir(outside);
  const good=path.join(downloads,'good.bin'),secret=path.join(outside,'secret.bin');
  await fs.writeFile(good,'downloaded');await fs.writeFile(secret,'private');
  const fake={request:async()=>({complete:true,filename:secret})};
  const unchecked=await callAction(fake,'get_download',{downloadId:1});
  assert.equal(unchecked.fileHashVerified,false);
  await assert.rejects(callAction(fake,'get_download',{downloadId:1,expectedSha256:'0'.repeat(64)}),/allow-download-root/);
  await assert.rejects(callAction(fake,'get_download',{downloadId:1},{downloadRoots:[downloads]}),/outside/);
  fake.request=async()=>({complete:true,filename:good});
  const verified=await callAction(fake,'get_download',{downloadId:1},{downloadRoots:[downloads]});
  assert.equal(verified.sha256,crypto.createHash('sha256').update('downloaded').digest('hex'));
  await assert.rejects(callAction(fake,'get_download',{downloadId:1,expectedSha256:'0'.repeat(64)},{downloadRoots:[downloads]}),/SHA-256/);
});
