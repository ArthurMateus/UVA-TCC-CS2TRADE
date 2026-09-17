"""Budget-constrained recipe search, risk-aware.

Two modes, because they answer different questions:

  GUARANTEED  every outcome beats the cost. Solved in closed form by
              risk.find_guaranteed() -- no combinatorics needed.
  RANKED      the general heuristic search, but now scored and filtered on
              P(win) and worst case, not on expected value alone.

The old default (sort by EV) surfaced lottery tickets: contracts with a 3%
chance of a large payout and a 97% chance of loss. Those are only rational
if you can run them hundreds of times. The defaults here target contracts
that win on a single roll.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from .data.loader import SkinDB
from .engine import (DEFAULT_ODDS_MODEL, OddsModel, evaluate_contract,
                     n_contracts_for_confidence)
from .models import ContractResult, InputItem, Rarity, Skin
from .pricing import PriceService, sourceable_float
from .risk import RiskProfile, profile

def contract_size(r: Rarity) -> int:
    return 5 if r == Rarity.COVERT else 10

@dataclass(slots=True)
class Unit:
    skin: Skin; wear: str; price: float; float_value: float; souvenir: bool = False
    @property
    def normalized(self) -> float:
        s = self.skin.max_float - self.skin.min_float
        return 0.0 if s <= 0 else (self.float_value - self.skin.min_float)/s

@dataclass(slots=True)
class Candidate:
    result: ContractResult
    target_collection: str
    filler_collection: Optional[str]
    k_target: int
    risk: Optional[RiskProfile] = None
    @property
    def score(self) -> float:
        return self.result.profit

def build_units(db, prices, rarity, *, float_percentile=0.15, stattrak=False,
                souvenir=True, max_unit_price=None) -> dict[str, list[Unit]]:
    """Purchasable inputs grouped by collection. A unit is (skin, wear, quality)."""
    if stattrak: souvenir = False
    u: dict[str, list[Unit]] = {}
    for coll in db.tradeable_collections(rarity):
        for skin in db.pool(coll, rarity):
            if stattrak and not skin.stattrak_capable: continue
            for wear in skin.reachable_wears():
                f = sourceable_float(skin, wear, float_percentile)
                if f is None: continue
                quals = (False, True) if (souvenir and skin.souvenir_capable) else (False,)
                for sv in quals:
                    p = prices.buy_price(skin, wear, stattrak, sv)
                    if p is None or p <= 0: continue
                    if max_unit_price is not None and p > max_unit_price: continue
                    u.setdefault(coll, []).append(Unit(skin, wear, p, f, sv))
    for c in u:
        u[c].sort(key=lambda x: (x.price, x.normalized))
    return u

def _cheapest(units, n):
    if not units: return []
    return [min(units, key=lambda u: (u.price, u.normalized))] * n

def _lowest_float(units, n, budget):
    aff = [u for u in units if u.price * n <= budget]
    if not aff: return []
    return [min(aff, key=lambda u: (u.normalized, u.price))] * n

def search(db, prices, rarity, *, budget, odds_model=DEFAULT_ODDS_MODEL,
           fee_rate=None, stattrak=False, souvenir=True, float_percentile=0.15,
           min_roi=1.0, min_win_chance=0.0, max_unpriced_mass=0.10,
           require_all_priced=True, top_n=50, max_filler_collections=8,
           progress=None) -> list[Candidate]:
    """Heuristic search, filtered on win chance as well as ROI.

    `require_all_priced` defaults True: an unpriced outcome scores as zero,
    which understates the floor and can make a bad contract look survivable.
    """
    n_in = contract_size(rarity)
    if rarity.next_tier() is None: return []
    units = build_units(db, prices, rarity, float_percentile=float_percentile,
                        stattrak=stattrak, souvenir=souvenir, max_unit_price=budget)
    if not units: return []
    fillers = sorted(units, key=lambda c: min(u.price for u in units[c]))[:max_filler_collections]
    pool, pfn = db.pool, prices.price_fn()
    out: list[Candidate] = []
    targets = list(units)
    for ti, target in enumerate(targets):
        if progress and ti % 10 == 0:
            progress(f"{ti}/{len(targets)} collections")
        tu = units[target]
        combos: list[tuple] = []
        pure = _cheapest(tu, n_in)
        if pure: combos.append((pure, None, n_in))
        lf = _lowest_float(tu, n_in, budget)
        if lf and lf != pure: combos.append((lf, None, n_in))
        for fl in fillers:
            if fl == target: continue
            fu = units[fl]
            for k in range(1, n_in):
                tp, fp = _cheapest(tu, k), _cheapest(fu, n_in-k)
                if not tp or not fp: continue
                if sum(x.price for x in tp+fp) > budget: continue
                combos.append((tp+fp, fl, k))
                rest = budget - sum(x.price for x in tp)
                fl2 = _lowest_float(fu, n_in-k, rest)
                if fl2 and fl2 != fp: combos.append((tp+fl2, fl, k))
        for cu, fl, k in combos:
            cost = sum(x.price for x in cu)
            if cost > budget or cost <= 0: continue
            ins = [InputItem(x.skin, x.float_value, x.price, stattrak, x.souvenir)
                   for x in cu]
            res = evaluate_contract(ins, pool, pfn, odds_model=odds_model,
                                    fee_rate=fee_rate, stattrak=stattrak,
                                    skin_by_id=db.by_id)
            if require_all_priced and res.unpriced_mass > 0: continue
            if res.unpriced_mass > max_unpriced_mass: continue
            if res.roi < min_roi: continue
            if res.profit_chance < min_win_chance: continue
            out.append(Candidate(res, target, fl, k, profile(res.outcomes, res.cost)))
    seen: dict = {}
    for c in out:
        key = tuple(sorted((i.skin.id, round(i.float_value,5), i.souvenir)
                           for i in c.result.inputs))
        if key not in seen or c.score > seen[key].score:
            seen[key] = c
    ranked = sorted(seen.values(),
                    key=lambda c: (c.risk.win_chance if c.risk else 0, c.score),
                    reverse=True)
    return ranked[:top_n]

def explain(c: Candidate) -> dict:
    r = c.result
    rk = c.risk or profile(r.outcomes, r.cost)
    return {
        "mode": "ranked",
        "target_collection": c.target_collection,
        "filler_collection": c.filler_collection,
        "k_target": c.k_target,
        "cost": r.cost, "ev_net": r.ev_net, "profit": r.profit,
        "roi_pct": round(r.roi*100, 2),
        "win_chance_pct": round(rk.win_chance*100, 2),
        "grade": rk.grade(),
        "worst_net": rk.worst_net, "best_net": rk.best_net,
        "median_net": rk.median_net,
        "worst_loss": rk.worst_loss, "expected_loss": rk.expected_loss,
        "guaranteed_profit": round(rk.worst_net - rk.cost, 4),
        "floor_positive": rk.floor_positive,
        "risk_reward": (None if rk.risk_reward == float("inf")
                        else round(rk.risk_reward, 3)),
        "top_ev_share_pct": round(rk.top_ev_share*100, 1),
        "stddev": r.stddev,
        "avg_normalized_float": round(r.avg_normalized, 8),
        "unpriced_mass_pct": round(r.unpriced_mass*100, 2),
        "souvenir_inputs": r.n_souvenir_inputs,
        "contracts_to_confidence": n_contracts_for_confidence(r),
        "inputs": [{"skin": i.skin.name,
                    "quality": "Souvenir" if i.souvenir else "Normal",
                    "collection": i.skin.collection,
                    "float": round(i.float_value,5),
                    "min_float": round(i.skin.min_float,4),
                    "max_float": round(i.skin.max_float,4),
                    "price": i.price} for i in r.inputs],
        "outcomes": [{"skin": o.skin.name, "wear": o.wear, "float": o.out_float,
                      "p_pct": round(o.probability*100,3),
                      "gross": o.gross_price, "net": o.net_price,
                      "min_float": round(o.skin.min_float,4),
                      "max_float": round(o.skin.max_float,4),
                      "priced": o.priced,
                      "profits": o.net_price > r.cost}
                     for o in sorted(r.outcomes, key=lambda x: x.net_price)],
    }
