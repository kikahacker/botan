
from __future__ import annotations
import os, io, math, json, asyncio, hashlib, datetime, logging
from typing import Any, Dict, List, Optional

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

# =========================
# External deps (kept optional to run standalone)
# =========================
try:
    from http_shared import get_client, PROXY_POOL    # type: ignore
except Exception:
    get_client = None                                  # type: ignore
    class _Pool:
        def any(self): return None
    PROXY_POOL = _Pool()                               # type: ignore

try:
    from config import CFG                              # type: ignore
except Exception:
    class _Cfg: pass
    CFG = _Cfg()                                        # type: ignore

try:
    import cache                                        # type: ignore
except Exception:
    class _MemCache:
        _m: Dict[str, bytes] = {}
        async def get_bytes(self, k, ttl): return self._m.get(k)
        async def set_bytes(self, k, v): self._m[k] = v
    cache = _MemCache()                                 # type: ignore

# =========================
# Tunables
# =========================
IMG_TTL = int(getattr(CFG, 'CACHE_IMG_TTL', 3600))
THUMB_TTL = int(getattr(CFG, 'THUMB_TTL', 86400))

# Default tile size (each item cell)
DEFAULT_TILE = int(os.getenv("TILE", "150"))

# Main switch: download thumbnails at this size (independent from tile)
# NEW: You can set THUMB_SIZE='auto' (default) and it will pick a sane value based on tile.
THUMB_SIZE_ENV = os.getenv('THUMB_SIZE', 'auto')

# Layout
PADDING_CONTENT = int(os.getenv('PADDING_CONTENT', '0'))
GAP_IMAGE_PRICE = int(os.getenv('GAP_IMAGE_PRICE', '6'))
GAP_IMAGE_TEXT  = int(os.getenv('GAP_IMAGE_TEXT',  '6'))
TITLE_SINGLE_LINE = str(os.getenv('TITLE_SINGLE_LINE', 'false')).strip().lower() in ('1','true','yes','y','on')
THEME_CLASSIC_BLUE = str(os.getenv('THEME_CLASSIC_BLUE', 'false')).strip().lower() in ('1','true','yes','y','on')

# NEW: Auto layout scales header/footer to tile size
AUTO_LAYOUT = str(os.getenv('AUTO_LAYOUT', '1')).lower() in ('1','true','yes','on','y')

SHOW_HEADER = str(os.getenv('SHOW_HEADER', 'true')).strip().lower() in ('1','true','yes','y','on')
SHOW_FOOTER = str(os.getenv('SHOW_FOOTER', 'true')).strip().lower() in ('1','true','yes','y','on')
HEADER_H_BASE = int(os.getenv('HEADER_H', '76'))
FOOTER_H_BASE = int(os.getenv('FOOTER_H', '140'))
FOOTER_ICON = os.getenv('FOOTER_ICON', os.path.join(getattr(CFG, 'ASSETS_DIR', 'assets'), 'footer_badge.png'))
FOOTER_BRAND = os.getenv('FOOTER_BRAND', 'raika.gg')

# Style for price pill and title
TITLE_TEXT_COLOR = (255, 255, 255, 255)
PRICE_TEXT_COLOR = (0, 0, 0, 255)

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
PRICE_TOP_PAD_PX = int(os.getenv('PRICE_TOP_PAD_PX', '10'))
PRICE_PILL_OUTLINE_PX = int(os.getenv('PRICE_PILL_OUTLINE_PX', '1'))
PRICE_PILL_FILL = _rgba_env('PRICE_PILL_FILL', '255,255,255,235')
PRICE_PILL_OUTLINE = _rgba_env('PRICE_PILL_OUTLINE', '0,0,0,200')
PRICE_PILL_RADIUS_PX = int(os.getenv('PRICE_PILL_RADIUS_PX', '0'))

TEXT_BOTTOM_PAD_PX = int(os.getenv('TEXT_BOTTOM_PAD_PX', '2'))
TITLE_FONT_TILE_DIV = int(os.getenv('TITLE_FONT_TILE_DIV', '7'))
PRICE_FONT_TILE_DIV = int(os.getenv('PRICE_FONT_TILE_DIV', '8'))

# =========================
# Pricing tiers (bg colors)
# =========================
FALLBACK_THRESHOLDS = [(1000, 'gold'), (500, 'orange'), (200, 'purple'), (0, 'blue')]

def _tier_by_price(price: int) -> str:
    for th, nm in FALLBACK_THRESHOLDS:
        if price >= th:
            return nm
    return 'common'

# =========================
# Fonts
# =========================
_FONT, _BOLD_FONT = ({}, {})

def _font(sz):
    if sz in _FONT: return _FONT[sz]
    try: f = ImageFont.truetype('arial.ttf', sz)
    except Exception: f = ImageFont.load_default()
    _FONT[sz] = f
    return f

