import asyncio, os
import uvloop
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import os
import speed_profiles
speed_profiles.apply()  # активирует профиль из .env

from config import CFG
import storage
from handlers import router

def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1","true","yes","y","on")

async def start_polling():
    await storage.init_db()
    bot = Bot(token=CFG.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    print("✅ bot started (polling)")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)

async def start_webhook():
    await storage.init_db()
    bot = Bot(token=CFG.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    webhook_path = os.getenv("WEBHOOK_PATH", "/webhook")
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    host = os.getenv("WEBAPP_HOST", "0.0.0.0")
    port = int(os.getenv("WEBAPP_PORT", "8080"))
    print(f"✅ bot started (webhook on http://{host}:{port}{webhook_path})")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    # Keep running
    while True:
        await asyncio.sleep(3600)

def main():
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    use_webhook = _bool_env("USE_WEBHOOK", False) or bool(os.getenv("WEBAPP_PORT") or os.getenv("WEBHOOK_PATH"))
    try:
        if use_webhook:
            asyncio.run(start_webhook())
        else:
            asyncio.run(start_polling())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 bot stopped")

if __name__ == "__main__":
    main()