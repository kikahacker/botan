from __future__ import annotations


def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0

_COOKIE_CACHE: dict[str, dict] = {}
_COOKIE_CACHE_TIME: float = 0.0
_COOKIE_CACHE_TTL: float = 300.0  # seconds
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

INV_TTL = int(getattr(CFG, "CACHE_INV_TTL", 1800))  # уменьшил для скорости
CATALOG_CONCURRENCY = int(getattr(CFG, "CATALOG_CONCURRENCY", 64))  # увеличил
CATALOG_RETRIES = int(getattr(CFG, "CATALOG_RETRIES", 2))  # уменьшил

# === Retry/backoff knobs (can be overridden via ENV) ===
INV_MAX_RETRIES = int(os.getenv("INV_MAX_RETRIES", "2"))  # уменьшил
INV_BACKOFF_BASE_MS = int(os.getenv("INV_BACKOFF_BASE_MS", "150"))  # уменьшил
INV_BACKOFF_CAP_MS = int(os.getenv("INV_BACKOFF_CAP_MS", "1000"))  # уменьшил

# === Новые настройки для скорости ===
INV_PARALLEL_TYPES = int(os.getenv("INV_PARALLEL_TYPES", "16"))  # параллелизм типов
INV_BATCH_SIZE = int(os.getenv("INV_BATCH_SIZE", "200"))  # размер батча
INV_TIMEOUT_PER_PAGE = float(os.getenv("INV_TIMEOUT_PER_PAGE", "4.0"))  # таймаут страницы
PUBLIC_MODE_MAX_COOKIES = int(os.getenv("PUBLIC_MODE_MAX_COOKIES", "2"))  # максимум куки
PUBLIC_MODE_TIMEOUT = float(os.getenv("PUBLIC_MODE_TIMEOUT", "8.0"))  # общий таймаут

log = logging.getLogger("roblox_client")
os.makedirs('logs', exist_ok=True)
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = logging.FileHandler(os.path.join('logs', 'roblox_client.log'), encoding='utf-8')
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    log.addHandler(_fh)

