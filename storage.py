import aiosqlite
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime, timedelta
import os

DB_PATH = Path(os.getenv('AUTH_DB', 'data/authorized.db'))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_STR = str(DB_PATH)

# Create/ensure schema (tables are expected to already exist in the project)
CREATE_USERS_SQL = '''
CREATE TABLE IF NOT EXISTS authorized_users (
  telegram_id INTEGER,
  roblox_id   INTEGER,
  username    TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (telegram_id, roblox_id)
);
'''
CREATE_METRICS_SQL = '''
CREATE TABLE IF NOT EXISTS metrics_events (
  name        TEXT,
  telegram_id INTEGER,
  roblox_id   INTEGER,
  meta_json   TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
'''
CREATE_SNAPSHOTS_SQL = '''
CREATE TABLE IF NOT EXISTS account_snapshots (
  roblox_id     INTEGER PRIMARY KEY,
  snapshot_json TEXT,
  version       INTEGER DEFAULT 0,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
'''
CREATE_COOKIES_SQL = '''
CREATE TABLE IF NOT EXISTS user_cookies (
  telegram_id INTEGER,
  roblox_id   INTEGER,
  cookie_enc  TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (telegram_id, roblox_id)
);
'''
CREATE_CACHE_SQL = '''
CREATE TABLE IF NOT EXISTS user_cache (
  roblox_id  INTEGER,
  cache_key  TEXT,
  payload    BLOB,
  expires_at TIMESTAMP,
  PRIMARY KEY (roblox_id, cache_key)
);
'''

async def _apply_pragmas(db: aiosqlite.Connection):
    # WAL for concurrent readers, fewer fsyncs; NORMAL for good durability/speed
    pragmas = [
        "PRAGMA journal_mode=WAL;",
        "PRAGMA synchronous=NORMAL;",
        "PRAGMA temp_store=MEMORY;",
        "PRAGMA mmap_size=268435456;",
        "PRAGMA cache_size=-200000;",
        "PRAGMA busy_timeout=5000;",
        "PRAGMA journal_size_limit=67108864;"
    ]
    for p in pragmas:
        try:
            await db.execute(p)
        except Exception:
            pass

async def init_db():
    async with aiosqlite.connect(DB_STR, isolation_level=None, timeout=5.0) as db:
        await _apply_pragmas(db)
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_METRICS_SQL)
        await db.execute(CREATE_SNAPSHOTS_SQL)
        await db.execute(CREATE_COOKIES_SQL)
        await db.execute(CREATE_CACHE_SQL)
        # Helpful indexes (no-ops if exist)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_users_tg ON authorized_users(telegram_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_auth_users_rb ON authorized_users(roblox_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics_events(name);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_exp ON user_cache(expires_at);")
        await db.commit()

# NOTE: keep the rest of the project's original CRUD functions as-is.
# This file only needs to be imported before first DB usage to ensure pragmas+indexes exist.