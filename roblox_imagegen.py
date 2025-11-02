
import os, io, math, asyncio, logging, time, csv
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

# =========================
# Tunables
# =========================
TILE_DEFAULT = int(os.getenv("TILE", "150"))
HEADER_H = 64
FOOTER_H = 46
SHOW_HEADER = True
SHOW_FOOTER = True
KEEP_INPUT_ORDER = True

READY_ITEM_DIR = os.getenv("READY_ITEM_DIR", "item_images")

TITLE_TEXT_COLOR = (255, 255, 255, 255)  # white
PRICE_PILL_FILL = (255, 255, 255, 235)   # semi-white
PRICE_TEXT_COLOR = (0, 0, 0, 255)        # black for contrast on white pill

# Telegram limits (hard 8000px per side, ~64M pixels total)
MAX_TG_DIM    = int(os.getenv("MAX_TG_DIM", "8000"))
MAX_TG_PIXELS = int(os.getenv("MAX_TG_PIXELS", "64000000"))

logger = logging.getLogger(__name__)

# =========================
# Fonts (stable, cached)
# =========================
_FONT_CACHE: Dict[int, ImageFont.FreeTypeFont] = {}

def _font(sz: int):
    f = _FONT_CACHE.get(sz)
    if f:
        return f
    try:
        f = ImageFont.truetype("font/FORTNITEBATTLEFEST.OTF", sz)
    except Exception:
        f = ImageFont.load_default()
    _FONT_CACHE[sz] = f
    return f

def _bold_font(sz: int):
    return _font(sz)

# =========================
# Helpers & caches (30-min TTL)
# =========================
_IMAGE_INDEX: Dict[int, str] = {}
_IMAGE_INDEX_TS: float = 0.0
_IMAGE_DIR = READY_ITEM_DIR
_IMAGE_TTL_SEC = 1800  # 30 min
_VALID_EXT = {".png", ".jpg", ".jpeg", ".webp"}

def _build_image_index_cached(force: bool=False) -> None:
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

def _read_ready_item(aid: int) -> Optional[Image.Image]:
    p = _IMAGE_INDEX.get(int(aid))
    if p and os.path.exists(p):
        try:
            return Image.open(p).convert("RGBA")
        except Exception as e:
            logger.warning(f"[local] open fail for {p}: {e}")
            return None
    # fallback legacy probing
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        q = os.path.join(READY_ITEM_DIR, f"{aid}{ext}")
        if os.path.exists(q):
            try:
                return Image.open(q).convert("RGBA")
            except Exception as e:
                logger.warning(f"[local] open fail for {q}: {e}")
    return None

_PRICES_CACHE: Optional[Dict[int, Dict[str, Any]]] = None
_PRICES_TS: float = 0.0
_PRICES_MTIME: float = -1.0
_PRICES_TTL_SEC = 1800  # 30 min

