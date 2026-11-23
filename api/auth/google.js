const { googleCreds, publicBase } = require('../_lib');

module.exports = (req, res) => {
  const { clientId, clientSecret } = googleCreds();
  const base = publicBase(req);
  if (!clientId || !clientSecret) {
    res.writeHead(302, { Location: '/#/?google=setup' });
    res.end();
    return;
  }
  const state = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const url = 'https://accounts.google.com/o/oauth2/v2/auth?' + new URLSearchParams({
    client_id: clientId,
    redirect_uri: base + '/auth/google/callback',
    response_type: 'code',
    scope: 'openid email profile',
    access_type: 'online',
    include_granted_scopes: 'true',
    state,
    prompt: 'select_account'
  }).toString();
  res.writeHead(302, { Location: url, 'Set-Cookie': 'ff_oauth_state=' + state + '; Path=/; HttpOnly; SameSite=Lax; Max-Age=600' });
  res.end();
};
