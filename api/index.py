#!/usr/bin/env python3
"""
Reuters Midterm Archive — query interface over Reuters USA-ELECTION wire coverage.

Magic-link auth (@thomsonreuters.com) shared with sibling apps (iran-archive,
trump-archive, trump-background) via the shared magic_tokens table.
"""

import os
import re
import sys
import time
import secrets
import string
import logging
from datetime import datetime, timedelta, date as date_
from functools import wraps
from pathlib import Path

import psycopg2
import psycopg2.extras
import httpx
from anthropic import Anthropic
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# =============================================================================
# CONFIG
# =============================================================================

DATABASE_URL      = os.getenv('DATABASE_URL')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
OPENAI_API_KEY    = os.getenv('OPENAI_API_KEY')
SECRET_KEY        = os.getenv('SECRET_KEY', secrets.token_hex(32))
RESEND_API_KEY    = os.getenv('RESEND_API_KEY')
EMAIL_FROM        = os.getenv('EMAIL_FROM', 'Reuters Midterm Archive <midterm@andysullivan.net>')

EMBEDDING_MODEL = "text-embedding-3-small"
CLAUDE_MODEL    = "claude-sonnet-4-6"

REUTERS_TOP_K = 14

# Default earliest date for the archive
ARCHIVE_START_DATE = "2026-01-01"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = SECRET_KEY


# =============================================================================
# DATABASE
# =============================================================================

def get_db():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


# =============================================================================
# AUTH (lifted from iran-archive/trump-background, branding swapped)
# =============================================================================

def validate_tr_email(email):
    if not email:
        return False
    email = email.lower().strip()
    return (email.endswith('@thomsonreuters.com') and
            bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email)))


def generate_code():
    chars = (string.ascii_uppercase + string.digits).translate(str.maketrans('', '', 'O0I1L'))
    return ''.join(secrets.choice(chars) for _ in range(6))


def check_rate_limit(ip):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT COUNT(*) as count FROM magic_tokens
            WHERE ip_address = %s
            AND created_at > CURRENT_TIMESTAMP - make_interval(mins => 15)
        """, (ip,))
        result = cur.fetchone()
        cur.close(); conn.close()
        return (result['count'] if result else 0) < 5
    except Exception as e:
        log.error(f"Rate limit check failed: {e}")
        return True


def store_magic_token(email, token, code, expires_at, ip, user_agent):
    conn = None
    try:
        conn = get_db(); conn.autocommit = False
        cur = conn.cursor()
        cur.execute("DELETE FROM magic_tokens WHERE email = %s", (email,))
        cur.execute("""
            INSERT INTO magic_tokens (email, token, code, expires_at, used, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, FALSE, %s, %s)
        """, (email, token, code, expires_at, ip, user_agent))
        conn.commit(); cur.close()
        return True
    except Exception as e:
        if conn: conn.rollback()
        log.error(f"Token store error: {e}")
        return False
    finally:
        if conn: conn.close()


def validate_and_consume_code(code):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE magic_tokens SET used = TRUE
            WHERE code = %s AND expires_at > CURRENT_TIMESTAMP AND used = FALSE
            RETURNING *
        """, (code.upper(),))
        conn.commit(); result = cur.fetchone(); cur.close(); conn.close()
        return result
    except Exception as e:
        log.error(f"Code validation error: {e}")
        return None


