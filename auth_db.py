"""SQLite users, sessions, forms + Google OAuth helpers."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
DB_PATH = os.path.join(DATA, 'formflow.db')

_local = None


def db():
    global _local
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      google_id TEXT UNIQUE,
      email TEXT,
      name TEXT,
      picture TEXT,
      created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id TEXT,
      created_at INTEGER,
      expires_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS forms (
      id TEXT PRIMARY KEY,
      owner_id TEXT,
      data TEXT,
      updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS oauth_states (
      state TEXT PRIMARY KEY,
      created_at INTEGER
    );
    ''')
    conn.commit()
    return conn


CONN = db()
LOCK = __import__('threading').Lock()


def meta_get(key, default=None):
    row = CONN.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def meta_set(key, value):
    with LOCK:
        CONN.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        CONN.commit()


def google_creds():
    cid = os.environ.get('GOOGLE_CLIENT_ID') or meta_get('google_client_id') or ''
    secret = os.environ.get('GOOGLE_CLIENT_SECRET') or meta_get('google_client_secret') or ''
    return cid.strip(), secret.strip()


def google_configured():
    cid, secret = google_creds()
    return bool(cid and secret)


def new_id():
    return secrets.token_urlsafe(12)


def create_session(user_id, days=30):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with LOCK:
        CONN.execute(
            'INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',
            (token, user_id, now, now + days * 86400),
        )
        CONN.commit()
    return token


def user_from_session(token):
    if not token:
        return None
    now = int(time.time())
    row = CONN.execute(
        'SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?',
        (token, now),
    ).fetchone()
    return dict(row) if row else None


def upsert_google_user(google_id, email, name, picture):
    now = int(time.time())
    with LOCK:
        row = CONN.execute('SELECT * FROM users WHERE google_id=?', (google_id,)).fetchone()
        if not row and email:
            row = CONN.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if row:
            CONN.execute(
                'UPDATE users SET google_id=?, email=?, name=?, picture=? WHERE id=?',
                (google_id, email, name, picture, row['id']),
            )
            CONN.commit()
            return dict(CONN.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone())
        uid = new_id()
        CONN.execute(
            'INSERT INTO users(id,google_id,email,name,picture,created_at) VALUES(?,?,?,?,?,?)',
            (uid, google_id, email, name, picture, now),
        )
        CONN.commit()
        return dict(CONN.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())


def guest_user():
    uid = 'guest-' + secrets.token_urlsafe(8)
    now = int(time.time())
    with LOCK:
        CONN.execute(
            'INSERT INTO users(id,google_id,email,name,picture,created_at) VALUES(?,?,?,?,?,?)',
            (uid, None, None, 'Guest', None, now),
        )
        CONN.commit()
    return uid


def list_forms(owner_id):
    rows = CONN.execute(
        'SELECT data FROM forms WHERE owner_id=? ORDER BY updated_at DESC', (owner_id,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r['data']))
        except Exception:
            pass
    return out


def public_form(form_id):
    row = CONN.execute('SELECT data FROM forms WHERE id=?', (form_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row['data'])
    except Exception:
        return None


def save_forms(owner_id, forms):
    now = int(time.time())
    with LOCK:
        existing = {r['id'] for r in CONN.execute('SELECT id FROM forms WHERE owner_id=?', (owner_id,))}
        keep = set()
        for f in forms or []:
            if not isinstance(f, dict) or not f.get('id'):
                continue
            fid = str(f['id'])[:80]
            keep.add(fid)
            payload = json.dumps(f)
            CONN.execute(
                'INSERT INTO forms(id,owner_id,data,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id, data=excluded.data, updated_at=excluded.updated_at',
                (fid, owner_id, payload, int(f.get('updatedAt') or now)),
            )
        for fid in existing - keep:
            CONN.execute('DELETE FROM forms WHERE id=? AND owner_id=?', (fid, owner_id))
        CONN.commit()


def save_one_form(owner_id, form):
    if not form or not form.get('id'):
        return
    save_forms(owner_id, [form] + [f for f in list_forms(owner_id) if f.get('id') != form.get('id')])


def append_response(form_id, response):
    with LOCK:
        row = CONN.execute('SELECT data, owner_id FROM forms WHERE id=?', (form_id,)).fetchone()
        if not row:
            return False
        try:
            form = json.loads(row['data'])
        except Exception:
            return False
        form.setdefault('responses', []).append(response)
        CONN.execute(
            'UPDATE forms SET data=?, updated_at=? WHERE id=?',
            (json.dumps(form), int(time.time()), form_id),
        )
        CONN.commit()
        return True


def reassign_forms(from_owner, to_owner):
    if not from_owner or not to_owner or from_owner == to_owner:
        return
    with LOCK:
        CONN.execute('UPDATE forms SET owner_id=? WHERE owner_id=?', (to_owner, from_owner))
        CONN.commit()


def put_oauth_state(state):
    with LOCK:
        CONN.execute('INSERT OR REPLACE INTO oauth_states(state,created_at) VALUES(?,?)', (state, int(time.time())))
        CONN.commit()


def take_oauth_state(state):
    with LOCK:
        row = CONN.execute('SELECT state FROM oauth_states WHERE state=?', (state,)).fetchone()
        if not row:
            return False
        CONN.execute('DELETE FROM oauth_states WHERE state=?', (state,))
        CONN.commit()
        return True


def http_form(url, fields, timeout=20):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'User-Agent': 'FormFlow/1.0',
    }, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_get_auth(url, token, timeout=20):
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'User-Agent': 'FormFlow/1.0',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def exchange_google_code(code, redirect_uri):
    cid, secret = google_creds()
    tok = http_form('https://oauth2.googleapis.com/token', {
        'code': code,
        'client_id': cid,
        'client_secret': secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
    })
    access = tok.get('access_token')
    if not access:
        raise RuntimeError('no access_token: %s' % tok)
    info = http_get_auth('https://www.googleapis.com/oauth2/v3/userinfo', access)
    return {
        'google_id': info.get('sub'),
        'email': info.get('email'),
        'name': info.get('name') or info.get('given_name') or (info.get('email') or '').split('@')[0],
        'picture': info.get('picture'),
    }
