"""
Shared HTTP utilities with proxy pooling.

Goals:
- Reuse httpx.AsyncClient instances (connection pooling).
- Support a finite proxy list while handling user counts >> proxy count.
- Limit concurrent requests per proxy to avoid "everything dies" when load spikes.
"""

from __future__ import annotations

import asyncio
import os
import threading
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

PROXY_FILE = os.getenv("PROXY_FILE", "proxies.txt")

_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_LIMITS = httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30.0)

# Tweakable: how many simultaneous requests we allow through ONE proxy.
# If you have strong proxies, you can bump it; if they are weak, drop it.
MAX_CONCURRENCY_PER_PROXY = int(os.getenv("MAX_CONCURRENCY_PER_PROXY", "6"))

# If a proxy keeps erroring, we temporarily cool it down and avoid selecting it.
FAIL_COOLDOWN_SECONDS = int(os.getenv("PROXY_FAIL_COOLDOWN", "15"))


def _normalize_proxy(line: str) -> Optional[str]:
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None

    # Allow formats like:
    # - 1.2.3.4:8080
    # - http://1.2.3.4:8080
    # - user:pass@1.2.3.4:8080
    # - http://user:pass@1.2.3.4:8080
    if "://" not in s:
        s = "http://" + s
    return s


@dataclass
class _ProxySlot:
    proxy: str
    sem: asyncio.Semaphore
    active: int = 0
    fail_count: int = 0
    cooldown_until: float = 0.0


class ProxyPool:
    def __init__(self, proxy_file: str = PROXY_FILE) -> None:
        self.proxy_file = proxy_file
        self._slots: list[_ProxySlot] = []
        self._lock = threading.Lock()
        self._rr = 0  # round-robin pointer

    def load(self) -> None:
        proxies: list[str] = []
        try:
            with open(self.proxy_file, "r", encoding="utf-8") as f:
                for line in f:
                    p = _normalize_proxy(line)
                    if p:
                        proxies.append(p)
        except FileNotFoundError:
            proxies = []

        # Dedup while keeping order
        seen = set()
        uniq: list[str] = []
        for p in proxies:
            if p not in seen:
                seen.add(p)
                uniq.append(p)

        self._slots = [
            _ProxySlot(proxy=p, sem=asyncio.Semaphore(MAX_CONCURRENCY_PER_PROXY))
            for p in uniq
        ]
        self._rr = 0

    def has_proxies(self) -> bool:
        return bool(self._slots)

    def any(self) -> Optional[str]:
        """Pick a proxy for the next request.

        NOTE: we don't "reserve" it here. Concurrency limiting happens inside SharedClient.
        """
        if not self._slots:
            return None

        now = time.time()
        with self._lock:
            # Prefer non-cooled proxies, pick the least active; tie-break by RR so we don't pin one proxy forever.
            candidates = [s for s in self._slots if s.cooldown_until <= now]
            if not candidates:
                # all in cooldown => fall back to everyone
                candidates = list(self._slots)

            # least-active
            min_active = min(s.active for s in candidates)
            least = [s for s in candidates if s.active == min_active]

            # round-robin among least-active
            if self._rr >= len(least):
                self._rr = 0
            slot = least[self._rr]
            self._rr = (self._rr + 1) % max(1, len(least))
            return slot.proxy

    async def _acquire(self, proxy: str) -> Optional[_ProxySlot]:
        slot = next((s for s in self._slots if s.proxy == proxy), None)
        if not slot:
            return None
        await slot.sem.acquire()
        with self._lock:
            slot.active += 1
        return slot

    async def _release(self, slot: _ProxySlot) -> None:
        with self._lock:
            slot.active = max(0, slot.active - 1)
        slot.sem.release()

    async def mark_failure(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            slot = next((s for s in self._slots if s.proxy == proxy), None)
            if not slot:
                return
            slot.fail_count += 1
            # exponential-ish backoff
            cooldown = min(FAIL_COOLDOWN_SECONDS * slot.fail_count, 10 * FAIL_COOLDOWN_SECONDS)
            slot.cooldown_until = time.time() + cooldown

    async def mark_success(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            slot = next((s for s in self._slots if s.proxy == proxy), None)
            if not slot:
                return
            slot.fail_count = 0
            slot.cooldown_until = 0.0


PROXY_POOL = ProxyPool()


# -------- Shared clients --------

_clients: Dict[str, httpx.AsyncClient] = {}
_clients_lock = asyncio.Lock()


def _default_headers() -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
    }


class SharedClient:
    """A thin wrapper over httpx.AsyncClient that:
    - limits concurrency per proxy via ProxyPool semaphores
    - records proxy failures/successes for cooldown
    """

    def __init__(self, client: httpx.AsyncClient, proxy: Optional[str]) -> None:
        self._client = client
        self._proxy = proxy

    def __getattr__(self, item):
        return getattr(self._client, item)

    async def request(self, method: str, url: str, **kwargs):
        slot = None
        try:
            if self._proxy:
                slot = await PROXY_POOL._acquire(self._proxy)
            resp = await self._client.request(method, url, **kwargs)
            # If it's a "proxy got blocked" vibe, treat as failure too.
            if resp.status_code in (407, 429, 502, 503, 504):
                await PROXY_POOL.mark_failure(self._proxy)
            else:
                await PROXY_POOL.mark_success(self._proxy)
            return resp
        except Exception:
            await PROXY_POOL.mark_failure(self._proxy)
            raise
        finally:
            if slot:
                await PROXY_POOL._release(slot)

    async def get(self, url: str, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs):
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs):
        return await self.request("DELETE", url, **kwargs)


async def get_client(proxy: Optional[str] = None) -> SharedClient:
    """Return a cached SharedClient for a given proxy (or direct)."""
    key = proxy or "__direct__"
    async with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = httpx.AsyncClient(
                timeout=_DEFAULT_TIMEOUT,
                limits=_LIMITS,
                headers=_default_headers(),
                follow_redirects=True,
                proxies=proxy,
            )
            _clients[key] = client
    return SharedClient(client, proxy)


async def close_clients() -> None:
    async with _clients_lock:
        items = list(_clients.items())
        _clients.clear()
    for _, c in items:
        try:
            await c.aclose()
        except Exception:
            pass


# Load proxies on import (safe if file missing)
PROXY_POOL.load()
