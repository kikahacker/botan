from __future__ import annotations
import os, io, math, json, asyncio, hashlib, datetime, logging, time, csv
from i18n import tr, get_current_lang

from datetime import datetime as _dt2
LOG_PRICE_PATH = os.path.join(os.path.dirname(__file__), "price_debug.log")
PRICE_CSV_PATH = os.getenv('PRICE_CSV_PATH', os.path.join(os.path.dirname(__file__), 'prices.csv'))
def _log_price_event(text: str):
    ts = _dt2.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {text}"
    try:
        with open(LOG_PRICE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        logger.info(line)
    except Exception:
        pass

# ---- helpers for robust ID/price parsing ----
def _to_int(v) -> int:
    try:
        if v is None:
            return 0
        s = str(v).strip().replace(',', '').replace(' ', '')
        if s.isdigit():
            return int(s)
        import re as _re
        m = _re.search(r'\d+', s)
        return int(m.group(0)) if m else 0
    except Exception:
        return 0

def _enrich_with_csv(it: dict, price_map: dict) -> dict:
    keys = [
        int(it.get('itemId') or 0),
        int(it.get('assetId') or 0),
        int(it.get('collectibleItemId') or it.get('collectibleId') or 0),
        int(it.get('id') or 0),
    ]
    rec = None
    for k in keys:
        if k and k in price_map:
            rec = price_map[k]
            break

    # fallback by name
    if not rec and it.get('name'):
        n = str(it.get('name') or '').strip().lower()
        for r in price_map.values():
            if str(r.get('name') or '').strip().lower() == n:
                rec = r
                break

    name = (it.get('name') or '').strip()
    pid = it.get('itemId') or it.get('assetId') or it.get('id')
    if rec:
        cur_name = str(it.get('name') or '').strip()
        csv_name = str(rec.get('name') or '').strip()
        pid_str = str(pid)
        # override name if empty OR looks like numeric id OR equals pid
        if (not cur_name) or cur_name.isdigit() or (cur_name == pid_str):
            if csv_name:
                it['name'] = csv_name
        it['priceInfo'] = {'value': int(str((rec.get('priceInfo') or {}).get('value') or 0))}
        _log_price_event(f"[PRICE_HIT] {name!r} (id={pid}) -> price={(rec.get('priceInfo') or {}).get('value')}")
    else:
        _log_price_event(f"[PRICE_MISS] {name!r} (id={pid}) -> not found in CSV")
    return it
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

# =========================
# Config & Logging
# =========================
READY_ITEM_DIR = os.path.join(os.getcwd(), "item_images")
os.makedirs(READY_ITEM_DIR, exist_ok=True)

LOG_DIR = os.getenv("IMAGEGEN_LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("imagegen")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOG_DIR, "imagegen.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(fh)

DEBUG_IMAGEGEN = str(os.getenv("DEBUG_IMAGEGEN", "0")).lower() in ("1","true","yes","on","y")

def _dbg(msg: str):
    if DEBUG_IMAGEGEN:
        logger.debug(msg)

def _info(msg: str):
    logger.info(msg)

def _err(prefix: str, e: Exception):
    logger.error(f"{prefix}: {type(e).__name__}: {e}", exc_info=True)

_info(f"[imagegen] cwd={os.getcwd()} READY_ITEM_DIR={os.path.abspath(READY_ITEM_DIR)} DEBUG={DEBUG_IMAGEGEN}")
_info(f"[prices] using CSV: {PRICE_CSV_PATH}")

# =========================
# External deps
# =========================
from http_shared import get_client, PROXY_POOL
from config import CFG
import cache

# =========================
# Tunables
# =========================
IMG_TTL = int(getattr(CFG, 'CACHE_IMG_TTL', 3600))
THUMB_TTL = int(getattr(CFG, 'THUMB_TTL', 86400))
HTTP_TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '10.0'))
HTTP_CONNECT_TIMEOUT = float(os.getenv('HTTP_CONNECT_TIMEOUT', '2.0'))
HTTP_READ_TIMEOUT = float(os.getenv('HTTP_READ_TIMEOUT', '8.0'))
THUMB_DL_CONCURRENCY = int(getattr(CFG, 'THUMB_DL_CONCURRENCY', 24))
THUMB_BATCH_CONCURRENCY = int(getattr(CFG, 'THUMB_BATCH_CONCURRENCY', 8))
RENDER_CONCURRENCY = int(os.getenv('RENDER_CONCURRENCY', str(max(4, (os.cpu_count() or 8) - 1))))

# Speed-first repoll: helps first-run latency a lot.
THUMB_REPOLL_DELAYS = [0.15, 0.30, 0.60]

ASSETS_DIR = getattr(CFG, 'ASSETS_DIR', 'assets')
CANVAS_BG_PATH = getattr(CFG, 'CANVAS_BG', os.path.join(ASSETS_DIR, 'canvas_bg.png'))

WRITE_READY_ITEM_IMAGES = str(os.getenv('WRITE_READY_ITEM_IMAGES', '0')).lower() in ("1","true","yes","on","y")
ROBUX_PREFIX = os.getenv('ROBUX_PREFIX', 'R$')
KEEP_INPUT_ORDER = str(os.getenv('KEEP_INPUT_ORDER', '0')).lower() in ("1","true","yes","on","y")

# Faster encoding (optimize=True can be very slow on large sheets)
PNG_OPTIMIZE = str(os.getenv('PNG_OPTIMIZE', '0')).lower() in ("1","true","yes","on","y")

# Main switch: download thumbnails at this size (independent from tile)
THUMB_SIZE = os.getenv('THUMB_SIZE', '420x420')
THUMB_SIZE_FORCE = str(os.getenv('THUMB_SIZE_FORCE', '0')).lower() in ("1","true","yes","on","y")

# Layout
PADDING_CONTENT = int(os.getenv('PADDING_CONTENT', '0'))
GAP_IMAGE_PRICE = int(os.getenv('GAP_IMAGE_PRICE', '6'))
GAP_IMAGE_TEXT = int(os.getenv('GAP_IMAGE_TEXT', '6'))
TITLE_SINGLE_LINE = str(os.getenv('TITLE_SINGLE_LINE', 'false')).strip().lower() in ('1','true','yes','y','on')
THEME_CLASSIC_BLUE = str(os.getenv('THEME_CLASSIC_BLUE', 'false')).strip().lower() in ('1','true','yes','y','on')
LINE_SCALE = float(os.getenv('LINE_SCALE', '1.0'))

SHOW_HEADER = str(os.getenv('SHOW_HEADER', 'true')).strip().lower() in ('1','true','yes','y','on')
SHOW_FOOTER = str(os.getenv('SHOW_FOOTER', 'true')).strip().lower() in ('1','true','yes','y','on')
HEADER_H = int(os.getenv('HEADER_H', '76'))
FOOTER_H = int(os.getenv('FOOTER_H', '140'))
FOOTER_ICON = os.getenv('FOOTER_ICON', os.path.join(ASSETS_DIR, 'footer_badge.png'))
FOOTER_BRAND = os.getenv('FOOTER_BRAND', 'raika.gg')

# Style for price pill and title
TITLE_TEXT_COLOR = (255, 255, 255, 255)
PRICE_TEXT_COLOR = (0, 0, 0, 255)