def send_magic_email(email, code):
    try:
        text_body = f"""Reuters Midterm Archive — Login

Enter this code to log in:

    {code}

This code expires in 3 hours.

If you didn't request this, you can safely ignore this email.
"""
        html_body = f"""<!DOCTYPE html>
<html><head><style>
body {{ font-family: Georgia, serif; line-height: 1.6; color: #000; background: #fff; margin: 0; padding: 20px; }}
.container {{ max-width: 600px; margin: 0 auto; }}
.header {{ border-top: 4px solid #d64000; border-bottom: 1px solid #ccc; padding: 16px 0; margin-bottom: 24px; }}
.header h1 {{ font-size: 22px; font-weight: bold; color: #000; margin: 0; }}
.header p {{ font-size: 13px; color: #666; margin: 4px 0 0 0; }}
.code-box {{ border: 2px solid #d64000; padding: 20px; text-align: center; margin: 24px 0; }}
.code-box span {{ font-size: 38px; font-weight: bold; letter-spacing: 10px; color: #d64000; font-family: monospace; }}
.footer {{ font-size: 12px; color: #999; border-top: 1px solid #eee; padding-top: 16px; margin-top: 24px; }}
</style></head><body><div class="container">
<div class="header">
    <h1>Midterm Archive</h1>
    <p>Reuters research tool</p>
</div>
<p>Enter this code on the login page:</p>
<div class="code-box"><span>{code}</span></div>
<p style="font-size: 13px; color: #666;">This code expires in 3 hours. If you didn't request this, ignore this email.</p>
<div class="footer">For Thomson Reuters journalists only.</div>
</div></body></html>"""

        r = httpx.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'from':    EMAIL_FROM,
                'to':      [email],
                'subject': 'Reuters Midterm Archive — Login Code',
                'text':    text_body,
                'html':    html_body,
            },
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('email'):
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


# =============================================================================
# SEARCH
# =============================================================================

