import aiosqlite
import json
import logging
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
  is_active         BOOLEAN DEFAULT TRUE,
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

# НОВАЯ таблица для хранения всех пользователей бота
CREATE_BOT_USERS_SQL = '''
CREATE TABLE IF NOT EXISTS bot_users (
  telegram_id INTEGER PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
  language_code TEXT DEFAULT 'en'
);
'''


async def migrate_add_is_active_column():
    """Добавляет колонку is_active если её нет"""
    try:
        async with aiosqlite.connect(DB_STR) as db:
            # Проверяем существует ли колонка
            cur = await db.execute("PRAGMA table_info(user_cookies)")
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]

            if 'is_active' not in column_names:
                await db.execute('ALTER TABLE user_cookies ADD COLUMN is_active BOOLEAN DEFAULT TRUE')
                await db.commit()
                logging.info("Added is_active column to user_cookies")
    except Exception as e:
        logging.error(f"Migration failed: {e}")


async def init_db():
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_METRICS_SQL)
        await db.execute(CREATE_SNAPSHOTS_SQL)
        await db.execute(CREATE_COOKIES_SQL)
        await db.execute(CREATE_CACHE_SQL)
        await db.execute(CREATE_BOT_USERS_SQL)
        await db.commit()

    # Запускаем миграцию
    await migrate_add_is_active_column()


async def track_bot_user(telegram_id: int, username: str = None, first_name: str = None, last_name: str = None,
                         language_code: str = 'en'):
    """Сохраняет/обновляет информацию о пользователе бота"""
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute('''
            INSERT INTO bot_users (telegram_id, username, first_name, last_name, last_seen, language_code)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen = CURRENT_TIMESTAMP,
                language_code = excluded.language_code
        ''', (telegram_id, username, first_name, last_name, language_code))
        await db.commit()


async def get_all_bot_users() -> List[Tuple[int, str, str]]:
    """Возвращает список всех пользователей бота для рассылки (telegram_id, username, first_name)"""
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute('SELECT telegram_id, username, first_name FROM bot_users ORDER BY first_seen DESC')
        rows = await cur.fetchall()
        return [(row[0], row[1] or '', row[2] or '') for row in rows]


async def get_bot_users_count() -> int:
    """Возвращает общее количество пользователей бота"""
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute('SELECT COUNT(*) FROM bot_users')
        return (await cur.fetchone())[0]


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
        logging.error(f'❌ Ошибка в list_users: {e}')
        return []


async def save_encrypted_cookie(telegram_id: int, roblox_id: int, enc_cookie: str) -> None:
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            '''
            INSERT OR REPLACE INTO user_cookies (telegram_id, roblox_id, enc_roblosecurity, is_active)
            VALUES (?, ?, ?, TRUE)
            ''',
            (telegram_id, roblox_id, enc_cookie)
        )
        await db.commit()


async def get_encrypted_cookie(telegram_id: int, roblox_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_STR) as db:
        cur = await db.execute(
            'SELECT enc_roblosecurity FROM user_cookies WHERE telegram_id=? AND roblox_id=? AND is_active = TRUE',
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


async def deactivate_cookie(telegram_id: int, roblox_id: int) -> None:
    """Деактивирует куки (помечает как неактивную)"""
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute(
            'UPDATE user_cookies SET is_active = FALSE WHERE telegram_id=? AND roblox_id=?',
            (telegram_id, roblox_id)
        )
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
        # Users
        cur = await db.execute('SELECT COUNT(*) FROM bot_users')
        total_users = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM bot_users WHERE date(first_seen)=date('now','localtime')")
        new_today = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM bot_users WHERE date(last_seen)=date('now','localtime')")
        active_today = (await cur.fetchone())[0]

        # Users with linked accounts
        cur = await db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT telegram_id FROM authorized_users)')
        users_with_accounts = (await cur.fetchone())[0]

        # Checks (overall)
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='check'")
        checks_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='check' AND date(created_at)=date('now','localtime')")
        checks_today = (await cur.fetchone())[0]

        # Split by category
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='profile_check'")
        profile_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='profile_check' AND date(created_at)=date('now','localtime')")
        profile_today = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='inventory_check'")
        inventory_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='inventory_check' AND date(created_at)=date('now','localtime')")
        inventory_today = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='favorites_check'")
        favorites_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='favorites_check' AND date(created_at)=date('now','localtime')")
        favorites_today = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='spending_check'")
        spending_total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM metrics_events WHERE event='spending_check' AND date(created_at)=date('now','localtime')")
        spending_today = (await cur.fetchone())[0]

    return {
        'total_users': total_users,
        'new_today': new_today,
        'active_today': active_today,
        'users_with_accounts': users_with_accounts,
        'checks_total': checks_total,
        'checks_today': checks_today,
        'profile_total': profile_total,
        'profile_today': profile_today,
        'inventory_total': inventory_total,
        'inventory_today': inventory_today,
        'favorites_total': favorites_total,
        'favorites_today': favorites_today,
        'spending_total': spending_total,
        'spending_today': spending_today,
    }
