import http from 'node:http';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import { createSession } from './server.mjs';
import { Providers } from './providers.mjs';
import { MAX_BYTES } from './protocol.mjs';

export async function serve({ uploads = [], downloads = [], host = process.env.STILLDESK_BIND || '127.0.0.1',
  port = Number(process.env.STILLDESK_PORT || 8791), token = process.env.STILLDESK_HTTP_TOKEN,
  allowedOrigins = (process.env.STILLDESK_ALLOWED_ORIGINS || '').split(',').filter(Boolean),
  allowedHosts = (process.env.STILLDESK_ALLOWED_HOSTS || 'localhost,127.0.0.1,[::1]').split(','),
  sessionFactory = () => createSession({ router: new Providers({ uploads, downloads }) }) } = {}) {
  if (typeof token !== 'string' || Buffer.byteLength(token) < 32) throw new Error('Set STILLDESK_HTTP_TOKEN to at least 32 random characters before serving');
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid port');
  const sessions = new Map();
  function json(res, status, body, headers = {}) {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers });
    res.end(body === undefined ? undefined : JSON.stringify(body));
  }
  const server = http.createServer(async (req, res) => {
    try {
      const header = req.headers.authorization || '';
      const candidate = Buffer.from(header.startsWith('Bearer ') ? header.slice(7) : '');
      const expected = Buffer.from(token);
      if (candidate.length !== expected.length || !timingSafeEqual(candidate, expected)) return json(res, 401, { error: 'Unauthorized' });
      const hostname = (req.headers.host || '').replace(/:\d+$/, '');
      if (!allowedHosts.includes(hostname)) return json(res, 403, { error: 'Host not allowed' });
      if (req.headers.origin && !allowedOrigins.includes(req.headers.origin)) return json(res, 403, { error: 'Origin not allowed' });
      if (req.url !== '/mcp') return json(res, 404, { error: 'Not found' });
      const sessionId = req.headers['mcp-session-id'];
      if (req.method === 'DELETE') {
        const slot = sessions.get(sessionId);
        if (!slot) return json(res, 404, { error: 'Unknown session' });
        slot.session.close(); sessions.delete(sessionId); return json(res, 200, { closed: true });
      }
      if (req.method !== 'POST') return json(res, 405, { error: 'Use POST for JSON Streamable HTTP' }, { Allow: 'POST, DELETE' });
      if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) return json(res, 415, { error: 'Expected application/json' });
      const chunks = []; let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_BYTES) return json(res, 413, { error: 'Request too large' });
        chunks.push(chunk);
      }
      let message;
      try { message = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { return json(res, 400, { error: 'Invalid JSON' }); }
      if (!message || typeof message !== 'object' || Array.isArray(message)) return json(res, 400, { error: 'Expected one JSON-RPC request' });
      let slot = sessions.get(sessionId), id = sessionId;
      if (message.method === 'initialize' && sessionId === undefined) {
        if (sessions.size >= 8) return json(res, 503, { error: 'Session capacity reached' });
        id = randomUUID(); slot = { session: sessionFactory(), updated: Date.now() }; sessions.set(id, slot);
      }
      if (!slot) return json(res, 404, { error: 'Unknown session; initialize a new one' });
      slot.updated = Date.now();
      const result = await slot.session.handle(message);
      if (result === undefined) return json(res, 202, undefined, { 'Mcp-Session-Id': id });
      return json(res, 200, result, { 'Mcp-Session-Id': id });
    } catch { if (!res.headersSent) json(res, 500, { error: 'Transport request failed' }); else res.end(); }
  });
  server.requestTimeout = 130000;
  const sweep = setInterval(() => {
    for (const [id, slot] of sessions) if (Date.now() - slot.updated > 30 * 60 * 1000) { slot.session.close(); sessions.delete(id); }
  }, 60000); sweep.unref();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, resolve); });
  const close = async () => {
    clearInterval(sweep); for (const slot of sessions.values()) slot.session.close(); sessions.clear();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  };
  process.stderr.write(`StillDesk MCP HTTP listening on ${host}:${server.address().port}/mcp (authentication required)\n`);
  return { server, close, address: server.address() };
}
