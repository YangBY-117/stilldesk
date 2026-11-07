import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { ROOT } from '../lib/providers.mjs';
import { MCPProcess } from '../lib/protocol.mjs';

test('Unified stdio MCP discovers native provider tools and preserves typed unconfigured state', {
  skip: !process.env.STILLDESK_PYTHON && !existsSync(path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')),
}, async () => {
  const child = new MCPProcess({ command: process.execPath, args: [path.join(ROOT, 'bin/stilldesk.mjs'), 'mcp'], cwd: ROOT });
  try {
    await child.initialize();
    const tools = (await child.request('tools/list')).tools;
    for (const name of ['browser_list_pages', 'wechat_status', 'qq_status', 'stilldesk_status']) assert.ok(tools.some(t => t.name === name), name);
    assert.ok(tools.some(t => t.name.startsWith('feishu_')));
    assert.ok(tools.some(t => t.name.startsWith('meeting_')));
    const status = await child.request('tools/call', { name: 'stilldesk_status', arguments: {} });
    const parsed = JSON.parse(status.content[0].text);
    assert.equal(parsed.policy.foregroundTakeover, false);
    assert.equal(parsed.policy.cursorControl, false);
    assert.equal(parsed.providers.find(p => p.name === 'wechat').configured, false);
    assert.ok(parsed.providers.every(p => p.schemaDiscovered));
  } finally { child.close(); }
});
