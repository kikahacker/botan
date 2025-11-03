import os
import html
import pathlib
import zipfile
import logging
import inspect
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from unittest.mock import call
from PIL import Image
import httpx
from aiogram import Router, types, F
from i18n import t, tr, get_user_lang, set_user_lang, set_current_lang
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto

ADMINS = set((int(x) for x in os.getenv('ADMINS', '').replace(',', ' ').split() if x))

# Simple in-memory profile cache (per-process)
_PROFILE_CACHE = {}  # {(tg_id, acc_id): (expires_ts, data)}
_PROFILE_TTL = 6 * 60 * 60  # 6 hours


def get_profile_mem(tg_id, acc_id):
    import time
    key = (tg_id, acc_id)
    row = _PROFILE_CACHE.get(key)
    if not row:
        return None
    exp, data = row
    if exp < time.time():
        _PROFILE_CACHE.pop(key, None)
        return None
    return data


def set_profile_mem(tg_id, acc_id, data):
    import time
    _PROFILE_CACHE[(tg_id, acc_id)] = (time.time() + _PROFILE_TTL, data)


def invalidate_profile_mem(tg_id, acc_id):
    _PROFILE_CACHE.pop((tg_id, acc_id), None)


def is_admin(uid: int) -> bool:
    return uid in ADMINS


from util.crypto import decrypt_text, encrypt_text
import storage
from config import CFG
from assets_manager import assets_manager
import roblox_client
from roblox_imagegen import generate_category_sheets, generate_full_inventory_grid
from cache_locks import get_lock

router = Router()

# === Strong profile cache (text + photo_id), key = (tg, rid, lang) ===
_PROFILE_TTL_NEW = 10 * 60  # 10 минут
_PROFILE_MEM_NEW: dict = {}  # {(tg, rid, lang): (exp_ts, {"text": str, "photo_id": Optional[str]})}


def _profile_key(tg_id: int, rid: int, lang: str) -> tuple:
    return (int(tg_id), int(rid), str(lang))


def _profile_mem_get2(tg_id: int, rid: int, lang: str):
    import time
    rec = _PROFILE_MEM_NEW.get(_profile_key(tg_id, rid, lang))
    if not rec: return None
    exp, data = rec
    if exp < time.time():
        _PROFILE_MEM_NEW.pop(_profile_key(tg_id, rid, lang), None)
        return None
    return data


def _profile_mem_set2(tg_id: int, rid: int, lang: str, *, text: str, photo_id: str | None):
    import time
    _PROFILE_MEM_NEW[_profile_key(tg_id, rid, lang)] = (
        time.time() + _PROFILE_TTL_NEW, {"text": text, "photo_id": photo_id}
    )


async def _profile_store_get2(storage, tg_id: int, rid: int, lang: str):
    k_text = f"profile_text:{lang}:{rid}:{tg_id}"
    k_photo = f"profile_photo_id:{rid}"
    try:
        text = await storage.get_cached_data(rid, k_text)
    except Exception:
        text = None
    try:
        photo_id = await storage.get_cached_data(rid, k_photo)
    except Exception:
        photo_id = None
    if isinstance(text, str):
        return {"text": text, "photo_id": photo_id}
    return None


async def _profile_store_set2(storage, tg_id: int, rid: int, lang: str, *, text: str, photo_id: str | None):
    try:
        await storage.set_cached_data(rid, f"profile_text:{lang}:{rid}:{tg_id}", text, 10 * 60)
    except Exception:
        pass
    if photo_id:
        try:
            await storage.set_cached_data(rid, f"profile_photo_id:{rid}", photo_id, 24 * 60)
        except Exception:
            pass


logger = logging.getLogger(__name__)
os.makedirs('temp', exist_ok=True)
from contextvars import ContextVar

_CURRENT_LANG = ContextVar('_CURRENT_LANG', default='en')
from aiogram.dispatcher.middlewares.base import BaseMiddleware


class LangMiddleware(BaseMiddleware):

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None) or getattr(getattr(event, 'message', None), 'from_user', None)
        if user:
            try:
                lang = await get_user_lang(storage, user.id, fallback='en')
            except Exception:
                lang = 'en'
            _CURRENT_LANG.set(lang)
        return await handler(event, data)


try:
    router.message.middleware(LangMiddleware())
    router.callback_query.middleware(LangMiddleware())
