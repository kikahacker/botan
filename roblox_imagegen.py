from __future__ import annotations
# ==== env bootstrap (inserted by assistant) ====
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)

def _env_rgba(name: str, default: str):
    raw = os.getenv(name, default)
    try:
        r, g, b, a = (int(x.strip()) for x in raw.split(','))
        return (r, g, b, a)
    except Exception:
        return tuple(int(x) for x in default.split(','))

# Public knobs
PRICE_TEXT_COLOR      = _env_rgba("PRICE_TEXT_COLOR",      "0,0,0,255")
TITLE_TEXT_COLOR      = _env_rgba("TITLE_TEXT_COLOR",      "255,255,255,255")

PRICE_FONT_TILE_DIV   = _env_int("PRICE_FONT_TILE_DIV",    8)
TITLE_FONT_TILE_DIV   = _env_int("TITLE_FONT_TILE_DIV",    7)

PRICE_TOP_PAD_PX      = _env_int("PRICE_TOP_PAD_PX",       8)
GAP_IMAGE_PRICE_PX    = _env_int("GAP_IMAGE_PRICE_PX",     10)
GAP_IMAGE_TEXT_PX     = _env_int("GAP_IMAGE_TEXT_PX",      8)
TEXT_BOTTOM_PAD_PX    = _env_int("TEXT_BOTTOM_PAD_PX",     8)

PRICE_PILL_PAD_X      = _env_int("PRICE_PILL_PAD_X",       7)
PRICE_PILL_PAD_Y      = _env_int("PRICE_PILL_PAD_Y",       4)
PRICE_PILL_RADIUS_PX  = _env_int("PRICE_PILL_RADIUS_PX",   999)
PRICE_PILL_OUTLINE_PX = _env_int("PRICE_PILL_OUTLINE_PX",  1)
PRICE_PILL_FILL       = _env_rgba("PRICE_PILL_FILL",       "255,255,255,235")
PRICE_PILL_OUTLINE    = _env_rgba("PRICE_PILL_OUTLINE",    "0,0,0,200")
# ==== /env bootstrap ====


import asyncio, io, json, logging, math, os, hashlib, datetime
from typing import Any, Dict, List, Optional, Tuple
import httpx
from PIL import Image, ImageDraw, ImageFont
from http_shared import get_client, PROXY_POOL
from config import CFG
import cache

