const { googleCreds, readSession, publicBase, sendJson } = require('./_lib');

module.exports = (req, res) => {
  const creds = googleCreds();
  const user = readSession(req.headers.cookie);
  const base = publicBase(req);
  sendJson(res, 200, {
    user: user ? { id: user.sub, email: user.email, name: user.name, picture: user.picture, google: true } : null,
    googleConfigured: !!(creds.clientId && creds.clientSecret),
    origin: base,
    redirectUri: base + '/auth/google/callback'
  });
};
