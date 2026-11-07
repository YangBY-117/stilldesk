import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SOURCES = {
  codex: 'https://developers.openai.com/codex/mcp',
  claude: 'https://code.claude.com/docs/en/mcp',
  gemini: 'https://geminicli.com/docs/tools/mcp-server/',
  kimi: 'https://moonshotai.github.io/kimi-code/en/customization/mcp.html',
  zcode: 'https://zcode.z.ai/en/docs/mcp-services',
  deepseek: 'https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md',
  openclaw: 'https://docs.openclaw.ai/tools/mcp',
  hermes: 'https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/',
  cursor: 'https://cursor.com/docs/context/mcp',
  vscode: 'https://code.visualstudio.com/docs/agents/reference/mcp-configuration',
  cline: 'https://docs.cline.bot/mcp/mcp-overview',
  opencode: 'https://opencode.ai/docs/mcp-servers/',
  plugin: 'https://developers.openai.com/plugins/build/plugins',
  mcp: 'https://modelcontextprotocol.io/specification/2025-11-25/basic/transports',
};
const CLIENTS = [
  { id: 'codex', name: 'Codex', format: 'toml', filename: '~/.codex/config.toml', source: SOURCES.codex },
  { id: 'claude-code', name: 'Claude Code', format: 'json', filename: '.mcp.json', source: SOURCES.claude,
    note: 'Configuration-only validation. Claude Code was not launched, as requested.' },
  { id: 'gemini', name: 'Gemini CLI', format: 'json', filename: '~/.gemini/settings.json', source: SOURCES.gemini },
  { id: 'kimi-code', name: 'Kimi Code', format: 'json', filename: '~/.kimi-code/mcp.json, KIMI_CODE_HOME/mcp.json, or project .kimi-code/mcp.json', source: SOURCES.kimi },
  { id: 'zcode', name: 'ZCode', format: 'json', filename: 'Settings → MCP Servers → Full configuration', source: SOURCES.zcode },
  { id: 'deepseek-harness', name: 'DeepSeek Harness', format: 'yaml', filename: 'Cordis scope: one @deepseek-ai/dsh-mcp-client entry', source: SOURCES.deepseek },
  { id: 'openclaw', name: 'OpenClaw', format: 'json', filename: '~/.openclaw/openclaw.json', source: SOURCES.openclaw },
  { id: 'hermes', name: 'Hermes Agent', format: 'yaml', filename: '~/.hermes/config.yaml', source: SOURCES.hermes },
  { id: 'cursor', name: 'Cursor', format: 'json', filename: '.cursor/mcp.json', source: SOURCES.cursor },
  { id: 'vscode', name: 'VS Code local harness', format: 'json', filename: '.vscode/mcp.json', source: SOURCES.vscode },
  { id: 'github-copilot', name: 'GitHub Copilot portable harness', format: 'json', filename: '.mcp.json or ~/.copilot/mcp-config.json', source: SOURCES.vscode },
  { id: 'cline', name: 'Cline', format: 'json', filename: '~/.cline/data/settings/cline_mcp_settings.json (CLI), or IDE MCP settings', source: SOURCES.cline },
  { id: 'opencode', name: 'OpenCode', format: 'json', filename: 'opencode.json', source: SOURCES.opencode },
  { id: 'chatgpt-desktop', name: 'ChatGPT / Codex desktop plugin', format: 'json', filename: 'plugin.json + mcp.json', source: SOURCES.plugin,
    note: 'Portable desktop package generation only. No public plugin directory submission or ChatGPT cloud deployment is implied.' },
  { id: 'generic', name: 'Other MCP stdio clients', format: 'json', filename: 'client-specific MCP server settings', source: SOURCES.mcp },
];
const aliases = { claude: 'claude-code', 'gemini-cli': 'gemini', kimi: 'kimi-code', deepseek: 'deepseek-harness',
  dsh: 'deepseek-harness', 'hermes-agent': 'hermes', 'vs-code': 'vscode', copilot: 'github-copilot',
  chatgpt: 'chatgpt-desktop', plugin: 'chatgpt-desktop' };
