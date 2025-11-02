import aiosqlite
import os
from pathlib import Path

DB_PATH = Path(os.getenv('AUTH_DB', 'data/authorized.db'))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_STR = str(DB_PATH)

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

async def _has_column(db: aiosqlite.Connection, table: str, col: str) -> bool:
    q = f"PRAGMA table_info({table});"
    async with db.execute(q) as cur:
        cols = [r[1] async for r in cur]  # r[1] is name
    return col in cols

async def _ensure_column(db: aiosqlite.Connection, table: str, col: str, coltype: str):
    if not await _has_column(db, table, col):
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype};")
        except Exception:
            pass

async def _safe_index(db: aiosqlite.Connection, idx_name: str, table: str, col: str):
    # Create index only if column exists
    if await _has_column(db, table, col):
        try:
            await db.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({col});")
        except Exception:
            pass

async def init_db():
    async with aiosqlite.connect(DB_STR, isolation_level=None, timeout=5.0) as db:
        await _apply_pragmas(db)
        # Ensure base tables exist
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_METRICS_SQL)
        await db.execute(CREATE_SNAPSHOTS_SQL)
        await db.execute(CREATE_COOKIES_SQL)
        await db.execute(CREATE_CACHE_SQL)

        # Backward-compatible migrations for existing installs
        # metrics_events may exist without 'name' in very old schema
        await _ensure_column(db, "metrics_events", "name", "TEXT")
        await _ensure_column(db, "metrics_events", "meta_json", "TEXT")
        await _ensure_column(db, "metrics_events", "created_at", "TEXT")

        # Indexes (only if columns exist)
        await _safe_index(db, "idx_auth_users_tg", "authorized_users", "telegram_id")
        await _safe_index(db, "idx_auth_users_rb", "authorized_users", "roblox_id")
        await _safe_index(db, "idx_metrics_name", "metrics_events", "name")
        await _safe_index(db, "idx_cache_exp", "user_cache", "expires_at")

        await db.commit()