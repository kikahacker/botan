from __future__ import annotations
import os, io, json, time, hashlib, asyncio
from typing import Optional
CACHE_DIR = os.getenv('CACHE_DIR', 'cache')
MAX_SIZE_MB = int(os.getenv('CACHE_MAX_MB', '5120'))
os.makedirs(CACHE_DIR, exist_ok=True)
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

def _key_to_path(key: str) -> str:
    h = hashlib.sha1(key.encode('utf-8')).hexdigest()
    return os.path.join(CACHE_DIR, h)

async def acquire(key: str):
    """Асинхронный per-key lock."""
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = asyncio.Lock()
    await lock.acquire()

    class _R:

        def __aenter__(self):
            return None

        async def __aenter__(self):
            return None

        def __aexit__(self, exc_type, exc, tb):
            lock.release()

        async def __aexit__(self, exc_type, exc, tb):
            lock.release()
    return _R()

def _file_fresh(path: str, ttl: int) -> bool:
    try:
        mtime = os.path.getmtime(path)
    except FileNotFoundError:
        return False
    return time.time() - mtime <= ttl

def _cleanup_if_needed():
    total = 0
    entries = []
    for name in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, name)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        total += st.st_size
        entries.append((st.st_mtime, p, st.st_size))
    limit = MAX_SIZE_MB * 1024 * 1024
    if total <= limit:
        return
    entries.sort(key=lambda x: x[0])
    need = total - limit
    freed = 0
    for _, p, sz in entries:
        try:
            os.remove(p)
            freed += sz
        except Exception:
            pass
        if freed >= need:
            break

async def get_bytes(key: str, ttl: int) -> Optional[bytes]:
    path = _key_to_path(key)
    if not _file_fresh(path, ttl):
        return None
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return None

async def set_bytes(key: str, data: bytes) -> None:
    path = _key_to_path(key)
    try:
        with open(path, 'wb') as f:
            f.write(data)
    finally:
        _cleanup_if_needed()

async def get_json(key: str, ttl: int):
    b = await get_bytes(key, ttl)
    if b is None:
        return None
    try:
        return json.loads(b.decode('utf-8'))
    except Exception:
        return None

async def set_json(key: str, obj) -> None:
    await set_bytes(key, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

async def delete(key: str) -> None:
    path = _key_to_path(key)
    try:
        os.remove(path)
    except Exception:
        pass