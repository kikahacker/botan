from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import CFG
import storage
from handlers import router
import asyncio

async def main():
    await storage.init_db()
    bot = Bot(token=CFG.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print('✅ bot started (polling)')
    await dp.start_polling(bot)
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print('🛑 bot stopped')