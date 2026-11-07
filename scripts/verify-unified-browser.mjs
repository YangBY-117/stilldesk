// Integration check for the public CLI/MCP router, using a disposable local fixture.
// No daily profile, remote debugging, real account, or GUI input is used.
import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { MCPProcess } from '../lib/protocol.mjs';
import { ROOT } from '../lib/providers.mjs';
import { prepare } from '../packages/browser/scripts/prepare.mjs';
import { applyRegistration, rollbackRegistration } from '../packages/browser/scripts/register-native-host.mjs';
import { LabClient, runtimePaths } from '../packages/browser/lib/transport.mjs';

const executable = process.env.STILLDESK_TEST_BROWSER;
if (!executable) throw new Error('Set STILLDESK_TEST_BROWSER to the explicitly selected Chrome for Testing executable');
const directory = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-unified-fixture-'));
process.env.STILLDESK_HOME = path.join(directory, 'state');
delete process.env.STILLDESK_BROWSER_RUNTIME;
const uploadRoot = path.join(directory, 'uploads'), downloadRoot = path.join(directory, 'downloads');
const profile = path.join(directory, 'profile');
await Promise.all([mkdir(uploadRoot), mkdir(downloadRoot), mkdir(path.join(profile, 'Default'), { recursive: true })]);
await writeFile(path.join(profile, 'Default/Preferences'), JSON.stringify({ download: { default_directory: downloadRoot, prompt_for_download: false } }));
const bytes = Buffer.from('StillDesk unified router fixture: synthetic contents.\n');
const sha256 = createHash('sha256').update(bytes).digest('hex');
const uploadPath = path.join(uploadRoot, 'report.txt'); await writeFile(uploadPath, bytes);
let received = false;
const server = http.createServer((req, res) => {
  if (req.method === 'POST') {
    const chunks = []; req.on('data', c => chunks.push(c)); req.on('end', () => {
      received = Buffer.concat(chunks).includes(bytes);
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ received }));
    });
  } else if (req.url === '/download') {
    res.writeHead(200, { 'Content-Type': 'text/plain', 'Content-Disposition': 'attachment; filename="report-copy.txt"' }); res.end(bytes);
  } else {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end('<!doctype html><h1>StillDesk unified fixture</h1><label>Title<input id="title"></label><input type="file" id="file"><output id="status"></output><script>document.querySelector("#file").onchange=async e=>{const f=new FormData();f.append("file",e.target.files[0]);const r=await fetch("/upload",{method:"POST",body:f});document.querySelector("#status").textContent=(await r.json()).received?"SERVER RECEIVED":"FAILED";}</script>');
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const address = `http://127.0.0.1:${server.address().port}/`;
const registrations = [], checks = [];
let browser, mcp;
const report = { passed: false, scope: 'public unified MCP router + real Native Messaging + local synthetic upload/download',
  browserRemoteDebugging: false, browserInheritedStillDeskEnvironment: false, checks };
const check = (name, value) => { checks.push({ name, passed: !!value }); if (!value) throw new Error(name); };
async function eventually(fn) {
  const deadline = Date.now() + 30000; let last;
  while (Date.now() < deadline) { try { const value = await fn(); if (value) return value; } catch (e) { last = e; } await new Promise(r => setTimeout(r, 150)); }
  throw last || new Error('Timed out awaiting fixture');
}
try {
  const built = await prepare();
  for (const family of ['chrome', 'chromium']) registrations.push({ family, receipt: await applyRegistration({ browser: family }) });
  const browserEnvironment = { ...process.env };
  delete browserEnvironment.STILLDESK_HOME; delete browserEnvironment.STILLDESK_BROWSER_RUNTIME;
  browser = spawn(executable, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--no-proxy-server', '--disable-background-networking', '--disable-field-trial-config',
    `--user-data-dir=${profile}`, `--disable-extensions-except=${built.extensionDirectory}`, `--load-extension=${built.extensionDirectory}`, address],
    { windowsHide: true, env: browserEnvironment, stdio: 'ignore' });
  mcp = new MCPProcess({ command: process.execPath, args: [path.join(ROOT, 'bin/stilldesk.mjs'), '--allow-upload-root', uploadRoot,
    '--allow-download-root', downloadRoot, 'mcp'], cwd: ROOT });
  await mcp.initialize();
  const tools = (await mcp.request('tools/list')).tools;
  check('Router discovers all native tool groups', tools.length >= 70 && tools.some(t => t.name === 'browser_download_file') && tools.some(t => t.name === 'wechat_status'));
  async function tool(name, args) {
    const r = await mcp.request('tools/call', { name, arguments: args });
    if (r.isError) throw new Error(r.content?.[0]?.text || 'Tool failed');
    return JSON.parse(r.content.find(c => c.type === 'text').text);
  }
  const pages = await eventually(() => tool('browser_list_pages', {}));
  const tab = pages.find(p => p.url === address);
  check('Pinned host runtime works without browser environment inheritance', !!tab);
  const snapshot = await eventually(async () => {
    const s = await tool('browser_take_snapshot', { pageId: tab.pageId });
    return s.frames?.[0]?.nodes?.some(n => n.id === 'file') ? s : null;
  });
  const uid = id => snapshot.frames[0].nodes.find(n => n.id === id).uid;
  await tool('browser_fill', { pageId: tab.pageId, uid: uid('title'), value: 'Complete through the public router' });
  check('Public prefixed tool routes a form action', true);
  const upload = await tool('browser_upload_file', { pageId: tab.pageId, uid: uid('file'), filePaths: [uploadPath] });
  await eventually(() => received);
  check('Public upload root reaches the server with the real file', upload.assigned && received);
  const download = await tool('browser_download_file', { url: address + 'download', filename: 'report-copy.txt', expectedSha256: sha256 });
  check('Public download-root argv permits independent SHA-256 readback', download.fileHashVerified && download.sha256 === sha256);
  check('Downloaded bytes equal the synthetic source', Buffer.compare(await readFile(download.filename), bytes) === 0);
  report.passed = true;
} catch (error) { report.error = error.message; process.exitCode = 1; }
finally {
  mcp?.close();
  if (browser?.exitCode === null) {
    if (process.platform === 'win32') { try { execFileSync('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true }); } catch {} }
    else browser.kill();
  }
  report.nativeRegistrationsRestored = true;
  for (const { family, receipt } of registrations.reverse()) if (!receipt.alreadyApplied) {
    try { await rollbackRegistration({ browser: family }); } catch { report.nativeRegistrationsRestored = false; report.passed = false; process.exitCode = 1; }
  }
  try { const cleanup = await LabClient.open({ runtime: runtimePaths(), autoStart: false }); await cleanup.request('shutdown'); cleanup.close(); } catch {}
  server.closeAllConnections(); server.close();
  await mkdir(path.join(ROOT, 'tests/browser'), { recursive: true });
  await writeFile(path.join(ROOT, 'tests/browser/unified-router.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
