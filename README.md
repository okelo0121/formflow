# FormFlow

Google Forms–style app: build forms, share a link, collect responses.

Live: https://formflow-alpha-six.vercel.app/

## Layout

```
index.html          Dashboard, editor, fill views
css/styles.css      Theme and layout
js/store.js         Local storage
js/ai.js            Help-me-write / generate-form client
js/app.js           UI
api/                Vercel serverless routes
  _lib.js           Session + store helpers
  health.js
  me.js
  setup.js          Save Google OAuth client id/secret
  generate-form.js
  forms.js
  forms/sync.js
  forms/[id].js
  forms/[id]/responses.js
  auth/google.js
  auth/callback.js
  auth/logout.js
server.py           Optional local Python server
auth_db.py          Optional local SQLite auth
vercel.json         Auth rewrites
```

## Local

```bash
python3 server.py
```

Or open `index.html` with a static server so `/api` and `/css` `/js` resolve.

## Vercel

Framework: Vite (static output `.`, build/install skipped). Set `SESSION_SECRET` in project env. Paste Google OAuth keys in Account; callback is `{origin}/auth/google/callback`.
