"""Steam Community Market price client (single-item endpoint).

`/market/priceoverview/` returns ONE item per request and is aggressively
rate limited **per IP** -- a 429 ban commonly lasts hours and re-arms every
time you hit it while banned. Use `bulk.py` to fill an empty cache; keep
this for precise confirmation of a short shortlist, where `median` and 24h
`volume` actually matter.

OBSERVABILITY: every blocking wait publishes its reason to `self.state`, so
a caller can render a live heartbeat. Without it, a long backoff sleep and
a genuine hang look identical from the outside.

ENCODING: every JSON read/write pins `encoding="utf-8"`. Market hash names
contain characters cp1252 cannot represent; on Windows the default codec
raises UnicodeEncodeError. Never drop the explicit encoding.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import httpx

APPID_CS2 = 730

# Steam currency ids
CURRENCY = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}

PRICE_RE = re.compile(r"[\d]+(?:[.,]\d+)*(?:[.,]\d{2})?")


def parse_price(raw: Optional[str]) -> Optional[float]:
    """Parse Steam's localized price strings ('R$ 1.234,56', '$12.34')."""
    if not raw:
        return None
    m = PRICE_RE.search(raw.replace("\xa0", " "))
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class ClientState:
    """What the client is doing right now, for live progress rendering."""

    phase: str = "idle"          # idle | throttling | requesting | backoff
    item: str = ""
    detail: str = ""
    since: float = field(default_factory=time.monotonic)

    def set(self, phase: str, detail: str = "") -> None:
        self.phase, self.detail, self.since = phase, detail, time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.since


class TokenBucket:
    """Thread-safe token bucket: `rate` requests per `per` seconds."""

    def __init__(self, rate: int = 18, per: float = 60.0):
        self.rate, self.per = rate, per
        self._tokens = float(rate)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, state: Optional[ClientState] = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.rate, self._tokens + (now - self._last) * self.rate / self.per)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * self.per / self.rate
            if state:
                state.set("throttling", f"pacing {wait:.1f}s")
            time.sleep(wait + random.uniform(0.05, 0.35))


@dataclass(slots=True)
class Quote:
    market_hash_name: str
    lowest: Optional[float]
    median: Optional[float]
    volume: Optional[int]
    fetched_at: float

    @property
    def best(self) -> Optional[float]:
        """Conservative reference price.

        For an OUTPUT you plan to sell, `median` is the honest number: the
        lowest listing is often a single outlier you cannot reproduce.
        For an INPUT you plan to buy, `lowest` is what you actually pay.
        """
        return self.median or self.lowest


class PriceCache:
    def __init__(self, path: Union[str, Path]):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS prices(
                 name TEXT, currency INT, lowest REAL, median REAL,
                 volume INT, ts REAL, PRIMARY KEY(name, currency))""")
        # remembers names Steam has no listing for, so they are not re-fetched
        # on every run -- these are a large share of a full sweep
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS misses(
                 name TEXT, currency INT, ts REAL, PRIMARY KEY(name, currency))""")
        self.db.commit()
        self._lock = threading.Lock()

    def get(self, name: str, currency: int, ttl: float) -> Optional[Quote]:
        cur = self.db.execute(
            "SELECT lowest, median, volume, ts FROM prices WHERE name=? AND currency=?",
            (name, currency)).fetchone()
        if not cur:
            return None
        lo, med, vol, ts = cur
        if time.time() - ts > ttl:
            return None
        return Quote(name, lo, med, vol, ts)

    def is_known_miss(self, name: str, currency: int, ttl: float) -> bool:
        row = self.db.execute(
            "SELECT ts FROM misses WHERE name=? AND currency=?",
            (name, currency)).fetchone()
        return bool(row) and (time.time() - row[0] <= ttl)

    def put(self, q: Quote, currency: int) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?)",
                (q.market_hash_name, currency, q.lowest, q.median, q.volume,
                 q.fetched_at))
            self.db.commit()

    def put_miss(self, name: str, currency: int) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO misses VALUES (?,?,?)",
                            (name, currency, time.time()))
            self.db.commit()

    def export(self, path: Union[str, Path], currency: int) -> int:
        rows = self.db.execute(
            "SELECT name, lowest, median, volume FROM prices WHERE currency=?",
            (currency,)).fetchall()
        payload = {n: {"lowest": lo, "median": me, "volume": v}
                   for n, lo, me, v in rows}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False),
                              encoding="utf-8")
        return len(payload)