def _bold_font(sz):
    if sz in _BOLD_FONT: return _BOLD_FONT[sz]
    for cand in ('arialbd.ttf', 'Arial Bold.ttf', 'Arial-Bold.ttf'):
        try:
            f = ImageFont.truetype(cand, sz); _BOLD_FONT[sz] = f; return f
        except Exception: pass
    _BOLD_FONT[sz] = _font(sz); return _BOLD_FONT[sz]

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

_bg_cache: Dict[tuple, Image.Image] = {}
def _get_tier_bg(tier: str, tile: int):
    key = (tier, tile)
    im = _bg_cache.get(key)
    if im: return im
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
# Local image helpers
# =========================
def _read_ready_item(aid: int) -> Optional[Image.Image]:
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        p = os.path.join(READY_ITEM_DIR, f'{aid}{ext}')
        if os.path.exists(p):
            try:
                return Image.open(p).convert('RGBA')
            except Exception as e:
                _err(f"[local] open fail for {p}", e)
    return None

# =========================
# Network fetch with cache (optional; thumbnails)
# =========================
async def _download_image_with_cache(url: str) -> Optional[Image.Image]:
    key = 'thumb:' + hashlib.sha1(url.encode()).hexdigest()
    b = await cache.get_bytes(key, THUMB_TTL) if hasattr(cache, 'get_bytes') else None
    if b:
        try:
            return Image.open(io.BytesIO(b)).convert('RGBA')
        except Exception as e:
            _err("[thumb] cache decode fail", e)
    if get_client is None:
        return None
    proxy = PROXY_POOL.any()
    client = await get_client(proxy)
    import httpx
    try:
        r = await client.get(url, timeout=httpx.Timeout(10.0, connect=2.0, read=8.0))
        r.raise_for_status()
        data = r.content
        if hasattr(cache, 'set_bytes'):
            await cache.set_bytes(key, data)
        return Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception as e:
        _err("[thumb] fetch fail", e)
        return None

async def _fetch_thumbs(ids: List[int], size: str='250x250') -> Dict[int, Image.Image]:
    result: Dict[int, Image.Image] = {}
    left: List[int] = []
    for aid in ids:
        imr = _read_ready_item(int(aid))
        if imr is not None:
            result[int(aid)] = imr
        else:
            left.append(int(aid))
    if not left: return result

    base = 'https://thumbnails.roblox.com/v1/assets'
    async def one_batch(ch: List[int]):
        if get_client is None:
            return {}
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        import httpx
        try:
            r = await client.get(
                base,
                params={'assetIds': ','.join(map(str, ch)), 'size': size, 'format': 'Png', 'isCircular': 'false', 'returnPolicy': 'PlaceHolder'},
                timeout=httpx.Timeout(10.0, connect=2.0, read=8.0)
            )
            r.raise_for_status()
            urls = {}
            for rec in r.json().get('data', []):
                aid = int(rec.get('targetId'))
                url = rec.get('imageUrl')
                if url: urls[aid] = url
            return urls
        except Exception as e:
            _err(f"[thumb] batch fail {ch[:3]}..", e)
            return {}

    batches = [left[i:i + 100] for i in range(0, len(left), 100)]
    maps = await asyncio.gather(*(one_batch(b) for b in batches))
    url_map: Dict[int, str] = {}
    for m in maps: url_map.update(m)

    async def dl(aid, url):
        im = await _download_image_with_cache(url)
        if im is not None:
            result[int(aid)] = im
    await asyncio.gather(*(dl(a, u) for a, u in url_map.items()))
    return result

# =========================
# Helpers
# =========================
def _num(v):
    try:
        if v is None: return 0
        if isinstance(v, (int, float)): return int(v)
        return int(float(v))
    except Exception:
        return 0

def _price_of(it: Dict[str, Any]) -> int:
    return _num((it.get('priceInfo') or {}).get('value'))