ASSET_TYPE_TO_CATEGORY = {
    2: "Classic Clothes",
    3: "Audio",
    8: "Hats",
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
    4: "Meshes",
    9: "Places",
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


PRICE_LOG_ENABLED = _asbool(os.getenv("PRICE_LOG", "false"))  # ВЫКЛЮЧИЛ для скорости
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

# --- Cookie cache (used by public/private full-inventory helpers)
try:
    _COOKIE_CACHE
    _COOKIE_CACHE_TIME
    _COOKIE_CACHE_TTL
except NameError:
    _COOKIE_CACHE: dict[str, dict] = {}
    _COOKIE_CACHE_TIME: float = 0.0
    _COOKIE_CACHE_TTL: float = 300.0  # seconds

async def _sleep_backoff(attempt: int) -> None:
    """
    Экспоненциальный бэкофф с полным джиттером.
    """
    base = INV_BACKOFF_BASE_MS / 1000.0
    cap = INV_BACKOFF_CAP_MS / 1000.0
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    delay = random.uniform(0.6 * delay, 1.2 * delay)
    await asyncio.sleep(delay)


async def _get_inventory_pages_fast(
        uid: int,
        asset_type: int,
        cookie: Optional[str],
        limit: int = INV_BATCH_SIZE,
) -> List[int]:
    """
    БЫСТРАЯ версия - с уменьшенными таймаутами и лимитами
    """
    endpoint = INVENTORY_URL.format(uid=uid, asset_type=asset_type)
    items: List[int] = []
    cursor: Optional[str] = None

    log.info(f"[inv_fast] begin uid={uid} type={asset_type} limit={limit}")
    t_start = time.time()

    max_pages = 3  # максимум страниц для скорости

    for page_num in range(max_pages):
        params = {"limit": limit, "sortOrder": "Desc"}
        if cursor:
            params["cursor"] = cursor

        ok = False
        last_err: Optional[str] = None

        for attempt in range(1, INV_MAX_RETRIES + 1):
            proxy = PROXY_POOL.any()
            client = await get_client(proxy)
            try:
                t_req = time.time()
                resp = await client.get(
                    endpoint,
                    params=params,
                    headers=_cookie_headers(cookie),
                    timeout=httpx.Timeout(INV_TIMEOUT_PER_PAGE, connect=1.2, read=3.0),  # уменьшил таймауты
                )
                status = resp.status_code
                dt = time.time() - t_req

                if status == 200:
                    js = resp.json() or {}
                    data = js.get("data") or []
                    for it in data:
                        aid = it.get("assetId") or it.get("id")
                        try:
                            if aid is not None:
                                items.append(int(aid))
                        except Exception:
                            pass
                    cursor = js.get("nextPageCursor")
                    ok = True
                    log.info(
                        f"[inv_fast] page uid={uid} type={asset_type} page={page_num + 1} items={len(data)} next={bool(cursor)} dt={dt:.3f}s")
                    break

                # временные статусы — повторяем
                if status in (429, 500, 502, 503, 504):
                    last_err = f"http {status}"
                    log.warning(f"[inv_fast] uid={uid} type={asset_type} {last_err}, attempt={attempt}")
                    await _sleep_backoff(attempt)
                    continue

                # 403 — может быть как временным (proxy/geo), так и из-за куки
                if status == 403:
                    last_err = "http 403"
                    log.warning(f"[inv_fast] uid={uid} type={asset_type} {last_err}, attempt={attempt}")
                    await _sleep_backoff(attempt)
                    continue

                # остальное — считаем фаталом
                last_err = f"http {status}"
                log.error(f"[inv_fast] uid={uid} type={asset_type} fatal {last_err}")
                return items

            except Exception as e:
                last_err = f"exc {type(e).__name__}: {e}"
                log.warning(f"[inv_fast] uid={uid} type={asset_type} {last_err}, attempt={attempt}")
                await _sleep_backoff(attempt)

        if not ok:
            log.error(f"[inv_fast] uid={uid} type={asset_type} giving up; collected={len(items)}")
            return items

        # следующая страница?
        if not cursor:
            break

    # uniq
    seen = set()
    out: List[int] = []
    for a in items:
        if a not in seen:
            out.append(a)
            seen.add(a)
    log.info(
        f"[inv_fast] end uid={uid} type={asset_type} collected={len(items)} uniq={len(out)} dt={time.time() - t_start:.3f}s")
    return out


async def fetch_full_inventory_parallel_fast(uid: int, asset_types: List[int], cookie: Optional[str]) -> Dict[
    int, List[int]]:
    """
    БЫСТРАЯ версия - грузит типы ассетов чанками с ограничением параллелизма
    """
    sem = asyncio.Semaphore(INV_PARALLEL_TYPES)

    async def task(t: int):
        async with sem:
            try:
                t0 = time.time()
                log.info(f"[inv_par_fast] start uid={uid} type={t}")
                lst = await _get_inventory_pages_fast(uid, t, cookie, limit=INV_BATCH_SIZE)
                log.info(f"[inv_par_fast] done uid={uid} type={t} items={len(lst)} dt={time.time() - t0:.3f}s")
                return (t, lst)
            except Exception as e:
                log.error(f"[inv_par_fast] crashed uid={uid} type={t}: {e}")
                return (t, [])

    # Разбиваем на чанки для лучшего контроля
    chunks = [asset_types[i:i + INV_PARALLEL_TYPES] for i in range(0, len(asset_types), INV_PARALLEL_TYPES)]
    all_results = {}

    for chunk in chunks:
        chunk_results = await asyncio.gather(*[task(t) for t in chunk])
        for t, lst in chunk_results:
            if lst:
                all_results[t] = lst
        # Небольшая пауза между чанками
        await asyncio.sleep(0.05)

    return all_results


async def _post_catalog_details_once(items, cookie, proxy, csrf=None):
    client = await get_client(proxy)
    headers = {"Content-Type": "application/json", **_cookie_headers(cookie)}
    if csrf:
        headers[CSRF_HEADER] = csrf
    return await client.post(
        CATALOG_DETAILS_URL,
        json={"items": items},
        headers=headers,
        timeout=httpx.Timeout(4.0, connect=1.2, read=2.8),  # уменьшил таймауты
    )


async def _catalog_batch_fast(items, cookie, retries: int) -> List[Dict[str, Any]]:
    attempt = 0
    csrf = None
    while attempt <= retries:
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
                await asyncio.sleep(0.1 * attempt)  # уменьшил задержку
                csrf = None
                continue
            return []
        except Exception:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(0.1 * attempt)  # уменьшил задержку
                continue
            return []
    return []


# ==== Local price cache (prices.csv) ====
_price_cache = None


def _load_prices_csv(path: str = PRICE_DUMP_PATH):
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


def _ensure_prices_csv_header(path: str) -> None:
    try:
        need_header = not os.path.exists(path) or os.path.getsize(path) == 0
        if need_header:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "name", "price", "collectible"])
    except Exception as e:
        log.warning(f"prices.csv header init failed: {e}")


