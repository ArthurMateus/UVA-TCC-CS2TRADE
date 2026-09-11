"""Budget-constrained search for profitable trade-up recipes.

The search space is huge (every combination of 10 inputs across every
collection and wear), so this uses a structured heuristic instead of brute
force -- the same structure real profitable contracts have:

    k inputs from a TARGET collection (the one with valuable outputs)
  + (N-k) FILLER inputs, chosen to be as cheap as possible and, ideally,
    to drag the normalized float average DOWN.

Since 22 May 2026 a purchasable "unit" is a (skin, wear, quality) triple,
where quality is normal or Souvenir. Souvenir copies are frequently the
cheapest way to fill a slot -- especially at Consumer and Industrial grade --
and the souvenir attributes are stripped by the contract anyway. The OUTPUT
is always normal quality.

Every candidate returned by `search` has been fully priced and scored by the
real engine; nothing is estimated at the end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .data.loader import SkinDB
from .engine import OddsModel, evaluate_contract, n_contracts_for_confidence
from .models import ContractResult, InputItem, Rarity, Skin
from .pricing import PriceService, sourceable_float


def contract_size(rarity: Rarity) -> int:
    """10 for normal contracts; Covert -> gold pool contracts take 5."""
    return 5 if rarity == Rarity.COVERT else 10


@dataclass(slots=True)
class Unit:
    """A purchasable input: a specific skin, wear and quality."""

    skin: Skin
    wear: str
    price: float
    float_value: float
    souvenir: bool = False

    @property
    def normalized(self) -> float:
        span = self.skin.max_float - self.skin.min_float
        return 0.0 if span <= 0 else (self.float_value - self.skin.min_float) / span

    @property
    def label(self) -> str:
        return f"{'Souvenir ' if self.souvenir else ''}{self.skin.name} ({self.wear})"


@dataclass(slots=True)
class Candidate:
    result: ContractResult
    target_collection: str
    filler_collection: Optional[str]
    k_target: int

    @property
    def score(self) -> float:
        return self.result.profit


def build_units(
    db: SkinDB,
    prices: PriceService,
    rarity: Rarity,
    *,
    float_percentile: float = 0.15,
    stattrak: bool = False,
    souvenir: bool = True,
    max_unit_price: Optional[float] = None,
) -> dict[str, list[Unit]]:
    """All purchasable inputs at `rarity`, grouped by collection.

    Only collections that actually have a higher tier are kept -- a skin from
    a dead-end collection is pure filler and contributes no outcome mass.

    `souvenir=True` additionally prices the Souvenir variant of every
    souvenir-capable skin and keeps it as a separate unit. StatTrak
    contracts cannot include Souvenir items, so the flag is forced off.
    """
    if stattrak:
        souvenir = False

    units: dict[str, list[Unit]] = {}
    for coll in db.tradeable_collections(rarity):
        for skin in db.pool(coll, rarity):
            if stattrak and not skin.stattrak_capable:
                continue
            for wear in skin.reachable_wears():
                f = sourceable_float(skin, wear, float_percentile)
                if f is None:
                    continue
                qualities = [False]
                if souvenir and skin.souvenir_capable:
                    qualities.append(True)
                for sv in qualities:
                    p = prices.buy_price(skin, wear, stattrak, sv)
                    if p is None or p <= 0:
                        continue
                    if max_unit_price is not None and p > max_unit_price:
                        continue
                    units.setdefault(coll, []).append(Unit(skin, wear, p, f, sv))
    for coll in units:
        units[coll].sort(key=lambda u: (u.price, u.normalized))
    return units


def _cheapest_low_float(units: list[Unit], n: int) -> list[Unit]:
    """Cheapest n units, tie-broken toward the lowest normalized float.

    Repeats are allowed: you can buy ten copies of the same cheap listing.
    """
    if not units:
        return []
    best = min(units, key=lambda u: (u.price, u.normalized))
    return [best] * n


def _best_float_efficient(units: list[Unit], n: int, budget: float) -> list[Unit]:
    """n copies of the unit with the lowest normalized float we can afford."""
    affordable = [u for u in units if u.price * n <= budget]
    if not affordable:
        return []
    best = min(affordable, key=lambda u: (u.normalized, u.price))
    return [best] * n


def search(
    db: SkinDB,
    prices: PriceService,
    rarity: Rarity,
    *,
    budget: float,
    odds_model: OddsModel = "outcome_weighted",
    fee_rate: Optional[float] = None,
    stattrak: bool = False,
    souvenir: bool = True,
    float_percentile: float = 0.15,
    min_roi: float = 1.0,
    max_unpriced_mass: float = 0.10,
    top_n: int = 25,
    max_filler_collections: int = 8,
    progress: Optional[Callable[[str], None]] = None,
) -> list[Candidate]:
    """Enumerate promising recipes at `rarity` under `budget` (total input cost)."""
    n_inputs = contract_size(rarity)
    if rarity.next_tier() is None:
        return []

    units = build_units(
        db, prices, rarity, float_percentile=float_percentile,
        stattrak=stattrak, souvenir=souvenir, max_unit_price=budget)
    if not units:
        return []

    # Good filler candidates are collections whose cheapest input is nearly free
    filler_ranked = sorted(units, key=lambda c: min(u.price for u in units[c]))
    fillers = filler_ranked[:max_filler_collections]

    pool, pfn = db.pool, prices.price_fn()
    out: list[Candidate] = []
    targets = list(units)

    for ti, target in enumerate(targets):
        if progress and ti % 10 == 0:
            progress(f"{ti}/{len(targets)} collections")
        t_units = units[target]

        # 1) pure single-collection contract
        combos: list[tuple[list[Unit], Optional[str], int]] = []
        pure = _cheapest_low_float(t_units, n_inputs)
        if pure:
            combos.append((pure, None, n_inputs))
        lowf = _best_float_efficient(t_units, n_inputs, budget)
        if lowf and lowf != pure:
            combos.append((lowf, None, n_inputs))

        # 2) target + filler mixes
        for filler in fillers:
            if filler == target:
                continue
            f_units = units[filler]
            for k in range(1, n_inputs):
                t_part = _cheapest_low_float(t_units, k)
                f_part = _cheapest_low_float(f_units, n_inputs - k)
                if not t_part or not f_part:
                    continue
                if sum(u.price for u in t_part + f_part) > budget:
                    continue
                combos.append((t_part + f_part, filler, k))

                # float-optimized variant: spend the remaining budget on
                # lower-float fillers, pulling the output wear down
                rest = budget - sum(u.price for u in t_part)
                f_low = _best_float_efficient(f_units, n_inputs - k, rest)
                if f_low and f_low != f_part:
                    combos.append((t_part + f_low, filler, k))

        for units_combo, filler, k in combos:
            cost = sum(u.price for u in units_combo)
            if cost > budget or cost <= 0:
                continue
            inputs = [InputItem(u.skin, u.float_value, u.price, stattrak, u.souvenir)
                      for u in units_combo]
            res = evaluate_contract(
                inputs, pool, pfn, odds_model=odds_model, fee_rate=fee_rate,
                stattrak=stattrak, skin_by_id=db.by_id)
            if res.unpriced_mass > max_unpriced_mass or res.roi < min_roi:
                continue
            out.append(Candidate(res, target, filler, k))

    # de-duplicate identical input baskets, keep the best
    seen: dict[tuple, Candidate] = {}
    for c in out:
        key = tuple(sorted((i.skin.id, round(i.float_value, 5), i.souvenir)
                           for i in c.result.inputs))
        if key not in seen or c.score > seen[key].score:
            seen[key] = c
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)[:top_n]


def robust_filter(
    candidates: Iterable[Candidate],
    db: SkinDB,
    prices: PriceService,
    *,
    fee_rate: Optional[float] = None,
) -> list[Candidate]:
    """Keep only candidates that stay profitable under BOTH odds models.

    Valve does not publish the outcome-selection rule and community tools
    disagree. A recipe profitable under only one interpretation is a
    modelling artifact, not an edge.
    """
    kept, pfn = [], prices.price_fn()
    for c in candidates:
        alt = evaluate_contract(
            c.result.inputs, db.pool, pfn, odds_model="per_input",
            fee_rate=fee_rate, stattrak=c.result.stattrak, skin_by_id=db.by_id)
        if alt.profit > 0 and c.result.profit > 0:
            kept.append(c)
    return kept


def explain(c: Candidate) -> dict:
    r = c.result
    return {
        "target_collection": c.target_collection,
        "filler_collection": c.filler_collection,
        "k_target": c.k_target,
        "cost": r.cost,
        "ev_net": r.ev_net,
        "profit": r.profit,
        "roi_pct": round(r.roi * 100, 2),
        "profit_chance_pct": round(r.profit_chance * 100, 2),
        "stddev": r.stddev,
        "avg_normalized_float": round(r.avg_normalized, 6),
        "unpriced_mass_pct": round(r.unpriced_mass * 100, 2),
        "souvenir_inputs": r.n_souvenir_inputs,
        "contracts_to_confidence": n_contracts_for_confidence(r),
        "inputs": [
            {
                "skin": i.skin.name,
                "quality": ("Souvenir" if i.souvenir
                            else ("StatTrak" if i.stattrak else "Normal")),
                "collection": i.skin.collection,
                "float": round(i.float_value, 5),
                "price": i.price,
            }
            for i in r.inputs
        ],
        "outcomes": [
            {
                "skin": o.skin.name, "wear": o.wear, "float": o.out_float,
                "p_pct": round(o.probability * 100, 3),
                "gross": o.gross_price, "net": o.net_price, "priced": o.priced,
            }
            for o in r.outcomes
        ],
    }
