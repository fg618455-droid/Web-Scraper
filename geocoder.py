"""Geocoding via Nominatim (OpenStreetMap) + Haversine distance.

Used by the PLZ-Umkreis-Filter (Iter. 24). Free + no API key, but
Nominatim's usage policy requires <= 1 request/second and a real
User-Agent string identifying the app. We respect both.

Results are cached in the SQLite `geocache` table — see database.py.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time

import requests

import database as db

logger = logging.getLogger(__name__)

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
# Nominatim policy: identify your app with a real contact (email or repo URL).
USER_AGENT = (
    'DealTracker/1.0 (Personal use; '
    'https://github.com/fg618455-droid/Web-Scraper)'
)
REQUEST_TIMEOUT = 10  # seconds
MIN_REQUEST_INTERVAL = 1.1  # seconds — keep safely above 1/s policy

_rate_lock = threading.Lock()
_last_request_ts: float = 0.0


def _normalize(query: str) -> str:
    """Strip whitespace + collapse repeats. Cache key uses the raw user input
    too, so '80331 München' and '80331  München' don't double-cache."""
    return re.sub(r'\s+', ' ', query.strip())


def _looks_like_german_plz(q: str) -> bool:
    """5 digits anywhere in the string."""
    return bool(re.search(r'\b\d{5}\b', q))


def _nominatim_request(query: str) -> tuple[float, float] | None:
    """One direct API call. Returns (lat, lon) or None if not found / error.
    Enforces the 1 req/sec rate limit globally via a module-level lock."""
    global _last_request_ts

    with _rate_lock:
        elapsed = time.time() - _last_request_ts
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        _last_request_ts = time.time()

    params = {
        'q': query,
        'format': 'jsonv2',
        'limit': 1,
        'addressdetails': 0,
    }
    # Bias towards Germany when the query has a PLZ — improves accuracy for
    # short queries like "80331" that match many countries.
    if _looks_like_german_plz(query):
        params['countrycodes'] = 'de'

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params=params,
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning('Nominatim HTTP %s for %r', resp.status_code, query)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning('Nominatim request failed for %r: %s', query, exc)
        return None

    if not data:
        return None
    try:
        return float(data[0]['lat']), float(data[0]['lon'])
    except (KeyError, ValueError, TypeError):
        return None


def geocode(query: str) -> tuple[float, float] | None:
    """Resolve a location string to (lat, lon). Hits the cache first; on miss,
    calls Nominatim (rate-limited) and caches the result. Returns None for
    unresolvable inputs — and remembers that as a 'notfound' cache entry so
    we don't keep retrying every minute."""
    if not query:
        return None
    q = _normalize(query)
    if not q:
        return None

    cached = db.geocache_lookup(q)
    if cached is not None:
        lat, lon, status = cached
        if status == 'ok' and lat is not None and lon is not None:
            return (lat, lon)
        return None  # 'notfound' (or unexpected status)

    coords = _nominatim_request(q)
    if coords:
        db.geocache_store(q, coords[0], coords[1], status='ok')
        return coords
    db.geocache_store(q, None, None, status='notfound')
    return None


# ── Haversine ───────────────────────────────────────────────────────────────

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in kilometres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_KM * c
