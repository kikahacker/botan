from __future__ import annotations


def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0

import asyncio
import csv
import hashlib
import logging
import os
import random
import time
from typing import Optional, Dict, Any, List

import httpx

# roblox_client.py — CSV append-only (skip existing) + правильный collectible
from http_shared import get_client, PROXY_POOL
from config import CFG
import storage
from util.crypto import decrypt_text
import cache

INVENTORY_URL = "https://inventory.roblox.com/v2/users/{uid}/inventory/{asset_type}"
CATALOG_DETAILS_URL = "https://catalog.roblox.com/v1/catalog/items/details"
CSRF_HEADER = "x-csrf-token"
BATCH_SIZE = 120

INV_TTL = int(getattr(CFG, "CACHE_INV_TTL", 3600))
CATALOG_CONCURRENCY = int(getattr(CFG, "CATALOG_CONCURRENCY", 24))
CATALOG_RETRIES = int(getattr(CFG, "CATALOG_RETRIES", 6))

# === Retry/backoff knobs (can be overridden via ENV) ===
INV_MAX_RETRIES = int(os.getenv("INV_MAX_RETRIES", "4"))
INV_BACKOFF_BASE_MS = int(os.getenv("INV_BACKOFF_BASE_MS", "300"))
INV_BACKOFF_CAP_MS = int(os.getenv("INV_BACKOFF_CAP_MS", "2500"))

log = logging.getLogger(__name__)

ASSET_TYPE_TO_CATEGORY = {
    2:  "Classic Clothes",
    3:  "Audio",
    8:  "Hats",
    11: "Classic Clothes",
    12: "Classic Clothes",
    17: "Heads",
    18: "Faces",
    19: "Gear",
    24: "Animations/Emotes",
    27: "Bundles/Packages",
    41: "Hair",
    42: "Accessories",
    43: "Accessories",
    44: "Accessories",
    45: "Accessories",
    46: "Accessories",
    47: "Accessories",
    48: "Accessories",
    49: "Accessories",
    50: "Accessories",
    61: "Emotes",
    # excluded (оставлены для полноты)
    4:  "Meshes",
    9:  "Places",
    10: "Models",
    13: "Decals",
    21: "Badges",
    38: "Plugins",
}

# ==== Category normalization / merge (server-side) ====
_BANNED_CATEGORIES = {"Meshes", "Places", "Models", "Decals", "Badges", "Plugins"}
_CLASSIC_FAMILY = {"Classic T-shirts", "Classic Shirts", "Classic Pants", "Classic Clothes"}
_ACCESSORY_FAMILY = {
    "Face Accessory", "Neck Accessory", "Shoulder Accessory", "Front Accessory",
    "Back Accessory", "Waist Accessory", "Shirt Accessories", "Pants Accessories",
    "Gear Accessories", "Accessories"
}

def _canon_cat(name: str) -> str:
    n = (name or "").strip()
    if not n or n in _BANNED_CATEGORIES:
        return ""
    if n in _CLASSIC_FAMILY:
        return "Classic Clothes"
    if n in _ACCESSORY_FAMILY or ("Accessory" in n or "Accessories" in n):
        return "Accessories"
    return n


# ===================== ЛОГИ И CSV ПО ЦЕНАМ =====================
def _asbool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

