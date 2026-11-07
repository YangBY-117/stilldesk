import { createHash, generateKeyPairSync } from 'node:crypto';
import { mkdir, readFile, writeFile, chmod } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dataDirectory } from '../lib/transport.mjs';
import { localAbsolutePath } from '../lib/paths.mjs';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const HOST_NAME = 'com.stilldesk.browser';

export function extensionIdFromKey(key) {
  const digest = createHash('sha256').update(Buffer.from(key, 'base64')).digest().subarray(0, 16);
  return [...digest].map(value => String.fromCharCode(97 + (value >> 4), 97 + (value & 15))).join('');
}

export async function prepare({ root = ROOT, nodePath = process.execPath, state = dataDirectory(), runtime = process.env.STILLDESK_BROWSER_RUNTIME || path.join(state,'runtime') } = {}) {
  root=localAbsolutePath(root,'Browser package directory');
  state=localAbsolutePath(state,'Browser state directory');
  nodePath=localAbsolutePath(nodePath,'Node executable');
  const runtimeDirectory=localAbsolutePath(runtime,'Native host runtime');
  const extensionDirectory = path.join(root, 'extension');
  const manifestPath = path.join(extensionDirectory, 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  if (!manifest.key) {
    const { publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
    manifest.key = publicKey.export({ type: 'spki', format: 'der' }).toString('base64');
    await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  }
  const extensionId = extensionIdFromKey(manifest.key);
  const nativeDirectory = path.join(state, 'native');
  await mkdir(nativeDirectory, { recursive: true });
  const launcherPath = path.join(nativeDirectory, process.platform === 'win32' ? 'host.cmd' : 'host.sh');
  // Chrome supplies only its trusted origin/window arguments here. Neither a
  // web page nor an MCP tool can choose arguments for this launcher.
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
  const launcher = process.platform === 'win32'
    ? `@echo off\r\nsetlocal DisableDelayedExpansion\r\n"${nodePath}" "${path.join(root, 'native-host.mjs')}" --runtime "${runtimeDirectory}" %*\r\n`
    : `#!/bin/sh\nexec ${quote(nodePath)} ${quote(path.join(root, 'native-host.mjs'))} --runtime ${quote(runtimeDirectory)} "$@"\n`;
  await writeFile(launcherPath, launcher, 'utf8');
  if (process.platform !== 'win32') await chmod(launcherPath, 0o700);
  const hostManifestPath = path.join(nativeDirectory, 'host-manifest.json');
  await writeFile(hostManifestPath, JSON.stringify({
    name: HOST_NAME,
    description: 'StillDesk Browser experimental native bridge (no debugger)',
    path: launcherPath,
    type: 'stdio',
    allowed_origins: [`chrome-extension://${extensionId}/`],
  }, null, 2) + '\n', 'utf8');
  return { version: '0.2.0', hostName: HOST_NAME, extensionId, extensionDirectory, hostManifestPath, launcherPath, runtimeDirectory };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  prepare().then(result => process.stdout.write(JSON.stringify(result, null, 2) + '\n')).catch(error => {
    process.stderr.write(`Preparation failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
