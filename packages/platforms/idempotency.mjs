import { mkdir, readFile, writeFile, rename, link, unlink } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { PlatformError } from './runner.mjs';
import { restrictPrivatePath } from './private-state.mjs';

export async function once(action, args, invoke, env = process.env) {
  const home = env.STILLDESK_HOME || path.join(os.homedir(), '.stilldesk');
  if (!path.isAbsolute(home)) throw new PlatformError('invalid_configuration', 'STILLDESK_HOME must be absolute');
  const directory = path.join(home, 'platforms', 'requests');
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await restrictPrivatePath(directory, true);
  const key = createHash('sha256').update(action + '\0' + args.request_id).digest('hex');
  const parameters = Object.fromEntries(Object.entries(args).filter(([k]) => !['authorized', 'dry_run'].includes(k)).sort(([a], [b]) => a.localeCompare(b)));
  const fingerprint = createHash('sha256').update(JSON.stringify(parameters)).digest('hex');
  const file = path.join(directory, key + '.json');
  const pendingFile = path.join(directory, key + '.' + randomUUID() + '.pending');
  try {
    await writeFile(pendingFile, JSON.stringify({ action, fingerprint, state: 'pending' }), { flag: 'wx', mode: 0o600 });
    await restrictPrivatePath(pendingFile, false);
    // Publish a complete record. A concurrent caller must never parse an empty file.
    await link(pendingFile, file);
  }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    await restrictPrivatePath(file, false);
    let old;
    try { old = JSON.parse(await readFile(file, 'utf8')); }
    catch { throw new PlatformError('outcome_unknown', 'The existing request receipt is unreadable; it was not replayed. Inspect the native result before retrying.'); }
    if (old.fingerprint !== fingerprint) throw new PlatformError('request_id_conflict', 'This request_id already belongs to different arguments');
    if (old.state !== 'complete') throw new PlatformError('outcome_unknown', 'This request may already have run. Inspect its session before any retry; it was not replayed.');
    return { ...old.result, replayedReceipt: true };
  } finally { await unlink(pendingFile).catch(() => {}); }
  // A failed/terminated process leaves pending state: an unknown write is never replayed.
  const result = await invoke();
  const temporary = path.join(directory, key + '.' + randomUUID() + '.tmp');
  await writeFile(temporary, JSON.stringify({ action, fingerprint, state: 'complete', result }), { flag: 'wx', mode: 0o600 });
  await restrictPrivatePath(temporary, false);
  await rename(temporary, file);
  return result;
}
