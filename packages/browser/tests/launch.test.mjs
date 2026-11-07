import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { prepare } from '../scripts/prepare.mjs';
import { ROOT, runtimePaths } from '../lib/transport.mjs';
import { startBroker } from '../broker.mjs';
import { nativeRuntimeArgument, localAbsolutePath } from '../lib/paths.mjs';

test('native runtime accepts only one explicit absolute local path and prioritizes it over fallback',()=>{
  const selected=path.join(os.tmpdir(),'selected-runtime');
  const fallback=path.join(os.tmpdir(),'wrong-runtime');
  assert.equal(nativeRuntimeArgument(['--runtime',selected,'chrome-extension://fixture/'],fallback),selected);
  assert.equal(nativeRuntimeArgument([],fallback),fallback);
  assert.throws(()=>nativeRuntimeArgument(['--runtime','relative'],fallback),/absolute/);
  assert.throws(()=>nativeRuntimeArgument(['--runtime',selected,'--runtime',fallback]),/at most one/);
  assert.throws(()=>nativeRuntimeArgument(['--runtime']),/absolute/);
  assert.throws(()=>localAbsolutePath(selected+'\nunsafe'),/control/);
  if(process.platform==='win32'){
    assert.throws(()=>localAbsolutePath('C:\\state\\%USERPROFILE%'),/percent/);
    assert.throws(()=>localAbsolutePath('C:\\state\\"unsafe'),/quotes/);
    assert.throws(()=>localAbsolutePath('\\\\server\\share\\runtime'),/local drive/);
  }
});

test('generated native launcher pins its setup runtime rather than browser environment',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'stilldesk-launch-plan-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const state=path.join(dir,'separate home','browser'),runtime=path.join(state,'pinned runtime');
  const result=await prepare({state,runtime});
  assert.equal(result.runtimeDirectory,runtime);
  const launcher=await fs.readFile(result.launcherPath,'utf8');
  assert.ok(launcher.includes('--runtime'));
  assert.ok(launcher.includes(runtime));
  assert.ok(!launcher.includes('STILLDESK_HOME')&&!launcher.includes('STILLDESK_BROWSER_RUNTIME'));
  if(process.platform==='win32')assert.ok(launcher.includes(`--runtime "${runtime}"`));
  else assert.ok(launcher.includes('--runtime '));
});

test('pinned launcher and standard MCP roundtrip under nonstandard home without browser environment inheritance',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'stilldesk-launch-wire-'));
  const state=path.join(dir,'nonstandard home','browser');
  const runtime=runtimePaths(path.join(state,'runtime'));
  const built=await prepare({state,runtime:runtime.dir});
  const broker=await startBroker(runtime);
  const children=[];
  t.after(async()=>{
    for(const child of children)if(child.exitCode===null){
      if(process.platform==='win32'){try{execFileSync('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});}catch{}}
      else child.kill();
    }
    await broker.close();await fs.rm(dir,{recursive:true,force:true});
  });
  const origin=`chrome-extension://${built.extensionId}/`;
  const wrong=path.join(dir,'wrong inherited runtime');
  const env={...process.env,STILLDESK_HOME:path.join(dir,'wrong browser home'),STILLDESK_BROWSER_RUNTIME:wrong};
  const native=process.platform==='win32'
    ? spawn(process.env.ComSpec||'cmd.exe',['/d','/s','/c',`""${built.launcherPath}" "${origin}""`],{windowsHide:true,windowsVerbatimArguments:true,stdio:['pipe','pipe','pipe'],env})
    : spawn(built.launcherPath,[origin],{stdio:['pipe','pipe','pipe'],env});
  children.push(native);
  let nativeBytes=Buffer.alloc(0),nativeError='';
  native.stderr.on('data',data=>nativeError+=data);
  native.on('error',error=>{nativeError+=error.message;});
  let observedCommand;
  native.stdout.on('data',data=>{
    nativeBytes=Buffer.concat([nativeBytes,data]);
    while(nativeBytes.length>=4 && nativeBytes.length>=4+nativeBytes.readUInt32LE()){
      const length=nativeBytes.readUInt32LE();
      const message=JSON.parse(nativeBytes.subarray(4,length+4).toString('utf8'));
      nativeBytes=nativeBytes.subarray(length+4);observedCommand=message;
      const answer=Buffer.from(JSON.stringify({type:'result',id:message.id,result:[{pageId:7,url:'https://fixture.invalid/',title:'pinned runtime fixture'}]}));
      const header=Buffer.alloc(4);header.writeUInt32LE(answer.length);native.stdin.write(Buffer.concat([header,answer]));
    }
  });
  const mcp=spawn(process.execPath,[path.join(ROOT,'mcp-server.mjs'),'--runtime',runtime.dir],{windowsHide:true,stdio:['pipe','pipe','pipe'],env});
  children.push(mcp);
  let buffer='';const pending=new Map();
  mcp.stdout.on('data',data=>{buffer+=data;let end;while((end=buffer.indexOf('\n'))>=0){const message=JSON.parse(buffer.slice(0,end));buffer=buffer.slice(end+1);const slot=pending.get(message.id);if(slot){clearTimeout(slot.timer);pending.delete(message.id);slot.resolve(message);}}});
  mcp.stderr.on('data',()=>{});
  const rpc=(id,method,params)=>new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error('MCP launcher fixture timed out: '+nativeError));},15000);
    pending.set(id,{resolve,timer});mcp.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n');
  });
  assert.equal((await rpc(1,'initialize',{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'pinned-launcher-fixture',version:'1'}})).result.serverInfo.name,'stilldesk-browser');
  const until=Date.now()+10000;
  while(!broker.status().nativeConnected){assert.ok(Date.now()<until,nativeError||'pinned host must connect');await new Promise(resolve=>setTimeout(resolve,50));}
  const response=await rpc(2,'tools/call',{name:'list_pages',arguments:{}});
  assert.equal(response.result.isError,undefined);
  assert.equal(JSON.parse(response.result.content[0].text)[0].pageId,7);
  assert.equal(observedCommand.action,'list_pages');
  await assert.rejects(fs.access(wrong),error=>error.code==='ENOENT');
  for(const child of [mcp,native]){
    const exited=new Promise(resolve=>child.once('exit',resolve));child.stdin.end();assert.equal(await exited,0);
  }
});
