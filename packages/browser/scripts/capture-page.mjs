// Render a page in an isolated headless browser and capture its active viewport.
// All page inspection uses the StillDesk extension. There is no debugging link.
import path from 'node:path';
import os from 'node:os';
import { mkdtemp, mkdir, writeFile, stat } from 'node:fs/promises';
import { spawn, execFileSync } from 'node:child_process';
import { prepare } from './prepare.mjs';
import { applyRegistration, rollbackRegistration } from './register-native-host.mjs';
import { LabClient, runtimePaths } from '../lib/transport.mjs';

const flags = Object.fromEntries(process.argv.slice(2).map((value,index,all)=>value.startsWith('--') ? [value.slice(2),all[index+1]] : null).filter(Boolean));
const address = new URL(flags.url);
if (!['http:','https:'].includes(address.protocol)) throw new Error('Use an HTTP/HTTPS page');
const executable = flags.executable || process.env.STILLDESK_BROWSER_TEST_BROWSER;
if (!executable) throw new Error('Pass --executable or set STILLDESK_BROWSER_TEST_BROWSER');
if(!(await stat(executable)).isFile())throw new Error('Browser executable must be a regular file');
const output = path.resolve(flags.output || 'viewport.png');
const width = Number(flags.width || 1440), height = Number(flags.height || 1000);
if (!Number.isInteger(width) || !Number.isInteger(height) || width < 300 || height < 300 || width > 4096 || height > 4096) throw new Error('Viewport dimensions must be integers between 300 and 4096');
const family = flags.browser || 'chrome';
if (!['chrome','edge','chromium'].includes(family)) throw new Error('Unsupported browser family');
const run = await mkdtemp(path.join(os.tmpdir(),'stilldesk-capture-'));
process.env.STILLDESK_HOME = path.join(run,'state');
const runtime = runtimePaths(path.join(run,'runtime'));
process.env.STILLDESK_BROWSER_RUNTIME = runtime.dir;
let child, client, spawnFailure;
const registrations = [];
const wait = ms=>new Promise(resolve=>setTimeout(resolve,ms));
try {
  const built = await prepare();
  for (const browser of (family==='edge'?['edge']:['chrome','chromium'])) registrations.push({browser,receipt:await applyRegistration({browser})});
  child=spawn(executable,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--no-proxy-server','--disable-background-networking','--disable-field-trial-config',
    '--force-device-scale-factor=1',`--window-size=${width},${height}`,`--user-data-dir=${path.join(run,'profile')}`,`--disable-extensions-except=${built.extensionDirectory}`,`--load-extension=${built.extensionDirectory}`,address.href],
    {windowsHide:true,stdio:['ignore','ignore','pipe'],env:process.env});
  let diagnostics='';child.stderr.on('data',c=>{diagnostics=(diagnostics+c).slice(-2000);});
  child.on('error',error=>{spawnFailure=error;});
  client=await LabClient.open({runtime});
  const deadline=Date.now()+30000;
  let tab;
  while(Date.now()<deadline){if(spawnFailure)throw spawnFailure;try{const tabs=await client.request('list_pages');tab=tabs.find(t=>t.url===address.href);if(tab)break;}catch{}await wait(200);}
  if(!tab)throw new Error('Extension did not open the target page. '+diagnostics);
  let snapshot;
  while(Date.now()<deadline){snapshot=await client.request('take_snapshot',{pageId:tab.pageId});if(snapshot.frames?.[0]?.nodes?.length)break;await wait(200);}
  if(!snapshot?.frames?.[0] || snapshot.frames[0].error)throw new Error('Target document could not be inspected through the extension');
  const initialWidth=snapshot.frames?.[0]?.viewport?.width;
  if(initialWidth && initialWidth!==width){
    await client.request('set_zoom',{pageId:tab.pageId,factor:Math.min(5,Math.max(0.25,initialWidth/width))});
    await wait(200);
    snapshot=await client.request('take_snapshot',{pageId:tab.pageId});
  }
  await wait(Number(flags.wait || 1000));
  const shot=await client.request('take_screenshot',{pageId:tab.pageId});
  const match=/^data:image\/png;base64,(.*)$/.exec(shot.dataUrl||'');
  if(!match)throw new Error('Expected a PNG screenshot');
  await mkdir(path.dirname(output),{recursive:true});
  const pixels=Buffer.from(match[1],'base64');
  await writeFile(output,pixels);
  if(flags.snapshot)await writeFile(path.resolve(flags.snapshot),JSON.stringify(snapshot,null,2)+'\n');
  process.stdout.write(JSON.stringify({output,requestedViewport:{width,height},cssViewport:snapshot.frames?.[0]?.viewport,
    documentLayout:snapshot.frames?.[0]?.layout,
    pngPixels:{width:pixels.readUInt32BE(16),height:pixels.readUInt32BE(20)},transport:'native-messaging',remoteDebugging:false,scope:'active viewport inside an isolated headless browser'})+'\n');
} finally {
  client?.close();
  if(child?.exitCode===null){if(process.platform==='win32'){try{execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});}catch{}}else child.kill();}
  const cleanupErrors=[];
  for(const {browser,receipt} of registrations.reverse())if(!receipt.alreadyApplied){try{await rollbackRegistration({browser});}catch(error){cleanupErrors.push(error);}}
  try{const cleanup=await LabClient.open({runtime,autoStart:false});await cleanup.request('shutdown');cleanup.close();}catch{}
  if(cleanupErrors.length)throw new AggregateError(cleanupErrors,'Native host rollback needs attention');
}
