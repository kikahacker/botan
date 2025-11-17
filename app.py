from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage  # 👈 добавляем
from config import CFG
import storage
from handlers import router
from handlers_extra_sections import router as extra_sections
from login_pass import router as logpass
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from update_all_cookies import schedule_daily_cookie_refresh
LOG_PATH = "bot_sections.log"


def _setup_logging():
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def main():
    _setup_logging()

    await storage.init_db()
    bot = Bot(
        token=CFG.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # 👇 ВАЖНО: даём FSM-хранилище
    dp = Dispatcher(storage=MemoryStorage())

    # порядок: основной, доп. секции, логин/пароль
    dp.include_router(router)
    dp.include_router(extra_sections)
    dp.include_router(logpass)
    asyncio.create_task(schedule_daily_cookie_refresh(hour=3, minute=30))
    print('✅ bot started (polling)')
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print('🛑 bot stopped')