PRICE_LOG_ENABLED = _asbool(os.getenv("PRICE_LOG", "true"))  # лог в файл включён по умолчанию
LOG_DIR = os.getenv("IMAGEGEN_LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

PRICE_LOG_PATH = os.path.join(LOG_DIR, "pricing.log")
PRICE_DUMP_PATH = os.getenv("PRICE_DUMP_PATH", os.path.join(LOG_DIR, "prices.csv"))

_price_logger = logging.getLogger("pricing")
if not _price_logger.handlers:
    _price_logger.setLevel(logging.INFO if PRICE_LOG_ENABLED else logging.CRITICAL)
    _fh = logging.FileHandler(PRICE_LOG_PATH, encoding="utf-8")
    _fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    _fh.setFormatter(_fmt)
    _price_logger.addHandler(_fh)

def _price_log(line: str):
    if PRICE_LOG_ENABLED:
        _price_logger.info(line)


def _category_for_asset_type(asset_type: int) -> str:
    return ASSET_TYPE_TO_CATEGORY.get(int(asset_type), "Accessories")

def _cookie_headers(cookie: Optional[str]) -> Dict[str, str]:
    return {"Cookie": f".ROBLOSECURITY={cookie}"} if cookie else {}


# ===================== Надёжная пагинация инвентаря =====================

async def _sleep_backoff(attempt: int) -> None:
    """
    Экспоненциальный бэкофф с полным джиттером.
    0.3s, 0.6s, 1.2s ... capped by INV_BACKOFF_CAP_MS.
    """
    base = INV_BACKOFF_BASE_MS / 1000.0
    cap = INV_BACKOFF_CAP_MS / 1000.0
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    delay = random.uniform(0.6 * delay, 1.2 * delay)
    await asyncio.sleep(delay)

async def _get_inventory_pages(
    uid: int,
    asset_type: int,
    cookie: Optional[str],
    limit: int = 100,
) -> List[int]:
    """
    Тянем ВСЕ страницы по одному типу ассетов с ретраями и логами причин.
    На фаталах не кидаем исключения — возвращаем то, что успели собрать (возможно пусто).
    """
    endpoint = INVENTORY_URL.format(uid=uid, asset_type=asset_type)
    items: List[int] = []
    cursor: Optional[str] = None

    while True:
        params = {"limit": limit, "sortOrder": "Desc"}
        if cursor:
            params["cursor"] = cursor

        ok = False
        last_err: Optional[str] = None

        # каждый "шаг" (страница) — с несколькими попытками
        for attempt in range(1, INV_MAX_RETRIES + 1):
            proxy = PROXY_POOL.any()
            client = await get_client(proxy)
            try:
                resp = await client.get(
                    endpoint,
                    params=params,
                    headers=_cookie_headers(cookie),
                    timeout=httpx.Timeout(8.0, connect=2.0, read=6.0),
                )
                status = resp.status_code

                if status == 200:
                    js = resp.json() or {}
                    for it in js.get("data") or []:
                        aid = it.get("assetId") or it.get("id")
                        try:
                            if aid is not None:
                                items.append(int(aid))
                        except Exception:
                            pass
                    cursor = js.get("nextPageCursor")
                    ok = True
                    break

                # временные статусы — повторяем
                if status in (429, 500, 502, 503, 504):
                    last_err = f"http {status}"
                    log.warning(f"[inv] uid={uid} type={asset_type} {last_err}, attempt={attempt}")
                    await _sleep_backoff(attempt)
                    continue

                # 403 — может быть как временным (proxy/geo), так и из-за куки
                if status == 403:
                    last_err = "http 403"
                    log.warning(f"[inv] uid={uid} type={asset_type} {last_err} (cookie/proxy?), attempt={attempt}")
                    await _sleep_backoff(attempt)
                    continue

                # остальное — считаем фаталом
                last_err = f"http {status}"
                log.error(f"[inv] uid={uid} type={asset_type} fatal {last_err}: {resp.text[:200]}")
                return items

            except Exception as e:
                last_err = f"exc {type(e).__name__}: {e}"
                log.warning(f"[inv] uid={uid} type={asset_type} {last_err}, attempt={attempt}")
                await _sleep_backoff(attempt)

        if not ok:
            # выгорели все попытки для этой страницы
            log.error(f"[inv] uid={uid} type={asset_type} giving up after retries; collected={len(items)} last_err={last_err}")
            return items

        # следующая страница?
        if not cursor:
            break

        # в редких случаях полезно чуть уступить API
        await asyncio.sleep(0)

    # uniq
    seen = set()
    out: List[int] = []
    for a in items:
        if a not in seen:
            out.append(a)
            seen.add(a)
    return out


async def fetch_full_inventory_parallel(uid: int, asset_types: List[int], cookie: Optional[str]) -> Dict[int, List[int]]:
    """
    Параллельно тянем инвентарь по типам, логируем результат/ошибки, не кидаем исключения.
    """
    async def task(t: int):
        try:
            lst = await _get_inventory_pages(uid, t, cookie)
            log.info(f"[inv] uid={uid} type={t} -> {len(lst)} items")
            return (t, lst)
        except Exception as e:
            log.error(f"[inv] uid={uid} type={t} crashed: {e}")
            return (t, [])

    res = await asyncio.gather(*[task(t) for t in asset_types], return_exceptions=False)
    out: Dict[int, List[int]] = {}
    for t, lst in res:
        if lst:
            out[t] = lst
    return out


async def _post_catalog_details_once(items, cookie, proxy, csrf=None):
    client = await get_client(proxy)
    headers = {"Content-Type": "application/json", **_cookie_headers(cookie)}
    if csrf:
        headers[CSRF_HEADER] = csrf
    return await client.post(
        CATALOG_DETAILS_URL,
        json={"items": items},
        headers=headers,
        timeout=httpx.Timeout(5.5, connect=1.5, read=3.5),
    )


async def _catalog_batch(items, cookie, retries: int) -> List[Dict[str, Any]]:
    attempt = 0
    csrf = None
    while True:
        proxy = PROXY_POOL.any()
        try:
            resp = await _post_catalog_details_once(items, cookie, proxy, csrf=csrf)
            if resp.status_code == 403 and CSRF_HEADER in resp.headers:
                csrf = resp.headers.get(CSRF_HEADER)
                resp = await _post_catalog_details_once(items, cookie, proxy, csrf=csrf)
            resp.raise_for_status()
            js = resp.json()
            if isinstance(js, dict) and "data" in js:
                return js["data"]
            if isinstance(js, list):
                return js
            return []
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.15 * attempt)
                csrf = None
                continue
            return []
        except Exception:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(0.15 * attempt)
                continue
            return []


