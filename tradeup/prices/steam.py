"""Price cache and quote model. Encoding is pinned UTF-8 everywhere:
skin names contain characters cp1252 cannot encode (Windows default)."""
from __future__ import annotations
import json, re, sqlite3, threading, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

from ..fsutil import atomic_write_text

CURRENCY = {"USD":1,"GBP":2,"EUR":3,"BRL":7,"RUB":5,"CNY":23}
CURRENCY_CODE = {v: k for k, v in CURRENCY.items()}
SYMBOL = {"USD": "$", "BRL": "R$", "EUR": "€", "GBP": "£",
          "RUB": "₽", "CNY": "¥"}
# a run of digits that may contain thousands/decimal separators or spaces
PRICE_RE = re.compile(r"\d[\d\s.,  ]*")
META_KEY = "__meta__"


def parse_price(raw: Optional[str]) -> Optional[float]:
    """Parse Steam's localized strings.

    Handles 'R$ 1.234,56', '$12.34', '1 234,56 pуб.', '€1.234', '¥ 1,234.56'.
    Rule: the LAST separator is decimal only if 1-2 digits follow it; a
    separator followed by exactly 3 digits (or repeated) is a thousands mark.
    """
    if not raw:
        return None
    m = PRICE_RE.search(raw)
    if not m:
        return None
    s = re.sub(r"[\s  ]", "", m.group(0)).rstrip(".,")
    if not s:
        return None
    last = max(s.rfind(","), s.rfind("."))
    if last == -1:
        num = s
    else:
        tail = s[last + 1:]
        sep = s[last]
        if len(tail) in (1, 2) and s.count(sep) == 1:
            num = re.sub(r"[.,]", "", s[:last]) + "." + tail
        else:                               # every separator is a thousands mark
            num = re.sub(r"[.,]", "", s)
    try:
        return float(num)
    except ValueError:
        return None


@dataclass(slots=True)
class Quote:
    market_hash_name: str
    lowest: Optional[float]
    median: Optional[float]
    volume: Optional[int]           # units SOLD in 24h (priceoverview)
    fetched_at: float
    listings: Optional[int] = None  # units LISTED now (search/render)

    @property
    def best(self) -> Optional[float]:
        return self.median or self.lowest

    @property
    def liquidity(self) -> Optional[int]:
        """Sales volume when known, else the weaker listing-count proxy."""
        return self.volume if self.volume is not None else self.listings


@dataclass
class ClientState:
    """What a long-running client is doing right now (for progress UIs)."""
    status: str = "idle"
    detail: str = ""
    since: float = field(default_factory=time.monotonic)
    history: list = field(default_factory=list)

    def set(self, status: str, detail: str = "") -> None:
        self.status, self.detail, self.since = status, detail, time.monotonic()
        self.history.append((self.since, status, detail))
        del self.history[:-50]


