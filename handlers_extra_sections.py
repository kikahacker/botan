import logging
import time
from typing import Optional, List, Dict, Any

from aiogram import Router, types, F
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto,
)

from handlers import edit_or_send, L, LL
from services_collectibles_pipeline import (
    collectibles_with_rap,
    offsale_collectibles,
)
from services_roblox_extra import get_revenue, get_past_usernames

import storage
from util.crypto import decrypt_text

router = Router()
log = logging.getLogger("handlers_extra_sections")


def _rid(data: str) -> Optional[int]:
    try:
        parts = data.split(":")
        return int(parts[1]) if len(parts) >= 2 else None
    except Exception:
        return None


async def _enc_cookie(tg_id: int, rid: int) -> Optional[str]:
    """
    Вернуть зашифрованную cookie из storage для (tg_id, rid).
    Тут storage — модуль, а не объект inside.
    """
    try:
        return await storage.get_encrypted_cookie(tg_id, rid)
    except Exception as e:
        log.warning(f"_enc_cookie failed tg={tg_id} rid={rid}: {e}")
        return None


async def _cookie(tg_id: int, rid: int) -> Optional[str]:
    """
    Расшифрованная cookie для (tg_id, rid).
    """
    try:
        enc = await storage.get_encrypted_cookie(tg_id, rid)
        return decrypt_text(enc) if enc else None
    except Exception as e:
        log.warning(f"_cookie failed tg={tg_id} rid={rid}: {e}")
        return None


# ===== simple cache for full revenue =====

_REVENUE_CACHE: dict[tuple[int, int], dict[str, Any]] = {}
_REVENUE_CACHE_TTL = 300  # seconds
REVENUE_ROWS_PER_PAGE = 15  # UI page size


async def _get_full_revenue(tg_id: int, rid: int, enc_cookie: str) -> Dict[str, Any]:
    """
    Тянем все страницы revenue через get_revenue и кэшируем по (tg_id, rid).
    Возвращаем dict {"rows": [...]} или {"error": "auth_required"}.
    """
    key = (int(tg_id), int(rid))
    now = time.time()
    cached = _REVENUE_CACHE.get(key)
    if cached and now - cached.get("ts", 0) < _REVENUE_CACHE_TTL:
        return {"rows": cached.get("rows", [])}

    all_rows: List[Dict[str, Any]] = []
    page = 1
    while True:
        data = await get_revenue(rid, enc_cookie=enc_cookie, page=page, per_page=100)
        if (data or {}).get("error") == "auth_required":
            return {"error": "auth_required"}

        rows = (data or {}).get("rows") or []
        all_rows.extend(rows)

        if not (data or {}).get("has_next"):
            break
        page += 1
        if page > 50:
            # safety limit
            break

    _REVENUE_CACHE[key] = {"rows": all_rows, "ts": now}
    return {"rows": all_rows}



# ===== simple cache for RAP result =====

_RAP_CACHE: dict[tuple[int, int], dict[str, Any]] = {}
_RAP_CACHE_TTL = 300  # seconds
RAP_ROWS_PER_PAGE = 15  # items per UI page


async def _get_full_rap(tg_id: int, rid: int, cookie: Optional[str]) -> Dict[str, Any]:
    """
    Кэшируем результат collectibles_with_rap по (tg_id, rid).
    Возвращает dict {"items": [...], "total": int, "image_path": Optional[str]}.
    """
    key = (int(tg_id), int(rid))
    now = time.time()
    cached = _RAP_CACHE.get(key)
    if cached and now - cached.get("ts", 0) < _RAP_CACHE_TTL:
        return {
            "items": cached.get("items", []),
            "total": int(cached.get("total") or 0),
            "image_path": cached.get("image_path"),
        }

    data = await collectibles_with_rap(rid, cookie)
    items = (data or {}).get("items") or []
    total = int((data or {}).get("total") or 0)
    image_path = (data or {}).get("image_path")
    _RAP_CACHE[key] = {"items": items, "total": total, "image_path": image_path, "ts": now}
    return {"items": items, "total": total, "image_path": image_path}


