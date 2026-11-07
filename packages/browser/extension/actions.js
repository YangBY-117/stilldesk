const uploads = new Map();
const LIMIT = 32 * 1024 * 1024;
const CHUNK = 192 * 1024;
const TTL = 120000;
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };

async function downloadInfo(downloadId) {
  const [item] = await chrome.downloads.search({ id: integer(downloadId, 'downloadId') });
  if (!item) fail('download_not_found', 'This browser does not contain the requested download');
  return { downloadId: item.id, url: item.finalUrl || item.url, filename: item.filename,
    state: item.state, complete: item.state === 'complete', bytesReceived: item.bytesReceived,
    totalBytes: item.totalBytes, error: item.error, danger: item.danger,
    fileHashVerified: false, foregroundChanged: false };
}
async function startDownload(args) {
  const source = url(args.url);
  if (args.filename !== undefined && (typeof args.filename !== 'string' || !args.filename || /[\\/\0]/.test(args.filename))) fail('invalid_filename', 'Use a file basename only');
  const downloadId = await chrome.downloads.download({ url: source, saveAs: false,
    ...(args.filename ? { filename: args.filename } : {}), conflictAction: 'uniquify' });
  const timeoutMs = Math.min(60000, Math.max(1000, args.timeoutMs || 30000));
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const info = await downloadInfo(downloadId);
    if (info.complete) return info;
    if (info.state === 'interrupted') fail('download_interrupted', info.error || 'The browser interrupted the download');
    if (info.danger && !['safe', 'accepted', 'allowlistedByPolicy', 'not_dangerous'].includes(info.danger)) fail('download_requires_user', 'The browser requires the user to review this download; no UI was opened by StillDesk');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return { ...(await downloadInfo(downloadId)), timedOut: true, outcome: 'Inspect get_download before retrying; the download may still complete' };
}
const uploadSweeper = setInterval(() => {
  for (const [uploadId, upload] of uploads) if (Date.now() - upload.updated > TTL) {
    uploads.delete(uploadId);
    void dom(upload.pageId, upload.target, 'upload_abort', { uploadId }).catch(() => {});
  }
}, 30000);
uploadSweeper.unref?.();

