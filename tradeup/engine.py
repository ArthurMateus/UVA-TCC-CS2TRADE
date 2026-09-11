"""Trade-up math: float mapping, outcome odds, expected value.

Terminology follows the in-game model (post-Retakes Update):

  Float Value          the raw wear rating on the item, 0.00-1.00
  Float Cap            a skin's own [min_float, max_float]; not every skin
                       spans the full range
  Trade-up Float       the float rescaled onto a universal 0-1 axis using the
                       skin's OWN cap:  (f - min) / (max - min)
                       Also called the Adjusted Float.
  Average Trade-up     the mean of the inputs' Trade-up Floats. NOT the mean
  Float                of the raw floats -- that is the pre-Retakes model and
                       gives wrong answers whenever caps differ.
  Outcome Float        the Average Trade-up Float rescaled back into the
                       OUTPUT skin's cap: avg * (omax - omin) + omin

Verified against known-good worked examples to 8 decimal places:

  P90 | Trigon 0.749 in cap 0.08-0.75      -> trade-up float 0.99850746
  10 of them                               -> average       0.99850746
  onto AWP | Asiimov cap 0.18-1.00         -> outcome float 0.99877612
  onto AUG | Chameleon cap 0.00-0.50       -> outcome float 0.49925373
  MAC-10 | Sakkaku 0.22798392 in 0.21-0.79 -> trade-up float 0.03100676

Everything here is pure and deterministic so it can be unit-tested without
network access. Prices are injected via a `price_fn` callback.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Literal, Optional, Sequence

from .models import ContractResult, InputItem, Outcome, Rarity, Skin, wear_from_float

# "per_input" is the CURRENT in-game rule (post-Retakes Update).
# "outcome_weighted" is the LEGACY pre-Retakes rule, kept only so you can
# diff against older calculators. Do not price real contracts with it.
OddsModel = Literal["per_input", "outcome_weighted"]

DEFAULT_ODDS_MODEL: OddsModel = "per_input"

STEAM_PLATFORM_FEE = 0.05   # Valve
STEAM_GAME_FEE = 0.10       # CS2 publisher fee
STEAM_TOTAL_FEE = STEAM_PLATFORM_FEE + STEAM_GAME_FEE


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def steam_net_from_buyer_price(buyer_price: float) -> float:
    """Seller proceeds when the BUYER pays `buyer_price`.

    Steam builds the fee ON TOP of the seller's cut:
        buyer = seller * (1 + 0.05 + 0.10)
    so the seller keeps buyer / 1.15 (~86.96%), not buyer * 0.85.
    A $0.01 minimum applies to each of the two components.
    """
    if buyer_price <= 0:
        return 0.0
    seller = buyer_price / (1.0 + STEAM_TOTAL_FEE)
    platform = max(0.01, math.floor(seller * STEAM_PLATFORM_FEE * 100) / 100)
    game = max(0.01, math.floor(seller * STEAM_GAME_FEE * 100) / 100)
    return round(max(0.0, buyer_price - platform - game), 4)


def net_after_fee(buyer_price: float, fee_rate: Optional[float] = None) -> float:
    """Generic marketplace net. `fee_rate=None` -> exact Steam model."""
    if fee_rate is None:
        return steam_net_from_buyer_price(buyer_price)
    return round(buyer_price * (1.0 - fee_rate), 4)


# ---------------------------------------------------------------------------
# Float mapping
# ---------------------------------------------------------------------------
def trade_up_float(float_value: float, min_float: float, max_float: float) -> float:
    """Rescale a raw float onto the universal 0-1 axis using the skin's cap.

    Also known as the Adjusted Float. This is why two items showing the same
    raw float can contribute very differently: 0.10 on a 0.00-1.00 skin is
    0.10, but 0.10 on a 0.00-0.50 skin is 0.20 -- already 20% through its
    own range.
    """
    span = max_float - min_float
    if span <= 0:
        return 0.0
    return (float_value - min_float) / span


def average_trade_up_float(inputs: Sequence[InputItem]) -> float:
    """Mean of the inputs' Trade-up Floats.

    NOT the mean of raw floats. Averaging raw floats is the pre-Retakes
    model and silently misprices any contract whose inputs have different
    float caps.
    """
    if not inputs:
        return 0.0
    return sum(i.trade_up_float for i in inputs) / len(inputs)


def outcome_float(avg_trade_up_float: float, out_skin: Skin) -> float:
    """Rescale the Average Trade-up Float into the OUTPUT skin's own cap.

    Each possible outcome has its own cap, so one contract yields a
    different outcome float -- and possibly a different wear tier -- per
    outcome skin.
    """
    return (avg_trade_up_float * (out_skin.max_float - out_skin.min_float)
            + out_skin.min_float)


# Backwards-compatible aliases (older code called these normalized/average)
normalized_float = trade_up_float
average_normalized = average_trade_up_float


def required_avg_for_wear(out_skin: Skin, target_wear: str) -> tuple[float, float]:
    """The (lo, hi) Average Trade-up Float window that lands `out_skin` in
    `target_wear`. Returns (nan, nan) if that wear is unreachable."""
    from .models import WEAR_BRACKETS

    bracket = next((b for b in WEAR_BRACKETS if b[0] == target_wear), None)
    if bracket is None:
        return (float("nan"), float("nan"))
    _, w_lo, w_hi = bracket
    span = out_skin.max_float - out_skin.min_float
    if span <= 0:
        return ((0.0, 1.0) if w_lo <= out_skin.min_float < w_hi
                else (float("nan"), float("nan")))
    lo = (max(w_lo, out_skin.min_float) - out_skin.min_float) / span
    hi = (min(w_hi, out_skin.max_float) - out_skin.min_float) / span
    if hi <= lo:
        return (float("nan"), float("nan"))
    return (max(0.0, lo), min(1.0, hi))


required_avg_norm_for_wear = required_avg_for_wear  # legacy alias


# ---------------------------------------------------------------------------
# Contract legality
# ---------------------------------------------------------------------------
def validate_contract(inputs: Sequence[InputItem]) -> list[str]:
    """Rule violations that would make the in-game contract impossible."""
    problems: list[str] = []
    if not inputs:
        return ["no inputs"]
    rarities = {i.skin.rarity for i in inputs}
    if len(rarities) > 1:
        problems.append("inputs span multiple rarities: "
                        + ", ".join(sorted(r.label for r in rarities)))
    has_st = any(i.stattrak for i in inputs)
    has_sv = any(i.souvenir for i in inputs)
    if has_st and not all(i.stattrak for i in inputs):
        problems.append("StatTrak must be all-or-nothing")
    if has_st and has_sv:
        problems.append("StatTrak items cannot be mixed with Souvenir items")
    for i in inputs:
        if not (i.skin.min_float <= i.float_value <= i.skin.max_float):
            problems.append(
                f"{i.skin.name} float {i.float_value:.4f} is outside its cap "
                f"{i.skin.min_float:.2f}-{i.skin.max_float:.2f}")
        if i.souvenir and not i.skin.souvenir_capable:
            problems.append(f"{i.skin.name} has no Souvenir variant")
        if i.stattrak and not i.skin.stattrak_capable:
            problems.append(f"{i.skin.name} has no StatTrak variant")
    return problems


# ---------------------------------------------------------------------------
# Outcome probabilities
# ---------------------------------------------------------------------------
def collection_odds(
    inputs: Sequence[InputItem],
    pool: Callable[[str, Rarity], list[Skin]],
) -> dict[str, float]:
    """P(the outcome comes from each collection), current rules.

    Each input contributes an equal 1/n share, so a collection's chance is
    simply how many of your inputs came from it:

        P(collection C) = inputs_from_C / n

    Seven inputs from one collection is a 70% chance of hitting that
    collection, regardless of how many outcomes any collection has.
    """
    if not inputs:
        return {}
    tier = inputs[0].skin.rarity.next_tier()
    if tier is None:
        return {}
    counts = Counter(i.skin.collection for i in inputs)
    # collections with no higher tier are dead ends: they contribute no
    # outcome mass, and the remaining share is renormalized over the rest
    live = {c: n for c, n in counts.items() if pool(c, tier)}
    total = sum(live.values())
    if not total:
        return {}
    return {c: n / total for c, n in live.items()}


def outcome_probabilities(
    inputs: Sequence[InputItem],
    pool: Callable[[str, Rarity], list[Skin]],
    model: OddsModel = DEFAULT_ODDS_MODEL,
) -> dict[str, float]:
    """Probability per output skin id.

    `per_input` -- CURRENT in-game rule. Odds are per item, not per
    collection outcome count. Each input carries 1/n; that share is then
    split evenly across its collection's outcomes at the next tier:

        P(skin s in collection C) = (inputs_from_C / n) / k_C

    where k_C is the number of outcomes collection C offers. So one
    M4A1-S | Knight among ten inputs is a flat 10% at Dragon Lore, because
    Cobblestone offers exactly one outcome above it -- while a collection
    offering two outcomes yields 5% each for the same single input.

    `outcome_weighted` -- LEGACY pre-Retakes rule, where a collection with
    more outcomes absorbed proportionally more total mass:

        P(skin s in C) = inputs_from_C / sum_k(inputs_from_k * k_k)

    On the ten-input example above this returns a uniform 5.26% for every
    outcome, which no longer matches the game. Kept only for comparison
    against older calculators.
    """
    if not inputs:
        return {}
    tier = inputs[0].skin.rarity.next_tier()
    if tier is None:
        return {}

    counts = Counter(i.skin.collection for i in inputs)
    pools = {c: pool(c, tier) for c in counts}
    pools = {c: p for c, p in pools.items() if p}
    if not pools:
        return {}

    probs: dict[str, float] = {}
    if model == "per_input":
        n = sum(counts[c] for c in pools)
        for c, skins in pools.items():
            share = counts[c] / n          # collection odds
            each = share / len(skins)      # split evenly across its outcomes
            for s in skins:
                probs[s.id] = probs.get(s.id, 0.0) + each
    elif model == "outcome_weighted":
        denom = sum(counts[c] * len(pools[c]) for c in pools)
        for c, skins in pools.items():
            for s in skins:
                probs[s.id] = probs.get(s.id, 0.0) + counts[c] / denom
    else:
        raise ValueError(f"unknown odds model: {model!r}")

    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()}


# ---------------------------------------------------------------------------
# Full contract evaluation
# ---------------------------------------------------------------------------
def evaluate_contract(
    inputs: Sequence[InputItem],
    pool: Callable[[str, Rarity], list[Skin]],
    price_fn: Callable[[Skin, str, bool], Optional[float]],
    *,
    odds_model: OddsModel = DEFAULT_ODDS_MODEL,
    fee_rate: Optional[float] = None,
    stattrak: bool = False,
    skin_by_id: Optional[Callable[[str], Skin]] = None,
) -> ContractResult:
    """Evaluate one concrete contract.

    `price_fn(skin, wear, stattrak)` prices the OUTPUT. It is never called
    with a souvenir flag: a trade-up output is always normal quality, even
    when every input was a Souvenir.
    """
    inputs = list(inputs)
    res = ContractResult(inputs=inputs, stattrak=stattrak)
    if not inputs:
        return res

    res.avg_normalized = average_trade_up_float(inputs)
    res.cost = round(sum(i.price for i in inputs), 4)
    res.collection_odds = collection_odds(inputs, pool)

    probs = outcome_probabilities(inputs, pool, odds_model)
    tier = inputs[0].skin.rarity.next_tier()
    lookup: dict[str, Skin] = {}
    for c in {i.skin.collection for i in inputs}:
        for s in pool(c, tier):
            lookup[s.id] = s

    outcomes: list[Outcome] = []
    for sid, p in probs.items():
        s = lookup.get(sid) or (skin_by_id(sid) if skin_by_id else None)
        if s is None:
            continue
        f = outcome_float(res.avg_normalized, s)
        wear = wear_from_float(f)
        gross = price_fn(s, wear, stattrak)
        priced = gross is not None
        gross = gross or 0.0
        outcomes.append(Outcome(
            skin=s, probability=p, out_float=round(f, 8), wear=wear,
            gross_price=round(gross, 4), net_price=net_after_fee(gross, fee_rate),
            priced=priced))

    outcomes.sort(key=lambda o: o.net_price, reverse=True)
    res.outcomes = outcomes
    res.unpriced_mass = round(sum(o.probability for o in outcomes if not o.priced), 6)
    res.ev_net = round(sum(o.probability * o.net_price for o in outcomes), 4)
    res.profit = round(res.ev_net - res.cost, 4)
    res.roi = round(res.ev_net / res.cost, 4) if res.cost > 0 else 0.0
    res.profit_chance = round(
        sum(o.probability for o in outcomes if o.net_price > res.cost), 6)
    var = sum(o.probability * (o.net_price - res.ev_net) ** 2 for o in outcomes)
    res.stddev = round(math.sqrt(max(var, 0.0)), 4)
    return res


def kelly_fraction(res: ContractResult) -> float:
    """Rough Kelly-style sizing signal (Gaussian approximation)."""
    if res.stddev <= 0 or res.cost <= 0:
        return 0.0
    return max(0.0, min(1.0, res.profit / (res.stddev ** 2)))


def n_contracts_for_confidence(res: ContractResult, z: float = 2.0) -> Optional[int]:
    """How many identical contracts before the edge dominates the noise.

    n such that n*profit >= z * stddev * sqrt(n)  ->  n >= (z*sd/profit)^2
    """
    if res.profit <= 0 or res.stddev <= 0:
        return None
    return math.ceil((z * res.stddev / res.profit) ** 2)
