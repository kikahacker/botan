"""
Конфигурация проекта Roblox Bot.
✔️ Поддержка Python 3.9
✔️ Автоматическая загрузка .env
✔️ Безопасная работа с FERNET_KEY
"""
import os
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv(override=False)

def _env_bool(name: str, default: bool=False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'y', 'on')

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

class _CFG(object):
    BOT_TOKEN: str = os.getenv('BOT_TOKEN', '')
    OWNER_ID: int = _env_int('OWNER_ID', 0)
    ALLOW_PUBLIC_COOKIE: bool = _env_bool('ALLOW_PUBLIC_COOKIE', True)
    FERNET_KEY: Optional[str] = os.getenv('FERNET_KEY')
    TIMEOUT: float = _env_float('HTTP_TIMEOUT', 15.0)
    CONCURRENT: int = _env_int('HTTP_CONCURRENT', 6)
    HTTP_PROXY: Optional[str] = os.getenv('HTTP_PROXY') or None
    DEBUG: bool = _env_bool('DEBUG', False)
    DEFAULT_CACHE_TTL_MIN: int = _env_int('DEFAULT_CACHE_TTL_MIN', 10)
    BRAND_NAME: str = os.getenv('BRAND_NAME', 'Roblox Private Bot')
    ASSETS_DIR: str = os.getenv('ASSETS_DIR', 'assets')
    TEMP_DIR: str = os.getenv('TEMP_DIR', 'temp')
CFG = _CFG()
ASSET_TYPES: List[str] = ['Hat', 'HairAccessory', 'FaceAccessory', 'NeckAccessory', 'ShoulderAccessory', 'FrontAccessory', 'BackAccessory', 'WaistAccessory', 'Gear', 'Face', 'TShirt', 'Shirt', 'Pants', 'Sweater', 'Jacket', 'DressSkirt', 'Shorts', 'Decal', 'Model', 'Audio', 'Mesh', 'Plugin', 'Package', 'EmoteAnimation', 'Animation']
_env_types = os.getenv('ASSET_TYPES_CSV')
if _env_types:
    parsed = [t.strip() for t in _env_types.split(',') if t.strip()]
    if parsed:
        ASSET_TYPES = parsed
if not CFG.FERNET_KEY:
    print('\n⚠️  ВНИМАНИЕ: FERNET_KEY не найден!\n   Добавь его в .env, например:\n   FERNET_KEY=AbCdEfGhIjKlMnOpQrStUvWxYz1234567890abcDEF=\n   (Сгенерировать можно: from cryptography.fernet import Fernet; print(Fernet.generate_key().decode()))\n')
else:
    print(f'[CFG] ✅ FERNET_KEY загружен (длина {len(CFG.FERNET_KEY)})')