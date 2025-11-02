from __future__ import annotations
import asyncio, random, time
import httpx
from typing import Dict, Any, List, Tuple
from config import CFG
from http_shared import get_client, PROXY_POOL
import cache
PROFILE_TTL = int(getattr(CFG, 'PUBLIC_PROFILE_TTL', 1800))
INV_TTL = int(getattr(CFG, 'PUBLIC_INV_TTL', 1800))
CATALOG_RETRIES = int(getattr(CFG, 'CATALOG_RETRIES', 8))
CATALOG_CONCURRENCY = int(getattr(CFG, 'CATALOG_CONCURRENCY', 8))
CATALOG_BATCH_SIZE = int(getattr(CFG, 'CATALOG_BATCH_SIZE', 60))
CATALOG_BASE_DELAY_MS = int(getattr(CFG, 'CATALOG_BASE_DELAY_MS', 450))
ASSET_TYPES: List[Tuple[int, str]] = [(8, 'Hats')]
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

def _price_info_from_detail(d: Dict[str, Any]) -> Dict[str, Any]:
    val = 0
    if isinstance(d.get('price'), (int, float)):
        val = int(d['price'])
    elif isinstance(d.get('lowestPrice'), (int, float)):
        val = int(d['lowestPrice'])
    elif isinstance(d.get('priceStatus'), str) and d['priceStatus'] == 'Free':
        val = 0
    return {'value': val, 'source': 'catalog'}

