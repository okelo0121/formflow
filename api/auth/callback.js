const { googleCreds, publicBase, signSession, cookieHeader } = require('../_lib');

module.exports = async (req, res) => {
  const base = publicBase(req);
  const u = new URL(req.url, base);
  const code = u.searchParams.get('code');
  const err = u.searchParams.get('error');
  if (err || !code) {
    res.writeHead(302, { Location: '/#/?google=error' });
    res.end();
    return;
  }
  const { clientId, clientSecret } = googleCreds();
  try {
    const tokRes = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        code,
        client_id: clientId,
        client_secret: clientSecret,
        redirect_uri: base + '/auth/google/callback',
        grant_type: 'authorization_code'
      })
    });
    const tok = await tokRes.json();
    if (!tok.access_token) throw new Error(JSON.stringify(tok));
    const infoRes = await fetch('https://www.googleapis.com/oauth2/v3/userinfo', {
      headers: { Authorization: 'Bearer ' + tok.access_token }
    });
    const info = await infoRes.json();
    const session = signSession({
      sub: info.sub,
      email: info.email,
      name: info.name || info.given_name || (info.email || '').split('@')[0],
      picture: info.picture || null
    });
    res.writeHead(302, {
      Location: '/#/?google=ok',
      'Set-Cookie': cookieHeader(session, req)
    });
    res.end();
  } catch (e) {
    res.writeHead(302, { Location: '/#/?google=error' });
    res.end();
  }
};