@router.callback_query(F.data == "noop")
async def cb_noop(call: types.CallbackQuery):
    try:
        await call.answer(cache_time=1)
    except Exception:
        pass


# ======================= RAP =======================


@router.callback_query(F.data.startswith("rap:"))
async def cb_rap(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[RAP] start rid={rid}")
    msg = await edit_or_send(call.message, f"📈 {L('rap.title')}\n{L('rap.loading')}")
    try:
        cookie = await _cookie(call.from_user.id, rid)
        data = await _get_full_rap(call.from_user.id, rid, cookie)
        items = (data or {}).get("items") or []
        total = int((data or {}).get("total") or 0)

        if items:
            txt = (
                f"📈 {L('rap.title')}\n"
                f"{L('rap.total', value=total)}\n"
                f"{L('inventory.total_items', count=len(items))}"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=L('rap.details'),
                            callback_data=f"rapd:{rid}:1",
                        )
                    ]
                ]
            )
            await edit_or_send(msg, txt, reply_markup=kb)

            img = (data or {}).get("image_path")
            if img:
                try:
                    media = InputMediaPhoto(media=FSInputFile(img), caption=txt)
                    await call.message.edit_media(media)
                except Exception as e:
                    log.warning(f"[RAP] edit_media fail: {e}")

            log.debug(f"[RAP] ok rid={rid} items={len(items)} total={total} dt={time.time()-t0:.3f}s")
        else:
            await edit_or_send(
                msg,
                f"📈 {L('rap.title')}\n{LL('rap.empty_generic', 'rap.no_items')}",
            )
            log.debug(f"[RAP] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[RAP] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"📈 {L('rap.title')}\n" + L('errors.generic', err=str(e)),
        )
    log.debug(f"[RAP] end rid={rid}")


@router.callback_query(F.data.startswith("rapd:"))
async def cb_rap_details(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    t0 = time.time()
    parts = call.data.split(":")
    try:
        rid = int(parts[1])
    except Exception:
        rid = None
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        page = 1

    log.debug(f"[RAP_DETAILS] start rid={rid} page={page}")
    msg = await edit_or_send(call.message, f"📈 {L('rap.title')}\n{L('rap.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[RAP_DETAILS] no rid dt={time.time()-t0:.3f}s")
            return

        cookie = await _cookie(call.from_user.id, rid)
        data = await _get_full_rap(call.from_user.id, rid, cookie)
        items = (data or {}).get("items") or []

        if not items:
            await edit_or_send(
                msg,
                f"📈 {L('rap.title')}\n{LL('rap.empty_generic', 'rap.no_items')}",
            )
            log.debug(f"[RAP_DETAILS] empty rid={rid} dt={time.time()-t0:.3f}s")
            return

        per_page = RAP_ROWS_PER_PAGE
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * per_page
        page_items = items[start_idx : start_idx + per_page]

        lines = [
            f"📈 {L('rap.title')} — "
            + L('games.page', cur=page, total=total_pages)
        ]
        for it in page_items:
            name = it.get("name") or it.get("assetName") or "-"
            rap_value = int(it.get("rap") or 0)
            aid = it.get("assetId") or it.get("id") or it.get("asset_id") or 0
            lines.append(
                L(
                    'rap.item_row_with_id',
                    name=name,
                    id=aid,
                    rap=rap_value,
                )
            )

        txt = "\n".join(lines)

        kb_rows = []
        nav_row = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=L('games.prev'),
                    callback_data=f"rapd:{rid}:{page-1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=L('games.page', cur=page, total=total_pages),
                callback_data="noop",
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=L('games.next'),
                    callback_data=f"rapd:{rid}:{page+1}",
                )
            )
        if nav_row:
            kb_rows.append(nav_row)

        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=L('rap.back_to_summary'),
                    callback_data=f"rap:{rid}",
                )
            ]
        )

        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await edit_or_send(msg, txt, reply_markup=kb)
        log.debug(
            f"[RAP_DETAILS] ok rid={rid} page={page}/{total_pages} rows={len(page_items)} dt={time.time()-t0:.3f}s"
        )
    except Exception as e:
        log.exception(f"[RAP_DETAILS] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"📈 {L('rap.title')}\n" + L('errors.generic', err=str(e)),
        )
    log.debug(f"[RAP_DETAILS] end rid={rid}")
