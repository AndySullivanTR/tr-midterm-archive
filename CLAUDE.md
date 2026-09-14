# Midterm Archive — Claude Instructions

A Q&A interface over Reuters USA-ELECTION wire coverage of the 2026 U.S. midterms. Sibling to `iran-archive` -- same Neon DB, same Reuters Connect / OpenAI / Anthropic accounts, same magic-link auth pattern, restricted to `@thomsonreuters.com` users.

---

## What this app does

1. User submits a question at `/`
2. Flask backend (`api/index.py`) strips question framing and embeds the core topic (OpenAI `text-embedding-3-small`)
3. Hybrid search (vector 70% + Postgres FTS 30%) runs against `ea_reuters_stories`
4. Top 14 matches, reordered reverse-chronologically
5. Claude (`claude-sonnet-4-6`) synthesizes a Reuters-style answer with numbered citations `[N]` linking back to source URLs
6. Answer + source list returned to the UI

---

## Stack

| Component | Tech |
|---|---|
| Database | Neon PostgreSQL + pgvector (shared with iran-archive/trump-archive/trump-background, prefix `ea_`) |
| Embeddings | OpenAI `text-embedding-3-small` (1536 dims) |
| Synthesis | Anthropic `claude-sonnet-4-6` |
| Backend | Flask on Vercel |
| Auth | Magic-link 6-character code to `@thomsonreuters.com`, 3-hour expiry (shared `magic_tokens` table), sent via Resend from `midterm@andysullivan.net` |
| Frontend | Single-page HTML/JS, Reuters brand styling |
| Refresh | GitHub Actions cron, 3:30am UTC nightly (offset from trump-background's 3:00am and iran-archive's 3:15am) |

---

## Directory layout

```
midterm-archive/
├── api/index.py                       # Flask app -- auth, search, synthesis, HTML
├── refresh.py                         # Ingest: Reuters Connect -> ea_reuters_stories
├── apply_schema.py                    # Runs schema.sql via psycopg2 (no psql CLI needed)
├── probe_election_archive.py          # One-off diagnostic: confirms topicCodes/slug filters
├── query.py                           # Command-line Q&A (works without Vercel deploy, needs
│                                       #   an Anthropic key without network restrictions)
├── schema.sql                         # ea_reuters_stories table + indexes
├── requirements.txt
├── vercel.json
├── .env / .env.example
├── README.md
├── CLAUDE.md                          # this file
└── .github/workflows/nightly-refresh.yml
```

---

## Database

### `ea_reuters_stories`

Same shape as iran-archive's `ia_reuters_stories`: `uri` (PK), `headline`, `slug`, `fragment`, `body_text`, `byline`, `first_created`, `reuters_url`, `topic_codes`, `embedding vector(1536)`, `ingested_at`. See `schema.sql`.

### Shared tables (do not modify from this app)

- `magic_tokens` — auth tokens, shared with iran-archive/trump-archive/trump-background

---

## Reuters ingest (`refresh.py`)

- Slug filter: `(^|-)USA-ELECTION(-|/|$)` — matches `USA-ELECTION`, `USA-ELECTION/TEXAS`, `USA-ELECTION-2026`, etc.
- `topicCodes = ["VOTE"]` — confirmed via `probe_election_archive.py` against two known-busy primary days (PA-10 May 19, Iowa June 2 2026). `VOTE` is a narrow, election-specific editorial tag; it matched the same or more `USA-ELECTION`-slugged stories than the much broader `US`/`POL` country/politics tags did, with far less pagination.
- Deliberately **not** ingesting `USA-CONGRESS` or `USA-TRUMP` slug families — they're reachable only through `US`/`POL`, which are mostly non-election noise (routine legislative business, day-to-day Trump administration news). Revisit if a reporter needs that coverage; the fix would be a keyword-filtered pull under `US`+`POL`, not a full sweep.

CLI:
```bash
python refresh.py                                    # auto: catch up since last DB row
python refresh.py --days 7                           # force last N days
python refresh.py --from 2026-01-01                  # backfill from start of year
python refresh.py --from 2026-01-01 --to 2026-09-13  # bounded backfill
```

---

## Local development

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Create the table in Neon (psycopg2-based, no psql CLI needed)
python apply_schema.py

# 3. Initial backfill
python refresh.py --from 2026-01-01

# 4. Run the app
python api/index.py
# -> http://localhost:5003
```

Log in with any `@thomsonreuters.com` email. A 6-character code is emailed via Resend, from `midterm@andysullivan.net`.

**Known issue:** the local `ANTHROPIC_API_KEY` in `.env` hit a `403 permission_error — Access restricted by network policy` when called from Andy's machine directly (via `query.py`). This is why the app needs to be deployed to Vercel — the key's network allow-list likely covers Vercel's egress IPs (since iran-archive works fine there) but not arbitrary local/VPN traffic. Confirm this resolves once deployed; if `/api/query` still 403s from Vercel, the key's allow-list needs to be widened by whoever manages the Anthropic Console org.

---

## Vercel deployment

```bash
vercel deploy --prod
```

Set environment variables in the Vercel dashboard (Project Settings → Environment Variables) -- same set as `.env`:
- `DATABASE_URL`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `REUTERS_CLIENT_ID` / `REUTERS_CLIENT_SECRET` (not needed at query time, but harmless to set)
- `RESEND_API_KEY` / `EMAIL_FROM` (defaults to `Reuters Midterm Archive <midterm@andysullivan.net>` if unset)
- `SECRET_KEY`

`vercel.json` is minimal (`{"version": 2}`) -- Vercel auto-detects `api/index.py` as a Python serverless function via its standard convention. No explicit routes/build config needed (same as iran-archive).

---

## Nightly refresh (GitHub Actions)

Workflow: `.github/workflows/nightly-refresh.yml`. Cron `30 3 * * *` (3:30am UTC).

Secrets needed in GitHub repo settings → Secrets and variables → Actions:
- `DATABASE_URL`
- `OPENAI_API_KEY`
- `REUTERS_CLIENT_ID`
- `REUTERS_CLIENT_SECRET`

Trigger manually from the Actions UI to verify after the first deploy.

---

## Code notes

- Flask app and HTML templates are inline in `api/index.py` (`LOGIN_HTML`, `MAIN_HTML`), same as iran-archive, for Vercel serverless deployment.
- Background watermark image: `public/bg.jpg` (Dallas photo, credit Callaghan O'Hare/Reuters, noted in the `.corpus-note` footer), same `body::before` CSS rule as iran-archive (`opacity: 0.25`, `background-size: cover`, `z-index: -1`) in both `LOGIN_HTML` and `MAIN_HTML`. Served from Vercel's static `public/` convention.
- `REUTERS_TOP_K = 14` controls how many sources feed the synthesis prompt. Single corpus (no Trump-statements cross-reference like iran-archive has) -- full budget always goes to Reuters.
- The hybrid search SQL is in `search_reuters()`, same 0.7 × vector + 0.3 × FTS shape as iran-archive.
- `ARCHIVE_START_DATE = "2026-01-01"` is the default lower bound for queries.
- Source URLs power both the `[N]` citation linkification and the source-list badges.
- `body_text` truncated to 2500 chars per source in the Claude prompt.
- Vercel function timeout is 60s (default, not explicitly set in `vercel.json`). If query time runs long, the bottleneck is almost certainly Claude synthesis (5-15s) plus the search SQL (1-3s).

---

## When making changes

- Do not store passwords, SSNs, or other sensitive PII in the database.
- The shared Neon DB also powers iran-archive/trump-archive/trump-background. Schema changes here must be additive -- never `DROP` or modify tables owned by siblings.
- The magic-link auth flow uses uppercase alphanumeric codes excluding `O/0/I/1/L` for legibility, same as siblings.
