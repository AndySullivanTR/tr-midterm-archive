-- USA Election Archive schema.
-- Run once against the shared Neon DB (same instance as iran-archive / trump-archive / trump-background).
-- Table prefix ea_ (Election Archive) to avoid collision with siblings' tables.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS ea_reuters_stories (
    uri            TEXT PRIMARY KEY,
    headline       TEXT,
    slug           TEXT,
    fragment       TEXT,
    body_text      TEXT,
    byline         TEXT,
    first_created  TIMESTAMPTZ,
    reuters_url    TEXT,
    topic_codes    TEXT[],
    embedding      vector(1536),
    ingested_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ea_stories_embedding_idx
    ON ea_reuters_stories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS ea_stories_date_idx
    ON ea_reuters_stories (first_created DESC);

CREATE INDEX IF NOT EXISTS ea_stories_slug_idx
    ON ea_reuters_stories (slug);

CREATE INDEX IF NOT EXISTS ea_stories_fts_idx
    ON ea_reuters_stories USING gin
    (to_tsvector('english', coalesce(headline, '') || ' ' || coalesce(body_text, '')));