class PriceCache:
    """Local SQLite store. WAL + batched commits: one fsync per batch, not
    per quote, so writes never stall the async fetch loop."""

    def __init__(self, path: Union[str, Path], batch_size: int = 200):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        # timeout/busy_timeout: wait for another process's write instead of
        # failing with "database is locked" (e.g. collector + web UI at once)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS prices(name TEXT,currency INT,
            lowest REAL,median REAL,volume INT,ts REAL,PRIMARY KEY(name,currency))""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(prices)")}
        if "listings" not in cols:
            self.db.execute("ALTER TABLE prices ADD COLUMN listings INT")
        self.db.execute("""CREATE TABLE IF NOT EXISTS misses(name TEXT,currency INT,
            ts REAL,PRIMARY KEY(name,currency))""")
        self.db.commit()
        self._lock = threading.Lock()
        self._pending = 0
        self.batch_size = batch_size

    # -- lifecycle --------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            self.db.commit(); self._pending = 0

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.db.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    def _wrote(self, n: int = 1) -> None:
        self._pending += n
        if self._pending >= self.batch_size:
            self.db.commit(); self._pending = 0

    # -- reads ------------------------------------------------------------
    def get(self, name, currency, ttl) -> Optional[Quote]:
        c = self.db.execute("SELECT lowest,median,volume,ts,listings FROM prices "
                            "WHERE name=? AND currency=?", (name, currency)).fetchone()
        if not c: return None
        lo, me, v, ts, li = c
        return None if time.time()-ts > ttl else Quote(name, lo, me, v, ts, li)

    def is_known_miss(self, name, currency, ttl) -> bool:
        r = self.db.execute("SELECT ts FROM misses WHERE name=? AND currency=?",
                            (name, currency)).fetchone()
        return bool(r) and (time.time()-r[0] <= ttl)

    # -- writes -----------------------------------------------------------
    def put(self, q: Quote, currency: int) -> None:
        self.put_many([q], currency)

    def put_many(self, quotes: Iterable[Quote], currency: int) -> None:
        rows = [(q.market_hash_name, currency, q.lowest, q.median, q.volume,
                 q.fetched_at, q.listings) for q in quotes]
        if not rows: return
        with self._lock:
            # keep whichever fields the new quote does not provide (bulk rows
            # carry listings but no median/volume, and vice versa)
            self.db.executemany(
                """INSERT INTO prices(name,currency,lowest,median,volume,ts,listings)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name,currency) DO UPDATE SET
                     lowest=excluded.lowest,
                     median=COALESCE(excluded.median, prices.median),
                     volume=COALESCE(excluded.volume, prices.volume),
                     listings=COALESCE(excluded.listings, prices.listings),
                     ts=excluded.ts""", rows)
            self.db.executemany("DELETE FROM misses WHERE name=? AND currency=?",
                                [(r[0], currency) for r in rows])
            self._wrote(len(rows))

    def put_miss(self, name, currency) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO misses VALUES (?,?,?)",
                            (name, currency, time.time()))
            self._wrote()

    # -- export -----------------------------------------------------------
    def export(self, path, currency) -> int:
        self.flush()
        rows = self.db.execute("SELECT name,lowest,median,volume,listings,ts FROM prices "
                               "WHERE currency=?", (currency,)).fetchall()
        p: dict = {n: {"lowest": lo, "median": me, "volume": v, "listings": li, "ts": ts}
                   for n, lo, me, v, li, ts in rows}
        p[META_KEY] = {"currency": currency,
                       "currency_code": CURRENCY_CODE.get(currency),
                       "exported_at": time.time(), "count": len(rows)}
        atomic_write_text(path, json.dumps(p, ensure_ascii=False))
        return len(rows)

    def depth(self, names, currency) -> dict:
        """name -> (sell_listings, volume24)."""
        out = {}
        ns = list(names)
        for i in range(0, len(ns), 400):
            ch = ns[i:i+400]
            q = ",".join("?"*len(ch))
            for nm, li, vol in self.db.execute(
                f"SELECT name,listings,volume FROM prices WHERE currency=? "
                f"AND name IN ({q})", [currency, *ch]):
                out[nm] = (li, vol)
        return out


class PriceBook:
    """Offline source: {market_hash_name: {lowest, median, volume, listings}}.

    An optional "__meta__" entry records the currency the snapshot was taken
    in, so callers can refuse to show USD prices with an R$ symbol.
    """
    def __init__(self, data: dict, meta: Optional[dict] = None):
        self.meta = dict(meta or data.get(META_KEY) or {})
        self.data = {k: v for k, v in data.items() if k != META_KEY}
        self.loaded_at = time.time()

    @property
    def currency(self) -> Optional[int]:
        c = self.meta.get("currency")
        return int(c) if c is not None else None

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, name) -> Optional[Quote]:
        d = self.data.get(name)
        if d is None: return None
        if isinstance(d, (int, float)):
            return Quote(name, float(d), float(d), None, self.loaded_at)
        return Quote(name, d.get("lowest"), d.get("median"), d.get("volume"),
                     d.get("ts") or self.loaded_at, d.get("listings"))

    def __len__(self) -> int:
        return len(self.data)
