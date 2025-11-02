from __future__ import annotations
import os, csv, io, asyncio, argparse
from typing import List, Dict, Any, Optional
import httpx
from PIL import Image
from http_shared import get_client, PROXY_POOL
LOG_DIR = os.getenv('IMAGEGEN_LOG_DIR', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
OUT_PRICES = os.getenv('PRICE_DUMP_PATH', os.path.join(LOG_DIR, 'prices.csv'))
OUT_IDS = os.getenv('SCAN_OUT_IDS', os.path.join(LOG_DIR, 'catalog_ids.txt'))
READY_ITEM_DIR = os.getenv('READY_ITEM_DIR', 'item_images')
WRITE_READY = os.getenv('WRITE_READY_ITEM_IMAGES', 'true').strip().lower() in ('1', 'true', 'yes', 'y', 'on')
os.makedirs(READY_ITEM_DIR, exist_ok=True)
HTTP_TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '10.0'))
HTTP_CONNECT_TIMEOUT = float(os.getenv('HTTP_CONNECT_TIMEOUT', '2.0'))
HTTP_READ_TIMEOUT = float(os.getenv('HTTP_READ_TIMEOUT', '8.0'))
SEARCH_CONCURRENCY = int(os.getenv('SCAN_CONCURRENCY', '16'))
DETAILS_CONCURRENCY = int(os.getenv('CATALOG_CONCURRENCY', '12'))
THUMB_CONCURRENCY = int(os.getenv('THUMB_DL_CONCURRENCY', '24'))
BATCH_SIZE = int(os.getenv('CATALOG_BATCH', '120'))
SEARCH_URL = 'https://catalog.roblox.com/v1/search/items'
DETAILS_URL = 'https://catalog.roblox.com/v1/catalog/items/details'
THUMB_URL = 'https://thumbnails.roblox.com/v1/assets'
CSRF_HEADER = 'x-csrf-token'

def _read_cookie_from_file(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:
            s = f.read().strip()
            return s or None
    except Exception:
        return None

def _resolve_cookie(cli_cookie: Optional[str], cli_cookie_file: Optional[str]) -> Optional[str]:
    if cli_cookie:
        return cli_cookie.strip()
    env_path = os.getenv('ROBLOX_COOKIE_PATH')
    cookie = _read_cookie_from_file(cli_cookie_file or env_path)
    if cookie:
        return cookie
    env_cookie = os.getenv('ROBLOSECURITY') or os.getenv('ROBLOX_COOKIE')
    return env_cookie.strip() if env_cookie else None

def _auth_headers(cookie: Optional[str]) -> Dict[str, str]:
    return {'Cookie': f'.ROBLOSECURITY={cookie}'} if cookie else {}

def _norm_price(v) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = v.strip().replace(',', '.').replace(' ', '')
            for t in ('R$', '₽', 'RBX', 'robux', 'руб', '$'):
                s = s.replace(t, '')
            return int(float(s))
    except Exception:
        return 0
    return 0

def _pick_price(d: Dict[str, Any]) -> int:
    vals = [_norm_price(d.get('price')), _norm_price(d.get('lowestPrice')), _norm_price(d.get('lowestResalePrice')), _norm_price(d.get('highestResalePrice'))]
    return max(vals) if any(vals) else 0

def _is_collectible(d: Dict[str, Any]) -> bool:
    v = d.get('Collectible')
    if isinstance(v, list):
        return any((str(x).strip().lower() == 'collectible' for x in v if x is not None))
    if isinstance(v, str):
        return v.strip().lower() == 'collectible'
    if isinstance(v, bool):
        return bool(v)
    return False

def _save_ready(aid: int, im: Image.Image):
    if not WRITE_READY:
        return
    p = os.path.join(READY_ITEM_DIR, f'{aid}.png')
    if not os.path.exists(p):
        try:
            im.save(p, 'PNG')
        except Exception:
            pass

async def _get(session: httpx.AsyncClient, url: str, *, headers: Optional[Dict[str, str]]=None, **kw):
    return await session.get(url, headers=headers or {}, timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT), **kw)

async def search_catalog_ids(keywords: List[str], asset_types: List[int], pages: int, cookie: Optional[str]) -> List[int]:
    proxy = PROXY_POOL.any()
    client = await get_client(proxy)
    ids: List[int] = []
    if not keywords:
        keywords = ['']
    if not asset_types:
        asset_types = []
    for kw in keywords:
        for at in asset_types or [None]:
            cursor = None
            passed = 0
            params = {'Category': 'All', 'Limit': 120, 'SortAggregation': 'PastDay', 'SortType': 'Relevance'}
            if kw:
                params['Keyword'] = kw
            if at:
                params['AssetType'] = int(at)
            while passed < pages:
                q = dict(params)
                if cursor:
                    q['Cursor'] = cursor
                try:
                    r = await _get(client, SEARCH_URL, params=q, headers=_auth_headers(cookie))
                    r.raise_for_status()
                    js = r.json()
                    data = js.get('data') or []
                    for it in data:
                        aid = it.get('id') or it.get('assetId')
                        if aid is not None:
                            try:
                                ids.append(int(aid))
                            except Exception:
                                pass
                    cursor = js.get('nextPageCursor')
                    passed += 1
                    if not cursor:
                        break
                except Exception:
                    break
                await asyncio.sleep(0)
    seen = set()
    out = []
    for a in ids:
        if a not in seen:
            out.append(a)
            seen.add(a)
    os.makedirs(os.path.dirname(OUT_IDS) or '.', exist_ok=True)
    with open(OUT_IDS, 'w', encoding='utf-8') as f:
        for a in out:
            f.write(str(a) + '\n')
    print(f'[scan] saved {len(out)} ids -> {OUT_IDS}')
    return out

