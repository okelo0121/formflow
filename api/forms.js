const { readSession, sendJson, readStore, writeStore } = require('./_lib');

module.exports = async (req, res) => {
  const user = readSession(req.headers.cookie);
  const key = (user && (user.sub || user.email)) || 'guest';
  const store = readStore();
  store.forms = store.forms || {};

  if (req.method === 'GET') {
    sendJson(res, 200, { forms: store.forms[key] || [] });
    return;
  }
  if (req.method === 'POST') {
    let body = req.body;
    if (!body || typeof body === 'string') {
      try {
        if (typeof body === 'string') body = JSON.parse(body);
        else {
          const chunks = [];
          for await (const c of req) chunks.push(c);
          body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
        }
      } catch (e) { body = {}; }
    }
    store.forms[key] = Array.isArray(body.forms) ? body.forms : [];
    writeStore(store);
    sendJson(res, 200, { ok: true, count: store.forms[key].length });
    return;
  }
  sendJson(res, 405, { ok: false, error: 'GET or POST' });
};
