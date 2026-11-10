"""Trade-up browser: risk-first web UI.

    uvicorn tradeup.web:app --reload      ->  http://localhost:8000

Nothing here re-implements maths. It calls `risk.find_guaranteed()` and
`optimizer.search()` exactly as the CLI does, so screen and terminal agree.

  GUARANTEED  only contracts where EVERY outcome beats the cost (net of fees).
  RANKED      the general search, filtered and sorted on P(win) first.

PAGINATION AND CACHING

A scan is computed once per parameter set and kept in a bounded LRU cache
(TTL 10 min). Filters, sorting and pages are applied to the cached rows, so
"Load more" (`offset`) and moving sliders do not recompute anything. A
per-key lock makes concurrent identical requests compute once (single
flight). Responses carry `total`, `limit`, `offset` and `next_offset`.

FLOAT CHECK

`float_check=true` re-prices the contracts ON THE RETURNED PAGE with
float-aware listings (CSFloat; needs CSFLOAT_API_KEY): see
`tradeup.float_pricing`. Off by default -- it costs API calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import config
from .data.loader import SkinDB
from .images import ImageIndex
from .models import Rarity
from .optimizer import explain, search
from .prices.steam import CURRENCY, SYMBOL, PriceBook, PriceCache
from .pricing import PriceService, PricingPolicy
from .risk import explain_guaranteed, find_guaranteed

app = FastAPI(title="CS2 Trade-Up Browser", version="2.1")

# read at import (tests reload this module after setting the env vars)
SNAPSHOT = config._path("TRADEUP_PRICE_SNAPSHOT", config.DATA_DIR / "price_snapshot.json")
CACHE_DB = config._path("TRADEUP_CACHE", config.DATA_DIR / "prices.sqlite")
SKINS_DB = os.getenv("TRADEUP_SKINS")

RARITIES = {k: v for k, v in config.RARITIES.items() if k != "mil-spec"}
MAX_LIMIT = 100
TTL = 600.0
CACHE_ENTRIES = 32
FLOAT_CHECK_MAX = 20

_db: Optional[SkinDB] = None
_img: Optional[ImageIndex] = None
_book: Optional[tuple[float, PriceBook]] = None         # (mtime, book)
_depth: Optional[PriceCache] = None
_cache: "OrderedDict[str, tuple[float, list]]" = OrderedDict()
_cache_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_state_lock = threading.Lock()
_index_html: Optional[str] = None


def db() -> SkinDB:
    global _db
    if _db is None:
        _db = SkinDB.load(SKINS_DB)
    return _db


def img() -> ImageIndex:
    global _img
    if _img is None:
        _img = ImageIndex.load()
    return _img


PRICE_SOURCE = os.getenv("TRADEUP_PRICE_SOURCE", "snapshot").lower()
SUPABASE_REFRESH = 300.0


def price_book() -> PriceBook:
    """Snapshot parsed once, reloaded only when the file changes.

    With TRADEUP_PRICE_SOURCE=supabase, prices come from the `price_best`
    view instead (currency TRADEUP_CURRENCY, default USD), refreshed every
    5 minutes."""
    global _book
    if PRICE_SOURCE == "supabase":
        with _state_lock:
            if _book is None or time.time() - _book[0] > SUPABASE_REFRESH:
                from .prices.pg_store import PgPriceStore
                cur = CURRENCY.get(os.getenv("TRADEUP_CURRENCY", "USD").upper(), 1)
                try:
                    with PgPriceStore(config.DATABASE_URL or "") as st:
                        _book = (time.time(), st.latest_book(cur))
                except Exception as e:
                    raise HTTPException(503, f"Supabase indisponível: {e}")
                with _cache_lock:
                    _cache.clear()
            return _book[1]
    if not SNAPSHOT.exists():
        raise HTTPException(
            503, f"Nenhum snapshot de preços em {SNAPSHOT}. Rode: tradeup bulk-warm --rarity milspec")
    mtime = SNAPSHOT.stat().st_mtime
    with _state_lock:
        if _book is None or _book[0] != mtime:
            _book = (mtime, PriceBook.load(SNAPSHOT))
            with _cache_lock:
                _cache.clear()                     # results depend on prices
        return _book[1]


def check_currency(currency: str) -> tuple[str, bool]:
    """Validate the code and that the snapshot is really in that currency."""
    code = currency.upper()
    if code not in CURRENCY:
        raise HTTPException(400, f"moeda desconhecida; use uma destas: {', '.join(CURRENCY)}")
    book_cur = price_book().currency
    if book_cur is not None and book_cur != CURRENCY[code]:
        have = next((k for k, v in CURRENCY.items() if v == book_cur), str(book_cur))
        raise HTTPException(409, f"os preços estão em {have}, não em {code}: rode de novo "
                                 f"`tradeup bulk-warm --currency {code}` ou peça {have}")
    return code, book_cur is not None


def svc(policy: PricingPolicy) -> PriceService:
    return PriceService(price_book(), policy)


def depth_for(names: set[str], currency: str) -> dict:
    global _depth
    if not CACHE_DB.exists() or not names:
        return {}
    try:
        with _state_lock:
            if _depth is None:
                _depth = PriceCache(CACHE_DB)
        return _depth.depth(names, CURRENCY.get(currency.upper(), 1))
    except Exception:
        return {}


def decorate(rows: list[dict], currency: str) -> list[dict]:
    """Attach float caps, images and market depth to every row."""
    d, images = db(), img()
    want: set[str] = set()
    for r in rows:
        for o in r["outcomes"]:
            want.add(f"{o['skin']} ({o['wear']})")
    depth = depth_for(want, currency)

    for r in rows:
        for o in r["outcomes"]:
            s = d.by_name(o["skin"])
            if s is not None:
                o.setdefault("min_float", round(s.min_float, 4))
                o.setdefault("max_float", round(s.max_float, 4))
            o["image"] = images.get(o["skin"])
            listings, vol = depth.get(f"{o['skin']} ({o['wear']})", (None, None))
            o["listings"], o["volume24"] = listings, vol
        for i in r["inputs"]:
            s = d.by_name(i["skin"])
            if s is not None:
                i.setdefault("min_float", round(s.min_float, 4))
                i.setdefault("max_float", round(s.max_float, 4))
            i["image"] = images.get(i["skin"])
    return rows


def cached(key: dict, build) -> list[dict]:
    """Bounded LRU with TTL and single-flight per key."""
    k = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()
    with _cache_lock:
        hit = _cache.get(k)
        if hit and time.time() - hit[0] < TTL:
            _cache.move_to_end(k)
            return hit[1]
        lock = _key_locks.setdefault(k, threading.Lock())
    with lock:                                   # identical requests wait here
        with _cache_lock:
            hit = _cache.get(k)
            if hit and time.time() - hit[0] < TTL:
                return hit[1]
        rows = build()
        with _cache_lock:
            _cache[k] = (time.time(), rows)
            _cache.move_to_end(k)
            while len(_cache) > CACHE_ENTRIES:
                old, _ = _cache.popitem(last=False)
                _key_locks.pop(old, None)
        return rows


def page(rows: list[dict], limit: int, offset: int) -> dict:
    total = len(rows)
    nxt = offset + limit if offset + limit < total else None
    return {"total": total, "count": total, "limit": limit, "offset": offset,
            "next_offset": nxt, "results": rows[offset:offset + limit]}


def finite(x: float, name: str) -> float:
    if not math.isfinite(x):
        raise HTTPException(422, f"{name} precisa ser um número finito")
    return x


def rarity_of(raw: str) -> Rarity:
    if raw not in RARITIES:
        raise HTTPException(400, f"raridade inválida; use uma destas: {', '.join(RARITIES)}")
    return RARITIES[raw]


def _snapshot_time(book) -> Optional[float]:
    """When the prices were exported (epoch seconds), for the UI."""
    if book is None:
        return None
    t = book.meta.get("exported_at")
    if t:
        return float(t)
    try:
        return SNAPSHOT.stat().st_mtime if PRICE_SOURCE != "supabase" else None
    except OSError:
        return None


def maybe_float_check(rows: list[dict], enabled: bool, top: int, policy: PricingPolicy,
                      fee_rate: Optional[float], currency: str) -> Optional[str]:
    """Attach `float_check` to the given rows; returns a note for the response."""
    if not enabled:
        return None
    if not config.CSFLOAT_API_KEY:
        return "checagem de float ignorada: CSFLOAT_API_KEY não está configurada"
    from .float_pricing import refine_rows
    from .prices.csfloat import CSFloatSource, FloatListingCache
    cache = FloatListingCache(CACHE_DB)
    src = CSFloatSource(config.CSFLOAT_API_KEY, cache=cache)
    try:
        refine_rows(rows, db(), src, svc(policy), top=top, fee_rate=fee_rate,
                    basis="premium")
    finally:
        src.close(); cache.close()
    return f"checagem de float: base premium, {src.requests} requisições ao CSFloat"


# ---------------------------------------------------------------------------
@app.get("/api/meta")
def meta():
    d = db()
    has = PRICE_SOURCE == "supabase" or SNAPSHOT.exists()
    try:
        book = price_book() if has else None
    except HTTPException:
        book, has = None, False
    return {
        "skins": len(d),
        "images": len(img()),
        "price_source": PRICE_SOURCE,
        "snapshot": str(SNAPSHOT),
        "snapshot_exists": has,
        "snapshot_currency": book.currency if book else None,
        "snapshot_quotes": len(book) if book else 0,
        "cache_exists": CACHE_DB.exists(),
        "float_check_available": bool(config.CSFLOAT_API_KEY),
        "warnings": d.quality_sanity(),
        "snapshot_updated_at": _snapshot_time(book),
        "price_basis": "wear",
        "tiers": [t for t in ({"key": k, "label": v.label,
                               "collections": len(d.tradeable_collections(v))}
                              for k, v in RARITIES.items() if v.next_tier())
                  if t["collections"] > 0],
    }


@app.get("/api/guaranteed")
def api_guaranteed(
    budget: float = Query(200.0, gt=0, le=1e7),
    rarity: str = "milspec",
    currency: str = Query(config.DEFAULT_CURRENCY, max_length=3),
    souvenir: bool = True,
    float_percentile: float = Query(0.15, ge=0.0, le=1.0),
    haircut: float = Query(0.03, ge=0.0, lt=1.0),
    fee_rate: Optional[float] = Query(None, ge=0.0, lt=1.0),
    min_profit: float = Query(0.0, ge=0.0),
    min_input_volume: int = Query(5, ge=0),
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    top: Optional[int] = Query(None, ge=1, le=MAX_LIMIT, description="sinônimo de limit"),
    float_check: bool = False,
    float_check_top: int = Query(5, ge=1, le=FLOAT_CHECK_MAX),
):
    """Contracts where the WORST outcome still beats the cost, net of fees."""
    r = rarity_of(rarity)
    finite(budget, "budget")
    code, verified = check_currency(currency)
    limit = top or limit
    # outputs need >= 3 listings, as in ranked mode: a "guaranteed" floor
    # resting on a single listing is not a guarantee
    policy = PricingPolicy(min_volume=3, min_input_volume=min_input_volume, haircut=haircut)

    # find_guaranteed picks the CHEAPEST qualifying input per collection, so the
    # result for budget B is the unbounded result filtered by cost <= B: the
    # budget is NOT part of the cache key and the slider never recomputes.
    def build():
        found = find_guaranteed(
            db(), svc(policy), r, budget=float("inf"),
            float_percentile=float_percentile, souvenir=souvenir,
            haircut=haircut, fee_rate=fee_rate, min_profit=min_profit)
        return decorate([explain_guaranteed(x) for x in found], code)

    rows = cached({"m": "g", "r": rarity, "c": code, "s": souvenir,
                   "f": float_percentile, "h": haircut, "fe": fee_rate,
                   "p": min_profit, "v": min_input_volume}, build)
    out = [x for x in rows if x["cost"] <= budget]
    resp = {"mode": "guaranteed", "currency": code, "currency_verified": verified,
            "symbol": SYMBOL.get(code, ""), **page(out, limit, offset)}
    resp["results"] = [json.loads(json.dumps(x)) for x in resp["results"]]  # no cache mutation
    note = maybe_float_check(resp["results"], float_check, float_check_top, policy,
                             fee_rate, code)
    if note:
        resp["note"] = note
    return resp


@app.get("/api/scan")
def api_scan(
    budget: float = Query(200.0, gt=0, le=1e7),
    rarity: str = "milspec",
    currency: str = Query(config.DEFAULT_CURRENCY, max_length=3),
    fee_rate: Optional[float] = Query(None, ge=0.0, lt=1.0),
    souvenir: bool = True,
    float_percentile: float = Query(0.15, ge=0.0, le=1.0),
    min_volume: int = Query(3, ge=0),
    min_input_volume: int = Query(5, ge=0),
    haircut: float = Query(0.03, ge=0.0, lt=1.0),
    odds: Literal["per_input", "outcome_weighted"] = "per_input",
    min_win_chance: float = Query(50.0, ge=0.0, le=100.0),
    min_roi: float = Query(105.0, ge=0.0),
    min_profit: float = 0.0,
    max_cost: Optional[float] = Query(None, gt=0),
    sort: Literal["win", "profit", "roi", "safety", "cost", "ev"] = "win",
    limit: int = Query(20, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    top: Optional[int] = Query(None, ge=1, le=MAX_LIMIT, description="sinônimo de limit"),
    float_check: bool = False,
    float_check_top: int = Query(5, ge=1, le=FLOAT_CHECK_MAX),
):
    """General search. Defaults filter and sort on P(win), not EV."""
    r = rarity_of(rarity)
    finite(budget, "budget")
    code, verified = check_currency(currency)
    limit = top or limit
    policy = PricingPolicy(min_volume=min_volume, min_input_volume=min_input_volume,
                           haircut=haircut)
    finite(min_profit, "min_profit")
    if max_cost is not None:
        finite(max_cost, "max_cost")

    # The candidate set does not depend on the budget (see optimizer._per_wear),
    # so it is computed ONCE per parameter set and the budget is a filter:
    # moving the budget slider never recomputes and never hides contracts
    # that fit a smaller budget.
    def build():
        cands = search(db(), svc(policy), r, budget=float("inf"),
                       odds_model=odds, fee_rate=fee_rate, souvenir=souvenir,
                       float_percentile=float_percentile,
                       min_roi=1.0, min_win_chance=0.0, top_n=10**6)
        return [explain(c) for c in cands]

    rows = cached({"m": "s", "r": rarity, "c": code,
                   "fe": fee_rate, "s": souvenir, "f": float_percentile,
                   "mv": min_volume, "mi": min_input_volume, "h": haircut,
                   "o": odds}, build)

    fits = [x for x in rows if x["cost"] <= budget]
    out = [x for x in fits
           if x["win_chance_pct"] >= min_win_chance
           and x["roi_pct"] >= min_roi
           and x["profit"] >= min_profit
           and (max_cost is None or x["cost"] <= max_cost)]

    keys = {"win": ("win_chance_pct", "profit"), "profit": ("profit", "win_chance_pct"),
            "roi": ("roi_pct", "win_chance_pct"), "ev": ("ev_net", "win_chance_pct"),
            "safety": ("guaranteed_profit", "win_chance_pct"),
            "cost": ("cost", "win_chance_pct")}
    a, b = keys[sort]
    out.sort(key=lambda x: (x.get(a, 0), x.get(b, 0)), reverse=(sort != "cost"))

    resp = {"mode": "ranked", "scanned": len(fits), "truncated": False,
            "currency": code, "currency_verified": verified,
            "symbol": SYMBOL.get(code, ""), **page(out, limit, offset)}
    resp["results"] = decorate([json.loads(json.dumps(x)) for x in resp["results"]], code)
    note = maybe_float_check(resp["results"], float_check, float_check_top, policy,
                             fee_rate, code)
    if note:
        resp["note"] = note
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    global _index_html
    if _index_html is None:
        _index_html = (Path(__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8")
    return _index_html
