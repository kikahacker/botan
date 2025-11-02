
import aiosqlite
import os
import logging
from pathlib import Path
from typing import List, Tuple

LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_logger = logging.getLogger("bot.storage")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
    _logger.addHandler(fh)

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

async def _apply_pragmas(db):
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
        except Exception as e:
            _logger.warning(f"PRAGMA failed: {p} ({e})")

async def _has_column(db, table, col):
    async with db.execute(f"PRAGMA table_info({table});") as cur:
        cols = [r[1] async for r in cur]
    return col in cols

async def _ensure_column(db, table, col, coltype):
    if not await _has_column(db, table, col):
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype};")
        except Exception as e:
            _logger.error(f"alter {table}.{col} failed: {e}")

async def _safe_index(db, idx, table, col):
    if await _has_column(db, table, col):
        try:
            await db.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({col});")
        except Exception as e:
            _logger.warning(f"index {idx} failed: {e}")

async def init_db():
    async with aiosqlite.connect(DB_STR, isolation_level=None) as db:
        await _apply_pragmas(db)
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_METRICS_SQL)
        await db.execute(CREATE_SNAPSHOTS_SQL)
        await db.execute(CREATE_COOKIES_SQL)
        await db.execute(CREATE_CACHE_SQL)
        await _ensure_column(db, "metrics_events", "name", "TEXT")
        await _ensure_column(db, "metrics_events", "meta_json", "TEXT")
        await _ensure_column(db, "metrics_events", "created_at", "TEXT")
        await _safe_index(db, "idx_auth_users_tg", "authorized_users", "telegram_id")
        await _safe_index(db, "idx_auth_users_rb", "authorized_users", "roblox_id")
        await _safe_index(db, "idx_metrics_name", "metrics_events", "name")
        await _safe_index(db, "idx_cache_exp", "user_cache", "expires_at")
        await db.commit()

async def _table_exists(db, name):
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)) as cur:
        return await cur.fetchone() is not None

async def list_users(telegram_id: int):
    out = {}
    async with aiosqlite.connect(DB_STR) as db:
        db.row_factory = aiosqlite.Row
        if await _table_exists(db, "users"):
            cur = await db.execute("SELECT roblox_id, COALESCE(username, '') AS username FROM users WHERE telegram_id=?", (telegram_id,))
            for r in await cur.fetchall():
                out[int(r["roblox_id"])] = r["username"] or ""
        if await _table_exists(db, "authorized_users"):
            cur = await db.execute("SELECT roblox_id, COALESCE(username, '') AS username FROM authorized_users WHERE telegram_id=?", (telegram_id,))
            for r in await cur.fetchall():
                rid = int(r["roblox_id"])
                if rid not in out or not out[rid]:
                    out[rid] = r["username"] or ""
    return sorted(out.items(), key=lambda x: ((x[1] == ""), x[1].lower(), x[0]))
