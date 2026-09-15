#!/usr/bin/env python3
"""
Refresh: fetch USA-ELECTION (and election-relevant USA-TRUMP) stories from
Reuters Connect and add them to Neon PostgreSQL. Mirrors iran-archive/refresh.py.

Slugs included:
  - Anything matching (^|-)USA-ELECTION(-|/|$), e.g. USA-ELECTION,
    USA-ELECTION/TEXAS, USA-ELECTION-2026. Always included (no keyword check
    needed -- the slug alone is a reliable election signal).
  - Anything matching (^|-)USA-TRUMP(-|/|$), e.g. USA-TRUMP/FED,
    USA-TRUMP-IRELAND, but ONLY if headline/fragment/slug also contains an
    election-ish keyword (see ELECTION_KEYWORD_RE). Confirmed via manual
    probe (Sept 2026, 30-day window) that topicCodes=["VOTE"] alone reaches
    USA-TRUMP-slugged stories, but most of them are unrelated to the
    midterms -- Fed rate decisions, tariffs, golf -- carrying VOTE only
    because it's a broad POTUS/politics tag. The keyword check keeps that
    noise out of the corpus while still catching genuine Trump-midterm
    coverage (rallies, endorsements, candidate comments, etc.).

TOPIC_CODES confirmed via probe_election_archive.py: ["VOTE"] alone reliably
surfaces USA-ELECTION-slugged stories with far less pagination than US/POL
(a narrow election-specific editorial tag vs. broad country/politics tags).

Usage:
    python refresh.py                                    # auto: fetch since last DB row
    python refresh.py --days 7                           # force last N days
    python refresh.py --from 2026-01-01                  # backfill from start of year
    python refresh.py --from 2026-01-01 --to 2026-09-13   # bounded backfill
"""

import os
import re
import sys
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta

import httpx
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

DATABASE_URL          = os.getenv('DATABASE_URL')
OPENAI_API_KEY        = os.getenv('OPENAI_API_KEY')
REUTERS_CLIENT_ID     = os.getenv('REUTERS_CLIENT_ID')
REUTERS_CLIENT_SECRET = os.getenv('REUTERS_CLIENT_SECRET')

AUDIENCE     = "7a14b6a2-73b8-4ab2-a610-80fb9f40f769"
TOKEN_URL    = "https://auth.thomsonreuters.com/oauth/token"
GRAPHQL_URL  = "https://api.reutersconnect.com/content/graphql"

EMBEDDING_MODEL = "text-embedding-3-small"
LIMIT = 100

# Matches USA-ELECTION, USA-ELECTION/TEXAS, USA-ELECTION-2026, etc.
# Does NOT match "USA-ELECTIONS-BOARD" style slugs (no boundary) -- re-check
# against probe_election_archive.py output if match rate looks off.
USA_ELECTION_SLUG_RE = re.compile(r'(^|-)USA-ELECTION(-|/|$)', re.IGNORECASE)

# Matches USA-TRUMP, USA-TRUMP/FED, USA-TRUMP-IRELAND, etc. Included only
# when ELECTION_KEYWORD_RE also matches -- see module docstring.
USA_TRUMP_SLUG_RE = re.compile(r'(^|-)USA-TRUMP(-|/|$)', re.IGNORECASE)

# Keyword gate for USA-TRUMP stories: checked against headline + fragment +
# slug. Keeps out Trump coverage that's tagged VOTE but isn't actually about
# the midterms (Fed policy, tariffs, golf, foreign affairs, etc.).
ELECTION_KEYWORD_RE = re.compile(
    r'\b(midterms?|primaries|primary|ballots?|candidac(?:y|ies)|candidates?|'
    r'districts?|runoffs?|endors(?:e|ed|ement|ing)|campaigns?|nominees?|'
    r'nominations?)\b',
    re.IGNORECASE,
)

