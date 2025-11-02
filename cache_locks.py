import asyncio
from typing import Dict
_locks: Dict[str, asyncio.Lock] = {}

def get_lock(key: str) -> asyncio.Lock:
    """Singleflight-замок на ключ — чтобы параллельные запросы не дублировали работу."""
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock