
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any, List, Optional

import httpx
import cache
from util.crypto import decrypt_text

# Reuse lower-level client helpers when available
try:
    from http_shared import get_client, PROXY_POOL  # your common httpx client + proxy pool
except Exception:  # fallback: plain client without proxy
    PROXY_POOL = None
    async def get_client(_proxy=None):
        return httpx.AsyncClient(http2=True, timeout=httpx.Timeout(10.0, connect=2.0, read=8.0))

# direct roblox_client fallbacks (used for RAP/off-sale/past usernames/games)
try:
    from roblox_client import (
        calc_user_rap as _rc_calc_user_rap,
        get_offsale_collectibles as _rc_get_offsale_collectibles,
        get_username_history as _rc_get_username_history,
        get_favorite_games as _rc_get_favorite_games,
    )
except Exception:
    _rc_calc_user_rap = _rc_get_offsale_collectibles = _rc_get_username_history = _rc_get_favorite_games = None

logger = logging.getLogger('services.extra')

def _coerce_amount(v):
    if isinstance(v, dict):
        # common shapes: {'amount': 123} or {'value': 123}
        v = v.get('amount') or v.get('value')
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return 0

ECON_TX_URL = "https://economy.roblox.com/v2/users/{uid}/transactions"

# ---------------------------- helpers ----------------------------