except Exception:
    pass


def L(key: str, **kw) -> str:
    lang = _CURRENT_LANG.get() or 'en'
    try:
        return tr(lang, key, **kw)
    except Exception:
        try:
            return tr('en', key, **kw)
        except Exception:
            pass
        try:
            return key.format(**kw)
        except Exception:
            return key


def LL(*keys, **kw) -> str:
    """Try multiple i18n keys in order, return the first that resolves (value != key)."""
    for k in keys:
        try:
            val = L(k, **kw)
        except Exception:
            val = k
        if val != k:
            return val
    last = keys[-1] if keys else ""
    try:
        return last.format(**kw)
    except Exception:
        return last


def _mask_email(email: str) -> str:
    if not email or email == '—':
        return '—'
    try:
        name, domain = email.split('@', 1)
        if len(name) <= 2:
            m = (name[:1] + '…') if name else '…'
        else:
            m = name[0] + '*' * (len(name) - 2) + name[-1]
        return f"{m}@{domain}"
    except Exception:
        return email


def render_profile_text_i18n(*, uname, dname, roblox_id, created, country, gender_raw, birthdate, age, email,
                             email_verified, robux, spent_val, banned) -> str:
    # Map raw gender text like "👨 Мужской" / "👩 Женский" to common keys
    gkey = 'unknown'
    gr = (gender_raw or '').lower()
    if 'male' in gr or 'муж' in gr:
        gkey = 'male'
    elif 'female' in gr or 'жен' in gr:
        gkey = 'female'
    skey = 'banned' if banned else 'active'
    text = L('profile.card',
             uname=uname or '—',
             display_name=dname or '—',
             rid=roblox_id,
             created=created,
             country=country or '—',
             gender=L(f'common.{gkey}'),
             birthday=birthdate or '—',
             age=age if age not in (None, '') else '—',
             email=_mask_email(email),
             email_verified=(L('common.yes') if email_verified else L('common.no')),
             robux=f"{robux} R$",
             spent=(f"{spent_val} R$" if isinstance(spent_val, (int, float)) and spent_val >= 0 else '—'),
             status=L(f'common.{skey}'))
    return text


async def use_lang_from_message(message) -> str:
    try:
        lang = await get_user_lang(storage, message.from_user.id, fallback='en')
    except Exception:
        lang = 'en'
    _CURRENT_LANG.set(lang)
    return lang


async def use_lang_from_call(call) -> str:
    try:
        lang = await get_user_lang(storage, call.from_user.id, fallback='en')
    except Exception:
        lang = 'en'
    _CURRENT_LANG.set(lang)
    return lang


import re as _re_mod

_BANNED_CATEGORIES = {'Meshes', 'Places', 'Models', 'Decals', 'Badges', 'Plugins'}
_ACCESSORY_FAMILY = {'Face Accessory', 'Neck Accessory', 'Shoulder Accessory', 'Front Accessory', 'Back Accessory',
                     'Waist Accessory', 'Shirt Accessories', 'Pants Accessories', 'Gear Accessories'}
_CLASSIC_FAMILY = {'Classic T-shirts', 'Classic Shirts', 'Classic Pants'}


def _canon_cat(name: str) -> str:
    n = (name or '').strip()
    if not n:
        return ''
    if n in _BANNED_CATEGORIES:
        return ''
    if n in _CLASSIC_FAMILY:
        return 'Classic Clothes'
    if n in _ACCESSORY_FAMILY or _re_mod.search('\\bAccessories?\\b', n, flags=_re_mod.IGNORECASE):
        return 'Accessories'
    n = _re_mod.sub('\\s+', ' ', n)
    return ' '.join((w.capitalize() if len(w) > 2 else w for w in n.split()))


def _merge_categories(by_cat: dict) -> dict:
    merged = {}
    for raw, items in (by_cat or {}).items():
        key = _canon_cat(raw)
        if not key:
            continue
        merged.setdefault(key, []).extend(items or [])
    return merged


def _all_categories() -> list[str]:
    try:
        raw = set(roblox_client.ASSET_TYPE_TO_CATEGORY.values())
        return sorted({_canon_cat(x) for x in raw if _canon_cat(x)})
    except Exception:
        return []


def _category_slug(name: str) -> str:
    return (name or '').lower().replace(' ', '_')


