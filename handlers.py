import os
import time
import traceback
import html
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
from roblox_imagegen import generate_category_sheets
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

# ---------- logging setup (file) ----------
os.makedirs('logs', exist_ok=True)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(os.path.join('logs', 'bot.log'), encoding='utf-8')
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(_fh)
# ------------------------------------------
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
            set_current_lang(lang)

        return await handler(event, data)


try:
    router.message.middleware(LangMiddleware())
    router.callback_query.middleware(LangMiddleware())
except Exception:
    pass


# === Public info helpers ===
async def _set_public_pending(tg_id: int, flag: bool, ttl: int = 600):
    try:
        await storage.set_cached_data(tg_id, 'await_public_id', 1 if flag else 0, ttl)
    except Exception:
        pass

async def _is_public_pending(tg_id: int) -> bool:
    try:
        v = await storage.get_cached_data(tg_id, 'await_public_id')
        return bool(int(v or 0))
    except Exception:
        return False




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
    set_current_lang(lang)
    return lang


async def use_lang_from_call(call) -> str:
    try:
        lang = await get_user_lang(storage, call.from_user.id, fallback='en')
    except Exception:
        lang = 'en'
    _CURRENT_LANG.set(lang)
    set_current_lang(lang)
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


def kb_main() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=L('menu.add_accounts') or '➕ Add accounts (.txt)', callback_data='menu:add'),
            InlineKeyboardButton(text=L('menu.saved_accounts') or '🧾 Saved accounts', callback_data='menu:accounts'),
        ],
        [
            InlineKeyboardButton(text=L('menu.public_info') or '🌐 Public info', callback_data='menu:public'),
            InlineKeyboardButton(text=L('menu.delete_account') or '🗑 Delete account', callback_data='menu:delete'),
        ],
        [InlineKeyboardButton(text=L('menu.cookie_script') or '🧰 Cookie script', callback_data='menu:script')],
        [InlineKeyboardButton(text=L('menu.settings') or '⚙️ Settings', callback_data='menu:settings')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_main_i18n(tg_id: int) -> InlineKeyboardMarkup:
    try:
        lang = await get_user_lang(storage, tg_id)
    except Exception:
        lang = 'en'
    _CURRENT_LANG.set(lang)
    return kb_main()

def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(tr(_CURRENT_LANG.get(), 'btn.lang') if 'tr' in globals() else '🌐 Язык'),
                              callback_data='lang:open')],
        [InlineKeyboardButton(text=L('buttons.home') or '🏠 В главное меню',
                              callback_data='menu:home')]
    ])