# ======================= OFFSALE =======================

@router.callback_query(F.data.startswith("offsale:"))
async def cb_offsale(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[OFFSALE] start rid={rid}")
    msg = await edit_or_send(call.message, f"🛑 {L('offsale.title')}\n{L('offsale.loading')}")
    try:
        cookie = await _cookie(call.from_user.id, rid)
        data = await offsale_collectibles(rid, cookie)
        rows = (data or {}).get("items") or []
        if rows:
            txt = f"🛑 {L('offsale.title')}\n{L('common.total')}: {len(rows)}"
            await edit_or_send(msg, txt)
            img = (data or {}).get("image_path")
            if img:
                try:
                    media = InputMediaPhoto(media=FSInputFile(img), caption=txt)
                    await call.message.edit_media(media)
                except Exception as e:
                    log.warning(f"[OFFSALE] edit_media fail: {e}")
            log.debug(f"[OFFSALE] ok rid={rid} count={len(rows)} dt={time.time()-t0:.3f}s")
        else:
            await edit_or_send(msg, f"🛑 {L('offsale.title')}\n{LL('offsale.empty_generic', 'offsale.empty')}")
            log.debug(f"[OFFSALE] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[OFFSALE] error rid={rid}: {e}")
        await edit_or_send(msg, f"🛑 {L('offsale.title')}\n" + L('errors.generic', err=e))
    log.debug(f"[OFFSALE] end rid={rid}")


# ======================= REVENUE SUMMARY =======================

@router.callback_query(F.data.startswith("revenue:"))
async def cb_revenue(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[REVENUE] start rid={rid}")
    msg = await edit_or_send(call.message, f"{L('revenue.title')}\n{L('revenue.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[REVENUE] no rid dt={time.time()-t0:.3f}s")
            return

        enc = await _enc_cookie(call.from_user.id, rid)
        if not enc:
            await edit_or_send(msg, L('revenue.auth_required'))
            log.debug(f"[REVENUE] no enc_cookie rid={rid} dt={time.time()-t0:.3f}s")
            return

        all_data = await _get_full_revenue(call.from_user.id, rid, enc)
        if (all_data or {}).get("error") == "auth_required":
            await edit_or_send(msg, L('revenue.auth_required'))
            log.debug(f"[REVENUE] auth_required rid={rid} dt={time.time()-t0:.3f}s")
            return

        all_rows = (all_data or {}).get("rows") or []
        if not all_rows:
            await edit_or_send(msg, f"{L('revenue.title')}\n" + L('revenue.empty'))
            log.debug(f"[REVENUE] empty rid={rid} dt={time.time()-t0:.3f}s")
            return

        total_count = len(all_rows)
        total_sum = sum(int(x.get("raw_amount") or x.get("amount") or 0) for x in all_rows)

        txt = (
            f"{L('revenue.title')}\n"
            + L('revenue.total_count', count=total_count) + "\n"
            + L('revenue.total_sum', sum=total_sum)
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=L('revenue.details'),
                        callback_data=f"revd:{rid}:1",
                    )
                ]
            ]
        )
        await edit_or_send(msg, txt, reply_markup=kb)
        log.debug(f"[REVENUE] ok rid={rid} items={len(all_rows)} total_sum={total_sum} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[REVENUE] error rid={rid}: {e}")
        await edit_or_send(msg, f"{L('revenue.title')}\n" + L('errors.generic', err=e))
    log.debug(f"[REVENUE] end rid={rid}")


# ======================= REVENUE DETAILS (PAGINATED) =======================

@router.callback_query(F.data.startswith("revd:"))
async def cb_revenue_details(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    t0 = time.time()
    parts = call.data.split(":")
    try:
        rid = int(parts[1])
    except Exception:
        rid = None
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        page = 1

    log.debug(f"[REVENUE_DETAILS] start rid={rid} page={page}")
    msg = await edit_or_send(call.message, f"{L('revenue.title')}\n{L('revenue.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[REVENUE_DETAILS] no rid dt={time.time()-t0:.3f}s")
            return

        enc = await _enc_cookie(call.from_user.id, rid)
        if not enc:
            await edit_or_send(msg, L('revenue.auth_required'))
            log.debug(f"[REVENUE_DETAILS] no enc_cookie rid={rid} dt={time.time()-t0:.3f}s")
            return

        all_data = await _get_full_revenue(call.from_user.id, rid, enc)
        if (all_data or {}).get("error") == "auth_required":
            await edit_or_send(msg, L('revenue.auth_required'))
            log.debug(f"[REVENUE_DETAILS] auth_required rid={rid} dt={time.time()-t0:.3f}s")
            return

        all_rows = (all_data or {}).get("rows") or []
        if not all_rows:
            await edit_or_send(msg, f"{L('revenue.title')}\n" + L('revenue.empty'))
            log.debug(f"[REVENUE_DETAILS] empty rid={rid} dt={time.time()-t0:.3f}s")
            return

        rows_per_page = REVENUE_ROWS_PER_PAGE
        total_pages = max(1, (len(all_rows) + rows_per_page - 1) // rows_per_page)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * rows_per_page
        rows = all_rows[start_idx : start_idx + rows_per_page]

        lines = [f"{L('revenue.title')} — " + L('games.page', cur=page, total=total_pages)]
        for it in rows:
            amt = int(it.get("raw_amount") or it.get("amount") or 0)
            src = it.get("source") or "-"
            dt_str = (it.get("date") or "")[:19]
            typ = it.get("type") or ""
            line = f"{dt_str} — {typ} — {amt} R$ — {src}".strip()
            lines.append(line)

        txt = "\n".join(lines)

        kb_rows = []
        nav_row = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ Prev",
                    callback_data=f"revd:{rid}:{page-1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=L('games.page', cur=page, total=total_pages),
                callback_data="noop",
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text="Next ➡️",
                    callback_data=f"revd:{rid}:{page+1}",
                )
            )
        if nav_row:
            kb_rows.append(nav_row)

        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=L('revenue.back_to_summary'),
                    callback_data=f"revenue:{rid}",
                )
            ]
        )

        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await edit_or_send(msg, txt, reply_markup=kb)
        log.debug(f"[REVENUE_DETAILS] ok rid={rid} page={page}/{total_pages} rows={len(rows)} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[REVENUE_DETAILS] error rid={rid}: {e}")
        await edit_or_send(msg, f"{L('revenue.title')}\n" + L('errors.generic', err=e))
    log.debug(f"[REVENUE_DETAILS] end rid={rid}")


# ======================= PAST USERNAMES =======================

@router.callback_query(F.data.startswith("usernames:"))
async def cb_usernames(call: types.CallbackQuery):
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[USERNAMES] start rid={rid}")
    msg = await edit_or_send(call.message, f"{L('usernames.title')}\n{L('usernames.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[USERNAMES] no rid dt={time.time()-t0:.3f}s")
            return

        data = await get_past_usernames(rid, page=1, per_page=25)
        rows = (data or {}).get("rows") or []

        if rows:
            lines = ["📜 Past usernames:"]
            for it in rows[:25]:
                name = it.get("name") or it.get("username") or "-"
                dt = (it.get("created") or it.get("createdAt") or "")[:10]
                if dt:
                    lines.append(L('usernames.row', name=name, changedAt=dt))
                else:
                    lines.append(L('usernames.row', name=name, changedAt=L('common.na')))
            txt = "\n".join(lines)
            await edit_or_send(msg, txt)
            log.debug(f"[USERNAMES] ok rid={rid} count={len(rows)} dt={time.time()-t0:.3f}s")
        else:
            await edit_or_send(msg, f"{L('usernames.title')}\n" + LL('usernames.empty_generic', 'usernames.empty'))
            log.debug(f"[USERNAMES] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[USERNAMES] error rid={rid}: {e}")
        await edit_or_send(msg, f"{L('usernames.title')}\n" + L('errors.generic', err=e))
    log.debug(f"[USERNAMES] end rid={rid}")
