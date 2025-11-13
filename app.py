from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import CFG
import storage
from handlers import router
from handlers_extra_sections import router as extra_sections
import asyncio
import logging
from logging.handlers import RotatingFileHandler

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
    bot = Bot(token=CFG.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(extra_sections)
    print('✅ bot started (polling)')
    await dp.start_polling(bot)
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print('🛑 bot stopped')