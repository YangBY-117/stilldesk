import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { prepare } from './prepare.mjs';
import { applyRegistration, rollbackRegistration } from './register-native-host.mjs';
import { LabClient, runtimePaths } from '../lib/transport.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const browserFamily = process.env.STILLDESK_BROWSER_TEST_FAMILY || 'chrome';
if (!['chrome','edge','chromium'].includes(browserFamily)) throw new Error('Unsupported test browser family');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function desktopState() {
  if (process.platform !== 'win32') return { available:false };
  const source = `$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class StillDeskObservation { [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; } [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p); }'; $p=New-Object StillDeskObservation+POINT; [void][StillDeskObservation]::GetCursorPos([ref]$p); @{available=$true;foreground=[StillDeskObservation]::GetForegroundWindow().ToInt64().ToString();x=$p.X;y=$p.Y} | ConvertTo-Json -Compress`;
  return JSON.parse(execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(source,'utf16le').toString('base64')],{encoding:'utf8',windowsHide:true}));
}
const evidence = { startedAt: new Date().toISOString(), liveBrowserTest: true,
  scope: 'isolated Chromium profile and local HTTP fixture only', tests: [], remoteDebugging: false,
  automationLibraries: [], dailyChromeProfileTested: false,
  browserFamily,
  trialConfiguration: 'Official CfT --disable-field-trial-config: compiled feature defaults; no claim that the NetworkService sandbox is enabled' };