@router.callback_query(F.data.startswith("pub_rap:"))
async def cb_pub_rap(call: types.CallbackQuery):
    """Public RAP summary (no personal cookie; may use public endpoints only)."""
    await call.answer(cache_time=1)
    parts = call.data.split(":")
    try:
        rid = int(parts[1])
    except Exception:
        rid = None
    t0 = time.time()
    log.debug(f"[PUB_RAP] start rid={rid}")
    msg = await edit_or_send(call.message, f"📈 {L('rap.title')}{L('rap.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[PUB_RAP] no rid dt={time.time()-t0:.3f}s")
            return

        # public mode: no user-bound cookie
        data = await _get_full_rap(call.from_user.id, rid, cookie=None)
        items = (data or {}).get("items") or []
        total = int((data or {}).get("total") or 0)

        if items:
            txt = (
                f"📈 {L('rap.title')}"
                f"{L('rap.total', value=total)}\n"
                f"{L('inventory.total_items', count=len(items))}"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=L('rap.details'),
                            callback_data=f"pub_rapd:{rid}:1",
                        )
                    ]
                ]
            )
            await edit_or_send(msg, txt, reply_markup=kb)
            log.debug(f"[PUB_RAP] ok rid={rid} items={len(items)} total={total} dt={time.time()-t0:.3f}s")
        else:
            # либо реально нет collectibles, либо недоступно без куки
            await edit_or_send(
                msg,
                f"📈 {L('rap.title')}{LL('rap.empty_generic', 'rap.no_items')}",
            )
            log.debug(f"[PUB_RAP] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[PUB_RAP] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"📈 {L('rap.title')}" + L('errors.generic', err=str(e)),
        )
    log.debug(f"[PUB_RAP] end rid={rid}")


@router.callback_query(F.data.startswith("pub_rapd:"))
async def cb_pub_rap_details(call: types.CallbackQuery):
    """Public RAP details with pagination."""
    await call.answer(cache_time=1)
    t0 = time.time()
    parts = call.data.split(":")
    try:
        rid = int(parts[1])
    except Exception:
        rid = None
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        page = 1

    log.debug(f"[PUB_RAP_DETAILS] start rid={rid} page={page}")
    msg = await edit_or_send(call.message, f"📈 {L('rap.title')}{L('rap.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[PUB_RAP_DETAILS] no rid dt={time.time()-t0:.3f}s")
            return

        data = await _get_full_rap(call.from_user.id, rid, cookie=None)
        items = (data or {}).get("items") or []

        if not items:
            await edit_or_send(
                msg,
                f"📈 {L('rap.title')}{LL('rap.empty_generic', 'rap.no_items')}",
            )
            log.debug(f"[PUB_RAP_DETAILS] empty rid={rid} dt={time.time()-t0:.3f}s")
            return

        per_page = RAP_ROWS_PER_PAGE
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * per_page
        page_items = items[start_idx : start_idx + per_page]

        lines = [
            f"📈 {L('rap.title')} — " + L('games.page', cur=page, total=total_pages)
        ]
        for it in page_items:
            name = it.get("name") or it.get("assetName") or "-"
            rap_value = int(it.get("rap") or 0)
            aid = it.get("assetId") or it.get("id") or it.get("asset_id") or 0
            lines.append(
                L(
                    'rap.item_row_with_id',
                    name=name,
                    id=aid,
                    rap=rap_value,
                )
            )

        txt = "\n".join(lines)

        kb_rows: List[List[InlineKeyboardButton]] = []
        nav_row: List[InlineKeyboardButton] = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=L('games.prev'),
                    callback_data=f"pub_rapd:{rid}:{page-1}",
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=L('games.page', cur=page, total=total_pages),
                callback_data="noop",
            )
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=L('games.next'),
                    callback_data=f"pub_rapd:{rid}:{page+1}",
                )
            )
        if nav_row:
            kb_rows.append(nav_row)

        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=L('rap.back_to_summary'),
                    callback_data=f"pub_rap:{rid}:1",
                )
            ]
        )

        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await edit_or_send(msg, txt, reply_markup=kb)
        log.debug(
            f"[PUB_RAP_DETAILS] ok rid={rid} page={page}/{total_pages} rows={len(page_items)} dt={time.time()-t0:.3f}s"
        )
    except Exception as e:
        log.exception(f"[PUB_RAP_DETAILS] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"📈 {L('rap.title')}" + L('errors.generic', err=str(e)),
        )
    log.debug(f"[PUB_RAP_DETAILS] end rid={rid}")


