import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, mkdir, writeFile, stat } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { callTool, dispatch, listTools } from '../index.mjs';
import { buildFeishuCommand, callFeishu, feishuStatus } from '../feishu.mjs';
import { buildMeetingCloudCommand, buildMeetingLocalCommand, callMeetingCloud, callMeetingLocal } from '../tencent-meeting.mjs';
import { runNative } from '../runner.mjs';
import { restrictPrivatePath } from '../private-state.mjs';

const result = data => ({ code: 0, stdout: JSON.stringify(data), stderr: '' });
const auth = { code: 0, stdout: 'OpenId: fixture-account-only\nRefreshToken: valid\n', stderr: '' };
const scope = createHash('sha256').update('fixture-account-only').digest('hex').slice(0, 24);
test('tool catalog has fixed schemas and no arbitrary command/login/UI entry point', () => {
  const tools = listTools();
  assert.equal(new Set(tools.map(t => t.name)).size, tools.length);
  assert.equal(tools.length, 25);
  assert(tools.every(t => t.inputSchema.additionalProperties === false));
  assert(!tools.some(t => /login|evaluate|shell|foreground|mouse|keyboard/.test(t.name)));
});
test('Feishu preview is useful without credentials and cannot run a subprocess', async () => {
  let calls = 0;
  const preview = await callFeishu('send_text', { chat_id: 'oc_fixture', text: 'Fixture message\nSecond line' }, { runner: () => { calls++; throw Error('must not run'); } });
  assert.equal(preview.executed, false); assert.equal(preview.dry_run, true); assert.equal(calls, 0);
  assert.equal(preview.plan.chat_id, 'oc_fixture');
});
test('Feishu writes need explicit authorization and preserve the idempotency key', () => {
  assert.throws(() => buildFeishuCommand('send_text', { chat_id: 'oc_fixture', text: 'demo', dry_run: false }), /authorization/i);
  assert.throws(() => buildFeishuCommand('send_text', { chat_id: 'oc_fixture', text: 'demo', dry_run: false, authorized: true, request_id: 'short' }), /request_id/);
  const command = buildFeishuCommand('send_text', { chat_id: 'oc_fixture', text: 'demo; $(echo injection)', dry_run: false, authorized: true, request_id: 'fixture-request-01' });
  assert(command.argv.includes('--text=demo; $(echo injection)'));
  assert(command.argv.includes('--idempotency-key=fixture-request-01'));
  assert.equal(command.argv[0], 'im');
});
test('Feishu pagination, timezones, enum and unknown flag boundaries are enforced', () => {
  const command = buildFeishuCommand('get_messages', { chat_id: 'oc_fixture', page_token: 'literal;touch-file', page_size: 5, start: '2026-11-07T00:00:00+08:00' });
  assert(command.argv.includes('--page-token=literal;touch-file'));
  assert.throws(() => buildFeishuCommand('get_messages', { chat_id: 'oc_fixture', start: '2026-11-07T00:00:00' }), /timezone/);
  assert.throws(() => buildFeishuCommand('get_messages', { chat_id: 'oc_fixture', page_size: 500 }), /integer/);
  assert.throws(() => buildFeishuCommand('agenda', { command: 'delete' }), /Unknown/);
  assert.throws(() => buildFeishuCommand('anything', {}), /Unsupported/);
});
test('Feishu status executes only the four diagnostic commands and never exports credentials', async () => {
  const calls = [];
  const secret = 'fixture-sensitive-value-must-be-absent';
  const status = await feishuStatus({ executable: '/fixture/lark', runner: async (_exe, args) => {
    calls.push(args);
    if (args[0] === '--version') return { code: 0, stdout: 'lark-cli version 1.0.4', stderr: '' };
    if (args[0] === 'config') return result({ app_id: 'fixture-app', app_secret: secret });
    if (args[0] === 'auth') return result({ authenticated: true, access_token: secret });
    return result({ ok: true });
  } });
  assert.deepEqual(calls, [['--version'], ['doctor', '--offline'], ['config', 'show'], ['auth', 'status']]);
  assert.equal(status.configurationPresent, true); assert.equal(status.authenticated, true);
  assert(!JSON.stringify(status).includes(secret)); assert.equal(status.execution.foregroundRequired, false);
});
test('Tencent cloud only reads the explicitly bound account; a failed call is not an empty history', async () => {
  const calls = [];
  const options = { executable: '/fixture/tmeet', runner: async (_exe, args) => { calls.push(args); return args[0] === 'auth' ? auth : result({ data: { items: [{ id: 'fixture' }], next_page_token: 'page2', has_more: true } }); } };
  await assert.rejects(callMeetingCloud('recordings_search', {}, options), e => e.code === 'account_binding_required');
  assert.equal(calls.length, 1);
  await assert.rejects(callMeetingCloud('recordings_search', { account_scope: '0'.repeat(24) }, options), e => e.code === 'account_changed');
  const output = await callMeetingCloud('recordings_search', { account_scope: scope, page_size: 2 }, options);
  assert.equal(output.next_page_token, 'page2'); assert.equal(output.all_history_complete, false);
  assert(calls.at(-1).includes('--page-size=2')); assert.equal(output.execution.uiFallback, false);
});
test('Tencent pagination contradictions and guessed arbitrary command names are rejected', async () => {
  const options = { executable: '/fixture/tmeet', runner: async (_exe, args) => args[0] === 'auth' ? auth : result({ data: { has_more: true, next_page_token: '' } }) };
  await assert.rejects(callMeetingCloud('recordings_search', { account_scope: scope }, options), e => e.code === 'invalid_pagination');
  assert.throws(() => buildMeetingCloudCommand('meeting_create', {}), /Unsupported/);
  assert.throws(() => buildMeetingCloudCommand('meeting_get', { meeting_id: 'a', meeting_code: 'b' }), /exactly one/);
  assert.throws(() => buildMeetingCloudCommand('recordings_search', { argv: ['delete'] }), /Unknown/);
});
test('Local capture preview cannot start capture; mic and audio retention default off', async () => {
  let calls = 0;
  const preview = await callMeetingLocal('start', { title: 'Synthetic fixture', target_pid: 1234 }, { runner: () => { calls++; throw Error('must not run'); } });
  assert.equal(calls, 0); assert.equal(preview.plan.include_microphone, false); assert.equal(preview.plan.save_audio, false);
  assert.equal(buildMeetingLocalCommand('devices', {}).argv[0], 'devices');
  assert.throws(() => buildMeetingLocalCommand('start', { title: 'x', target_pid: 1234, dry_run: false }), /authorization/i);
  assert.throws(() => buildMeetingLocalCommand('read_transcript', { session_id: '../../private' }), /session_id/);
  assert.throws(() => buildMeetingLocalCommand('start', { title: 'x' }), /target_pid/);
});
test('Local mutation receipts prevent duplicate execution and reject request ID conflicts', async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-platform-fixture-'));
  let calls = 0;
  const options = { env: { ...process.env, STILLDESK_HOME: home }, runner: async (_exe, args) => {
    calls++; assert(args.includes('--no-microphone')); return result({ session_id: 'fixture-session', state: 'starting' });
  } };
  const args = { title: 'Synthetic only', target_pid: 1234, dry_run: false, authorized: true, request_id: 'fixture-start-0001' };
  try {
    assert.equal((await callMeetingLocal('start', args, options)).captureStarted, true);
    assert.equal((await callMeetingLocal('start', args, options)).replayedReceipt, true);
    assert.equal(calls, 1);
    await assert.rejects(callMeetingLocal('start', { ...args, title: 'different' }, options), e => e.code === 'request_id_conflict');
  } finally { await rm(home, { recursive: true, force: true }); }
});
test('Native stdout budgets and cancellation are enforced without a shell', async () => {
  await assert.rejects(runNative(process.execPath, ['-e', 'process.stdout.write("x".repeat(10000))'], { maxBytes: 100 }), e => e.code === 'response_too_large');
  const controller = new AbortController(); controller.abort();
  await assert.rejects(runNative(process.execPath, ['-e', 'throw Error("must not run")'], { signal: controller.signal }), e => e.code === 'cancelled');
});
test('Concurrent local mutation calls cannot replay an in-flight request', async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-platform-race-'));
  let calls = 0, release, markStarted;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { markStarted = resolve; });
  const options = { env: { ...process.env, STILLDESK_HOME: home }, runner: async () => {
    calls++; markStarted(); await gate; return result({ session_id: 'fixture-session', state: 'starting' });
  } };
  const args = { title: 'Synthetic concurrency fixture', target_pid: 1234, dry_run: false, authorized: true, request_id: 'fixture-concurrent-01' };
  let first;
  try {
    first = callMeetingLocal('start', args, options);
    await Promise.race([started, first.then(() => { throw new Error('Request finished before fixture execution'); })]);
    await assert.rejects(callMeetingLocal('start', args, options), e => e.code === 'outcome_unknown');
    assert.equal(calls, 1); release(); await first;
    assert.equal((await callMeetingLocal('start', args, options)).replayedReceipt, true); assert.equal(calls, 1);
  } finally { release(); await first?.catch(() => {}); await rm(home, { recursive: true, force: true }); }
});
test('Missing native setup is an explicit capability failure, never a success or UI fallback', async () => {
  const output = await dispatch('feishu', 'list_chats', {}, { env: { PATH: '', APPDATA: '/nonexistent-fixture', STILLDESK_LARK_CLI: path.resolve('missing-native-fixture.exe') } });
  assert.equal(output.ok, false); assert.equal(output.error.code, 'not_configured'); assert.equal(output.execution.uiFallback, false);
  assert.equal((await callTool('made_up_tool', {})).error.code, 'unknown_tool');
});
test('Private request storage removes explicit Everyone grants on reused paths', async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-private-fixture-'));
  const folder = path.join(home, 'reused'), file = path.join(folder, 'fixture.json');
  await mkdir(folder); await writeFile(file, '{}');
  try {
    if (process.platform === 'win32') execFileSync('icacls.exe', [folder, '/grant', '*S-1-1-0:(OI)(CI)F', '/T'], { windowsHide: true, stdio: 'pipe' });
    await restrictPrivatePath(folder, true); await restrictPrivatePath(file, false);
    if (process.platform === 'win32') {
      const script = `$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $expected=@([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18'); $checks=@(); foreach($entry in @(@{p=$env:STILLDESK_FIXTURE_DIR;d=$true},@{p=$env:STILLDESK_FIXTURE_FILE;d=$false})){ if($entry.d){$acl=[System.IO.Directory]::GetAccessControl($entry.p)}else{$acl=[System.IO.File]::GetAccessControl($entry.p)}; $rules=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])); $bad=@($rules|Where-Object{$expected -notcontains $_.IdentityReference.Value}); $checks+=@{protected=$acl.AreAccessRulesProtected; count=$rules.Count; unexpected=$bad.Count} }; ConvertTo-Json -InputObject $checks -Compress`;
      const raw = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
        { windowsHide: true, encoding: 'utf8', env: { ...process.env, STILLDESK_FIXTURE_DIR: folder, STILLDESK_FIXTURE_FILE: file } });
      const checks = JSON.parse(raw); assert.equal(checks.length, 2);
      for (const check of checks) assert.deepEqual(check, { protected: true, count: 2, unexpected: 0 });
    } else {
      assert.equal((await stat(folder)).mode & 0o777, 0o700); assert.equal((await stat(file)).mode & 0o777, 0o600);
    }
  } finally { await rm(home, { recursive: true, force: true }); }
});
test('Feishu ledger binds content and never retries a completed send with a changed request', async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), 'stilldesk-feishu-ledger-'));
  let calls = 0;
  const options = { executable: '/fixture/lark', env: { ...process.env, STILLDESK_HOME: home }, runner: async () => { calls++; return result({ message_id: 'fixture-message' }); } };
  const args = { chat_id: 'oc_fixture', text: 'Synthetic only', dry_run: false, authorized: true, request_id: 'fixture-feishu-0001' };
  try {
    assert.equal((await callFeishu('send_text', args, options)).ok, true);
    assert.equal((await callFeishu('send_text', args, options)).replayedReceipt, true); assert.equal(calls, 1);
    await assert.rejects(callFeishu('send_text', { ...args, text: 'changed' }, options), e => e.code === 'request_id_conflict');
    assert.equal(calls, 1);
  } finally { await rm(home, { recursive: true, force: true }); }
});