async def fetch_details(asset_ids: List[int], cookie: Optional[str]) -> List[Dict[str, Any]]:
    if not asset_ids:
        return []
    sem = asyncio.Semaphore(DETAILS_CONCURRENCY)
    CSRF_HEADER = 'x-csrf-token'

    async def post_once(batch, csrf=None):
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers.update(_auth_headers(cookie))
        if csrf:
            headers[CSRF_HEADER] = csrf
        r = await client.post(DETAILS_URL, json={'items': batch}, headers=headers, timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT))
        return r

    async def one(batch):
        async with sem:
            csrf = None
            for _ in range(4):
                try:
                    resp = await post_once(batch, csrf=csrf)
                    if resp.status_code == 403 and CSRF_HEADER in resp.headers:
                        csrf = resp.headers.get(CSRF_HEADER)
                        continue
                    resp.raise_for_status()
                    js = resp.json()
                    if isinstance(js, dict) and 'data' in js:
                        return js['data']
                    if isinstance(js, list):
                        return js
                    return []
                except Exception:
                    await asyncio.sleep(0.25)
            return []
    tasks = []
    for i in range(0, len(asset_ids), BATCH_SIZE):
        items = [{'id': int(a), 'itemType': 'Asset'} for a in asset_ids[i:i + BATCH_SIZE]]
        tasks.append(asyncio.create_task(one(items)))
    blocks = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, list):
            out.extend(b)
    return out

def dump_prices_csv(rows: List[Dict[str, Any]], path: str, mode: str='w') -> None:
    write_header = mode == 'w' or not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, mode, encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(['itemId', 'name', 'pricePicked', 'collectible'])
        for r in rows:
            w.writerow([r['id'], r['name'], r['picked'], 'Collectible' if r.get('collectible') else ''])

async def fetch_thumb_and_save(aid: int, cookie: Optional[str]) -> None:
    proxy = PROXY_POOL.any()
    client = await get_client(proxy)
    try:
        r = await _get(client, THUMB_URL, params={'assetIds': str(aid), 'size': '420x420', 'format': 'Png', 'isCircular': 'false'}, headers=_auth_headers(cookie))
        r.raise_for_status()
        js = r.json()
        data = js.get('data') or []
        url = data[0].get('imageUrl') if data else None
        if not url:
            return
        rr = await _get(client, url, headers=_auth_headers(cookie))
        rr.raise_for_status()
        im = Image.open(io.BytesIO(rr.content)).convert('RGBA')
        _save_ready(aid, im)
    except Exception:
        return

async def warm_thumbs(ids: List[int], cookie: Optional[str]) -> None:
    if not WRITE_READY:
        print('[thumbs] WRITE_READY_ITEM_IMAGES is disabled, skip')
        return
    sem = asyncio.Semaphore(THUMB_CONCURRENCY)

    async def one(a):
        async with sem:
            await fetch_thumb_and_save(a, cookie)
    await asyncio.gather(*(one(a) for a in ids))

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keywords', nargs='*', default=[], help='Список ключевых слов для поиска')
    ap.add_argument('--asset-types', nargs='*', default=[], help='Список типов ассетов (числа)')
    ap.add_argument('--pages', type=int, default=40, help='Сколько страниц на связку ключ/тип (по 120 на страницу)')
    ap.add_argument('--append', action='store_true', help='Не перезаписывать prices.csv, а дописывать')
    ap.add_argument('--skip-thumbs', action='store_true', help='Не качать превью (только CSV)')
    ap.add_argument('--cookie', type=str, default=None, help='ROBLOSECURITY значение')
    ap.add_argument('--cookie-file', type=str, default=None, help='Путь к файлу с ROBLOSECURITY')
    args = ap.parse_args()
    at = [int(x) for x in args.asset_types if str(x).isdigit()]
    cookie = _resolve_cookie(args.cookie, args.cookie_file)
    if not args.keywords and (not at):
        at = [8, 11, 12, 18, 17, 19, 38]
        args.keywords = ['', 'collectible', 'hat', 'face', 'classic', 'rare']
    ids = await search_catalog_ids(args.keywords, at, args.pages, cookie)
    if not ids:
        print('ничего не нашли, выходим')
        return 0
    dets = await fetch_details(ids, cookie)
    by_id = {int(d.get('id')): d for d in dets if d.get('id') is not None}
    rows = []
    for aid in ids:
        d = by_id.get(int(aid), {}) or {}
        name = d.get('name') or f'Item {aid}'
        picked = _pick_price(d)
        rows.append({'id': int(aid), 'name': name, 'picked': picked, 'collectible': _is_collectible(d)})
    dump_prices_csv(rows, OUT_PRICES, mode='a' if args.append else 'w')
    print(f'[dump] wrote {len(rows)} rows -> {OUT_PRICES}')
    if not args.skip_thumbs:
        await warm_thumbs(ids, cookie)
        print(f'[thumbs] warmed into {READY_ITEM_DIR} (enabled={WRITE_READY})')
    return 0
if __name__ == '__main__':
    asyncio.run(main())