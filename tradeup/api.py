"""FastAPI service + single-page UI.

    uvicorn tradeup.api:app --reload

Point it at real prices first, otherwise it serves the fabricated sample data:

    $env:TRADEUP_PRICE_SNAPSHOT="data/price_snapshot.json"   # PowerShell
    export TRADEUP_PRICE_SNAPSHOT=data/price_snapshot.json   # bash

Endpoints
    GET  /api/health
    GET  /api/collections?rarity=consumer
    POST /api/evaluate     -> score one explicit contract
    POST /api/scan         -> budget-constrained recipe search
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .data.loader import SkinDB
from .engine import evaluate_contract, validate_contract
from .models import InputItem, Rarity
from .optimizer import explain, robust_filter, search
from .prices.steam import PriceBook, SteamMarket
from .pricing import PriceService, PricingPolicy

app = FastAPI(title="CS2 Trade-Up Lab", version="1.2")

RARITIES = {
    "consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
    "milspec": Rarity.MILSPEC, "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED, "covert": Rarity.COVERT,
}

SNAPSHOT = Path(os.getenv("TRADEUP_PRICE_SNAPSHOT", "data/sample_prices.json"))


@lru_cache(maxsize=1)
def get_db() -> SkinDB:
    return SkinDB.load()


@lru_cache(maxsize=8)
def get_prices(offline: bool, currency: str, min_volume: int,
               min_input_volume: int, haircut: float) -> PriceService:
    policy = PricingPolicy(min_volume=min_volume,
                           min_input_volume=min_input_volume, haircut=haircut)
    if offline:
        return PriceService(PriceBook.load(SNAPSHOT), policy)
    return PriceService(SteamMarket(currency=currency), policy)


class InputSpec(BaseModel):
    name: str
    collection: Optional[str] = None
    float_value: float = Field(..., ge=0.0, le=1.0)
    price: float = 0.0
    souvenir: bool = False


class EvaluateRequest(BaseModel):
    inputs: list[InputSpec]
    odds_model: Literal["outcome_weighted", "per_input"] = "outcome_weighted"
    fee_rate: Optional[float] = None
    stattrak: bool = False
    offline: bool = True
    currency: str = "USD"


class ScanRequest(BaseModel):
    budget: float = Field(..., gt=0)
    rarity: str = "restricted"
    odds_model: Literal["outcome_weighted", "per_input"] = "outcome_weighted"
    fee_rate: Optional[float] = None
    stattrak: bool = False
    souvenir: bool = True
    float_percentile: float = 0.15
    min_roi: float = 1.05
    min_volume: int = 3
    min_input_volume: int = 10
    haircut: float = 0.03
    robust: bool = True
    top: int = 15
    offline: bool = True
    currency: str = "USD"


@app.get("/api/health")
def health():
    db = get_db()
    return {
        "ok": True,
        "skins": len(db),
        "snapshot": str(SNAPSHOT),
        "snapshot_exists": SNAPSHOT.exists(),
        "warnings": db.quality_sanity(),
        "tiers": {r.label: len(db.tradeable_collections(r))
                  for r in (Rarity.CONSUMER, Rarity.INDUSTRIAL, Rarity.MILSPEC,
                            Rarity.RESTRICTED, Rarity.CLASSIFIED)},
    }


@app.get("/api/collections")
def collections(rarity: str = "restricted"):
    db = get_db()
    r = RARITIES.get(rarity.lower())
    if r is None:
        raise HTTPException(400, f"Bad rarity: {rarity}")
    return {
        "rarity": r.label,
        "collections": [
            {
                "name": c,
                "inputs": len(db.pool(c, r)),
                "outputs": len(db.pool(c, r.next_tier())) if r.next_tier() else 0,
                "souvenir_inputs": sum(1 for s in db.pool(c, r) if s.souvenir_capable),
            }
            for c in db.tradeable_collections(r)
        ],
    }


@app.post("/api/evaluate")
def evaluate(req: EvaluateRequest):
    db = get_db()
    prices = get_prices(req.offline, req.currency, 0, 0, 0.0)
    items = []
    for spec in req.inputs:
        skin = (db.by_id(f"{spec.name}|{spec.collection}") if spec.collection
                else db.by_name(spec.name))
        if skin is None:
            raise HTTPException(404, f"Unknown skin: {spec.name}")
        items.append(InputItem(skin, spec.float_value, spec.price,
                               req.stattrak, spec.souvenir))
    if not items:
        raise HTTPException(400, "No inputs")
    res = evaluate_contract(items, db.pool, prices.price_fn(),
                            odds_model=req.odds_model, fee_rate=req.fee_rate,
                            stattrak=req.stattrak, skin_by_id=db.by_id)
    fake = type("C", (), {"result": res, "target_collection": items[0].skin.collection,
                          "filler_collection": None, "k_target": len(items)})()
    payload = explain(fake)
    payload["rule_warnings"] = validate_contract(items)
    return payload


@app.post("/api/scan")
def scan(req: ScanRequest):
    db = get_db()
    prices = get_prices(req.offline, req.currency, req.min_volume,
                        req.min_input_volume, req.haircut)
    r = RARITIES.get(req.rarity.lower())
    if r is None:
        raise HTTPException(400, f"Bad rarity: {req.rarity}")
    cands = search(db, prices, r, budget=req.budget, odds_model=req.odds_model,
                   fee_rate=req.fee_rate, stattrak=req.stattrak,
                   souvenir=req.souvenir, float_percentile=req.float_percentile,
                   min_roi=req.min_roi, top_n=req.top * 3)
    if req.robust:
        cands = robust_filter(cands, db, prices, fee_rate=req.fee_rate)
    return {"count": len(cands[:req.top]),
            "results": [explain(c) for c in cands[:req.top]]}


INDEX = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CS2 Trade-Up Lab</title>
<style>
:root{--bg:#0f1216;--card:#171c23;--line:#242c36;--fg:#e6edf3;--dim:#8b98a5;--ok:#3fb950;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:18px}
h1 span{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
main{padding:24px;max-width:1150px;margin:auto}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;background:var(--card);padding:16px;border:1px solid var(--line);border-radius:10px}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:4px}
input,select{width:100%;background:#0d1117;border:1px solid var(--line);color:var(--fg);padding:8px;border-radius:6px}
button{grid-column:1/-1;background:var(--acc);border:0;color:#06121f;font-weight:600;padding:10px;border-radius:6px;cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:20px;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--dim);font-weight:500}tbody tr{cursor:pointer}tbody tr:hover{background:#1c2530}
.pos{color:var(--ok)}.neg{color:var(--bad)}.sv{color:var(--acc)}
#detail{margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.muted{color:var(--dim);font-size:12px}code{background:#0d1117;padding:1px 5px;border-radius:4px}
</style>
<header><h1>CS2 Trade-Up Lab <span>float mapping, collection odds and fee-aware EV</span></h1></header>
<main>
<form id="f">
  <div><label>Budget (total input cost)</label><input name="budget" type="number" value="20" step="1"></div>
  <div><label>Input rarity</label><select name="rarity">
    <option selected>consumer</option><option>industrial</option><option>milspec</option>
    <option>restricted</option><option>classified</option></select></div>
  <div><label>Odds model</label><select name="odds_model">
    <option value="outcome_weighted">outcome weighted</option><option value="per_input">per input</option></select></div>
  <div><label>Sell fee (blank = Steam 15%)</label><input name="fee_rate" placeholder="e.g. 0.02"></div>
  <div><label>Souvenir inputs</label><select name="souvenir">
    <option value="true" selected>allow</option><option value="false">exclude</option></select></div>
  <div><label>Float percentile you can source</label><input name="float_percentile" type="number" step="0.05" value="0.15"></div>
  <div><label>Min ROI</label><input name="min_roi" type="number" step="0.05" value="1.05"></div>
  <div><label>Min output volume</label><input name="min_volume" type="number" value="3"></div>
  <div><label>Min input volume</label><input name="min_input_volume" type="number" value="10"></div>
  <div><label>Sell haircut</label><input name="haircut" type="number" step="0.01" value="0.03"></div>
  <button>Scan</button>
</form>
<div id="out"></div><div id="detail" hidden></div>
</main>
<script>
const f=document.getElementById('f'),out=document.getElementById('out'),det=document.getElementById('detail');
let rows=[];
f.onsubmit=async e=>{e.preventDefault();out.innerHTML='<p class="muted">Scanning...</p>';det.hidden=true;
 const d=Object.fromEntries(new FormData(f));
 const body={budget:+d.budget,rarity:d.rarity,odds_model:d.odds_model,
  fee_rate:d.fee_rate===''?null:+d.fee_rate,souvenir:d.souvenir==='true',
  float_percentile:+d.float_percentile,min_roi:+d.min_roi,min_volume:+d.min_volume,
  min_input_volume:+d.min_input_volume,haircut:+d.haircut,robust:true,top:15,offline:true};
 const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json(); rows=j.results||[];
 if(!rows.length){out.innerHTML='<p class="muted">Nothing cleared the filters. Lower Min ROI, raise the budget, or lower Min input volume.</p>';return}
 out.innerHTML='<table><thead><tr><th>#</th><th>Target collection</th><th>k</th><th>Sv</th><th>Cost</th><th>EV net</th><th>Profit</th><th>ROI</th><th>P(win)</th><th>&sigma;</th><th>n*</th></tr></thead><tbody>'+
  rows.map((x,i)=>`<tr data-i="${i}"><td>${i+1}</td><td>${x.target_collection}</td><td>${x.k_target}</td>
   <td class="sv">${x.souvenir_inputs||0}</td>
   <td>${x.cost.toFixed(2)}</td><td>${x.ev_net.toFixed(2)}</td>
   <td class="${x.profit>0?'pos':'neg'}">${x.profit>0?'+':''}${x.profit.toFixed(2)}</td>
   <td>${x.roi_pct}%</td><td>${x.profit_chance_pct}%</td><td>${x.stddev.toFixed(2)}</td>
   <td>${x.contracts_to_confidence??'-'}</td></tr>`).join('')+'</tbody></table>'+
  '<p class="muted">Sv = Souvenir inputs. Attributes are stripped; the output is always normal quality. n* = contracts needed before the edge beats the noise.</p>';
 out.querySelectorAll('tbody tr').forEach(tr=>tr.onclick=()=>show(rows[+tr.dataset.i]));
};
function show(x){det.hidden=false;
 det.innerHTML=`<h3 style="margin-top:0">Recipe detail</h3>
 <p class="muted">Filler: ${x.filler_collection??'none'} &middot; avg normalized float <code>${x.avg_normalized_float}</code> &middot; unpriced outcome mass ${x.unpriced_mass_pct}%</p>
 <table><thead><tr><th>Buy</th><th>Quality</th><th>Collection</th><th>Float</th><th>Price</th></tr></thead><tbody>
 ${x.inputs.map(i=>`<tr><td>${i.skin}</td><td class="${i.quality==='Souvenir'?'sv':''}">${i.quality}</td><td>${i.collection}</td><td>${i.float}</td><td>${i.price.toFixed(2)}</td></tr>`).join('')}</tbody></table>
 <table><thead><tr><th>Outcome</th><th>Wear</th><th>Float</th><th>P</th><th>Gross</th><th>Net</th></tr></thead><tbody>
 ${x.outcomes.map(o=>`<tr><td>${o.skin}</td><td>${o.wear}</td><td>${o.float.toFixed(4)}</td><td>${o.p_pct}%</td><td>${o.gross.toFixed(2)}</td><td>${o.net.toFixed(2)}${o.priced?'':' <span class="neg">no data</span>'}</td></tr>`).join('')}</tbody></table>`;
 det.scrollIntoView({behavior:'smooth'});}
</script></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX
