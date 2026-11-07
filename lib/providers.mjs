import os from 'node:os';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { MCPProcess } from './protocol.mjs';
import { runtimePaths as browserRuntimePaths } from '../packages/browser/lib/transport.mjs';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const home = () => path.resolve(process.env.STILLDESK_HOME || path.join(os.homedir(), '.stilldesk'));
function describeError(value) {
  if (!value) return null;
  const missing = /No module named ['"]([^'"]+)['"]/.exec(value);
  if (missing) return { code: 'dependency_missing', message: `Python package ${missing[1]} is not installed in the selected environment`,
    recovery: 'Run node scripts/setup.mjs --messaging, or set STILLDESK_PYTHON to an environment with the native app packages.' };
  if (/ENOENT|not found|not recognized/i.test(value)) return { code: 'executable_missing', message: 'The configured provider executable is unavailable',
    recovery: 'Check the selected interpreter and run the setup command for this provider.' };
  return { code: 'provider_unavailable', message: String(value).split(/\r?\n/)[0].slice(0, 240),
    recovery: 'Check the provider setup guide. No login or foreground fallback was attempted.' };
}
export function pythonExecutable() {
  const venv = path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return process.env.STILLDESK_PYTHON || (existsSync(venv) ? venv : process.platform === 'win32' ? 'python' : 'python3');
}
function pythonProvider(name) {
  const config = process.env[`STILLDESK_${name.toUpperCase()}_CONFIG`];
  return { command: pythonExecutable(), args: ['-X', 'utf8', '-m', `${name}_bridge.server`, ...(config ? ['--config', config] : [])],
    cwd: path.join(ROOT, 'integrations', name), env: { PYTHONUTF8: '1', PYTHONPATH: path.join(ROOT, 'integrations', name) } };
}
export class Providers {
  constructor({ uploads = [], downloads = [], definitions, platforms } = {}) {
    this.uploads = uploads; this.downloads = downloads; this.definitions = definitions; this.platformsOverride = platforms;
    this.children = new Map(); this.errors = new Map(); this.routes = new Map(); this.closed = false;
    this.initializing = new Map();
  }
  definitionsFor() {
    return this.definitions || {
      browser: { command: process.execPath, args: [path.join(ROOT, 'packages/browser/mcp-server.mjs'),
        '--runtime', browserRuntimePaths().dir, ...this.uploads.flatMap(p => ['--allow-upload-root', path.resolve(p)]),
        ...this.downloads.flatMap(p => ['--allow-download-root', path.resolve(p)])], cwd: ROOT },
      wechat: pythonProvider('wechat'), qq: pythonProvider('qq'),
    };
  }
  async platforms() {
    return this.platformsOverride || import('../packages/platforms/index.mjs');
  }
  async backend(name) {
    if (this.closed) throw new Error('Router closed');
    if (this.initializing.has(name)) return this.initializing.get(name);
    const existing = this.children.get(name);
    if (existing && !existing.closed) return existing;
    const definition = this.definitionsFor()[name];
    if (!definition) throw new Error('Unknown provider');
    const processClient = new MCPProcess(definition);
    this.children.set(name, processClient);
    const initialization = processClient.initialize().then(() => {
      this.errors.delete(name); return processClient;
    }).catch(error => {
      processClient.close(); this.errors.set(name, error.message); throw error;
    }).finally(() => this.initializing.delete(name));
    this.initializing.set(name, initialization);
    return initialization;
  }
  async listTools() {
    const result = [{ name: 'stilldesk_status', description: 'Report provider setup and honest background capability states without logging in or starting capture.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true } }];
    for (const name of Object.keys(this.definitionsFor())) {
      try {
        const child = await this.backend(name);
        const response = await child.request('tools/list');
        for (const tool of response.tools || []) {
          const publicName = tool.name.startsWith(name + '_') ? tool.name : `${name}_${tool.name}`;
          this.routes.set(publicName, { provider: name, original: tool.name });
          result.push({ ...tool, name: publicName });
        }
      } catch (error) { this.errors.set(name, error.message); }
    }
    try {
      const platform = await this.platforms();
      for (const tool of await platform.listTools()) {
        this.routes.set(tool.name, { provider: 'platforms', original: tool.name }); result.push(tool);
      }
    } catch (error) { this.errors.set('platforms', error.message); }
    return result;
  }
  async status() {
    return { version: '0.1.0', policy: { backgroundOnly: true, cursorControl: false, foregroundTakeover: false,
      browserRemoteDebugging: false, unsupportedActionsFailExplicitly: true },
      providers: Object.keys(this.definitionsFor()).map(name => ({ name,
        schemaDiscovered: this.children.has(name) && !this.children.get(name).closed,
        configured: name === 'browser' ? 'extension-installation-required' : !!process.env[`STILLDESK_${name.toUpperCase()}_CONFIG`],
        error: describeError(this.errors.get(name)) })), platformDiscoveryError: describeError(this.errors.get('platforms')),
      clientValidation: 'Protocol and exported configuration checks are distinct from a live harness test. Claude Code is never launched by project verification.' };
  }
  async callTool(name, args, { signal } = {}) {
    if (name === 'stilldesk_status') return { content: [{ type: 'text', text: JSON.stringify(await this.status()) }] };
    if (!this.routes.has(name)) await this.listTools();
    const route = this.routes.get(name);
    if (!route) throw new Error('Unknown or unavailable tool: ' + name);
    if (route.provider === 'platforms') {
      const result = await (await this.platforms()).callTool(route.original, args, { signal });
      return result?.content ? result : { ...(result?.ok === false ? { isError: true } : {}),
        content: [{ type: 'text', text: JSON.stringify(result) }] };
    }
    return (await this.backend(route.provider)).request('tools/call', { name: route.original, arguments: args }, { signal, timeoutMs: 120000 });
  }
  close() { this.closed = true; for (const child of this.children.values()) child.close(); this.children.clear(); }
}
