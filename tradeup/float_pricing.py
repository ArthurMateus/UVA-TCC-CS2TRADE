"""Second-pass validation of a contract against float-aware listing prices.

THE GAP THIS CLOSES

The search prices each input at the cheapest listing of its WEAR, but
evaluates the contract as if that input had a low float inside the wear
(`float_percentile`, 15% by default). The cheapest listing of a wear is
usually a HIGH float inside it, so the first pass pairs a good float with
a bad float's price: cost is understated exactly on the contracts whose
profit depends on landing a better output wear.

WHAT THIS DOES

1. From the contract's outcomes, compute the largest average trade-up
   float that keeps EVERY outcome in the wear the first pass assumed
   (`max_avg_preserving_wears`). Anything below it gives the same payout.
2. Spread that slack over the inputs: each group of identical inputs may
   go up to its assumed normalized float + (max_avg - assumed_avg), capped
   at its own wear's upper bound. This is the LOOSEST float cap that keeps
   the result, so it is also the cheapest honest one.
3. Ask a float-aware source for the k cheapest listings under that cap.
4. Re-evaluate the contract with the REAL floats and prices of those
   listings (outcomes, odds, fees: same engine as the first pass).

PRICE BASIS

* "premium" (default): Steam unit price x CSFloat float premium, where the
  premium is (k cheapest under the cap) / (k cheapest in the whole wear),
  both on CSFloat. Keeps the cost in the snapshot's market and currency.
* "csfloat": the actual CSFloat prices (USD) times `usd_rate`. Use when you
  would really buy the inputs on CSFloat.

A contract whose inputs cannot be bought (fewer than k listings under the
cap) is reported as `infeasible` -- itself a liquidity finding.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Literal, Optional

from .engine import evaluate_contract, trade_up_float
from .models import WEAR_BRACKETS, InputItem, Rarity, wear_from_float
from .prices.csfloat import FloatListingSource
from .risk import profile

Basis = Literal["premium", "csfloat"]
EPS = 1e-6


def _wear_bounds(wear: str) -> tuple[float, float]:
    b = next(x for x in WEAR_BRACKETS if x[0] == wear)
    return b[1], b[2]


def max_avg_preserving_wears(outcomes: list[tuple]) -> float:
    """Largest average trade-up float keeping every outcome in its wear.

    `outcomes` is [(Skin, wear)]. Output float = avg*span + min, and the
    wear's upper bound is exclusive, so the average must stay strictly
    below (hi - min)/span for every outcome.
    """
    limit = 1.0
    for s, w in outcomes:
        span = s.max_float - s.min_float
        if span <= 0:
            continue
        hi = min(_wear_bounds(w)[1], s.max_float)
        if hi >= s.max_float:            # top of the cap: any average works
            continue
        limit = min(limit, (hi - s.min_float) / span)
    return max(0.0, limit - EPS)


def float_dependence(inputs: list[tuple], outcomes: list[tuple]) -> dict:
    """How much the contract relies on buying LOW floats inside each wear.

    `inputs` is [(Skin, wear, assumed_float)], `outcomes` [(Skin, wear)].
    Prices are per wear (cheapest listing, any float), but the contract is
    evaluated at the assumed float. `float_slack` is how far the average
    trade-up float can rise before any outcome changes wear;
    `float_sensitive` is True when inputs at the TOP of their wear (the float
    the cheapest listing usually has) would already push an outcome into a
    worse wear -- i.e. the shown input price is probably optimistic.
    """
    max_avg = max_avg_preserving_wears(outcomes)
    norms, tops = [], []
    for s, wear, f in inputs:
        span = s.max_float - s.min_float
        norms.append(trade_up_float(f, s.min_float, s.max_float))
        hi = min(_wear_bounds(wear)[1], s.max_float) - EPS
        tops.append(0.0 if span <= 0 else (hi - s.min_float) / span)
    avg = sum(norms) / len(norms) if norms else 0.0
    top = sum(tops) / len(tops) if tops else 0.0
    return {"max_avg_float": round(max_avg, 6),
            "float_slack": round(max_avg - avg, 6),
            "avg_float_at_worst": round(top, 6),
            "float_sensitive": top > max_avg}


def _find_skin(db, name: str, collection: Optional[str], rarity: Optional[Rarity]):
    if collection:
        s = db.by_id(f"{name}|{collection}")
        if s is not None:
            return s
    return db.by_name(name)


def refine_row(row: dict, db, source: FloatListingSource, prices, *,
               fee_rate: Optional[float] = None, stattrak: bool = False,
               basis: Basis = "premium", usd_rate: Optional[float] = None) -> dict:
    """Return a `float_check` dict for one explained contract row."""
    if basis == "csfloat" and not usd_rate:
        return {"status": "unavailable",
                "reason": "basis=csfloat needs usd_rate (snapshot currency per USD)"}

    # -- rebuild the contract from the explained row ----------------------
    inputs: list[tuple] = []            # (skin, wear, souvenir, assumed_float, steam_price)
    for i in row["inputs"]:
        s = _find_skin(db, i["skin"], i.get("collection"), None)
        if s is None:
            return {"status": "unavailable", "reason": f"unknown skin {i['skin']!r}"}
        sv = i.get("quality") == "Souvenir"
        wear = i.get("wear") or wear_from_float(i["float"])
        for _ in range(int(i.get("count") or 1)):
            inputs.append((s, wear, sv, float(i["float"]), float(i["price"])))
    outs = []
    for o in row["outcomes"]:
        s = db.by_name(o["skin"])
        if s is not None:
            outs.append((s, o["wear"]))
    if not inputs or not outs:
        return {"status": "unavailable", "reason": "contract could not be rebuilt"}

    norms = [trade_up_float(f, s.min_float, s.max_float) for s, _, _, f, _ in inputs]
    assumed_avg = sum(norms) / len(norms)
    max_avg = max_avg_preserving_wears(outs)
    slack = max(0.0, max_avg - assumed_avg)

    # -- group identical inputs and query each group once -----------------
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    for (s, wear, sv, f, p), nrm in zip(inputs, norms):
        key = (s.id, wear, sv)
        g = groups.setdefault(key, {"skin": s, "wear": wear, "souvenir": sv,
                                    "assumed_float": f, "steam_price": p,
                                    "norm": nrm, "k": 0})
        g["k"] += 1

    new_items: list[InputItem] = []
    report = []
    for g in groups.values():
        s, wear, k = g["skin"], g["wear"], g["k"]
        name = s.market_hash_name(wear, stattrak, g["souvenir"])
        w_lo, w_hi = _wear_bounds(wear)
        lo = max(w_lo, s.min_float)
        cap = min(s.min_float + (g["norm"] + slack) * (s.max_float - s.min_float),
                  min(w_hi, s.max_float) - EPS)
        cap = max(cap, g["assumed_float"])
        try:
            got = source.cheapest(name, min_float=lo, max_float=cap, n=k)
        except Exception as e:               # network / auth: report, never crash
            return {"status": "unavailable", "reason": str(e)}
        entry = {"skin": s.name, "wear": wear, "quality": "Souvenir" if g["souvenir"] else "Normal",
                 "count": k, "assumed_float": round(g["assumed_float"], 5),
                 "max_float_allowed": round(cap, 5), "found": len(got),
                 "steam_unit_price": g["steam_price"]}
        if len(got) < k:
            entry["status"] = "not enough listings under the float cap"
            report.append(entry)
            return {"status": "infeasible", "inputs": report,
                    "max_avg_float": round(max_avg, 6),
                    "reason": f"only {len(got)} of {k} {name} listed with float <= {cap:.4f}"}

        if basis == "premium":
            base = source.cheapest(name, min_float=lo, max_float=min(w_hi, s.max_float), n=k)
            base_cost = sum(x.price for x in base[:k])
            capped_cost = sum(x.price for x in got)
            premium = (capped_cost / base_cost) if base_cost > 0 else 1.0
            premium = max(1.0, premium)          # a cap can only restrict the set
            unit = [g["steam_price"] * premium] * k
            entry["float_premium_pct"] = round((premium - 1) * 100, 2)
        else:
            unit = [x.price * float(usd_rate) for x in got]
        entry["unit_prices"] = [round(u, 4) for u in unit]
        entry["floats"] = [round(x.float_value, 6) for x in got]
        report.append(entry)
        for x, u in zip(got, unit):
            new_items.append(InputItem(s, x.float_value, round(u, 4), stattrak, g["souvenir"]))

    res = evaluate_contract(new_items, db.pool, prices.price_fn(), fee_rate=fee_rate,
                            stattrak=stattrak, skin_by_id=db.by_id)
    rk = profile(res.outcomes, res.cost)
    est_cost = float(row["cost"])
    was = {o["skin"]: o["wear"] for o in row["outcomes"]}
    return {
        "status": "ok",
        "basis": basis,
        "max_avg_float": round(max_avg, 6),
        "real_avg_float": round(res.avg_normalized, 6),
        "est_cost": round(est_cost, 4),
        "real_cost": res.cost,
        "cost_delta_pct": round((res.cost / est_cost - 1) * 100, 2) if est_cost else None,
        "est_profit": row.get("profit"),
        "real_ev_net": res.ev_net,
        "real_profit": res.profit,
        "real_roi_pct": round(res.roi * 100, 2),
        "real_win_chance_pct": round(rk.win_chance * 100, 2),
        "real_worst_net": rk.worst_net,
        "real_floor_positive": rk.floor_positive,
        "real_grade": rk.grade(),
        # real floats are <= the caps, so wears can only stay or improve
        "outcomes_changed": [{"skin": o.skin.name, "from": was[o.skin.name], "to": o.wear}
                             for o in res.outcomes
                             if o.skin.name in was and was[o.skin.name] != o.wear],
        "inputs": report,
    }


def refine_rows(rows: list[dict], db, source: FloatListingSource, prices, *,
                top: int = 10, **kw) -> list[dict]:
    """Attach `float_check` to the first `top` rows (in place) and return them."""
    for r in rows[:top]:
        r["float_check"] = refine_row(r, db, source, prices, **kw)
    return rows