def _append_prices_csv_bulk(details: list, path: str = PRICE_DUMP_PATH) -> int:
    """Append fetched price details to CSV (id,name,price,collectible) if missing in local cache."""
    try:
        _ensure_prices_csv_header(path)
        local = _load_prices_csv(path)
        cnt = 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for d in details:
                try:
                    aid = int(d.get("id") or 0)
                    if not aid or aid in local:
                        continue
                    price = _price_pick(d)
                    name = str(d.get("name") or "").strip()
                    collectible = 1 if _is_collectible(d) else 0
                    w.writerow([aid, name, price, collectible])
                    local[aid] = {"price": price, "collectible": bool(collectible)}
                    _price_log(f"[CSV_APPEND] id={aid} price={price} collectible={collectible}")
                    cnt += 1
                except Exception:
                    continue
        return cnt
    except Exception as e:
        log.warning(f"prices.csv append failed: {e}")
        return 0


async def fetch_catalog_details_fast(asset_ids: List[int], cookie: Optional[str]) -> List[Dict[str, Any]]:
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
                return await _catalog_batch_fast(items, cookie, CATALOG_RETRIES)

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
    """True только если в itemRestrictions реально есть тег 'Collectible'."""
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
        r = await client.get(SOCIAL_LINKS_URL.format(uid=uid), timeout=httpx.Timeout(6.0, connect=1.5, read=4.5))
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
                timeout=httpx.Timeout(8.0, connect=1.5, read=6.5),
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

    # 1) собираем assetIds по всем типам параллельно (БЫСТРАЯ версия)
    per_type = await fetch_full_inventory_parallel_fast(roblox_id, asset_types, cookie)
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
                'itemId': _to_int(d.get('itemId') or d.get('collectibleItemId') or 0)})
        if arr:
            by_cat.setdefault(cat, []).extend(arr)
            total_count += len(arr)

    data = {"total": int(total_count), "byCategory": by_cat}
    await cache.set_json(cache_key, data)
    return data


