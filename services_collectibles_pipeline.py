# services_collectibles_pipeline.py
# Inventory -> filter collectibles -> RAP + Off-sale + images (uses your roblox_client & roblox_imagegen)
from __future__ import annotations
import asyncio, logging, os, time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import httpx

# ---------- dedicated rotating logger ----------
LOG_FILE = os.environ.get("COLLECTIBLES_LOG", "collectibles_debug.log")
log = logging.getLogger("svc.collectibles")
if not log.handlers:
    log.setLevel(logging.DEBUG)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_fh)
    log.propagate = False

# ---------- external deps (do NOT modify roblox_client.py) ----------
from roblox_client import (
    fetch_full_inventory_parallel_fast,
    ASSET_TYPE_TO_CATEGORY,
    get_full_inventory_with_cookie,
)

# Image rendering (same style as inventory)
try:
    import roblox_imagegen as imggen
except Exception:
    imggen = None

INV_TYPES = sorted({int(t) for t in ASSET_TYPE_TO_CATEGORY.keys()})
HTTP_T = httpx.Timeout(8.0, connect=2.0, read=6.0)

# In-memory cache for inventory_collectibles (shared between inventory, RAP, off-sale)
_INV_CACHE: Dict[tuple[int, int], Dict[str, Any]] = {}
_INV_CACHE_TTL = int(os.environ.get("COLLECTIBLES_INV_TTL", "300"))


def _coerce_int(v) -> int:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return 0


def _is_collectible(detail: dict) -> bool:
    """
    Return True only if itemRestrictions explicitly mark the item as Collectible.
    We ignore collectibleItemId, collectibleId and assetTypeShort heuristics
    to avoid overcounting.
    """
    if not isinstance(detail, dict):
        return False
    restr = detail.get("itemRestrictions") or []
    if not isinstance(restr, list):
        return False
    try:
        return any(str(x).strip().lower() == "collectible" for x in restr)
    except Exception:
        return False


async def _catalog_details(asset_ids: List[int], cookie: Optional[str]) -> List[dict]:
    # Prefer your client's batch details when available
    t0 = time.time()
    try:
        from roblox_client import _catalog_batch_fast as details_batch
        items = [{"itemType": "Asset", "id": int(a)} for a in asset_ids]
        out: List[dict] = []
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            rows = await details_batch(chunk, cookie, retries=2)
            if rows:
                out.extend(rows)
        log.debug(f"[details] ok items={len(asset_ids)} rows={len(out)} dt={time.time()-t0:.3f}s")
        return out
    except Exception as e:
        log.warning(f"[details] client-batch failed: {e}")

    # Fallback: ask imagegen if it exposes a details fetcher
    try:
        if imggen and hasattr(imggen, "fetch_catalog_details"):
            out = await imggen.fetch_catalog_details(asset_ids, cookie=cookie)
            log.debug(f"[details] imagegen rows={len(out)} dt={time.time()-t0:.3f}s")
            return out
    except Exception as e:
        log.warning(f"[details] imagegen failed: {e}")
    log.debug(f"[details] empty dt={time.time()-t0:.3f}s")
    return []


async def _resale_data(asset_id: int, cookie: Optional[str]) -> dict:
    url = f"https://economy.roblox.com/v1/assets/{int(asset_id)}/resale-data"
    try:
        async with httpx.AsyncClient(timeout=HTTP_T) as cli:
            headers = {"Cookie": f".ROBLOSECURITY={cookie}"} if cookie else None
            r = await cli.get(url, headers=headers)
            if r.status_code >= 400:
                return {}
            return r.json() or {}
    except Exception as e:
        log.debug(f"[resale] {asset_id} fail: {e}")
        return {}


def _price_from_detail_or_img(detail: dict) -> int:
    pi = detail.get("priceInfo")
    if isinstance(pi, dict):
        return _coerce_int(pi.get("value"))
    return max(_coerce_int(detail.get("lowestPrice")), _coerce_int(detail.get("price")))


