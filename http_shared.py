from __future__ import annotations
import os, random, asyncio, httpx
from typing import Optional, Dict

# Tighter connect timeout + longer read (tunable via env)
_DEFAULT_TIMEOUT = httpx.Timeout(
    float(os.getenv("HTTP_CONNECT_TIMEOUT", "3.0")),
    connect=float(os.getenv("HTTP_CONNECT_TIMEOUT", "3.0")),
    read=float(os.getenv("HTTP_READ_TIMEOUT", "15.0")),
    write=float(os.getenv("HTTP_WRITE_TIMEOUT", "10.0")),
    pool=float(os.getenv("HTTP_POOL_TIMEOUT", "3.0")),
)
_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("HTTP_MAX_CONNECTIONS", "300")),
    max_keepalive_connections=int(os.getenv("HTTP_MAX_KEEPALIVE", "150")),
    keepalive_expiry=float(os.getenv("HTTP_KEEPALIVE_SECS", "30.0")),
)

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
    def pick(self) -> Optional[str]:
        if not self._proxies:
            return None
        return random.choice(self._proxies)

_clients: Dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()
_proxy_pool = ProxyPool()

def _headers_base() -> dict:
    return {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': os.getenv("HTTP_USER_AGENT", "Mozilla/5.0 (compatible; Botik/1.0)")
    }

async def get_client(proxy: Optional[str]=None) -> httpx.AsyncClient:
    key = proxy or 'none'
    async with _lock:
        client = _clients.get(key)
        if client and (not client.is_closed):
            return client
        client = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            limits=_LIMITS,
            http2=True,
            headers=_headers_base(),
            proxies=proxy,
            trust_env=False,
        )
        _clients[key] = client
        return client

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