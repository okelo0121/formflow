const { sendJson, readStore, writeStore } = require('../../_lib');

module.exports = async (req, res) => {
  if (req.method !== 'POST') { sendJson(res, 405, { ok: false }); return; }
  const id = req.query.id;
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
  const rec = body.response || body;
  const store = readStore();
  store.forms = store.forms || {};
  let ok = false;
  Object.keys(store.forms).forEach(k => {
    store.forms[k] = (store.forms[k] || []).map(f => {
      if (f && f.id === id) {
        f.responses = f.responses || [];
        f.responses.push(rec);
        ok = true;
      }
      return f;
    });
  });
  writeStore(store);
  sendJson(res, ok ? 200 : 404, { ok });
};