def _asbool(v, default=False):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'y', 'on') if v is not None else default
DEBUG_IMAGEGEN = _asbool(os.getenv('DEBUG_IMAGEGEN', 'false'))
LOG_DIR = os.getenv('IMAGEGEN_LOG_DIR', 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger('imagegen')
if not logger.handlers:
    logger.setLevel(logging.DEBUG if DEBUG_IMAGEGEN else logging.INFO)
    fh = logging.FileHandler(os.path.join(LOG_DIR, 'imagegen.log'), encoding='utf-8')
    fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%H:%M:%S'))
    logger.addHandler(fh)

def _log(msg):
    if DEBUG_IMAGEGEN:
        logger.debug(msg)
IMG_TTL = int(getattr(CFG, 'CACHE_IMG_TTL', 3600))
THUMB_TTL = int(getattr(CFG, 'THUMB_TTL', 86400))
HTTP_TIMEOUT = float(os.getenv('HTTP_TIMEOUT', '10.0'))
HTTP_CONNECT_TIMEOUT = float(os.getenv('HTTP_CONNECT_TIMEOUT', '2.0'))
HTTP_READ_TIMEOUT = float(os.getenv('HTTP_READ_TIMEOUT', '8.0'))
THUMB_DL_CONCURRENCY = int(getattr(CFG, 'THUMB_DL_CONCURRENCY', 24))
THUMB_BATCH_CONCURRENCY = int(getattr(CFG, 'THUMB_BATCH_CONCURRENCY', 8))
RENDER_CONCURRENCY = int(os.getenv('RENDER_CONCURRENCY', '8'))
THUMB_REPOLL_DELAYS = [0.2, 0.5, 1.0, 1.6, 2.5]
ASSETS_DIR = getattr(CFG, 'ASSETS_DIR', 'assets')
CANVAS_BG_PATH = getattr(CFG, 'CANVAS_BG', os.path.join(ASSETS_DIR, 'canvas_bg.png'))
READY_ITEM_DIR = os.getenv('READY_ITEM_DIR', 'thumb_cache')
os.makedirs(READY_ITEM_DIR, exist_ok=True)
WRITE_READY_ITEM_IMAGES = _asbool(os.getenv('WRITE_READY_ITEM_IMAGES', 'false'))
ROBUX_PREFIX = os.getenv('ROBUX_PREFIX', 'R$')
KEEP_INPUT_ORDER = _asbool(os.getenv('KEEP_INPUT_ORDER', 'false'))
PADDING_CONTENT = int(os.getenv('PADDING_CONTENT', '6'))
GAP_LINE_TITLE = int(os.getenv('GAP_LINE_TITLE', '6'))
GAP_PRICE_LINE = int(os.getenv('GAP_PRICE_LINE', '4'))
TITLE_SINGLE_LINE = _asbool(os.getenv('TITLE_SINGLE_LINE', 'false'))
THEME_CLASSIC_BLUE = _asbool(os.getenv('THEME_CLASSIC_BLUE', 'false'))
LINE_SCALE = float(os.getenv('LINE_SCALE', '1.0'))
GAP_IMAGE_PRICE = int(os.getenv('GAP_IMAGE_PRICE', str(GAP_PRICE_LINE)))
SHOW_HEADER = _asbool(os.getenv('SHOW_HEADER', 'true'))
SHOW_FOOTER = _asbool(os.getenv('SHOW_FOOTER', 'true'))
HEADER_H = int(os.getenv('HEADER_H', '76'))
FOOTER_H = int(os.getenv('FOOTER_H', '140'))
FOOTER_ICON = os.getenv('FOOTER_ICON', os.path.join(ASSETS_DIR, 'footer_badge.png'))
FOOTER_BRAND = os.getenv('FOOTER_BRAND', 'raika.gg')

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
        _log(f'PRICE_RULES parse fail: {e}')
        return None
RULES_JSON = _load_price_rules()
FALLBACK_THRESHOLDS = [(1000, 'gold'), (500, 'orange'), (200, 'purple'), (0, 'blue')]
DEFAULT_TIER_BACKGROUNDS = {'gold': getattr(CFG, 'BG_TIER_GOLD', os.path.join(ASSETS_DIR, 'bg_tier_gold.png')), 'orange': getattr(CFG, 'BG_TIER_ORANGE', os.path.join(ASSETS_DIR, 'bg_tier_orange.png')), 'purple': getattr(CFG, 'BG_TIER_PURPLE', os.path.join(ASSETS_DIR, 'bg_tier_purple.png')), 'blue': getattr(CFG, 'BG_TIER_BLUE', os.path.join(ASSETS_DIR, 'bg_tier_blue.png')), 'common': os.path.join(ASSETS_DIR, 'bg_tier_common.png')}

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
_FONT, _BOLD_FONT = ({}, {})


# === font helpers (Fortnite first) ===
_FONT_CACHE = {}
def _font(size: int) -> ImageFont.FreeTypeFont:
    key = ("regular", size)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    for path in (os.path.join("font","FORTNITEBATTLEFEST.OTF"),
                 os.path.join("font","FortniteBattleFest.otf"),
                 "arial.ttf"):
        try:
            f = ImageFont.truetype(path, size=size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            pass
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

def _bold_font(size: int) -> ImageFont.FreeTypeFont:
    key = ("bold", size)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    for path in (os.path.join("font","FORTNITEBATTLEFEST.OTF"),
                 os.path.join("font","FortniteBattleFest.otf"),
                 "arialbd.ttf", "Arial Bold.ttf"):
        try:
            f = ImageFont.truetype(path, size=size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            pass
    return _font(size)


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
_canvas_bg_cache = {}

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
_bg_cache = {}

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

def _wrap_fixed(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int, max_lines: int) -> List[str]:
    words = text.split()
    lines = []
    cur = ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if int(draw.textlength(t, font=font)) <= max_w:
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
        while int(draw.textlength(last, font=font)) > max_w and len(last) > 1:
            last = last[:-1]
        if last != lines[-1] and int(draw.textlength(last + '…', font=font)) <= max_w:
            last = last + '…'
        lines[-1] = last
    return lines[:max_lines]

def _fit_center_box(im: Image.Image, box_w: int, box_h: int) -> Image.Image:
    iw, ih = im.size
    k = min(box_w / max(1, iw), box_h / max(1, ih))
    nw, nh = (max(1, int(iw * k)), max(1, int(ih * k)))
    return im.resize((nw, nh), Image.BILINEAR)
ROBLOSECURITY = os.getenv('ROBLOSECURITY') or os.getenv('ROBLOX_COOKIE')

def _auth_headers():
    return {'Cookie': f'.ROBLOSECURITY={ROBLOSECURITY}'} if ROBLOSECURITY else {}

def _read_ready_item(aid: int) -> Optional[Image.Image]:
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        p = os.path.join(READY_ITEM_DIR, f'{aid}{ext}')
        if os.path.exists(p):
            try:
                return Image.open(p).convert('RGBA')
            except Exception:
                pass
    return None

def _write_ready_item(aid: int, im: Image.Image):
    try:
        import os
        p = os.path.join(READY_ITEM_DIR, f"{aid}.png")
        tmp = p + ".tmp"
        im.save(tmp, format="PNG")
        os.replace(tmp, p)
    except Exception:
        pass

async def _download_image_with_cache(url: str) -> Optional[Image.Image]:
    key = 'thumb:' + hashlib.sha1(url.encode()).hexdigest()
    b = await cache.get_bytes(key, THUMB_TTL)
    if b:
        try:
            return Image.open(io.BytesIO(b)).convert('RGBA')
        except Exception:
            pass
    for attempt in range(4):
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)
        try:
            r = await client.get(url, headers=_auth_headers(), timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT))
            r.raise_for_status()
            data = r.content
            await cache.set_bytes(key, data)
            return Image.open(io.BytesIO(data)).convert('RGBA')
        except Exception as e:
            _log(f'[thumb] fetch fail try {attempt + 1}: {e}')
            await asyncio.sleep(0.2 * (attempt + 1))
    return None

async def _fetch_thumbs(ids: List[int], size: str='150x150') -> Dict[int, Image.Image]:
    result = {}
    left = []
    for aid in ids:
        imr = _read_ready_item(int(aid))
        if imr is not None:
            result[int(aid)] = imr
        else:
            left.append(int(aid))
    if not left:
        return result
    base = 'https://thumbnails.roblox.com/v1/assets'
    legacy = 'https://www.roblox.com/asset-thumbnail/image'

    async def one_batch(ch: List[int]):
        proxy = PROXY_POOL.any()
        client = await get_client(proxy)

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
        r = await client.get(base, params={'assetIds': ','.join(map(str, ch)), 'size': size, 'format': 'Png', 'isCircular': 'false', 'returnPolicy': 'PlaceHolder'}, headers=_auth_headers(), timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT))
        r.raise_for_status()
        urls, pending = parse(r.json())
        for d in THUMB_REPOLL_DELAYS:
            if not pending:
                break
            await asyncio.sleep(d)
            rr = await client.get(base, params={'assetIds': ','.join(map(str, pending)), 'size': size, 'format': 'Png', 'isCircular': 'false', 'returnPolicy': 'PlaceHolder'}, headers=_auth_headers(), timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT))
            if rr.status_code == 200:
                got, next_pend = parse(rr.json())
                urls.update(got)
                pending = [a for a in next_pend if a not in urls]
            else:
                break
        if pending:
            w, h = size.split('x')
            for aid in pending:
                urls[aid] = f'{legacy}?assetId={aid}&width={w}&height={h}&format=png'
        return urls
    batches = [left[i:i + 100] for i in range(0, len(left), 100)]
    sem_b = asyncio.Semaphore(THUMB_BATCH_CONCURRENCY)

    async def guarded(b):
        async with sem_b:
            try:
                return await one_batch(b)
            except Exception as e:
                _log(f'[thumb] batch fail {b[:3]}.. {e}')
                return {}
    maps = await asyncio.gather(*(guarded(b) for b in batches))
    url_map = {}
    [url_map.update(m) for m in maps]
    sem_dl = asyncio.Semaphore(THUMB_DL_CONCURRENCY)

    async def dl(aid, url):
        async with sem_dl:
            im = await _download_image_with_cache(url)
            if im is not None:
                result[int(aid)] = im
                _write_ready_item(int(aid), im)
            else:
                _log(f'[thumb] empty for {aid}')
    await asyncio.gather(*(dl(a, u) for a, u in url_map.items()))
    return result

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