# ==== Local price cache (prices.csv) ====
_price_cache = None

def _load_prices_csv(path: str = "prices.csv"):
    global _price_cache
    if _price_cache is not None:
        return _price_cache
    _price_cache = {}
    try:
        if not os.path.exists(path):
            return _price_cache
        with open(path, "r", encoding="utf-8") as f:
            import csv as _csv
            r = _csv.DictReader(f)
            for row in r:
                try:
                    aid = int(row.get("assetId") or row.get("id") or 0)
                    if not aid:
                        continue
                    price_raw = row.get("price") or row.get("lowestPrice") or "0"
                    price = int(float(price_raw)) if str(price_raw).strip() else 0
                    col_raw = (row.get("Collectible") or row.get("collectible") or "").strip().lower()
                    collectible = col_raw in ("1", "true", "yes")
                    _price_cache[aid] = {"price": price, "collectible": collectible}
                except Exception:
                    continue
    except Exception as e:
        logging.warning(f"prices.csv load failed: {e}")
    return _price_cache


async def fetch_catalog_details_fast(asset_ids: List[int], cookie: Optional[str]) -> List[Dict[str, Any]]:
    if not asset_ids:
        return []
    sem = asyncio.Semaphore(CATALOG_CONCURRENCY)

    async def run(items):
        if not asset_ids:
            return []

    local_prices = _load_prices_csv()
    found_local = []
    need_fetch = []
    # de-dup incoming ids
    seen = set()
    for aid in asset_ids:
        try:
            aid = int(aid)
        except Exception:
            continue
        if aid in seen:
            continue
        seen.add(aid)
        if aid in local_prices:
            pinfo = local_prices[aid]
            found_local.append({
                "id": aid,
                "itemType": "Asset",
                "price": pinfo.get("price", 0),
                "lowestPrice": pinfo.get("price", 0),
                "itemRestrictions": ["Collectible"] if pinfo.get("collectible") else [],
            })
        else:
            need_fetch.append(aid)

    out = []
    if need_fetch:
        sem = asyncio.Semaphore(CATALOG_CONCURRENCY)

        async def run(items):
            async with sem:
                return await _catalog_batch(items, cookie, CATALOG_RETRIES)

        tasks = []
        for i in range(0, len(need_fetch), BATCH_SIZE):
            items = [{"id": int(aid), "itemType": "Asset"} for aid in need_fetch[i:i + BATCH_SIZE]]
            tasks.append(asyncio.create_task(run(items)))

        res = await asyncio.gather(*tasks, return_exceptions=True)
        for r in res:
            if isinstance(r, list):
                out.extend(r)

    # append local cached results last (or merge if duplicates)
    if found_local:
        out.extend(found_local)

    return out


