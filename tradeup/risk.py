"""Risk metrics and guaranteed-profit search.

THE PROBLEM THIS SOLVES

Maximising expected value finds lottery tickets. A contract costing 101.80
with outcomes {271.60 @ 50%, 1.13 @ 25%, 0.93 @ 25%} is "EV positive", but
you lose money on 2 of 3 rolls and the whole edge rests on one illiquid
Well-Worn skin being priced correctly.

EV is the right metric only if you can run the contract enough times for the
average to materialise. At R$100 a go, nobody can. What matters instead is:

    P(win)          how often a SINGLE contract profits
    worst case      what you get when the cheapest outcome lands
    floor positive  is EVEN the worst outcome profitable?

A floor-positive contract is risk-free in the only sense that matters: every
possible result beats what you paid. They exist because input prices and
output prices are set by different supply dynamics -- a Mil-Spec skin nobody
wants can trade far below 1/11.5 of the cheapest Restricted in its own
collection.

THE CLOSED FORM

For a single-collection contract every outcome is equally likely, so:

    guaranteed  <=>  min(outcome_net) > total_input_cost
                <=>  cheapest_outcome * fee_multiplier > n * input_price
                <=>  input_price < cheapest_outcome * fee_multiplier / n

That is a direct constraint, not a search. For each collection we look up the
cheapest next-tier outcome, derive the maximum viable input price, and then
ask which inputs fall below it. O(collections), not combinatorial.

Mixed-collection contracts can also be floor positive, but only if EVERY
contributing collection's cheapest outcome clears the bar -- adding a
collection can only lower the floor, never raise it.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .models import Rarity, Skin

# Fraction of the buyer-facing price the seller keeps on Steam.
# 5% + 10% of the seller's proceeds == 13.04% of what the buyer pays.
STEAM_KEEP = 1.0 / 1.15


@dataclass(slots=True)
class RiskProfile:
    """What actually happens when you run this contract once."""

    cost: float = 0.0
    ev: float = 0.0

    worst_net: float = 0.0           # cheapest outcome, after fees
    best_net: float = 0.0
    median_net: float = 0.0          # probability-weighted 50th percentile

    win_chance: float = 0.0          # P(single roll beats cost)
    loss_chance: float = 0.0
    floor_positive: bool = False     # even the worst outcome profits

    worst_loss: float = 0.0          # most you can lose on one contract
    expected_loss: float = 0.0       # E[loss | you lose]
    downside_ev: float = 0.0         # probability-weighted loss

    stddev: float = 0.0
    top_ev_share: float = 0.0        # largest single outcome's share of EV
    n_outcomes: int = 0

    @property
    def worst_roi(self) -> float:
        return self.worst_net / self.cost if self.cost > 0 else 0.0

    @property
    def risk_reward(self) -> float:
        """Expected profit per unit of worst-case loss.

        Infinite (returned as a large sentinel) when nothing can be lost.
        """
        profit = self.ev - self.cost
        if self.worst_loss <= 0:
            return float("inf") if profit > 0 else 0.0
        return profit / self.worst_loss

    def grade(self) -> str:
        """A blunt label, so the UI does not have to re-derive judgement."""
        if self.floor_positive:
            return "guaranteed"
        if self.win_chance >= 0.75:
            return "strong"
        if self.win_chance >= 0.50:
            return "even"
        if self.win_chance >= 0.25:
            return "risky"
        return "lottery"


def profile(outcomes: Sequence, cost: float) -> RiskProfile:
    """Compute the risk profile of an evaluated contract.

    `outcomes` are `tradeup.models.Outcome` (or anything with
    `.probability` and `.net_price`).
    """
    p = RiskProfile(cost=round(cost, 4), n_outcomes=len(outcomes))
    if not outcomes or cost <= 0:
        return p

    nets = [(o.probability, o.net_price) for o in outcomes]
    nets.sort(key=lambda x: x[1])

    p.ev = round(sum(pr * n for pr, n in nets), 4)
    p.worst_net = round(nets[0][1], 4)
    p.best_net = round(nets[-1][1], 4)

    # probability-weighted median: walk the sorted outcomes to 50% mass
    acc = 0.0
    p.median_net = nets[-1][1]
    for pr, n in nets:
        acc += pr
        if acc >= 0.5:
            p.median_net = round(n, 4)
            break

    p.win_chance = round(sum(pr for pr, n in nets if n > cost), 6)
    p.loss_chance = round(1.0 - p.win_chance, 6)
    p.floor_positive = p.worst_net > cost

    p.worst_loss = round(max(0.0, cost - p.worst_net), 4)
    losing = [(pr, n) for pr, n in nets if n <= cost]
    loss_mass = sum(pr for pr, _ in losing)
    p.downside_ev = round(sum(pr * (cost - n) for pr, n in losing), 4)
    p.expected_loss = round(p.downside_ev / loss_mass, 4) if loss_mass > 0 else 0.0

    var = sum(pr * (n - p.ev) ** 2 for pr, n in nets)
    p.stddev = round(math.sqrt(max(var, 0.0)), 4)
    if p.ev > 0:
        p.top_ev_share = round(max(pr * n for pr, n in nets) / p.ev, 4)
    return p


# ---------------------------------------------------------------------------
# Guaranteed-profit search
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class GuaranteedRecipe:
    """A single-collection contract where every outcome beats the cost."""

    collection: str
    input_rarity: Rarity
    n_inputs: int
    inputs: list          # [(Skin, wear, float, price)] repeated to n_inputs
    outcomes: list        # [(Skin, wear, out_float, net)]
    cost: float
    worst_net: float
    best_net: float
    ev: float
    avg_trade_up_float: float

    @property
    def guaranteed_profit(self) -> float:
        return round(self.worst_net - self.cost, 4)

    @property
    def guaranteed_roi(self) -> float:
        return round(self.worst_net / self.cost, 4) if self.cost else 0.0

    @property
    def upside(self) -> float:
        return round(self.best_net - self.cost, 4)


def max_input_price_for_guarantee(
    cheapest_outcome_gross: float,
    n_inputs: int = 10,
    fee_keep: float = STEAM_KEEP,
    haircut: float = 0.0,
) -> float:
    """The most you can pay per input and still be guaranteed a profit.

    Solves  cheapest_outcome * keep * (1-haircut) > n * price  for price.
    """
    if cheapest_outcome_gross <= 0 or n_inputs <= 0:
        return 0.0
    net = cheapest_outcome_gross * fee_keep * (1.0 - haircut)
    return net / n_inputs


def find_guaranteed(
    db,
    prices,
    rarity: Rarity,
    *,
    budget: float,
    float_percentile: float = 0.15,
    souvenir: bool = True,
    fee_keep: float = STEAM_KEEP,
    haircut: float = 0.03,
    min_profit: float = 0.0,
    require_all_outcomes_priced: bool = True,
    liquidity_fn: Optional[Callable[[str], Optional[int]]] = None,
    min_listings: int = 0,
    progress: Optional[Callable[[str], None]] = None,
) -> list[GuaranteedRecipe]:
    """Find single-collection contracts where the WORST outcome still profits.

    Direct rather than combinatorial: for each collection, price its outcomes,
    take the cheapest, derive the input price ceiling, and keep inputs under
    it. If none qualify, that collection cannot produce a guarantee and is
    skipped immediately.

    `require_all_outcomes_priced` matters: an unpriced outcome is scored as
    zero elsewhere in this codebase, which would make the floor look like a
    total loss. Here we refuse to call a contract guaranteed when we cannot
    see every outcome's price.
    """
    from .engine import outcome_float, trade_up_float
    from .models import wear_from_float
    from .pricing import sourceable_float

    out: list[GuaranteedRecipe] = []
    nxt = rarity.next_tier()
    if nxt is None:
        return out
    n_inputs = 5 if rarity == Rarity.COVERT else 10

    colls = db.tradeable_collections(rarity)
    for ci, coll in enumerate(colls):
        if progress and ci % 10 == 0:
            progress(f"{ci}/{len(colls)} collections")

        out_skins = db.pool(coll, nxt)
        if not out_skins:
            continue

        # Cheapest possible output across every reachable wear. This is the
        # floor of the contract before float is chosen.
        cheapest = None
        for s in out_skins:
            for w in s.reachable_wears():
                g = prices.sell_price(s, w)
                if g is None:
                    continue
                if cheapest is None or g < cheapest:
                    cheapest = g
        if cheapest is None:
            continue

        ceiling = max_input_price_for_guarantee(
            cheapest, n_inputs, fee_keep, haircut)
        if ceiling <= 0:
            continue

        # Any input under the ceiling makes the contract floor-positive.
        # Take the cheapest with the lowest normalized float, which also
        # maximises the chance of landing outputs in a better wear.
        best_unit = None
        for skin in db.pool(coll, rarity):
            for wear in skin.reachable_wears():
                fv = sourceable_float(skin, wear, float_percentile)
                if fv is None:
                    continue
                for sv in ((False, True) if (souvenir and skin.souvenir_capable)
                           else (False,)):
                    p = prices.buy_price(skin, wear, False, sv)
                    if p is None or p <= 0 or p >= ceiling:
                        continue
                    if p * n_inputs > budget:
                        continue
                    if liquidity_fn and min_listings:
                        got = liquidity_fn(skin.market_hash_name(wear, False, sv))
                        if got is not None and got < min_listings:
                            continue
                    norm = trade_up_float(fv, skin.min_float, skin.max_float)
                    key = (p, norm)
                    if best_unit is None or key < best_unit[0]:
                        best_unit = (key, skin, wear, fv, p, sv, norm)
        if best_unit is None:
            continue

        _, skin, wear, fv, price, sv, norm = best_unit
        cost = round(price * n_inputs, 4)

        # every input identical -> the average IS that input's adjusted float
        avg = norm
        resolved: list[tuple] = []
        ok = True
        for s in out_skins:
            f = outcome_float(avg, s)
            w = wear_from_float(f)
            g = prices.sell_price(s, w)
            if g is None:
                if require_all_outcomes_priced:
                    ok = False
                    break
                continue
            resolved.append((s, w, round(f, 8), round(g, 4)))
        if not ok or not resolved:
            continue

        nets = [r[3] for r in resolved]
        worst, best = min(nets), max(nets)
        if worst <= cost or (worst - cost) < min_profit:
            continue

        out.append(GuaranteedRecipe(
            collection=coll, input_rarity=rarity, n_inputs=n_inputs,
            inputs=[(skin, wear, fv, price, sv)] * n_inputs,
            outcomes=resolved, cost=cost,
            worst_net=round(worst, 4), best_net=round(best, 4),
            ev=round(sum(nets) / len(nets), 4),
            avg_trade_up_float=round(avg, 8),
        ))

    out.sort(key=lambda r: r.guaranteed_profit, reverse=True)
    return out


def explain_guaranteed(r: GuaranteedRecipe) -> dict:
    skin, wear, fv, price, sv = r.inputs[0]
    return {
        "mode": "guaranteed",
        "target_collection": r.collection,
        "filler_collection": None,
        "k_target": r.n_inputs,
        "cost": r.cost,
        "ev_net": r.ev,
        "profit": round(r.ev - r.cost, 4),
        "roi_pct": round(r.ev / r.cost * 100, 2) if r.cost else 0.0,
        "guaranteed_profit": r.guaranteed_profit,
        "guaranteed_roi_pct": round(r.guaranteed_roi * 100, 2),
        "upside": r.upside,
        "worst_net": r.worst_net,
        "best_net": r.best_net,
        "win_chance_pct": 100.0,
        "grade": "guaranteed",
        "avg_normalized_float": r.avg_trade_up_float,
        "souvenir_inputs": r.n_inputs if sv else 0,
        "inputs": [{
            "skin": skin.name,
            "quality": "Souvenir" if sv else "Normal",
            "collection": skin.collection,
            "wear": wear,
            "float": round(fv, 5),
            "min_float": round(skin.min_float, 4),
            "max_float": round(skin.max_float, 4),
            "price": price,
            "count": r.n_inputs,
        }],
        "outcomes": [{
            "skin": s.name, "wear": w, "float": f,
            "p_pct": round(100.0 / len(r.outcomes), 3),
            "net": net, "gross": round(net / (1 - 0.03) if net else 0, 4),
            "min_float": round(s.min_float, 4),
            "max_float": round(s.max_float, 4),
            "priced": True,
            "profits": net > r.cost,
        } for s, w, f, net in sorted(r.outcomes, key=lambda x: x[3])],
    }
