const { sendJson, readStore, writeStore } = require('../_lib');

module.exports = async (req, res) => {
  const id = req.query.id;
  const store = readStore();
  store.forms = store.forms || {};
  let found = null;
  Object.keys(store.forms).forEach(k => {
    (store.forms[k] || []).forEach(f => { if (f && f.id === id) found = f; });
  });
  if (req.method === 'GET') {
    if (!found) { sendJson(res, 404, { error: 'not-found' }); return; }
    sendJson(res, 200, { form: found });
    return;
  }
  sendJson(res, 405, { ok: false });
};
