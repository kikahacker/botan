import asyncio
import aiosqlite
from storage import DB_STR

async def fix_database():
    async with aiosqlite.connect(DB_STR) as db:
        await db.execute('DROP TABLE IF EXISTS user_cookies')
        await db.execute('DROP TABLE IF EXISTS authorized_users')
        await db.execute('\n        CREATE TABLE IF NOT EXISTS authorized_users (\n          telegram_id INTEGER NOT NULL,\n          roblox_id   INTEGER NOT NULL,\n          username    TEXT,\n          created_at  TEXT,\n          linked_at   TEXT DEFAULT CURRENT_TIMESTAMP,\n          PRIMARY KEY (telegram_id, roblox_id)\n        )\n        ')
        await db.execute('\n        CREATE TABLE IF NOT EXISTS user_cookies (\n          telegram_id      INTEGER NOT NULL,\n          roblox_id        INTEGER NOT NULL,\n          enc_roblosecurity TEXT NOT NULL,\n          saved_at         TEXT DEFAULT CURRENT_TIMESTAMP,\n          PRIMARY KEY (telegram_id, roblox_id),\n          FOREIGN KEY (telegram_id, roblox_id) REFERENCES authorized_users(telegram_id, roblox_id)\n        )\n        ')
        await db.commit()
        print('✅ База данных исправлена!')
if __name__ == '__main__':
    asyncio.run(fix_database())