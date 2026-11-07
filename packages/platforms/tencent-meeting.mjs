import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { access } from 'node:fs/promises';
import { once } from './idempotency.mjs';
import { execution, findExecutable, runNative, parseNative, PlatformError, object, text, integer, isoTime, writePlan } from './runner.mjs';

const localRoot = fileURLToPath(new URL('./tencent-meeting/local/', import.meta.url));
const hexScope = /^[a-f0-9]{24}$/;
const flag = (argv, name, value) => { if (value !== undefined && value !== '') argv.push(`--${name}=${value}`); };
const id = (value, name) => text(value, name, { required: true, maximum: 512 });
const sessionId = value => text(value, 'session_id', { required: true, maximum: 128, pattern: /^[A-Za-z0-9_-]+$/ });

export function buildMeetingCloudCommand(action, args = {}) {
  const common = ['account_scope'];
  const argv = [];
  switch (action) {
    case 'recordings_search':
      object(args, [...common, 'query', 'query_field', 'file_type', 'meeting_id', 'meeting_code', 'start', 'end', 'page_token', 'page_size']);
      if (args.query_field !== undefined && !['all', 'subject', 'creator', 'transcript_content', 'smart_minutes', 'timeline'].includes(args.query_field)) throw new PlatformError('invalid_argument', 'Unsupported query_field');
      if (args.file_type !== undefined && !['all', 'video', 'transcript'].includes(args.file_type)) throw new PlatformError('invalid_argument', 'Unsupported file_type');
      argv.push('record', 'search');
      flag(argv, 'query', text(args.query ?? '', 'query')); flag(argv, 'query-field', args.query_field ?? 'all'); flag(argv, 'file-type', args.file_type ?? 'all');
      flag(argv, 'meeting-id', text(args.meeting_id, 'meeting_id')); flag(argv, 'meeting-code', text(args.meeting_code, 'meeting_code'));
      flag(argv, 'start', isoTime(args.start, 'start')); flag(argv, 'end', isoTime(args.end, 'end'));
      flag(argv, 'page-token', text(args.page_token, 'page_token')); flag(argv, 'page-size', integer(args.page_size, 'page_size', 1, 30, 20)); break;
    case 'meeting_get':
      object(args, [...common, 'meeting_id', 'meeting_code']);
      if (Boolean(args.meeting_id) === Boolean(args.meeting_code)) throw new PlatformError('invalid_argument', 'Supply exactly one of meeting_id and meeting_code');
      argv.push('meeting', 'get'); flag(argv, 'meeting-id', text(args.meeting_id, 'meeting_id')); flag(argv, 'meeting-code', text(args.meeting_code, 'meeting_code')); break;
    case 'recording_files':
      object(args, [...common, 'meeting_record_id', 'page_token', 'page_size']);
      argv.push('record', 'address', `--meeting-record-id=${id(args.meeting_record_id, 'meeting_record_id')}`);
      flag(argv, 'page-token', text(args.page_token, 'page_token')); flag(argv, 'page-size', integer(args.page_size, 'page_size', 1, 30, 20)); break;
    case 'transcript_paragraphs':
      object(args, [...common, 'record_file_id', 'meeting_id']);
      argv.push('record', 'transcript-paragraphs', `--record-file-id=${id(args.record_file_id, 'record_file_id')}`);
      flag(argv, 'meeting-id', text(args.meeting_id, 'meeting_id')); break;
    case 'transcript_get':
      object(args, [...common, 'record_file_id', 'meeting_id', 'pid', 'limit']);
      argv.push('record', 'transcript-get', `--record-file-id=${id(args.record_file_id, 'record_file_id')}`, `--pid=${id(args.pid, 'pid')}`);
      flag(argv, 'meeting-id', text(args.meeting_id, 'meeting_id')); flag(argv, 'limit', integer(args.limit, 'limit', 1, 100, 10)); break;
    case 'recording_minutes':
      object(args, [...common, 'record_file_id']);
      argv.push('record', 'smart-minutes', `--record-file-id=${id(args.record_file_id, 'record_file_id')}`, '--lang=default'); break;
    default: throw new PlatformError('unsupported_operation', `Unsupported meeting cloud operation: ${action}`);
  }
  if (args.account_scope !== undefined) text(args.account_scope, 'account_scope', { pattern: hexScope });
  argv.push('--format=json');
  return argv;
}
export function parseMeetingAuth(result) {
  if (result.code !== 0) return { authenticated: false, account_scope: null };
  const match = /OpenId:\s*(\S+)/.exec(result.stdout);
  return { authenticated: Boolean(match) && !/Session has expired|RefreshToken:\s*expired/i.test(result.stdout),
    account_scope: match ? createHash('sha256').update(match[1]).digest('hex').slice(0, 24) : null };
}
async function cloudStatus(options) {
  const executable = options.executable || await findExecutable('tmeet', options.env);
  const status = parseMeetingAuth(await (options.runner || runNative)(executable, ['auth', 'status'], { env: options.env, signal: options.signal }));
  const expected = options.env?.STILLDESK_TMEET_ACCOUNT_SCOPE ?? process.env.STILLDESK_TMEET_ACCOUNT_SCOPE;
  return { ok: true, installed: true, ...status, account_bound: Boolean(expected) && status.account_scope === expected,
    execution, cloudCapabilities: ['recordings_search', 'meeting_get', 'recording_files', 'transcript_paragraphs', 'transcript_get', 'recording_minutes'],
    mutationCapabilities: [], coverage: 'account-accessible records only; no meeting is joined or recorded by this status call' };
}
export async function callMeetingCloud(action, args = {}, options = {}) {
  if (action === 'cloud_status') { object(args, []); return cloudStatus(options); }
  const argv = buildMeetingCloudCommand(action, args);
  const executable = options.executable || await findExecutable('tmeet', options.env);
  const runner = options.runner || runNative;
  const status = parseMeetingAuth(await runner(executable, ['auth', 'status'], { env: options.env, signal: options.signal }));
  if (!status.authenticated) throw new PlatformError('authentication_required', 'Authorize the intended account using the official tmeet client. StillDesk never opens login UI automatically.');
  const expected = args.account_scope || options.env?.STILLDESK_TMEET_ACCOUNT_SCOPE || process.env.STILLDESK_TMEET_ACCOUNT_SCOPE;
  if (!expected) throw new PlatformError('account_binding_required', 'Read cloud_status, then explicitly bind its account_scope before reading meeting data.');
  if (!hexScope.test(expected) || status.account_scope !== expected) throw new PlatformError('account_changed', 'The official client is not using the explicitly bound account');
  const raw = parseNative(await runner(executable, argv, { env: options.env, signal: options.signal }));
  let data = raw?.data ?? raw;
  if (typeof data === 'string') { try { data = JSON.parse(data); } catch { throw new PlatformError('unexpected_response', 'Tencent nested data is not JSON'); } }
  if (!data || typeof data !== 'object' || Array.isArray(data) || (data.code !== undefined && ![0, '0', null].includes(data.code))) throw new PlatformError('unexpected_response', 'Tencent response is not a successful data object');
  const token = data.next_page_token;
  if (token !== undefined && typeof token !== 'string') throw new PlatformError('invalid_pagination', 'Tencent returned an invalid page token');
  if (data.has_more !== undefined && typeof data.has_more !== 'boolean') throw new PlatformError('invalid_pagination', 'Tencent has_more is invalid');
  if ((data.has_more === true && !token) || (data.has_more === false && token) || (token && token === args.page_token)) throw new PlatformError('invalid_pagination', 'Tencent pagination is contradictory or repeats the input cursor');
  return { ok: true, data, execution, untrusted_data: true, coverage: 'this service response only', all_history_complete: false,
    ...(action === 'recording_minutes' ? { content_kind: 'Tencent generated summary, not verbatim transcript' } : {}),
    ...(token !== undefined || data.has_more !== undefined ? { has_more: Boolean(token), next_page_token: token || null } : {}) };
}

