#!/usr/bin/env python3
"""
Apply schema.sql against the Neon DB using psycopg2, for environments
without the psql CLI installed (e.g. Windows without PostgreSQL client
tools on PATH).

Usage:
    python apply_schema.py
"""

import os
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

schema_path = Path(__file__).parent / 'schema.sql'
sql = schema_path.read_text()

conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
cur.close()
conn.close()

print("Schema applied successfully (ea_reuters_stories table + indexes created or already present).")