def cat_label(cat_raw: str) -> str:
    """
    Return localized label for category based on slug; fallback to original.
    """
    try:
        slug = _category_slug(cat_raw)
    except Exception:
        slug = str(cat_raw).lower().replace(' ', '_')
    return L(f'cat.{slug}') or cat_raw
    return (name or '').lower().replace(' ', '_')


def _unslug(slug: str) -> str:
    return (slug or '').replace('_', ' ').title()


async def _get_selected_cats(tg_id: int, roblox_id: int) -> set[str]:
    key = f'inv_sel:{roblox_id}'
    sel = await storage.get_cached_data(tg_id, key)
    if not isinstance(sel, list):
        sel = []
    return set((str(x) for x in sel))


async def _set_selected_cats(tg_id: int, roblox_id: int, selected: set[str]):
    key = f'inv_sel:{roblox_id}'
    await storage.set_cached_data(tg_id, key, list(selected), 60 * 30)


def _build_cat_kb(selected: set[str], roblox_id: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for cat in _all_categories():
        slug = _category_slug(cat)
        on = slug in selected
        txt = f"{('✅' if on else '🚫')} {cat_label(cat)}"
        row.append(InlineKeyboardButton(text=txt, callback_data=f'inv_cfg_toggle:{roblox_id}:{slug}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text=LL('buttons.all_on', 'btn.all_on'), callback_data=f'inv_cfg_allon:{roblox_id}'),
         InlineKeyboardButton(text=LL('buttons.all_off', 'btn.none'), callback_data=f'inv_cfg_alloff:{roblox_id}')])
    rows.append([InlineKeyboardButton(text=LL('buttons.next', 'btn.next'), callback_data=f'inv_cfg_next:{roblox_id}'),
                 InlineKeyboardButton(text=L('btn.back_to_profile'), callback_data=f'acct:{roblox_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clean_cookie_value(cookie_value: str) -> str:
    warning_patterns = [
        '_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_',
        'WARNING:-DO-NOT-SHARE-THIS.',
        'Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.']
    cleaned = cookie_value.strip()
    for pat in warning_patterns:
        cleaned = cleaned.replace(pat, '')
    return cleaned.strip()


async def validate_and_clean_cookie(cookie_value: str) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """
    Проверяет .ROBLOSECURITY, возвращает (ok, cleaned_cookie, user_data|None)
    """
    try:
        cleaned_cookie = clean_cookie_value(cookie_value)
        if not cleaned_cookie:
            return (False, None, None)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Referer': 'https://www.roblox.com/'}
        cookies = {'.ROBLOSECURITY': cleaned_cookie}
        async with httpx.AsyncClient(timeout=CFG.TIMEOUT) as c:
            r = await c.get('https://users.roblox.com/v1/users/authenticated', headers=headers, cookies=cookies)
            if r.status_code == 200:
                return (True, cleaned_cookie, r.json())
        return (False, None, None)
    except Exception as e:
        logger.error(f'❌ Ошибка при валидации куки: {e}')
        return (False, None, None)


async def edit_or_send(message: types.Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None,
                       photo: Optional[FSInputFile] = None, parse_mode: str = 'HTML'):
    """
    Ставит твой интерфейс «на рельсы»: если сообщение можно отредактировать — редактируем,
    если нет — отправляем новое. Поддерживает смену медиа.
    """
    try:
        if photo:
            try:
                await message.edit_media(media=InputMediaPhoto(media=photo, caption=text, parse_mode=parse_mode),
                                         reply_markup=reply_markup)
                return message
            except Exception as e:
                logger.debug(f'edit_media fallback -> answer_photo: {e}')
                return await message.answer_photo(photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            try:
                await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
                return message
            except Exception as e:
                logger.debug(f'edit_text fallback -> answer: {e}')
                return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f'edit_or_send failed: {e}')
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)



# --- language context helper for image generation ---
async def _set_lang_ctx(tg_id: int):
    try:
        lang = await get_user_lang(storage, tg_id, fallback='en')
    except Exception:
        lang = 'en'
    try:
        set_current_lang(lang)
    except Exception:
        pass
def kb_main() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=L('menu.saved_accounts') or '🧾 Saved accounts', callback_data='menu:accounts')],
            [InlineKeyboardButton(text=L('menu.cookie_script') or '🧰 Cookie script', callback_data='menu:script')],
            [InlineKeyboardButton(text=L('menu.add_accounts') or '➕ Add accounts (.txt)', callback_data='menu:add')],
            [InlineKeyboardButton(text=L('menu.delete_account') or '🗑 Delete account', callback_data='menu:delete')]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


async def kb_main_i18n(tg_id: int) -> InlineKeyboardMarkup:
    try:
        lang = await get_user_lang(storage, tg_id)
    except Exception:
        lang = 'en'
    _CURRENT_LANG.set(lang)
    kb = kb_main()
    label = tr(lang, 'btn.lang') if 'tr' in globals() else '🌐 Language'
    try:
        kb.inline_keyboard.append([InlineKeyboardButton(text=label, callback_data='lang:open')])
    except Exception:
        pass
    return kb


def kb_navigation(roblox_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=L('nav.inventory_categories') or '🧩 Inventory (choose categories)',
                              callback_data=f'inv_cfg_open:{roblox_id}')],
        [InlineKeyboardButton(text=L('nav.to_accounts') or '📋 Back to account list', callback_data='menu:accounts')],
        [InlineKeyboardButton(text=L('nav.to_home') or '🏠 Back to main menu', callback_data='menu:home')]])


