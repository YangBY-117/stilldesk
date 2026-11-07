import path from 'node:path';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { ROOT } from '../lib/providers.mjs';
const flags = new Set(process.argv.slice(2));
const known = new Set(['--messaging', '--test-deps', '--help']);
for (const flag of flags) if (!known.has(flag)) throw new Error('Unknown setup option: ' + flag);
if (flags.has('--help')) {
  console.log('node scripts/setup.mjs [--messaging] [--test-deps]\nPrepares the browser host files. Optional messaging installs a private .venv. Does not register native hosts, open login, or change agent configuration.');
} else {
  const run = (command, args) => new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: ROOT, stdio: 'inherit', windowsHide: true, shell: false });
    child.once('error', reject); child.once('exit', code => code === 0 ? resolve() : reject(new Error(`${command} exited (${code})`)));
  });
  await run(process.execPath, [path.join(ROOT, 'packages/browser/scripts/prepare.mjs')]);
  if (flags.has('--messaging') || flags.has('--test-deps')) {
    const environment = path.join(ROOT, '.venv');
    const python = path.join(environment, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    if (!existsSync(python)) await run(process.env.STILLDESK_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-m', 'venv', environment]);
    await run(python, ['-m', 'pip', 'install', '-e', path.join(ROOT, 'integrations/wechat'), '-e', path.join(ROOT, 'integrations/qq'),
      ...(flags.has('--test-deps') ? ['pytest>=8'] : [])]);
  }
  console.log('StillDesk files prepared. Load the browser extension and review native-host registration. Native app account setup remains explicit.');
}
