import net from 'node:net';
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { runtimePaths, ensureRuntime, messages, send, parseArgs, proof, proofMatches } from './lib/transport.mjs';

export async function startBroker(runtime = runtimePaths()) {
  const token = await ensureRuntime(runtime);
  const clients = new Set(); let native, active, queue = [], stopped = false;
  const safeSend = (socket, value) => { try { send(socket, value); } catch { socket.destroy(); } };
  const status = () => ({ version: '0.2.0', pid: process.pid, nativeConnected: Boolean(native && !native.destroyed), queued: queue.length, inFlight: Boolean(active) });
  const saveStatus = () => fs.writeFile(runtime.status, JSON.stringify(status(), null, 2), { mode: 0o600 }).catch(() => {});
  function finish(item, error, result) {
    clearTimeout(item.timer);
    if (!item.cancelled && !item.owner.destroyed) safeSend(item.owner, { type: 'result', id: item.originalId, ...(error ? { error } : { result }) });
  }
  function pump() {
    if (active || stopped) return;
    while (queue.length) {
      const item = queue.shift();
      if (item.cancelled || item.owner.destroyed) { clearTimeout(item.timer); continue; }
      if (!native || native.destroyed) { finish(item, 'Chrome extension is not connected. Load the lab extension and connect its native host.'); continue; }
      active = item; safeSend(native, { type: 'command', id: item.id, action: item.action, args: item.args }); break;
    }
    void saveStatus();
  }
  function cancel(socket, id) {
    queue = queue.filter(item => {
      if (item.owner === socket && (id === undefined || item.originalId === id)) { item.cancelled = true; clearTimeout(item.timer); return false; }
      return true;
    });
    if (active?.owner === socket && (id === undefined || active.originalId === id)) active.cancelled = true;
  }
  const server = net.createServer(socket => {
    let role, authed = false;
    const challenge=crypto.randomBytes(32).toString('hex');
    const timer = setTimeout(() => socket.destroy(), 3000);
    socket.on('error', () => {});
    safeSend(socket,{type:'challenge',nonce:challenge,proof:proof(token,`server:${challenge}`)});
    messages(socket, msg => {
      if (!authed) {
        if (msg.type !== 'hello' || !['native', 'client'].includes(msg.role) || typeof msg.nonce!=='string'||!/^[a-f0-9]{64}$/.test(msg.nonce)||!proofMatches(msg.proof,proof(token,`client:${challenge}:${msg.nonce}:${msg.role}`))) return socket.destroy();
        role = msg.role;
        if (role === 'native' && native && !native.destroyed) return safeSend(socket, { type: 'error', error: 'An extension is already connected' });
        authed = true; clearTimeout(timer);
        if (role === 'native') native = socket; else clients.add(socket);
        safeSend(socket, { type: 'ready',proof:proof(token,`ready:${challenge}:${msg.nonce}:${role}`) }); void saveStatus(); return;
      }
      if (role === 'native') {
        if (msg.type === 'hello') return;
        if (msg.type !== 'result' || !active || msg.id !== active.id) return;
        const item = active; active = undefined;
        finish(item, msg.error, msg.result); pump(); return;
      }
      if (msg.type === 'cancel') { cancel(socket, msg.id); return; }
      if (msg.type !== 'request' || typeof msg.id !== 'string' || typeof msg.action !== 'string' || !msg.args || typeof msg.args !== 'object' || Array.isArray(msg.args)) return socket.destroy();
      if (msg.action === 'get_status') return safeSend(socket, { type: 'result', id: msg.id, result: status() });
      if (msg.action === 'shutdown') {
        safeSend(socket, { type: 'result', id: msg.id, result: { stopped: true } });
        setTimeout(() => void close(), 30); return;
      }
      if (queue.length >= 64 || queue.filter(item => item.owner === socket).length >= 16) return safeSend(socket, { type: 'result', id: msg.id, error: 'Broker queue is full' });
      const timeout = Number.isInteger(msg.timeoutMs) ? Math.min(120000, Math.max(1000, msg.timeoutMs)) : 45000;
      const item = { id: crypto.randomUUID(), originalId: msg.id, owner: socket, action: msg.action, args: msg.args, cancelled: false };
      item.timer = setTimeout(() => {
        if (active === item) {
          active = undefined; finish(item, 'Browser action timed out and was not replayed. It may have completed; inspect before repeating.');
          const old = native; native = undefined; old?.destroy();
        } else { queue = queue.filter(entry => entry !== item); finish(item, 'Queued browser request timed out; it was not executed.'); }
        pump();
      }, timeout);
      queue.push(item); pump();
    });
    socket.on('close', () => {
      clearTimeout(timer); clients.delete(socket); cancel(socket);
      if (socket === native) {
        native = undefined;
        if (active) { finish(active, 'Extension disconnected. An action may have completed; it was not replayed.'); active = undefined; }
        pump();
      }
      void saveStatus();
    });
  });
  async function close() {
    if (stopped) return; stopped = true;
    for (const item of queue) finish(item, 'Lab broker stopped before executing request'); queue = [];
    if (active) { finish(active, 'Lab broker stopped. An action may have completed; inspect before repeating.'); active = undefined; }
    native?.destroy(); for (const socket of clients) socket.destroy();
    await new Promise(resolve => server.close(resolve));
    await saveStatus();
  }
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(runtime.pipe, resolve); });
  await saveStatus();
  return { server, close, status };
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const options = parseArgs(process.argv.slice(2));
  startBroker(options.runtime).then(broker => {
    for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => void broker.close());
  }).catch(error => { if (error.code !== 'EADDRINUSE') { process.stderr.write(error.message + '\n'); process.exitCode = 1; } });
}
