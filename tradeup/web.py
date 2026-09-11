"""Trade-up browser: the same engine, served as a web UI.

    uvicorn tradeup.web:app --reload
    -> http://localhost:8000

Point it at real prices first, or it serves the fabricated sample data:

    $env:TRADEUP_PRICE_SNAPSHOT="data/price_snapshot.json"

Nothing here re-implements the maths. It calls `optimizer.search()` and
`liquidity.assess()` exactly as the CLI does, so the numbers on screen are
the numbers the CLI prints.

Results are cached per filter-set, because a scan over 86 collections takes
a few seconds and the budget slider would otherwise re-run it constantly.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .data.loader import SkinDB
from .images import ImageIndex
from .models import Rarity
from .optimizer import explain, search
from .prices.steam import PriceBook, SteamMarket
from .pricing import PriceService, PricingPolicy

try:
    from .liquidity import LiquidityPolicy, assess, breakeven_realization
    HAVE_LIQUIDITY = True
except ImportError:      # liquidity.py not installed yet
    HAVE_LIQUIDITY = False

app = FastAPI(title="CS2 Trade-Up Browser", version="2.0")

SNAPSHOT = Path(os.getenv("TRADEUP_PRICE_SNAPSHOT", "data/sample_prices.json"))
CACHE_PATH = Path(os.getenv("TRADEUP_CACHE", "data/prices.sqlite"))

RARITIES = {
    "consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
    "milspec": Rarity.MILSPEC, "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED, "covert": Rarity.COVERT,
}

CURRENCY_SYMBOL = {"USD": "$", "BRL": "R$", "EUR": "\u20ac", "GBP": "\u00a3",
                   "RUB": "\u20bd", "CNY": "\u00a5"}

_db: Optional[SkinDB] = None
_images: Optional[ImageIndex] = None
_scan_cache: dict[str, tuple[float, list]] = {}
SCAN_TTL = 600.0


def db() -> SkinDB:
    global _db
    if _db is None:
        _db = SkinDB.load()
    return _db


def images() -> ImageIndex:
    global _images
    if _images is None:
        _images = ImageIndex.load()
    return _images


def price_service(policy: PricingPolicy, currency: str) -> PriceService:
    if SNAPSHOT.exists():
        return PriceService(PriceBook.load(SNAPSHOT), policy)
    return PriceService(SteamMarket(currency=currency), policy)


# ---------------------------------------------------------------------------
def depth_map(names: set[str], currency: int) -> dict[str, tuple]:
    """market_hash_name -> (sell_listings, 24h_volume) straight from cache.

    With bulk data the `volume` column holds SELL LISTINGS (supply). Once
    warm-prices has run, `median` is set and the same column is real 24h
    sales. The two are distinguished here so the liquidity model can weight
    them correctly.
    """
    import sqlite3

    out: dict[str, tuple] = {}
    if not CACHE_PATH.exists() or not names:
        return out
    conn = sqlite3.connect(str(CACHE_PATH))
    try:
        qs = list(names)
        for i in range(0, len(qs), 400):
            chunk = qs[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for nm, vol, med in conn.execute(
                    f"SELECT name, volume, median FROM prices "
                    f"WHERE currency=? AND name IN ({marks})",
                    [currency, *chunk]):
                out[nm] = (None, vol) if med is not None else (vol, None)
    except Exception:
        pass
    finally:
        conn.close()
    return out


CUR_ID = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}


def run_scan(p: dict[str, Any]) -> list[dict]:
    key = hashlib.md5(json.dumps(p, sort_keys=True).encode()).hexdigest()
    hit = _scan_cache.get(key)
    if hit and time.time() - hit[0] < SCAN_TTL:
        return hit[1]

    d = db()
    r = RARITIES[p["rarity"]]
    policy = PricingPolicy(min_volume=p["min_volume"],
                           min_input_volume=p["min_input_volume"],
                           haircut=p["haircut"])
    prices = price_service(policy, p["currency"])

    cands = search(d, prices, r,
                   budget=p["budget"],
                   odds_model=p["odds"],
                   fee_rate=p["fee_rate"],
                   souvenir=p["souvenir"],
                   float_percentile=p["float_percentile"],
                   min_roi=0.0,               # filter later, in the UI
                   top_n=400)

    rows = [explain(c) for c in cands]
    img = images()
    cur = CUR_ID.get(p["currency"].upper(), 1)

    # attach float caps, images and liquidity
    want: set[str] = set()
    for row in rows:
        for o in row["outcomes"]:
            want.add(f"{o['skin']} ({o['wear']})")
    depth = depth_map(want, cur)
    pol = LiquidityPolicy() if HAVE_LIQUIDITY else None

    for row in rows:
        for o in row["outcomes"]:
            s = d.by_name(o["skin"])
            o["min_float"] = round(s.min_float, 4) if s else None
            o["max_float"] = round(s.max_float, 4) if s else None
            o["image"] = img.get(o["skin"])
            nm = f"{o['skin']} ({o['wear']})"
            listings, vol = depth.get(nm, (None, None))
            o["listings"] = listings
            o["volume24"] = vol
            if pol:
                o["liquidity"] = pol.tier(listings, vol).label
        for i in row["inputs"]:
            s = d.by_name(i["skin"])
            i["min_float"] = round(s.min_float, 4) if s else None
            i["max_float"] = round(s.max_float, 4) if s else None
            i["image"] = img.get(i["skin"])

        if HAVE_LIQUIDITY:
            class _O:  # tiny shim so assess() can read the dicts
                __slots__ = ("skin", "probability", "wear", "net_price")

                def __init__(self, o):
                    self.skin = type("S", (), {
                        "market_hash_name": staticmethod(
                            lambda w, st=False, sv=False, n=o["skin"]: f"{n} ({w})")})
                    self.probability = o["p_pct"] / 100.0
                    self.wear = o["wear"]
                    self.net_price = o["net"]

            shim = [_O(o) for o in row["outcomes"]]
            rep = assess(shim, pol, depth, row["cost"])
            row["ev_adjusted"] = round(rep.ev_adjusted, 2)
            row["profit_adjusted"] = round(rep.profit_adjusted, 2)
            row["roi_adjusted_pct"] = round(rep.roi_adjusted * 100, 2)
            row["top_ev_share_pct"] = round(rep.top_ev_share * 100, 1)
            row["top_outcome"] = rep.top_outcome
            row["concentrated"] = rep.concentrated
            row["worst_liquidity"] = rep.worst_tier.label
            row["warnings"] = rep.warnings()
            be = breakeven_realization(shim, row["cost"], rep.top_outcome)
            row["breakeven_pct"] = round(be * 100, 1) if be else None
        else:
            row["ev_adjusted"] = row["ev_net"]
            row["profit_adjusted"] = row["profit"]
            row["roi_adjusted_pct"] = row["roi_pct"]
            row["warnings"] = []
            row["concentrated"] = False

    _scan_cache[key] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------------------
@app.get("/api/meta")
def meta():
    d = db()
    return {
        "skins": len(d),
        "images": len(images()),
        "snapshot": str(SNAPSHOT),
        "snapshot_exists": SNAPSHOT.exists(),
        "liquidity": HAVE_LIQUIDITY,
        "tiers": [
            {"key": k, "label": v.label,
             "collections": len(d.tradeable_collections(v)),
             "outputs": len(d.tradeable_collections(v.next_tier()))
                        if v.next_tier() else 0}
            for k, v in RARITIES.items() if v.next_tier()
        ],
    }


@app.get("/api/scan")
def api_scan(
    budget: float = Query(200.0, gt=0),
    rarity: str = "milspec",
    currency: str = "BRL",
    fee_rate: Optional[float] = Query(None, description="blank = exact Steam model"),
    souvenir: bool = True,
    float_percentile: float = 0.15,
    min_volume: int = 3,
    min_input_volume: int = 10,
    haircut: float = 0.03,
    odds: Literal["per_input", "outcome_weighted"] = "per_input",
    # UI-side filters
    min_roi: float = 100.0,
    min_profit: float = 0.0,
    max_cost: Optional[float] = None,
    liquidity: Literal["any", "ok", "deep"] = "any",
    hide_concentrated: bool = False,
    sort: Literal["profit", "roi", "ev", "cost", "safety"] = "profit",
    adjusted: bool = True,
    top: int = 20,
):
    if rarity not in RARITIES:
        raise HTTPException(400, f"bad rarity: {rarity}")

    rows = run_scan({
        "budget": budget, "rarity": rarity, "currency": currency,
        "fee_rate": fee_rate, "souvenir": souvenir,
        "float_percentile": float_percentile, "min_volume": min_volume,
        "min_input_volume": min_input_volume, "haircut": haircut, "odds": odds,
    })

    pk = "profit_adjusted" if adjusted else "profit"
    rk = "roi_adjusted_pct" if adjusted else "roi_pct"
    ek = "ev_adjusted" if adjusted else "ev_net"

    out = []
    rank = {"dead": 0, "thin": 1, "ok": 2, "deep": 3, "unknown": 0}
    for r in rows:
        if r[rk] < min_roi or r[pk] < min_profit:
            continue
        if max_cost is not None and r["cost"] > max_cost:
            continue
        if hide_concentrated and r.get("concentrated"):
            continue
        if liquidity != "any":
            need = rank["ok"] if liquidity == "ok" else rank["deep"]
            if rank.get(r.get("worst_liquidity", "unknown"), 0) < need:
                continue
        out.append(r)

    keys = {"profit": pk, "roi": rk, "ev": ek, "cost": "cost",
            "safety": "profit_chance_pct"}
    rev = sort != "cost"
    out.sort(key=lambda r: r.get(keys[sort], 0), reverse=rev)

    return {
        "count": len(out),
        "scanned": len(rows),
        "currency": currency,
        "symbol": CURRENCY_SYMBOL.get(currency.upper(), ""),
        "adjusted": adjusted and HAVE_LIQUIDITY,
        "liquidity_available": HAVE_LIQUIDITY,
        "results": out[:top],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8")
