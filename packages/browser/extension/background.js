import { dispatch } from './actions.js';

let port;
let retry;
let queue = Promise.resolve();
function connect() {
  clearTimeout(retry);
  try {
    port = chrome.runtime.connectNative('com.stilldesk.browser');
    const current = port;
    current.postMessage({ type: 'hello', extensionId: chrome.runtime.id, version: '0.2.0' });
    current.onMessage.addListener(message => {
      if (message?.type !== 'command' || typeof message.id !== 'string') return;
      queue = queue.catch(() => {}).then(async () => {
        if (port !== current) return;
        try {
          const result = await dispatch(message.action, message.args || {});
          current.postMessage({ type: 'result', id: message.id, result });
        } catch (error) {
          if (port === current) current.postMessage({ type: 'result', id: message.id,
            error: { code: error.code || 'browser_action_failed', message: String(error?.message || error) } });
        }
      });
    });
    current.onDisconnect.addListener(() => {
      // Consume lastError; never replay pending actions after reconnecting.
      const failure = chrome.runtime.lastError?.message;
      if (failure) console.warn('StillDesk Browser native host disconnected:', failure);
      if (port === current) port = undefined;
      retry = setTimeout(connect, 3000);
    });
  } catch {
    retry = setTimeout(connect, 3000);
  }
}
connect();
chrome.action.onClicked.addListener(() => { if (!port) connect(); });
