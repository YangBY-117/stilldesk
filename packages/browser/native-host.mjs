import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { connect, runtimePaths, messages, send, MAX_MESSAGE, ROOT } from './lib/transport.mjs';
import { nativeRuntimeArgument } from './lib/paths.mjs';

// Chrome launches this host using the manifest's exact allowed_origins list.
// Check the launch origin too; it is never used as a path or command argument.
const manifest = JSON.parse(await fs.readFile(path.join(ROOT, 'extension', 'manifest.json'), 'utf8'));
const keyHash = crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16);
const extensionId = [...keyHash].map(v => String.fromCharCode(97 + (v >> 4), 97 + (v & 15))).join('');
const expectedOrigin = `chrome-extension://${extensionId}/`;
const arguments_ = process.argv.slice(2);
const origin = arguments_.find(arg => arg.startsWith('chrome-extension://'));
if (origin !== expectedOrigin) {
  process.stderr.write('Native host launch origin is not authorized\n');
  process.exit(1);
}
let input = Buffer.alloc(0), expected;
const selectedRuntime = nativeRuntimeArgument(arguments_,process.env.STILLDESK_BROWSER_RUNTIME);
const socket = await connect(runtimePaths(selectedRuntime), 'native');
const stop = messages(socket, message => {
  if (message.type !== 'command') return;
  const bytes = Buffer.from(JSON.stringify(message));
  if (bytes.length > 1024 * 1024 - 1) {
    send(socket, { type: 'result', id: message.id, error: 'Native message exceeds the Chrome 1 MB input limit' }); return;
  }
  const header = Buffer.alloc(4); header.writeUInt32LE(bytes.length);
  process.stdout.write(Buffer.concat([header, bytes]));
}, error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; process.stdin.destroy(); });
socket.on('error', error => { process.stderr.write(error.message + '\n'); process.stdin.destroy(); });
socket.on('close', () => { stop(); process.stdin.destroy(); });
process.stdin.on('data', chunk => {
  input = Buffer.concat([input, chunk]);
  try {
    while (true) {
      if (expected === undefined) {
        if (input.length < 4) break;
        expected = input.readUInt32LE(); input = input.subarray(4);
        if (expected < 1 || expected > MAX_MESSAGE) throw new Error('Invalid native message length');
      }
      if (input.length < expected) break;
      const message = JSON.parse(input.subarray(0, expected).toString('utf8'));
      input = input.subarray(expected); expected = undefined;
      if (message.type === 'result' || message.type === 'hello') send(socket, message);
    }
  } catch (error) { process.stderr.write(error.message + '\n'); socket.destroy(); process.stdin.destroy(); process.exitCode = 1; }
});
process.stdin.on('end', () => socket.end());
