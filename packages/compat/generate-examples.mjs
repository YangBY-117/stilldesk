import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { generateConfig, generatePluginManifest, listClients } from './index.mjs';

const directory = fileURLToPath(new URL('./examples/', import.meta.url));
await mkdir(directory, { recursive: true });
for (const client of listClients()) {
  const example = generateConfig(client.id, { root: '/path/to/stilldesk' });
  await writeFile(path.join(directory, `${client.id}.${example.format}`), example.content, 'utf8');
}
await writeFile(path.join(directory, 'plugin.json'), JSON.stringify(generatePluginManifest(), null, 2) + '\n');
process.stdout.write(`Generated ${listClients().length} configuration examples without starting any harness.\n`);
