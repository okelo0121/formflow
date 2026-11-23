const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const TMP = '/tmp/formflow-vercel.json';

function readStore() {
  try { return JSON.parse(fs.readFileSync(TMP, 'utf8')); } catch (e) { return {}; }
}
function writeStore(obj) {
  fs.writeFileSync(TMP, JSON.stringify(obj));
}

function googleCreds() {
  const store = readStore();
  return {
    clientId: (process.env.GOOGLE_CLIENT_ID || store.google_client_id || '').trim(),
    clientSecret: (process.env.GOOGLE_CLIENT_SECRET || store.google_client_secret || '').trim()
  };
}

function sessionSecret() {
  return process.env.SESSION_SECRET || 'formflow-dev-secret-change-me';
}

function signSession(payload) {
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const sig = crypto.createHmac('sha256', sessionSecret()).update(body).digest('base64url');
  return body + '.' + sig;
}

function readSession(cookieHeader) {
  try {
    const raw = String(cookieHeader || '');
    const m = raw.match(/(?:^|;\s*)ff_session=([^;]+)/);
    if (!m) return null;
    let token = m[1];
    try { token = decodeURIComponent(token); } catch (e) {}
    const parts = String(token).split('.');
    const body = parts[0];
    const sig = parts[1];
    if (!body || !sig) return null;
    const expect = crypto.createHmac('sha256', sessionSecret()).update(body).digest('base64url');
    if (expect !== sig) return null;
    return JSON.parse(Buffer.from(body, 'base64url').toString());
  } catch (e) {
    return null;
  }
}

function publicBase(req) {
  const forced = (process.env.PUBLIC_BASE || readStore().public_base || '').replace(/\/$/, '');
  if (forced) return forced;
  const proto = (req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim();
  const host = (req.headers['x-forwarded-host'] || req.headers.host || '').split(',')[0].trim();
  return proto + '://' + host;
}

function sendJson(res, status, obj, extraHeaders) {
  const body = JSON.stringify(obj);
  const headers = Object.assign({
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-cache'
  }, extraHeaders || {});
  res.writeHead(status, headers);
  res.end(body);
}

function cookieHeader(token, req) {
  const secure = publicBase(req).startsWith('https') ? '; Secure' : '';
  return 'ff_session=' + token + '; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000' + secure;
}

module.exports = {
  readStore, writeStore, googleCreds, signSession, readSession,
  publicBase, sendJson, cookieHeader
};