# CONFIRMED via probe_election_archive.py against two known-busy primary
# days (PA-10 May 19, Iowa June 2 2026). VOTE alone matched the same or more
# USA-ELECTION-slugged stories than US/POL did, despite US/POL having
# 15-25x the total hit volume to page through -- VOTE is a narrow,
# election-specific editorial tag, while US/POL are broad country/politics
# tags diluted with mostly-unrelated content. Deliberately not including
# US/POL: they were needed to reach USA-CONGRESS and USA-TRUMP slugs, but
# we're not ingesting those slug families for now (see README).
TOPIC_CODES = ["VOTE"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def is_relevant_story(item):
    """True if a search result should be ingested.

    USA-ELECTION slugs are always relevant. USA-TRUMP slugs are relevant
    only if the headline/fragment/slug also carries an election-ish
    keyword -- see ELECTION_KEYWORD_RE and the module docstring for why.
    """
    slug = item.get('slug') or ''
    if USA_ELECTION_SLUG_RE.search(slug):
        return True
    if USA_TRUMP_SLUG_RE.search(slug):
        text = f"{item.get('headLine', '')} {item.get('fragment', '')} {slug}"
        return bool(ELECTION_KEYWORD_RE.search(text))
    return False


# =============================================================================
# DATABASE
# =============================================================================

def get_db():
    return psycopg2.connect(DATABASE_URL)


def get_latest_date():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT MAX(first_created) FROM ea_reuters_stories")
    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


def story_needs_processing(cur, uri):
    """True if the story is missing entirely, or present but never got an
    embedding (e.g. a prior run's OpenAI call failed) -- either way it needs
    to go through the insert/embed path again."""
    cur.execute("SELECT embedding IS NULL FROM ea_reuters_stories WHERE uri = %s", (uri,))
    row = cur.fetchone()
    if row is None:
        return True
    return row[0]


def insert_story(cur, story, body_text, embedding):
    uri = story.get('uri')
    headline = story.get('headLine', '')
    slug = story.get('slug', '')
    fragment = story.get('fragment', '')
    byline = story.get('byLine', '')
    first_created = story.get('firstCreated') or None
    reuters_url = get_reuters_url(story)
    subjects = story.get('subject') or []
    topic_codes = [s['code'] for s in subjects if s and s.get('code')] or None

    embedding_str = None
    if embedding:
        embedding_str = '[' + ','.join(f'{v:.8f}' for v in embedding) + ']'

    cur.execute(
        """INSERT INTO ea_reuters_stories
               (uri, headline, slug, fragment, body_text, byline,
                first_created, reuters_url, topic_codes, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
           ON CONFLICT (uri) DO UPDATE SET
               embedding = EXCLUDED.embedding,
               body_text = EXCLUDED.body_text,
               topic_codes = EXCLUDED.topic_codes
           WHERE ea_reuters_stories.embedding IS NULL
             AND EXCLUDED.embedding IS NOT NULL""",
        (uri, headline, slug, fragment, body_text, byline,
         first_created, reuters_url, topic_codes, embedding_str)
    )
    return cur.rowcount > 0


def backfill_topic_codes_if_missing(cur, uri, topic_codes):
    if not topic_codes:
        return False
    cur.execute(
        """UPDATE ea_reuters_stories
           SET topic_codes = %s
           WHERE uri = %s AND topic_codes IS NULL""",
        (topic_codes, uri)
    )
    return cur.rowcount > 0


# =============================================================================
# REUTERS API
# =============================================================================

class ReutersAPI:

    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0

    def get_token(self):
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token
        data = {
            'grant_type': 'client_credentials',
            'client_id': REUTERS_CLIENT_ID,
            'client_secret': REUTERS_CLIENT_SECRET,
            'audience': AUDIENCE,
            'scope': 'https://api.thomsonreuters.com/auth/reutersconnect.contentapi.read https://api.thomsonreuters.com/auth/reutersconnect.contentapi.write'
        }
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
        resp.raise_for_status()
        token_data = resp.json()
        self.access_token = token_data['access_token']
        self.token_expires_at = time.time() + token_data.get('expires_in', 3600) - 300
        return self.access_token

    def execute_query(self, query, variables=None):
        token = self.get_token()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        payload = {'query': query}
        if variables:
            payload['variables'] = variables
        resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if 'errors' in result:
            raise Exception(f"GraphQL errors: {result['errors']}")
        return result.get('data', {})

    def _paginate_search(self, filter_parts, filter_vars, extra_var_decls=''):
        all_items = []
        cursor = None
        seen_uris = set()
        topic_var = ', $topicCodes: [String!]' if TOPIC_CODES else ''

        for page in range(1, 1000):
            cursor_param = ', $cursor: String' if cursor else ''
            cursor_arg = ', cursor: $cursor' if cursor else ''
            filter_str = ', '.join(filter_parts)

            graphql = f"""
            query Search($limit: Int{topic_var}{extra_var_decls}{cursor_param}) {{
                search(
                    filter: {{ {filter_str} }},
                    limit: $limit{cursor_arg}
                ) {{
                    totalHits
                    pageInfo {{ endCursor, hasNextPage }}
                    items {{
                        uri, headLine, slug, fragment, byLine,
                        firstCreated, language, usn,
                        subject {{ code }}
                    }}
                }}
            }}
            """
            variables = dict(filter_vars)
            variables['limit'] = LIMIT
            if TOPIC_CODES:
                variables['topicCodes'] = TOPIC_CODES
            if cursor:
                variables['cursor'] = cursor

            result = self.execute_query(graphql, variables)
            search_data = result.get('search', {})
            items = search_data.get('items', [])
            page_info = search_data.get('pageInfo', {})

            if page == 1:
                logger.info(f"Reuters reports {search_data.get('totalHits', '?')} total hits")

            matching = [
                i for i in items
                if is_relevant_story(i)
                and (i.get('language') or '').lower() == 'en'
                and i.get('uri') not in seen_uris
            ]
            for i in matching:
                seen_uris.add(i.get('uri'))
            all_items.extend(matching)

            if not page_info.get('hasNextPage') or not items:
                break
            cursor = page_info.get('endCursor')
            if not cursor:
                break

            time.sleep(0.15)

        return all_items

    def search_recent(self, max_age_hours):
        filter_parts = ['maxAge: $maxAge', 'mediaTypes: [TEXT]']
        if TOPIC_CODES:
            filter_parts.append('topicCodes: $topicCodes')
        return self._paginate_search(
            filter_parts,
            {'maxAge': f'{max_age_hours}h'},
            extra_var_decls=', $maxAge: String',
        )

    def search_date_range(self, date_from, date_to):
        filter_parts = ['dateRange: $dateRange', 'mediaTypes: [TEXT]']
        if TOPIC_CODES:
            filter_parts.append('topicCodes: $topicCodes')
        return self._paginate_search(
            filter_parts,
            {'dateRange': f'{date_from[:10].replace("-",".")}-{date_to[:10].replace("-",".")}'},
            extra_var_decls=', $dateRange: String',
        )

    def fetch_body(self, uri):
        try:
            graphql = f'''
            query GetItem {{
                item(id: "{uri}", option: {{fragmentLength: 400}}) {{
                    bodyXhtml
                }}
            }}
            '''
            result = self.execute_query(graphql)
            item_data = result.get('item', {})
            if item_data and item_data.get('bodyXhtml'):
                text = re.sub(r'<[^>]+>', ' ', item_data['bodyXhtml'])
                return re.sub(r'\s+', ' ', text).strip()
        except Exception as e:
            logger.debug(f"Failed to fetch body for {uri}: {e}")
        return ''


# =============================================================================
# HELPERS
# =============================================================================

def get_reuters_url(story):
    usn = story.get('usn')
    if usn:
        return f"https://www.reuters.com/article/idUS{usn}/"
    uri = story.get('uri', '')
    match = re.search(r'newsml_([A-Z0-9]+)', uri)
    if match:
        return f"https://www.reuters.com/article/idUS{match.group(1)}/"
    return None


def embed_texts(texts):
    if not texts:
        return []
    response = httpx.post(
        'https://api.openai.com/v1/embeddings',
        headers={
            'Authorization': f'Bearer {OPENAI_API_KEY}',
            'Content-Type': 'application/json',
        },
        json={'model': EMBEDDING_MODEL, 'input': texts},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()['data']
    data.sort(key=lambda x: x['index'])
    return [item['embedding'] for item in data]


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=None,
                        help='Force fetch last N days via maxAge (capped at 30 by Reuters Connect)')
    parser.add_argument('--from', dest='date_from', default=None,
                        help='Backfill start date via dateRange filter, e.g. 2026-01-01')
    parser.add_argument('--to', dest='date_to', default=None,
                        help='Backfill end date via dateRange filter (default: today)')
    args = parser.parse_args()

    if not all([DATABASE_URL, OPENAI_API_KEY, REUTERS_CLIENT_ID, REUTERS_CLIENT_SECRET]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    api = ReutersAPI()

    if args.date_from:
        date_from = args.date_from if 'T' in args.date_from else args.date_from + 'T00:00:00Z'
        date_to_raw = args.date_to or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        date_to = date_to_raw if 'T' in date_to_raw else date_to_raw + 'T23:59:59Z'
        logger.info(f"Archive backfill mode: dateRange {date_from} -> {date_to}")
        logger.info("Searching Reuters Connect...")
        items = api.search_date_range(date_from, date_to)
    elif args.days:
        max_age_hours = args.days * 24
        logger.info(f"Forced maxAge fetch: last {args.days} days ({max_age_hours}h)")
        logger.info("Searching Reuters Connect...")
        items = api.search_recent(max_age_hours)
    else:
        now = datetime.now(timezone.utc)
        latest = get_latest_date()
        if latest:
            if isinstance(latest, str):
                latest_dt = datetime.fromisoformat(latest.replace('Z', '+00:00'))
            else:
                latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
            date_from = (latest_dt - timedelta(hours=2)).strftime('%Y-%m-%d')
            logger.info(f"Latest story in DB: {latest_dt.isoformat()[:10]}; fetching {date_from} to today")
        else:
            date_from = (now - timedelta(days=7)).strftime('%Y-%m-%d')
            logger.info("No stories in DB, fetching last 7 days")
        date_to = now.strftime('%Y-%m-%d')
        logger.info("Searching Reuters Connect...")
        items = api.search_date_range(date_from, date_to)

    logger.info(f"Found {len(items)} candidate stories (USA-ELECTION, plus election-relevant USA-TRUMP)")

    if not items:
        logger.info("Nothing to do.")
        return

    conn = get_db()
    conn.autocommit = False
    cur = conn.cursor()

    new_stories = []
    skipped = 0
    backfilled = 0
    for item in items:
        uri = item.get('uri')
        if story_needs_processing(cur, uri):
            new_stories.append(item)
            continue
        subjects = item.get('subject') or []
        codes = [s['code'] for s in subjects if s and s.get('code')]
        if backfill_topic_codes_if_missing(cur, uri, codes):
            backfilled += 1
        skipped += 1

    if backfilled:
        conn.commit()
    logger.info(f"New/needing-embedding stories: {len(new_stories)} | Already complete in DB: {skipped} (topic_codes backfilled: {backfilled})")

    if not new_stories:
        cur.close()
        conn.close()
        return

    BATCH_SIZE = 20
    inserted = 0
    embedding_failures = 0

    for i in range(0, len(new_stories), BATCH_SIZE):
        batch = new_stories[i:i + BATCH_SIZE]

        bodies = []
        for story in batch:
            body = api.fetch_body(story['uri'])
            bodies.append(body)
            time.sleep(0.2)

        texts = [
            f"{s.get('headLine', '')} {bodies[j] or s.get('fragment', '')}".strip()
            for j, s in enumerate(batch)
        ]
        try:
            embeddings = embed_texts(texts)
        except Exception as e:
            logger.error(f"Embedding batch failed: {e}")
            embeddings = [None] * len(batch)
            embedding_failures += len(batch)

        for story, body, embedding in zip(batch, bodies, embeddings):
            try:
                if insert_story(cur, story, body, embedding):
                    inserted += 1
            except Exception as e:
                logger.error(f"Failed to insert {story.get('uri')}: {e}")
                conn.rollback()
                continue

        conn.commit()
        logger.info(f"  Batch {i // BATCH_SIZE + 1}: {inserted} inserted so far")
        time.sleep(0.5)

    cur.close()
    conn.close()

    logger.info(f"Refresh complete. Inserted: {inserted} | Skipped: {skipped}")

    if embedding_failures:
        logger.error(
            f"{embedding_failures} stor{'y' if embedding_failures == 1 else 'ies'} "
            "inserted without an embedding (OpenAI call failed) -- will be "
            "retried on the next refresh, but flagging now so the run shows "
            "as failed."
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