def _norm_price(v) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = v.strip().replace(",", ".").replace(" ", "")
            for token in ("R$", "₽", "RBX", "robux", "руб", "$"):
                s = s.replace(token, "")
            return int(float(s))
    except Exception:
        return 0
    return 0


def _price_pick(detail: dict) -> int:
    p = _norm_price(detail.get("price"))
    lp = _norm_price(detail.get("lowestPrice"))
    lrp = _norm_price(detail.get("lowestResalePrice"))
    hrp = _norm_price(detail.get("highestResalePrice"))
    vals = [p, lp, lrp, hrp]
    mx = max(vals) if any(vals) else 0
    detail["_dbg_prices_tuple"] = (p, lp, lrp, hrp, mx)
    return mx


def _is_collectible(detail: dict) -> bool:
    """True только если в itemRestrictions реально есть тег 'Collectible'.
    Раньше мы также считали collectible при наличии collectibleItemId — можно включить фоллбэк через
    USE_COLLECTIBLE_ITEMID_FALLBACK=1.
    """
    ir = detail.get("itemRestrictions") or []
    try:
        for x in ir:
            if str(x).strip().lower() == "collectible":
                return True
    except Exception:
        pass
    use_fallback = str(os.getenv("USE_COLLECTIBLE_ITEMID_FALLBACK", "0")).lower() in ("1", "true", "yes", "y")
    if use_fallback and detail.get("collectibleItemId"):
        return True
    return False


def _parse_asset_types_from_cfg() -> List[int]:
    ats = getattr(CFG, "ASSET_TYPES", None)
    if isinstance(ats, (list, tuple)) and ats:
        try:
            return [int(x) for x in ats]
        except Exception:
            pass
    # default set: include as much as возможно, исключая video/plugins/places/models/meshes/decals/badges
    include = {
        2, 3, 8, 11, 12, 17, 18, 19, 24, 27,
        41, 42, 43, 44, 45, 46, 47, 48, 49, 50,
        61,
    }
    exclude = {4, 9, 10, 13, 21, 38, 62}  # 62 ~ video (на всякий случай)
    return sorted(list(include - exclude))


# === Spending helpers (Robux) ===
import datetime

ECON_TX_URL = "https://economy.roblox.com/v2/users/{uid}/transactions"
SOCIAL_LINKS_URL = "https://users.roblox.com/v1/users/{uid}/social-links"

async def get_social_links(uid: int) -> dict:
    """Возвращает непустые соцсети пользователя в виде {platform: url}."""
    try:
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        r = await client.get(SOCIAL_LINKS_URL.format(uid=uid), timeout=httpx.Timeout(8.0, connect=2.0, read=6.0))
        if r.status_code != 200:
            return {}
        js = r.json() or {}
        links = {}
        for it in js.get("data") or []:
            platform = str(it.get("type") or "").strip()
            url = str(it.get("url") or "").strip()
            if platform and url:
                links[platform] = url
        return links
    except Exception:
        return {}