def _render_tile(it: Dict[str, Any], thumb: Image.Image, tile: int) -> Image.Image:
    price = _price_of(it)
    tier = _tier_by_price(price)
    name = str(it.get('name') or it.get('assetId') or '')
    out = Image.new('RGBA', (tile, tile), (0, 0, 0, 0))
    out.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    d = ImageDraw.Draw(out)
    d.rectangle([1, 1, tile - 2, tile - 2], outline=(0, 0, 0, 255), width=3)
    title_font = _bold_font(max(18, tile // 7))
    price_font = _bold_font(max(14, tile // 8))
    max_title_w = tile - 14
    max_lines = 1 if TITLE_SINGLE_LINE else 2
    lines = _wrap_fixed(d, name, title_font, max_title_w, max_lines)
    line_h = title_font.getbbox('Ag')[3]
    title_total_h = line_h * len(lines)
    title_top = tile - PADDING_CONTENT - title_total_h
    base_y = title_top
    for i, line in enumerate(lines):
        tw = int(d.textlength(line, font=title_font))
        x = (tile - tw) // 2
        y = base_y + i * line_h
        if THEME_CLASSIC_BLUE:
            d.text((x + 1, y + 1), line, fill=(0, 0, 0, 200), font=title_font)
            d.text((x, y), line, fill=(255, 255, 255, 255), font=title_font)
        else:
            d.text((x, y), line, fill=(0, 0, 0, 255), font=title_font)
    line_th = max(1, int(round(2 * LINE_SCALE)))
    line_y = title_top - GAP_LINE_TITLE
    col = _tier_color(tier)
    d.line([(PADDING_CONTENT, line_y), (tile - PADDING_CONTENT, line_y)], fill=(col[0], col[1], col[2], 255), width=line_th)
    price_text = f'{price} {ROBUX_PREFIX}'.strip()
    w_text = int(d.textlength(price_text, font=price_font))
    pill_pad_x, pill_pad_y = (7, 4)
    pill_w = w_text + pill_pad_x * 2
    pill_h = price_font.getbbox('Ag')[3] + pill_pad_y * 2 - 2
    right = tile - (PADDING_CONTENT + 2)
    bottom = max(line_y - GAP_PRICE_LINE, PADDING_CONTENT + 18)
    top = bottom - pill_h
    left = right - pill_w
    d.rounded_rectangle([left, top, right, bottom], radius=pill_h // 2, fill=(255, 255, 255, 235), outline=(0, 0, 0, 200), width=1)
    d.text((left + (pill_w - w_text) // 2, top + (pill_h - price_font.getbbox('Ag')[3]) // 2 - 1), price_text, fill=(0, 0, 0, 255), font=price_font)
    box_top = PADDING_CONTENT
    box_bottom = top - GAP_IMAGE_PRICE
    box_h = max(1, box_bottom - box_top)
    box_w = tile - PADDING_CONTENT * 2
    im2 = _fit_center_box(thumb, box_w, box_h)
    im_x = PADDING_CONTENT + (box_w - im2.width) // 2
    im_y = box_top + (box_h - im2.height) // 2
    out.alpha_composite(im2, (im_x, im_y))
    return out

def _draw_header(canvas: Image.Image, count: int, title: str):
    if not SHOW_HEADER:
        return
    W, H = canvas.size
    band = Image.new('RGBA', (W, HEADER_H), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, 0))
    d = ImageDraw.Draw(canvas)
    big = _bold_font(max(26, HEADER_H // 2))
    small = _bold_font(max(20, HEADER_H // 3))
    x = 16
    y = 8
    d.text((x, y), f'{count}', fill=(255, 255, 255, 255), font=big)
    y2 = y + big.getbbox('Ag')[3] - 4
    d.text((x, y2), f'{title}', fill=(255, 255, 255, 220), font=small)

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
    date_text = datetime.datetime.now().strftime('%d %B %Y')
    who = username if username and str(username).strip() else str(user_id) if user_id is not None else '@unknown'
    if isinstance(who, str) and who and (not who.startswith('@')) and (not who.isdigit()):
        who = f'@{who}'
    line1 = date_text
    line2 = f'Проверено: {who}'
    line3 = f'{FOOTER_BRAND}'
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
        if s1 > MIN:
            s1 -= 1
        if s2 > MIN:
            s2 -= 1
        if s3 > MIN:
            s3 -= 1
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

async def _render_grid(items: List[Dict[str, Any]], tile: int=150, title: str='Items', username: Optional[str]=None, user_id: Optional[int]=None) -> bytes:
    if not KEEP_INPUT_ORDER:
        items = sorted(items, key=lambda x: x.get('priceInfo', {}).get('value') or 0, reverse=True)
    ids = [int(x['assetId']) for x in items if 'assetId' in x]
    thumbs = await _fetch_thumbs(ids, size=f'{tile}x{tile}')
    n = len(items)
    cols = min(10, max(1, n))
    rows = int(math.ceil(n / cols))
    W0 = cols * tile
    H0 = rows * tile
    top = HEADER_H if SHOW_HEADER else 0
    bottom = FOOTER_H if SHOW_FOOTER else 0
    W = W0
    H = H0 + top + bottom + 2
    canvas = Image.new('RGBA', (W, H))
    canvas.alpha_composite(_get_canvas_bg(W, H), (0, 0))
    _draw_header(canvas, n, title)
    _draw_footer(canvas, username, user_id)
    sem = asyncio.Semaphore(RENDER_CONCURRENCY)

    async def make_tile(it):
        async with sem:
            aid = int(it['assetId'])
            thumb = thumbs.get(aid) or Image.new('RGBA', (tile - 12, tile - 26), (70, 80, 96, 255))
            return _render_tile(it, thumb, tile)
    tiles = await asyncio.gather(*(make_tile(it) for it in items))
    k = 0
    for r in range(rows):
        for c in range(cols):
            if k >= n:
                break
            canvas.alpha_composite(tiles[k], (c * tile, top + r * tile))
            k += 1
    out = io.BytesIO()
    canvas.convert('RGB').save(out, 'PNG', optimize=True, quality=90)
    return out.getvalue()

async def generate_full_inventory_grid(items: List[Dict[str, Any]], tile: int=150, pad: int=0, username: Optional[str]=None, user_id: Optional[int]=None, title: Optional[int]=None) -> bytes:
    return await _render_grid(items, tile=tile, title=title or 'Инвентарь', username=username, user_id=user_id)

async def generate_inventory_preview(tg_id: int, roblox_id: int, categories_limit: int=8, username: Optional[str]=None) -> bytes:
    from roblox_client import get_full_inventory
    data = await get_full_inventory(tg_id, roblox_id)
    items = []
    for arr in (data.get('byCategory') or {}).values():
        items.extend(arr)
    return await _render_grid(items, tile=150, title='Инвентарь', username=username, user_id=tg_id)

async def generate_category_sheets(tg_id: int, roblox_id: int, category: str, limit: int=0, tile: int=150, force: bool=False, username: Optional[str]=None) -> bytes:
    from roblox_client import get_full_inventory
    data = await get_full_inventory(tg_id, roblox_id)
    items = (data.get('byCategory') or {}).get(category, [])
    if limit and limit > 0:
        items = items[:limit]
    return await _render_grid(items, tile=tile, title=category, username=username, user_id=tg_id)

def _render_tile(it: dict, thumb: Image.Image, tile: int) -> Image.Image:
    # data
    price = _price_of(it)
    tier  = _tier_by_price(price)
    name  = str(it.get("name") or it.get("assetId") or "").upper()

    # canvas
    out = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
    out.alpha_composite(_get_tier_bg(tier, tile), (0, 0))
    d = ImageDraw.Draw(out)

    # fonts
    title_font = _bold_font(max(12, tile // max(1, TITLE_FONT_TILE_DIV)))
    price_font = _bold_font(max(10, tile // max(1, PRICE_FONT_TILE_DIV)))

    # bottom title (fit to one line)
    max_title_w = tile - 14
    title_text = name
    size_try = max(12, tile // max(1, TITLE_FONT_TILE_DIV))
    while size_try > 10 and int(d.textlength(title_text, font=_bold_font(size_try))) > max_title_w:
        size_try -= 1
    title_font = _bold_font(size_try)
    line_h = title_font.getbbox("Ag")[3]
    y_bottom = tile - TEXT_BOTTOM_PAD_PX
    y_top = y_bottom - line_h
    tw = int(d.textlength(title_text, font=title_font))
    tx = (tile - tw) // 2
    ty = y_top
    d.text((tx, ty), title_text, fill=TITLE_TEXT_COLOR, font=title_font)

    # price pill (top-right)
    price_text = f"{price} {ROBUX_PREFIX}".strip()
    w_text = int(d.textlength(price_text, font=price_font))
    pill_w = w_text + PRICE_PILL_PAD_X * 2
    pill_h = price_font.getbbox("Ag")[3] + PRICE_PILL_PAD_Y * 2 - 2
    right = tile - (PADDING_CONTENT + 2)
    top   = PADDING_CONTENT + PRICE_TOP_PAD_PX
    bottom= top + pill_h
    left  = right - pill_w
    d.rounded_rectangle([left, top, right, bottom],
                        radius=PRICE_PILL_RADIUS_PX or pill_h // 2,
                        fill=PRICE_PILL_FILL,
                        outline=PRICE_PILL_OUTLINE,
                        width=PRICE_PILL_OUTLINE_PX)
    d.text((left + (pill_w - w_text)//2,
            top + (pill_h - price_font.getbbox("Ag")[3])//2 - 1),
           price_text, fill=PRICE_TEXT_COLOR, font=price_font)

    # image region between price and bottom title
    box_top    = bottom + GAP_IMAGE_PRICE_PX
    box_bottom = y_top - GAP_IMAGE_TEXT_PX
    box_h      = max(1, box_bottom - box_top)
    box_w      = tile - PADDING_CONTENT * 2
    im2        = _fit_center_box(thumb, box_w, box_h)
    im_x       = PADDING_CONTENT + (box_w - im2.width) // 2
    im_y       = box_top + (box_h - im2.height) // 2
    out.alpha_composite(im2, (im_x, im_y))

    return out