# pill + colors via ENV
def _rgba_env(key: str, default: str) -> tuple:
    raw = os.getenv(key, default)
    try:
        parts = [int(x.strip()) for x in str(raw).split(',')]
        if len(parts) == 3:
            parts.append(255)
        if len(parts) == 4:
            return (parts[0], parts[1], parts[2], parts[3])
    except Exception:
        pass
    r, g, b, *rest = [int(x) for x in default.split(',')]
    a = rest[0] if rest else 255
    return (r, g, b, a)

PRICE_PILL_PAD_X = int(os.getenv('PRICE_PILL_PAD_X', '7'))
PRICE_PILL_PAD_Y = int(os.getenv('PRICE_PILL_PAD_Y', '4'))
PRICE_TOP_PAD_PX = int(os.getenv('PRICE_TOP_PAD_PX', '6'))
PRICE_PILL_OUTLINE_PX = int(os.getenv('PRICE_PILL_OUTLINE_PX', '1'))
PRICE_PILL_FILL = _rgba_env('PRICE_PILL_FILL', '255,255,255,235')
PRICE_PILL_OUTLINE = _rgba_env('PRICE_PILL_OUTLINE', '0,0,0,200')
# 0 = авто (pill_h//2). Любое >0 — берём твоё, но клемпим, чтобы не стало овалом.
PRICE_PILL_RADIUS_PX = int(os.getenv('PRICE_PILL_RADIUS_PX', '0'))

TEXT_BOTTOM_PAD_PX = 6
TITLE_FONT_TILE_DIV = int(os.getenv('TITLE_FONT_TILE_DIV', '7'))
PRICE_FONT_TILE_DIV = int(os.getenv('PRICE_FONT_TILE_DIV', '8'))


# =========================
# Pricing tiers (only for bg)
# =========================

def _load_price_rules():
    raw = getattr(CFG, 'PRICE_RULES', '') or os.environ.get('PRICE_RULES', '')
    if not raw:
        return None
    try:
        rules = json.loads(raw)
        out = []
        for r in rules:
            try:
                out.append({'name': str(r.get('name')), 'min': int(float(r.get('min', 0))), 'bg': r.get('bg'), 'stripe': r.get('stripe')})
            except Exception:
                pass
        out.sort(key=lambda x: x['min'], reverse=True)
        return out or None
    except Exception as e:
        _dbg(f'PRICE_RULES parse fail: {e}')
        return None

RULES_JSON = _load_price_rules()
FALLBACK_THRESHOLDS = [(1000, 'gold'), (500, 'orange'), (200, 'purple'), (0, 'blue')]
DEFAULT_TIER_BACKGROUNDS = {
    'gold': getattr(CFG, 'BG_TIER_GOLD', os.path.join(ASSETS_DIR, 'bg_tier_gold.png')),
    'orange': getattr(CFG, 'BG_TIER_ORANGE', os.path.join(ASSETS_DIR, 'bg_tier_orange.png')),
    'purple': getattr(CFG, 'BG_TIER_PURPLE', os.path.join(ASSETS_DIR, 'bg_tier_purple.png')),
    'blue': getattr(CFG, 'BG_TIER_BLUE', os.path.join(ASSETS_DIR, 'bg_tier_blue.png')),
    'common': os.path.join(ASSETS_DIR, 'bg_tier_common.png'),
}


def _tier_by_price(price: int) -> str:
    if RULES_JSON:
        for r in RULES_JSON:
            if price >= r['min']:
                return r['name']
        return RULES_JSON[-1]['name']
    for th, nm in FALLBACK_THRESHOLDS:
        if price >= th:
            return nm
    return 'common'


def _paths_for_tier(name: str):
    if RULES_JSON:
        for r in RULES_JSON:
            if r['name'].lower() == name.lower():
                return (r.get('bg'), r.get('stripe'))
        return (None, None)
    return (DEFAULT_TIER_BACKGROUNDS.get(name), None)

# =========================
# Fonts & drawing utils
# =========================
_FONT, _BOLD_FONT = ({}, {})

def _font(sz):
    if 'FORTNITEBATTLEFEST.OTF' in '':
        pass
    try:
        f = ImageFont.truetype('font/FORTNITEBATTLEFEST.OTF', sz)
    except Exception:
        f = ImageFont.load_default()
    return f

def _bold_font(sz):
    return _font(sz)



