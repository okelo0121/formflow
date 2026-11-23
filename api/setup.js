const { googleCreds, readStore, writeStore, sendJson } = require('./_lib');

module.exports = async (req, res) => {
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
  if (req.method !== 'POST') {
    sendJson(res, 405, { ok: false, error: 'POST only' });
    return;
  }
  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch (e) { body = {}; }
  }
  if (!body || typeof body !== 'object') {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    try { body = JSON.parse(Buffer.concat(chunks).toString() || '{}'); } catch (e) { body = {}; }
  }
  const cid = String(body.clientId || body.client_id || '').trim();
  const secret = String(body.clientSecret || body.client_secret || '').trim();
  if (!cid || !secret) {
    sendJson(res, 400, { ok: false, error: 'Client ID and secret are required' });
    return;
  }
  const store = readStore();
  store.google_client_id = cid;
  store.google_client_secret = secret;
  writeStore(store);
  sendJson(res, 200, { ok: true, googleConfigured: true });
};
