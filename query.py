#!/usr/bin/env python3
"""
Command-line Q&A over the USA Election Archive. Mirrors iran-archive/_query.py.

Embeds the question, runs a hybrid vector+FTS search against
ea_reuters_stories, and asks Claude to synthesize a cited answer from the
top matches.

Usage:
    python query.py "have we covered Georgia's new voter ID law?"
    python query.py "how many stories have we run on mail-in ballots this year?"
"""

import os
import sys
import psycopg2
import psycopg2.extras
import anthropic
import openai
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

TOP_K = 14


def get_query():
    if len(sys.argv) < 2:
        sys.exit('Usage: python query.py "your question here"')
    return ' '.join(sys.argv[1:])


def search(query_text):
    oa = openai.OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    vec = oa.embeddings.create(model='text-embedding-3-small', input=query_text).data[0].embedding
    vec_str = '[' + ','.join(str(v) for v in vec) + ']'

    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        WITH vector_cands AS (
            SELECT uri, headline, slug, fragment, body_text, byline, first_created, reuters_url,
                   1 - (embedding <=> %s::vector) AS vec_score
            FROM ea_reuters_stories
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 200
        ),
        fts_cands AS (
            SELECT uri, headline, slug, fragment, body_text, byline, first_created, reuters_url,
                   1 - (embedding <=> %s::vector) AS vec_score
            FROM ea_reuters_stories
            WHERE embedding IS NOT NULL
              AND to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(body_text,''))
                  @@ plainto_tsquery('english', %s)
            LIMIT 50
        ),
        candidates AS (SELECT * FROM vector_cands UNION SELECT * FROM fts_cands),
        scored AS (
            SELECT *, ts_rank_cd(
                to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(body_text,'')),
                plainto_tsquery('english', %s)
            ) AS fts_score FROM candidates
        ),
        final AS (
            SELECT *, (0.7 * vec_score + 0.3 * fts_score) AS score,
                   regexp_replace(regexp_replace(upper(slug), '\\s*\\([^)]*\\)', '', 'g'), '/', '-', 'g') AS norm_slug
            FROM scored
        ),
        deduped AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY first_created::date, norm_slug ORDER BY score DESC, length(body_text) DESC
            ) AS rn FROM final
        )
        SELECT * FROM deduped WHERE rn = 1 ORDER BY score DESC LIMIT %s
    """, [vec_str, vec_str, vec_str, query_text, query_text, TOP_K])

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def synthesize(query_text, rows):
    sources = []
    for r in rows:
        sources.append({
            'date': r['first_created'].date().isoformat(),
            'slug': r['slug'],
            'title': r['headline'],
            'byline': r['byline'],
            'url': r['reuters_url'],
            'text': (r['body_text'] or r['fragment'] or '')[:2500],
        })

    blocks = []
    for i, s in enumerate(sources, 1):
        tag = f"REUTERS | {s.get('slug') or ''} | {s['date']}"
        byline = f"\nByline: {s['byline']}" if s.get('byline') else ''
        blocks.append(f"SOURCE {i} [{tag}]\nHeadline: {s['title']}{byline}\nURL: {s['url']}\nText: {s['text']}\n")
    sources_block = '\n---\n'.join(blocks)

    prompt = f"""You are a Reuters research assistant. A journalist has asked: "{query_text}"

All sources below are Reuters wire stories about US elections. Each is tagged with slug and date. **Sources are listed in reverse-chronological order -- SOURCE 1 is the most recent.** Use ONLY these sources.

ANSWER GUIDANCE:
- For "what is the current status of X?" or "where do things stand on X?" questions:
  Open with ONE present-tense sentence summarizing where things stand right now. Then give the most recent concrete development with its date. Then the substantive positions of each side. Then brief background. Use inverted pyramid. Aim for 120-200 words.
- For "how many stories have we run on X?" questions: count and list them plainly rather than synthesizing prose.
- Always include the date inline when describing a specific event or development.
- Reuters wire style: active voice, short sentences, no editorializing. Aim for 150-350 words.
- If the sources don't contain enough information to answer, say so plainly.

CITATIONS:
- After each factual claim, add a bracketed reference like [1] or [2] corresponding to the source number below.
- Multiple citations per sentence are fine: [1][3].
- Do not fabricate quotes or facts not present in the sources.

SOURCES:
{sources_block}

Answer now. No preamble."""

    ac = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    response = ac.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1500,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return response.content[0].text, sources


def main():
    query_text = get_query()
    print(f'Searching for: "{query_text}"\n')

    rows = search(query_text)
    print(f"Found {len(rows)} sources")
    for r in rows:
        print(f"  {r['first_created'].date()} {float(r['score']):.3f} {r['headline'][:80]}")

    if not rows:
        print("\nNo matching stories in the archive.")
        return

    answer, sources = synthesize(query_text, rows)

    print("\n=== ANSWER ===\n")
    print(answer)
    print("\n=== SOURCES ===")
    for i, s in enumerate(sources, 1):
        print(f"[{i}] {s['date']} -- {s['title']}")
        print(f"    {s['url']}")


if __name__ == '__main__':
    main()