def _make_grad(w, h, c1, c2):
    im = Image.new('RGBA', (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        d.line([(0, y), (w, y)], fill=(r, g, b, 255))
    return im

_canvas_bg_cache: Dict[tuple, Image.Image] = {}

def _get_canvas_bg(W, H):
    key = (W, H)
    im = _canvas_bg_cache.get(key)
    if im:
        return im
    if THEME_CLASSIC_BLUE:
        im = _make_grad(W, H, (19, 120, 255), (15, 99, 255))
    else:
        try:
            base = Image.open(CANVAS_BG_PATH).convert('RGBA').resize((W, H), Image.BILINEAR)
            im = base
        except Exception:
            im = _make_grad(W, H, (180, 210, 235), (140, 175, 215))
    _canvas_bg_cache[key] = im
    return im

_bg_cache: Dict[tuple, Image.Image] = {}

def _get_tier_bg(tier: str, tile: int):
    key = (tier, tile)
    im = _bg_cache.get(key)
    if im:
        return im
    bg, _ = _paths_for_tier(tier)
    try:
        if bg and os.path.exists(bg):
            im = Image.open(bg).convert('RGBA').resize((tile, tile), Image.BILINEAR)
        else:
            raise FileNotFoundError
    except Exception:
        # fallback gradient by tier
        t = tier.lower()
        if t in ('gold', 'mythic'):
            im = _make_grad(tile, tile, (250, 200, 80), (180, 120, 20))
        elif t in ('orange', 'legendary'):
            im = _make_grad(tile, tile, (230, 150, 40), (140, 80, 10))
        elif t in ('purple', 'epic'):
            im = _make_grad(tile, tile, (170, 120, 230), (90, 40, 160))
        elif t in ('blue', 'rare'):
            im = _make_grad(tile, tile, (40, 150, 245), (20, 60, 130))
        else:
            im = _make_grad(tile, tile, (200, 200, 200), (120, 120, 120))
    _bg_cache[key] = im
    return im

# =========================
# Local images & auth
# =========================
ROBLOSECURITY = os.getenv('ROBLOSECURITY') or os.getenv('ROBLOX_COOKIE')

def _auth_headers():
    return {'Cookie': f'.ROBLOSECURITY={ROBLOSECURITY}'} if ROBLOSECURITY else {}

def _read_ready_item(aid: int) -> Optional[Image.Image]:
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        p = os.path.join(READY_ITEM_DIR, f'{aid}{ext}')
        if os.path.exists(p):
            try:
                im = Image.open(p).convert('RGBA')
                _dbg(f"[local] hit {aid}{ext} size={im.size}")
                return im
            except Exception as e:
                _err(f"[local] open fail for {p}", e)
    _dbg(f"[local] miss {aid}")
    return None

def _write_ready_item(aid: int, im: Image.Image):
    if not WRITE_READY_ITEM_IMAGES:
        return
    try:
        p = os.path.join(READY_ITEM_DIR, f"{aid}.png")
        tmp = p + ".tmp"
        im.save(tmp, format='PNG')
        os.replace(tmp, p)
        _dbg(f"[local] saved {p}")
    except Exception as e:
        _err("[local] save fail", e)

# =========================
# Network fetch with cache (THUMB_SIZE enforced)
# =========================
async def _download_image_with_cache(url: str) -> Optional[Image.Image]:
    key = 'thumb:' + hashlib.sha1(url.encode()).hexdigest()
    b = await cache.get_bytes(key, THUMB_TTL)
    if b:
        try:
            _dbg(f"[thumb] cache HIT url={url}")
            return Image.open(io.BytesIO(b)).convert('RGBA')
        except Exception as e:
            _err("[thumb] cache decode fail", e)
    _dbg(f"[thumb] cache MISS url={url}")
    for attempt in range(4):
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        try:
            r = await client.get(url, headers=_auth_headers(), timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT))
            if r.status_code != 200:
                _info(f"[thumb] GET status={r.status_code} attempt={attempt+1} url={url[:120]}")
            r.raise_for_status()
            data = r.content
            await cache.set_bytes(key, data)
            _dbg(f"[thumb] downloaded {len(data)}b")
            return Image.open(io.BytesIO(data)).convert('RGBA')
        except Exception as e:
            _err(f"[thumb] fetch fail try {attempt+1}", e)
            await asyncio.sleep(0.2 * (attempt + 1))
    return None

async def _fetch_thumbs(ids: List[int], size: str='150x150') -> Dict[int, Image.Image]:
    result: Dict[int, Image.Image] = {}
    left: List[int] = []
    for aid in ids:
        imr = _read_ready_item(int(aid))
        if imr is not None:
            result[int(aid)] = imr
        else:
            left.append(int(aid))
    _info(f"[thumb] input={len(ids)} local_hits={len(result)} need_fetch={len(left)} size={size}")
    if not left:
        return result

    base = 'https://thumbnails.roblox.com/v1/assets'
    legacy = 'https://www.roblox.com/asset-thumbnail/image'

    async def one_batch(ch: List[int]):
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        try:
            r = await client.get(
                base,
                params={'assetIds': ','.join(map(str, ch)), 'size': size, 'format': 'Png', 'isCircular': 'false', 'returnPolicy': 'PlaceHolder'},
                headers=_auth_headers(),
                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT)
            )
            _info(f"[thumb] batch {ch[0]}..{ch[-1]} count={len(ch)} size={size} status={r.status_code}")
            r.raise_for_status()
            def parse(js):
                urls, pending = ({}, [])
                for rec in js.get('data', []):
                    aid = int(rec.get('targetId'))
                    url = rec.get('imageUrl')
                    state = rec.get('state')
                    if url:
                        urls[aid] = url
                    if state and state != 'Completed':
                        pending.append(aid)
                return (urls, pending)
            urls, pending = parse(r.json())
            _dbg(f"[thumb] urls={len(urls)} pending={len(pending)}")
            for d in THUMB_REPOLL_DELAYS:
                if not pending:
                    break
                await asyncio.sleep(d)
                rr = await client.get(
                    base,
                    params={'assetIds': ','.join(map(str, pending)), 'size': size, 'format': 'Png', 'isCircular': 'false', 'returnPolicy': 'PlaceHolder'},
                    headers=_auth_headers(),
                    timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT)
                )
                _dbg(f"[thumb] repoll status={rr.status_code} pend_before={len(pending)} delay={d}")
                if rr.status_code == 200:
                    got, next_pend = parse(rr.json())
                    urls.update(got)
                    pending = [a for a in next_pend if a not in urls]
                else:
                    break
            if pending:
                w, h = size.split('x')
                _info(f"[thumb] legacy_fallback count={len(pending)} size={size}")
                for aid in pending:
                    urls[aid] = f'{legacy}?assetId={aid}&width={w}&height={h}&format=png'
            return urls
        except Exception as e:
            _err(f"[thumb] batch fail {ch[:3]}..", e)
            return {}

    batches = [left[i:i + 100] for i in range(0, len(left), 100)]
    sem_b = asyncio.Semaphore(THUMB_BATCH_CONCURRENCY)

    async def guarded(b):
        async with sem_b:
            return await one_batch(b)

    maps = await asyncio.gather(*(guarded(b) for b in batches))
    url_map: Dict[int, str] = {}
    for m in maps:
        url_map.update(m)

    sem_dl = asyncio.Semaphore(THUMB_DL_CONCURRENCY)

    async def dl(aid, url):
        async with sem_dl:
            im = await _download_image_with_cache(url)
            if im is not None:
                result[int(aid)] = im
                if WRITE_READY_ITEM_IMAGES:
                    _write_ready_item(int(aid), im)
            else:
                _info(f"[thumb] empty for {aid} (url={url[:100]})")

    await asyncio.gather(*(dl(a, u) for a, u in url_map.items()))
    _info(f"[thumb] fetch done total={len(ids)} have={len(result)} missing={len(ids)-len(result)}")
    return result



# =========================
# Fast local I/O caches (30 min TTL)
# =========================

_IMAGE_INDEX: Dict[int, str] = {}
_IMAGE_INDEX_TS: float = 0.0
_IMAGE_DIR = READY_ITEM_DIR
_IMAGE_TTL_SEC = 1800  # 30 min
_VALID_EXT = {'.png', '.jpg', '.jpeg', '.webp'}

def _build_image_index_cached(force: bool=False):
    global _IMAGE_INDEX, _IMAGE_INDEX_TS
    now = time.time()
    if not force and _IMAGE_INDEX and (now - _IMAGE_INDEX_TS) < _IMAGE_TTL_SEC:
        return
    idx: Dict[int, str] = {}
    try:
        with os.scandir(_IMAGE_DIR) as it:
            for e in it:
                if not e.is_file():
                    continue
                root, ext = os.path.splitext(e.name)
                if ext.lower() not in _VALID_EXT:
                    continue
                try:
                    aid = int(root)
                except Exception:
                    continue
                idx[aid] = e.path
    except FileNotFoundError:
        idx = {}
    _IMAGE_INDEX = idx
    _IMAGE_INDEX_TS = now

# Override local image reader to use index first
def _read_ready_item(aid: int) -> Optional[Image.Image]:  # type: ignore[func-override]
    p = _IMAGE_INDEX.get(int(aid))
    if p and os.path.exists(p):
        try:
            return Image.open(p).convert('RGBA')
        except Exception as e:
            _err(f"[local] open fail for {p}", e)
            return None
    # Fallback legacy probing
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        q = os.path.join(READY_ITEM_DIR, f'{aid}{ext}')
        if os.path.exists(q):
            try:
                return Image.open(q).convert('RGBA')
            except Exception as e:
                _err(f"[local] open fail for {q}", e)
    return None

# Cached prices.csv reader (30 min TTL)
_PRICES_CACHE: Optional[Dict[int, Dict[str, Any]]] = None
_PRICES_TS: float = 0.0
_PRICES_MTIME: float = -1.0
_PRICES_TTL_SEC = 1800  # 30 min

def load_prices_csv_cached(path: str = PRICE_CSV_PATH) -> Dict[int, Dict[str, Any]]:
    global _PRICES_CACHE, _PRICES_TS, _PRICES_MTIME
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return {}
    now = time.time()
    if (_PRICES_CACHE is not None) and ((now - _PRICES_TS) < _PRICES_TTL_SEC) and (_PRICES_MTIME == mtime):
        return _PRICES_CACHE

    out: Dict[int, Dict[str, Any]] = {}
    import csv
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for row in reader:
                    r = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
                    id_raw = r.get("itemid") or r.get("collectibleitemid") or r.get("collectibleid") or r.get("assetid") or r.get("id")
                    if not id_raw:
                        continue
                    aid = _to_int(id_raw)
                    name = r.get("name", "")
                    price_field = r.get("pricepicked") or r.get("price") or r.get("value") or r.get("cost")
                    price_val = _to_int(price_field or 0)
                    out[aid] = {"name": name, "priceInfo": {"value": price_val}}
            else:
                f.seek(0)
                reader2 = csv.reader(f)
                for row in reader2:
                    if not row or len(row) < 3:
                        continue
                    aid = _to_int(row[0]); name = (row[1] or "").strip(); price_val = _to_int(row[2])
                    out[aid] = {"name": name, "priceInfo": {"value": price_val}}
    except Exception as e:
        _err("[prices] read fail", e)

    _PRICES_CACHE = out
    _PRICES_TS = now
    _PRICES_MTIME = mtime
    return out