# === Helper: fetch inventory for a target Roblox ID using a provided ENCRYPTED cookie ===
async def get_full_inventory_by_encrypted_cookie(enc_cookie: str, roblox_id: int, force_refresh: bool = False) -> dict:
    """Try to fetch full inventory for *roblox_id* using the given encrypted cookie."""
    try:
        # Декодируем куки
        cookie = decrypt_text(enc_cookie) if 'decrypt_text' in globals() else enc_cookie
        if not cookie:
            return {"total": 0, "byCategory": {}}

        # Используем ТОЧНО ТУ ЖЕ ЛОГИКУ, что и в get_full_inventory
        asset_types = _parse_asset_types_from_cfg()

        # 1) собираем assetIds по всем типам параллельно (БЫСТРАЯ версия)
        per_type = await fetch_full_inventory_parallel_fast(roblox_id, asset_types, cookie)
        all_ids = [a for ids in per_type.values() for a in ids]
        if not all_ids:
            return {"total": 0, "byCategory": {}}

        # 2) тянем детали по всем assetId
        details = await fetch_catalog_details_fast(all_ids, cookie)
        by_id = {int(d.get("id")): d for d in details if d.get("id") is not None}

        # 3) собираем по категориям (ТОЧНО КАК В ОСНОВНОЙ ФУНКЦИИ)
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
                price = _price_pick_local(d)
                arr.append({
                    "assetId": int(aid),
                    "priceInfo": {"value": int(price)},
                    "name": d.get("name") or "",
                    "assetType": int(at),
                    'itemId': _to_int(d.get('itemId') or d.get('collectibleItemId') or 0)
                })
            if arr:
                by_cat.setdefault(cat, []).extend(arr)
                total_count += len(arr)

        return {"total": int(total_count), "byCategory": by_cat}

    except Exception as e:
        log.error(f"Failed to get inventory by encrypted cookie: {e}")
        return {"total": 0, "byCategory": {}}


# Глобальный кэш рабочих куки
_COOKIE_CACHE: Dict[int, str] = {}
_COOKIE_CACHE_TIME: Dict[int, float] = {}
COOKIE_CACHE_TTL = 300  # 5 минут


