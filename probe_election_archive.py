#!/usr/bin/env python3
"""
Probe Reuters Connect to confirm slug + topicCode filters for the midterm
archive, now covering three plausible slug families per Andy:
  - USA-ELECTION  (election-specific coverage)
  - USA-CONGRESS  (Congress coverage, likely broader than just elections)
  - USA-TRUMP     (Trump coverage, likely broader than just elections)

v2 sampled the last 14 days, which for a quiet Sunday night undercounted
badly if results are sorted newest-first. This version probes known-busy
primary days instead, and reports match counts per slug family separately
so we can see how much volume each contributes before deciding what to
fold into the archive.

Usage:
    python probe_election_archive.py
"""

import os
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

REUTERS_CLIENT_ID     = os.getenv('REUTERS_CLIENT_ID')
REUTERS_CLIENT_SECRET = os.getenv('REUTERS_CLIENT_SECRET')
AUDIENCE              = "7a14b6a2-73b8-4ab2-a610-80fb9f40f769"
TOKEN_URL             = "https://auth.thomsonreuters.com/oauth/token"
GRAPHQL_URL           = "https://api.reutersconnect.com/content/graphql"

# Slug families to check independently. Each matches the tag as a bounded
# token, e.g. USA-ELECTION/TEXAS or USA-ELECTION-2026, not "USA-ELECTIONS-BOARD".
SLUG_PATTERNS = {
    'USA-ELECTION': re.compile(r'(^|-)USA-ELECTION(-|/|$)', re.IGNORECASE),
    'USA-CONGRESS': re.compile(r'(^|-)USA-CONGRESS(-|/|$)', re.IGNORECASE),
    'USA-TRUMP':    re.compile(r'(^|-)USA-TRUMP(-|/|$)', re.IGNORECASE),
}

# Primary candidate per Andy. Kept as a list so you can add/remove combos
# and re-run without touching the rest of the script.
TOPIC_CODE_CANDIDATES = [
    ["US", "VOTE", "POL"],
    ["US"],
    ["VOTE"],
    ["POL"],
]

# Known-busy windows instead of "the last 14 days" -- probing right now (a
# quiet Sunday night with no election imminent) mostly samples a low-volume
# overnight/weekend slice if results are sorted newest-first, regardless of
# how big the fetch is. These are primary days we already confirmed had
# heavy Reuters coverage, so a correct filter should find matches fast.
DATE_WINDOWS = [
    ("PA-10 primary (Stelson v. Douglas), May 19 2026", "2026-05-19", "2026-05-21"),
    ("Iowa primary (Trone Garriott, James), June 2 2026", "2026-06-01", "2026-06-03"),
]

PAGES_PER_CANDIDATE = 15   # 15 * 100 = 1,500 items per candidate/window
PAGE_SIZE = 100


def get_token():
    resp = requests.post(TOKEN_URL, data={
        'grant_type':    'client_credentials',
        'client_id':     REUTERS_CLIENT_ID,
        'client_secret': REUTERS_CLIENT_SECRET,
        'audience':      AUDIENCE,
        'scope':         'https://api.thomsonreuters.com/auth/reutersconnect.contentapi.read',
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()['access_token']


def graphql(token, query, variables=None):
    resp = requests.post(
        GRAPHQL_URL,
        json={'query': query, 'variables': variables or {}},
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise RuntimeError(data['errors'])
    return data.get('data', {})


def fetch_many(token, topic_codes, date_from, date_to, pages=PAGES_PER_CANDIDATE, page_size=PAGE_SIZE):
    """Paginate through up to `pages` pages of results for a topicCodes filter."""
    fmt = lambda d: d[:10].replace('-', '.')
    date_range_str = f"{fmt(date_from)}-{fmt(date_to)}"

    all_items = []
    cursor = None
    total_hits = None

    for page in range(pages):
        cursor_decl = ', $cursor: String' if cursor else ''
        cursor_arg = ', cursor: $cursor' if cursor else ''
        q = f"""
        query Probe($limit: Int!, $topicCodes: [String!], $dateRange: String!{cursor_decl}) {{
            search(
                filter: {{ dateRange: $dateRange, mediaTypes: [TEXT], topicCodes: $topicCodes }},
                limit: $limit{cursor_arg}
            ) {{
                totalHits
                pageInfo {{ endCursor, hasNextPage }}
                items {{ uri headLine slug firstCreated language subject {{ code }} }}
            }}
        }}
        """
        variables = {'limit': page_size, 'topicCodes': topic_codes, 'dateRange': date_range_str}
        if cursor:
            variables['cursor'] = cursor

        try:
            data = graphql(token, q, variables)
        except Exception as e:
            return total_hits, all_items, str(e)

        search = data.get('search', {})
        if total_hits is None:
            total_hits = search.get('totalHits', 0)
        items = search.get('items', [])
        all_items.extend(items)

        page_info = search.get('pageInfo', {})
        if not page_info.get('hasNextPage') or not items:
            break
        cursor = page_info.get('endCursor')
        if not cursor:
            break
        time.sleep(0.15)

    return total_hits, all_items, None


def main():
    if not (REUTERS_CLIENT_ID and REUTERS_CLIENT_SECRET):
        sys.exit("ERROR: REUTERS_CLIENT_ID / REUTERS_CLIENT_SECRET not set (check .env)")

    print("Authenticating with Reuters Connect...")
    token = get_token()
    print("OK\n")

    for window_label, window_from, window_to in DATE_WINDOWS:
        print(f"##### WINDOW: {window_label} ({window_from} -> {window_to}) #####\n")

        for topic_codes in TOPIC_CODE_CANDIDATES:
            label = f"topicCodes={topic_codes}"
            print(f"[{label}]  sampling up to {PAGES_PER_CANDIDATE * PAGE_SIZE} items")

            total_hits, items, err = fetch_many(token, topic_codes, window_from, window_to)

            if err:
                print(f"  ERROR: {err}\n")
                continue

            en_items = [i for i in items if (i.get('language') or '').lower() == 'en']
            print(f"  totalHits (API): {total_hits} | fetched: {len(items)} | English: {len(en_items)}")

            for name, pattern in SLUG_PATTERNS.items():
                matches = [i for i in en_items if pattern.search(i.get('slug') or '')]
                print(f"  {name}: {len(matches)} matches")
                for i in matches[:5]:
                    print(f"    [{i.get('firstCreated','?')[:10]}] {i.get('headLine','(no headline)')}  |  slug: {i.get('slug','?')}")
            print()
            time.sleep(0.3)
        print()

    print("Done.")
    print("Compare match counts across topicCode candidates and slug families above.")
    print("Whichever topicCodes setting consistently surfaces all three slug families")
    print("goes into refresh.py's TOPIC_CODES. If USA-CONGRESS or USA-TRUMP show heavy")
    print("non-election volume (e.g. routine Trump news unrelated to the midterms),")
    print("decide whether to include them at all or just log them separately.")


if __name__ == '__main__':
    main()
