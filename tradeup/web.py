"""Trade-up browser: risk-first web UI.

    uvicorn tradeup.web:app --reload      ->  http://localhost:8000

Nothing here re-implements maths. It calls `risk.find_guaranteed()` and
`optimizer.search()` exactly as the CLI does, so screen and terminal agree.

Two modes, exposed as a top-level toggle because they answer different
questions:

  GUARANTEED  only contracts where EVERY outcome beats the cost.
              Closed-form, ~3 ms, no combinatorics.
  RANKED      the general search, filtered and sorted on P(win) first.

Scans are cached per parameter set so moving the budget slider does not
re-run an expensive search on every keystroke.
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
from .prices.steam import PriceBook, PriceCache
from .pricing import PriceService, PricingPolicy
from .risk import explain_guaranteed, find_guaranteed

app = FastAPI(title="CS2 Trade-Up Browser", version="2.0")

SNAPSHOT = Path(os.getenv("TRADEUP_PRICE_SNAPSHOT", "data/price_snapshot.json"))
CACHE_DB = Path(os.getenv("TRADEUP_CACHE", "data/prices.sqlite"))
SKINS_DB = os.getenv("TRADEUP_SKINS")

RARITIES = {"consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
            "milspec": Rarity.MILSPEC, "restricted": Rarity.RESTRICTED,
            "classified": Rarity.CLASSIFIED, "covert": Rarity.COVERT}
CUR_ID = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}
SYMBOL = {"USD": "$", "BRL": "R$", "EUR": "\u20ac", "GBP": "\u00a3",
          "RUB": "\u20bd", "CNY": "\u00a5"}

_db: Optional[SkinDB] = None
_img: Optional[ImageIndex] = None
_cache: dict[str, tuple[float, list]] = {}
TTL = 600.0


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


def svc(policy: PricingPolicy, currency: str) -> PriceService:
    if not SNAPSHOT.exists():
        raise HTTPException(
            503, f"No price snapshot at {SNAPSHOT}. Run: tradeup warm --rarity milspec")
    return PriceService(PriceBook.load(SNAPSHOT), policy)


def depth_for(names: set[str], currency: str) -> dict:
    if not CACHE_DB.exists() or not names:
        return {}
    try:
        return PriceCache(CACHE_DB).depth(names, CUR_ID.get(currency.upper(), 1))
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
    k = hashlib.md5(json.dumps(key, sort_keys=True).encode()).hexdigest()
    hit = _cache.get(k)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    rows = build()
    _cache[k] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------------------
@app.get("/api/meta")
def meta():
    d = db()
    return {
        "skins": len(d),
        "images": len(img()),
        "snapshot": str(SNAPSHOT),
        "snapshot_exists": SNAPSHOT.exists(),
        "cache_exists": CACHE_DB.exists(),
        "warnings": d.quality_sanity(),
        "tiers": [{"key": k, "label": v.label,
                   "collections": len(d.tradeable_collections(v))}
                  for k, v in RARITIES.items() if v.next_tier()],
    }


@app.get("/api/guaranteed")
def api_guaranteed(
    budget: float = Query(200.0, gt=0),
    rarity: str = "milspec",
    currency: str = "BRL",
    souvenir: bool = True,
    float_percentile: float = 0.15,
    haircut: float = 0.03,
    min_profit: float = 0.0,
    min_input_volume: int = 5,
    top: int = 20,
):
    """Contracts where the WORST outcome still beats the cost."""
    if rarity not in RARITIES:
        raise HTTPException(400, f"bad rarity: {rarity}")

    def build():
        prices = svc(PricingPolicy(min_volume=1,
                                   min_input_volume=min_input_volume,
                                   haircut=haircut), currency)
        found = find_guaranteed(
            db(), prices, RARITIES[rarity], budget=budget,
            float_percentile=float_percentile, souvenir=souvenir,
            haircut=haircut, min_profit=min_profit)
        return decorate([explain_guaranteed(r) for r in found], currency)

    rows = cached({"m": "g", "b": budget, "r": rarity, "c": currency,
                   "s": souvenir, "f": float_percentile, "h": haircut,
                   "p": min_profit, "v": min_input_volume}, build)
    return {"mode": "guaranteed", "count": len(rows),
            "currency": currency, "symbol": SYMBOL.get(currency.upper(), ""),
            "results": rows[:top]}


@app.get("/api/scan")
def api_scan(
    budget: float = Query(200.0, gt=0),
    rarity: str = "milspec",
    currency: str = "BRL",
    fee_rate: Optional[float] = None,
    souvenir: bool = True,
    float_percentile: float = 0.15,
    min_volume: int = 3,
    min_input_volume: int = 5,
    haircut: float = 0.03,
    odds: Literal["per_input", "outcome_weighted"] = "per_input",
    min_win_chance: float = 50.0,
    min_roi: float = 105.0,
    min_profit: float = 0.0,
    max_cost: Optional[float] = None,
    sort: Literal["win", "profit", "roi", "safety", "cost", "ev"] = "win",
    top: int = 20,
):
    """General search. Defaults filter and sort on P(win), not EV."""
    if rarity not in RARITIES:
        raise HTTPException(400, f"bad rarity: {rarity}")

    def build():
        prices = svc(PricingPolicy(min_volume=min_volume,
                                   min_input_volume=min_input_volume,
                                   haircut=haircut), currency)
        cands = search(db(), prices, RARITIES[rarity], budget=budget,
                       odds_model=odds, fee_rate=fee_rate, souvenir=souvenir,
                       float_percentile=float_percentile,
                       min_roi=1.0, min_win_chance=0.0, top_n=600)
        return decorate([explain(c) for c in cands], currency)

    rows = cached({"m": "s", "b": budget, "r": rarity, "c": currency,
                   "fe": fee_rate, "s": souvenir, "f": float_percentile,
                   "mv": min_volume, "mi": min_input_volume, "h": haircut,
                   "o": odds}, build)

    out = [r for r in rows
           if r["win_chance_pct"] >= min_win_chance
           and r["roi_pct"] >= min_roi
           and r["profit"] >= min_profit
           and (max_cost is None or r["cost"] <= max_cost)]

    keys = {"win": ("win_chance_pct", "profit"), "profit": ("profit", "win_chance_pct"),
            "roi": ("roi_pct", "win_chance_pct"), "ev": ("ev_net", "win_chance_pct"),
            "safety": ("guaranteed_profit", "win_chance_pct"),
            "cost": ("cost", "win_chance_pct")}
    a, b = keys[sort]
    out.sort(key=lambda r: (r.get(a, 0), r.get(b, 0)), reverse=(sort != "cost"))

    return {"mode": "ranked", "count": len(out), "scanned": len(rows),
            "currency": currency, "symbol": SYMBOL.get(currency.upper(), ""),
            "results": out[:top]}


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8")
