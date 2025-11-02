# speed_profiles.py
# Drop-in: call apply() *before* loading config/env consumers.
import os

PROFILES = {
    # conservative on CPU/RAM
    "gentle": {
        "HTTP_CONNECT_TIMEOUT": "3.5",
        "HTTP_READ_TIMEOUT": "12.0",
        "HTTP_WRITE_TIMEOUT": "8.0",
        "HTTP_POOL_TIMEOUT": "3.0",
        "HTTP_MAX_CONNECTIONS": "200",
        "HTTP_MAX_KEEPALIVE": "120",
        "HTTP_KEEPALIVE_SECS": "30.0",
        "THUMB_BATCH_CONCURRENCY": "12",
        "THUMB_DL_CONCURRENCY": "24",
        "RENDER_CONCURRENCY": "6",
        "RENDER_THREADS": "4",
    },
    # balanced defaults
    "fast": {
        "HTTP_CONNECT_TIMEOUT": "2.5",
        "HTTP_READ_TIMEOUT": "10.0",
        "HTTP_WRITE_TIMEOUT": "7.0",
        "HTTP_POOL_TIMEOUT": "2.0",
        "HTTP_MAX_CONNECTIONS": "400",
        "HTTP_MAX_KEEPALIVE": "200",
        "HTTP_KEEPALIVE_SECS": "25.0",
        "THUMB_BATCH_CONCURRENCY": "24",
        "THUMB_DL_CONCURRENCY": "40",
        "RENDER_CONCURRENCY": "12",
        "RENDER_THREADS": "8",
    },
    # aggressive for strong servers
    "extreme": {
        "HTTP_CONNECT_TIMEOUT": "2.0",
        "HTTP_READ_TIMEOUT": "8.0",
        "HTTP_WRITE_TIMEOUT": "6.0",
        "HTTP_POOL_TIMEOUT": "2.0",
        "HTTP_MAX_CONNECTIONS": "600",
        "HTTP_MAX_KEEPALIVE": "300",
        "HTTP_KEEPALIVE_SECS": "20.0",
        "THUMB_BATCH_CONCURRENCY": "32",
        "THUMB_DL_CONCURRENCY": "64",
        "RENDER_CONCURRENCY": "16",
        "RENDER_THREADS": "8",
    },
}

def apply() -> str:
    name = (os.getenv("PROFILE_SPEED") or "").strip().lower()
    if not name:
        return ""
    cfg = PROFILES.get(name)
    if not cfg:
        return ""
    # override env for downstream importers
    for k, v in cfg.items():
        os.environ[k] = str(v)
    return name