export function buildMeetingLocalCommand(action, args = {}) {
  const argv = [];
  switch (action) {
    case 'local_status': case 'list_sessions': case 'devices':
      object(args, []); argv.push(action === 'local_status' ? 'doctor' : action === 'devices' ? 'devices' : 'sessions'); break;
    case 'session_status':
      object(args, ['session_id']); argv.push('status', sessionId(args.session_id)); break;
    case 'read_transcript':
      object(args, ['session_id', 'after_seq', 'limit', 'wait_seconds']);
      argv.push('transcript', sessionId(args.session_id), '--after-seq', String(integer(args.after_seq, 'after_seq', 0, 100000000, 0)), '--limit', String(integer(args.limit, 'limit', 1, 500, 100)), '--wait-seconds', String(integer(args.wait_seconds, 'wait_seconds', 0, 20, 0))); break;
    case 'read_events':
      object(args, ['session_id', 'after_event_seq']); argv.push('events', sessionId(args.session_id), '--after-event-seq', String(integer(args.after_event_seq, 'after_event_seq', 0, 100000000, 0))); break;
    case 'pause': case 'resume': case 'stop':
      object(args, ['session_id', 'dry_run', 'authorized', 'request_id']);
      { const session = sessionId(args.session_id); const plan = writePlan(`meeting.${action}`, args, { session_id: session }); if (plan) return { plan }; argv.push(action, session); } break;
    case 'start':
      object(args, ['title', 'include_microphone', 'target_pid', 'save_audio', 'dry_run', 'authorized', 'request_id']);
      { const title = id(args.title, 'title');
        if (args.include_microphone !== undefined && typeof args.include_microphone !== 'boolean') throw new PlatformError('invalid_argument', 'include_microphone must be boolean');
        if (args.save_audio !== undefined && typeof args.save_audio !== 'boolean') throw new PlatformError('invalid_argument', 'save_audio must be boolean');
        const target = integer(args.target_pid, 'target_pid', 1, 2147483647);
        const plan = writePlan('meeting.start', args, { title, target_pid: target, include_microphone: args.include_microphone === true, save_audio: args.save_audio === true });
        if (plan) return { plan };
        argv.push('start', '--title', title, '--target-pid', String(target));
        argv.push(args.include_microphone === true ? '--microphone' : '--no-microphone');
        if (args.save_audio === true) argv.push('--save-audio');
      } break;
    case 'export':
      object(args, ['session_id', 'format', 'dry_run', 'authorized', 'request_id']);
      { const session = sessionId(args.session_id), format = args.format || 'markdown';
        if (!['markdown', 'json', 'text', 'srt'].includes(format)) throw new PlatformError('invalid_argument', 'Invalid export format');
        const plan = writePlan('meeting.export', args, { session_id: session, format }); if (plan) return { plan };
        argv.push('export', session, '--format', format);
      } break;
    default: throw new PlatformError('unsupported_operation', `Unsupported local meeting operation: ${action}`);
  }
  return { argv };
}
export async function callMeetingLocal(action, args = {}, options = {}) {
  const built = buildMeetingLocalCommand(action, args);
  if (built.plan) return built.plan;
  const env = options.env || process.env;
  const root = env.STILLDESK_MEETING_LOCAL_ROOT || localRoot;
  if (!path.isAbsolute(root)) throw new PlatformError('invalid_configuration', 'STILLDESK_MEETING_LOCAL_ROOT must be absolute');
  const script = path.join(root, 'runner.py');
  try { await access(script); } catch { throw new PlatformError('not_configured', 'The local meeting source package is missing'); }
  const python = env.STILLDESK_PYTHON || (process.platform === 'win32' ? 'python.exe' : 'python3');
  const invoke = async () => {
    const raw = await (options.runner || runNative)(python, ['-X', 'utf8', script, ...built.argv], { env, cwd: root, signal: options.signal });
    const parsed = parseNative(raw);
    if (parsed?.ok === false) throw new PlatformError('local_backend_error', parsed.error?.message || 'The local meeting backend could not complete the operation');
    return { ok: true, data: parsed, execution: { ...execution, mode: 'local-process-audio-and-session-storage' },
      untrusted_data: true, captureStarted: action === 'start',
      verificationScope: 'A successful command is not proof of complete meeting coverage; inspect transcript and coverage events.' };
  };
  return ['start', 'pause', 'resume', 'stop', 'export'].includes(action) ? once(`meeting.${action}`, args, invoke, env) : invoke();
}