@router.callback_query(F.data == 'menu:settings')
async def cb_settings(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    await edit_or_send(
        call.message,
        L('settings.title') or '⚙️ Settings',
        reply_markup=kb_settings()
    )

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



async def _get_inventory_cached(tg_id: int, roblox_id: int) -> dict:
    """Try to get inventory:
    1) via cookie bound to (tg_id, roblox_id);
    2) if not available/failed/empty — iterate any valid cookie from DB;
    If still no items, treat as private.
    """
    # Try direct (bound) cookie first
    try:
        data = await roblox_client.get_full_inventory(tg_id, roblox_id)
        # If it returned non-empty byCategory -> good
        if isinstance(data, dict) and (data.get('byCategory') or {}):
            return data
    except Exception:
        data = None
    # Fallback: try any cookie present in DB
    try:
        # Pull any cookie for this roblox_id first (same owner), else ANY cookie in DB
        enc = await storage.get_any_encrypted_cookie_by_roblox_id(roblox_id)
        tried = set()
        candidates = []
        if enc:
            candidates.append(enc)
            tried.add(enc)
        # Optional: scan all owners for more cookies (best-effort)
        if hasattr(storage, 'list_all_owners'):
            try:
                owners = await storage.list_all_owners()
            except Exception:
                owners = []
        else:
            owners = []
        # For performance, we only try up to 5 random cookies (deterministic order here).
        if owners:
            for oid in owners[:5]:
                # For each owner, try to fetch their cookie for their FIRST account (cheap)
                # We'll query DB directly through helper below.
                pass
        # If we don't have iteration helpers, at least use the one cookie we found above.
        for enc_cookie in candidates or []:
            try:
                data2 = await roblox_client.get_full_inventory_by_encrypted_cookie(enc_cookie, roblox_id)
                if isinstance(data2, dict) and (data2.get('byCategory') or {}):
                    return data2
            except Exception:
                continue
    except Exception:
        pass
    # If we reached here — most likely private
    return {'byCategory': {}}

def _asset_or_none(name: str) -> Optional[FSInputFile]:
    """Менюшные картинки отключены: всегда возвращаем None."""
    return None


@router.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    # ensure lang context is set to user's stored language
    await use_lang_from_message(message)
    photo = _asset_or_none('main')
    text = LL("messages.welcome", "welcome")
    tg = message.from_user.id
    await edit_or_send(message, text, reply_markup=await kb_main_i18n(tg), photo=photo)


@router.callback_query(F.data == 'menu:home')
async def cb_home(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    photo = _asset_or_none('main')
    text = LL("messages.welcome", "welcome")
    tg = call.from_user.id
    await edit_or_send(call.message, text, reply_markup=await kb_main_i18n(tg), photo=photo)



@router.callback_query(F.data == 'menu:public')
async def cb_public_open(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    await _set_public_pending(tg, True, ttl=600)
    await edit_or_send(call.message, L('public.ask_id'), reply_markup=await kb_main_i18n(tg))


@router.callback_query(F.data.startswith('menu:'))
async def cb_menu(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    action = call.data.split(':', 1)[1]
    if action == 'accounts':
        try:
            accounts = await storage.list_users(tg)
            photo = _asset_or_none('accounts')
            if not accounts:
                msg = L("status.no_accounts")
                await edit_or_send(call.message, msg, reply_markup=await kb_main_i18n(tg), photo=photo)
                return
            rows = [[InlineKeyboardButton(text=u if u else f'ID: {r}', callback_data=f'acct:{r}')] for r, u in accounts]
            rows.append(
                [InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')])
            caption = LL("captions.accounts_list", "caption.accounts_list", count=len(accounts))
            await edit_or_send(call.message, caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               photo=photo)
        except Exception as e:
            logger.error(f'menu:accounts error: {e}')
            await edit_or_send(call.message, '❌ Ошибка при загрузке аккаунтов.', reply_markup=await kb_main_i18n(tg))
    elif action == 'script':
        try:
            text = L("cookie.instructions")
            await edit_or_send(call.message, text, reply_markup=await kb_main_i18n(tg), photo=_asset_or_none('script'))
            zip_path = create_cookie_zip(tg)
            if os.path.exists(zip_path):
                await call.message.answer_document(FSInputFile(zip_path, filename='cookie_kit.zip'),
                                                   caption=L("cookie.kit_caption"))
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
            else:
                await call.message.answer(L('msg.auto_b95899d0eb'))
        except Exception as e:
            logger.error(f'menu:script zip error: {e}')
            await call.message.answer(L('msg.auto_a6dc67d175'))
    elif action == 'add':
        await edit_or_send(call.message, L("status.pick_file"), reply_markup=await kb_main_i18n(tg),
                           photo=_asset_or_none('add'))
    elif action == 'delete':
        try:
            accounts = await storage.list_users(tg)
            if not accounts:
                await edit_or_send(call.message, '⚠️ Нет аккаунтов для удаления.', reply_markup=await kb_main_i18n(tg),
                                   photo=_asset_or_none('delete'))
                return
            rows = [[InlineKeyboardButton(text=u if u else f'ID: {r}', callback_data=f'delacct:{r}')] for r, u in
                    accounts]
            rows.append(
                [InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')])
            await edit_or_send(call.message, L("msg.delete_pick_account"),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), photo=_asset_or_none('delete'))
        except Exception as e:
            logger.error(f'menu:delete error: {e}')
            await edit_or_send(call.message, '❌ Ошибка при загрузке аккаунтов.', reply_markup=await kb_main_i18n(tg))


@router.message(F.document)
async def handle_txt_upload(message: types.Message) -> None:
    tg = call.from_user.id
    if not CFG.ALLOW_PUBLIC_COOKIE and tg != CFG.OWNER_ID:
        await edit_or_send(message, '⛔ Загрузка cookie разрешена только владельцу бота.',
                           reply_markup=await kb_main_i18n(tg))
        return
    doc = message.document
    name = (doc.file_name or '').lower()
    mime = (doc.mime_type or '').lower()
    if not (name.endswith('.txt') or mime == 'text/plain'):
        await edit_or_send(message, '⚠️ Это не .txt. Пришли файл с куки в формате .txt',
                           reply_markup=await kb_main_i18n(tg))
        return
    await edit_or_send(message, '📥 Файл получен, проверяю...', reply_markup=await kb_main_i18n(tg))
    os.makedirs('temp', exist_ok=True)
    tmp_path = f'temp/cookies_{tg}_{doc.file_unique_id}.txt'
    try:
        await message.bot.download(doc, destination=tmp_path)
    except Exception:
        try:
            f = await message.bot.get_file(doc.file_id)
            await message.bot.download(f.file_path, destination=tmp_path)
        except Exception as e2:
            await edit_or_send(message, L("err.download_file", error=e2), reply_markup=await kb_main_i18n(tg))
            return
    try:
        with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception as e:
        await edit_or_send(message, L("err.read_file", error=e), reply_markup=await kb_main_i18n(tg))
        return
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    if not lines:
        await edit_or_send(message, L("status.file_empty"), reply_markup=await kb_main_i18n(tg))
        return
    ok, bad = (0, 0)
    added: List[Tuple[int, str]] = []
    for line in lines[:1000]:
        is_valid, cleaned_cookie, user_data = await validate_and_clean_cookie(line)
        if not is_valid:
            bad += 1
            continue
        rid = int(user_data['id'])
        uname = user_data.get('name') or ''
        enc = encrypt_text(cleaned_cookie)
        await storage.save_encrypted_cookie(tg, rid, enc)
        await storage.upsert_user(tg, rid, uname, user_data.get('created'))
        await storage.log_event('user_linked', telegram_id=tg, roblox_id=rid)
        ok += 1
        added.append((rid, uname))
    if ok == 0:
        await edit_or_send(message, '❌ Все строки невалидны.', reply_markup=await kb_main_i18n(tg))
        return
    rows = [[InlineKeyboardButton(text=u if u else f'ID: {r}', callback_data=f'acct:{r}')] for r, u in added]
    rows.extend([[InlineKeyboardButton(text=L('btn.auto_8cd0fba739'), callback_data='menu:accounts')],
                 [InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')]])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await edit_or_send(message, L("status.added_result", ok=ok, bad=bad), reply_markup=kb)


@router.callback_query(F.data.startswith('delacct:'))
async def cb_delete_account(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    rid = int(call.data.split(':', 1)[1])
    try:
        await storage.delete_cookie(tg, rid)
        await edit_or_send(call.message, L("status.account_deleted"), reply_markup=await kb_main_i18n(tg))
    except Exception as e:
        logger.error(f'delete account error {rid}: {e}')
        await edit_or_send(call.message, '❌ Ошибка при удалении аккаунта.', reply_markup=await kb_main_i18n(tg))


@router.callback_query(F.data.startswith('acct:'))
async def cb_show_account(call: types.CallbackQuery) -> None:
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':', 1)[1])
    # ---------- FAST PATH: cache first ----------
    lang = await use_lang_from_call(call)
    # try our new mem cache
    rec = _profile_mem_get2(tg, roblox_id, lang)
    if isinstance(rec, dict) and rec.get("text"):
        pid = rec.get("photo_id")
        try:
            if pid:
                await call.message.answer_photo(pid, caption=rec["text"], reply_markup=kb_navigation(roblox_id))
            else:
                await call.message.answer(rec["text"], reply_markup=kb_navigation(roblox_id))
        except Exception:
            await call.message.answer(rec["text"], reply_markup=kb_navigation(roblox_id))
        return
    # try existing project mem cache if present
    try:
        _rec_old = _get_profile_mem(tg, roblox_id)  # may not exist
    except Exception:
        _rec_old = None
    if isinstance(_rec_old, dict) and _rec_old.get("text"):
        pid = _rec_old.get("photo_id")
        try:
            if pid:
                await call.message.answer_photo(pid, caption=_rec_old["text"], reply_markup=kb_navigation(roblox_id))
            else:
                await call.message.answer(_rec_old["text"], reply_markup=kb_navigation(roblox_id))
        except Exception:
            await call.message.answer(_rec_old["text"], reply_markup=kb_navigation(roblox_id))
        return
    # storage cache
    try:
        rec = await _profile_store_get2(storage, tg, roblox_id, lang)
    except Exception:
        rec = None
    if isinstance(rec, dict) and rec.get("text"):
        pid = rec.get("photo_id")
        try:
            if pid:
                await call.message.answer_photo(pid, caption=rec["text"], reply_markup=kb_navigation(roblox_id))
            else:
                await call.message.answer(rec["text"], reply_markup=kb_navigation(roblox_id))
        except Exception:
            await call.message.answer(rec["text"], reply_markup=kb_navigation(roblox_id))
        _profile_mem_set2(tg, roblox_id, lang, text=rec["text"], photo_id=rec.get("photo_id"))
        return
    # --------------------------------------------

    loader = await call.message.answer(LL('status.loading_profile', 'msg.auto_cefe60da21'))
    try:
        enc = await storage.get_encrypted_cookie(tg, roblox_id)
        if not enc:
            await edit_or_send(call.message, '⚠️ Cookie для этого аккаунта не найдена.',
                               reply_markup=await kb_main_i18n(tg))
            return
        cookie = decrypt_text(enc)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Cookie': f'.ROBLOSECURITY={cookie}',
                   'Referer': 'https://www.roblox.com/'}
        async with httpx.AsyncClient(timeout=20.0) as c:
            u = await c.get(f'https://users.roblox.com/v1/users/{roblox_id}', headers=headers)
            if u.status_code != 200:
                await edit_or_send(call.message, '❌ Не удалось получить профиль.', reply_markup=await kb_main_i18n(tg))
                return
            user = u.json()
            uname = html.escape(user.get('name', '—'))
            dname = html.escape(user.get('displayName', '—'))
            created = (user.get('created') or 'N/A').split('T')[0]
            banned = bool(user.get('isBanned', False))
            country = await storage.get_cached_data(roblox_id, 'acc_country_v1')
            if country is None:
                r = await c.get('https://accountsettings.roblox.com/v1/account/settings/account-country',
                                headers=headers)
                country = '—'
                if r.status_code == 200:
                    v = (r.json() or {}).get('value', {})
                    country = v.get('localizedName') or v.get('countryName') or '—'
                await storage.set_cached_data(roblox_id, 'acc_country_v1', country, 24 * 60)
            refresh_email = True
            email_data = None
            if not refresh_email:
                email_data = await storage.get_cached_data(roblox_id, 'acc_email_v1')
            if not isinstance(email_data, dict):
                email, email_verified = ('—', False)
                r = await c.get('https://accountsettings.roblox.com/v1/email', headers=headers)
                if r.status_code == 200:
                    ej = r.json() or {}
                    email = ej.get('email') or ej.get('emailAddress') or ej.get('contactEmail') or '—'
                    email_verified = bool(ej.get('verified') or ej.get('isVerified'))
                await storage.set_cached_data(roblox_id, 'acc_email_v1', {'email': email, 'verified': email_verified},
                                              24 * 60)
            else:
                email = email_data.get('email', '—')
                email_verified = email_data.get('verified', False)
            gender = await storage.get_cached_data(roblox_id, 'acc_gender_v1')
            if gender is None:
                r = await c.get('https://accountinformation.roblox.com/v1/gender', headers=headers)
                gender = 'Не указан'
                if r.status_code == 200:
                    g = (r.json() or {}).get('gender')
                    if g == 1:
                        gender = '👩 Женский'
                    elif g == 2:
                        gender = '👨 Мужской'
                await storage.set_cached_data(roblox_id, 'acc_gender_v1', gender, 24 * 60)
            bd_cache = await storage.get_cached_data(roblox_id, 'acc_birth_v1')
            if isinstance(bd_cache, dict):
                birthdate, age = (bd_cache.get('birthdate', '—'), bd_cache.get('age', '—'))
            else:
                birthdate, age = ('—', '—')
                r = await c.get('https://accountinformation.roblox.com/v1/birthdate', headers=headers)
                if r.status_code == 200:
                    bd = r.json() or {}
                    d, m, y = (bd.get('birthDay'), bd.get('birthMonth'), bd.get('birthYear'))
                    if all([d, m, y]):
                        birthdate = f'{d:02d}.{m:02d}.{y}'
                        now = datetime.now()
                        age = now.year - y - (1 if (now.month, now.day) < (m, d) else 0)
                await storage.set_cached_data(roblox_id, 'acc_birth_v1', {'birthdate': birthdate, 'age': age}, 24 * 60)
            robux = await storage.get_cached_data(roblox_id, 'acc_robux_v1')
            if robux is None:
                r = await c.get('https://economy.roblox.com/v1/user/currency', headers=headers)
                robux = r.json().get('robux', 0) if r.status_code == 200 else 0
                await storage.set_cached_data(roblox_id, 'acc_robux_v1', robux, 5)
            spent_val = -1
            cached = await storage.get_cached_data(roblox_id, 'acc_spent_robux_v1')
            if cached is None:
                try:
                    import asyncio
                    spent_val = await asyncio.wait_for(roblox_client.get_total_spent_robux(roblox_id, cookie),
                                                       timeout=1.5)
                    await storage.set_cached_data(roblox_id, 'acc_spent_robux_v1', int(spent_val), 300)
                except Exception:
                    spent_val = -1

                    async def _warm():
                        try:
                            v = await roblox_client.get_total_spent_robux(roblox_id, cookie)
                            await storage.set_cached_data(roblox_id, 'acc_spent_robux_v1', int(v), 300)
                        except Exception:
                            pass

                    try:
                        import asyncio as _a;
                        _a.create_task(_warm())
                    except Exception:
                        pass
            else:
                spent_val = int(cached)
            premium = await storage.get_cached_data(roblox_id, 'acc_premium_v1')
            if premium is None:
                premium = '❌ Обычный'
                r = await c.get(f'https://premiumfeatures.roblox.com/v1/users/{roblox_id}/validate-membership',
                                headers=headers)
                if r.status_code == 200:
                    pj = r.json()
                    if isinstance(pj, bool) and pj or (isinstance(pj, dict) and (
                            pj.get('isPremium') or pj.get('hasMembership') or pj.get('premium'))):
                        premium = '⭐ Премиум'
                await storage.set_cached_data(roblox_id, 'acc_premium_v1', premium, 60)
            avatar_url = await storage.get_cached_data(roblox_id, 'acc_avatar_v1')
            if avatar_url is None:
                avatar_url = None
                ra = await c.get(
                    f'https://thumbnails.roblox.com/v1/users/avatar?userIds={roblox_id}&size=420x420&format=Png&isCircular=false',
                    headers=headers)
                if ra.status_code == 200 and (ra.json() or {}).get('data'):
                    avatar_url = ra.json()['data'][0].get('imageUrl')
                await storage.set_cached_data(roblox_id, 'acc_avatar_v1', avatar_url, 60)
        status = '✅ Активен' if not banned else '❌ Забанен'
        socials = await storage.get_cached_data(roblox_id, 'acc_socials_v1')
        if not isinstance(socials, dict):
            try:
                socials = await roblox_client.get_social_links(roblox_id)
            except Exception:
                socials = {}
            await storage.set_cached_data(roblox_id, 'acc_socials_v1', socials, 24 * 60)
        text = render_profile_text_i18n(
            uname=uname,
            dname=dname,
            roblox_id=roblox_id,
            created=created,
            country=country,
            gender_raw=gender,
            birthdate=birthdate,
            age=age,
            email=email,
            email_verified=email_verified,
            robux=robux,
            spent_val=spent_val,
            banned=banned,
        )

        try:
            await loader.delete()
        except Exception:
            pass
        if avatar_url:
            async with httpx.AsyncClient(timeout=20.0) as c:
                im = await c.get(avatar_url)
                if im.status_code == 200:
                    path = f'temp/avatar_{roblox_id}.png'
                    open(path, 'wb').write(im.content)
                    await edit_or_send(call.message, text, reply_markup=kb_navigation(roblox_id),
                                       photo=FSInputFile(path))
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    return
        await edit_or_send(call.message, text, reply_markup=kb_navigation(roblox_id))
    except Exception as e:
        logger.error(f'acct view error {roblox_id}: {e}')
        await edit_or_send(call.message, L("err.profile_load"), reply_markup=await kb_main_i18n(tg))


from typing import Dict, List, Any, Tuple
import roblox_client
from roblox_imagegen import generate_category_sheets, generate_full_inventory_grid
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from typing import Dict, List, Any, Tuple
from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
import roblox_client
from roblox_imagegen import generate_category_sheets, generate_full_inventory_grid

RICON = 'R$'
try:
    router
except NameError:
    from aiogram import Router

    router = Router()
_CAT_SHORTMAP: Dict[Tuple[int, str], str] = {}


def _price_of(it: Dict[str, Any]) -> int:
    v = it.get('priceInfo', {}).get('value')
    return int(v) if isinstance(v, (int, float)) else 0


def _sum_items(arr: List[Dict[str, Any]]) -> int:
    return sum((_price_of(x) for x in arr if _price_of(x) > 0))


def _filter_nonzero(arr: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [x for x in arr if _price_of(x) > 0]


def _short_name(roblox_id: int, name: str, max_len: int = 28) -> str:
    short = name if len(name) <= max_len else name[:max_len - 1] + '…'
    _CAT_SHORTMAP[roblox_id, short] = name
    return short


def _kb_categories_only(roblox_id: int, by_cat: Dict[str, List[Dict[str, Any]]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for cat, items in sorted(by_cat.items(), key=lambda kv: kv[0].lower()):
        nz = _filter_nonzero(items)
        if not nz:
            continue
        rows.append([InlineKeyboardButton(text=f'{cat} — {len(nz)} шт · {_sum_items(nz):,} {RICON}'.replace(',', ' '),
                                          callback_data=f'invcat:{roblox_id}:{_short_name(roblox_id, cat)}')])
    rows.append([InlineKeyboardButton(text=LL('buttons.refresh', 'btn.auto_e436dd91b6'),
                                      callback_data=f'invall_refresh:{roblox_id}')])
    rows.append([InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def _likely_private_inventory(err: Exception) -> bool:
    s = str(err) if err else ''
    for token in ('403', 'Forbidden', 'forbidden', 'private', 'privacy'):
        if token in s:
            return True
    return False

def _caption_full_inventory(total_count: int, total_sum: int) -> str:
    # "📦 {full_title}\n{total_items}\n{total_sum}"
    line1 = f"📦 {L('inventory.full_title') or 'Full inventory'}"
    line2 = L('inventory.total_items', count=total_count) or f"📦 Items with price: {total_count}"
    line3 = L('inventory.total_sum', sum=f"{total_sum:,}") or f"💰 Total inventory value: {total_sum:,}"
    return (line1 + "\n" + line2 + "\n" + line3).replace(',', ' ')

def _caption_category(cat_name: str, count: int, total_sum: int) -> str:
    # "📂 {cat}\nВсего: {count} шт · {sum} R$" via i18n
    cat_loc = cat_label(cat_name)
    txt = L('inventory.by_cat', cat=cat_loc, count=count, sum=f"{total_sum:,}")
    if not txt or txt == 'inventory.by_cat':
        txt = f"📂 {cat_loc}\nВсего: {count} шт · {total_sum:,} R$"
    return txt.replace(',', ' ')


def _kb_category_view(roblox_id: int, short_cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LL('buttons.all_items', 'btn.auto_c06b2d0103'),
                              callback_data=f'invall:{roblox_id}')],
        [InlineKeyboardButton(text=LL('buttons.refresh_category', 'btn.auto_57bdc7b26f'),
                              callback_data=f'invcat_refresh:{roblox_id}:{short_cat}')],
        [InlineKeyboardButton(text=LL('nav.categories', 'btn.auto_ac13d12b32'),
                              callback_data=f'inv_stream:{roblox_id}')],
        [InlineKeyboardButton(text=LL('buttons.home', 'btn.auto_46cf19b1dd'), callback_data='menu:home')]])


@router.callback_query(F.data.startswith('inv:'))
async def cb_inventory_full_then_categories(call: types.CallbackQuery) -> None:
    """Показываем СРАЗУ картинку ВСЕГО инвентаря (без 0 R$) + под ней кнопки категорий (без кнопки 'Все предметы')."""
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':', 1)[1])
    t0 = time.time()
    loader = await call.message.answer(L('msg.auto_e030221412'))
    try:
        logger.info(f"[inv_full] start tg={tg} rid={roblox_id}")
        data = await _get_inventory_cached(tg, roblox_id)
        logger.info(f"[inv_full] got inventory dict={isinstance(data, dict)} keys={list((data or {}).keys())}")
        await storage.log_event('check', telegram_id=tg, roblox_id=roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        logger.info(f"[inv_full] by_cat_count={len(by_cat)}")
        all_items: List[Dict[str, Any]] = []
        for arr in by_cat.values():
            all_items.extend(_filter_nonzero(arr))
        if not all_items:
            await loader.edit_text(L('public.inventory_private'))
            return
        img_bytes = await generate_full_inventory_grid(all_items, tile=150, pad=6, username=call.from_user.username,
                                                       user_id=call.from_user.id)
        import os
        os.makedirs('temp', exist_ok=True)
        path = f'temp/inventory_all_{tg}_{roblox_id}.png'
        with open(path, 'wb') as f:
            f.write(img_bytes)
        total = len(all_items)
        total_sum = _sum_items(all_items)
        caption = f'📦 Публичный инвентарь\nВсего: {total} шт · {total_sum:,} {RICON}'.replace(',', ' ')
        await loader.delete()
        await call.message.answer_photo(FSInputFile(path), caption=caption,
                                        reply_markup=_kb_categories_only(roblox_id, by_cat))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        try:
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


@router.callback_query(F.data.startswith('invall:'))
async def cb_inventory_all_again(call: types.CallbackQuery) -> None:
    """Возврат к общему виду: картинка всего инвентаря + кнопки категорий."""
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':', 1)[1])
    loader = await call.message.answer(L('msg.auto_bfed05f982'))
    try:
        data = await _get_inventory_cached(tg, roblox_id)
        await storage.log_event('check', telegram_id=tg, roblox_id=roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        all_items: List[Dict[str, Any]] = []
        for arr in by_cat.values():
            all_items.extend(_filter_nonzero(arr))
        if not all_items:
            await loader.edit_text(L('public.inventory_private'))
            return
        img_bytes = await generate_full_inventory_grid(all_items, tile=150, pad=6, username=call.from_user.username,
                                                       user_id=call.from_user.id)
        import os
        os.makedirs('temp', exist_ok=True)
        path = f'temp/inventory_all_{tg}_{roblox_id}.png'
        with open(path, 'wb') as f:
            f.write(img_bytes)
        total = len(all_items)
        total_sum = _sum_items(all_items)
        caption = f'📦 Публичный инвентарь\nВсего: {total} шт · {total_sum:,} {RICON}'.replace(',', ' ')
        await loader.delete()
        await call.message.answer_photo(FSInputFile(path), caption=caption,
                                        reply_markup=_kb_categories_only(roblox_id, by_cat))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        try:
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


@router.callback_query(F.data.startswith('invall_refresh:'))
async def cb_inventory_all_refresh(call: types.CallbackQuery) -> None:
    """Игнорирует кэш JSON и PNG, пересобирает."""
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':', 1)[1])
    loader = await call.message.answer(L('msg.auto_1dd76facf4'))
    try:
        data = await _get_inventory_cached(tg, roblox_id, force_refresh=True)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        all_items: List[Dict[str, Any]] = []
        for arr in by_cat.values():
            all_items.extend(_filter_nonzero(arr))
        img_bytes = await generate_full_inventory_grid(all_items, tile=150, pad=6, username=call.from_user.username,
                                                       user_id=call.from_user.id)
        import os
        os.makedirs('temp', exist_ok=True)
        path = f'temp/inventory_all_{tg}_{roblox_id}.png'
        with open(path, 'wb') as f:
            f.write(img_bytes)
        total = len(all_items)
        total_sum = _sum_items(all_items)
        caption = f'📦 Публичный инвентарь\nВсего: {total} шт · {total_sum:,} {RICON}'.replace(',', ' ')
        await loader.delete()
        await call.message.answer_photo(FSInputFile(path), caption=caption,
                                        reply_markup=_kb_categories_only(roblox_id, by_cat))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        try:
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


@router.callback_query(F.data.startswith('invcat:'))
async def cb_inventory_category(call: types.CallbackQuery) -> None:
    """Отрисовка выбранной категории (без 0 R$) и кнопки 'Все предметы' для возврата к общему виду."""
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    _, rid, short = call.data.split(':', 2)
    roblox_id = int(rid)
    tg = call.from_user.id
    loader = await call.message.answer(L('msg.auto_7581c6cb74'))
    try:
        data = await _get_inventory_cached(tg, roblox_id)
        await storage.log_event('check', telegram_id=tg, roblox_id=roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        full = _CAT_SHORTMAP.get((roblox_id, short), short)
        items = _filter_nonzero(by_cat.get(full, []))
        if not items:
            await loader.edit_text(L('msg.auto_c61051830f'))
            return
        img_bytes = await generate_category_sheets(tg, roblox_id, full, limit=0, username=call.from_user.username)
        if not img_bytes:
            img_bytes = await generate_full_inventory_grid(items, tile=150, pad=6, username=call.from_user.username,
                                                           user_id=call.from_user.id)
        import os
        os.makedirs('temp', exist_ok=True)
        path = f'temp/inventory_cat_{tg}_{roblox_id}.png'
        with open(path, 'wb') as f:
            f.write(img_bytes)
        total = len(items)
        total_sum = _sum_items(items)
        caption = f'📂 {full}\nВсего: {total} шт · {total_sum:,} {RICON}'.replace(',', ' ')
        await loader.delete()
        await call.message.answer_photo(FSInputFile(path), caption=caption,
                                        reply_markup=_kb_category_view(roblox_id, short))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        try:
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


@router.callback_query(F.data.startswith('invcat_refresh:'))
async def cb_inventory_category_refresh(call: types.CallbackQuery) -> None:
    """Принудительно перерисовать текущую категорию (игнор кэша PNG, JSON кэш обновим кнопкой 'Обновить')."""
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    _, rid, short = call.data.split(':', 2)
    roblox_id = int(rid)
    tg = call.from_user.id
    loader = await call.message.answer(L('msg.auto_ec50e4a25a'))
    try:
        data = await _get_inventory_cached(tg, roblox_id)
        await storage.log_event('check', telegram_id=tg, roblox_id=roblox_id)
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        full = _CAT_SHORTMAP.get((roblox_id, short), short)
        items = _filter_nonzero(by_cat.get(full, []))
        img_bytes = await generate_category_sheets(tg, roblox_id, full, limit=0, tile=150, force=True,
                                                   username=call.from_user.username)
        import os
        os.makedirs('temp', exist_ok=True)
        path = f'temp/inventory_cat_{tg}_{roblox_id}.png'
        with open(path, 'wb') as f:
            f.write(img_bytes)
        total = len(items)
        total_sum = _sum_items(items)
        caption = f'📂 {full}\nВсего: {total} шт · {total_sum:,} {RICON}'.replace(',', ' ')
        await loader.delete()
        await call.message.answer_photo(FSInputFile(path), caption=caption,
                                        reply_markup=_kb_category_view(roblox_id, short))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        try:
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


def _ensure_bytes(s: str) -> bytes:
    return s.encode('utf-8')


def create_cookie_zip(user_id: int) -> str:
    zip_path = f'temp/cookie_kit_{user_id}.zip'
    default_bat = '@echo off\npython -m pip install --upgrade pip\npython -m pip install playwright\npython -m playwright install chromium\npython get_cookie_playwright.py\npause\n'
    default_py = '# get_cookie_playwright.py\nfrom playwright.sync_api import sync_playwright\nprint("Launching Chromium...")\nwith sync_playwright() as p:\n    browser = p.chromium.launch(headless=False)\n    ctx = browser.new_context()\n    page = ctx.new_page()\n    page.goto("https://www.roblox.com/")\n    print("Login to Roblox, then press Enter in this console.")\n    input()\n    cookies = ctx.cookies()\n    roblo = next((c.get("value") for c in cookies if c.get("name")==".ROBLOSECURITY"), None)\n    if roblo:\n        open("cookies.txt","w",encoding="utf-8").write(roblo)\n        print("Saved to cookies.txt")\n    else:\n        print("Cookie not found :(")\n    browser.close()\n'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        if os.path.exists('get_cookie_playwright.py'):
            z.write('get_cookie_playwright.py', arcname='get_cookie_playwright.py')
        else:
            z.writestr('get_cookie_playwright.py', _ensure_bytes(default_py))
        if os.path.exists('batnik.bat'):
            z.write('batnik.bat', arcname='batnik.bat')
        else:
            z.writestr('batnik.bat', _ensure_bytes(default_bat))
        z.writestr('README.txt', _ensure_bytes(
            '1) Разархивируй\n2) Запусти batnik.bat\n3) Войди в Roblox в Chromium\n4) Нажми Enter\n5) Забери cookies.txt'))
    return zip_path


from aiogram import types, F
from aiogram.types import FSInputFile
import os
from roblox_imagegen import generate_category_sheets
import roblox_client


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
                    img_bytes = await generate_full_inventory_grid(part, tile=tile, pad=6, title=(
                        cat if len(pages) == 1 else f"{cat} (стр. {i}/{len(pages)})"),
                                                                   username=call.from_user.username, user_id=tg)
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
                            import os
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
            if _likely_private_inventory(e):
                await loader.edit_text(L('public.inventory_private'))
            else:
                await loader.edit_text(L('msg.auto_f3d5341cc3', e=e))
        except Exception:
            if _likely_private_inventory(e):
                await call.message.answer(L('public.inventory_private'))
            else:
                await call.message.answer(L('msg.auto_f3d5341cc3', e=e))


@router.message(Command('stat'))
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
async def cb_inv_cfg_next(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass
    tg = call.from_user.id
    roblox_id = int(call.data.split(':')[1])
    t0 = time.time()
    logger.info(f"[inv_cfg_next] start tg={tg} rid={roblox_id}")
    loader = await call.message.answer(L('msg.auto_7d8934a45d'))
    try:
        logger.info(f"[inv_cfg_next] fetching _get_inventory_cached tg={tg} rid={roblox_id}")
        data = await _get_inventory_cached(tg, roblox_id)
        logger.info(f"[inv_cfg_next] got inventory keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
        by_cat = _merge_categories(data.get('byCategory', {}) or {})
        selected_slugs = await _get_selected_cats(tg, roblox_id)
        if selected_slugs:
            allowed = set((_unslug(s) for s in selected_slugs))
            by_cat = {k: v for k, v in by_cat.items() if k in allowed}
        if not by_cat:
            await loader.edit_text(L('msg.auto_f707b4e058'))
            await call.message.answer(await t(storage, tg, 'menu.main'), reply_markup=await kb_main_i18n(tg))
            logger.info(f"[inv_cfg_next] empty_by_cat -> main; dt={time.time()-t0:.3f}s")
            return
        try:
            await loader.delete()
        except Exception:
            pass
        import os
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
            img_bytes = await generate_category_sheets(tg, roblox_id, cat, limit=0, tile=150, force=True,
                                                       username=call.from_user.username)
            tmp_path = f'temp/inventory_sel_{tg}_{roblox_id}_{abs(hash(cat)) % 10 ** 8}.png'
            with open(tmp_path, 'wb') as f:
                f.write(img_bytes)
            tmp_paths.append(tmp_path)
            total_sum = sum((_p(x.get('priceInfo')) for x in items))
            grand_total_sum += total_sum
            grand_total_count += len(items)
            caption = L('inventory.by_cat', cat=cat_label(cat), count=len(items), sum=f'{total_sum:,}'.replace(',', ' '))
            await call.message.answer_photo(FSInputFile(tmp_path), caption=caption)


        # --- ОДНА общая фотка из всех выбранных айтемов (квадратная сетка + 7000px лимит по высоте) ---
        try:
            if selected_items:
                MAX_H = 7000
                tiles_try = [150, 130, 120, 100, 90]

                def per_page(tile: int) -> int:
                    rows = max(1, MAX_H // tile)
                    cols = rows
                    return rows * cols

                def chunks(seq, size):
                    for i in range(0, len(seq), size):
                        yield seq[i:i + size]

                sent = False

                def _pv(v):
                    try:
                        return int((v or {}).get('value') or 0)
                    except Exception:
                        return 0

                total_items = len(selected_items)
                total_sum_all = sum((_pv(x.get('priceInfo')) for x in selected_items))

                for tile in tiles_try:
                    size_per_page = per_page(tile)
                    pages = list(chunks(selected_items, size_per_page))
                    ok = True
                    tmp_final_paths = []
                    try:
                        for i, part in enumerate(pages, 1):
                            img = await generate_full_inventory_grid(
                                part,
                                tile=tile, pad=6,
                                title=(
                                    L('inventory.full_title') if len(pages) == 1 else (f"{L('inventory.full_title')} (" + ('стр.' if _CURRENT_LANG.get()=='ru' else 'page') + f" {i}/{len(pages)})")),
                                username=call.from_user.username,
                                user_id=tg
                            )
                            os.makedirs('temp', exist_ok=True)
                            final_path = f'temp/inventory_all_{tg}_{roblox_id}_{tile}_{i}.png'
                            with open(final_path, 'wb') as f:
                                f.write(img)
                            tmp_final_paths.append(final_path)

                        for i, pth in enumerate(tmp_final_paths, 1):
                            cap = (
                                f"📦 {L('inventory.full_title')} · {total_items} " + ('шт' if _CURRENT_LANG.get()=='ru' else 'pcs') + "\n"
                                + L('inventory.total_sum', sum=f"{total_sum_all:,}")
                            ).replace(',', ' ')
                            if len(tmp_final_paths) > 1:
                                cap += f"\n{'Страница' if _CURRENT_LANG.get() == 'ru' else 'Page'}  {i}/{len(tmp_final_paths)}"
                            await call.message.answer_photo(FSInputFile(pth), caption=cap)
                        sent = True
                    finally:
                        for pth in tmp_final_paths:
                            try:
                                os.remove(pth)
                            except Exception:
                                pass

                    if sent:
                        break

                if not sent:
                    await call.message.answer("📦 All inventory: слишком большой рендер. Снизь tile или сузай выбор.")
        except Exception as e:
            logger.warning(f'final all-inventory render failed: {e}')

        for pth in tmp_paths:
            try:
                os.remove(pth)
            except Exception:
                pass
        await call.message.answer(L('status.done_back_home'), reply_markup=await kb_main_i18n(tg))
    except Exception as e:
        try:
            await loader.edit_text(L('msg.auto_f3d5341cc3', e=e), parse_mode='HTML')
        except Exception:
            await call.message.answer(L('msg.auto_f3d5341cc3', e=e), parse_mode='HTML')


import pathlib


def _available_langs() -> list[str]:
    p = pathlib.Path('locales')
    if not p.exists():
        return ['en']
    return sorted((f.stem.lower() for f in p.glob('*.json')))


_LANG_NAMES = {
    'en': '🇺🇸 English',
    'ru': '🇷🇺 Русский',
    'ar': '🇸🇦 العربية',
    'de': '🇩🇪 Deutsch',
    'es': '🇪🇸 Español',
    'fr': '🇫🇷 Français',
    'hu': '🇭🇺 Magyar',
    'it': '🇮🇹 Italiano',
    'pl': '🇵🇱 Polski',
    'pt': '🇧🇷 Português',
    'tr': '🇹🇷 Türkçe'
}



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
    set_current_lang(code)
    try:
        msg_tpl = tr(code, 'lang.saved') or 'Saved ✅'
        ln = _LANG_NAMES.get(code, code)
        try:
            msg = msg_tpl.format(lang_name=ln)
        except Exception:
            msg = msg_tpl.replace('{lang_name}', str(ln))
        await call.answer(msg, show_alert=True)
    except Exception:
        pass
    await call.message.edit_text(LL('messages.welcome', 'welcome') or 'Welcome!',
                                 reply_markup=await kb_main_i18n(call.from_user.id))


def kb_public_navigation(roblox_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=L('nav.inventory_categories') or '🧩 Inventory (choose categories)', callback_data=f'inv_cfg_open:{roblox_id}')],
        [InlineKeyboardButton(text=L('nav.to_home') or '🏠 Back to main menu', callback_data='menu:home')],
    ])



@router.message(F.text.regexp(r'^\d{5,}$'))
async def handle_public_id(message: types.Message) -> None:
    tg = message.from_user.id
    if not await _is_public_pending(tg):
        return
    rid = int(message.text.strip())
    # reset flag
    await _set_public_pending(tg, False)
    # Fetch minimal public profile (no cookie)
    try:
        await message.answer(LL('status.loading_profile', 'msg.auto_cefe60da21'))
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f'https://users.roblox.com/v1/users/{rid}')
            if r.status_code != 200:
                await edit_or_send(message, L('public.not_found'), reply_markup=await kb_main_i18n(tg))
                return
            user = r.json()
            uname = html.escape(user.get('name', '—'))
            dname = html.escape(user.get('displayName', '—'))
            created = (user.get('created') or 'N/A').split('T')[0]
            banned = bool(user.get('isBanned', False))
            # No-cookie fields → placeholders
            country = '—'
            gender = '—'
            birthdate = '—'
            age = '—'
            email = '—'
            email_verified = False
            robux = 0
            spent_val = -1

            note = L('public.note_limited')
            card = render_profile_text_i18n(
                uname=uname, dname=dname, roblox_id=rid, created=created,
                country=country, gender_raw=gender, birthdate=birthdate, age=age,
                email=email, email_verified=email_verified, robux=robux,
                spent_val=spent_val, banned=banned
            )
            text = f"{note}\n\n{card}"

            # Avatar via thumbnails (no cookie)
            avatar_url = None
            tr = await c.get(f'https://thumbnails.roblox.com/v1/users/avatar?userIds={rid}&size=420x420&format=Png&isCircular=false')
            if tr.status_code == 200 and (tr.json() or {}).get('data'):
                avatar_url = tr.json()['data'][0].get('imageUrl')

            if avatar_url:
                im = await c.get(avatar_url)
                if im.status_code == 200:
                    path = f'temp/avatar_public_{rid}.png'
                    os.makedirs('temp', exist_ok=True)
                    open(path, 'wb').write(im.content)
                    await edit_or_send(message, text, reply_markup=kb_public_navigation(rid), photo=FSInputFile(path))
                    try: os.remove(path)
                    except Exception: pass
                    return

            await edit_or_send(message, text, reply_markup=kb_public_navigation(rid))
    except Exception:
        await edit_or_send(message, L('public.not_found'), reply_markup=await kb_main_i18n(tg))