class SteamMarket:
    """Polite, cached Steam Community Market client (one item per request)."""

    URL = "https://steamcommunity.com/market/priceoverview/"

    def __init__(
        self,
        currency: str = "USD",
        cache_path: Union[str, Path] = Path("data/prices.sqlite"),
        ttl_hours: float = 8.0,
        rate: int = 18,
        per: float = 60.0,
        max_retries: int = 5,
        timeout: float = 20.0,
        max_backoff: float = 120.0,
        miss_ttl_hours: float = 168.0,
    ):
        self.currency = CURRENCY.get(currency.upper(), 1)
        self.cache = PriceCache(cache_path)
        self.ttl = ttl_hours * 3600
        self.miss_ttl = miss_ttl_hours * 3600
        self.bucket = TokenBucket(rate, per)
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self.state = ClientState()
        self.client = httpx.Client(
            # a connect timeout is essential: without it a black-holed TCP
            # connection blocks far longer than the read timeout implies
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": "cs2-tradeup-lab/1.0 (personal research tool)"},
        )
        self.stats = {"hits": 0, "misses": 0, "429s": 0, "errors": 0,
                      "timeouts": 0, "empty": 0, "gave_up": 0}

    def close(self) -> None:
        self.client.close()

    def get(self, market_hash_name: str) -> Optional[Quote]:
        self.state.item = market_hash_name
        cached = self.cache.get(market_hash_name, self.currency, self.ttl)
        if cached:
            self.stats["hits"] += 1
            self.state.set("idle")
            return cached
        if self.cache.is_known_miss(market_hash_name, self.currency, self.miss_ttl):
            self.stats["empty"] += 1
            self.state.set("idle")
            return None
        self.stats["misses"] += 1

        backoff = min(5.0, self.max_backoff)
        for attempt in range(self.max_retries):
            self.bucket.take(self.state)
            self.state.set("requesting", f"attempt {attempt + 1}")
            try:
                r = self.client.get(self.URL, params={
                    "appid": APPID_CS2,
                    "currency": self.currency,
                    "market_hash_name": market_hash_name,
                })
            except httpx.TimeoutException:
                self.stats["timeouts"] += 1
                self.state.set("backoff", f"timeout, waiting {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            except httpx.HTTPError as e:
                self.stats["errors"] += 1
                self.state.set("backoff", f"{type(e).__name__}, waiting {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if r.status_code == 429:
                self.stats["429s"] += 1
                ra = float(r.headers.get("Retry-After", 0) or 0)
                wait = min(max(ra, backoff), self.max_backoff)
                self.state.set("backoff", f"429, waiting {wait:.0f}s")
                time.sleep(wait + random.uniform(0, 2))
                backoff = min(backoff * 2, self.max_backoff)
                continue
            if r.status_code == 500:
                # Steam returns 500 for names that do not exist on the market
                self.cache.put_miss(market_hash_name, self.currency)
                self.state.set("idle")
                return None
            if r.status_code != 200:
                self.stats["errors"] += 1
                self.state.set("backoff", f"HTTP {r.status_code}, waiting {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            try:
                data = r.json()
            except ValueError:
                # Steam serves an HTML maintenance/error page with a 200
                self.stats["errors"] += 1
                self.state.set("backoff", f"non-JSON body, waiting {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if not data.get("success"):
                self.cache.put_miss(market_hash_name, self.currency)
                self.state.set("idle")
                return None
            q = Quote(
                market_hash_name,
                parse_price(data.get("lowest_price")),
                parse_price(data.get("median_price")),
                int(str(data.get("volume", "0")).replace(",", "") or 0),
                time.time(),
            )
            self.cache.put(q, self.currency)
            self.state.set("idle")
            return q

        # Out of retries: record it so a resumed run does not stall here again
        self.stats["gave_up"] += 1
        self.state.set("idle")
        return None


class PriceBook:
    """Offline price source: a plain {market_hash_name: {...}} JSON snapshot."""

    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, path: Union[str, Path]) -> "PriceBook":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, market_hash_name: str) -> Optional[Quote]:
        d = self.data.get(market_hash_name)
        if d is None:
            return None
        if isinstance(d, (int, float)):
            return Quote(market_hash_name, float(d), float(d), None, time.time())
        return Quote(market_hash_name, d.get("lowest"), d.get("median"),
                     d.get("volume"), time.time())