const clean = (value, label) => {
  if (typeof value !== 'string' || !value || /[\0\r\n]/.test(value)) throw new Error(`${label} must be a nonempty single-line string`);
  return value;
};
const absolute = value => path.isAbsolute(value) || path.win32.isAbsolute(value);
const quoted = value => JSON.stringify(value);
function yaml(value, indent = 0) {
  const space = ' '.repeat(indent);
  return Object.entries(value).map(([key, item]) => {
    if (item && typeof item === 'object' && !Array.isArray(item)) return `${space}${key}:\n${yaml(item, indent + 2)}`;
    return `${space}${key}: ${JSON.stringify(item)}`;
  }).join('\n');
}
export function listClients() {
  return CLIENTS.map(c => ({ ...c, transport: 'stdio', validation: 'official documentation + static generated configuration tests',
    clientLaunched: false, endToEndClientVerified: false }));
}
export function generateConfig(client, options = {}) {
  client = aliases[client] || client;
  const entry = CLIENTS.find(c => c.id === client);
  if (!entry) throw new Error(`Unknown client. Available: ${CLIENTS.map(c => c.id).join(', ')}`);
  const root = clean(options.root || fileURLToPath(new URL('../../', import.meta.url)), 'root');
  if (!absolute(root)) throw new Error('root must be an absolute path');
  const join = path.win32.isAbsolute(root) && !path.posix.isAbsolute(root) ? path.win32.join : path.join;
  const command = clean(options.node || 'node', 'node');
  const args = [join(root, 'bin', 'stilldesk.mjs'), 'mcp'];
  if (options.uploads !== undefined && !Array.isArray(options.uploads)) throw new Error('uploads must be an array of explicit absolute roots');
  for (const upload of options.uploads || []) {
    clean(upload, 'upload root'); if (!absolute(upload)) throw new Error('Upload roots must be absolute');
    args.push('--allow-upload-root', upload);
  }
  if (options.downloads !== undefined && !Array.isArray(options.downloads)) throw new Error('downloads must be an array of explicit absolute roots');
  for (const download of options.downloads || []) {
    clean(download, 'download root'); if (!absolute(download)) throw new Error('Download roots must be absolute');
    args.push('--allow-download-root', download);
  }
  const server = { command, args };
  if (options.python) server.env = { STILLDESK_PYTHON: clean(options.python, 'python') };
  let configuration, content;
  if (client === 'codex') {
    configuration = { mcp_servers: { stilldesk: server } };
    content = `[mcp_servers.stilldesk]\ncommand = ${quoted(command)}\nargs = ${quoted(args)}\n`;
    if (server.env) content += `[mcp_servers.stilldesk.env]\nSTILLDESK_PYTHON = ${quoted(server.env.STILLDESK_PYTHON)}\n`;
  } else if (client === 'hermes') {
    configuration = { mcp_servers: { stilldesk: server } };
    content = yaml(configuration) + '\n';
  } else if (client === 'deepseek-harness') {
    configuration = [{ id: 'mcp-stilldesk', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'stilldesk', transport: 'stdio', ...server } }];
    content = '- id: "mcp-stilldesk"\n  name: "@deepseek-ai/dsh-mcp-client"\n  config:\n' + yaml(configuration[0].config, 4) + '\n';
  } else if (client === 'openclaw') configuration = { mcp: { servers: { stilldesk: { transport: 'stdio', enabled: true, ...server } } } };
  else if (client === 'vscode') configuration = { servers: { stilldesk: { type: 'stdio', ...server } } };
  else if (client === 'opencode') configuration = { $schema: 'https://opencode.ai/config.json', mcp: { servers: { stilldesk: { type: 'local', command: [command, ...args],
    ...(server.env ? { environment: server.env } : {}) } } } };
  else if (client === 'chatgpt-desktop') configuration = {
    '$schema': 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json', mcpServers: { stilldesk: { type: 'stdio', ...server } },
  };
  else configuration = { mcpServers: { stilldesk: { ...(['claude-code'].includes(client) ? { type: 'stdio' } : {}), ...server } } };
  content ||= JSON.stringify(configuration, null, 2) + '\n';
  return { client, format: entry.format, filename: entry.filename, configuration, content,
    validation: 'static-format-verified; not client runtime acceptance', clientLaunched: false,
    sources: [entry.source], notes: [entry.note || 'Merge only the named server into the client configuration. Review the command and explicit upload roots first.'] };
}
export function generatePluginManifest() {
  return { $schema: 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json', name: 'stilldesk', version: '0.1.0', description: 'Background-first native app tools. Your cursor stays yours.',
    author: { name: 'StillDesk contributors' }, license: 'MIT',
    extensions: { 'com.openai': { interface: { displayName: 'StillDesk', shortDescription: 'Native app tools without taking over your desktop.',
      longDescription: 'Connect background browser, message, and meeting tools through MCP. Each integration reports its supported capabilities and configuration requirements.' } } } };
}
