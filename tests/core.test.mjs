import test from 'node:test';
import assert from 'node:assert/strict';
import { createSession } from '../lib/server.mjs';
import { serve } from '../lib/http.mjs';
import { Providers } from '../lib/providers.mjs';
import { redact } from '../lib/protocol.mjs';
import { MCPProcess, MAX_BYTES } from '../lib/protocol.mjs';
import os from 'node:os';
import path from 'node:path';
import { runtimePaths as browserRuntimePaths } from '../packages/browser/lib/transport.mjs';

const fakeRouter = () => ({ closed: false,
  async listTools() { return [{ name: 'echo', inputSchema: { type: 'object' } }]; },
  async callTool(name, args, { signal } = {}) {
    if (name === 'wait') await new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true }));
    return { content: [{ type: 'text', text: JSON.stringify(args) }] };
  }, close() { this.closed = true; } });
test('MCP session initializes, rejects invalid methods and routes arguments', async () => {
  const session = createSession({ router: fakeRouter() });
  assert.equal((await session.handle({ jsonrpc: '2.0', id: 1, method: 'tools/list' })).error.code, -32000);
  const initialized = await session.handle({ jsonrpc: '2.0', id: 2, method: 'initialize', params: { protocolVersion: '2025-03-26' } });
  assert.equal(initialized.result.protocolVersion, '2025-03-26');
  assert.equal((await session.handle({ jsonrpc: '2.0', id: 3, method: 'tools/list' })).result.tools[0].name, 'echo');
  const answer = await session.handle({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'echo', arguments: { value: 'fixture' } } });
  assert.deepEqual(JSON.parse(answer.result.content[0].text), { value: 'fixture' });
  assert.equal((await session.handle({ jsonrpc: '2.0', id: 5, method: 'unknown' })).error.code, -32601);
  session.close();
});
test('Cancellation reaches an in-flight provider without replay', async () => {
  const session = createSession({ router: fakeRouter() });
  await session.handle({ jsonrpc: '2.0', id: 1, method: 'initialize' });
  const operation = session.handle({ jsonrpc: '2.0', id: 'operation', method: 'tools/call', params: { name: 'wait', arguments: {} } });
  await session.handle({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: 'operation' } });
  assert.equal((await operation).result.isError, true);
  session.close();
});
test('HTTP MCP requires strong credentials and rejects unauthorized/foreign origins', async () => {
  await assert.rejects(() => serve({ token: 'short', port: 0 }), /32/);
  const token = 'fixture-only-' + 'x'.repeat(40);
  const instance = await serve({ token, port: 0, sessionFactory: () => createSession({ router: fakeRouter() }) });
  try {
    const url = `http://127.0.0.1:${instance.address.port}/mcp`;
    const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
    assert.equal((await fetch(url, { method: 'POST', body: '{}' })).status, 401);
    assert.equal((await fetch(url, { method: 'POST', headers: { ...headers, Origin: 'https://foreign.invalid' }, body: '{}' })).status, 403);
    const init = await fetch(url, { method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize' }) });
    assert.equal(init.status, 200); const id = init.headers.get('mcp-session-id'); assert.ok(id);
    const list = await fetch(url, { method: 'POST', headers: { ...headers, 'Mcp-Session-Id': id }, body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }) });
    assert.equal((await list.json()).result.tools[0].name, 'echo');
    assert.equal((await fetch(url, { method: 'DELETE', headers: { ...headers, 'Mcp-Session-Id': id } })).status, 200);
    assert.equal((await fetch(url, { method: 'POST', headers: { ...headers, 'Mcp-Session-Id': id }, body: '{}' })).status, 404);
  } finally { await instance.close(); }
});
test('Status never substitutes a missing native backend with an available one', async () => {
  const router = new Providers({ definitions: { missing: { command: 'stilldesk-fixture-missing-executable' } }, platforms: { listTools: () => [] } });
  try {
    assert.equal((await router.listTools()).length, 1);
    const status = await router.status();
    assert.equal(status.providers[0].schemaDiscovered, false);
    assert.ok(status.providers[0].error);
    await assert.rejects(() => router.callTool('missing_send', {}), /unavailable/);
  } finally { router.close(); }
});
test('Transport diagnostics redact credential values', () => {
  assert.equal(redact('Authorization: Bearer fixture-secret\napi_key=private-value'), 'Authorization: Bearer [redacted]\napi_key=[redacted]');
});
for (const kind of ['malformed', 'oversized']) test(`A ${kind} backend frame terminates its provider instead of retaining an output stream`, async () => {
  const script = kind === 'malformed'
    ? "process.stdout.write('invalid-json\\n');setInterval(()=>process.stdout.write('more'),10)"
    : `process.stdout.write('x'.repeat(${MAX_BYTES + 1}));setInterval(()=>process.stdout.write('more'),10)`;
  const child = new MCPProcess({ command: process.execPath, args: ['-e', script] });
  try {
    await assert.rejects(() => child.initialize(), /Invalid JSON|size limit/);
    assert.equal(child.closed, true);
    assert.equal(child.child.killed, true);
    assert.equal(child.pending.size, 0);
  } finally { child.close(); }
});
test('Concurrent first requests wait for one backend initialization handshake', async () => {
  const script = "let b='',ready=false;process.stdin.setEncoding('utf8');process.stdin.on('data',c=>{b+=c;let i;while((i=b.indexOf('\\n'))>=0){const m=JSON.parse(b.slice(0,i));b=b.slice(i+1);if(m.method==='initialize')setTimeout(()=>{ready=true;process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{protocolVersion:'2025-03-26',capabilities:{},serverInfo:{name:'fixture',version:'1'}}})+'\\n')},150);else if(m.method==='tools/list')process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,...(ready?{result:{tools:[]}}:{error:{code:-32000,message:'not initialized'}})})+'\\n')}})";
  const router = new Providers({ definitions: { delayed: { command: process.execPath, args: ['-e', script] } }, platforms: { listTools: () => [] } });
  try {
    const [first, secondResult] = await Promise.all([router.backend('delayed'),
      router.backend('delayed').then(child => child.request('tools/list'))]);
    assert.deepEqual(secondResult, { tools: [] });
    assert.equal(router.children.size, 1);
    assert.equal(first, router.children.get('delayed'));
  } finally { router.close(); }
});
test('Unified browser provider and Native Messaging host resolve the same default and overridden runtime', () => {
  const beforeHome = process.env.STILLDESK_HOME, beforeRuntime = process.env.STILLDESK_BROWSER_RUNTIME;
  try {
    process.env.STILLDESK_HOME = path.join(os.tmpdir(), 'stilldesk-runtime-path-fixture');
    delete process.env.STILLDESK_BROWSER_RUNTIME;
    let definition = new Providers().definitionsFor().browser;
    assert.equal(definition.args[definition.args.indexOf('--runtime') + 1], browserRuntimePaths().dir);
    assert.equal(browserRuntimePaths().dir, path.join(process.env.STILLDESK_HOME, 'browser', 'runtime'));
    process.env.STILLDESK_BROWSER_RUNTIME = path.join(os.tmpdir(), 'stilldesk-explicit-runtime-fixture');
    definition = new Providers().definitionsFor().browser;
    assert.equal(definition.args[definition.args.indexOf('--runtime') + 1], browserRuntimePaths().dir);
  } finally {
    if (beforeHome === undefined) delete process.env.STILLDESK_HOME; else process.env.STILLDESK_HOME = beforeHome;
    if (beforeRuntime === undefined) delete process.env.STILLDESK_BROWSER_RUNTIME; else process.env.STILLDESK_BROWSER_RUNTIME = beforeRuntime;
  }
});