# =========================
# Helpers
# =========================

def _num(v):
    try:
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        return int(float(v))
    except Exception:
        return 0


def _price_of(it: Dict[str, Any]) -> int:
    v = it.get('priceInfo', {}).get('value')
    return _num(v)


def _tier_color(name: str):
    nm = name.lower()
    if nm in ('gold', 'mythic'):
        return (250, 200, 80)
    if nm in ('orange', 'legendary'):
        return (230, 150, 40)
    if nm in ('purple', 'epic'):
        return (170, 120, 230)
    if nm in ('blue', 'rare'):
        return (40, 150, 245)
    return (240, 240, 240)

# =========================
# Tile rendering (price pill top-right, title bottom, NO stripe)
# =========================

# ---- render caches (for first-run speed) ----
_LAYOUT_CACHE: Dict[int, Dict[str, Any]] = {}
_BASE_TILE_CACHE: Dict[tuple, Image.Image] = {}
_PILL_BG_CACHE: Dict[tuple, Image.Image] = {}

try:
    from collections import OrderedDict
except Exception:
    OrderedDict = dict  # type: ignore

_TILE_MEM_MAX = int(os.getenv('TILE_MEM_MAX', '256'))
_TILE_MEM: "OrderedDict[str, Image.Image]" = OrderedDict()  # type: ignore

def _tile_mem_get(key: str) -> Optional[Image.Image]:
    try:
        im = _TILE_MEM.get(key)
        if im is None:
            return None
        # move to end (LRU)
        try:
            _TILE_MEM.move_to_end(key)  # type: ignore[attr-defined]
        except Exception:
            pass
        return im
    except Exception:
        return None

def _tile_mem_put(key: str, im: Image.Image):
    try:
        _TILE_MEM[key] = im
        try:
            _TILE_MEM.move_to_end(key)  # type: ignore[attr-defined]
        except Exception:
            pass
        while len(_TILE_MEM) > _TILE_MEM_MAX:
            try:
                _TILE_MEM.popitem(last=False)  # type: ignore[attr-defined]
            except Exception:
                # fallback: clear all
                _TILE_MEM.clear()
                break
    except Exception:
        pass

