from __future__ import annotations
import os, json, pathlib
from functools import lru_cache
from typing import Any, Dict, Optional
import contextvars

# ===== CONFIG =====
_DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en").lower()
DEFAULT_LANG_TTL_MIN = int(os.getenv("DEFAULT_LANG_TTL_MIN", "43200"))  # 30 days
_LOCALE_DIR = pathlib.Path(__file__).parent / "locales"

# ===== CONTEXT VAR =====
_CURRENT_LANG: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_lang", default=_DEFAULT_LANG
)

# ===== LOCALE LOADING =====
@lru_cache(maxsize=1)
def _load_all_locales() -> Dict[str, Dict[str, Any]]:
    data: Dict[str, Dict[str, Any]] = {}
    if _LOCALE_DIR.exists():
        for p in _LOCALE_DIR.glob("*.json"):
            try:
                with p.open("r", encoding="utf-8") as f:
                    data[p.stem.lower()] = json.load(f)
            except Exception:
                pass
    return data

def _get_by_path(d: Dict[str, Any], dotted: str) -> Optional[str]:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, str) else None

def _norm_lang(lang: Optional[str]) -> str:
    if not lang:
        return _DEFAULT_LANG
    lang = lang.lower()
    if "-" in lang:
        lang = lang.split("-")[0]
    locs = _load_all_locales()
    return lang if lang in locs else _DEFAULT_LANG

def _lookup(lang: str, key: str) -> Optional[str]:
    lang = _norm_lang(lang)
    locs = _load_all_locales()
    val = _get_by_path(locs.get(lang, {}), key)
    if val is None:
        val = _get_by_path(locs.get(_DEFAULT_LANG, {}), key)
    return val if isinstance(val, str) else None

# ===== TRANSLATION =====
def tr(lang: str, key: str, **fmt: Any) -> str:
    val = _lookup(lang, key)
    if val is None:
        return key
    try:
        return val.format(**fmt) if fmt else val
    except Exception:
        return val

def t(key: str, **fmt: Any) -> str:
    lang = _CURRENT_LANG.get()
    return tr(lang, key, **fmt)

# ===== USER LANG STORAGE =====
async def get_user_lang(storage, user_id: int, fallback: Optional[str] = None) -> str:
    try:
        lang = await storage.get_cached_data(int(user_id), "lang")
        return _norm_lang(lang or fallback or _DEFAULT_LANG)
    except Exception:
        return _norm_lang(fallback or _DEFAULT_LANG)

async def set_user_lang(storage, user_id: int, lang: str) -> bool:
    try:
        await storage.set_cached_data(
            int(user_id), "lang", str(lang).lower(), DEFAULT_LANG_TTL_MIN
        )
        _CURRENT_LANG.set(_norm_lang(lang))
        return True
    except Exception:
        return False

# ===== LANG CONTEXT =====
def set_current_lang(lang: str) -> None:
    _CURRENT_LANG.set(_norm_lang(lang))

def get_current_lang() -> str:
    return _CURRENT_LANG.get()

def available_langs() -> list[str]:
    return sorted(_load_all_locales().keys())

def reload_locales() -> None:
    try:
        _load_all_locales.cache_clear()
    except Exception:
        pass
