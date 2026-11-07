import test from 'node:test';
import assert from 'node:assert/strict';

const calls=[];
let active=true;
globalThis.chrome={tabs:{
  async get(id){return {id,active,url:'https://example.test/',windowId:1};},
  async create(options){calls.push(['create',options]);return {id:2,pendingUrl:options.url};},
  async remove(id){calls.push(['remove',id]);},
  async captureVisibleTab(){calls.push(['capture']);return 'unreachable';}
}};
const {dispatch}=await import('../extension/actions.js');

test('new tabs default to background and explicit foreground requests are refused',async()=>{
  calls.length=0;
  assert.equal((await dispatch('new_page',{url:'https://example.test/'})).foregroundChanged,false);
  assert.equal(calls[0][1].active,false);
  await assert.rejects(dispatch('new_page',{url:'https://example.test/',background:false}),e=>e.code==='foreground_not_allowed');
  assert.equal(calls.length,1);
});
test('logical selection never calls tab activation or a window API',async()=>{
  calls.length=0;
  const result=await dispatch('select_page',{pageId:1});
  assert.equal(result.foregroundChanged,false);assert.equal(result.selected,true);
  assert.equal(calls.length,0);
});
test('active tabs and inactive screenshots are protected without changing focus',async()=>{
  calls.length=0;active=true;
  await assert.rejects(dispatch('close_page',{pageId:1}),e=>e.code==='active_tab_protected');
  active=false;
  await assert.rejects(dispatch('take_screenshot',{pageId:1}),e=>e.code==='background_screenshot_unsupported');
  assert.equal(calls.length,0);
  assert.equal((await dispatch('close_page',{pageId:1})).closed,true);
  assert.deepEqual(calls,[['remove',1]]);
});
test('native dialog capability fails explicitly without touching browser UI',async()=>{
  calls.length=0;
  await assert.rejects(dispatch('handle_dialog',{pageId:1}),e=>e.code==='native_dialog_unsupported');
  assert.equal(calls.length,0);
});