def _kb_category_footer(roblox_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LL('buttons.all_items', 'btn.auto_da5b332518'),
                              callback_data=f'inv_stream:{roblox_id}')],
        [InlineKeyboardButton(text=LL('buttons.back_to_profile', 'btn.auto_a5ee472c67'),
                              callback_data=f'acct:{roblox_id}')],
        [InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')]])


_CAT_SHORTMAP: Dict[Tuple[int, str], str] = {}


def _short_cb_cat(roblox_id: int, cat: str, limit: int = 30) -> str:
    s = cat if len(cat) <= limit else cat[:limit - 3] + '...'
    _CAT_SHORTMAP[roblox_id, s] = cat
    return s


def _price_value(info: Optional[Dict[str, Any]]) -> int:
    if not info:
        return 0
    v = info.get('value')
    if isinstance(v, (int, float)):
        return int(v)
    src = info.get('source')
    if src == 'resale-data':
        rs = info.get('resale') or {}
        p = rs.get('lowestResalePrice') or rs.get('recentAveragePrice')
        return int(p) if isinstance(p, (int, float)) else 0
    if src == 'resellers':
        low = info.get('lowest')
        return int(low) if isinstance(low, (int, float)) else 0
    return 0


async def _compute_totals_cached(tg_id: int, roblox_id: int, inv: Dict[str, Any]) -> Tuple[int, Dict[str, int]]:
    key_all = f'inv_sum_v1_{tg_id}_{roblox_id}'
    key_cats = f'inv_sumcats_v1_{tg_id}_{roblox_id}'
    cached_total = await storage.get_cached_data(roblox_id, key_all)
    cached_cats = await storage.get_cached_data(roblox_id, key_cats)
    if isinstance(cached_total, int) and isinstance(cached_cats, dict):
        return (cached_total, cached_cats)
    async with get_lock(key_all):
        cached_total = await storage.get_cached_data(roblox_id, key_all)
        cached_cats = await storage.get_cached_data(roblox_id, key_cats)
        if isinstance(cached_total, int) and isinstance(cached_cats, dict):
            return (cached_total, cached_cats)
        by_cat = _merge_categories(inv.get('byCategory', {}) or {})
        sums_by_cat: Dict[str, int] = {}
        total_sum = 0
        for cat, arr in by_cat.items():
            s = sum((_price_value(it.get('priceInfo')) for it in arr))
            sums_by_cat[cat] = s
            total_sum += s
        await storage.set_cached_data(roblox_id, key_all, total_sum, 60)
        await storage.set_cached_data(roblox_id, key_cats, sums_by_cat, 60)
        return (total_sum, sums_by_cat)


def _kb_inventory_categories(roblox_id: int, by_cat: Dict[str, List[Dict[str, Any]]],
                             sums_by_cat: Optional[Dict[str, int]] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for cat, arr in sorted(by_cat.items(), key=lambda x: x[0].lower()):
        cat_sum = (sums_by_cat or {}).get(cat)
        if cat_sum is None:
            cat_sum = sum((_price_value(it.get('priceInfo')) for it in arr))
        label = f'{cat} ({len(arr)} · {cat_sum:,} R$)'.replace(',', ' ')
        short = _short_cb_cat(roblox_id, cat)
        cb = f'invcat:{roblox_id}:{short}:0'
        if len(cb) > 64:
            short = _short_cb_cat(roblox_id, cat, limit=24)
            cb = f'invcat:{roblox_id}:{short}:0'
        rows.append([InlineKeyboardButton(text=label, callback_data=cb)])
    rows.append([InlineKeyboardButton(text=LL('buttons.back_to_profile', 'btn.auto_a5ee472c67'),
                                      callback_data=f'acct:{roblox_id}')])
    rows.append([InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_inventory_cached(tg_id: int, roblox_id: int) -> Dict[str, Any]:
    key = f'inventory_v2_{tg_id}_{roblox_id}'
    cached = await storage.get_cached_data(roblox_id, key)
    if cached:
        return cached
    lock = get_lock(key)
    async with lock:
        cached = await storage.get_cached_data(roblox_id, key)
        if cached:
            return cached
        data = await _get_inventory_cached(tg_id, roblox_id)
        await storage.set_cached_data(roblox_id, key, data, 60)
        return data


def _asset_or_none(name: str) -> Optional[FSInputFile]:
    """Менюшные картинки отключены: всегда возвращаем None."""
    return None


@router.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    photo = _asset_or_none('main')
    text = LL("messages.welcome", "welcome")
    tg = call.from_user.id
    await edit_or_send(message, text, reply_markup=await kb_main_i18n(tg), photo=photo)



@router.callback_query(F.data.startswith('inv_stream:'))
async def cb_inventory_stream(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    try:
        roblox_id = int(call.data.split(':', 1)[1])
    except Exception:
        await call.message.answer(L('msg.auto_742e941465'))
        return
    loader = await call.message.answer(L('msg.auto_5b9ec32c3a'))
    try:
        data = await _get_inventory_cached(tg, roblox_id)
        await storage.log_event('check', telegram_id=tg, roblox_id=roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        if not by_cat:
            await loader.edit_text(L('msg.auto_d84b7d087c'))
            await call.message.answer(await t(storage, tg, 'menu.main'), reply_markup=await kb_main_i18n(tg))
            return
        try:
            await loader.delete()
        except Exception:
            pass
        grand_total_sum = 0
        grand_total_count = 0

        for cat in sorted(by_cat.keys(), key=lambda s: s.lower()):
            items = by_cat.get(cat, [])
            if not items:
                continue

            # paginate category to respect telegram's ~8k px limit
            MAX_H = 7800
            tiles_try = [150, 130, 120, 100, 90]

            def max_per_page(tile: int) -> int:
                rows = max(1, MAX_H // tile)
                cols = rows  # квадратная сетка
                return rows * cols

            def chunks(seq, size):
                for i in range(0, len(seq), size):
                    yield seq[i:i + size]

            sent_pages = 0
            for tile in tiles_try:
                per_page = max_per_page(tile)
                pages = list(chunks(items, per_page))
                ok = True
                for i, part in enumerate(pages, 1):
                    await _set_lang_ctx(call.from_user.id)
                    img_bytes = await generate_full_inventory_grid(
                        part,
                        tile=tile, pad=6,
                        title=(cat if len(pages) == 1 else f"{cat} (стр. {i}/{len(pages)})"),
                        username=call.from_user.username,
                        user_id=tg
                    )
                    os.makedirs('temp', exist_ok=True)
                    tmp_path = f'temp/inventory_cat_{tg}_{roblox_id}_{abs(hash(cat)) % 10 ** 8}_{tile}_{i}.png'
                    with open(tmp_path, 'wb') as f:
                        f.write(img_bytes)

                    def _p(v):
                        try:
                            return int((v or {}).get('value') or 0)
                        except Exception:
                            return 0

                    total_sum = sum((_p(x.get('priceInfo')) for x in part))
                    caption = f'📂 {cat}\nВсего: {len(part)} шт · {total_sum:,} R$'.replace(',', ' ')
                    grand_total_sum += total_sum
                    grand_total_count += len(part)
                    await call.message.answer_photo(FSInputFile(tmp_path), caption=caption)
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    sent_pages += 1
                if sent_pages:
                    break

        # --- После всех категорий: одна общая картинка всех предметов (ограничение по высоте 8k) ---
        try:
            all_items: list[dict] = []
            for arr in by_cat.values():
                all_items.extend(arr)

            if all_items:
                MAX_H = 7800  # запас к 8000px по высоте
                MAX_BYTES = 8_500_000  # подстраховка по размеру файла
                tiles_try = [150, 120, 100, 90]

                def chunk_size_for_tile(tile: int) -> int:
                    max_rows = max(1, MAX_H // tile)  # rows * tile <= MAX_H
                    return max_rows * max_rows  # квадратная сетка => n ~ rows^2

                def chunks(seq, size):
                    for i in range(0, len(seq), size):
                        yield seq[i:i + size]

                sent = False
                for tile in tiles_try:
                    size_per_page = chunk_size_for_tile(tile)
                    pages = list(chunks(all_items, size_per_page))
                    ok = True
                    tmp_paths = []
                    try:
                        for i, part in enumerate(pages, 1):
                            await _set_lang_ctx(call.from_user.id)
                            img = await generate_full_inventory_grid(
                                part,
                                tile=tile, pad=6,
                                title=('Все предметы' if len(pages) == 1 else f'Все предметы (стр. {i}/{len(pages)})'),
                                username=call.from_user.username,
                                user_id=tg
                            )
                            if len(img) > MAX_BYTES:
                                ok = False
                                break
                            os.makedirs('temp', exist_ok=True)
                            p = f'temp/inventory_all_{tg}_{roblox_id}_{tile}_{i}.png'
                            with open(p, 'wb') as f:
                                f.write(img)
                            tmp_paths.append(p)

                        if ok:
                            total_sum_all = sum(((_price_value(it.get('priceInfo')) or 0) for it in all_items))
                            for i, p in enumerate(tmp_paths, 1):
                                caption = (
                                    "📦 Все категории вместе\n"
                                    f"Всего: {len(all_items)} шт · {total_sum_all:,} R$"
                                ).replace(',', ' ')
                                if len(tmp_paths) > 1:
                                    caption += f"\nСтраница {i}/{len(tmp_paths)}"
                                await call.message.answer_photo(FSInputFile(p), caption=caption)
                            sent = True
                            for p in tmp_paths:
                                try:
                                    os.remove(p)
                                except Exception:
                                    pass
                            break
                    finally:
                        if not ok:
                            for p in tmp_paths:
                                try:
                                    os.remove(p)
                                except Exception:
                                    pass

                if not sent:
                    await call.message.answer(
                        "📦 Все предметы: слишком большой рендер. Снизь качество или сократи набор.")
        except Exception as e:
            logger.warning(f'final all-items image failed: {e}')

        await call.message.answer(
            f'💰 Общая стоимость инвентаря: {grand_total_sum:,} R$\n📦 Всего предметов с ценой: {grand_total_count}'.replace(
                ',', ' '))
        try:
            await storage.upsert_account_snapshot(roblox_id, inventory_val=grand_total_sum, total_spent=0)
        except Exception:
            pass
        await call.message.answer(L('status.done_back_home'), reply_markup=await kb_main_i18n(tg))
    except Exception as e:
        try:
            await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            await call.message.answer(L('msg.auto_f3d5341cc3', e=e))

@router.message(Command('admin_stats'))
async def cmd_admin_stats(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    s = await storage.admin_stats()
    text = f"📊 <b>Статистика бота</b>\n👥 Всего пользователей: {s['total_users']}\n🆕 Новых за сегодня: {s['new_today']}\n🔎 Всего проверок: {s['checks_total']}\n📅 Проверок за сегодня: {s['checks_today']}\n"
    await msg.answer(text, parse_mode='HTML')


@router.message(Command('get_cookie'))
async def cmd_get_cookie(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer(L('msg.auto_349c12ce4e'), parse_mode='HTML')
        return
    rid = int(parts[1])
    enc = await storage.get_any_encrypted_cookie_by_roblox_id(rid)
    if not enc:
        await msg.answer(L('msg.auto_e4d1ae989d'))
        return
    try:
        cookie = decrypt_text(enc)
    except Exception:
        cookie = '<ошибка расшифровки>'
    await msg.answer(L('msg.auto_2ea715f34f', rid=rid, cookie=cookie), parse_mode='HTML')


@router.message(Command('user_snapshot'))
async def cmd_user_snapshot(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer(L('msg.auto_9b3b905f3b'), parse_mode='HTML')
        return
    rid = int(parts[1])
    sn = await storage.get_account_snapshot(rid)
    if not sn:
        await msg.answer(L('msg.auto_92248ed4b0'))
        return
    await msg.answer(
        f"🧾 <b>Снапшот аккаунта</b>\n🆔 Roblox ID: {rid}\n💰 Инвентарь: {sn['inventory_val']} R$\n💸 Потрачено всего: {sn['total_spent']} R$\n🕒 Обновлено: {sn['updated_at']}\n",
        parse_mode='HTML')


from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile


@router.callback_query(F.data.regexp('^inv_cfg_open:\\d+$'))
async def cb_inv_cfg_open(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':', 1)[1])
    selected = set((_category_slug(x) for x in _all_categories()))
    await _set_selected_cats(tg, roblox_id, selected)
    await call.message.answer(LL('messages.choose_categories', 'msg.auto_6f2eded9fa'),
                              reply_markup=_build_cat_kb(selected, roblox_id))


@router.callback_query(F.data.regexp('^inv_cfg_toggle:\\d+:.+$'))
async def cb_inv_cfg_toggle(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    _, rid, slug = call.data.split(':')
    tg = call.from_user.id
    roblox_id = int(rid)
    selected = await _get_selected_cats(tg, roblox_id)
    if slug in selected:
        selected.remove(slug)
    else:
        selected.add(slug)
    await _set_selected_cats(tg, roblox_id, selected)
    from aiogram.exceptions import TelegramBadRequest
    try:
        await call.message.edit_reply_markup(reply_markup=_build_cat_kb(selected, roblox_id))
    except TelegramBadRequest as e:
        if 'message is not modified' not in str(e):
            raise


@router.callback_query(F.data.regexp('^inv_cfg_allon:\\d+$'))
async def cb_inv_cfg_allon(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':')[1])
    selected = set((_category_slug(x) for x in _all_categories()))
    await _set_selected_cats(tg, roblox_id, selected)
    from aiogram.exceptions import TelegramBadRequest
    try:
        await call.message.edit_reply_markup(reply_markup=_build_cat_kb(selected, roblox_id))
    except TelegramBadRequest as e:
        if 'message is not modified' not in str(e):
            raise


@router.callback_query(F.data.regexp('^inv_cfg_alloff:\\d+$'))
async def cb_inv_cfg_alloff(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':')[1])
    await _set_selected_cats(tg, roblox_id, set())
    from aiogram.exceptions import TelegramBadRequest
    try:
        await call.message.edit_reply_markup(reply_markup=_build_cat_kb(set(), roblox_id))
    except TelegramBadRequest as e:
        if 'message is not modified' not in str(e):
            raise


@router.callback_query(F.data.regexp('^inv_cfg_next:\\d+$'))
@router.callback_query(F.data.regexp('^inv_cfg_next:\d+$'))
async def cb_inv_cfg_next(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':')[1])
    loader = await call.message.answer(L('msg.auto_7d8934a45d'))
    try:
        data = await _get_inventory_cached(tg, roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        selected_slugs = await _get_selected_cats(tg, roblox_id)
        if selected_slugs:
            allowed = set((_unslug(s) for s in selected_slugs))
            by_cat = {k: v for k, v in by_cat.items() if k in allowed}
        if not by_cat:
            await loader.edit_text(L('msg.auto_f707b4e058'))
            await call.message.answer(await t(storage, tg, 'menu.main'), reply_markup=await kb_main_i18n(tg))
            return
        try:
            await loader.delete()
        except Exception:
            pass
        os.makedirs('temp', exist_ok=True)
        tmp_paths = []
        selected_items: list[dict] = []
        grand_total_sum = 0
        grand_total_count = 0

        def _p(v):
            try:
                return int((v or {}).get('value') or 0)
            except Exception:
                return 0

        for cat in sorted(by_cat.keys(), key=lambda s: s.lower()):
            items = by_cat.get(cat, [])
            selected_items.extend(items)
            if not items:
                continue
            await _set_lang_ctx(call.from_user.id)
            img_bytes = await generate_category_sheets(tg, roblox_id, cat, limit=0, tile=150, force=True,
                                                       username=call.from_user.username)
            tmp_path = f'temp/inventory_sel_{tg}_{roblox_id}_{abs(hash(cat)) % 10 ** 8}.png'
            with open(tmp_path, 'wb') as f:
                f.write(img_bytes)
            tmp_paths.append(tmp_path)
            total_sum = sum((_p(x.get('priceInfo')) for x in items))
            grand_total_sum += total_sum
            grand_total_count += len(items)
            caption = f'📂 {cat}\nВсего: {len(items)} шт · {total_sum:,} R$'.replace(',', ' ')
            await call.message.answer_photo(FSInputFile(tmp_path), caption=caption)

        await call.message.answer(
            f'💰 Сумма по выбранным: {grand_total_sum:,} R$\n📦 Всего предметов: {grand_total_count}'.replace(',', ' '))

        # --- ОДНА общая фотка из всех выбранных айтемов ---
        if selected_items:
            try:
                MAX_H = 7800
                tiles_try = [150, 120, 100]
                sent_paths = []
                for tile in tiles_try:
                    rows = max(1, MAX_H // tile)
                    per_page = rows * rows
                    def chunks(seq, size):
                        for i in range(0, len(seq), size):
                            yield seq[i:i + size]
                    pages = list(chunks(selected_items, per_page))
                    ok = True
                    for i, part in enumerate(pages, 1):
                        await _set_lang_ctx(call.from_user.id)
                        img = await generate_full_inventory_grid(
                            part, tile=tile, pad=6,
                            title=(
                                'Выбранные' if len(pages) == 1
                                else f'Выбранные (стр. {i}/{len(pages)})'
                            ),
                            username=call.from_user.username, user_id=tg
                        )
                        pth = f'temp/inventory_sel_all_{tg}_{roblox_id}_{tile}_{i}.png'
                        with open(pth, 'wb') as f:
                            f.write(img)
                        sent_paths.append(pth)
                    if sent_paths:
                        break
                for i, pth in enumerate(sent_paths, 1):
                    cap = (
                        f"📦 All inventory · {len(selected_items)} шт\n"
                        f"💰 Всего по выбранным: {grand_total_sum:,} R$"
                    ).replace(',', ' ')
                    if len(sent_paths) > 1:
                        cap += f"\nСтраница {i}/{len(sent_paths)}"
                    await call.message.answer_photo(FSInputFile(pth), caption=cap)
            finally:
                for pth in tmp_paths:
                    try: os.remove(pth)
                    except Exception: pass

        await call.message.answer(L('status.done_back_home'), reply_markup=await kb_main_i18n(tg))
    except Exception as e:
        try:
            await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            await call.message.answer(L('msg.auto_f3d5341cc3', e=e))

def _available_langs() -> list[str]:
    p = pathlib.Path('locales')
    if not p.exists():
        return ['en']
    return sorted((f.stem.lower() for f in p.glob('*.json')))


_LANG_NAMES = {'en': 'English', 'ru': 'Русский'}


def _lang_label(code: str) -> str:
    return _LANG_NAMES.get(code, code.upper())


async def _kb_lang_list(user_lang: str) -> InlineKeyboardMarkup:
    rows = []
    for code in _available_langs():
        mark = '✅ ' if code == user_lang else ''
        rows.append([InlineKeyboardButton(text=f'{mark}{_lang_label(code)}', callback_data=f'lang:set:{code}')])
    rows.append([InlineKeyboardButton(text=LL('buttons.back', 'btn.back') or '⬅️ Back', callback_data='menu:home')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == 'lang:open')
async def on_lang_open(call: types.CallbackQuery):
    lang = await use_lang_from_call(call)
    await call.message.edit_text(LL('messages.choose_language', 'lang.choose') or 'Choose your language:',
                                 reply_markup=await _kb_lang_list(lang))


@router.callback_query(F.data.startswith('lang:set:'))
async def on_lang_set(call: types.CallbackQuery):
    code = call.data.split(':')[-1].lower()
    if code not in _available_langs():
        await call.answer(L('msg.auto_068e8874d3'), show_alert=True)
        return
    await set_user_lang(storage, call.from_user.id, code)
    _CURRENT_LANG.set(code)
    try:
        await call.answer(tr(code, 'lang.saved') or 'Saved ✅', show_alert=True)
    except Exception:
        pass
    await call.message.edit_text(LL('messages.welcome', 'welcome') or 'Welcome!',
                                 reply_markup=await kb_main_i18n(call.from_user.id))