# ============= Public API =============

async def inventory_collectibles(uid: int, cookie: Optional[str]) -> Dict[str, Any]:
    """
    Pulls full inventory via roblox_client.get_full_inventory_with_cookie, then filters collectibles by catalog details.
    Returns {"items":[{assetId,name,thumbnailUrl,detail}], "all_count":N, "collectibles_count":M}
    Uses a shared in-memory cache so RAP / off-sale / collectibles inventory
    do not refetch inventory separately within the TTL, and reuses the same
    full-inventory cache that is used by the main inventory UI.
    """
    t0 = time.time()
    key = (int(uid), 1 if cookie else 0)
    now = time.time()
    cached = _INV_CACHE.get(key)
    if cached and now - cached.get("ts", 0) < _INV_CACHE_TTL:
        log.debug(
            "[inv_cache] hit uid=%s cookie=%s items=%d",
            uid,
            bool(cookie),
            len(cached.get("items", [])),
        )
        # strip ts when returning
        return {k: v for k, v in cached.items() if k != "ts"}

    inv = await get_full_inventory_with_cookie(int(uid), cookie)
    by_cat = (inv or {}).get("byCategory") or {}
    ids: List[int] = []
    for _, arr in by_cat.items():
        for it in arr or []:
            aid = it.get("assetId")
            if not aid:
                continue
            try:
                aid_int = int(aid)
            except Exception:
                continue
            ids.append(aid_int)

    # uniq keep order
    seen: set[int] = set()
    uniq: List[int] = []
    for a in ids:
        if a not in seen:
            seen.add(a)
            uniq.append(a)

    log.debug("[inv] uid=%s all_assets=%d dt=%.3fs", uid, len(uniq), time.time() - t0)
    if not uniq:
        res = {"items": [], "all_count": 0, "collectibles_count": 0}
        _INV_CACHE[key] = {**res, "ts": time.time()}
        return res

    # Pull catalog details only once; thumbnail + restrictions + etc.
    details = await _catalog_details(uniq, cookie)
    col = [d for d in details if _is_collectible(d)]
    items = [
        {
            "assetId": _coerce_int(d.get("id") or d.get("assetId")),
            "name": (d.get("name") or "").strip() or str(d.get("id")),
            "thumbnailUrl": d.get("thumbnailUrl") or d.get("thumbnailUrlFinal") or "",
            "detail": d,
        }
        for d in col
    ]

    log.debug(
        "[filter] collectibles=%d / %d total dt=%.3fs",
        len(items),
        len(uniq),
        time.time() - t0,
    )
    res = {"items": items, "all_count": len(uniq), "collectibles_count": len(items)}
    _INV_CACHE[key] = {**res, "ts": time.time()}
    return res