@router.callback_query(F.data.startswith("pub_offsale:"))
async def cb_pub_offsale(call: types.CallbackQuery):
    """Public off-sale collectibles summary."""
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[PUB_OFFSALE] start rid={rid}")
    msg = await edit_or_send(call.message, f"🛑 {L('offsale.title')}{L('offsale.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[PUB_OFFSALE] no rid dt={time.time()-t0:.3f}s")
            return

        data = await offsale_collectibles(rid, cookie=None)
        rows = (data or {}).get("items") or []
        if rows:
            txt = f"🛑 {L('offsale.title')}\n{L('common.total')}: {len(rows)}"
            await edit_or_send(msg, txt)
            log.debug(f"[PUB_OFFSALE] ok rid={rid} count={len(rows)} dt={time.time()-t0:.3f}s")
        else:
            await edit_or_send(
                msg,
                f"🛑 {L('offsale.title')}\n{LL('offsale.empty_generic', 'offsale.empty')}",
            )
            log.debug(f"[PUB_OFFSALE] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[PUB_OFFSALE] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"🛑 {L('offsale.title')}" + L('errors.generic', err=e),
        )
    log.debug(f"[PUB_OFFSALE] end rid={rid}")


@router.callback_query(F.data.startswith("pub_revenue:"))
async def cb_pub_revenue(call: types.CallbackQuery):
    """Public revenue is not available without linking a cookie."""
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[PUB_REVENUE] start rid={rid}")
    msg = await edit_or_send(call.message, f"{L('revenue.title')}")
    try:
        # Revenue is strictly private, requires per-account cookie
        await edit_or_send(msg, L('revenue.auth_required'))
        log.debug(f"[PUB_REVENUE] auth_required rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[PUB_REVENUE] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"{L('revenue.title')}" + L('errors.generic', err=e),
        )
    log.debug(f"[PUB_REVENUE] end rid={rid}")


@router.callback_query(F.data.startswith("pub_usernames:"))
async def cb_pub_usernames(call: types.CallbackQuery):
    """Public view of past usernames (does not require cookie)."""
    await call.answer(cache_time=1)
    rid = _rid(call.data)
    t0 = time.time()
    log.debug(f"[PUB_USERNAMES] start rid={rid}")
    msg = await edit_or_send(call.message, f"{L('usernames.title')}{L('usernames.loading')}")
    try:
        if rid is None:
            await edit_or_send(msg, L('errors.bad_callback'))
            log.debug(f"[PUB_USERNAMES] no rid dt={time.time()-t0:.3f}s")
            return

        data = await get_past_usernames(rid, page=1, per_page=25)
        rows = (data or {}).get("rows") or []

        if rows:
            lines = [LL('usernames.header', '📜 Past usernames:')]
            for it in rows[:25]:
                name = it.get("name") or it.get("username") or "-"
                dt = (it.get("created") or it.get("createdAt") or "")[:10]
                if dt:
                    lines.append(L('usernames.row', name=name, changedAt=dt))
                else:
                    lines.append(L('usernames.row', name=name, changedAt=L('common.na')))
            txt = "\n".join(lines)
            await edit_or_send(msg, txt)
            log.debug(f"[PUB_USERNAMES] ok rid={rid} count={len(rows)} dt={time.time()-t0:.3f}s")
        else:
            await edit_or_send(
                msg,
                f"{L('usernames.title')}" + LL('usernames.empty_generic', 'usernames.empty'),
            )
            log.debug(f"[PUB_USERNAMES] empty rid={rid} dt={time.time()-t0:.3f}s")
    except Exception as e:
        log.exception(f"[PUB_USERNAMES] error rid={rid}: {e}")
        await edit_or_send(
            msg,
            f"{L('usernames.title')}" + L('errors.generic', err=e),
        )
    log.debug(f"[PUB_USERNAMES] end rid={rid}")