async def get_total_spent_robux(uid: int, cookie: str) -> int:
    """Return lifetime spent Robux based on Economy transactions."""
    total = 0
    cursor = None
    pages = 0
    consecutive_errors = 0
    while pages < 2000:
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        try:
            r = await client.get(
                ECON_TX_URL.format(uid=uid),
                params={"transactionType": "Purchase", "limit": 100, "cursor": cursor or ""},
                headers=_cookie_headers(cookie),
                timeout=httpx.Timeout(10.0, connect=2.0, read=8.0),
            )
            if r.status_code == 429:
                await asyncio.sleep(0.6)
                continue
            if r.status_code in (401, 403):
                # Not authorized / wrong cookie -> let caller decide, don't report 0
                raise RuntimeError(f"Economy transactions auth failed: {r.status_code}")
            r.raise_for_status()
            js = r.json() or {}
            pages += 1
            consecutive_errors = 0
            data = js.get("data") or []
            for it in data:
                try:
                    amt = int(it.get("currencyAmount") or 0)
                    if amt < 0:
                        total += -amt
                except Exception:
                    pass
            cursor = js.get("nextPageCursor")
            if not cursor:
                break
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                break
            await asyncio.sleep(0.3)
            continue
    return total


async def get_full_inventory(tg_id: int, roblox_id: int, force_refresh: bool = False) -> dict:
    """
    Возвращает агрегированный инвентарь пользователя с ценами, сгруппированный по категориям.
    Структура:
        {"total": int, "byCategory": { <CategoryName>: [ {assetId, priceInfo{value}, name, assetType}, ... ] } }
    """
    try:
        enc = await storage.get_encrypted_cookie(tg_id, roblox_id)
        if not enc:
            return {"total": 0, "byCategory": {}}
        cookie = decrypt_text(enc)
    except Exception:
        return {"total": 0, "byCategory": {}}

    asset_types = _parse_asset_types_from_cfg()
    ats_hash = hashlib.sha1(",".join(map(str, asset_types)).encode()).hexdigest()[:8]
    cache_key = f"inv:{roblox_id}:{ats_hash}"

    if not force_refresh:
        cached = await cache.get_json(cache_key, INV_TTL)
        if cached:
            return cached

    # 1) собираем assetIds по всем типам параллельно
    per_type = await fetch_full_inventory_parallel(roblox_id, asset_types, cookie)
    all_ids = [a for ids in per_type.values() for a in ids]
    if not all_ids:
        data = {"total": 0, "byCategory": {}}
        await cache.set_json(cache_key, data)
        return data

    # 2) тянем детали по всем assetId
    details = await fetch_catalog_details_fast(all_ids, cookie)
    by_id = {int(d.get("id")): d for d in details if d.get("id") is not None}

    # 3) собираем по категориям
    by_cat: dict[str, list] = {}
    total_count = 0

    def _price_pick_local(detail: dict) -> int:
        try:
            p = detail.get("price")
            lp = detail.get("lowestPrice")
            lrp = detail.get("lowestResalePrice")
            for v in (p, lp, lrp):
                if v is not None:
                    return int(v)
        except Exception:
            pass
        return 0

    for at, ids in per_type.items():
        cat = _canon_cat(ASSET_TYPE_TO_CATEGORY.get(int(at), "Other"))
        arr = []
        if not cat:
            continue
        for aid in ids:
            d = by_id.get(int(aid))
            if not d:
                continue
            price = _price_pick_local(d)  # 0 если цены нет
            arr.append({
                "assetId": int(aid),
                "priceInfo": {"value": int(price)},
                "name": d.get("name") or "",
                "assetType": int(at),
             "itemId": _to_int(d.get("itemId") or d.get("collectibleItemId") or 0) })
        if arr:
            by_cat.setdefault(cat, []).extend(arr)
            total_count += len(arr)

    data = {"total": int(total_count), "byCategory": by_cat}
    await cache.set_json(cache_key, data)
    return data
