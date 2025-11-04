import aiosqlite
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime, timedelta
import os

DB_PATH = Path(os.getenv('AUTH_DB', 'data/authorized.db'))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DB_STR = str(DB_PATH)

CREATE_USERS_SQL = '''
CREATE TABLE IF NOT EXISTS authorized_users (
  telegram_id INTEGER NOT NULL,
  roblox_id   INTEGER NOT NULL,
  username    TEXT,
  created_at  TEXT,
  linked_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (telegram_id, roblox_id)
);
'''

CREATE_METRICS_SQL = '''
CREATE TABLE IF NOT EXISTS metrics_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event       TEXT NOT NULL,
  telegram_id INTEGER,
  roblox_id   INTEGER,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
'''

CREATE_SNAPSHOTS_SQL = '''
CREATE TABLE IF NOT EXISTS account_snapshots (
  roblox_id     INTEGER PRIMARY KEY,
  inventory_val INTEGER DEFAULT 0,
  total_spent   INTEGER DEFAULT 0,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
'''

CREATE_COOKIES_SQL = '''
CREATE TABLE IF NOT EXISTS user_cookies (
  telegram_id       INTEGER NOT NULL,
  roblox_id         INTEGER NOT NULL,
  enc_roblosecurity TEXT NOT NULL,
  saved_at          TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (telegram_id, roblox_id)
);
'''

CREATE_CACHE_SQL = '''
CREATE TABLE IF NOT EXISTS user_cache (
    roblox_id INTEGER NOT NULL,
    cache_key TEXT NOT NULL,
    cache_data TEXT NOT NULL,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    PRIMARY KEY (roblox_id, cache_key)
);
'''

async def init_db():
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_METRICS_SQL)
        await db.execute(CREATE_SNAPSHOTS_SQL)
        await db.execute(CREATE_COOKIES_SQL)
        await db.execute(CREATE_CACHE_SQL)
        await db.commit()

async def upsert_user(telegram_id: int, roblox_id: int, username: str, created_at: Optional[str]) -> None:
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            '''
            INSERT OR REPLACE INTO authorized_users (telegram_id, roblox_id, username, created_at)
            VALUES (?, ?, ?, ?)
            ''',
            (telegram_id, roblox_id, username, created_at)
        )
        await db.commit()

async def get_user(telegram_id: int, roblox_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            'SELECT username, created_at, linked_at FROM authorized_users WHERE telegram_id=? AND roblox_id=?',
            (telegram_id, roblox_id)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {'username': row[0], 'created_at': row[1], 'linked_at': row[2]}

async def list_users(telegram_id: int) -> List[Tuple[int, str]]:
    """Вернёт список (roblox_id, username) для клавиатуры."""
    try:
        async with aiosqlite.connect(DB_STR) as db:
            cur = await db.execute(
                "SELECT roblox_id, COALESCE(username, '') FROM authorized_users WHERE telegram_id=? ORDER BY linked_at DESC",
                (telegram_id,)
            )
            rows = await cur.fetchall()
            result = [(int(r[0]), r[1]) for r in rows]
            return result
    except Exception as e:
        print(f'❌ Ошибка в list_users: {e}')
        return []

async def save_encrypted_cookie(telegram_id: int, roblox_id: int, enc_cookie: str) -> None:
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            '''
            INSERT OR REPLACE INTO user_cookies (telegram_id, roblox_id, enc_roblosecurity)
            VALUES (?, ?, ?)
            ''',
            (telegram_id, roblox_id, enc_cookie)
        )
        await db.commit()

