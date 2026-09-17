"""Price cache and quote model. Encoding is pinned UTF-8 everywhere:
skin names contain characters cp1252 cannot encode (Windows default)."""
from __future__ import annotations
import json, re, sqlite3, threading, time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

CURRENCY = {"USD":1,"GBP":2,"EUR":3,"BRL":7,"RUB":5,"CNY":23}
PRICE_RE = re.compile(r"[\d]+(?:[.,]\d+)*(?:[.,]\d{2})?")

def parse_price(raw: Optional[str]) -> Optional[float]:
    """Parse Steam's localized strings ('R$ 1.234,56', '$12.34')."""
    if not raw: return None
    m = PRICE_RE.search(raw.replace("\xa0"," "))
    if not m: return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".","").replace(",",".") if s.rfind(",")>s.rfind(".") else s.replace(",","")
    elif "," in s:
        s = s.replace(",",".") if len(s.split(",")[-1])==2 else s.replace(",","")
    try: return float(s)
    except ValueError: return None

@dataclass(slots=True)
class Quote:
    market_hash_name: str
    lowest: Optional[float]
    median: Optional[float]
    volume: Optional[int]
    fetched_at: float
    @property
    def best(self) -> Optional[float]:
        return self.median or self.lowest

class PriceCache:
    def __init__(self, path: Union[str, Path]):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS prices(name TEXT,currency INT,
            lowest REAL,median REAL,volume INT,ts REAL,PRIMARY KEY(name,currency))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS misses(name TEXT,currency INT,
            ts REAL,PRIMARY KEY(name,currency))""")
        self.db.commit(); self._lock = threading.Lock()
    def get(self, name, currency, ttl):
        c = self.db.execute("SELECT lowest,median,volume,ts FROM prices "
                            "WHERE name=? AND currency=?", (name,currency)).fetchone()
        if not c: return None
        lo, me, v, ts = c
        return None if time.time()-ts > ttl else Quote(name, lo, me, v, ts)
    def is_known_miss(self, name, currency, ttl) -> bool:
        r = self.db.execute("SELECT ts FROM misses WHERE name=? AND currency=?",
                            (name,currency)).fetchone()
        return bool(r) and (time.time()-r[0] <= ttl)
    def put(self, q: Quote, currency: int):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?)",
                (q.market_hash_name,currency,q.lowest,q.median,q.volume,q.fetched_at))
            self.db.commit()
    def put_miss(self, name, currency):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO misses VALUES (?,?,?)",
                            (name,currency,time.time()))
            self.db.commit()
    def export(self, path, currency) -> int:
        rows = self.db.execute("SELECT name,lowest,median,volume FROM prices "
                               "WHERE currency=?", (currency,)).fetchall()
        p = {n:{"lowest":lo,"median":me,"volume":v} for n,lo,me,v in rows}
        Path(path).write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        return len(p)
    def depth(self, names, currency) -> dict:
        """name -> (sell_listings, volume24). Bulk data has no median, and
        stores listings in the volume column; warm data has both."""
        out = {}
        ns = list(names)
        for i in range(0, len(ns), 400):
            ch = ns[i:i+400]
            q = ",".join("?"*len(ch))
            for nm, vol, med in self.db.execute(
                f"SELECT name,volume,median FROM prices WHERE currency=? "
                f"AND name IN ({q})", [currency,*ch]):
                out[nm] = (None, vol) if med is not None else (vol, None)
        return out

class PriceBook:
    """Offline source: {market_hash_name: {lowest, median, volume}}."""
    def __init__(self, data: dict): self.data = data
    @classmethod
    def load(cls, path): return cls(json.loads(Path(path).read_text(encoding="utf-8")))
    def get(self, name):
        d = self.data.get(name)
        if d is None: return None
        if isinstance(d,(int,float)): return Quote(name,float(d),float(d),None,time.time())
        return Quote(name, d.get("lowest"), d.get("median"), d.get("volume"), time.time())
