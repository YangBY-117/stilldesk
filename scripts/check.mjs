import path from 'node:path';
import { readFile, access } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { ROOT, pythonExecutable } from '../lib/providers.mjs';
import { publicFiles } from './files.mjs';
const files = await publicFiles();
const errors = [];
let javascript = 0, python = 0, json = 0, links = 0;
for (const file of files) {
  const rel = path.relative(ROOT, file);
  if (/\.(?:mjs|js)$/.test(file)) {
    const check = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8', windowsHide: true });
    if (check.status !== 0) errors.push({ file: rel, kind: 'javascript-syntax', message: check.stderr.trim() });
    javascript++;
  } else if (file.endsWith('.json')) {
    try { JSON.parse(await readFile(file, 'utf8')); } catch (e) { errors.push({ file: rel, kind: 'json-syntax', message: e.message }); }
    json++;
  }
  if (file.endsWith('.md')) {
    const text = await readFile(file, 'utf8');
    for (const match of text.matchAll(/\[[^\]\n]+\]\(([^)\s]+)(?:\s+[^)]*)?\)/g)) {
      const target = match[1].replace(/^<|>$/g, '').split('#')[0];
      if (!target || /^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('//')) continue;
      links++;
      try { await access(path.resolve(path.dirname(file), decodeURIComponent(target))); }
      catch { errors.push({ file: rel, kind: 'local-link', target }); }
    }
  }
}
const pythonFiles = files.filter(p => p.endsWith('.py'));
if (pythonFiles.length) {
  const source = 'import ast,json,sys\nerrors=[]\nfor f in json.load(sys.stdin):\n try:\n  ast.parse(open(f,encoding="utf-8-sig").read(),filename=f)\n except Exception as e: errors.append({"file":f,"error":str(e)})\nprint(json.dumps(errors))\nsys.exit(bool(errors))\n';
  const check = spawnSync(pythonExecutable(), ['-X', 'utf8', '-c', source], { input: JSON.stringify(pythonFiles), encoding: 'utf8', windowsHide: true });
  if (check.status !== 0) errors.push({ kind: 'python-syntax', message: check.stdout || check.stderr });
  python = pythonFiles.length;
}
console.log(JSON.stringify({ passed: errors.length === 0, javascript, python, json, localLinks: links, errors }, null, 2));
if (errors.length) process.exitCode = 1;