def embed_query(text):
    r = httpx.post(
        'https://api.openai.com/v1/embeddings',
        headers={'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'},
        json={'model': EMBEDDING_MODEL, 'input': [text]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()['data'][0]['embedding']


# Strip question scaffolding so "What happened in the PA-10 primary?" and
# "What have we written about the PA-10 primary?" both reduce to "the PA-10
# primary" before embedding. This aligns the query vector with wire story
# embeddings, which are journalistic prose, not questions.
_QUESTION_STRIP = re.compile(
    r"""^(?:
        what\s+happened\s+(?:in|at|to|with|during|after|before)\s+|
        what\s+(?:have|did)\s+(?:we|reuters)\s+(?:write|written|report|reported|cover|covered|publish|published|say|said)\s+(?:about|on|regarding)\s+|
        what\s+(?:is|was|are|were)\s+(?:the\s+)?(?:status|situation|position|state|stance|view|role)\s+(?:of|on|in|regarding|about)\s+|
        what\s+(?:is|was|are|were)\s+(?:the\s+)?(?:latest|current|recent|new)\s+(?:(?:developments?|updates?|news|reports?|information)\s+)?(?:on|about|regarding|in|with)\s+|
        what\s+(?:is|are|was|were)\s+(?:happening|going\s+on)\s+(?:in|with|at|regarding)?\s+|
        (?:tell|show|give)\s+me\s+(?:about|what\s+you\s+know\s+about|everything\s+(?:about|on)|all\s+about)\s+|
        (?:have|did|do|does)\s+(?:we|reuters)\s+(?:have\s+(?:coverage\s+of|any\s+(?:stories|reports)\s+(?:about|on))|cover(?:ed)?|report(?:ed)?\s+on|writ(?:e|ten)\s+about)\s+|
        (?:is|are|was|were)\s+there\s+(?:any\s+)?(?:coverage|reporting|stories|reports|articles|news)\s+(?:about|on|of|regarding)\s+|
        (?:search\s+(?:for|about)|find(?:\s+me)?|get\s+me|look\s+up)\s+(?:(?:stories|reports|coverage|articles|information)\s+(?:about|on|of|regarding)\s+)?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

def make_search_query(user_query: str) -> str:
    """Strip question framing to extract the core topic for embedding."""
    stripped = _QUESTION_STRIP.sub('', user_query.strip(), count=1).rstrip('?.! ').strip()
    return stripped if len(stripped) >= 3 else user_query


def search_reuters(query, vec_str, top_k, date_from, date_to):
    """Hybrid (vector + FTS) search over ea_reuters_stories."""
    date_clauses = []
    date_params  = []
    if date_from:
        date_clauses.append("first_created >= %s")
        date_params.append(date_from)
    if date_to:
        date_clauses.append("first_created <= %s")
        date_params.append(date_to + ' 23:59:59' if len(date_to) == 10 else date_to)

    date_where = (' AND ' + ' AND '.join(date_clauses)) if date_clauses else ''

    # params layout (ORDER follows SQL placeholder order):
    #   vector_cands : vec_str (score), *date_params, vec_str (ORDER BY)
    #   fts_cands    : vec_str (score), *date_params, query   (FTS WHERE)
    #   scored       : query  (ts_rank_cd)
    #   final LIMIT  : top_k
    params = (
        [vec_str] + date_params + [vec_str] +
        [vec_str] + date_params + [query]   +
        [query, top_k]
    )

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        WITH vector_cands AS (
            -- Top 200 by cosine similarity (primary retrieval path)
            SELECT uri, headline, slug, fragment, body_text, byline,
                   first_created, reuters_url,
                   1 - (embedding <=> %s::vector) AS vec_score
            FROM ea_reuters_stories
            WHERE embedding IS NOT NULL {date_where}
            ORDER BY embedding <=> %s::vector
            LIMIT 200
        ),
        fts_cands AS (
            -- FTS fallback: catches exact term matches that rank outside the
            -- top-200 vector window (e.g. specific place names, proper nouns)
            SELECT uri, headline, slug, fragment, body_text, byline,
                   first_created, reuters_url,
                   1 - (embedding <=> %s::vector) AS vec_score
            FROM ea_reuters_stories
            WHERE embedding IS NOT NULL {date_where}
              AND to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(body_text,''))
                  @@ plainto_tsquery('english', %s)
            LIMIT 50
        ),
        candidates AS (
            SELECT * FROM vector_cands
            UNION
            SELECT * FROM fts_cands
        ),
        scored AS (
            SELECT *,
                   ts_rank_cd(
                       to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(body_text,'')),
                       plainto_tsquery('english', %s)
                   ) AS fts_score
            FROM candidates
        ),
        final AS (
            SELECT *,
                   (0.7 * vec_score + 0.3 * fts_score) AS score,
                   regexp_replace(
                       regexp_replace(upper(slug), '\\s*\\([^)]*\\)', '', 'g'),
                       '/', '-', 'g'
                   ) AS norm_slug
            FROM scored
        ),
        deduped AS (
            -- Reuters republishes the same story under multiple slugs/URIs
            -- (UPDATE 1, WRAPUP, base slug). Keep the highest-scoring version
            -- of each (date, normalized-slug) group.
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY first_created::date, norm_slug
                ORDER BY score DESC, length(body_text) DESC
            ) AS rn
            FROM final
        )
        SELECT * FROM deduped WHERE rn = 1 ORDER BY score DESC LIMIT %s
    """, params)
    rows = cur.fetchall()
    cur.close(); conn.close()

    return [{
        'id':          row['uri'],
        'source_type': 'reuters',
        'doc_type':    'wire',
        'title':       row['headline'],
        'slug':        row['slug'],
        'date':        row['first_created'].date().isoformat() if row['first_created'] else '',
        'url':         row['reuters_url'],
        'text':        (row['body_text'] or row['fragment'] or '')[:2500],
        'byline':      row['byline'],
        'score':       float(row['score']),
    } for row in rows]


def search_unified(query, date_from=None, date_to=None):
    search_q = make_search_query(query)
    log.info("search_q: %r (from query: %r)", search_q, query)
    vec = embed_query(search_q)
    vec_str = '[' + ','.join(str(v) for v in vec) + ']'
    return search_reuters(search_q, vec_str, REUTERS_TOP_K, date_from, date_to)


# =============================================================================
# CORPUS STATS
# =============================================================================

def get_corpus_stats():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT COUNT(*) AS n,
               MIN(first_created) AS date_from,
               MAX(first_created) AS date_to
        FROM ea_reuters_stories
        WHERE embedding IS NOT NULL
    """)
    r = cur.fetchone()
    cur.close(); conn.close()
    return {
        'reuters_count': r['n'] or 0,
        'reuters_from':  r['date_from'].date().isoformat() if r['date_from'] else '',
        'reuters_to':    r['date_to'].date().isoformat() if r['date_to'] else '',
    }


# =============================================================================
# CLAUDE SYNTHESIS
# =============================================================================

def call_claude(prompt, max_tokens=1200):
    client = Anthropic(
        api_key=ANTHROPIC_API_KEY,
        timeout=120,
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{'role': 'user', 'content': prompt}],
    )
    return response.content[0].text


def synthesize_answer(query, sources):
    blocks = []
    for i, s in enumerate(sources, 1):
        tag = f"REUTERS | {s.get('slug') or ''} | {s['date']}"
        byline = f"\nByline: {s['byline']}" if s.get('byline') else ''
        blocks.append(
            f"SOURCE {i} [{tag}]\nHeadline: {s['title']}{byline}\nURL: {s['url']}\nText: {s['text']}\n"
        )

    sources_block = '\n---\n'.join(blocks)

    prompt = f"""You are a Reuters research assistant. A journalist has asked: "{query}"

All sources below are Reuters wire stories about the 2026 U.S. midterm elections (slug USA-ELECTION). Each is tagged with slug and date. **Sources are listed in reverse-chronological order — SOURCE 1 is the most recent.** Use ONLY these sources.

ANSWER GUIDANCE:
- For "have we written/reported on X?" questions: state plainly whether the archive has coverage, then summarize what it says, leading with the most relevant or most recent story.
- For "what is the current status of X?" or "where do things stand on X?" questions: open with ONE present-tense sentence summarizing where things stand right now, then the most recent concrete development with its date, then relevant background. Use inverted pyramid: most important info first. Aim for 120-200 words.
- For "how many stories have we run on X?" questions: count and list them plainly with dates and headlines rather than writing prose.
- Always include the date inline when describing a specific event or development.
- Reuters wire style: active voice, short sentences, no editorializing. Aim for 150-350 words unless a different range is specified above.
- If the sources don't contain enough information to answer, say so plainly rather than guessing.

CITATIONS:
- After each factual claim, add a bracketed reference like [1] or [2] corresponding to the source number below.
- Multiple citations per sentence are fine: [1][3].
- Do not fabricate quotes or facts not present in the sources.

SOURCES:
{sources_block}

Answer now. No preamble."""

    return call_claude(prompt, max_tokens=1500).strip()


# =============================================================================
# HTML TEMPLATES
# =============================================================================

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reuters Midterm Archive — Login</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Reem+Kufi:wght@400;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Georgia, 'Times New Roman', serif; background: #f4f8f4; color: #212223; line-height: 1.6; position: relative; }
        body::before { content: ''; position: fixed; inset: 0; background-image: url('/bg.jpg'); background-size: cover; background-position: center top; opacity: 0.25; z-index: -1; pointer-events: none; }
        .container { max-width: 480px; margin: 60px auto; padding: 20px; }
        header { border-top: 4px solid #d64000; border-bottom: 1px solid #ccc; padding: 20px 0; margin-bottom: 30px; }
        h1 { font-size: 26px; font-weight: 700; color: #000; font-family: 'Reem Kufi', sans-serif; }
        .subtitle { font-size: 13px; color: #666; margin-top: 4px; }
        .card { background: #fff; border: 1px solid #ddd; padding: 28px; }
        label { display: block; font-weight: bold; margin-bottom: 8px; font-size: 14px; }
        input { width: 100%; padding: 10px 12px; font-family: Georgia, serif; font-size: 15px; border: 1px solid #ccc; border-radius: 2px; margin-bottom: 14px; }
        input:focus { outline: none; border-color: #d64000; }
        button { background: #d64000; color: #fff; border: none; padding: 10px 24px; font-size: 15px; font-family: Arial, sans-serif; cursor: pointer; width: 100%; }
        button:hover { background: #bf3800; }
        button:disabled { background: #999; cursor: not-allowed; }
        .error { color: #c00; font-size: 14px; margin-top: 12px; padding: 10px; background: #fff0f0; border: 1px solid #fcc; display: none; }
        .note { font-size: 13px; color: #888; margin-top: 16px; }
        .step { display: none; } .step.active { display: block; }
        .code-input { font-size: 24px; letter-spacing: 6px; text-align: center; font-family: monospace; text-transform: uppercase; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Reuters Midterm Archive</h1>
            <div class="subtitle">Reuters research tool</div>
        </header>
        <div class="card">
            <div class="step active" id="step-email">
                <label for="email">Thomson Reuters email</label>
                <input type="email" id="email" placeholder="yourname@thomsonreuters.com" />
                <button onclick="sendCode()">Send login code</button>
                <div class="error" id="email-error"></div>
                <p class="note">A 6-character code will be emailed to you.</p>
            </div>
            <div class="step" id="step-code">
                <label for="code">Enter your login code</label>
                <input type="text" id="code" class="code-input" maxlength="6" placeholder="ABC123" />
                <button onclick="verifyCode()">Log in</button>
                <div class="error" id="code-error"></div>
                <p class="note">Check your Thomson Reuters email. Code expires in 3 hours.</p>
            </div>
        </div>
    </div>
    <script>
        async function sendCode() {
            const email = document.getElementById('email').value.trim();
            const err = document.getElementById('email-error');
            err.style.display = 'none';
            try {
                const r = await fetch('/auth/send-code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Failed');
                document.getElementById('step-email').classList.remove('active');
                document.getElementById('step-code').classList.add('active');
                document.getElementById('code').focus();
            } catch(e) { err.textContent = e.message; err.style.display = 'block'; }
        }
        async function verifyCode() {
            const code = document.getElementById('code').value.trim();
            const err = document.getElementById('code-error');
            err.style.display = 'none';
            try {
                const r = await fetch('/auth/verify-code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || 'Invalid code');
                window.location.href = '/';
            } catch(e) { err.textContent = e.message; err.style.display = 'block'; }
        }
        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('email').addEventListener('keydown', e => { if (e.key==='Enter') sendCode(); });
            document.getElementById('code').addEventListener('keydown',  e => { if (e.key==='Enter') verifyCode(); });
        });
    </script>
</body>
</html>"""


MAIN_HTML = """<!DOCTYPE html>
<!-- deploy-test: 2026-09-14 -->
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reuters Midterm Archive</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Reem+Kufi:wght@400;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Georgia, 'Times New Roman', serif; background: #f4f8f4; color: #212223; line-height: 1.6; position: relative; }
        body::before { content: ''; position: fixed; inset: 0; background-image: url('/bg.jpg'); background-size: cover; background-position: center top; opacity: 0.25; z-index: -1; pointer-events: none; }
        .container { max-width: 860px; margin: 0 auto; padding: 20px; }
        header { border-top: 4px solid #d64000; border-bottom: 1px solid #ccc; padding: 20px 0; margin-bottom: 28px; display: flex; justify-content: space-between; align-items: flex-end; }
        h1 { font-size: 28px; font-weight: 700; font-family: 'Reem Kufi', sans-serif; }
        .subtitle { font-size: 13px; color: #666; margin-top: 4px; }
        .logout { font-size: 13px; color: #888; font-family: Arial, sans-serif; }
        .logout a { color: #d64000; text-decoration: none; }
        .logout a:hover { text-decoration: underline; }

        .query-row { display: flex; gap: 10px; margin-bottom: 12px; }
        textarea { flex: 1; height: 84px; padding: 10px 12px; font-family: Georgia, serif; font-size: 15px; border: 1px solid #ccc; border-radius: 2px; resize: vertical; }
        textarea:focus { outline: none; border-color: #d64000; }
        button.submit { background: #d64000; color: #fff; border: none; padding: 10px 22px; font-size: 15px; font-family: Arial, sans-serif; cursor: pointer; align-self: flex-end; white-space: nowrap; }
        button.submit:hover { background: #bf3800; }
        button.submit:disabled { background: #999; cursor: not-allowed; }

        .corpus-desc { font-size: 13px; color: #666; font-family: Arial, sans-serif; margin-bottom: 6px; line-height: 1.6; border-left: 3px solid #e3e3e3; padding-left: 12px; }
        .corpus-note { font-size: 12px; color: #555; font-family: Arial, sans-serif; margin-bottom: 22px; line-height: 1.5; padding-left: 15px; }
        .corpus-note a { color: #555; }

        .loading { color: #666; font-style: italic; margin: 16px 0; display: none; }
        .error-msg { color: #c00; margin: 16px 0; padding: 10px; background: #fff0f0; border: 1px solid #fcc; display: none; }

        .answer-block { background: #fff; border-left: 4px solid #d64000; padding: 20px; margin-bottom: 20px; font-size: 16px; line-height: 1.7; display: none; }
        .answer-block p { margin-bottom: 12px; }
        .answer-block p:last-child { margin-bottom: 0; }

        .sources-section { display: none; }
        .sources-section h3 { font-size: 15px; margin-bottom: 10px; color: #333; border-bottom: 1px solid #ddd; padding-bottom: 6px; font-family: Arial, sans-serif; }
        .source-item { padding: 8px 0; border-bottom: 1px solid #eee; font-size: 13px; font-family: Arial, sans-serif; }
        .source-item:last-child { border-bottom: none; }
        .source-item a { color: #d64000; text-decoration: none; }
        .source-item a:hover { text-decoration: underline; }
        .source-badge { display: inline-block; background: #fff3ee; border: 1px solid #d64000; border-radius: 2px; padding: 1px 6px; font-size: 11px; color: #d64000; margin-right: 6px; }
        .source-date { color: #888; }

        .copy-btn { background: #666; color: #fff; border: none; padding: 5px 14px; font-size: 12px; font-family: Arial, sans-serif; cursor: pointer; margin-top: 10px; }
        .copy-btn:hover { background: #444; }

        .stats { font-size: 11px; color: #aaa; margin-top: 16px; font-family: monospace; }
        .page-footer { font-size: 12px; color: #aaa; font-family: Arial, sans-serif; margin-top: 40px; padding-top: 16px; border-top: 1px solid #e3e3e3; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div>
            <h1>Reuters Midterm Archive</h1>
            <div class="subtitle">Search Reuters coverage of the 2026 U.S. midterm elections.</div>
        </div>
        <div class="logout">{{ email }} &mdash; <a href="/auth/logout">Log out</a></div>
    </header>

    <div class="query-row">
        <textarea id="query" placeholder="e.g. Have we covered Georgia's new voter ID law? Which Democratic candidates are running as people of faith? What happened in the PA-10 primary?"></textarea>
        <button class="submit" id="submit-btn" onclick="runQuery()">Search</button>
    </div>

    <div class="corpus-desc" id="corpus-desc">Loading corpus stats...</div>
    <div class="corpus-note">Built by Andy Sullivan for Reuters. Powered by Claude AI. Feedback welcome at andy.sullivan (at) thomsonreuters.com<br>Photo by Callaghan O'Hare/Reuters</div>

    <div class="loading" id="loading">Searching archive...</div>
    <div class="error-msg" id="error"></div>

    <div class="answer-block" id="answer-block"></div>
    <button class="copy-btn" id="copy-btn" onclick="copyAnswer()" style="display:none">Copy answer</button>

    <div class="sources-section" id="sources-section">
        <h3>Sources used</h3>
        <div id="sources-list"></div>
        <div class="stats" id="stats"></div>
    </div>

    <div class="page-footer">
    </div>
</div>

<script>
    // Load corpus stats
    const statsCtrl = new AbortController();
    const statsTimeout = setTimeout(() => statsCtrl.abort(), 15000);
    fetch('/api/stats', { signal: statsCtrl.signal }).then(r => {
        clearTimeout(statsTimeout);
        if (!r.ok) throw new Error('Stats request failed');
        return r.json();
    }).then(d => {
        const fmt = n => (n || 0).toLocaleString();
        const today = new Date().toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'});
        const rFrom = d.reuters_from ? new Date(d.reuters_from + 'T12:00:00').toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}) : '';
        const rTo   = d.reuters_to   ? new Date(d.reuters_to   + 'T12:00:00').toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}) : '';
        const reutersRange = (rFrom && rTo) ? ` (${rFrom}–${rTo})` : '';
        document.getElementById('corpus-desc').innerHTML =
            `Drawing on <strong>${fmt(d.reuters_count)} Reuters USA-ELECTION stories</strong>${reutersRange}. As of ${today}.`;
    }).catch(() => {
        clearTimeout(statsTimeout);
        document.getElementById('corpus-desc').textContent = 'Could not load corpus stats. Search is still available.';
    });

    async function runQuery() {
        const query = document.getElementById('query').value.trim();
        if (!query) return;

        const btn     = document.getElementById('submit-btn');
        const loading = document.getElementById('loading');
        const errDiv  = document.getElementById('error');
        const ansDiv  = document.getElementById('answer-block');
        const copyBtn = document.getElementById('copy-btn');
        const srcSec  = document.getElementById('sources-section');

        btn.disabled = true;
        loading.style.display = 'block';
        errDiv.style.display = 'none';
        ansDiv.style.display = 'none';
        copyBtn.style.display = 'none';
        srcSec.style.display = 'none';

        const payload = { query };

        try {
            const queryCtrl = new AbortController();
            const queryTimeout = setTimeout(() => queryCtrl.abort(), 55000);
            const r = await fetch('/api/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
                signal: queryCtrl.signal,
            });
            clearTimeout(queryTimeout);
            if (r.status === 401) { window.location.href = '/login'; return; }
            let d;
            try { d = await r.json(); } catch(_) {
                throw new Error('Server error (status ' + r.status + '). Try again.');
            }
            if (!r.ok) throw new Error(d.error || 'Request failed');

            // Linkify [N] citations
            const srcUrls = {};
            d.sources.forEach((s, i) => { if (s.url) srcUrls[i+1] = s.url; });
            const citRe = /\\[(\\d+)\\]/g;
            const linked = d.answer.replace(citRe, function(m, num) {
                const url = srcUrls[parseInt(num)];
                if (!url) return m;
                return '<a href="' + url + '" target="_blank" style="color:#d64000;font-weight:bold;text-decoration:none" title="Source ' + num + '">[' + num + ']</a>';
            });
            ansDiv.innerHTML = linked.split('\\n\\n').map(function(p) {
                return '<p>' + p.replace(/\\n/g, '<br>') + '</p>';
            }).join('');
            ansDiv.style.display = 'block';
            copyBtn.style.display = 'inline-block';

            // Render source list
            const srcList = document.getElementById('sources-list');
            srcList.innerHTML = d.sources.map((s, i) => {
                const title = s.title || s.id || 'View';
                const link = s.url
                    ? `<a href="${s.url}" target="_blank">${title}</a>`
                    : `<span>${title}</span>`;
                return `<div class="source-item">` +
                       `<span style="font-family:monospace;color:#aaa;font-size:11px">[${i+1}]</span> ` +
                       `<span class="source-badge">reuters</span>` +
                       ` ${link} <span class="source-date">${s.date}</span></div>`;
            }).join('');

            document.getElementById('stats').textContent =
                `${d.elapsed.toFixed(1)}s · ${d.sources.length} sources · ${(d.total_docs || 0).toLocaleString()} docs in corpus`;
            srcSec.style.display = 'block';
        } catch(e) {
            errDiv.textContent = e.name === 'AbortError'
                ? 'Search timed out. The server may be warming up — try again.'
                : e.message;
            errDiv.style.display = 'block';
        } finally {
            btn.disabled = false;
            loading.style.display = 'none';
        }
    }

    function copyAnswer() {
        const text = document.getElementById('answer-block').innerText;
        navigator.clipboard.writeText(text).then(() => {
            const btn = document.getElementById('copy-btn');
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = 'Copy answer', 2000);
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('query').addEventListener('keydown', e => {
            if (e.ctrlKey && e.key === 'Enter') runQuery();
        });
    });
</script>
</body>
</html>"""


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/login')
def login_page():
    if session.get('email'):
        return redirect(url_for('index'))
    return LOGIN_HTML


@app.route('/auth/send-code', methods=['POST'])
def send_code():
    data = request.get_json()
    email = (data or {}).get('email', '').lower().strip()
    if not validate_tr_email(email):
        return jsonify({'error': 'A @thomsonreuters.com email is required.'}), 400
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    if not check_rate_limit(ip):
        return jsonify({'error': 'Too many requests. Try again in 15 minutes.'}), 429
    token = secrets.token_urlsafe(32)
    code = generate_code()
    expires_at = datetime.utcnow() + timedelta(hours=3)
    if not store_magic_token(email, token, code, expires_at, ip, request.headers.get('User-Agent', '')):
        return jsonify({'error': 'Server error. Please try again.'}), 500
    if not send_magic_email(email, code):
        return jsonify({'error': 'Failed to send email. Please try again.'}), 500
    return jsonify({'ok': True})


@app.route('/auth/verify-code', methods=['POST'])
def verify_code():
    data = request.get_json()
    code = (data or {}).get('code', '').strip()
    if not code:
        return jsonify({'error': 'No code provided.'}), 400
    result = validate_and_consume_code(code)
    if not result:
        return jsonify({'error': 'Invalid or expired code.'}), 401
    session['email'] = result['email']
    session.permanent = True
    return jsonify({'ok': True})


@app.route('/auth/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


@app.route('/')
@login_required
def index():
    return render_template_string(MAIN_HTML, email=session['email'])


@app.route('/api/query', methods=['POST'])
@login_required
def query_endpoint():
    start = time.time()
    data = request.get_json()
    query = (data or {}).get('query', '').strip()
    if not query:
        return jsonify({'error': 'No query provided'}), 400

    date_from = (data or {}).get('date_from') or ARCHIVE_START_DATE
    date_to   = (data or {}).get('date_to')

    try:
        sources = search_unified(query, date_from, date_to)
        if not sources:
            return jsonify({'error': 'No matching documents found.'}), 404

        # Reorder reverse-chronologically so [1] is the most recent source.
        sources.sort(key=lambda s: s.get('date') or '0000-00-00', reverse=True)

        answer = synthesize_answer(query, sources)
        elapsed = time.time() - start
        stats = get_corpus_stats()
        total_docs = stats['reuters_count']

        return jsonify({
            'answer':  answer,
            'sources': [{
                'id':          s.get('id'),
                'source_type': s.get('source_type'),
                'doc_type':    s.get('doc_type'),
                'title':       s.get('title'),
                'date':        s.get('date'),
                'url':         s.get('url'),
                'slug':        s.get('slug'),
                'score':       s.get('score'),
            } for s in sources],
            'elapsed':    elapsed,
            'total_docs': total_docs,
        })
    except Exception as e:
        log.error(f"Query error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/stats')
@login_required
def stats_endpoint():
    try:
        return jsonify(get_corpus_stats())
    except Exception as e:
        log.error(f"Stats error: {e}")
        return jsonify({'reuters_count': 0, 'reuters_from': '', 'reuters_to': ''})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5003))
    app.run(host='0.0.0.0', port=port, debug=True)
