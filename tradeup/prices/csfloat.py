"""Float-aware listing prices from the CSFloat marketplace API.

WHY THIS EXISTS

Steam prices are per WEAR ("AK-47 | X (Field-Tested)"), but a Field-Tested
skin at 0.151 and one at 0.379 are not the same purchase: the trade-up
output float is driven by the input floats, so low-float inputs are what
make a contract land on a better wear -- and they cost more. Pricing the
input at the cheapest listing of the wear while assuming a low float is
optimistic. Steam's own listing endpoint no longer returns machine-readable
float data, so the float dimension has to come from a marketplace that
exposes it per listing.

CSFloat's `GET /api/v1/listings` returns buy-now listings with each item's
`float_value` and accepts `market_hash_name`, `min_float`, `max_float`,
`category` and `sort_by=lowest_price` filters, so "the N cheapest copies
with float <= f" is ONE request. An API key (CSFLOAT_API_KEY, from the
user's own CSFloat account settings) is required.

Prices are USD cents. Results are cached in SQLite (`float_listings`) with
a short TTL; the float bounds are bucketed to 3 decimals, rounding the cap
DOWN, which is conservative (never assumes a looser float than allowed)
and lets nearby queries share a cache entry.

NOTE: parameter and field names follow CSFloat's public API as used by
community clients (docs.csfloat.com). They are isolated in `_params()` and
`_parse()` so a schema change is a two-line fix.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Union

import httpx

from .fetcher import parse_retry_after

API_BASE = "https://csfloat.com/api/v1"
USER_AGENT = "cs2-tradeup-lab/2.1 (academic research; low-rate)"
PAGE_LIMIT = 50                       # CSFloat's max page size


@dataclass(frozen=True, slots=True)
class FloatListing:
    price: float          # USD
    float_value: float
    listing_id: str = ""


class FloatListingSource(Protocol):
    def cheapest(self, market_hash_name: str, *, min_float: float,
                 max_float: float, n: int) -> list[FloatListing]: ...


def category_for(market_hash_name: str) -> int:
    """CSFloat category: 1 normal, 2 StatTrak, 3 Souvenir."""
    if market_hash_name.startswith("StatTrak"):
        return 2
    if market_hash_name.startswith("Souvenir "):
        return 3
    return 1


def bucket_down(f: float, step: float = 0.001) -> float:
    return math.floor(f / step + 1e-9) * step


def bucket_up(f: float, step: float = 0.001) -> float:
    return math.ceil(f / step - 1e-9) * step


class FloatListingCache:
    def __init__(self, path: Union[str, Path]):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS float_listings(
            name TEXT, min_f REAL, max_f REAL, listings TEXT, ts REAL,
            PRIMARY KEY(name, min_f, max_f))""")
        self.db.commit()
        self._lock = threading.Lock()

    def get(self, name, lo, hi, ttl) -> Optional[list[FloatListing]]:
        r = self.db.execute("SELECT listings, ts FROM float_listings WHERE name=? "
                            "AND min_f=? AND max_f=?", (name, lo, hi)).fetchone()
        if not r or time.time() - r[1] > ttl:
            return None
        return [FloatListing(*x) for x in json.loads(r[0])]

    def put(self, name, lo, hi, items: list[FloatListing]) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO float_listings VALUES (?,?,?,?,?)",
                            (name, lo, hi, json.dumps([[i.price, i.float_value, i.listing_id]
                                                       for i in items]), time.time()))
            self.db.commit()

    def close(self) -> None:
        self.db.close()


class CSFloatError(RuntimeError):
    pass


class CSFloatSource:
    """Cheapest buy-now listings under a float cap, with caching and pacing."""

    def __init__(self, api_key: str, cache: Optional[FloatListingCache] = None,
                 ttl_minutes: float = 120.0, min_interval: float = 1.5,
                 timeout: float = 20.0, max_attempts: int = 3,
                 client: Optional[httpx.Client] = None):
        if not api_key:
            raise CSFloatError("CSFLOAT_API_KEY is not set (create one in your "
                               "CSFloat account and put it in .env)")
        self.cache = cache
        self.ttl = ttl_minutes * 60
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self._last = 0.0
        self.requests = 0
        self.rate_limited = 0
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": api_key, "User-Agent": USER_AGENT,
                     "Accept": "application/json"})

    def close(self) -> None:
        self.client.close()

    # -- request/response mapping (the only API-specific bits) -----------
    @staticmethod
    def _params(name: str, lo: float, hi: float) -> dict:
        return {"market_hash_name": name, "min_float": f"{lo:.3f}",
                "max_float": f"{hi:.3f}", "sort_by": "lowest_price",
                "type": "buy_now", "category": category_for(name),
                "limit": PAGE_LIMIT}

    @staticmethod
    def _parse(payload) -> list[FloatListing]:
        rows = payload.get("data") if isinstance(payload, dict) else payload
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            item = row.get("item") or {}
            try:
                cents = int(row["price"])
                fv = float(item["float_value"])
            except (KeyError, TypeError, ValueError):
                continue
            if cents > 0 and 0.0 <= fv <= 1.0:
                out.append(FloatListing(cents / 100.0, fv, str(row.get("id", ""))))
        out.sort(key=lambda x: (x.price, x.float_value))
        return out

    # -- transport --------------------------------------------------------
    def _pace(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.2))
        self._last = time.monotonic()

    def _fetch(self, name: str, lo: float, hi: float) -> list[FloatListing]:
        backoff = 5.0
        for _ in range(self.max_attempts):
            self._pace()
            try:
                r = self.client.get(f"{API_BASE}/listings", params=self._params(name, lo, hi))
            except httpx.HTTPError:
                time.sleep(backoff); backoff *= 2
                continue
            self.requests += 1
            if r.status_code == 429:
                self.rate_limited += 1
                time.sleep(max(parse_retry_after(r.headers.get("Retry-After")), backoff))
                backoff *= 2
                continue
            if r.status_code in (401, 403):
                raise CSFloatError(f"CSFloat rejected the API key (HTTP {r.status_code})")
            if r.status_code != 200:
                time.sleep(backoff); backoff *= 2
                continue
            try:
                return self._parse(r.json())
            except ValueError:
                time.sleep(backoff); backoff *= 2
        raise CSFloatError(f"CSFloat request failed for {name!r}")

    def cheapest(self, market_hash_name: str, *, min_float: float = 0.0,
                 max_float: float = 1.0, n: int = 10) -> list[FloatListing]:
        lo, hi = round(bucket_up(min_float), 3), round(bucket_down(max_float), 3)
        if hi <= lo:
            return []
        items = self.cache.get(market_hash_name, lo, hi, self.ttl) if self.cache else None
        if items is None:
            items = self._fetch(market_hash_name, lo, hi)
            if self.cache:
                self.cache.put(market_hash_name, lo, hi, items)
        return items[:max(0, n)]


class StaticFloatSource:
    """In-memory source for tests and offline experiments."""

    def __init__(self, listings: dict[str, list[tuple[float, float]]]):
        self.listings = {k: sorted((FloatListing(p, f) for p, f in v),
                                   key=lambda x: (x.price, x.float_value))
                         for k, v in listings.items()}
        self.calls = 0

    def cheapest(self, market_hash_name: str, *, min_float: float = 0.0,
                 max_float: float = 1.0, n: int = 10) -> list[FloatListing]:
        self.calls += 1
        return [x for x in self.listings.get(market_hash_name, [])
                if min_float <= x.float_value <= max_float][:n]