def load_prices_csv_cached(path: str = "prices.csv") -> Dict[int, Dict[str, Any]]:
    global _PRICES_CACHE, _PRICES_TS, _PRICES_MTIME
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return {}
    now = time.time()
    if (_PRICES_CACHE is not None) and ((now - _PRICES_TS) < _PRICES_TTL_SEC) and (_PRICES_MTIME == mtime):
        return _PRICES_CACHE

    out: Dict[int, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            id_raw = row.get("id") or row.get("assetId")
            if not id_raw:
                continue
            try:
                aid = int(float(id_raw))
            except Exception:
                continue
            name = (row.get("name") or "").strip()
            try:
                price_val = int(float(row.get("price") or 0))
            except Exception:
                price_val = 0
            out[aid] = {"name": name, "priceInfo": {"value": price_val}}

    _PRICES_CACHE = out
    _PRICES_TS = now
    _PRICES_MTIME = mtime
    return out

# =========================
# Drawing utils
# =========================
def _get_canvas_bg(W: int, H: int) -> Image.Image:
    # simple dark background
    im = Image.new("RGBA", (W, H), (24, 26, 32, 255))
    return im

def _price_pill(draw: ImageDraw.ImageDraw, right: int, top: int, text: str, font: ImageFont.ImageFont) -> Tuple[int, int, int, int]:
    w = int(draw.textlength(text, font=font)) if hasattr(draw, "textlength") else draw.textbbox((0,0), text, font=font)[2]
    pad_x, pad_y = 8, 5
    pill_w = w + pad_x*2
    bbox = draw.textbbox((0,0), "Ag", font=font)
    text_h = bbox[3] - bbox[1]
    pill_h = text_h + pad_y*2
    left = right - pill_w
    bottom = top + pill_h
    radius = max(6, pill_h // 2)
    draw.rounded_rectangle([left, top, right, bottom], radius=radius, fill=PRICE_PILL_FILL)
    # center text
    tx = left + (pill_w - w)//2
    ty = top + (pill_h - text_h)//2 - 1
    draw.text((tx, ty), text, fill=PRICE_TEXT_COLOR, font=font)
    return left, top, right, bottom

def _render_tile(it: Dict[str, Any], thumb: Image.Image, tile: int) -> Image.Image:
    im = Image.new("RGBA", (tile, tile), (40, 150, 245, 255))  # blue-ish tier bg
    d = ImageDraw.Draw(im)

    # price pill
    price_val = (it.get("priceInfo") or {}).get("value")
    price_txt = f"{int(price_val)} R$" if price_val is not None else ""
    price_font = _font(max(12, tile // 10))
    if price_txt:
        _price_pill(d, tile - 8, 8, price_txt, price_font)

    # thumb box
    box_margin = 10
    title_font = _font(max(14, tile // 7))
    bbox = d.textbbox((0,0), "Ag", font=title_font)
    title_h = (bbox[3] - bbox[1])
    box_top = 16 + (price_font.size + 10)
    box_bottom = tile - title_h - 10
    box_top = max(box_top, 10)
    box_bottom = max(box_bottom, box_top + 40)
    # place thumb fit
    tw, th = thumb.size
    avail_w = tile - box_margin*2
    avail_h = box_bottom - box_top
    scale = min(avail_w / max(1, tw), avail_h / max(1, th))
    new_w = max(1, int(tw * scale))
    new_h = max(1, int(th * scale))
    thumb_resized = thumb.resize((new_w, new_h), Image.BILINEAR)
    px = (tile - new_w)//2
    py = box_top + (avail_h - new_h)//2
    im.alpha_composite(thumb_resized, (px, py))

    # title — uppercase + white, single line, center
    name = str(it.get("name") or it.get("assetId") or "").upper()
    title_txt = name
    max_w = tile - 12
    # truncate
    while True:
        tw2 = int(d.textlength(title_txt, font=title_font)) if hasattr(d, "textlength") else d.textbbox((0,0), title_txt, font=title_font)[2]
        if tw2 <= max_w or len(title_txt) <= 1:
            break
        title_txt = title_txt[:-1]
    if title_txt != name:
        if (int(d.textlength(title_txt + "…", font=title_font)) if hasattr(d, "textlength") else d.textbbox((0,0), title_txt + "…", font=title_font)[2]) <= max_w:
            title_txt += "…"

    tw2 = int(d.textlength(title_txt, font=title_font)) if hasattr(d, "textlength") else d.textbbox((0,0), title_txt, font=title_font)[2]
    tx = (tile - tw2)//2
    ty = tile - title_h - 6
    d.text((tx, ty), title_txt, fill=TITLE_TEXT_COLOR, font=title_font)

    return im

def _draw_header(canvas: Image.Image, count: int, title: str) -> None:
    if not SHOW_HEADER:
        return
    W, H = canvas.size
    band = Image.new("RGBA", (W, HEADER_H), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, 0))
    d = ImageDraw.Draw(canvas)
    font = _bold_font(max(26, HEADER_H // 2))
    text = f"{count}  {title}"
    if hasattr(d, "textlength"):
        tw = int(d.textlength(text, font=font))
        th = font.getbbox("Ag")[3]
    else:
        tw = d.textbbox((0,0), text, font=font)[2]
        th = d.textbbox((0,0), "Ag", font=font)[3]
    x = max(10, (W - tw)//2)
    y = max(6, (HEADER_H - th)//2)
    d.text((x, y), text, fill=(255,255,255,255), font=font)

def _draw_footer(canvas: Image.Image, username: Optional[str], user_id: Optional[int]) -> None:
    if not SHOW_FOOTER:
        return
    W, H = canvas.size
    band = Image.new("RGBA", (W, FOOTER_H), (0, 0, 0, 255))
    canvas.alpha_composite(band, (0, H - FOOTER_H))
    d = ImageDraw.Draw(canvas)
    font = _font(18)
    text = f"@{username}" if username else (str(user_id) if user_id else "")
    if not text:
        return
    if hasattr(d, "textlength"):
        tw = int(d.textlength(text, font=font))
        th = font.getbbox("Ag")[3]
    else:
        tw = d.textbbox((0,0), text, font=font)[2]
        th = d.textbbox((0,0), "Ag", font=font)[3]
    x = max(10, W - tw - 10)
    y = H - FOOTER_H + max(6, (FOOTER_H - th)//2)
    d.text((x, y), text, fill=(200,200,200,255), font=font)

# --- Telegram safety clamp ---
def _tg_safe_resize(im: Image.Image) -> Image.Image:
    W, H = im.width, im.height
    if W <= 0 or H <= 0:
        return im
    if W <= MAX_TG_DIM and H <= MAX_TG_DIM and (W * H) <= MAX_TG_PIXELS:
        return im
    sx = MAX_TG_DIM / W
    sy = MAX_TG_DIM / H
    sp = math.sqrt(MAX_TG_PIXELS / (W * H))
    scale = min(sx, sy, sp, 1.0)  # no upscaling
    new_w = max(1, int(W * scale))
    new_h = max(1, int(H * scale))
    return im.resize((new_w, new_h), Image.LANCZOS)

# =========================
# Thumbs
# =========================
async def _fetch_thumbs(ids: List[int], size: Tuple[int, int]=(256,256)) -> Dict[int, Image.Image]:
    _build_image_index_cached()
    out: Dict[int, Image.Image] = {}
    for aid in ids:
        im = _read_ready_item(aid)
        if im is None:
            # placeholder
            w, h = size
            ph = Image.new("RGBA", (w, h), (70, 80, 96, 255))
            out[aid] = ph
        else:
            out[aid] = im
    return out

# =========================
# Public: Telegram-safe pagination album
# =========================
async def generate_overall_inventory_album(items: List[Dict[str, Any]], tile: int=TILE_DEFAULT,
                                           username: Optional[str]=None, user_id: Optional[int]=None,
                                           title: str="Все предметы") -> List[bytes]:
    n = len(items)
    if n == 0:
        return []

    # enrich with prices/names if missing
    pm = load_prices_csv_cached("prices.csv")
    for it in items:
        try:
            aid = int(it.get("assetId"))
        except Exception:
            continue
        rec = pm.get(aid)
        if rec:
            it.setdefault("name", rec.get("name"))
            if not it.get("priceInfo") or it.get("priceInfo", {}).get("value") in (None, 0):
                it["priceInfo"] = rec.get("priceInfo")

    if not KEEP_INPUT_ORDER:
        items = sorted(items, key=lambda x: x.get("priceInfo", {}).get("value") or 0, reverse=True)

    ids = [int(x["assetId"]) for x in items if "assetId" in x]
    size = (256,256)
    thumbs = await _fetch_thumbs(ids, size=size)

    header_h = HEADER_H if SHOW_HEADER else 0
    footer_h = FOOTER_H if SHOW_FOOTER else 0
    band_h = header_h + footer_h + 2

    def dims_ok(c, r, tl):
        W = c * tl
        H = r * tl + band_h
        return (W > 0 and H > 0 and W <= MAX_TG_DIM and H <= MAX_TG_DIM and (W * H) <= MAX_TG_PIXELS)

    # initial guesses that respect limits
    max_cols = max(1, min(MAX_TG_DIM // max(1, tile), int(math.sqrt(max(1, MAX_TG_PIXELS // max(1, tile*tile))))))
    max_rows = max(1, min((MAX_TG_DIM - band_h) // max(1, tile), MAX_TG_PIXELS // max(1, tile*max(1, max_cols))))

    # if even that doesn't fit, shrink tile (down to 96px)
    while not dims_ok(max_cols, max_rows, tile) and tile > 96:
        tile = int(tile * 0.9)
        max_cols = max(1, min(MAX_TG_DIM // max(1, tile), int(math.sqrt(max(1, MAX_TG_PIXELS // max(1, tile*tile))))))
        max_rows = max(1, min((MAX_TG_DIM - band_h) // max(1, tile), MAX_TG_PIXELS // max(1, tile*max(1, max_cols))))

    pages: List[bytes] = []
    i = 0
    while i < n:
        rem = n - i
        cols = min(max_cols, rem)
        rows = max(1, (rem + cols - 1) // cols)
        rows = min(rows, max_rows)

        # iterative shrink until fits
        while not dims_ok(cols, rows, tile):
            if cols > 1 and (cols >= rows or cols * tile > MAX_TG_DIM):
                cols -= 1
            elif rows > 1:
                rows -= 1
            else:
                break

        if not dims_ok(cols, rows, tile):
            if cols > 1:
                cols = 1
                rows = min(rem, max(1, (MAX_TG_DIM - band_h) // max(1, tile)))
            elif rows > 1:
                rows = 1
                cols = min(rem, max(1, MAX_TG_DIM // max(1, tile)))
            while not dims_ok(cols, rows, tile) and (cols > 1 or rows > 1):
                if cols >= rows and cols > 1: cols -= 1
                elif rows > 1: rows -= 1
                else: break

        cols = max(1, cols)
        rows = max(1, rows)
        page_cap = max(1, min(rem, rows * cols))
        chunk = items[i:i+page_cap]

        W = cols * tile
        H = rows * tile + band_h

        canvas = Image.new("RGBA", (W, H))
        canvas.alpha_composite(_get_canvas_bg(W, H), (0, 0))
        _draw_header(canvas, n, title)
        _draw_footer(canvas, username, user_id)

        sem = asyncio.Semaphore(int(os.getenv("RENDER_CONCURRENCY", "12")))
        async def make_tile(it):
            async with sem:
                aid = int(it["assetId"])
                thumb = thumbs.get(aid) or Image.new("RGBA", (tile - 12, tile - 26), (70, 80, 96, 255))
                return _render_tile(it, thumb, tile)

        tiles = await asyncio.gather(*(make_tile(it) for it in chunk))

        k = 0
        top_offset = header_h
        for r in range(rows):
            for c in range(cols):
                if k >= len(tiles):
                    break
                canvas.alpha_composite(tiles[k], (c * tile, top_offset + r * tile))
                k += 1

        # Safety clamp for Telegram limits
        canvas = _tg_safe_resize(canvas)

        out = io.BytesIO()
        canvas.convert("RGB").save(out, "PNG", optimize=True, quality=90)
        pages.append(out.getvalue())

        i += page_cap

    return pages

# Optional helper if you want to fetch inventory inside this module (requires your client)
async def generate_all_items_album(tg_id: int, roblox_id: int, tile: int = TILE_DEFAULT,
                                   username: Optional[str]=None, title: str="Все предметы") -> List[bytes]:
    try:
        from roblox_client import get_full_inventory  # your project-specific client
    except Exception as e:
        raise RuntimeError("roblox_client.get_full_inventory is required for generate_all_items_album") from e
    data = await get_full_inventory(tg_id, roblox_id)
    items: List[Dict[str, Any]] = []
    for arr in (data.get("byCategory") or {}).values():
        items.extend(arr)
    return await generate_overall_inventory_album(items, tile=tile, username=username, user_id=tg_id, title=title)

# Backward-compatible stubs (so imports don't break)
async def generate_category_sheets(*args, **kwargs):
    return []

async def generate_full_inventory_grid(*args, **kwargs):
    # Kept for compatibility; use generate_overall_inventory_album instead.
    return b""