async def _req_json(client: httpx.AsyncClient, method: str, url: str, **kw) -> Any:
    for attempt in range(0, CATALOG_RETRIES):
        try:
            r = await client.request(method, url, timeout=CFG.TIMEOUT, **kw)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (429, 403, 500, 502, 503, 504) and attempt < CATALOG_RETRIES - 1:
                await asyncio.sleep(0.15 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < CATALOG_RETRIES - 1:
                await asyncio.sleep(0.15 * (attempt + 1))
                continue
            raise

async def _ensure_csrf(client: httpx.AsyncClient) -> str:
    url = 'https://catalog.roblox.com/v1/catalog/items/details'
    try:
        r = await client.post(url, json={'items': []}, headers={'User-Agent': UA, 'Accept': 'application/json, text/plain, */*', 'Origin': 'https://www.roblox.com', 'Referer': 'https://www.roblox.com/', 'X-CSRF-TOKEN': 'fetch'}, timeout=CFG.TIMEOUT)
        return r.headers.get('x-csrf-token') or r.headers.get('X-CSRF-TOKEN') or ''
    except httpx.HTTPStatusError as e:
        return e.response.headers.get('x-csrf-token') or e.response.headers.get('X-CSRF-TOKEN') or ''
    except Exception:
        return ''
_global_lock = asyncio.Lock()
_last_post_ts = 0.0

async def _throttle():
    global _last_post_ts
    base = CATALOG_BASE_DELAY_MS / 1000.0
    jitter = random.uniform(0.25 * base, 0.75 * base)
    need_gap = base + jitter
    async with _global_lock:
        now = time.monotonic()
        delta = now - _last_post_ts
        if delta < need_gap:
            await asyncio.sleep(need_gap - delta)
        _last_post_ts = time.monotonic()

async def fetch_public_profile(user_id: int) -> Dict[str, Any]:
    key = f'pub:profile:{user_id}'
    cached = await cache.get_json(key, PROFILE_TTL)
    if cached:
        return cached
    proxy = PROXY_POOL.any()
    client = await get_client(proxy)
    data = await _req_json(client, 'GET', f'https://users.roblox.com/v1/users/{user_id}', headers={'User-Agent': UA, 'Referer': 'https://www.roblox.com/', 'Accept': 'application/json, text/plain, */*'})
    result = {'id': data.get('id') or user_id, 'name': data.get('name'), 'displayName': data.get('displayName') or data.get('name'), 'created': data.get('created'), 'isBanned': data.get('isBanned', False)}
    await cache.set_json(key, result)
    return result

async def _fetch_inventory_for_type(client: httpx.AsyncClient, user_id: int, asset_type: int) -> List[Dict[str, Any]]:
    url = f'https://inventory.roblox.com/v2/users/{user_id}/inventory/{asset_type}'
    params = {'limit': 100, 'sortOrder': 'Desc'}
    items: List[Dict[str, Any]] = []
    cursor = ''
    while True:
        q = params.copy()
        q['cursor'] = cursor or ''
        js = await _req_json(client, 'GET', url, params=q, headers={'User-Agent': UA, 'Referer': 'https://www.roblox.com/', 'Accept': 'application/json, text/plain, */*'})
        data = js.get('data') or []
        for it in data:
            aid = it.get('assetId') or it.get('id') or 0
            if not aid:
                continue
            items.append({'assetId': int(aid), 'name': it.get('name') or str(aid), 'assetType': asset_type})
        cursor = js.get('nextPageCursor')
        if not cursor:
            break
    return items

async def _fetch_catalog_prices(client_seed: httpx.AsyncClient, asset_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if not asset_ids:
        return out
    url = 'https://catalog.roblox.com/v1/catalog/items/details'
    chunks = [asset_ids[i:i + CATALOG_BATCH_SIZE] for i in range(0, len(asset_ids), CATALOG_BATCH_SIZE)]
    sem = asyncio.Semaphore(CATALOG_CONCURRENCY)

    async def one(idx: int, chunk: List[int]):
        payload = {'items': [{'id': int(x), 'itemType': 'Asset'} for x in chunk]}
        await asyncio.sleep(idx % CATALOG_CONCURRENCY * (CATALOG_BASE_DELAY_MS / 1000.0) * 0.5)
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        token = await _ensure_csrf(client)
        headers = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*', 'Origin': 'https://www.roblox.com', 'Referer': 'https://www.roblox.com/'}
        if token:
            headers['X-CSRF-TOKEN'] = token
        backoff = 0.4
        for attempt in range(0, CATALOG_RETRIES):
            try:
                async with sem:
                    await _throttle()
                    r = await client.post(url, json=payload, headers=headers, timeout=CFG.TIMEOUT)
                    if r.status_code == 429:
                        await asyncio.sleep(backoff + random.uniform(0, 0.3))
                        proxy = PROXY_POOL.any()
                        client = await get_client(proxy)
                        token = await _ensure_csrf(client)
                        if token:
                            headers['X-CSRF-TOKEN'] = token
                        backoff = min(backoff * 1.7, 3.0)
                        if attempt < CATALOG_RETRIES - 1:
                            continue
                    if r.status_code == 403:
                        token = r.headers.get('x-csrf-token') or await _ensure_csrf(client)
                        if token:
                            headers['X-CSRF-TOKEN'] = token
                        if attempt < CATALOG_RETRIES - 1:
                            await asyncio.sleep(0.25 * (attempt + 1))
                            continue
                    r.raise_for_status()
                    js = r.json()
                for d in js or []:
                    aid = d.get('id')
                    if aid is None:
                        continue
                    out[int(aid)] = {'priceInfo': _price_info_from_detail(d), 'name': d.get('name') or str(aid)}
                return
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (500, 502, 503, 504) and attempt < CATALOG_RETRIES - 1:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise
            except Exception:
                if attempt < CATALOG_RETRIES - 1:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                raise
    await asyncio.gather(*(one(i, ch) for i, ch in enumerate(chunks)))
    return out

async def fetch_public_inventory(user_id: int) -> Dict[str, Any]:
    key = f'pub:inv:v2:{user_id}'
    cached = await cache.get_json(key, INV_TTL)
    if cached:
        return cached
    proxy = PROXY_POOL.any()
    client = await get_client(proxy)
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    all_ids: List[int] = []
    for asset_type, cat_name in ASSET_TYPES:
        arr = await _fetch_inventory_for_type(client, user_id, asset_type)
        by_cat[cat_name] = arr
        all_ids.extend([x['assetId'] for x in arr])
    price_map = await _fetch_catalog_prices(client, all_ids)
    for cat_name, arr in by_cat.items():
        for it in arr:
            aid = it['assetId']
            extra = price_map.get(aid) or {}
            if 'name' not in it and extra.get('name'):
                it['name'] = extra['name']
            it['priceInfo'] = extra.get('priceInfo') or {'value': 0, 'source': 'unknown'}
    result = {'userId': user_id, 'byCategory': by_cat}
    await cache.set_json(key, result)
    return result