async def get_encrypted_cookie(telegram_id: int, roblox_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            'SELECT enc_roblosecurity FROM user_cookies WHERE telegram_id=? AND roblox_id=?',
            (telegram_id, roblox_id)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def delete_cookie(telegram_id: int, roblox_id: int) -> None:
    """Удаляет и куки и запись об аккаунте"""
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute('DELETE FROM user_cookies WHERE telegram_id=? AND roblox_id=?', (telegram_id, roblox_id))
        await db.execute('DELETE FROM authorized_users WHERE telegram_id=? AND roblox_id=?', (telegram_id, roblox_id))
        await db.commit()

async def get_cached_data(roblox_id: int, key: str) -> Optional[Any]:
    """Получить данные из кэша"""
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            "SELECT cache_data FROM user_cache WHERE roblox_id=? AND cache_key=? AND expires_at > datetime('now')",
            (roblox_id, key)
        )
        row = await cur.fetchone()
        if row:
            return json.loads(row[0])
        return None

async def set_cached_data(roblox_id: int, key: str, data: Any, ttl_minutes: int = 5) -> None:
    """Сохранить данные в кэш"""
    expires_at = datetime.now() + timedelta(minutes=ttl_minutes)
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            '''
            INSERT OR REPLACE INTO user_cache (roblox_id, cache_key, cache_data, expires_at)
            VALUES (?, ?, ?, ?)
            ''',
            (roblox_id, key, json.dumps(data), expires_at.isoformat())
        )
        await db.commit()

async def clear_user_cache(roblox_id: int) -> None:
    """Очистить кэш пользователя (при изменении данных)"""
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute('DELETE FROM user_cache WHERE roblox_id=?', (roblox_id,))
        await db.commit()

async def log_event(event: str, telegram_id: int | None, roblox_id: int | None) -> None:
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            'INSERT INTO metrics_events(event, telegram_id, roblox_id) VALUES (?, ?, ?)',
            (event, telegram_id, roblox_id)
        )
        await db.commit()

async def admin_stats() -> dict:
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT telegram_id FROM authorized_users)')
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM authorized_users WHERE date(linked_at) = date('now','localtime')")
        new_today = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='check'")
        checks_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='check' AND date(created_at) = date('now','localtime')")
        checks_today = (await cur.fetchone())[0]
    return {
        'total_users': total_users,
        'new_today': new_today,
        'checks_total': checks_total,
        'checks_today': checks_today
    }

async def get_any_encrypted_cookie_by_roblox_id(roblox_id: int) -> Optional[str]:
    """Вернёт любую (первую попавшуюся) зашифрованную куку для данного roblox_id, если есть."""
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            'SELECT enc_roblosecurity FROM user_cookies WHERE roblox_id=? LIMIT 1',
            (roblox_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def upsert_account_snapshot(roblox_id: int, inventory_val: int, total_spent: int) -> None:
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            '''
            INSERT INTO account_snapshots(roblox_id, inventory_val, total_spent, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(roblox_id) DO UPDATE SET
                inventory_val=excluded.inventory_val,
                total_spent=excluded.total_spent,
                updated_at=CURRENT_TIMESTAMP
            ''',
            (roblox_id, int(inventory_val), int(total_spent))
        )
        await db.commit()

async def get_account_snapshot(roblox_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            'SELECT inventory_val, total_spent, updated_at FROM account_snapshots WHERE roblox_id=?',
            (roblox_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {'inventory_val': row[0], 'total_spent': row[1], 'updated_at': row[2]}

# =========================
# NEW: глобальные хелперы для перебора кук
# =========================

async def list_all_owners() -> List[int]:
    """Все telegram_id, у которых сохранена хотя бы одна кука."""
    try:
        async with aiosqlite.connect(DB_STR) as db:
            cur = await db.execute('SELECT DISTINCT telegram_id FROM user_cookies')
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]
    except Exception:
        return []

async def list_all_cookies() -> List[Tuple[int, int, str]]:
    """
    Вернёт список всех кук из БД.
    Формат: [(telegram_id, roblox_id, enc_roblosecurity), ...]
    """
    try:
        async with aiosqlite.connect(DB_STR) as db:
            cur = await db.execute('SELECT telegram_id, roblox_id, enc_roblosecurity FROM user_cookies')
            rows = await cur.fetchall()
            return [(int(r[0]), int(r[1]), r[2]) for r in rows]
    except Exception:
        return []
