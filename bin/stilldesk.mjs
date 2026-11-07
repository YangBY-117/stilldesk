#!/usr/bin/env node
import path from 'node:path';
import { spawn } from 'node:child_process';
import { ROOT, Providers, pythonExecutable } from '../lib/providers.mjs';
import { runStdio } from '../lib/server.mjs';

function options(argv) {
  const result = { rest: [], uploads: [], downloads: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--allow-upload-root' || argv[i] === '--allow-download-root') {
      if (!argv[i + 1] || !path.isAbsolute(argv[i + 1])) throw new Error(`${argv[i]} requires an absolute directory`);
      result[argv[i] === '--allow-upload-root' ? 'uploads' : 'downloads'].push(argv[++i]);
    } else result.rest.push(argv[i]);
  }
  return result;
}
function execute(command, args, env = {}) {
  const child = spawn(command, args, { cwd: ROOT, env: { ...process.env, ...env }, shell: false, windowsHide: true, stdio: 'inherit' });
  child.once('error', error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
  child.once('exit', code => { process.exitCode = code ?? 1; });
}
try {
  const config = options(process.argv.slice(2));
  const [command = 'help', ...rest] = config.rest;
  if (command === 'mcp') await runStdio(config);
  else if (command === 'browser') execute(process.execPath, [path.join(ROOT, 'packages/browser/cli.mjs'),
    ...config.uploads.flatMap(p => ['--allow-upload-root', p]), ...config.downloads.flatMap(p => ['--allow-download-root', p]), ...rest]);
  else if (command === 'wechat' || command === 'qq') {
    const cfg = process.env[`STILLDESK_${command.toUpperCase()}_CONFIG`];
    execute(pythonExecutable(), ['-X', 'utf8', '-m', `${command}_bridge.cli`, ...(cfg ? ['--config', cfg] : []), ...rest],
      { PYTHONUTF8: '1', PYTHONPATH: path.join(ROOT, 'integrations', command) });
  } else if (command === 'feishu' || command === 'meeting') {
    const { dispatch } = await import('../packages/platforms/index.mjs');
    console.log(JSON.stringify(await dispatch(command, rest[0] || 'status', rest[1] ? JSON.parse(rest[1]) : {}), null, 2));
  } else if (command === 'doctor') {
    const providers = new Providers(config);
    try { await providers.listTools(); console.log(JSON.stringify(await providers.status(), null, 2)); }
    finally { providers.close(); }
  } else if (command === 'config') {
    const { generateConfig, listClients } = await import('../packages/compat/index.mjs');
    if (!rest[0] || rest[0] === 'list') console.log(JSON.stringify(await listClients(), null, 2));
    else { const result = await generateConfig(rest[0], { root: ROOT, python: pythonExecutable(), uploads: config.uploads, downloads: config.downloads });
      process.stdout.write(typeof result === 'string' ? result : rest.includes('--metadata') ? JSON.stringify(result, null, 2) + '\n' : result.content); }
  } else if (command === 'serve') {
    const { serve } = await import('../lib/http.mjs'); await serve(config);
  } else if (command === '--version' || command === 'version') console.log('0.1.0');
  else if (command === 'help' || command === '--help') console.log(`StillDesk — Your cursor stays yours.

  stilldesk doctor                     Check configured providers
  stilldesk mcp                        MCP server over stdio
  stilldesk browser ACTION '{...}'       Native Messaging browser tools
  stilldesk wechat COMMAND              Background local WeChat tools
  stilldesk qq COMMAND                  Background local QQ tools
  stilldesk feishu ACTION '{...}'        Official Feishu API/CLI tools
  stilldesk meeting ACTION '{...}'       Tencent Meeting background tools
  stilldesk config CLIENT               Print portable client configuration
  stilldesk serve                       Authenticated Streamable HTTP MCP

  --allow-upload-root ABSOLUTE_DIR       Permit explicit browser uploads
  --allow-download-root ABSOLUTE_DIR     Permit downloaded-file readback and hashing

No login, app recording, or foreground automation is started by help/doctor.
See docs/capabilities.md for verified behavior and native backend gaps.`);
  else throw new Error('Unknown command. Run stilldesk help.');
} catch (error) { process.stderr.write(error.message + '\n'); process.exitCode = 1; }