def _cookie_headers(cookie: str) -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.roblox.com/",
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Accept": "application/json, text/plain, */*",
    }

def _quantize_limit(per_page: int | str | None) -> int:
    allowed = (10, 25, 50, 100)
    try:
        pp = int(per_page) if per_page is not None else 25
    except Exception:
        pp = 25
    return min(allowed, key=lambda v: abs(v - pp))

# ---------------------------- RAP ----------------------------

async def get_collectibles_with_rap(uid: int, cookie: Optional[str] = None) -> Dict[str, Any]:
    """Return dict with 'items' from roblox_client.calc_user_rap if available; else empty list."""
    logger.debug("get_collectibles_with_rap uid=%s", uid)
    try:
        if _rc_calc_user_rap:
            data = await _rc_calc_user_rap(int(uid), cookie=cookie)
            # normalize
            items = (data or {}).get('items') or data or []
            return {"items": items}
    except Exception as e:
        logger.exception("collectibles_with_rap failed: %s", e)
    return {"items": []}

# ---------------------------- Off-sale ----------------------------

async def get_offsale_items(uid: int, cookie: Optional[str] = None) -> Dict[str, Any]:
    """Return dict with 'items' (collectibles that are off-sale)."""
    logger.debug("get_offsale_items uid=%s", uid)
    try:
        if _rc_get_offsale_collectibles:
            items = await _rc_get_offsale_collectibles(int(uid), cookie=cookie)
            # normalize to dict
            if isinstance(items, dict) and "items" in items:
                return items
            return {"items": items or []}
    except Exception as e:
        logger.exception("offsale_items failed: %s", e)
    return {"items": []}

# ---------------------------- Past usernames ----------------------------

async def get_past_usernames(uid: int, page: int = 1, per_page: int = 15) -> Dict[str, Any]:
    """Wrap roblox_client.get_username_history and paginate in-process if needed."""
    logger.debug("get_past_usernames uid=%s page=%s per_page=%s", uid, page, per_page)
    try:
        rows = await _rc_get_username_history(int(uid), limit=300) if _rc_get_username_history else []
    except Exception as e:
        logger.exception("usernames page failed: %s", e)
        rows = []

    rows = rows or []
    # simple pagination
    pp = max(1, int(per_page))
    total_pages = max(1, (len(rows) + pp - 1) // pp)
    page = max(1, min(int(page), total_pages))
    start = (page - 1) * pp
    out = rows[start:start + pp]
    # cache page quickly (positional ttl to be compatible with your cache API)
    try:
        ck = f"usernames:{int(uid)}:p{page}:pp{pp}"
        await cache.set_json(ck, out, 24 * 60 * 60)
    except Exception:
        pass
    return {
        "rows": out,
        "page": page,
        "pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }

# ---------------------------- Revenue (Sales) ----------------------------

async def _fetch_revenue_page(uid: int, cookie: str, cursor: Optional[str], per_page: int) -> Dict[str, Any]:
    """Fetch one Economy 'Sale' page. Returns dict {rows, next_cursor} or raises on fatal auth."""
    proxy = PROXY_POOL.any() if PROXY_POOL else None
    client = await get_client(proxy)
    limit = _quantize_limit(per_page)
    params = {"transactionType": "Sale", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    try:
        r = await client.get(
            ECON_TX_URL.format(uid=int(uid)),
            params=params,
            headers=_cookie_headers(cookie),
            timeout=httpx.Timeout(8.0, connect=2.0, read=6.0),
        )
        if r.status_code in (401, 403):
            # auth issues
            raise PermissionError(f"auth {r.status_code}")
        r.raise_for_status()
        js = r.json() or {}
        data = js.get("data") or []
        rows = []
        for it in data:
            # normalize row
            rows.append({
                "id": it.get("id"),
                "type": it.get("type") or it.get("transactionType") or "",
                "amount": (_coerce_amount(it.get("currency")) or _coerce_amount(it.get("robux")) or _coerce_amount(it.get("amount"))),
                "raw_amount": (_coerce_amount(it.get("currency")) or _coerce_amount(it.get("robux")) or _coerce_amount(it.get("amount"))),
                "date": it.get("created") or it.get("date") or "",
                "source": it.get("details", {}).get("name") if isinstance(it.get("details"), dict) else (it.get("name") or ""),
            })
        return {
            "rows": rows,
            "next": js.get("nextPageCursor") or None,
        }
    except PermissionError:
        raise
    except Exception as e:
        logger.exception("revenue_page failed: %s", e)
        return {"rows": [], "next": None}

async def get_revenue(uid: int, enc_cookie: str, page: int = 1, per_page: int = 25) -> Dict[str, Any]:
    """
    Paged revenue (Sales) view.
    Returns dict with keys: rows, page, pages, has_prev, has_next, summary_sum
    On missing/invalid cookie -> {"error": "auth_required"}.
    """
    logger.debug("get_revenue uid=%s page=%s per_page=%s", uid, page, per_page)
    # decrypt cookie
    try:
        cookie = decrypt_text(enc_cookie)
    except Exception as e:
        logger.error("decrypt cookie failed: %s", e)
        return {"error": "auth_required"}

    if not cookie:
        return {"error": "auth_required"}

    # cursor paging via cache
    pp = _quantize_limit(per_page)
    page = max(1, int(page))
    cursor_key = f"rev:cursor:{int(uid)}:pp{pp}"
    cursors: List[Optional[str]] = [None]  # cursor[1] is for page=1 start
    try:
        saved = await cache.get_json(cursor_key, 60*60)
        if isinstance(saved, list) and saved:
            cursors = saved
    except Exception:
        pass

    # ensure cursors list long enough
    while len(cursors) <= page:
        cursors.append(None)

    # walk pages until desired page, saving cursors along the way
    cur = cursors[page-1]
    if page == 1:
        cur = None

    summary_sum = 0
    rows: List[Dict[str, Any]] = []

    try:
        resp = await _fetch_revenue_page(uid, cookie, cur, pp)
        rows = resp.get("rows", [])
        nxt = resp.get("next")
        # store cursor for next page index
        if len(cursors) <= page:
            cursors.extend([None] * (page - len(cursors) + 1))
        cursors[page] = nxt
        try:
            await cache.set_json(cursor_key, cursors, 60*60)  # positional ttl arg
        except Exception:
            pass

        # we can't know total pages without traversing, so infer has_next by presence of next cursor
        has_next = bool(nxt)
        has_prev = page > 1
        # compute summary sum for shown page (if нужно all-time — понадобился бы проход)
        summary_sum = sum(int(x.get("raw_amount", 0)) for x in rows)

        return {
            "rows": rows,
            "page": page,
            "pages": page + (1 if has_next else 0),
            "has_prev": has_prev,
            "has_next": has_next,
            "summary_sum": summary_sum,
        }
    except PermissionError:
        return {"error": "auth_required"}
    except Exception as e:
        logger.exception("get_revenue failed: %s", e)
        return {"rows": [], "page": page, "pages": page, "has_prev": page > 1, "has_next": False, "summary_sum": 0}

# ---------------------------- Favorite games passthrough ----------------------------

async def get_favorite_games(uid: int) -> List[Dict[str, Any]]:
    logger.debug("get_favorite_games uid=%s", uid)
    try:
        if _rc_get_favorite_games:
            return await _rc_get_favorite_games(int(uid))
    except Exception as e:
        logger.exception("favorite_games failed: %s", e)
    return []


# ====== INVENTORY + CATALOG + RESALE implementation (self-contained) ======

CATALOG_DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"
INV_URL = "https://inventory.roblox.com/v2/users/{uid}/inventory/{asset_type}"
RESALE_URL = "https://economy.roblox.com/v1/assets/{aid}/resale-data"

# Common wearable/collectible-capable types (fallback)
_DEFAULT_TYPES = [2,3,8,11,12,17,18,19,24,27,41,42,43,44,45,46,47,48,49,50,51,61]

def _is_collectible_detail(d: Dict[str, Any]) -> bool:
    """Treat as collectible ONLY when itemRestrictions explicitly contain 'Collectible'."""
    if not isinstance(d, dict):
        return False
    ir = d.get("itemRestrictions") or []
    if not isinstance(ir, list):
        return False
    try:
        irs = [str(x).strip().lower() for x in ir]
    except Exception:
        return False
    return any(x == "collectible" for x in irs)

async def _http_get_json(url: str, *, params: Dict[str, Any] | None = None, headers: Dict[str,str] | None = None, timeout: float = 8.0):
    proxy = PROXY_POOL.any() if PROXY_POOL else None
    client = await get_client(proxy)
    try:
        r = await client.get(url, params=params, headers=headers or {}, timeout=httpx.Timeout(timeout, connect=2.0, read=timeout-1.5))
        if r.status_code != 200:
            logger.debug("[svc.http] %s -> %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.debug("[svc.http] fail %s: %s", url, e)
        return None

async def _http_post_json(url: str, payload: Dict[str, Any], *, headers: Dict[str,str] | None = None, timeout: float = 10.0):
    proxy = PROXY_POOL.any() if PROXY_POOL else None
    client = await get_client(proxy)
    try:
        r = await client.post(url, json=payload, headers=headers or {}, timeout=httpx.Timeout(timeout, connect=2.0, read=timeout-1.5))
        if r.status_code != 200:
            logger.debug("[svc.http] POST %s -> %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.debug("[svc.http] post fail %s: %s", url, e)
        return None

async def _fetch_inventory_asset_ids(uid: int, *, cookie: Optional[str]) -> List[int]:
    """Collect all assetIds from inventory v2 across a set of asset types."""
    headers = _cookie_headers(cookie) if cookie else {"User-Agent": "Mozilla/5.0"}
    asset_ids: List[int] = []
    types = _DEFAULT_TYPES
    for t in types:
        cursor = None
        rounds = 0
        while rounds < 40:
            rounds += 1
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            js = await _http_get_json(INV_URL.format(uid=int(uid), asset_type=t), params=params, headers=headers)
            if not js or "data" not in js:
                break
            for it in js.get("data", []) or []:
                aid = it.get("assetId") or it.get("id")
                try:
                    aid = int(aid)
                    asset_ids.append(aid)
                except Exception:
                    continue
            cursor = js.get("nextPageCursor")
            if not cursor:
                break
    # de-dupe, keep order
    seen = set()
    uniq = []
    for a in asset_ids:
        if a not in seen:
            uniq.append(a); seen.add(a)
    return uniq

async def _catalog_details_bulk(asset_ids: List[int], *, cookie: Optional[str]) -> List[Dict[str, Any]]:
    """POST details in batches of 100. Returns list of details (dicts)."""
    if not asset_ids:
        return []
    items = [{"itemType": "Asset", "id": int(a)} for a in asset_ids if a]
    out: List[Dict[str, Any]] = []
    headers = _cookie_headers(cookie) if cookie else {"User-Agent": "Mozilla/5.0"}
    BATCH = 100
    for i in range(0, len(items), BATCH):
        payload = {"items": items[i:i+BATCH]}
        js = await _http_post_json(CATALOG_DETAILS_URL, payload, headers=headers)
        data = (js or {}).get("data") if isinstance(js, dict) else (js if isinstance(js, list) else [])
        if data:
            out.extend(data)
    return out

async def _resale_cached(aid: int) -> Dict[str, Any] | None:
    key = f"resale:{aid}"
    try:
        cached = await cache.get_json(key, 30*60)
        if cached is not None:
            return cached
    except Exception:
        pass
    js = await _http_get_json(RESALE_URL.format(aid=int(aid)))
    try:
        await cache.set_json(key, js or {}, 30*60)  # positional ttl
    except Exception:
        pass
    return js or {}

async def _collectible_map(uid: int, cookie: Optional[str]) -> Dict[int, str]:
    """assetId -> name for only collectible items."""
    all_ids = await _fetch_inventory_asset_ids(uid, cookie=cookie)
    details = await _catalog_details_bulk(all_ids, cookie=cookie)
    out: Dict[int, str] = {}
    for d in details or []:
        try:
            aid = int(d.get("id") or 0)
        except Exception:
            continue
        if not aid:
            continue
        if _is_collectible_detail(d):
            name = d.get("name") or (d.get("asset") or {}).get("name") or "Unknown"
            out[aid] = name
    return out

async def get_collectibles_with_rap(uid: int, cookie: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute RAP using inventory + catalog details (collectible filter) + resale-data.
    Returns: {"items":[{"assetId","name","rap"}], "total": int}
    """
    logger.debug("get_collectibles_with_rap[v2] uid=%s", uid)
    id2name = await _collectible_map(int(uid), cookie)
    if not id2name:
        return {"items": [], "total": 0}
    import asyncio
    sem = asyncio.Semaphore(16)
    items: List[Dict[str, Any]] = []
    async def probe(aid: int, name: str):
        async with sem:
            rs = await _resale_cached(aid)
        if not isinstance(rs, dict):
            return
        rap = int(rs.get("recentAveragePrice") or 0)
        items.append({"assetId": aid, "name": name, "rap": rap})
    await asyncio.gather(*(probe(a, n) for a, n in id2name.items()))
    items.sort(key=lambda x: x.get("rap", 0), reverse=True)
    total = sum(x.get("rap", 0) for x in items)
    return {"items": items, "total": int(total)}

async def get_offsale_items(uid: int, cookie: Optional[str] = None) -> Dict[str, Any]:
    """Collect only collectible items that are currently off-sale (no active offers)."""
    logger.debug("get_offsale_items[v2] uid=%s", uid)
    id2name = await _collectible_map(int(uid), cookie)
    if not id2name:
        return {"items": []}
    import asyncio
    sem = asyncio.Semaphore(16)
    out: List[Dict[str, Any]] = []
    async def probe(aid: int, name: str):
        async with sem:
            rs = await _resale_cached(aid)
        if not isinstance(rs, dict):
            return
        lowest = rs.get("lowestResalePrice")
        remaining = rs.get("numberRemaining")
        if lowest in (None, 0) and (remaining in (None, 0)):
            rap = int(rs.get("recentAveragePrice") or 0)
            out.append({"assetId": aid, "name": name, "rap": rap})
    await asyncio.gather(*(probe(a, n) for a, n in id2name.items()))
    out.sort(key=lambda x: x.get("rap", 0), reverse=True)
    return {"items": out}
