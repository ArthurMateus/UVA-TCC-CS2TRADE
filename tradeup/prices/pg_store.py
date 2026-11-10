"""Postgres/Supabase persistence (optional).

The local SQLite cache stays the working store (fast, offline, used by the
tests). This module mirrors it into the Supabase schema in
`supabase/schema.sql`, so the history of prices is kept server-side and
other machines / the deployed web app can read the same data:

    tradeup db-push --currency USD     # catalogue + latest quotes -> Supabase
    TRADEUP_PRICE_SOURCE=supabase tradeup serve   # read prices from Supabase

Connection: DATABASE_URL (Supabase -> Project Settings -> Database ->
connection string; use the pooler URI). It authenticates as the `postgres`
role, which bypasses RLS: keep it on the server only, never in the browser.

Requires `psycopg[binary]` (pip install -e ".[supabase]").
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

from .steam import CURRENCY_CODE, PriceBook, Quote

SOURCES = ("steam_priceoverview", "steam_search", "skinport", "csfloat", "manual")


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError as e:              # pragma: no cover - optional dependency
        raise RuntimeError("psycopg is not installed: pip install 'psycopg[binary]'") from e
    return psycopg.connect(dsn, autocommit=False, prepare_threshold=None)


def quote_source(q: Quote) -> str:
    """Bulk rows have listings but no median; priceoverview rows the reverse."""
    return "steam_search" if (q.median is None and q.listings is not None) \
        else "steam_priceoverview"


class PgPriceStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set (see .env.example)")
        self.dsn = dsn
        self.conn = _connect(dsn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # -- catalogue ------------------------------------------------------
    def push_catalogue(self, skins: Iterable, images: Optional[dict] = None) -> int:
        skins = list(skins)
        images = images or {}
        with self.conn.cursor() as cur:
            cur.executemany(
                "insert into public.collections(name) values (%s) "
                "on conflict (name) do nothing",
                [(c,) for c in sorted({s.collection for s in skins})])
            cur.execute("select id, name from public.collections")
            cid = {n: i for i, n in cur.fetchall()}
            cur.executemany(
                """insert into public.skins(id,name,collection_id,rarity,min_float,max_float,
                       stattrak_capable,souvenir_capable,image_url,updated_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   on conflict (id) do update set
                     name=excluded.name, collection_id=excluded.collection_id,
                     rarity=excluded.rarity, min_float=excluded.min_float,
                     max_float=excluded.max_float,
                     stattrak_capable=excluded.stattrak_capable,
                     souvenir_capable=excluded.souvenir_capable,
                     image_url=coalesce(excluded.image_url, public.skins.image_url),
                     updated_at=now()""",
                [(s.id, s.name, cid[s.collection], int(s.rarity), s.min_float,
                  s.max_float, s.stattrak_capable, s.souvenir_capable,
                  images.get(s.name)) for s in skins])
        self.conn.commit()
        return len(skins)

    # -- prices ---------------------------------------------------------
    def put_many(self, quotes: Iterable[Quote], currency: int, batch: int = 500) -> int:
        rows = [(q.market_hash_name, currency, quote_source(q), q.lowest, q.median,
                 q.volume, q.listings, q.fetched_at) for q in quotes
                if q.lowest is not None or q.median is not None]
        with self.conn.cursor() as cur:
            for i in range(0, len(rows), batch):
                cur.executemany(
                    """insert into public.price_quotes(market_hash_name,currency,source,
                           lowest,median,volume_24h,sell_listings,fetched_at)
                       values (%s,%s,%s::public.price_source,%s,%s,%s,%s,to_timestamp(%s))""",
                    rows[i:i + batch])
        self.conn.commit()
        return len(rows)

    def latest_book(self, currency: int) -> PriceBook:
        """PriceBook built from the `price_best` view (one row per item)."""
        with self.conn.cursor() as cur:
            cur.execute("""select market_hash_name, lowest, median, volume_24h,
                                  sell_listings, extract(epoch from fetched_at)
                           from public.price_best where currency=%s""", (currency,))
            data = {n: {"lowest": float(lo) if lo is not None else None,
                        "median": float(me) if me is not None else None,
                        "volume": v, "listings": li, "ts": float(ts or time.time())}
                    for n, lo, me, v, li, ts in cur.fetchall()}
        return PriceBook(data, meta={"currency": currency,
                                     "currency_code": CURRENCY_CODE.get(currency),
                                     "source": "supabase"})
