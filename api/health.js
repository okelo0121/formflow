const { googleCreds, sendJson } = require('./_lib');
module.exports = (req, res) => {
  const c = googleCreds();
  sendJson(res, 200, { ok: true, service: 'formflow', vercel: true, google: !!(c.clientId && c.clientSecret) });
};
