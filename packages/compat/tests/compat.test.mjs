import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { listClients, generateConfig, generatePluginManifest } from '../index.mjs';

const root = 'C:\\path with spaces\\stilldesk';
test('all fifteen documented clients generate static configurations, never execute clients', () => {
  assert.equal(listClients().length, 15);
  for (const client of listClients()) {
    const generated = generateConfig(client.id, { root });
    assert.equal(generated.clientLaunched, false);
    assert.equal(client.endToEndClientVerified, false);
    assert(generated.sources.every(url => url.startsWith('https://')));
    if (generated.format === 'json') assert.deepEqual(JSON.parse(generated.content), generated.configuration);
    assert(generated.content.includes('stilldesk.mjs'));
    assert(!generated.content.includes('bypassPermissions'));
  }
});
test('Claude Code is config only; generator has no launch or shell operation', () => {
  const config = generateConfig('claude-code', { root });
  assert.equal(config.configuration.mcpServers.stilldesk.type, 'stdio');
  assert.equal(config.configuration.mcpServers.stilldesk.command, 'node');
  assert(config.notes.some(note => note.includes('not launched')));
});
test('current OpenClaw, Hermes, DeepSeek, VS Code, OpenCode use their own schemas', () => {
  assert.equal(generateConfig('openclaw', { root }).configuration.mcp.servers.stilldesk.transport, 'stdio');
  assert(generateConfig('hermes', { root }).configuration.mcp_servers.stilldesk);
  assert.equal(generateConfig('deepseek-harness', { root }).configuration[0].name, '@deepseek-ai/dsh-mcp-client');
  assert(generateConfig('vscode', { root }).configuration.servers.stilldesk);
  const opencode = generateConfig('opencode', { root }).configuration.mcp.servers.stilldesk;
  assert.equal(opencode.type, 'local'); assert(Array.isArray(opencode.command)); assert(!Object.hasOwn(opencode, 'enabled'));
});
test('upload roots are explicit argv entries and unsafe configuration values are rejected', () => {
  const output = generateConfig('generic', { root, uploads: ['C:\\fixture\\uploads; literal'], python: 'python3' });
  assert.deepEqual(output.configuration.mcpServers.stilldesk.args.slice(-2), ['--allow-upload-root', 'C:\\fixture\\uploads; literal']);
  assert.equal(output.configuration.mcpServers.stilldesk.env.STILLDESK_PYTHON, 'python3');
  assert.throws(() => generateConfig('codex', { root: 'relative' }), /absolute/);
  assert.throws(() => generateConfig('generic', { root, uploads: ['relative'] }), /absolute/);
  const downloads = generateConfig('generic', { root, downloads: ['C:\\fixture\\downloads'] });
  assert.deepEqual(downloads.configuration.mcpServers.stilldesk.args.slice(-2), ['--allow-download-root', 'C:\\fixture\\downloads']);
  assert.throws(() => generateConfig('generic', { root, downloads: ['relative'] }), /absolute/);
  assert.throws(() => generateConfig('generic', { root, node: 'node\nmalicious' }), /single-line/);
  assert.throws(() => generateConfig('unknown', { root }), /Unknown client/);
});
test('Codex TOML parses with the Python standard library, including Windows backslashes', () => {
  const generated = generateConfig('codex', { root, python: 'C:\\path with spaces\\python.exe' });
  const child = spawnSync(process.platform === 'win32' ? 'python.exe' : 'python3', ['-c', 'import tomllib,sys,json; print(json.dumps(tomllib.loads(sys.stdin.read())))'], { input: generated.content, encoding: 'utf8', windowsHide: true });
  assert.equal(child.status, 0, child.stderr);
  assert.deepEqual(JSON.parse(child.stdout), generated.configuration);
});
test('portable plugin metadata is separate from credentials and public deployment status', () => {
  const manifest = generatePluginManifest();
  assert.equal(manifest.name, 'stilldesk'); assert(manifest.extensions['com.openai']);
  assert.equal(manifest.$schema, 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json');
  assert.equal(manifest.extensions['com.openai'].interface.displayName, 'StillDesk');
  assert(!Object.hasOwn(manifest.extensions['com.openai'], 'displayName'));
  const example = generateConfig('chatgpt-desktop', { root });
  assert.equal(example.configuration.$schema, 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json');
  assert(example.notes.some(n => n.includes('No public plugin directory')));
});