async def collectibles_with_rap(
    uid: int,
    cookie: Optional[str],
    *,
    generate_image: bool = False,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Returns {"items":[{assetId,name,rap,thumbnailUrl}], "total":int, "image_path":str|None}.

    generate_image:
        False -> только считаем RAP (image_path будет None, картинка не рендерится)
        True  -> дополнительно рендерим грид, image_path указывает на png (если imagegen не упал)
    """
    t0 = time.time()
    base = await inventory_collectibles(uid, cookie)
    items = base["items"]
    total = 0

    # tune concurrency
    sem = asyncio.Semaphore(12)

    async def _one(it):
        aid = it["assetId"]
        async with sem:
            rs = await _resale_data(aid, cookie)
        rap = max(
            _coerce_int((rs or {}).get("recentAveragePrice")),
            _price_from_detail_or_img(it["detail"]),
        )
        return {
            "assetId": aid,
            "name": it["name"],
            "rap": rap,
            "thumbnailUrl": it["thumbnailUrl"],
        }

    out = await asyncio.gather(*[_one(x) for x in items])
    for x in out:
        total += _coerce_int(x["rap"])
    log.debug(f"[rap] items={len(out)} total={total} dt={time.time()-t0:.3f}s")

    image_path: Optional[str] = None
    # Рисуем грид так же, как у инвентаря, НО только если generate_image=True
    if generate_image and imggen and hasattr(imggen, "generate_full_inventory_grids") and out:
        try:
            render_items = [
                {
                    "assetId": x["assetId"],
                    "name": x["name"],
                    "priceInfo": {"value": _coerce_int(x["rap"])},
                    "thumbnailUrl": x["thumbnailUrl"],
                }
                for x in out
            ]

            # лимиты такие же, как в обычном инвентаре
            try:
                cap = max(1, int(os.getenv("MAX_ITEMS_PER_IMAGE", "650")))
            except Exception:
                cap = 650
            try:
                tile = int(os.getenv("INVENTORY_TILE", "150"))
            except Exception:
                tile = 150

            pages = await imggen.generate_full_inventory_grids(
                render_items,
                tile=tile,
                username=username or str(uid),
                user_id=int(uid),
                title="RAP",
                max_items_per_image=cap,
            )

            if pages:
                os.makedirs("temp", exist_ok=True)
                image_path = os.path.abspath(os.path.join("temp", f"rap_{uid}.png"))
                with open(image_path, "wb") as f:
                    f.write(pages[0])

            log.debug(f"[rap] image generated path={image_path!r}")
        except Exception as e:
            log.debug(f"[rap] imagegen fail: {e}")
            image_path = None

    return {"items": out, "total": total, "image_path": image_path}


async def offsale_collectibles(uid: int, cookie: Optional[str]) -> Dict[str, Any]:
    """
    Returns {"items":[{assetId,name,rap,thumbnailUrl}], "image_path":str|None} for collectibles not on sale.
    """
    t0 = time.time()
    base = await inventory_collectibles(uid, cookie)
    rows: List[dict] = []
    for it in base["items"]:
        d = it["detail"]
        is_for_sale = bool(d.get("isForSale")) or bool(d.get("lowestResalePrice"))
        if is_for_sale:
            continue
        rs = await _resale_data(it["assetId"], cookie)
        rap = max(
            _coerce_int((rs or {}).get("recentAveragePrice")),
            _price_from_detail_or_img(d),
        )
        rows.append(
            {
                "assetId": it["assetId"],
                "name": it["name"],
                "rap": rap,
                "thumbnailUrl": it["thumbnailUrl"],
            }
        )

    rows.sort(key=lambda x: _coerce_int(x["rap"]), reverse=True)
    log.debug(f"[offsale] items={len(rows)} dt={time.time()-t0:.3f}s")

    image_path: Optional[str] = None
    if imggen and hasattr(imggen, "generate_full_inventory_grids") and rows:
        try:
            render_items = [
                {
                    "assetId": x["assetId"],
                    "name": x["name"],
                    "priceInfo": {"value": _coerce_int(x["rap"])},
                    "thumbnailUrl": x["thumbnailUrl"],
                }
                for x in rows
            ]

            try:
                cap = max(1, int(os.getenv("MAX_ITEMS_PER_IMAGE", "650")))
            except Exception:
                cap = 650
            try:
                tile = int(os.getenv("INVENTORY_TILE", "150"))
            except Exception:
                tile = 150

            pages = await imggen.generate_full_inventory_grids(
                render_items,
                tile=tile,
                username=str(uid),
                user_id=int(uid),
                title="Off-sale",
                max_items_per_image=cap,
            )

            if pages:
                os.makedirs("temp", exist_ok=True)
                image_path = os.path.abspath(os.path.join("temp", f"offsale_{uid}.png"))
                with open(image_path, "wb") as f:
                    f.write(pages[0])

            log.debug(f"[offsale] image generated path={image_path!r}")
        except Exception as e:
            log.debug(f"[offsale] imagegen fail: {e}")
            image_path = None

    return {"items": rows, "image_path": image_path}