function integer(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${label} must be a nonnegative integer`);
  return value;
}
function url(value) {
  const parsed = new URL(value);
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Only HTTP/HTTPS pages are supported');
  return parsed.href;
}
async function page(pageId, expectedUrl) {
  const tab = await chrome.tabs.get(integer(pageId, 'pageId'));
  url(tab.url);
  if (expectedUrl !== undefined && tab.url !== expectedUrl) throw new Error('Page URL changed; inspect again');
  return tab;
}
function reference(uid) {
  if (typeof uid !== 'string') throw new Error('A snapshot uid is required');
  const parts = uid.split('/');
  if (parts.length !== 3 || !/^\d+$/.test(parts[0]) || !/^[\w-]+$/.test(parts[1]) || !/^[\w-]+:\d+$/.test(parts[2])) {
    throw new Error('Invalid uid; take a new snapshot');
  }
  return { frameId: Number(parts[0]), documentId: parts[1], ref: parts[2] };
}
async function dom(pageId, target, action, args = {}) {
  await page(pageId);
  const injectionTarget = target.documentId
    ? { tabId: pageId, documentIds: [target.documentId] }
    : { tabId: pageId, frameIds: [target.frameId ?? 0] };
  await chrome.scripting.executeScript({ target: injectionTarget, world: 'ISOLATED', files: ['dom.js'] });
  const [answer] = await chrome.scripting.executeScript({ target: injectionTarget, world: 'ISOLATED',
    func: (operation, parameters) => globalThis.__stilldeskBrowser.run(operation, parameters),
    args: [action, JSON.parse(JSON.stringify(args))] });
  if (!answer?.result) throw new Error('No document result; the page may have navigated');
  if (!answer.result.ok) throw new Error(answer.result.error);
  return { value: answer.result.value, frameId: answer.frameId, documentId: answer.documentId };
}
async function snapshot(args) {
  const tab = await page(args.pageId, args.expectedUrl);
  const frames = await chrome.webNavigation.getAllFrames({ tabId: args.pageId });
  const results = [];
  // Only inspect each document under its own host permission and origin.
  for (const frame of frames || []) {
    try {
      url(frame.url);
      const r = await dom(args.pageId, frame, 'snapshot', { maxNodes: args.maxNodes, maxText: args.maxText });
      results.push({ frameId: r.frameId, documentId: r.documentId, ...r.value,
        nodes: r.value.nodes.map(n => ({ ...n, uid: `${r.frameId}/${r.documentId}/${n.ref}`, ref: undefined })) });
    } catch (error) {
      results.push({ frameId: frame.frameId, url: frame.url, error: String(error.message) });
    }
  }
  return { pageId: args.pageId, url: tab.url, title: tab.title, frames: results,
    kind: 'DOM snapshot (not the browser accessibility tree)' };
}
async function getUpload(args) {
  const upload = uploads.get(args.uploadId);
  if (!upload || upload.pageId !== args.pageId) throw new Error('Unknown or expired upload');
  if (Date.now() - upload.updated > TTL) {
    uploads.delete(args.uploadId);
    await dom(upload.pageId, upload.target, 'upload_abort', { uploadId: args.uploadId }).catch(() => {});
    throw new Error('Upload expired');
  }
  upload.updated = Date.now();
  return upload;
}
async function beginUpload(args) {
  await page(args.pageId, args.expectedUrl);
  const target = reference(args.uid);
  if (!Array.isArray(args.files) || !args.files.length || args.files.length > 16) throw new Error('Expected 1–16 files');
  let total = 0;
  for (const file of args.files) {
    integer(file.size, 'file size');
    total += file.size;
    if (typeof file.name !== 'string' || /[\\/\0]/.test(file.name) || !file.name) throw new Error('Invalid file name');
    if (!/^[a-f0-9]{64}$/.test(file.sha256)) throw new Error('Missing file SHA-256');
  }
  if (total > LIMIT) throw new Error('Upload exceeds the experimental 32 MiB limit');
  // Expired staging state is cleaned before accepting a new transfer.
  for (const [id, old] of uploads) if (Date.now() - old.updated > TTL) {
    uploads.delete(id);
    await dom(old.pageId, old.target, 'upload_abort', { uploadId: id }).catch(() => {});
  }
  if (uploads.size >= 2) throw new Error('Too many staged uploads');
  const uploadId = crypto.randomUUID();
  await dom(args.pageId, target, 'upload_begin', { ref: target.ref, uploadId, files: args.files });
  uploads.set(uploadId, { pageId: args.pageId, target, files: args.files,
    chunks: args.files.map(() => []), received: args.files.map(() => 0), updated: Date.now() });
  return { uploadId, chunkBytes: CHUNK };
}
async function chunkUpload(args) {
  const upload = await getUpload(args);
  const index = integer(args.fileIndex, 'fileIndex');
  if (!upload.files[index] || args.offset !== upload.received[index]) throw new Error('Out-of-order file chunk');
  if (typeof args.base64 !== 'string' || args.base64.length > CHUNK * 4 / 3 + 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(args.base64)) {
    throw new Error('Invalid or oversized base64 chunk');
  }
  const data = Uint8Array.from(atob(args.base64), c => c.charCodeAt(0));
  if (!data.length || data.length > CHUNK || upload.received[index] + data.length > upload.files[index].size) {
    throw new Error('File chunk exceeds declared size');
  }
  await dom(upload.pageId, upload.target, 'upload_chunk', {
    uploadId: args.uploadId, fileIndex: index, offset: args.offset, base64: args.base64 });
  upload.chunks[index].push(data);
  upload.received[index] += data.length;
  return { received: upload.received[index] };
}
async function commitUpload(args) {
  const upload = await getUpload(args);
  for (let index = 0; index < upload.files.length; index++) {
    const file = upload.files[index];
    if (upload.received[index] !== file.size) throw new Error('Incomplete file transfer');
    const bytes = new Uint8Array(file.size);
    let offset = 0;
    for (const chunk of upload.chunks[index]) { bytes.set(chunk, offset); offset += chunk.length; }
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
    if (digest !== file.sha256) throw new Error('File SHA-256 mismatch');
  }
  const result = await dom(upload.pageId, upload.target, 'upload_commit', { uploadId: args.uploadId });
  uploads.delete(args.uploadId);
  return { ...result.value, transferVerified: true, eventIsTrusted: false };
}
export async function dispatch(action, args) {
  switch (action) {
    case 'capabilities': return { version: '0.2.0', transport: 'native-messaging',
      remoteDebugging: false, debuggerPermission: false, maxUploadBytes: LIMIT, chunkBytes: CHUNK,
      quietMode: true, canChangeForeground: false, canMoveSystemPointer: false,
      supported: ['background tabs', 'DOM snapshot', 'click', 'fill', 'scroll', 'select', 'check', 'DOM drag', 'file input upload', 'background download', 'visible active tab screenshot'],
      limitations: ['DOM events are synthetic (isTrusted=false)', 'No DevTools trace/heap/network API',
        'No browser-native dialogs or file chooser automation', 'HTTP/HTTPS frames only; host permissions required'] };
    case 'list_pages': return (await chrome.tabs.query({})).map(t => ({ pageId: t.id, url: t.url,
      title: t.title, active: t.active, windowId: t.windowId, supported: /^https?:/.test(t.url || '') }));
    case 'new_page': {
      if (args.background === false) fail('foreground_not_allowed', 'StillDesk creates background tabs only');
      const tab = await chrome.tabs.create({ url: url(args.url), active: false });
      return { pageId: tab.id, url: tab.url || tab.pendingUrl, foregroundChanged: false };
    }
    case 'navigate_page': {
      await page(args.pageId, args.expectedUrl);
      if (args.type === 'back') { await chrome.tabs.goBack(args.pageId); return { navigationStarted:true, foregroundChanged:false }; }
      if (args.type === 'forward') { await chrome.tabs.goForward(args.pageId); return { navigationStarted:true, foregroundChanged:false }; }
      if (args.type === 'reload') { await chrome.tabs.reload(args.pageId); return { navigationStarted:true, foregroundChanged:false }; }
      await chrome.tabs.update(args.pageId, { url: url(args.url) });
      return { pageId: args.pageId, url: url(args.url), navigationStarted: true };
    }
    case 'close_page': {
      const tab = await page(args.pageId, args.expectedUrl);
      if (tab.active) fail('active_tab_protected', 'Closing an active tab changes the user view; close a background tab instead');
      await chrome.tabs.remove(args.pageId); return { closed: true, foregroundChanged: false };
    }
    case 'select_page': {
      await page(args.pageId, args.expectedUrl);
      return { selected: true, pageId: args.pageId, selection: 'logical-only; pass pageId on subsequent calls', foregroundChanged: false };
    }
    case 'set_zoom': {
      await page(args.pageId,args.expectedUrl);
      if(typeof args.factor!=='number'||!Number.isFinite(args.factor)||args.factor<0.25||args.factor>5) fail('invalid_zoom','Zoom factor must be between 0.25 and 5');
      await chrome.tabs.setZoomSettings(args.pageId,{mode:'automatic',scope:'per-tab'});
      await chrome.tabs.setZoom(args.pageId,args.factor);
      return {factor:await chrome.tabs.getZoom(args.pageId),scope:'per-tab',foregroundChanged:false};
    }
    case 'take_snapshot': return snapshot(args);
    case 'read_text': {
      await page(args.pageId, args.expectedUrl);
      return (await dom(args.pageId, { frameId: args.frameId ?? 0 }, 'read_text', args)).value;
    }
    case 'wait_for': {
      if (typeof args.text !== 'string' || !args.text) fail('invalid_wait_text', 'A nonempty literal text is required');
      const until = Date.now() + Math.min(30000, Math.max(100,args.timeoutMs || 10000));
      do {
        const result = (await dom(args.pageId, {frameId:0}, 'read_text', {maxText:60000})).value;
        if (result.text.includes(args.text)) return { matched:true, url:result.url, foregroundChanged:false };
        await new Promise(resolve=>setTimeout(resolve,100));
      } while (Date.now() < until);
      fail('wait_timeout','Literal page text did not appear before timeout');
    }
    case 'fill_form': {
      await page(args.pageId, args.expectedUrl);
      if (!Array.isArray(args.elements) || !args.elements.length) throw new Error('Expected form elements');
      // Validate every reference before applying the first field (not an atomic form transaction).
      const entries = args.elements.map(e => ({ ...e, target: reference(e.uid) }));
      for (const entry of entries) await dom(args.pageId, entry.target, 'validate', { ref: entry.target.ref });
      const results = [];
      for (const entry of entries) results.push((await dom(args.pageId, entry.target, 'fill', {
        ref: entry.target.ref, value: entry.value })).value);
      return { results, atomic: false };
    }
    case 'drag': {
      await page(args.pageId, args.expectedUrl);
      const source = reference(args.from_uid), target = reference(args.to_uid);
      if (source.documentId !== target.documentId) fail('cross_document_drag_unsupported', 'DOM drag requires both nodes in the same document');
      return (await dom(args.pageId, source, 'drag', { from: source.ref, to: target.ref })).value;
    }
    case 'click': case 'fill': case 'hover': case 'press_key': case 'scroll': case 'select': case 'check': {
      await page(args.pageId, args.expectedUrl);
      const target = args.uid ? reference(args.uid) : { frameId: 0 };
      const result = (await dom(args.pageId, target, action, { ...args, ref: target.ref })).value;
      if (action === 'click' && result.openBackgroundUrl) {
        const tab = await chrome.tabs.create({ url: url(result.openBackgroundUrl), active: false,
          openerTabId: args.pageId });
        return { dispatched: true, pageId: tab.id, openedInBackground: true, foregroundChanged: false,
          eventIsTrusted: false, note: 'New-window link navigated in a background tab; its custom click handler was not executed' };
      }
      return result;
    }
    case 'upload_begin': return beginUpload(args);
    case 'upload_chunk': return chunkUpload(args);
    case 'upload_commit': return commitUpload(args);
    case 'upload_abort': {
      const upload = uploads.get(args.uploadId);
      if (upload && upload.pageId === args.pageId) {
        uploads.delete(args.uploadId);
        await dom(upload.pageId, upload.target, 'upload_abort', { uploadId: args.uploadId }).catch(() => {});
      }
      return { aborted: true };
    }
    case 'take_screenshot': {
      const tab = await page(args.pageId, args.expectedUrl);
      if (!tab.active) fail('background_screenshot_unsupported', 'Background screenshots are unavailable without debugging; the tab was not activated');
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
      const [current] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
      if (current?.id !== tab.id || current.url !== tab.url) throw new Error('Active tab changed during screenshot; image discarded');
      return { pageId: tab.id, dataUrl, scope: 'visible viewport' };
    }
    case 'download_file': return startDownload(args);
    case 'get_download': return downloadInfo(args.downloadId);
    case 'handle_dialog': fail('native_dialog_unsupported', 'Native JavaScript dialogs require user interaction in this non-debugging route; StillDesk will not focus or click browser UI');
    default: throw new Error(`Unsupported action: ${action}`);
  }
}
