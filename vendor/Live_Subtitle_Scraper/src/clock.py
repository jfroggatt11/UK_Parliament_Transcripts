"""Reference clock using Akamai UTC endpoint for drift-free timestamps."""

import logging
import time
from datetime import datetime, timezone

import httpx

from src.config import AKAMAI_CACHE_TTL_S, AKAMAI_TIME_URL, AKAMAI_TIMEOUT_S

log = logging.getLogger(__name__)

_cached_time: float = 0.0
_cached_at: float = 0.0


async def get_akamai_time(client: httpx.AsyncClient) -> float:
    """Fetch current UTC time from Akamai, returning a Unix timestamp.

    Results are cached for AKAMAI_CACHE_TTL_S to avoid excessive requests
    when multiple calls happen within the same polling cycle.
    Falls back to local system time with a warning if Akamai is unreachable.
    """
    global _cached_time, _cached_at

    now_mono = time.monotonic()
    elapsed = now_mono - _cached_at
    if _cached_time and elapsed < AKAMAI_CACHE_TTL_S:
        return _cached_time + elapsed

    try:
        resp = await client.get(AKAMAI_TIME_URL, timeout=AKAMAI_TIMEOUT_S)
        resp.raise_for_status()
        iso_str = resp.text.strip()
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        unix_ts = dt.timestamp()
        _cached_time = unix_ts
        _cached_at = now_mono
        return unix_ts
    except Exception as exc:
        log.warning("Akamai clock unavailable (%s), falling back to system time", exc)
        return time.time()
