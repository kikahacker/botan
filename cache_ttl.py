from __future__ import annotations
import time
from typing import Optional, Any

# we expect your project to have a cache module with async get_json/set_json and optional delete
try:
    import cache  # type: ignore
except Exception as e:
    raise RuntimeError("cache_ttl requires a 'cache' module with get_json/set_json") from e

async def get_json_ttl(key: str) -> Optional[dict]:
    """Return cached payload['data'] if not expired (payload['_exp'] > now)."""
    obj = await cache.get_json(key)
    if not obj:
        return None
    exp = obj.get("_exp")
    if isinstance(exp, (int, float)) and exp > time.time():
        return obj.get("data")
    # expired: try to delete silently
    try:
        if hasattr(cache, "delete"):
            await cache.delete(key)
    except Exception:
        pass
    return None

async def set_json_ttl(key: str, data: Any, ttl_seconds: int) -> None:
    payload = {"_exp": time.time() + int(ttl_seconds), "data": data}
    await cache.set_json(key, payload)
