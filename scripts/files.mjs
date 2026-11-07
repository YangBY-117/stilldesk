import path from 'node:path';
import { readdir, lstat } from 'node:fs/promises';
import { ROOT } from '../lib/providers.mjs';
export const EXCLUDED = new Set(['.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.private', '.local',
  '.runtime', 'runtime', 'sessions', 'models', 'dist', 'test_artifacts', 'test-artifacts']);
export async function publicFiles(root = ROOT) {
  const result = [];
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory() && (EXCLUDED.has(entry.name) || entry.name.endsWith('.egg-info'))) continue;
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink() || (await lstat(file)).isSymbolicLink()) throw new Error('Public tree contains a symlink: ' + path.relative(root, file));
      if (entry.isDirectory()) await visit(file);
      else if (entry.isFile()) result.push(file);
    }
  }
  await visit(root);
  return result.sort();
}
