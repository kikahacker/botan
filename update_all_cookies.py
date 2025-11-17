# refresh_all_cookies.py
#
# Скрипт, который:
#  - забирает все активные куки из БД
#  - проверяет валидность
#  - невалидные УДАЛЯЕТ из user_cookies + authorized_users
#  - валидные пытается обновить и сохраняет в ту же строку

import asyncio
import logging
from typing import Tuple, List

from util.crypto import decrypt_text, encrypt_text  # у тебя уже есть decrypt_text, encrypt_text — аналогично
from storage import (
    init_db,
    get_all_cookies_with_ids,
    save_encrypted_cookie,
    delete_cookie,
)
from update_cookie import RobloxCookieRefresher


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
)


async def refresh_all_cookies() -> Tuple[int, int, int]:
    """
    Основная логика:
      - перебираем все активные куки
      - удаляем невалидные
      - обновляем валидные
    Возвращает: (total, updated, deleted)
    """
    await init_db()  # на всякий — чтобы таблицы были

    rows: List[tuple[int, int, str]] = await get_all_cookies_with_ids()
    logger.info(f"🌐 Нашёл {len(rows)} активных куков в user_cookies")

    refresher = RobloxCookieRefresher()

    total = len(rows)
    updated = 0
    deleted = 0

    for telegram_id, roblox_id, enc_cookie in rows:
        tag = f"tg={telegram_id}, rid={roblox_id}"

        # 1) Расшифровка
        try:
            cookie_plain = decrypt_text(enc_cookie)
        except Exception as e:
            logger.error(f"[{tag}] не смог расшифровать куку: {e}")
            # если даже расшифровать не можем — такая запись нам вообще не нужна
            await delete_cookie(telegram_id, roblox_id)
            deleted += 1
            continue

        # 2) Проверяем валидность
        try:
            is_valid, user_data = refresher.check_cookie_validity(cookie_plain)
        except Exception as e:
            logger.error(f"[{tag}] ошибка при check_cookie_validity: {e}")
            # на всякий случай просто удаляем, чтобы не висело мёртвым
            await delete_cookie(telegram_id, roblox_id)
            deleted += 1
            continue

        if not is_valid:
            logger.info(f"[{tag}] ❌ кука невалидна — удаляю запись из БД")
            await delete_cookie(telegram_id, roblox_id)
            deleted += 1
            continue

        logger.info(
            f"[{tag}] ✅ кука валидна, юзер: {user_data.get('name')} ({user_data.get('id')}) — обновляю…"
            if user_data else f"[{tag}] ✅ кука валидна — обновляю…"
        )

        # 3) Пытаемся обновить
        try:
            new_cookie = refresher.comprehensive_refresh(cookie_plain)
        except Exception as e:
            logger.error(f"[{tag}] ошибка в comprehensive_refresh: {e}")
            # не получилось обновить — оставим старую валидную
            continue

        if not new_cookie:
            logger.warning(f"[{tag}] не удалось получить новый куки, оставляю старый")
            continue

        # 4) Шифруем и сохраняем НОВУЮ куку в ту же строку
        try:
            new_enc = encrypt_text(new_cookie)
        except Exception as e:
            logger.error(f"[{tag}] не смог зашифровать новый куки: {e}")
            # если новый не шифруется — лучше оставить старую рабочую, вообще ничего не трогаем
            continue

        try:
            await save_encrypted_cookie(telegram_id, roblox_id, new_enc)
            updated += 1
            logger.info(f"[{tag}] 🔁 кука успешно обновлена в БД (INSERT OR REPLACE по тому же ключу)")
        except Exception as e:
            logger.error(f"[{tag}] ошибка при сохранении обновлённой куки: {e}")
            # опять же — старую запись не трогаем

    logger.info(
        f"🏁 Рефреш кук завершён. Всего: {total}, обновлено: {updated}, удалено: {deleted}"
    )
    return total, updated, deleted


# ===== Планировщик для ежедневного запуска =====

from datetime import datetime, timedelta


async def schedule_daily_cookie_refresh(hour: int = 3, minute: int = 0) -> None:
    """
    Бесконечный цикл, который раз в сутки в заданное время гоняет refresh_all_cookies().
    hour/minute — по локальному времени сервера.
    """
    logger.info(f"⏰ Планировщик кук запущен: каждый день в {hour:02d}:{minute:02d}")

    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        sleep_for = (target - now).total_seconds()
        logger.info(f"Следующая проверка кук через ~{int(sleep_for)} сек, в {target}")

        await asyncio.sleep(sleep_for)

        try:
            logger.info("🚀 Запускаю ежедневный рефреш кук…")
            await refresh_all_cookies()
        except Exception:
            logger.exception("🔥 Ошибка при ежедневном рефреше кук")


if __name__ == "__main__":
    # Можно просто запускать этот файл отдельно:
    #   python refresh_all_cookies.py
    asyncio.run(refresh_all_cookies())