def _layout_for_tile(tile: int) -> Dict[str, Any]:
    """Cache all repeated geometry/font calculations for a tile size."""
    info = _LAYOUT_CACHE.get(tile)
    if info:
        return info

    title_font = _bold_font(max(12, tile // max(1, TITLE_FONT_TILE_DIV)))
    price_font = _bold_font(max(10, tile // max(1, PRICE_FONT_TILE_DIV)))

    # measure line height
    try:
        line_h = title_font.getbbox('Ag')[3]
    except Exception:
        line_h = 18

    # pill height is stable for a given tile
    pill_h = price_font.getbbox('Ag')[3] + PRICE_PILL_PAD_Y * 2 - 2
    radius_val = PRICE_PILL_RADIUS_PX if PRICE_PILL_RADIUS_PX > 0 else (pill_h // 2)
    radius_val = min(radius_val, pill_h // 2)

    # fixed placement anchors
    right = tile - (PADDING_CONTENT + 2)
    top = PADDING_CONTENT + PRICE_TOP_PAD_PX
    bottom = top + pill_h

    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2

    # title area
    y_bottom = tile - TEXT_BOTTOM_PAD_PX

    # image area depends on actual number of lines; store anchors
    info = {
        'title_font': title_font,
        'price_font': price_font,
        'line_h': line_h,
        'pill_h': pill_h,
        'pill_radius': radius_val,
        'pill_right': right,
        'pill_top': top,
        'pill_bottom': bottom,
        'max_title_w': max_title_w,
        'max_lines': max_lines,
        'y_bottom': y_bottom,
    }
    _LAYOUT_CACHE[tile] = info
    return info

def _get_base_tile(tier: str, tile: int) -> Image.Image:
    """Base layer: transparent + tier background only."""
    key = (tier, tile)
    im = _BASE_TILE_CACHE.get(key)
    if im is not None:
        return im
    base = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    base.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    _BASE_TILE_CACHE[key] = base
    return base

def _get_pill_bg(pill_w: int, pill_h: int, radius: int) -> Image.Image:
    key = (pill_w, pill_h, radius, PRICE_PILL_FILL, PRICE_PILL_OUTLINE, PRICE_PILL_OUTLINE_PX)
    im = _PILL_BG_CACHE.get(key)
    if im is not None:
        return im
    p = Image.new('RGBA', (pill_w, pill_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(p)
    d.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=radius,
                        fill=PRICE_PILL_FILL, outline=PRICE_PILL_OUTLINE, width=PRICE_PILL_OUTLINE_PX)
    _PILL_BG_CACHE[key] = p
    return p

def _tile_cache_key(aid: int, tile: int, tier: str, price: int, name: str) -> str:
    # keep key short-ish but collision-safe
    h = hashlib.sha1(name.encode('utf-8', errors='ignore')).hexdigest()[:10]
    theme = 'cb' if THEME_CLASSIC_BLUE else 'd'
    single = '1' if TITLE_SINGLE_LINE else '2'
    return f"tile:v3:{aid}:{tile}:{tier}:{price}:{theme}:{single}:{h}"
    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2

    # price pill geometry (depends only on font metrics)
    pill_h = price_font.getbbox('Ag')[3] + PRICE_PILL_PAD_Y * 2 - 2
    radius_val = PRICE_PILL_RADIUS_PX if PRICE_PILL_RADIUS_PX > 0 else (pill_h // 2)
    radius_val = min(radius_val, pill_h // 2)
    top = PADDING_CONTENT + PRICE_TOP_PAD_PX
    bottom = top + pill_h

    lay = {
        'title_font': title_font,
        'price_font': price_font,
        'line_h': line_h,
        'max_title_w': max_title_w,
        'max_lines': max_lines,
        'pill_h': pill_h,
        'pill_radius': radius_val,
        'pill_top': top,
        'pill_bottom': bottom,
        'pill_right': tile - (PADDING_CONTENT + 2),
    }
    _LAYOUT_CACHE[tile] = lay
    return lay

def _get_base_tile(tier: str, tile: int) -> Image.Image:
    """Base tile = tier background only (precomposited)."""
    key = (tier, tile, THEME_CLASSIC_BLUE)
    im = _BASE_TILE_CACHE.get(key)
    if im is not None:
        return im
    out = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    out.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    _BASE_TILE_CACHE[key] = out
    return out

def _get_pill_bg(pill_w: int, pill_h: int, radius: int) -> Image.Image:
    """Cache just the pill shape (rounded rect) as RGBA."""
    key = (pill_w, pill_h, radius, PRICE_PILL_FILL, PRICE_PILL_OUTLINE, PRICE_PILL_OUTLINE_PX)
    im = _PILL_BG_CACHE.get(key)
    if im is not None:
        return im
    im = Image.new('RGBA', (pill_w, pill_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        [0, 0, pill_w - 1, pill_h - 1],
        radius=radius,
        fill=PRICE_PILL_FILL,
        outline=PRICE_PILL_OUTLINE,
        width=PRICE_PILL_OUTLINE_PX,
    )
    _PILL_BG_CACHE[key] = im
    return im

def _tile_cache_key(it: Dict[str, Any], tile: int) -> str:
    """Stable key for ready-to-use tile PNG."""
    aid = int(it.get('assetId') or 0)
    price = _price_of(it)
    name = str(it.get('name') or '').strip().upper()
    # small hash for long names
    name_h = hashlib.sha1(name.encode('utf-8', 'ignore')).hexdigest()[:10]
    return f"tile:v2:{aid}:{tile}:{price}:{name_h}:{int(TITLE_SINGLE_LINE)}:{int(THEME_CLASSIC_BLUE)}"

def _tile_mem_get(key: str) -> Optional[Image.Image]:
    im = _TILE_MEM.get(key)
    if im is None:
        return None
    try:
        _TILE_MEM.move_to_end(key)  # type: ignore[attr-defined]
    except Exception:
        pass
    return im

def _tile_mem_put(key: str, im: Image.Image):
    if _TILE_MEM_MAX <= 0:
        return
    try:
        _TILE_MEM[key] = im
        _TILE_MEM.move_to_end(key)  # type: ignore[attr-defined]
        while len(_TILE_MEM) > _TILE_MEM_MAX:
            _TILE_MEM.popitem(last=False)  # type: ignore[attr-defined]
    except Exception:
        # fallback: best-effort
        _TILE_MEM.clear()


def _layout(tile: int) -> Dict[str, Any]:
    """Compute layout constants once per tile size."""
    if tile in _LAYOUT_CACHE:
        return _LAYOUT_CACHE[tile]
    title_font = _bold_font(max(12, tile // max(1, TITLE_FONT_TILE_DIV)))
    price_font = _bold_font(max(10, tile // max(1, PRICE_FONT_TILE_DIV)))
    # cheap metrics
    line_h = title_font.getbbox('Ag')[3]
    price_h = price_font.getbbox('Ag')[3]
    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2
    lay = {
        'title_font': title_font,
        'price_font': price_font,
        'line_h': line_h,
        'price_h': price_h,
        'max_title_w': max_title_w,
        'max_lines': max_lines,
    }
    _LAYOUT_CACHE[tile] = lay
    return lay


def _get_base_tile(tier: str, tile: int) -> Image.Image:
    """Base tile = tier bg only. Cached as PIL image."""
    key = (tier, tile)
    im = _BASE_TILE_CACHE.get(key)
    if im is not None:
        return im
    base = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    base.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    _BASE_TILE_CACHE[key] = base
    return base


def _get_pill_bg(pill_w: int, pill_h: int, radius: int) -> Image.Image:
    """Cached pill background (rounded rectangle)."""
    key = (pill_w, pill_h, radius, PRICE_PILL_FILL, PRICE_PILL_OUTLINE, PRICE_PILL_OUTLINE_PX)
    im = _PILL_BG_CACHE.get(key)
    if im is not None:
        return im
    out = Image.new('RGBA', (pill_w, pill_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle(
        [0, 0, pill_w - 1, pill_h - 1],
        radius=radius,
        fill=PRICE_PILL_FILL,
        outline=PRICE_PILL_OUTLINE,
        width=PRICE_PILL_OUTLINE_PX,
    )
    _PILL_BG_CACHE[key] = out
    return out

# In-memory LRU for already-rendered tiles (decoded PIL images)
_TILE_MEM_MAX = int(os.getenv('TILE_MEM_MAX', '256'))
_TILE_MEM: Dict[str, Image.Image] = {}
_TILE_MEM_ORDER: List[str] = []

def _tile_mem_get(key: str) -> Optional[Image.Image]:
    im = _TILE_MEM.get(key)
    if im is None:
        return None
    try:
        _TILE_MEM_ORDER.remove(key)
    except ValueError:
        pass
    _TILE_MEM_ORDER.append(key)
    return im

def _tile_mem_put(key: str, im: Image.Image) -> None:
    if key in _TILE_MEM:
        try:
            _TILE_MEM_ORDER.remove(key)
        except ValueError:
            pass
    _TILE_MEM[key] = im
    _TILE_MEM_ORDER.append(key)
    while len(_TILE_MEM_ORDER) > _TILE_MEM_MAX:
        k = _TILE_MEM_ORDER.pop(0)
        _TILE_MEM.pop(k, None)

def _get_layout(tile: int) -> Dict[str, Any]:
    """Cache layout metrics/fonts per tile size."""
    lay = _LAYOUT_CACHE.get(tile)
    if lay is not None:
        return lay
    title_font = _bold_font(max(12, tile // max(1, TITLE_FONT_TILE_DIV)))
    price_font = _bold_font(max(10, tile // max(1, PRICE_FONT_TILE_DIV)))
    line_h = title_font.getbbox('Ag')[3]
    price_h = price_font.getbbox('Ag')[3]
    pill_h = price_h + PRICE_PILL_PAD_Y * 2 - 2
    radius_val = PRICE_PILL_RADIUS_PX if PRICE_PILL_RADIUS_PX > 0 else (pill_h // 2)
    radius_val = min(radius_val, pill_h // 2)
    top = PADDING_CONTENT + PRICE_TOP_PAD_PX
    bottom = top + pill_h

    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2
    y_bottom = tile - TEXT_BOTTOM_PAD_PX
    # y_top depends on amount of lines; computed per item, but we keep bottom anchor.
    box_w = tile - PADDING_CONTENT * 2

    lay = {
        'title_font': title_font,
        'price_font': price_font,
        'line_h': line_h,
        'max_title_w': max_title_w,
        'max_lines': max_lines,
        'pill_h': pill_h,
        'pill_radius': radius_val,
        'pill_top': top,
        'pill_bottom': bottom,
        'box_w': box_w,
        'y_bottom': y_bottom,
    }
    _LAYOUT_CACHE[tile] = lay
    return lay

def _pill_bg(pill_w: int, pill_h: int, radius: int) -> Image.Image:
    """Cache pill background image by geometry/colors."""
    key = (pill_w, pill_h, radius, PRICE_PILL_FILL, PRICE_PILL_OUTLINE, PRICE_PILL_OUTLINE_PX)
    im = _PILL_BG_CACHE.get(key)
    if im is not None:
        return im
    im = Image.new('RGBA', (pill_w, pill_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        [0, 0, pill_w - 1, pill_h - 1],
        radius=radius,
        fill=PRICE_PILL_FILL,
        outline=PRICE_PILL_OUTLINE,
        width=PRICE_PILL_OUTLINE_PX,
    )
    _PILL_BG_CACHE[key] = im
    return im

def _base_tile(tier: str, tile: int) -> Image.Image:
    """Cache base tile (tier bg only) to avoid recreating images."""
    key = (tier, tile)
    im = _BASE_TILE_CACHE.get(key)
    if im is not None:
        return im
    out = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    out.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    _BASE_TILE_CACHE[key] = out
    return out

def _render_tile(it: Dict[str, Any], thumb: Image.Image, tile: int) -> Image.Image:
    """CPU-bound tile render.

    Optimized for first-run speed:
    - reuse cached base tile (tier bg)
    - reuse cached layout (fonts/geometry)
    - reuse cached price pill background (rounded rect)
    """
    price = _price_of(it)
    tier = _tier_by_price(price)
    name = str(it.get('name') or it.get('assetId') or '').strip().upper()

    lay = _get_layout(tile) if '_get_layout' in globals() else _layout_for_tile(tile)
    title_font = lay.get('title_font')
    price_font = lay.get('price_font')
    line_h = int(lay.get('line_h', title_font.getbbox('Ag')[3]))
    max_title_w = int(lay.get('max_title_w', tile - 14))
    max_lines = int(lay.get('max_lines', 2))

    # Start from cached base
    base = _get_base_tile(tier, tile)
    out = base.copy()
    d = ImageDraw.Draw(out)

    # Bottom title (single or two lines)
    words = name.split()
    lines: List[str] = []
    cur = ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if int(d.textlength(t, font=title_font)) <= max_title_w:
            cur = t
        elif cur:
            lines.append(cur)
            cur = w
        else:
            cur = w
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines:
        last = lines[-1]
        while int(d.textlength(last, font=title_font)) > max_title_w and len(last) > 1:
            last = last[:-1]
        if last != lines[-1] and int(d.textlength(last + '…', font=title_font)) <= max_title_w:
            last = last + '…'
        lines[-1] = last

    title_total_h = line_h * len(lines)
    y_bottom = int(lay.get('y_bottom', tile - TEXT_BOTTOM_PAD_PX))
    y_top = y_bottom - title_total_h
    y_top = y_top + 2  # shift title 2px down

    for i, line in enumerate(lines):
        tw = int(d.textlength(line, font=title_font))
        x = (tile - tw) // 2
        y = y_top + i * line_h
        if THEME_CLASSIC_BLUE:
            d.text((x + 1, y + 1), line, fill=(0, 0, 0, 200), font=title_font)
            d.text((x, y), line, fill=(255, 255, 255, 255), font=title_font)
        else:
            d.text((x, y), line, fill=TITLE_TEXT_COLOR, font=title_font)

    # Price pill (fixed top-right)
    price_text = f'{price} {ROBUX_PREFIX}'.strip()
    w_text = int(d.textlength(price_text, font=price_font))
    pill_w = w_text + PRICE_PILL_PAD_X * 2
    pill_h = int(lay.get('pill_h') or (price_font.getbbox('Ag')[3] + PRICE_PILL_PAD_Y * 2 - 2))
    radius_val = int(lay.get('pill_radius') or (pill_h // 2))
    right = int(lay.get('pill_right') or (tile - (PADDING_CONTENT + 2)))
    top = int(lay.get('pill_top') or (PADDING_CONTENT + PRICE_TOP_PAD_PX))
    bottom = int(lay.get('pill_bottom') or (top + pill_h))
    left = right - pill_w

    # cached rounded-rect layer
    pill_bg = _get_pill_bg(pill_w, pill_h, radius_val)
    out.alpha_composite(pill_bg, (left, top))
    d.text(
        (left + (pill_w - w_text) // 2,
         top + (pill_h - price_font.getbbox('Ag')[3]) // 2 - 1),
        price_text,
        fill=PRICE_TEXT_COLOR,
        font=price_font,
    )

    # Image box (between pill and title)
    box_top    = bottom + GAP_IMAGE_PRICE
    box_bottom = y_top - GAP_IMAGE_TEXT
    box_h      = max(1, box_bottom - box_top)
    box_w      = tile - PADDING_CONTENT * 2

    iw, ih = thumb.size
    k = min(box_w / max(1, iw), box_h / max(1, ih))
    nw, nh = (max(1, int(iw * k)), max(1, int(ih * k)))
    resample = Image.LANCZOS if k < 1.0 else Image.BILINEAR
    im2 = thumb.resize((nw, nh), resample)

    im_x = PADDING_CONTENT + (box_w - im2.width) // 2
    im_y = box_top + (box_h - im2.height) // 2
    out.alpha_composite(im2, (im_x, im_y))
    return out

# =========================
# Header / Footer
# =========================


def _draw_header(canvas: Image.Image, count: int, title: str):
    if not SHOW_HEADER:
        return
    W, H = canvas.size
    band = Image.new('RGBA', (W, HEADER_H), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, 0))
    d = ImageDraw.Draw(canvas)
    font = _bold_font(max(26, HEADER_H // 2))
    text = f"{count}  {title}"
    try:
        tw = int(d.textlength(text, font=font))
        th = font.getbbox('Ag')[3]
    except Exception:
        tw = d.textbbox((0,0), text, font=font)[2]
        th = d.textbbox((0,0), 'Ag', font=font)[3]
    x = max(10, (W - tw) // 2)
    y = max(6, (HEADER_H - th) // 2)
    d.text((x, y), text, fill=(255, 255, 255, 255), font=font)



def _draw_footer(canvas: Image.Image, username: Optional[str], user_id: Optional[int]):
    if not SHOW_FOOTER:
        return
    W, H = canvas.size
    band = Image.new('RGBA', (W, FOOTER_H), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, H - FOOTER_H))
    d = ImageDraw.Draw(canvas)
    x = 12
    base_y = H - FOOTER_H + 10
    try:
        if os.path.exists(FOOTER_ICON):
            ic = Image.open(FOOTER_ICON).convert('RGBA').resize((FOOTER_H - 20, FOOTER_H - 20), Image.BILINEAR)
        else:
            raise FileNotFoundError
    except Exception:
        ic = Image.new('RGBA', (FOOTER_H - 20, FOOTER_H - 20), (40, 40, 40, 255))
        ImageDraw.Draw(ic).rectangle([2, 2, ic.width - 2, ic.height - 2], outline=(200, 200, 200, 255), width=2)
    canvas.alpha_composite(ic, (x, base_y))

    tx = x + ic.width + 12
    right_pad = 12
    max_w = max(10, W - tx - right_pad)
    max_h = max(10, FOOTER_H - 20)

    lang = get_current_lang()
    now = datetime.datetime.now()
    _months = {
        'en': ["January","February","March","April","May","June","July","August","September","October","November","December"],
        'ru': ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"],
    }
    mlist = _months.get(lang, _months['en'])
    month = mlist[now.month-1]
    date_text = tr(lang, 'footer.date', day=f"{now.day:02d}", month=month, year=now.year)

    who = username if username and str(username).strip() else str(user_id) if user_id is not None else '@unknown'
    if isinstance(who, str) and who and (not who.startswith('@')) and (not who.isdigit()):
        who = f'@{who}'
    line1 = date_text
    line2 = tr(lang, 'footer.checked_by', username=who)
    line3 = tr(lang, 'footer.domain') if tr(lang, 'footer.domain') != 'footer.domain' else f'{FOOTER_BRAND}'

    base1 = max(20, FOOTER_H // 3)
    base2 = max(16, FOOTER_H // 4)
    MIN = 10
    SP12, SP23 = (6, 4)

    def get_font(sz, bold=False):
        return (_bold_font if bold else _font)(int(max(MIN, sz)))

    def text_w(text, font):
        try:
            return d.textlength(text, font=font)
        except Exception:
            try:
                return d.textbbox((0, 0), text, font=font)[2]
            except Exception:
                return font.getsize(text)[0]

    def text_h(font):
        try:
            return font.getbbox('Ag')[3]
        except Exception:
            try:
                return d.textbbox((0, 0), 'Ag', font=font)[3]
            except Exception:
                return font.getsize('Ag')[1]

    def ellipsize(text, font):
        if text_w(text, font) <= max_w:
            return text
        base = text
        ell = '…'
        lo, hi = (0, len(base))
        res = ell
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = base[:mid] + ell
            if text_w(cand, font) <= max_w:
                res = cand
                lo = mid + 1
            else:
                hi = mid - 1
        return res

    def fit_width(text, size, bold=False):
        sz = int(size)
        while sz > MIN and text_w(text, get_font(sz, bold)) > max_w:
            sz -= 1
        return max(MIN, sz)

    s1 = fit_width(line1, base1, bold=True)
    s2 = fit_width(line2, base2, bold=False)
    s3 = fit_width(line3, base2, bold=False)
    MAX_BRAND = int(max(12, FOOTER_H * 0.22))
    if s3 > s2:
        s3 = s2
    if s3 > MAX_BRAND:
        s3 = MAX_BRAND

    def total_height(a, b, c):
        return text_h(get_font(a, True)) + SP12 + text_h(get_font(b)) + SP23 + text_h(get_font(c))

    while total_height(s1, s2, s3) > max_h and (s1 > MIN or s2 > MIN or s3 > MIN):
        if s1 > MIN: s1 -= 1
        if s2 > MIN: s2 -= 1
        if s3 > MIN: s3 -= 1

    font1, font2a, font2b = (get_font(s1, True), get_font(s2), get_font(s3))
    line1_draw = ellipsize(line1, font1)
    line2_draw = ellipsize(line2, font2a)
    line3_draw = ellipsize(line3, font2b)

    total_h = text_h(font1) + SP12 + text_h(font2a) + SP23 + text_h(font2b)
    y0 = max(H - FOOTER_H + 10, H - 10 - total_h)
    y1 = y0
    y2 = y1 + text_h(font1) + SP12
    y3 = y2 + text_h(font2a) + SP23

    d.text((tx, y1), line1_draw, fill=(255, 255, 255, 255), font=font1)
    d.text((tx, y2), line2_draw, fill=(255, 255, 255, 230), font=font2a)
    d.text((tx, y3), line3_draw, fill=(255, 255, 255, 200), font=font2b)

# =========================
# Grid rendering (square-ish layout)
# =========================
async def _render_grid(items: List[Dict[str, Any]], tile: int=150, title: str='Items', username: Optional[str]=None, user_id: Optional[int]=None) -> bytes:
    _build_image_index_cached()
    price_map = load_prices_csv_cached(PRICE_CSV_PATH)
    # обогащаем КАЖДЫЙ айтем ценой из CSV (itemId/collectibleItemId/assetId)
    items = [_enrich_with_csv(it, price_map) for it in items]
    n = len(items)

    # Guard: nothing to render -> return compact placeholder instead of crashing on 0px width
    if n == 0:
        top = HEADER_H if SHOW_HEADER else 0
        bottom = FOOTER_H if SHOW_FOOTER else 0
        W = max(420, 2 * tile)
        H = max(160, top + bottom + 2)
        canvas = Image.new('RGBA', (W, H))
        canvas.alpha_composite(_get_canvas_bg(W, H), (0, 0))
        _draw_header(canvas, 0, title)
        _draw_footer(canvas, username, user_id)
        def _save_empty() -> bytes:
            out = io.BytesIO()
            canvas.convert('RGB').save(out, 'PNG', optimize=PNG_OPTIMIZE, quality=90)
            return out.getvalue()
        blob = await asyncio.to_thread(_save_empty)
        _info(f"[grid] placeholder rendered for empty items, bytes={len(blob)}")
        return blob

    if not KEEP_INPUT_ORDER:
        items = sorted(items, key=lambda x: x.get('priceInfo', {}).get('value') or 0, reverse=True)
    ids = [int(x['assetId']) for x in items if 'assetId' in x]

    # pick smallest reasonable thumb size for this tile unless forced
    if THUMB_SIZE_FORCE:
        size = THUMB_SIZE
    else:
        # cheap heuristic: 150->150, 250->250, else 420
        if tile <= 150:
            size = '150x150'
        elif tile <= 250:
            size = '250x250'
        else:
            size = '420x420'
    _info(f"[grid] start items={n} tile={tile} size={size}")
    thumbs = await _fetch_thumbs(ids, size=size)

    # --- square-ish grid: pick cols = ceil(sqrt(n)), rows = ceil(n/cols)
    if n == 0:
        cols, rows = 0, 0
    else:
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))

    # Canvas size
    top = HEADER_H if SHOW_HEADER else 0
    bottom = FOOTER_H if SHOW_FOOTER else 0
    W, H = (cols * tile, rows * tile + top + bottom + 2)

    canvas = Image.new('RGBA', (W, H))
    canvas.alpha_composite(_get_canvas_bg(W, H), (0, 0))
    _draw_header(canvas, n, title)
    _draw_footer(canvas, username, user_id)

    # ---- Render pipeline ----
    # Stage A: load/decode tile from mem/disk cache (cheap)
    # Stage B: CPU render (PIL) in worker threads
    # Stage C: encode tile PNG in worker threads + store in cache

    cpu_sem = asyncio.Semaphore(RENDER_CONCURRENCY)

    async def _decode_png(b: bytes) -> Image.Image:
        return await asyncio.to_thread(lambda: Image.open(io.BytesIO(b)).convert('RGBA'))

    async def _encode_png(im: Image.Image) -> bytes:
        def _enc() -> bytes:
            bio = io.BytesIO()
            im.save(bio, format='PNG', optimize=False)
            return bio.getvalue()
        return await asyncio.to_thread(_enc)

    async def render_one(idx: int, it: Dict[str, Any]) -> tuple[int, Image.Image]:
        aid = int(it.get('assetId') or 0)

        # tile key (prefer newest helper if exists)
        try:
            tkey = _tile_cache_key(it, tile)  # type: ignore
        except Exception:
            price = _price_of(it)
            tier = _tier_by_price(price)
            name = str(it.get('name') or '').strip().upper()
            tkey = _tile_cache_key(aid, tile, tier, price, name)  # type: ignore

        # RAM hit
        hit = _tile_mem_get(tkey)
        if hit is not None:
            return idx, hit

        # persistent cache hit
        b = await cache.get_bytes(tkey, IMG_TTL)
        if b:
            try:
                im = await _decode_png(b)
                _tile_mem_put(tkey, im)
                return idx, im
            except Exception:
                pass

        # render path (CPU)
        thumb = thumbs.get(aid)
        if thumb is None:
            thumb = Image.new('RGBA', (tile - 12, tile - 26), (70, 80, 96, 255))

        async with cpu_sem:
            im = await asyncio.to_thread(_render_tile, it, thumb, tile)

        # encode + store (not holding cpu_sem)
        try:
            b2 = await _encode_png(im)
            await cache.set_bytes(tkey, b2)
        except Exception:
            pass

        _tile_mem_put(tkey, im)
        return idx, im

    # render concurrently and composite as results arrive
    tasks = [asyncio.create_task(render_one(i, it)) for i, it in enumerate(items)]

    # precompute positions
    y0 = (HEADER_H if SHOW_HEADER else 0)
    pos: List[tuple[int, int]] = []
    for i in range(n):
        r = i // cols
        c = i % cols
        pos.append((c * tile, y0 + r * tile))

    for fut in asyncio.as_completed(tasks):
        idx, im = await fut
        x, y = pos[idx]
        canvas.alpha_composite(im, (x, y))

    # encode whole sheet off-thread
    def _save_canvas() -> bytes:
        out = io.BytesIO()
        canvas.convert('RGB').save(out, 'PNG', optimize=PNG_OPTIMIZE, quality=90)
        return out.getvalue()

    blob = await asyncio.to_thread(_save_canvas)
    _info(f"[grid] done items={n} cols={cols} rows={rows} tile={tile} bytes={len(blob)}")
    return blob


# =========================
# Public API (clean signatures)
# =========================
from typing import List, Dict, Any, Optional
from i18n import tr, get_current_lang

async def generate_full_inventory_grid(
    items: List[Dict[str, Any]],
    tile: int = 150,
    pad: int = 6,
    username: Optional[str] = None,
    user_id: Optional[int] = None,
    title: Optional[str] = None
) -> bytes:
    lang = get_current_lang()
    default_title = tr(lang, 'inventory.full_title')
    return await _render_grid(items, tile=tile, title=(title or default_title), username=username, user_id=user_id)


# Добавить в функцию generate_inventory_preview параметр is_public

# === NEW: multi-photo generator with hard cap per image ===
async def generate_full_inventory_grids(
    items,
    tile: int = 150,
    pad: int = 6,
    username: "Optional[str]" = None,
    user_id: "Optional[int]" = None,
    title: "Optional[str]" = None,
    max_items_per_image: "Optional[int]" = None
) -> list[bytes]:
    """
    Split items into several images if they exceed MAX_ITEMS_PER_IMAGE (env or arg).
    - Keeps existing single-image API intact (see generate_full_inventory_grid).
    - Titles are auto-suffixed with localized "Page X/Y".
    """
    from typing import Optional, List, Dict, Any
    lang = get_current_lang()
    base_title = title or tr(lang, 'inventory.full_title')
    # Env override; default 650
    cap = max(1, int(os.getenv("MAX_ITEMS_PER_IMAGE", "650")))
    if isinstance(max_items_per_image, int) and max_items_per_image > 0:
        cap = max_items_per_image

    n = len(items or [])
    if n <= cap:
        # Single page -> keep title as-is
        img = await _render_grid(items, tile=tile, title=base_title, username=username, user_id=user_id)
        return [img]

    # Chunk into pages
    chunks = [items[i:i+cap] for i in range(0, n, cap)]
    total = len(chunks)
    out: list[bytes] = []
    for idx, chunk in enumerate(chunks, start=1):
        page_title = f"{base_title} ({tr(lang, 'inventory_view.page', current=idx, total=total)})"
        img = await _render_grid(chunk, tile=tile, title=page_title, username=username, user_id=user_id)
        out.append(img)
    return out

async def generate_inventory_preview(
        tg_id: int,
        roblox_id: int,
        categories_limit: int = 8,
        username: Optional[str] = None,
        is_public: bool = False  # НОВЫЙ ПАРАМЕТР
) -> bytes:
    if is_public:
        from roblox_client import get_inventory_public_ultra_fast
        data = await get_inventory_public_ultra_fast(roblox_id)
    else:
        from roblox_client import get_full_inventory
        data = await get_full_inventory(tg_id, roblox_id)

    items: List[Dict[str, Any]] = []
    for arr in (data.get('byCategory') or {}).values():
        items.extend(arr)
    lang = get_current_lang()
    return await _render_grid(items, tile=150, title=tr(lang, 'inventory.title'), username=username, user_id=tg_id)


import re as _re_cat

def _slug_cat(s: str) -> str:
    s = str(s or '').lower()
    s = s.replace('ё', 'е').replace('&', 'and')
    s = _re_cat.sub(r'[^a-z0-9]+', '_', s)
    return s.strip('_')

_CAT_SYNONYMS = {
    'bundles_packages': {'bundles_packages','bundles','packages','package','rthro','avatars'},
    'hats': {'hats','head_accessories','головные_уборы'},
    'heads': {'heads','head','головы'},
    'hair': {'hair','hairs','волосы'},
}

def _resolve_category_key(bycat: dict, incoming: str) -> str | None:
    if not isinstance(bycat, dict):
        return None
    if incoming in bycat:
        return incoming
    inc = _slug_cat(incoming)
    # try exact slug
    for k in bycat.keys():
        if _slug_cat(k) == inc:
            return k
    # singular/plural heuristic
    variants = {inc}
    if inc.endswith('s'):
        variants.add(inc[:-1])
    else:
        variants.add(inc + 's')
    # add dictionary synonyms
    for canon, syns in _CAT_SYNONYMS.items():
        if inc in syns:
            variants |= set(syns)
    for k in bycat.keys():
        if _slug_cat(k) in variants:
            return k
    # prefix fallback
    for k in bycat.keys():
        sk = _slug_cat(k)
        if sk.startswith(inc[: max(3, min(len(inc), 8))]):
            return k
    return None
async def generate_category_sheets(
    tg_id: int,
    roblox_id: int,
    category: str,
    limit: int = 0,
    tile: int = 150,
    force: bool = False,
    username: Optional[str] = None
, *, items_override: Optional[List[Dict[str, Any]]] = None) -> bytes:
    # Use override items when provided (public flow); otherwise fetch like private
    items = None
    if items_override is not None:
        items = items_override
    else:
        from roblox_client import get_full_inventory
        data = await get_full_inventory(tg_id, roblox_id)
        items = (data.get('byCategory') or {}).get(category, [])
    price_map = load_prices_csv_cached()
    items = [_enrich_with_csv(x, price_map) for x in items]
    if limit and limit > 0:
        items = items[:limit]
    # Localize category title
    lang = get_current_lang()
    slug = str(category or '').lower().replace(' ', '_')
    loc = tr(lang, f'cat.{slug}')
    title = loc if loc and loc != f'cat.{slug}' else category
    return await _render_grid(items, tile=tile, title=title, username=username, user_id=tg_id)


# --- PATCH: public helpers to render RAP & off-sale using the existing grid renderer ---
from typing import Any, Dict, List, Optional
from i18n import tr, get_current_lang

async def generate_rap_sheet(tg_id: int, roblox_id: int, *, cookie: Optional[str] = None, tile: int = 150, title: Optional[str] = None) -> bytes:
    from roblox_client import calc_user_rap
    data = await calc_user_rap(roblox_id, cookie=cookie)
    items = data.get("items") or []
    price_map = load_prices_csv_cached()
    items = [_enrich_with_csv({"assetId": it.get("assetId"), "name": it.get("name"), "priceInfo": {"value": it.get("rap", 0)}}, price_map) for it in items]
    lang = get_current_lang()
    ttl = title or tr(lang, "rap.title")
    return await _render_grid(items, tile=tile, title=ttl, username=None, user_id=tg_id)

async def generate_offsale_sheet(tg_id: int, roblox_id: int, *, cookie: Optional[str] = None, tile: int = 150, title: Optional[str] = None) -> bytes:
    from roblox_client import get_offsale_collectibles
    data = await get_offsale_collectibles(roblox_id, cookie=cookie)
    items = data or []
    price_map = load_prices_csv_cached()
    # Use RAP as price value for rendering
    norm = [{"assetId": it.get("assetId"), "name": it.get("name"), "priceInfo": {"value": it.get("rap", 0)}} for it in items]
    items = [_enrich_with_csv(x, price_map) for x in norm]
    lang = get_current_lang()
    ttl = title or tr(lang, "offsale.title")
    return await _render_grid(items, tile=tile, title=ttl, username=None, user_id=tg_id)
