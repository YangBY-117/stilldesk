import path from 'node:path';

export function localAbsolutePath(value, label='Path') {
  if(typeof value!=='string' || !value || /[\0\r\n]/.test(value) || !path.isAbsolute(value)) throw new Error(`${label} must be an absolute local filesystem path without control characters`);
  if(process.platform==='win32' && (!/^[a-z]:[\\/]/i.test(value) || /["%]/.test(value))) {
    throw new Error(`${label} must use an absolute local drive path without quotes or percent expansion`);
  }
  return path.normalize(value);
}

export function nativeRuntimeArgument(args, fallback) {
  const positions=args.flatMap((value,index)=>value==='--runtime'?[index]:[]);
  if(positions.length>1 || args.some(value=>value.startsWith('--runtime='))) throw new Error('Supply at most one --runtime option with a separate absolute path argument');
  if(positions.length && args[positions[0]+1]===undefined) throw new Error('Native host runtime must be an absolute local filesystem path');
  const value=positions.length ? args[positions[0]+1] : fallback;
  return value===undefined ? undefined : localAbsolutePath(value,'Native host runtime');
}
