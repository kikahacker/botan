from __future__ import annotations
import os, random, asyncio, httpx
from typing import Optional, Dict
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=20.0, read=20.0, write=20.0)
_LIMITS = httpx.Limits(max_connections=200, max_keepalive_connections=100)

class ProxyPool:

    def __init__(self) -> None:
        self._proxies: list[str] = []
        base = os.path.dirname(__file__)
        p = os.path.join(base, 'proxies.txt')
        if os.path.exists(p):
            for line in open(p, 'r', encoding='utf-8', errors='ignore'):
                s = line.strip()
                if s and (not s.startswith('#')):
                    self._proxies.append(s)
        env = os.getenv('PROXIES')
        if env:
            for s in env.split(','):
                s = s.strip()
                if s:
                    self._proxies.append(s)
        seen, uniq = (set(), [])
        for x in self._proxies:
            if x not in seen:
                uniq.append(x)
                seen.add(x)
        self._proxies = uniq

    def any(self) -> Optional[str]:
        if not self._proxies:
            return None
        return random.choice(self._proxies)

    def list(self) -> list[str]:
        return list(self._proxies)
PROXY_POOL = ProxyPool()
_clients: Dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()

def _headers_base() -> dict:
    return {'Accept': 'application/json, text/plain, */*', 'Accept-Encoding': 'gzip, deflate, br', 'User-Agent': 'Mozilla/5.0 (compatible; Botik/1.0; +https://example.local)'}

async def get_client(proxy: Optional[str]=None) -> httpx.AsyncClient:
    key = proxy or 'none'
    async with _lock:
        if key in _clients and (not _clients[key].is_closed):
            return _clients[key]
        _clients[key] = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_LIMITS, http2=True, headers=_headers_base(), proxies=proxy)
        return _clients[key]

async def client() -> httpx.AsyncClient:
    return await get_client(None)

async def close_all():
    async with _lock:
        for c in list(_clients.values()):
            try:
                await c.aclose()
            except Exception:
                pass
        _clients.clear()