# Midterm Archive

A Q&A tool over Reuters USA-ELECTION wire coverage of the 2026 U.S. midterms. Sibling to `iran-archive` -- same Neon DB, same Reuters Connect / OpenAI / Anthropic accounts, same magic-link auth restricted to `@thomsonreuters.com`.

Two ways to use it:
1. **Command line** (`query.py`) -- works today, but hit a `403 permission_error` from Andy's local machine because the Anthropic key's network policy likely doesn't allow-list arbitrary local traffic.
2. **Hosted web app** (`api/index.py` on Vercel) -- same as `iran-archive`. This is the fix for the 403: the key's allow-list almost certainly already covers Vercel's egress IPs since iran-archive runs fine there.

## What it does

1. `refresh.py` pulls Reuters wire stories tagged `USA-ELECTION` (matches `(^|-)USA-ELECTION(-|/|$)`) via `topicCodes=["VOTE"]` into `ea_reuters_stories` with OpenAI embeddings.
2. Ask plain-English questions -- e.g. "Have we covered Georgia's new voter ID law?" -- and get a Reuters-style answer with numbered citations, synthesized by Claude from the archive.

## Setup (already done)

```bash
pip install -r requirements.txt
python probe_election_archive.py    # confirmed topicCodes=["VOTE"] is the right filter
python apply_schema.py              # created ea_reuters_stories table
```

## Next: backfill and deploy

```bash
# 1. Backfill this year's stories (if not already run)
python refresh.py --from 2026-01-01

# 2. Try the CLI (may hit the 403 network-policy error -- that's expected, see below)
python query.py "have we covered Georgia's new voter ID law?"

# 3. Deploy the hosted app
vercel deploy --prod
```

Then in the Vercel dashboard (Project Settings -> Environment Variables), set:
`DATABASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `REUTERS_CLIENT_ID`, `REUTERS_CLIENT_SECRET`, `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `SMTP_SERVER`, `SMTP_PORT`, `SECRET_KEY` -- all already populated in your local `.env`, just copy them over.

Once deployed, log in at the Vercel URL with any `@thomsonreuters.com` email (6-character code emailed via Gmail, same sender as iran-archive).

## Sharing with colleagues

The Vercel URL works for anyone with a `@thomsonreuters.com` email -- no separate invite needed, the magic-link auth handles it. Just share the link.

## Keeping it current

`.github/workflows/nightly-refresh.yml` runs `refresh.py` automatically at 3:30am UTC once you push this repo to GitHub and set the four secrets (`DATABASE_URL`, `OPENAI_API_KEY`, `REUTERS_CLIENT_ID`, `REUTERS_CLIENT_SECRET`) in the repo's Actions settings. Until then, run `python refresh.py` manually.

## Design decisions (see CLAUDE.md for full detail)

- **`topicCodes=["VOTE"]`, not `US`/`POL`**: confirmed via probing two known-busy primary days. `VOTE` is a narrow election-specific tag; `US`/`POL` are broad country/politics tags that would require paging through 15-25x more volume for the same recall.
- **`USA-CONGRESS` and `USA-TRUMP` slugs are out of scope for now**: they're not reachable through `VOTE`, and pulling them via `US`/`POL` would mean mostly non-election noise (routine legislative business, day-to-day Trump news). Can revisit with a keyword-filtered approach if needed.
