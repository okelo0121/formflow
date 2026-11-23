#!/usr/bin/env python3
"""FormFlow static server + live AI form generator.

Tries free OpenAI-compatible providers in order until one returns a valid form spec.
No API key required (anonymous free tiers).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import auth_db as adb

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', '8611'))

SYSTEM = (
    'You are FormFlow, an expert form designer. Given a user request, design a complete form. '
    'Reply with ONLY compact JSON (no markdown, no commentary) matching: '
    '{"title":string,"description":string,"theme":"#hex from '
    '[#673ab7,#1a73e8,#009688,#34a853,#e8710a,#d93025,#e91e63,#5f6368]",'
    '"collectEmail":boolean,"questions":[{"type":"short|paragraph|multiple_choice|checkbox|'
    'dropdown|linear_scale|date|time","title":string,"required":boolean,"options":[string],'
    '"scaleMin":1,"scaleMax":5,"scaleMinLabel":string,"scaleMaxLabel":string}]}. '
    'Rules: 5-10 questions; mix types; choice types need 3-6 real options; linear_scale uses '
    'scaleMin 0 or 1 and scaleMax 5 or 10 with labels; first questions should be required; '
    'match the user language; be specific to the topic, not generic; never include a password field.'
)

TYPES = {
    'short', 'paragraph', 'multiple_choice', 'checkbox', 'dropdown',
    'linear_scale', 'date', 'time'
}
TYPE_ALIAS = {
    'text': 'short', 'short_answer': 'short', 'shortanswer': 'short',
    'long': 'paragraph', 'long_text': 'paragraph', 'textarea': 'paragraph', 'essay': 'paragraph',
    'radio': 'multiple_choice', 'mcq': 'multiple_choice', 'choice': 'multiple_choice',
    'single_choice': 'multiple_choice', 'multiplechoice': 'multiple_choice',
    'checkboxes': 'checkbox', 'multi': 'checkbox', 'multiselect': 'checkbox',
    'select': 'dropdown', 'drop_down': 'dropdown',
    'scale': 'linear_scale', 'rating': 'linear_scale', 'nps': 'linear_scale',
    'likert': 'linear_scale', 'calendar': 'date', 'clock': 'time',
}
THEMES = [
    '#673ab7', '#1a73e8', '#009688', '#34a853',
    '#e8710a', '#d93025', '#e91e63', '#5f6368'
]

CACHE = {}
CACHE_TTL = 15 * 60
CACHE_LOCK = threading.Lock()
GEN_LOCK = threading.Lock()

UA = 'FormFlow/1.0 (+https://formflow.local)'


def log(msg):
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


def snap_theme(hex_val):
    n = str(hex_val or '').replace('#', '').strip()
    if len(n) != 6:
        return THEMES[0]
    try:
        rgb = [int(n[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return THEMES[0]
    best, best_d = THEMES[0], 10 ** 9
    for c in THEMES:
        cr = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
        d = sum((a - b) ** 2 for a, b in zip(rgb, cr))
        if d < best_d:
            best, best_d = c, d
    return best


def coerce_type(t):
    k = re.sub(r'[\s-]+', '_', str(t or 'short').lower())
    if k in TYPES:
        return k
    return TYPE_ALIAS.get(k, 'short')


def clean_options(arr):
    out = []
    if not isinstance(arr, list):
        return out
    for o in arr:
        s = str(o if o is not None else '').strip()[:80]
        if s and s not in out:
            out.append(s)
    return out[:8]


def extract_json(text):
    s = str(text or '').strip()
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', s, re.I)
    if fence:
        s = fence.group(1).strip()
    start, end = s.find('{'), s.rfind('}')
    if start >= 0 and end > start:
        s = s[start:end + 1]
    return json.loads(s)


def normalize_spec(raw, prompt):
    src = raw if isinstance(raw, dict) else {}
    questions = []
    for q in src.get('questions') or []:
        if not isinstance(q, dict):
            continue
        qtype = coerce_type(q.get('type'))
        title = str(q.get('title') or q.get('label') or q.get('question') or '').strip()[:160]
        if not title:
            continue
        item = {
            'type': qtype,
            'title': title,
            'required': bool(q.get('required')),
            'options': clean_options(q.get('options') or q.get('choices') or []),
        }
        if q.get('required') is None:
            item['required'] = qtype in ('short', 'multiple_choice', 'linear_scale')
        if qtype == 'linear_scale':
            try:
                mn = int(q.get('scaleMin', 1))
            except (TypeError, ValueError):
                mn = 1
            if mn not in (0, 1):
                mn = 1
            try:
                mx = int(q.get('scaleMax', 5))
            except (TypeError, ValueError):
                mx = 5
            if mx < 2 or mx > 10:
                mx = 5
            if mx <= mn:
                mx = mn + 4
            item['scaleMin'] = mn
            item['scaleMax'] = mx
            item['scaleMinLabel'] = str(q.get('scaleMinLabel') or '')[:40]
            item['scaleMaxLabel'] = str(q.get('scaleMaxLabel') or '')[:40]
        if qtype in ('multiple_choice', 'checkbox', 'dropdown'):
            if len(item['options']) < 2:
                item['options'] = ['Option 1', 'Option 2', 'Option 3']
        questions.append(item)
        if len(questions) >= 12:
            break
    if len(questions) < 1:
        raise ValueError('too-few-questions')
    title = str(src.get('title') or 'Untitled form').strip()[:80] or 'Untitled form'
    description = str(src.get('description') or '').strip()[:240]
    return {
        'title': title,
        'description': description,
        'theme': snap_theme(src.get('theme')),
        'collectEmail': bool(src.get('collectEmail') or src.get('collect_email')),
        'questions': questions,
    }


def http_json(url, payload=None, headers=None, timeout=28, method=None):
    h = {'Accept': 'application/json', 'User-Agent': UA}
    if headers:
        h.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        h.setdefault('Content-Type', 'application/json')
        method = method or 'POST'
    req = urllib.request.Request(url, data=data, headers=h, method=method or 'GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode('utf-8'))
            except Exception:
                parsed = {'text': raw.decode('utf-8', 'replace')}
            return 200, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read() or b''
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except Exception:
            parsed = {'error': raw.decode('utf-8', 'replace')[:400]}
        return e.code, parsed, raw


def content_from_chat(parsed):
    if isinstance(parsed, dict):
        choices = parsed.get('choices')
        if choices:
            msg = (choices[0] or {}).get('message') or {}
            return msg.get('content') or ''
        if 'text' in parsed:
            return parsed.get('text') or ''
        if 'content' in parsed and isinstance(parsed['content'], str):
            return parsed['content']
    if isinstance(parsed, str):
        return parsed
    return ''


def retry_after(parsed, status):
    blob = json.dumps(parsed) if not isinstance(parsed, str) else parsed
    m = re.search(r'(?:retry after|retry in|Retry after)\s+(\d+)\s*s', blob, re.I)
    if m:
        return int(m.group(1))
    if status == 429:
        return 20
    return 0


def call_llm7(model, messages):
    status, parsed, _ = http_json(
        'https://api.llm7.io/v1/chat/completions',
        {
            'model': model,
            'messages': messages,
            'temperature': 0.4,
            'max_tokens': 1400,
        },
        {'Authorization': 'Bearer unused'},
        timeout=10,
    )
    if status != 200:
        return None, retry_after(parsed, status), '%s:%s' % (status, json.dumps(parsed)[:160])
    text = content_from_chat(parsed)
    return text, 0, parsed.get('model') or model


def call_pollinations_post(messages):
    status, parsed, _ = http_json(
        'https://text.pollinations.ai/openai',
        {'model': 'openai', 'messages': messages, 'temperature': 0.4},
        timeout=10,
    )
    if status != 200:
        return None, retry_after(parsed, status), '%s:%s' % (status, json.dumps(parsed)[:160])
    text = content_from_chat(parsed)
    return text, 0, parsed.get('model') or 'pollinations'


def call_pollinations_get(prompt):
    packed = (
        'Return ONLY JSON form spec {title,description,theme,collectEmail,questions:['
        '{type,title,required,options,scaleMin,scaleMax,scaleMinLabel,scaleMaxLabel}]}. '
        'Types: short,paragraph,multiple_choice,checkbox,dropdown,linear_scale,date,time. '
        '6-8 specific questions. User request: '
    ) + prompt
    url = 'https://text.pollinations.ai/' + urllib.parse.quote(packed[:1800])
    status, parsed, raw = http_json(url, payload=None, timeout=10, method='GET')
    if status != 200:
        return None, retry_after(parsed, status), '%s' % status
    text = content_from_chat(parsed) or (raw.decode('utf-8', 'replace') if raw else '')
    return text, 0, 'pollinations-get'


PROVIDERS = [
    ('pollinations', lambda messages, prompt: call_pollinations_post(messages)),
    ('llm7-mistral', lambda messages, prompt: call_llm7('mistral-Nemo-Instruct-2407', messages)),
    ('llm7-codestral', lambda messages, prompt: call_llm7('codestral-latest', messages)),
    ('pollinations-get', lambda messages, prompt: call_pollinations_get(prompt)),
]


def cache_get(key):
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if not hit:
            return None
        spec, provider, model, ts = hit
        if time.time() - ts > CACHE_TTL:
            CACHE.pop(key, None)
            return None
        return spec, provider, model


def cache_put(key, spec, provider, model):
    with CACHE_LOCK:
        CACHE[key] = (spec, provider, model, time.time())


def _q(qtype, title, required=False, options=None, **scale):
    item = {'type': qtype, 'title': title, 'required': required, 'options': options or []}
    item.update(scale)
    return item


def guess_title(prompt):
    s = re.sub(
        r'^(please\s+)?((can|could)\s+you\s+)?(create|make|generate|build|design|draft)\s+(me\s+)?(a|an)\s+',
        '', str(prompt or ''), flags=re.I)
    s = re.sub(r'\b(google\s+)?forms?\b', ' ', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip().rstrip('?.!')
    m = re.search(r'\bfor\s+(?:a|an|the)\s+(.+?)(?:\s+with\b|\s+that\b|,|$)', s, re.I)
    topic = m.group(1).strip() if m else ''
    low = (prompt or '').lower()
    if topic:
        topic = topic[:1].upper() + topic[1:]
        if re.search(r'feedback|satisf|review', low):
            return topic + ' feedback'
        if re.search(r'rsvp|wedding|party', low):
            return topic + ' RSVP'
        if re.search(r'application|hiring|job', low):
            return topic + ' application'
        return topic[:80]
    if not s:
        return 'Untitled form'
    if len(s) > 60:
        s = s[:60].rsplit(' ', 1)[0]
    return s[:1].upper() + s[1:]


def draft_spec(prompt):
    """Always-available designer used when live models are rate-limited."""
    low = (prompt or '').lower()
    title = guess_title(prompt)
    questions = []
    desc = 'Please take a few minutes to complete this form.'
    theme = '#673ab7'
    collect = bool(re.search(r'\bemail\b', low))

    packs = [
        (r'coffee|caf[eé]|restaurant|dining|food|bakery|hotel', '#e8710a',
         'Tell us how we did — it takes about a minute.',
         [
             _q('short', 'Your name (optional)'),
             _q('multiple_choice', 'How often do you visit us?', True,
                ['First time', 'Weekly', 'A few times a month', 'Rarely']),
             _q('linear_scale', 'How would you rate your overall experience?', True,
                scaleMin=1, scaleMax=5, scaleMinLabel='Poor', scaleMaxLabel='Excellent'),
             _q('checkbox', 'What did you order or use?', False,
                ['Drinks', 'Food', 'Takeaway', 'Dine-in', 'Delivery']),
             _q('linear_scale', 'How likely are you to recommend us?', True,
                scaleMin=1, scaleMax=10, scaleMinLabel='Not likely', scaleMaxLabel='Extremely likely'),
             _q('paragraph', 'What could we do better?'),
         ]),
        (r'wedding|rsvp|birthday|party|invitation', '#e91e63',
         'Please RSVP so we can plan seating, meals, and timing.',
         [
             _q('short', 'Your full name', True),
             _q('multiple_choice', 'Will you attend?', True,
                ['Joyfully accept', 'Regretfully decline', 'Not sure yet']),
             _q('short', 'Number of guests (including you)'),
             _q('checkbox', 'Meal preference', False,
                ['Chicken', 'Fish', 'Vegetarian', 'Vegan', 'Kids meal']),
             _q('short', 'Any allergies or access needs?'),
             _q('paragraph', 'A note for the hosts'),
         ]),
        (r'job|hiring|applicant|recruit|career|developer|role\b', '#1a73e8',
         'Apply in a few minutes. We’ll only use this to review your application.',
         [
             _q('short', 'Full name', True),
             _q('short', 'Email address', True),
             _q('short', 'Phone number', True),
             _q('short', 'Link to portfolio, GitHub, or LinkedIn'),
             _q('dropdown', 'Years of relevant experience', True,
                ['0–1', '1–3', '3–5', '5–8', '8+']),
             _q('checkbox', 'Which skills do you bring?', False,
                ['JavaScript', 'TypeScript', 'React', 'CSS', 'Backend', 'Design', 'Other']),
             _q('paragraph', 'Why are you a great fit for this role?', True),
             _q('date', 'When could you start?'),
         ]),
        (r'workshop|conference|event|meetup|webinar|registration', '#009688',
         'Reserve your spot. We’ll send details to the email you provide.',
         [
             _q('short', 'Full name', True),
             _q('short', 'Organization / company'),
             _q('multiple_choice', 'How will you attend?', True,
                ['In person', 'Online', 'Not sure yet']),
             _q('checkbox', 'Which sessions interest you?', False,
                ['Keynote', 'Workshops', 'Networking', 'Panels']),
             _q('dropdown', 'T-shirt size', False, ['XS', 'S', 'M', 'L', 'XL', 'XXL', 'No shirt']),
             _q('paragraph', 'Anything we should know?'),
         ]),
        (r'quiz|trivia|counties|kenya|exam|test your', '#34a853',
         'Answer each question. Good luck!',
         [
             _q('short', 'Your name'),
             _q('multiple_choice', 'How confident are you about this topic?', True,
                ['Very confident', 'Somewhat', 'Still learning']),
             _q('dropdown', 'Pick a favorite related to this topic', True,
                ['Option A', 'Option B', 'Option C', 'Option D', 'Other']),
             _q('linear_scale', 'How interested are you in this topic?', True,
                scaleMin=1, scaleMax=5, scaleMinLabel='Not much', scaleMaxLabel='Very'),
             _q('paragraph', 'What would you like to learn next?'),
         ]),
        (r'employee|pulse|sprint|retro|team|workplace|staff', '#34a853',
         'Honest, kind, and optional to sign. Used to improve how we work.',
         [
             _q('short', 'Your name (or leave blank to stay anonymous)'),
             _q('linear_scale', 'How was this period overall?', True,
                scaleMin=1, scaleMax=5, scaleMinLabel='Rough', scaleMaxLabel='Great'),
             _q('paragraph', 'What went well?', True),
             _q('paragraph', 'What should we improve next?', True),
             _q('checkbox', 'Where did we get stuck?', False,
                ['Unclear goals', 'Too many meetings', 'Blocked on others', 'Tools / process', 'Workload']),
         ]),
        (r'clinic|patient|dental|health|hospital|intake', '#1a73e8',
         'Please complete this before your visit.',
         [
             _q('short', 'Full name', True),
             _q('date', 'Date of birth', True),
             _q('short', 'Phone number'),
             _q('multiple_choice', 'Have you visited us before?', False, ['Yes', 'No']),
             _q('paragraph', 'What is the main reason for your visit?'),
             _q('time', 'Preferred appointment time'),
         ]),
        (r'bug|issue|ticket|support|report', '#d93025',
         'Help us reproduce the problem.',
         [
             _q('short', 'Short summary', True),
             _q('dropdown', 'Severity', True,
                ['Cosmetic', 'Annoying', 'Blocks my work', 'Data loss / security']),
             _q('paragraph', 'Steps to reproduce', True),
             _q('paragraph', 'What did you expect to happen?'),
             _q('paragraph', 'What actually happened?'),
             _q('short', 'Your email so we can follow up'),
         ]),
        (r'satisf|nps|feedback|survey|review', '#673ab7',
         'Help us improve — it takes less than 2 minutes.',
         [
             _q('short', 'How did you hear about us?', True),
             _q('multiple_choice', 'How satisfied are you overall?', True,
                ['Very satisfied', 'Satisfied', 'Neutral', 'Dissatisfied', 'Very dissatisfied']),
             _q('linear_scale', 'How likely are you to recommend us to a friend?', True,
                scaleMin=1, scaleMax=10, scaleMinLabel='Not likely', scaleMaxLabel='Extremely likely'),
             _q('checkbox', 'Which parts matter most to you?', False,
                ['Quality', 'Price', 'Support', 'Speed', 'Ease of use']),
             _q('paragraph', 'What can we do better?'),
         ]),
        (r'contact|enquiry|inquiry|get in touch', '#1a73e8',
         "We'd love to hear from you.",
         [
             _q('short', 'Full name', True),
             _q('short', 'Email address', True),
             _q('short', 'Phone (optional)'),
             _q('dropdown', 'Topic', False, ['General', 'Support', 'Sales', 'Partnership', 'Other']),
             _q('paragraph', 'Your message', True),
         ]),
    ]

    for keys, th, d, qs in packs:
        if re.search(keys, low):
            theme, desc, questions = th, d, qs
            if 'email' in keys or re.search(r'job|event|clinic|contact|rsvp', keys):
                collect = True
            break
    if not questions:
        questions = [
            _q('short', 'Your name'),
            _q('multiple_choice', 'Overall, how would you rate this?', True,
               ['Excellent', 'Good', 'Okay', 'Poor']),
            _q('linear_scale', 'How likely are you to recommend this?', True,
               scaleMin=1, scaleMax=10, scaleMinLabel='Not likely', scaleMaxLabel='Extremely likely'),
            _q('checkbox', 'What matters most to you?', False,
               ['Quality', 'Price', 'Speed', 'Support', 'Something else']),
            _q('paragraph', 'Anything else we should know?'),
        ]

    # Numbered lines in the prompt become extra questions
    for line in str(prompt or '').splitlines():
        m = re.match(r'\s*(?:\d+[.)]|[-*•])\s+(.+)', line)
        if m:
            questions.insert(len(questions) - 1, _q('short', m.group(1).strip()[:160], True))

    if collect and not any('email' in q['title'].lower() for q in questions):
        questions.insert(1, _q('short', 'Email address', True))

    return normalize_spec({
        'title': title,
        'description': desc,
        'theme': theme,
        'collectEmail': collect,
        'questions': questions[:12],
    }, prompt)


def generate_form(prompt):
    prompt = str(prompt or '').strip()[:4000]
    if len(prompt) < 3:
        return {'ok': False, 'error': 'Describe the form you want first.'}
    key = hashlib.sha256(prompt.lower().encode('utf-8')).hexdigest()
    cached = cache_get(key)
    if cached:
        spec, provider, model = cached
        return {'ok': True, 'spec': spec, 'provider': provider, 'model': model, 'cached': True}

    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': prompt},
    ]
    with GEN_LOCK:
        cached = cache_get(key)
        if cached:
            spec, provider, model = cached
            return {'ok': True, 'spec': spec, 'provider': provider, 'model': model, 'cached': True}

        def try_fn(name, fn):
            t0 = time.time()
            try:
                text, wait, extra = fn(messages, prompt)
            except Exception as e:
                log('provider %s exception: %s' % (name, e))
                return None
            if not text:
                log('provider %s failed in %.1fs: %s' % (name, time.time() - t0, extra))
                return None
            try:
                spec = normalize_spec(extract_json(text), prompt)
            except Exception as e:
                log('provider %s parse fail: %s :: %s' % (name, e, (text or '')[:240]))
                return None
            model = extra if isinstance(extra, str) and len(extra) < 80 else name
            cache_put(key, spec, name, model)
            log('provider %s ok in %.1fs (%s qs)' % (name, time.time() - t0, len(spec['questions'])))
            return {
                'ok': True,
                'spec': spec,
                'provider': name,
                'model': model,
                'cached': False,
            }

        for name, fn in PROVIDERS:
            result = try_fn(name, fn)
            if result:
                return result

        spec = draft_spec(prompt)
        cache_put(key, spec, 'formflow', 'formflow-draft')
        log('fallback draft used (%s qs)' % len(spec['questions']))
        return {
            'ok': True,
            'spec': spec,
            'provider': 'formflow',
            'model': 'formflow-draft',
            'cached': False,
        }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _cors(self):
        origin = self.headers.get('Origin') or '*'
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _cookies(self):
        out = {}
        raw = self.headers.get('Cookie') or ''
        for part in raw.split(';'):
            if '=' in part:
                k, v = part.strip().split('=', 1)
                out[k] = urllib.parse.unquote(v)
        return out

    def _public_base(self):
        forced = (os.environ.get('PUBLIC_BASE') or adb.meta_get('public_base') or '').strip().rstrip('/')
        if forced:
            return forced
        host = (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or ('localhost:%s' % PORT))
        host = host.split(',')[0].strip()
        proto = (self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip()
        if proto not in ('http', 'https'):
            if any(s in host for s in ('e2b.app', 'trycloudflare.com', 'ngrok', 'localhost.run')) or host.endswith(':443'):
                proto = 'https'
            else:
                proto = 'http'
        return proto + '://' + host

    def _user(self):
        return adb.user_from_session(self._cookies().get('ff_session'))

    def _require_user(self, create_guest=True):
        user = self._user()
        if user:
            return user, None
        if not create_guest:
            return None, None
        uid = adb.guest_user()
        token = adb.create_session(uid)
        return adb.user_from_session(token), token

    def _set_session_cookie(self, token):
        base = self._public_base()
        secure = '; Secure' if base.startswith('https') else ''
        self.send_header('Set-Cookie', 'ff_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000%s' % (token, secure))

    def _json(self, status, obj, extra_headers=None):
        data = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url, token=None):
        self.send_response(302)
        self.send_header('Location', url)
        if token:
            self._set_session_cookie(token)
        self.end_headers()

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(0, min(length, 2_000_000)))
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/api/health':
            self._json(200, {
                'ok': True,
                'service': 'formflow',
                'db': True,
                'google': adb.google_configured(),
                'providers': [p[0] for p in PROVIDERS],
            })
            return

        if path == '/api/me':
            user = self._user()
            base = self._public_base()
            self._json(200, {
                'user': ({'id': user['id'], 'email': user.get('email'), 'name': user.get('name'),
                          'picture': user.get('picture'), 'google': bool(user.get('google_id'))} if user else None),
                'googleConfigured': adb.google_configured(),
                'origin': base,
                'redirectUri': base + '/auth/google/callback',
            })
            return

        if path == '/api/forms':
            user, token = self._require_user()
            forms = adb.list_forms(user['id']) if user else []
            extra = []
            if token:
                extra.append(('Set-Cookie', self._cookie_header(token)))
            self._json(200, {'forms': forms}, extra)
            return

        if path.startswith('/api/forms/') and path.count('/') == 3:
            fid = path.split('/')[-1]
            form = adb.public_form(fid)
            if not form:
                self._json(404, {'error': 'not-found'})
                return
            self._json(200, {'form': form})
            return

        if path == '/auth/google':
            cid, secret = adb.google_creds()
            if not cid or not secret:
                self._redirect('/#/?google=setup')
                return
            state = secrets.token_urlsafe(24)
            adb.put_oauth_state(state)
            redirect_uri = self._public_base() + '/auth/google/callback'
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
                'client_id': cid,
                'redirect_uri': redirect_uri,
                'response_type': 'code',
                'scope': 'openid email profile',
                'access_type': 'online',
                'include_granted_scopes': 'true',
                'state': state,
                'prompt': 'select_account',
            })
            self._redirect(url)
            return

        if path == '/auth/google/callback':
            code = (qs.get('code') or [''])[0]
            state = (qs.get('state') or [''])[0]
            err = (qs.get('error') or [''])[0]
            if err or not code or not adb.take_oauth_state(state):
                self._redirect('/#/?google=error')
                return
            redirect_uri = self._public_base() + '/auth/google/callback'
            try:
                info = adb.exchange_google_code(code, redirect_uri)
            except Exception as e:
                log('google oauth failed: %s' % e)
                self._redirect('/#/?google=error')
                return
            prev = self._user()
            user = adb.upsert_google_user(info['google_id'], info['email'], info['name'], info.get('picture'))
            if prev and not prev.get('google_id'):
                adb.reassign_forms(prev['id'], user['id'])
            token = adb.create_session(user['id'])
            self._redirect('/#/?google=ok', token)
            return

        if path == '/auth/logout':
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'ff_session=; Path=/; Max-Age=0')
            self.end_headers()
            return

        return SimpleHTTPRequestHandler.do_GET(self)

    def _cookie_header(self, token):
        base = self._public_base()
        secure = '; Secure' if base.startswith('https') else ''
        return 'ff_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000%s' % (token, secure)

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        payload = self._read_json()

        if path == '/api/setup':
            try:
                cid = str(payload.get('clientId') or payload.get('client_id') or '').strip()
                secret = str(payload.get('clientSecret') or payload.get('client_secret') or '').strip()
                if not cid or not secret:
                    self._json(400, {'ok': False, 'error': 'Client ID and secret are required'})
                    return
                adb.meta_set('google_client_id', cid)
                adb.meta_set('google_client_secret', secret)
                log('google credentials saved')
                self._json(200, {'ok': True, 'googleConfigured': True})
            except Exception as e:
                log('setup failed: %s' % e)
                self._json(500, {'ok': False, 'error': 'Could not write credentials: %s' % e})
            return

        if path == '/api/forms/sync':
            user, token = self._require_user()
            forms = payload.get('forms') if isinstance(payload.get('forms'), list) else []
            adb.save_forms(user['id'], forms)
            extra = [('Set-Cookie', self._cookie_header(token))] if token else None
            self._json(200, {'ok': True, 'count': len(forms)}, extra)
            return

        if path.startswith('/api/forms/') and path.endswith('/responses'):
            fid = path.split('/')[3]
            resp = payload.get('response') or payload
            ok = adb.append_response(fid, resp)
            self._json(200 if ok else 404, {'ok': ok})
            return

        if path in ('/api/ai', '/api/generate-form'):
            if path == '/api/ai' and payload.get('messages') and not payload.get('prompt'):
                messages = payload.get('messages')[:12]
                text, wait, extra = call_pollinations_post(messages)
                if not text:
                    text, wait, extra = call_llm7('mistral-Nemo-Instruct-2407', messages)
                if not text:
                    self._json(200, {
                        'choices': [{'message': {'role': 'assistant', 'content': json.dumps(draft_spec('a feedback form'))}}],
                        'model': 'formflow-draft',
                    })
                    return
                self._json(200, {
                    'choices': [{'message': {'role': 'assistant', 'content': text}}],
                    'model': extra,
                })
                return

            prompt = str(payload.get('prompt') or '')
            if not prompt and payload.get('messages'):
                for m in reversed(payload.get('messages') or []):
                    if isinstance(m, dict) and m.get('role') == 'user':
                        prompt = str(m.get('content') or '')
                        break
            result = generate_form(prompt)
            self._json(200, result)
            return

        self.send_error(404)


if __name__ == '__main__':
    httpd = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log('FormFlow server on 0.0.0.0:%s db=%s google=%s' % (PORT, adb.DB_PATH, adb.google_configured()))
    httpd.serve_forever()