const checks = (name, passed, detail) => {
  evidence.tests.push({ name, passed: !!passed, detail });
  process.stdout.write(`${passed ? 'PASS' : 'FAIL'} ${name}\n`);
  if (!passed) throw new Error(`${name}: ${JSON.stringify(detail)}`);
};
function multipart(bytes, contentType) {
  const match = /boundary=(?:"([^"]+)"|([^;]+))/.exec(contentType);
  if (!match) throw new Error('No multipart boundary');
  const boundary = Buffer.from('--' + (match[1] || match[2]));
  const result = [];
  let cursor = bytes.indexOf(boundary);
  while (cursor >= 0) {
    const next = bytes.indexOf(boundary, cursor + boundary.length);
    if (next < 0) break;
    const headerEnd = bytes.indexOf('\r\n\r\n', cursor + boundary.length);
    if (headerEnd < 0 || headerEnd > next) break;
    const headers = bytes.subarray(cursor + boundary.length + 2, headerEnd).toString('utf8');
    const filename = /filename="([^"]*)"/.exec(headers)?.[1];
    if (filename !== undefined) {
      const content = bytes.subarray(headerEnd + 4, next - 2);
      result.push({ name: filename, size: content.length, sha256: hash(content),
        type: /content-type:\s*([^\r\n]+)/i.exec(headers)?.[1] || '' });
    }
    cursor = next;
  }
  return result;
}
let framePort;
const receipts = [];
const requests = [];
const pageHtml = () => `<!doctype html><html><meta charset="utf-8"><title>StillDesk Browser local test</title>
<body><h1>Local no-debugger fixture</h1><label>Name <input id="name"></label>
<label>Controlled setter <input id="controlled"></label><output id="state"></output>
<label><input id="check" type="checkbox">Remember</label>
<select id="select"><option value="a">Alpha</option><option value="b">Beta</option></select>
<button id="ordinary">Ordinary click</button><button id="trusted">Trusted only</button><output id="events"></output>
<button id="hover-test">Hover event</button><input id="key-test"><output id="input-events"></output>
<button id="disable-file">Disable target file</button><button id="adopt">Move node to frame</button><input id="adopt-target">
<a id="background-link" href="/linked" target="_blank">Background link</a>
<div id="drag-source" draggable="true">Drag me</div><div id="drop-target" role="region" aria-label="Drop target">Drop here</div><output id="drop-result"></output>
<label>Normal file<input id="normal" type="file"></label>
<input id="hidden" type="file" style="display:none">
<label>Multiple files<input id="multi" type="file" multiple></label>
<div id="shadow-host"></div>
<iframe title="same origin" src="/frame?kind=same"></iframe>
<iframe title="cross origin" src="http://127.0.0.1:${framePort}/frame?kind=cross"></iframe>
<script>
let ordinary = 0, denied = 0;
document.querySelector('#ordinary').addEventListener('click', () => { ordinary++; document.querySelector('#events').textContent = 'ordinary=' + ordinary; });
document.querySelector('#trusted').addEventListener('click', e => { if (!e.isTrusted) denied++; document.querySelector('#events').textContent = 'trusted-denied=' + denied; });
document.querySelector('#hover-test').addEventListener('pointerover', e => { document.querySelector('#input-events').textContent = 'pointer-event=' + (e instanceof PointerEvent) + ':' + e.pointerType; });
document.querySelector('#key-test').addEventListener('keydown', e => { document.querySelector('#input-events').textContent = 'key=' + e.key + ':ctrl=' + e.ctrlKey + ':trusted=' + e.isTrusted; });
document.querySelector('#disable-file').addEventListener('click', () => { document.querySelector('#normal').disabled = true; });
document.querySelector('#adopt').addEventListener('click', () => { const d=document.querySelector('iframe').contentDocument; d.body.append(d.adoptNode(document.querySelector('#adopt-target'))); });
document.querySelector('#drop-target').addEventListener('drop', () => document.querySelector('#drop-result').textContent='dropped-ok');
const controlled = document.querySelector('#controlled');
const native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
Object.defineProperty(controlled, 'value', { get() { return native.get.call(this); }, set(v) { native.set.call(this, 'INTERCEPTED:' + v); } });
controlled.addEventListener('input', () => { document.querySelector('#state').textContent = controlled.value; });
const shadow = document.querySelector('#shadow-host').attachShadow({mode:'closed'});
shadow.innerHTML = '<label>Shadow file<input id="shadow-file" type="file"></label><button id="shadow-button">Shadow click</button>';
function listen(input) { input.addEventListener('change', async e => { const f = new FormData(); for (const file of input.files) f.append('files', file, file.name); await fetch('/upload?case=' + input.id + '&trusted=' + e.isTrusted, {method:'POST', body:f}); }); }
document.querySelectorAll('input[type=file]').forEach(listen); shadow.querySelectorAll('input[type=file]').forEach(listen);
</script></body></html>`;
const frameHtml = kind => `<!doctype html><meta charset="utf-8"><title>${kind} frame</title>
<label>${kind} frame field<input id="${kind}-field"></label><input id="${kind}-file" type="file">
<script>document.querySelector('input[type=file]').addEventListener('change',async e => { const f=new FormData(); for(const v of e.target.files) f.append('files',v,v.name); await fetch('/upload?case=${kind}-file&trusted='+e.isTrusted,{method:'POST',body:f}); });</script>`;
const insecureHtml = `<!doctype html><meta charset="utf-8"><title>Ordinary HTTP fixture</title>
<input id="insecure-field"><input id="insecure-file" type="file">
<script>document.querySelector('input[type=file]').addEventListener('change',async e=>{const f=new FormData();for(const v of e.target.files)f.append('files',v,v.name);await fetch('/upload?case=insecure-file&trusted='+e.isTrusted,{method:'POST',body:f});});</script>`;
function fixture(req, res) {
  requests.push({ method: req.method, url: req.url });
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/download') {
    res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="stilldesk-fixture.bin"'});
    res.end(downloadBytes); return;
  }
  if (req.method === 'POST' && u.pathname === '/upload') {
    const chunks = [];
    let size = 0;
    req.on('data', chunk => { size += chunk.length; if (size > 40 * 1024 * 1024) req.destroy(); else chunks.push(chunk); });
    req.on('end', () => {
      try {
        const files = multipart(Buffer.concat(chunks), req.headers['content-type'] || '');
        receipts.push({ case: u.searchParams.get('case'), trusted: u.searchParams.get('trusted'), files });
        res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ received: true }));
      } catch (e) { res.writeHead(400); res.end(e.message); }
    });
    return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(u.pathname === '/frame' ? frameHtml(u.searchParams.get('kind') || 'same') : u.pathname === '/plain' ? insecureHtml : pageHtml());
}
function client(child) {
  let serial = 0, buffer = '';
  const pending = new Map();
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    buffer += chunk;
    let p;
    while ((p = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, p); buffer = buffer.slice(p + 1);
      if (!line.trim()) continue;
      let message;
      try { message = JSON.parse(line); } catch { continue; }
      const slot = pending.get(message.id);
      if (slot) { clearTimeout(slot.timer); pending.delete(message.id); message.error ? slot.reject(new Error(message.error.message)) : slot.resolve(message.result); }
    }
  });
  child.on('exit', code => { for (const slot of pending.values()) { clearTimeout(slot.timer); slot.reject(new Error('MCP exited: ' + code)); } pending.clear(); });
  return {
    request(method, params) {
      return new Promise((resolve, reject) => {
        const id = ++serial;
        const timer = setTimeout(() => { pending.delete(id); reject(new Error('MCP timeout: ' + method)); }, 45000);
        pending.set(id, { resolve, reject, timer });
        child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      });
    },
    notify(method, params) { child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n'); },
  };
}
async function eventually(fn, timeout = 15000) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeout) {
    try { const r = await fn(); if (r) return r; } catch (e) { last = e; }
    await delay(200);
  }
  throw last || new Error('Timed out awaiting browser state');
}
let browserProcess, mcpProcess, testRuntime;
let run, desktopBefore;
const originalHome = process.env.STILLDESK_HOME;
const downloadBytes = randomBytes(512 * 1024 + 7);
const registrations = [];
let browserErrors = '', mcpErrors = '';
const server = http.createServer(fixture);
const frameServer = http.createServer(fixture);
try {
  await mkdir(path.join(ROOT,'evidence'),{recursive:true});
  await new Promise(resolve => frameServer.listen(0, '127.0.0.1', resolve));
  framePort = frameServer.address().port;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  run = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-browser-e2e-'));
  process.env.STILLDESK_HOME = path.join(run, 'state');
  testRuntime = runtimePaths(path.join(run, 'runtime'));
  const built = await prepare({runtime:testRuntime.dir});
  const filesDir = path.join(run, 'files');
  await mkdir(filesDir);
  const downloadsDir = path.join(run,'downloads');
  await mkdir(downloadsDir);
  await mkdir(path.join(run,'profile','Default'),{recursive:true});
  await writeFile(path.join(run,'profile','Default','Preferences'),JSON.stringify({download:{default_directory:downloadsDir,prompt_for_download:false}}));
  const files = [
    { name: '中文小文件.txt', bytes: Buffer.from('StillDesk Browser 无调试上传\n') },
    { name: 'binary-2MiB.bin', bytes: randomBytes(2 * 1024 * 1024 + 37) },
    { name: 'empty.txt', bytes: Buffer.alloc(0) },
  ];
  for (const file of files) { file.path = path.join(filesDir, file.name); file.sha256 = hash(file.bytes); await writeFile(file.path, file.bytes); }
  const chromium = process.env.STILLDESK_BROWSER_TEST_BROWSER;
  if (!chromium) throw new Error('Set STILLDESK_BROWSER_TEST_BROWSER to a compatible Chrome for Testing or Edge executable');
  const browserArgs = ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--no-proxy-server', '--disable-background-networking', '--enable-logging=stderr',
    '--disable-field-trial-config',
    '--host-resolver-rules=MAP stilldesk-browser.test 127.0.0.1',
    `--user-data-dir=${path.join(run, 'profile')}`, `--disable-extensions-except=${built.extensionDirectory}`,
    `--load-extension=${built.extensionDirectory}`, origin];
  if (browserArgs.some(a => /remote-debugging|enable-automation/.test(a))) throw new Error('Debugging/automation flags forbidden in this test');
  evidence.browserExecutable = chromium; evidence.browserArguments = browserArgs;
  evidence.browserExecutableSha256 = hash(await readFile(chromium));
  evidence.extensionId = built.extensionId; evidence.artifactDirectory = run;
  // This is a temporary test-host registration; restore its prior value in finally.
  for (const browser of (browserFamily === 'edge' ? ['edge'] : ['chromium', 'chrome'])) registrations.push({ browser, receipt: await applyRegistration({ browser }) });
  desktopBefore = desktopState();
  browserProcess = spawn(chromium, browserArgs, { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, STILLDESK_BROWSER_RUNTIME: testRuntime.dir } });
  browserProcess.stderr.on('data', c => { browserErrors = (browserErrors + c).slice(-8000); });
  browserProcess.on('error',error=>{browserErrors+='\n'+error.message;});
  mcpProcess = spawn(process.execPath, [path.join(ROOT, 'mcp-server.mjs'), '--runtime', testRuntime.dir, '--allow-upload-root', filesDir, '--allow-download-root', downloadsDir],
    { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  mcpProcess.stderr.on('data', c => { mcpErrors = (mcpErrors + c).slice(-4000); });
  mcpProcess.on('error',error=>{mcpErrors+='\n'+error.message;});
  const rpc = client(mcpProcess);
  await rpc.request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'stilldesk-browser-e2e', version: '0.2.0' } });
  rpc.notify('notifications/initialized');
  const tools = (await rpc.request('tools/list')).tools;
  checks('MCP protocol and fixed tool discovery', tools.some(t => t.name === 'upload_file') && !tools.some(t => /eval|debugger/.test(t.name)), tools.map(t => t.name));
  async function tool(name, args) {
    const r = await rpc.request('tools/call', { name, arguments: args });
    const content = r.content?.find(c => c.type === 'text')?.text || '';
    if (r.isError) throw new Error(content);
    try { return JSON.parse(content); } catch { return content; }
  }
  const capabilities = await eventually(() => tool('get_capabilities', {}), 25000);
  const extensionManifest = JSON.parse(await readFile(path.join(ROOT, 'extension', 'manifest.json'), 'utf8'));
  checks('No remote debugging or extension debugger permission', capabilities.remoteDebugging === false &&
    capabilities.debuggerPermission === false && !extensionManifest.permissions.includes('debugger'), capabilities);
  const tabs = await eventually(async () => {
    const current = await tool('list_pages', {});
    const list = Array.isArray(current) ? current : current.pages;
    return list?.some(t => t.url?.startsWith(origin)) ? current : null;
  }, 25000);
  const tabArray = Array.isArray(tabs) ? tabs : tabs.pages;
  if (!tabArray) throw new Error('Unrecognized tab list: ' + JSON.stringify(tabs));
  const tab = tabArray.find(t => t.url?.startsWith(origin));
  checks('Native Messaging connected to isolated browser', !!tab, { tabs: tabArray, browserErrors, mcpErrors });
  const pageId = tab.pageId;
  let snap = await eventually(async () => {
    const s = await tool('take_snapshot', { pageId });
    evidence.lastSnapshot = s;
    const mainError = s.frames?.find(f => f.frameId === 0)?.error;
    if (mainError) {
      if (!evidence.errorScreenshot) {
        const shot = await rpc.request('tools/call', { name: 'take_screenshot', arguments: { pageId } });
        const img = shot.content?.find(c => c.type === 'image');
        if (img) { evidence.errorScreenshot = path.join(ROOT, 'evidence', 'error-page.png'); await writeFile(evidence.errorScreenshot, Buffer.from(img.data, 'base64')); }
      }
      throw new Error('Main frame snapshot: ' + mainError);
    }
    return s.frames?.filter(f => f.nodes).length >= 3 && s.frames[0].nodes.some(n => n.id === 'normal') ? s : null;
  });
  const all = () => snap.frames.flatMap(f => f.nodes || []);
  const find = id => { const n = all().find(n => n.id === id); if (!n) throw new Error('Missing fixture uid: ' + id); return n.uid; };
  checks('DOM snapshot reads main and cross-origin frame', snap.frames.some(f => f.nodes?.some(n => n.id === 'cross-field')), snap.frames.map(f => ({ url: f.url, nodeCount: f.nodes?.length, error: f.error })));
  checks('Snapshot discovers hidden file and closed shadow root', !!find('hidden') && !!find('shadow-file'), {});
  const desktopAtActionStart = desktopState();
  const beforeZoom=snap.frames[0].viewport.width;
  await tool('set_zoom',{pageId,factor:1.25});
  const zoomed=await tool('take_snapshot',{pageId});
  await tool('set_zoom',{pageId,factor:1});
  snap=await tool('take_snapshot',{pageId});
  checks('Per-tab native zoom changes CSS viewport without activation',zoomed.frames[0].viewport.width<beforeZoom && snap.frames[0].viewport.width===beforeZoom,{});
  await tool('fill_form', { pageId, elements: [{ uid: find('name'), value: '实验输入' }, { uid: find('controlled'), value: 'controlled-ok' }, { uid: find('check'), value: 'true' }, { uid: find('select'), value: 'b' }] });
  const state = await tool('read_text', { pageId });
  checks('Native value setter and input/change events', state.text.includes('controlled-ok') && !state.text.includes('INTERCEPTED:'), state);
  await tool('fill', { pageId, uid: find('cross-field'), value: 'cross-origin-input' });
  await tool('click', { pageId, uid: find('ordinary') });
  checks('Ordinary DOM click executes page handler', (await tool('read_text', { pageId })).text.includes('ordinary=1'), {});
  await tool('click', { pageId, uid: find('trusted') });
  checks('Trusted-event restriction is preserved', (await tool('read_text', { pageId })).text.includes('trusted-denied=1'), {});
  await tool('hover', { pageId, uid: find('hover-test') });
  checks('Pointer hover event preserves PointerEvent type', (await tool('read_text', { pageId })).text.includes('pointer-event=true:mouse'), {});
  await tool('press_key', { pageId, uid: find('key-test'), key: 'Control+a' });
  checks('Synthetic keyboard modifier is represented honestly', (await tool('read_text', { pageId })).text.includes('key=a:ctrl=true:trusted=false'), {});
  await tool('check',{pageId,uid:find('check'),checked:false});
  await tool('select',{pageId,uid:find('select'),value:'a'});
  snap=await tool('take_snapshot',{pageId});
  checks('Dedicated checkbox and select tools',all().find(n=>n.id==='check')?.checked===false && all().find(n=>n.id==='select')?.value==='a',{});
  await tool('drag',{pageId,from_uid:find('drag-source'),to_uid:find('drop-target')});
  checks('DOM drag/drop uses no system pointer', (await tool('read_text',{pageId})).text.includes('dropped-ok'),{});
  await tool('scroll',{pageId,y:100});
  checks('DOM scroll and wait tools', (await tool('wait_for',{pageId,text:'dropped-ok'})).matched,{});
  const linked = await tool('click',{pageId,uid:find('background-link')});
  const afterLink = await tool('list_pages',{});
  checks('New-window links open in a background tab',linked.openedInBackground && afterLink.find(t=>t.pageId===linked.pageId)?.active===false,linked);
  const logical = await tool('select_page',{pageId:linked.pageId});
  checks('Selecting a page does not activate it',logical.foregroundChanged===false && (await tool('list_pages',{})).find(t=>t.pageId===linked.pageId)?.active===false,logical);
  let backgroundShotRejected=false;
  try { await tool('take_screenshot',{pageId:linked.pageId}); } catch(e){backgroundShotRejected=/background_screenshot_unsupported/.test(e.message);}
  checks('Background screenshot fails without activation',backgroundShotRejected,{});
  await tool('close_page',{pageId:linked.pageId});
  const downloaded = await tool('download_file',{url:origin+'/download',filename:'stilldesk-fixture.bin',expectedSha256:hash(downloadBytes)});
  checks('Browser download completes and local SHA-256 is verified',downloaded.complete && downloaded.fileHashVerified && downloaded.sha256===hash(downloadBytes) && downloaded.bytes===downloadBytes.length,{bytes:downloaded.bytes,sha256:downloaded.sha256});
  let dialogUnsupported=false;
  try { await tool('handle_dialog',{pageId}); } catch(e){dialogUnsupported=/native_dialog_unsupported/.test(e.message);}
  checks('Native dialogs report an explicit capability boundary',dialogUnsupported,{});
  async function upload(id, selected) {
    const before = receipts.length;
    const result = await tool('upload_file', { pageId, uid: find(id), filePaths: selected.map(f => f.path) });
    const receipt = await eventually(() => receipts.slice(before).find(r => r.case === id));
    checks(`HTTP upload byte/hash verification: ${id}`, result.assigned && receipt.trusted === 'false' && receipt.files.length === selected.length && selected.every((f, i) => {
      const r = receipt.files[i]; return r.name === f.name && r.size === f.bytes.length && r.sha256 === f.sha256;
    }), { result, receipt });
  }
  await upload('normal', [files[0]]);
  await upload('hidden', [files[1]]);
  await upload('multi', files);
  await upload('same-file', [files[0]]);
  await upload('cross-file', [files[1]]);
  await upload('shadow-file', [files[2]]);
  let rejected = false;
  try { await tool('upload_file', { pageId, uid: find('normal'), filePaths: [path.join(ROOT, 'package.json')] }); }
  catch (e) { rejected = /root|allow|permission|authoriz/i.test(e.message); }
  checks('Upload outside explicit filesystem root is rejected', rejected, {});
  rejected = false;
  try { await tool('upload_file', { pageId, uid: find('normal'), filePaths: files.slice(0, 2).map(f => f.path) }); }
  catch (e) { rejected = /multiple/i.test(e.message); }
  checks('Multiple files rejected on a single-file input', rejected, {});
  const raw = await LabClient.open({ runtime: testRuntime });
  try {
    const emptyMeta = { name: 'empty.txt', size: 0, type: 'text/plain', lastModified: Date.now(), sha256: hash(Buffer.alloc(0)) };
    const { uploadId } = await raw.request('upload_begin', { pageId, uid: find('normal'), files: [emptyMeta] });
    await tool('click', { pageId, uid: find('disable-file') });
    rejected = false;
    try { await raw.request('upload_commit', { pageId, uploadId }); } catch (e) { rejected = /properties changed|disabled/i.test(e.message); }
    await raw.request('upload_abort', { pageId, uploadId });
    checks('Changing target input during upload prevents commit', rejected, {});
    const bad = await raw.request('upload_begin', { pageId, uid: find('hidden'), files: [{ ...emptyMeta, sha256: '0'.repeat(64) }] });
    rejected = false;
    try { await raw.request('upload_commit', { pageId, uploadId: bad.uploadId }); } catch (e) { rejected = /SHA-256 mismatch/.test(e.message); }
    await raw.request('upload_abort', { pageId, uploadId: bad.uploadId });
    checks('Extension independently rejects a mismatched file digest', rejected, {});
  } finally { raw.close(); }
  const adoptedUid = find('adopt-target');
  await tool('click', { pageId, uid: find('adopt') });
  rejected = false;
  try { await tool('fill', { pageId, uid: adoptedUid, value: 'must-fail' }); } catch (e) { rejected = /stale|snapshot/i.test(e.message); }
  checks('UID cannot follow a node adopted into another document', rejected, {});
  const oldUid = find('ordinary');
  await tool('take_snapshot', { pageId });
  rejected = false;
  try { await tool('click', { pageId, uid: oldUid }); } catch (e) { rejected = /stale|snapshot/i.test(e.message); }
  checks('Stale snapshot UID is rejected', rejected, {});
  snap = await tool('take_snapshot', { pageId });
  const navigationUid = find('ordinary');
  await tool('navigate_page', { pageId, url: origin + '/after-navigation' });
  await eventually(async () => { const p = await tool('list_pages', {}); const a = Array.isArray(p) ? p : p.pages; return a.some(t => t.pageId === pageId && t.url === origin + '/after-navigation'); });
  rejected = false;
  try { await tool('click', { pageId, uid: navigationUid }); } catch { rejected = true; }
  checks('Navigation invalidates document-bound UID', rejected, {});
  const shot = await rpc.request('tools/call', { name: 'take_screenshot', arguments: { pageId } });
  checks('Active viewport screenshot', !shot.isError && (shot.content?.some(c => c.type === 'image') || shot.content?.some(c => c.text?.includes('data:image/png') || c.text?.includes('filePath'))), { types: shot.content?.map(c => c.type) });
  const shotImage = shot.content?.find(c => c.type === 'image');
  if (shotImage) await writeFile(path.join(ROOT, 'evidence', 'fixture-viewport.png'), Buffer.from(shotImage.data, 'base64'));
  const plainUrl = `http://stilldesk-browser.test:${server.address().port}/plain`;
  const newTab = await tool('new_page', { url: plainUrl, background: true });
  checks('Create a tab through extension API', Number.isInteger(newTab.pageId), newTab);
  const insecure = await eventually(async () => {
    const s = await tool('take_snapshot', { pageId: newTab.pageId });
    return s.frames?.[0]?.nodes?.some(n => n.id === 'insecure-file') ? s : null;
  });
  const insecureUid = id => insecure.frames[0].nodes.find(n => n.id === id).uid;
  await tool('fill', { pageId: newTab.pageId, uid: insecureUid('insecure-field'), value: 'ordinary-http-ok' });
  const count = receipts.length;
  await tool('upload_file', { pageId: newTab.pageId, uid: insecureUid('insecure-file'), filePaths: [files[0].path] });
  const received = await eventually(() => receipts.slice(count).find(r => r.case === 'insecure-file'));
  checks('Snapshot, fill and upload work on non-secure HTTP origin', received.files[0]?.sha256 === files[0].sha256, { url: plainUrl, receipt: received });
  await tool('close_page', { pageId: newTab.pageId });
  checks('Close created tab through extension API', true, {});
  const desktopAfter = desktopState();
  if(desktopAfter.available) checks('Foreground window and system pointer remain unchanged during actions',
    desktopAfter.foreground===desktopAtActionStart.foreground && desktopAfter.x===desktopAtActionStart.x && desktopAfter.y===desktopAtActionStart.y,
    {foregroundUnchanged:desktopAfter.foreground===desktopAtActionStart.foreground,pointerUnchanged:desktopAfter.x===desktopAtActionStart.x && desktopAfter.y===desktopAtActionStart.y});
  evidence.passed = true;
  delete evidence.lastSnapshot;
} catch (error) {
  evidence.passed = false;
  evidence.failure = String(error.stack || error);
  process.exitCode = 1;
  process.stderr.write(evidence.failure + '\n');
} finally {
  if (mcpProcess) { mcpProcess.stdin.end(); mcpProcess.kill(); }
  if (browserProcess && browserProcess.exitCode === null) {
    // Stop only the process tree launched by this test, never the daily Chrome tree.
    if (process.platform === 'win32') { try { execFileSync('taskkill.exe', ['/PID', String(browserProcess.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); } catch {} }
    else browserProcess.kill();
  }
  evidence.nativeHostRollback = [];
  for (const { browser, receipt } of registrations.reverse()) if (!receipt.alreadyApplied) {
    try { evidence.nativeHostRollback.push(await rollbackRegistration({ browser })); }
    catch (e) { evidence.nativeHostRollbackError = e.message; evidence.passed = false; process.exitCode = 1; }
  }
  if (testRuntime) {
    try { const c = await LabClient.open({ runtime: testRuntime, autoStart: false }); await c.request('shutdown'); c.close(); }
    catch (e) { evidence.brokerCleanup = e.message; }
  }
  server.closeAllConnections(); frameServer.closeAllConnections(); server.close(); frameServer.close();
  evidence.completedAt = new Date().toISOString();
  if (!evidence.passed) evidence.diagnostics = { browser: browserErrors, mcp: mcpErrors, httpRequests: requests };
  const privateEvidence = run ? path.join(run,'evidence.json') : path.join(os.tmpdir(),'stilldesk-browser-e2e-failed.json');
  await writeFile(privateEvidence,JSON.stringify(evidence,null,2)+'\n');
  const publicSummary = {version:'0.2.0',browserFamily,platform:process.platform,passed:evidence.passed,
    liveBrowserTest:true,dailyProfileTested:false,remoteDebugging:false,debuggerPermission:false,
    executableSha256:evidence.browserExecutableSha256,completedAt:evidence.completedAt,
    tests:evidence.tests.map(t=>({name:t.name,passed:t.passed})),trialConfiguration:evidence.trialConfiguration,
    nativeRegistrationsRestored:evidence.nativeHostRollback?.every(r=>r.rolledBack)??false,
    ...(evidence.failure?{failure:evidence.failure.split('\n')[0].replaceAll(ROOT,'$PACKAGE').replaceAll(run||'__unused__','$FIXTURE')}: {})};
  const results = path.resolve(ROOT,'..','..','tests','browser');
  await mkdir(results,{recursive:true});
  await writeFile(path.join(results,`e2e-${browserFamily}.json`),JSON.stringify(publicSummary,null,2)+'\n');
  process.stdout.write('Private evidence: '+privateEvidence+'\n');
  if(originalHome===undefined)delete process.env.STILLDESK_HOME;else process.env.STILLDESK_HOME=originalHome;
}
