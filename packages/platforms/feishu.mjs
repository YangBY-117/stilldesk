import { execution, findExecutable, runNative, parseNative, PlatformError, object, text, paragraph, integer, isoTime, writePlan } from './runner.mjs';
import { once } from './idempotency.mjs';

const chat = value => text(value, 'chat_id', { required: true, maximum: 256, pattern: /^oc_[A-Za-z0-9_-]+$/ });
const identity = value => { if (value !== undefined && !['user', 'bot'].includes(value)) throw new PlatformError('invalid_argument', 'as must be user or bot'); return value || 'user'; };
const flag = (args, key, value) => { if (value !== undefined && value !== '') args.push(`--${key}=${value}`); };
export function buildFeishuCommand(action, args = {}) {
  const argv = [], as = identity(args.as);
  let plan;
  switch (action) {
    case 'list_chats':
      object(args, ['query', 'page_token', 'page_size', 'as']);
      argv.push('im', '+chat-search', '--format=json', `--as=${as}`, '--disable-search-by-user');
      flag(argv, 'query', text(args.query ?? '', 'query', { maximum: 64 }));
      flag(argv, 'page-token', text(args.page_token, 'page_token'));
      flag(argv, 'page-size', integer(args.page_size, 'page_size', 1, 100, 20)); break;
    case 'get_messages':
      object(args, ['chat_id', 'page_token', 'page_size', 'start', 'end', 'sort', 'as']);
      if (args.sort !== undefined && !['asc', 'desc'].includes(args.sort)) throw new PlatformError('invalid_argument', 'sort must be asc or desc');
      argv.push('im', '+chat-messages-list', '--format=json', `--as=${as}`, `--chat-id=${chat(args.chat_id)}`);
      flag(argv, 'page-token', text(args.page_token, 'page_token'));
      flag(argv, 'page-size', integer(args.page_size, 'page_size', 1, 50, 30));
      flag(argv, 'start', isoTime(args.start, 'start')); flag(argv, 'end', isoTime(args.end, 'end')); flag(argv, 'sort', args.sort ?? 'desc'); break;
    case 'search_docs':
      object(args, ['query', 'page_token', 'page_size', 'as']);
      argv.push('docs', '+search', '--format=json', `--as=${as}`);
      flag(argv, 'query', text(args.query, 'query', { required: true }));
      flag(argv, 'page-token', text(args.page_token, 'page_token'));
      flag(argv, 'page-size', integer(args.page_size, 'page_size', 1, 20, 15)); break;
    case 'get_doc':
      object(args, ['document', 'offset', 'limit', 'as']);
      argv.push('docs', '+fetch', '--format=json', `--as=${as}`);
      flag(argv, 'doc', text(args.document, 'document', { required: true, maximum: 2048 }));
      flag(argv, 'offset', integer(args.offset, 'offset', 0, 1000000, 0)); flag(argv, 'limit', integer(args.limit, 'limit', 1, 1000, 100)); break;
    case 'agenda':
      object(args, ['start', 'end', 'calendar_id', 'as']);
      argv.push('calendar', '+agenda', '--format=json', `--as=${as}`);
      flag(argv, 'start', isoTime(args.start, 'start')); flag(argv, 'end', isoTime(args.end, 'end'));
      flag(argv, 'calendar-id', text(args.calendar_id ?? 'primary', 'calendar_id')); break;
    case 'send_text': {
      object(args, ['chat_id', 'text', 'as', 'dry_run', 'authorized', 'request_id']);
      const recipient = chat(args.chat_id), content = paragraph(args.text, 'text');
      plan = writePlan('feishu.send_text', args, { chat_id: recipient, text: content, as });
      if (plan) return { plan };
      argv.push('im', '+messages-send', `--as=${as}`, `--chat-id=${recipient}`, `--text=${content}`, `--idempotency-key=${args.request_id}`); break;
    }
    default: throw new PlatformError('unsupported_operation', `Unsupported Feishu operation: ${action}`);
  }
  return { argv };
}
function present(value, pattern) {
  if (!value || typeof value !== 'object') return false;
  return Object.entries(value).some(([key, item]) => (pattern.test(key) && !!item) || (item && typeof item === 'object' && present(item, pattern)));
}
export async function feishuStatus({ env = process.env, runner = runNative, executable, signal } = {}) {
  const command = executable || await findExecutable('lark', env);
  const checks = [];
  const outputs = new Map();
  for (const args of [['--version'], ['doctor', '--offline'], ['config', 'show'], ['auth', 'status']]) {
    const result = await runner(command, args, { env, signal });
    checks.push({ command: args.join(' '), exitCode: result.code });
    outputs.set(args.join(' '), result);
  }
  const config = outputs.get('config show'), auth = outputs.get('auth status');
  let configurationPresent = 'unknown', authenticated = 'unknown';
  try { configurationPresent = present(JSON.parse(config.stdout), /app.?id/i); } catch {}
  try {
    const status = JSON.parse(auth.stdout);
    const encoded = JSON.stringify(status);
    if (/"(?:authenticated|logged_in|loggedIn)"\s*:\s*true/.test(encoded)) authenticated = true;
    else if (/"(?:authenticated|logged_in|loggedIn)"\s*:\s*false/.test(encoded) || auth.code !== 0) authenticated = false;
  } catch { if (auth.code !== 0) authenticated = false; }
  return { ok: true, installed: true, version: outputs.get('--version').stdout.match(/\d+\.\d+\.\d+(?:[-+][\w.-]+)?/)?.[0] ?? 'unknown',
    configurationPresent, authenticated, checks, execution, readyForApiCalls: configurationPresent === true && authenticated === true,
    setupRequired: authenticated === false || configurationPresent === false,
    capabilities: ['list_chats', 'get_messages', 'search_docs', 'get_doc', 'agenda', 'send_text'],
    authenticationNote: 'Authentication remains with the official CLI; no login, browser, or app UI is opened by status.' };
}
export async function callFeishu(action, args = {}, options = {}) {
  if (action === 'status') { object(args, []); return feishuStatus(options); }
  const built = buildFeishuCommand(action, args);
  if (built.plan) return built.plan;
  const command = options.executable || await findExecutable('lark', options.env);
  const invoke = async () => {
    const result = await (options.runner || runNative)(command, built.argv, { env: options.env, signal: options.signal });
    return { ok: true, data: parseNative(result), execution, untrusted_data: true,
      ...(action === 'send_text' ? { request_id: args.request_id, deliveryScope: 'service response; not proof of recipient reading' } : {}) };
  };
  return action === 'send_text' ? once('feishu.send_text', args, invoke, options.env) : invoke();
}