# Улучшенная функция get_inventory_public_ultra_fast
async def get_inventory_public_ultra_fast(roblox_id: int) -> dict:
    """
    УЛЬТРА-БЫСТРЫЙ public-режим с кэшированием рабочей куки
    """
    try:
        # Используем таймаут для всего public режима
        return await asyncio.wait_for(
            _get_inventory_public_ultra_fast_internal(roblox_id),
            timeout=PUBLIC_MODE_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error(f"[ULTRA_FAST] TIMEOUT for {roblox_id} after {PUBLIC_MODE_TIMEOUT}s")
        return {"total": 0, "byCategory": {}}
    except Exception as e:
        log.error(f"[ULTRA_FAST] ERROR for {roblox_id}: {e}")
        return {"total": 0, "byCategory": {}}


async def _get_inventory_public_ultra_fast_internal(roblox_id: int) -> dict:
    now = time.time()

    # 1. Проверяем кэш (самый быстрый путь)
    cached_cookie = _COOKIE_CACHE.get(roblox_id)
    if cached_cookie and (now - _COOKIE_CACHE_TIME.get(roblox_id, 0)) < COOKIE_CACHE_TTL:
        try:
            result = await get_full_inventory_by_encrypted_cookie(cached_cookie, roblox_id)
            if result.get("total", 0) > 0:
                log.info(f"[ULTRA_FAST] Cache HIT for {roblox_id}, items: {result['total']}")
                return result
            else:
                log.info(f"[ULTRA_FAST] Cache MISS (no items) for {roblox_id}")
        except Exception as e:
            log.warning(f"[ULTRA_FAST] Cache FAIL for {roblox_id}: {e}")
        # Удаляем нерабочую куки из кэша
        _COOKIE_CACHE.pop(roblox_id, None)

    # 2. Ищем новую рабочую куки
    log.info(f"[ULTRA_FAST] Searching working cookie for {roblox_id}")
    cookies = await storage.get_multiple_cookies_quick(limit=PUBLIC_MODE_MAX_COOKIES)

    if not cookies:
        log.error(f"[ULTRA_FAST] No cookies in DB for {roblox_id}")
        return {"total": 0, "byCategory": {}}

    # Пробуем куки по очереди
    working_cookie = None
    working_result = None

    for i, enc_cookie in enumerate(cookies):
        try:
            log.info(f"[ULTRA_FAST] Trying cookie {i + 1}/{len(cookies)} for {roblox_id}")
            result = await get_full_inventory_by_encrypted_cookie(enc_cookie, roblox_id)

            if result.get("total", 0) > 0:
                working_cookie = enc_cookie
                working_result = result  # Сохраняем результат
                log.info(f"[ULTRA_FAST] SUCCESS with cookie {i + 1}, items: {result['total']}")
                break
            else:
                log.info(f"[ULTRA_FAST] Cookie {i + 1} worked but no items")
        except Exception as e:
            log.warning(f"[ULTRA_FAST] Cookie {i + 1} failed: {e}")
            continue

    # 3. Сохраняем в кэш и возвращаем результат
    if working_cookie and working_result:
        _COOKIE_CACHE[roblox_id] = working_cookie
        _COOKIE_CACHE_TIME[roblox_id] = now
        return working_result  # Возвращаем сохраненный результат

    log.error(f"[ULTRA_FAST] ALL cookies failed for {roblox_id}")
    return {"total": 0, "byCategory": {}}

async def clear_cookie_cache(roblox_id: Optional[int] = None):
    """Очищает кэш куки (для дебага или принудительного обновления)"""
    if roblox_id:
        _COOKIE_CACHE.pop(roblox_id, None)
        _COOKIE_CACHE_TIME.pop(roblox_id, None)
        log.info(f"[CACHE] Cleared cache for {roblox_id}")
    else:
        _COOKIE_CACHE.clear()
        _COOKIE_CACHE_TIME.clear()
        log.info("[CACHE] Cleared ALL cookie cache")


# --- Public wrapper that mirrors private pipeline using DB cookies ---
async def get_full_inventory_public_like_private(roblox_id: int, cookies_limit: int | None = None, force_refresh: bool = False) -> dict:
    """Use the same full-inventory pipeline as private, but pick any working encrypted cookie from DB."""
    try:
        limit = cookies_limit or (PUBLIC_MODE_MAX_COOKIES if 'PUBLIC_MODE_MAX_COOKIES' in globals() else 10)
        log.info(f"[PUBLIC_LIKE_PRIV] Searching working cookie for {roblox_id} (limit={limit})")
        # get encrypted cookies from storage
        try:
            cookies = await storage.get_multiple_cookies_quick(limit=limit)
        except Exception as e:
            log.warning(f"[PUBLIC_LIKE_PRIV] storage.get_multiple_cookies_quick failed: {e}")
            cookies = []
        if not cookies:
            log.warning("[PUBLIC_LIKE_PRIV] No cookies available in DB")
            return {"total": 0, "byCategory": {}}

        # try cookies sequentially until one succeeds
        for idx, enc_cookie in enumerate(cookies, start=1):
            try:
                log.info(f"[PUBLIC_LIKE_PRIV] Try cookie {idx}/{len(cookies)} for {roblox_id}")
                result = await get_full_inventory_by_encrypted_cookie(enc_cookie, roblox_id, force_refresh=force_refresh)
                total = int(result.get("total", 0) or 0) if isinstance(result, dict) else 0
                if total > 0 and isinstance(result.get("byCategory"), dict) and result["byCategory"]:
                    log.info(f"[PUBLIC_LIKE_PRIV] SUCCESS with cookie {idx}, items: {total}")
                    return result
                else:
                    log.info(f"[PUBLIC_LIKE_PRIV] Cookie {idx} returned empty/hidden inventory")
            except Exception as e:
                log.warning(f"[PUBLIC_LIKE_PRIV] Cookie {idx} failed: {type(e).__name__}: {e}")
                continue

        log.warning(f"[PUBLIC_LIKE_PRIV] All cookies failed for {roblox_id}")
        return {"total": 0, "byCategory": {}}
    except Exception as e:
        log.error(f"[PUBLIC_LIKE_PRIV] Unexpected error for {roblox_id}: {type(e).__name__}: {e}", exc_info=True)
        return {"total": 0, "byCategory": {}}