# =========================
# Tile rendering
# =========================
def _render_tile(it: Dict[str, Any], thumb: Image.Image, tile: int) -> Image.Image:
    price = _price_of(it)
    tier  = _tier_by_price(price)
    name  = str(it.get('name') or it.get('assetId') or '').upper()

    out = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    out.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    d = ImageDraw.Draw(out)

    title_font = _bold_font(max(12, tile // max(1, TITLE_FONT_TILE_DIV)))
    price_font = _bold_font(max(10, tile // max(1, PRICE_FONT_TILE_DIV)))

    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2
    words = name.split()
    lines: List[str] = []
    cur = ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if int(d.textlength(t, font=title_font)) <= max_title_w:
            cur = t
        elif cur:
            lines.append(cur); cur = w
        else:
            cur = w
        if len(lines) >= max_lines: break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines:
        last = lines[-1]
        while int(d.textlength(last, font=title_font)) > max_title_w and len(last) > 1:
            last = last[:-1]
        if last != lines[-1] and int(d.textlength(last + '…', font=title_font)) <= max_title_w:
            last = last + '…'
        lines[-1] = last

    line_h = title_font.getbbox('Ag')[3]
    title_total_h = line_h * len(lines)
    y_bottom = tile - TEXT_BOTTOM_PAD_PX
    y_top = y_bottom - title_total_h

    for i, line in enumerate(lines):
        tw = int(d.textlength(line, font=title_font))
        x = (tile - tw) // 2
        y = y_top + i * line_h
        d.text((x, y), line, fill=TITLE_TEXT_COLOR, font=title_font)

    price_text = f'{price} {os.getenv("ROBUX_PREFIX", "R$")}'.strip()
    w_text = int(d.textlength(price_text, font=price_font))
    pill_w = w_text + PRICE_PILL_PAD_X * 2
    pill_h = price_font.getbbox('Ag')[3] + PRICE_PILL_PAD_Y * 2 - 2
    right = tile - (PADDING_CONTENT + 2)
    top   = PADDING_CONTENT + PRICE_TOP_PAD_PX
    bottom= top + pill_h
    left  = right - pill_w
    radius_val = PRICE_PILL_RADIUS_PX if PRICE_PILL_RADIUS_PX > 0 else (pill_h // 2)
    radius_val = min(radius_val, pill_h // 2)

    d.rounded_rectangle([left, top, right, bottom],
                        radius=radius_val,
                        fill=PRICE_PILL_FILL,
                        outline=PRICE_PILL_OUTLINE,
                        width=PRICE_PILL_OUTLINE_PX)
    d.text((left + (pill_w - w_text) // 2,
            top + (pill_h - price_font.getbbox('Ag')[3]) // 2),
           price_text, fill=PRICE_TEXT_COLOR, font=price_font)

    box_top    = bottom + GAP_IMAGE_PRICE
    box_bottom = y_top - GAP_IMAGE_TEXT
    box_h      = max(1, box_bottom - box_top)
    box_w      = tile - PADDING_CONTENT * 2

    iw, ih = thumb.size
    k0 = min(box_w / max(1, iw), box_h / max(1, ih))
    k  = min(1.0, k0)  # do not upscale low-res thumbs
    nw, nh = (max(1, int(iw * k)), max(1, int(ih * k)))
    im2 = thumb.resize((nw, nh), Image.LANCZOS)

    im_x = PADDING_CONTENT + (box_w - im2.width) // 2
    im_y = box_top + (box_h - im2.height) // 2
    out.alpha_composite(im2, (im_x, im_y))
    return out

# =========================
# Header / Footer (auto scale)
# =========================
def _scaled_header_footer(tile: int):
    if not AUTO_LAYOUT:
        return HEADER_H_BASE, FOOTER_H_BASE
    # keep proportions near original look
    header = max(48, int(tile * 0.4))
    footer = max(90, int(tile * 0.9))
    return header, footer

def _draw_header(canvas: Image.Image, count: int, title: str, header_h: int):
    if not SHOW_HEADER:
        return
    W, H = canvas.size
    band = Image.new('RGBA', (W, header_h), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, 0))
    d = ImageDraw.Draw(canvas)
    big = _bold_font(max(26, header_h // 2))
    small = _bold_font(max(20, header_h // 3))
    x = 16; y = 8
    d.text((x, y), f'{count}', fill=(255, 255, 255, 255), font=big)
    y2 = y + big.getbbox('Ag')[3] - 4
    d.text((x, y2), f'{title}', fill=(255, 255, 255, 220), font=small)

def _draw_footer(canvas: Image.Image, username: Optional[str], user_id: Optional[int], footer_h: int):
    if not SHOW_FOOTER:
        return
    W, H = canvas.size
    band = Image.new('RGBA', (W, footer_h), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, H - footer_h))
    d = ImageDraw.Draw(canvas)
    x = 12
    base_y = H - footer_h + 10
    try:
        if os.path.exists(FOOTER_ICON):
            ic = Image.open(FOOTER_ICON).convert('RGBA').resize((footer_h - 20, footer_h - 20), Image.BILINEAR)
        else:
            raise FileNotFoundError
    except Exception:
        ic = Image.new('RGBA', (footer_h - 20, footer_h - 20), (40, 40, 40, 255))
        ImageDraw.Draw(ic).rectangle([2, 2, ic.width - 2, ic.height - 2], outline=(200, 200, 200, 255), width=2)
    canvas.alpha_composite(ic, (x, base_y))

    tx = x + ic.width + 12
    right_pad = 12
    max_w = max(10, W - tx - right_pad)
    max_h = max(10, footer_h - 20)

    date_text = datetime.datetime.now().strftime('%d %B %Y')
    who = username if username and str(username).strip() else str(user_id) if user_id is not None else '@unknown'
    if isinstance(who, str) and who and (not who.startswith('@')) and (not who.isdigit()):
        who = f'@{who}'
    line1 = date_text
    line2 = f'Проверено: {who}'
    line3 = f'{FOOTER_BRAND}'

    def text_w(text, font):
        try: return d.textlength(text, font=font)
        except Exception: return font.getsize(text)[0]
    def text_h(font):
        try: return font.getbbox('Ag')[3]
        except Exception: return font.getsize('Ag')[1]

    # Fit fonts to both width and height
    def fit_line(text, base_sz, bold=False):
        MIN = 10
        get = _bold_font if bold else _font
        sz = int(base_sz)
        f = get(sz)
        while (sz > MIN) and (text_w(text, f) > max_w):
            sz -= 1; f = get(sz)
        return f

    font1 = fit_line(line1, max(20, footer_h // 3), bold=True)
    font2 = fit_line(line2, max(16, footer_h // 4))
    font3 = fit_line(line3, max(16, footer_h // 4))

    total = text_h(font1) + 6 + text_h(font2) + 4 + text_h(font3)
    # If still too tall, shrink proportionally
    while total > max_h and font1.size > 10 and font2.size > 10 and font3.size > 10:
        font1 = _bold_font(font1.size - 1)
        font2 = _font(font2.size - 1)
        font3 = _font(font3.size - 1)
        total = text_h(font1) + 6 + text_h(font2) + 4 + text_h(font3)

    y1 = H - footer_h + 10
    y2 = y1 + text_h(font1) + 6
    y3 = y2 + text_h(font2) + 4

    d.text((tx, y1), line1, fill=(255, 255, 255, 255), font=font1)
    d.text((tx, y2), line2, fill=(255, 255, 255, 230), font=font2)
    d.text((tx, y3), line3, fill=(255, 255, 255, 200), font=font3)

# =========================
# Grid rendering
# =========================
async def _render_grid(items: List[Dict[str, Any]], tile: int=DEFAULT_TILE, title: str='Инвентарь', username: Optional[str]=None, user_id: Optional[int]=None) -> bytes:
    n = len(items)
    ids = [int(x['assetId']) for x in items if 'assetId' in x]

    # --- Auto thumbnail size based on tile (prevents blurry scaling up or overfetching) ---
    if THUMB_SIZE_ENV.strip().lower() == 'auto':
        s = max(150, min(420, tile * 2))
        size = f"{s}x{s}"
    else:
        size = THUMB_SIZE_ENV

    _info(f"[grid] start items={n} tile={tile} thumb_size={size}")

    thumbs = await _fetch_thumbs(ids, size=size)

    # square-ish grid
    if n == 0:
        cols, rows = 0, 0
    else:
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))

    header_h, footer_h = _scaled_header_footer(tile)
    top = header_h if SHOW_HEADER else 0
    bottom = footer_h if SHOW_FOOTER else 0
    W, H = (cols * tile, rows * tile + top + bottom + 2)

    canvas = Image.new('RGBA', (W, H), (30, 40, 60, 255))
    if SHOW_HEADER: _draw_header(canvas, n, title, header_h)
    if SHOW_FOOTER: _draw_footer(canvas, username, user_id, footer_h)

    async def make_tile(it):
        aid = int(it['assetId'])
        thumb = thumbs.get(aid) or Image.new('RGBA', (tile - 12, tile - 26), (70, 80, 96, 255))
        return _render_tile(it, thumb, tile)

    tiles = await asyncio.gather(*(make_tile(it) for it in items))

    k = 0
    for r in range(rows):
        for c in range(cols):
            if k >= n: break
            canvas.alpha_composite(tiles[k], (c * tile, (header_h if SHOW_HEADER else 0) + r * tile))
            k += 1

    out = io.BytesIO()
    canvas.convert('RGB').save(out, 'PNG', optimize=True, quality=90)
    _info(f"[grid] done items={n} cols={cols} rows={rows} tile={tile} bytes={out.tell()}")
    return out.getvalue()

# =========================
# Public API
# =========================
async def generate_full_inventory_grid(items: List[Dict[str, Any]], tile: int=DEFAULT_TILE, pad: int=0, username: Optional[str]=None, user_id: Optional[int]=None, title: Optional[str]=None) -> bytes:
    return await _render_grid(items, tile=tile, title=title or 'Инвентарь', username=username, user_id=user_id)

async def generate_category_sheets(tg_id: int, roblox_id: int, category: str, limit: int=0, tile: int=DEFAULT_TILE, force: bool=False, username: Optional[str]=None) -> bytes:
    # Placeholder: in your project, import your roblox client and fetch data
    raise NotImplementedError("Hook this into your data source as in your original project.")
