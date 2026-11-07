import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { ROOT } from '../lib/providers.mjs';
import { publicFiles } from './files.mjs';
const files = await publicFiles();
const problems = [];
for (const file of files) {
  const rel = path.relative(ROOT, file).replaceAll('\\', '/');
  if (/\.(?:db|sqlite(?:3)?|dmp|pem|key|pfx|p12|exe|dll|wav|mp3|mp4)$/i.test(file) || /(?:^|\/)(?:auth|credentials|config\.local)\.json$/i.test(rel)) {
    problems.push({ path: rel, category: 'private-or-binary-file' }); continue;
  }
  if (/\.(?:png|webp|gif|ico)$/i.test(file)) continue;
  const text = await readFile(file, 'utf8');
  const rules = [
    ['private-key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/],
    ['github-token', /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b/],
    ['openai-style-secret', /\bsk-(?:proj-)?[A-Za-z0-9_-]{36,}\b/],
    ['private-user-path', /(?:C:[\\/]+Users[\\/]+yby|D:[\\/]+university|(?:C|D|E|F):\\\\(?:Users\\\\yby|university))/i],
  ];
  for (const [category, regex] of rules) {
    const match = regex.exec(text);
    if (match) problems.push({ path: rel, category, line: text.slice(0, match.index).split('\n').length });
  }
}
console.log(JSON.stringify({ passed: problems.length === 0, examinedFiles: files.length,
  excludesRuntimeAndPrivateState: true, findings: problems }, null, 2));
if (problems.length) process.exitCode = 1;
