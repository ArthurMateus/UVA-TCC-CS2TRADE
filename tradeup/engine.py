"""Trade-up math: float mapping, per-item odds, fees, expected value.

Pure and deterministic. Prices arrive via a `price_fn` callback, so the
whole module is testable with no network.

Terminology (post-Retakes Update):
  Trade-up Float      (f - min) / (max - min), using the skin's OWN cap
  Average Trade-up    mean of those over n inputs (NOT a mean of raw floats)
  Outcome Float       average rescaled into the OUTPUT skin's cap
"""
from __future__ import annotations
import math
from collections import Counter
from typing import Callable, Literal, Optional, Sequence
from .models import ContractResult, InputItem, Outcome, Rarity, Skin, wear_from_float

OddsModel = Literal["per_input", "outcome_weighted"]
DEFAULT_ODDS_MODEL: OddsModel = "per_input"

STEAM_PLATFORM_FEE = 0.05
STEAM_GAME_FEE = 0.10
STEAM_TOTAL_FEE = 0.15          # of the SELLER's proceeds == 13.04% of buyer price

def steam_net_from_buyer_price(buyer: float) -> float:
    """Seller proceeds when the buyer pays `buyer`.

    The fee is built on top of the seller's cut, so you keep buyer/1.15
    (86.96%), not buyer*0.85. Each component floor-rounds with a 0.01 min.
    """
    if buyer <= 0:
        return 0.0
    s = buyer / (1.0 + STEAM_TOTAL_FEE)
    p = max(0.01, math.floor(s * STEAM_PLATFORM_FEE * 100) / 100)
    g = max(0.01, math.floor(s * STEAM_GAME_FEE * 100) / 100)
    return round(max(0.0, buyer - p - g), 4)

def net_after_fee(buyer: float, fee_rate: Optional[float] = None) -> float:
    return steam_net_from_buyer_price(buyer) if fee_rate is None \
        else round(buyer * (1.0 - fee_rate), 4)

def trade_up_float(f: float, mn: float, mx: float) -> float:
    s = mx - mn
    return 0.0 if s <= 0 else (f - mn) / s

def average_trade_up_float(inputs: Sequence[InputItem]) -> float:
    return 0.0 if not inputs else sum(i.trade_up_float for i in inputs) / len(inputs)

def outcome_float(avg: float, out: Skin) -> float:
    return avg * (out.max_float - out.min_float) + out.min_float

normalized_float = trade_up_float
average_normalized = average_trade_up_float

def required_avg_for_wear(out: Skin, wear: str) -> tuple[float, float]:
    from .models import WEAR_BRACKETS
    b = next((x for x in WEAR_BRACKETS if x[0] == wear), None)
    if b is None:
        return (float("nan"),) * 2
    _, wlo, whi = b
    span = out.max_float - out.min_float
    if span <= 0:
        return (0.0, 1.0) if wlo <= out.min_float < whi else (float("nan"),) * 2
    lo = (max(wlo, out.min_float) - out.min_float) / span
    hi = (min(whi, out.max_float) - out.min_float) / span
    return (max(0.0, lo), min(1.0, hi)) if hi > lo else (float("nan"),) * 2

def validate_contract(inputs: Sequence[InputItem]) -> list[str]:
    p: list[str] = []
    if not inputs:
        return ["no inputs"]
    if len({i.skin.rarity for i in inputs}) > 1:
        p.append("inputs span multiple rarities")
    st = any(i.stattrak for i in inputs); sv = any(i.souvenir for i in inputs)
    if st and not all(i.stattrak for i in inputs):
        p.append("StatTrak must be all-or-nothing")
    if st and sv:
        p.append("StatTrak items cannot be mixed with Souvenir items")
    for i in inputs:
        if not (i.skin.min_float <= i.float_value <= i.skin.max_float):
            p.append(f"{i.skin.name} float {i.float_value:.4f} outside cap "
                     f"{i.skin.min_float:.2f}-{i.skin.max_float:.2f}")
        if i.souvenir and not i.skin.souvenir_capable:
            p.append(f"{i.skin.name} has no Souvenir variant")
    return p

def collection_odds(inputs, pool) -> dict[str, float]:
    """P(outcome comes from each collection) = inputs_from_C / n."""
    if not inputs:
        return {}
    t = inputs[0].skin.rarity.next_tier()
    if t is None:
        return {}
    c = Counter(i.skin.collection for i in inputs)
    live = {k: v for k, v in c.items() if pool(k, t)}
    tot = sum(live.values())
    return {} if not tot else {k: v / tot for k, v in live.items()}

def outcome_probabilities(inputs, pool, model: OddsModel = DEFAULT_ODDS_MODEL):
    """per_input: each input carries 1/n, split across its collection's outcomes.

    P(skin s in C) = (inputs_from_C / n) / outcomes_in_C
    outcome_weighted is the LEGACY pre-Retakes rule, kept for comparison only.
    """
    if not inputs:
        return {}
    t = inputs[0].skin.rarity.next_tier()
    if t is None:
        return {}
    c = Counter(i.skin.collection for i in inputs)
    pools = {k: pool(k, t) for k in c}
    pools = {k: v for k, v in pools.items() if v}
    if not pools:
        return {}
    pr: dict[str, float] = {}
    if model == "per_input":
        n = sum(c[k] for k in pools)
        for k, sk in pools.items():
            each = (c[k] / n) / len(sk)
            for s in sk:
                pr[s.id] = pr.get(s.id, 0.0) + each
    elif model == "outcome_weighted":
        d = sum(c[k] * len(pools[k]) for k in pools)
        for k, sk in pools.items():
            for s in sk:
                pr[s.id] = pr.get(s.id, 0.0) + c[k] / d
    else:
        raise ValueError(f"unknown odds model: {model!r}")
    tot = sum(pr.values())
    return {k: v / tot for k, v in pr.items()}

def evaluate_contract(inputs, pool, price_fn, *, odds_model=DEFAULT_ODDS_MODEL,
                      fee_rate=None, stattrak=False, skin_by_id=None):
    """price_fn(skin, wear, stattrak) prices the OUTPUT.

    Never called with a souvenir flag: a trade-up output is always normal
    quality, even when every input was a Souvenir.
    """
    inputs = list(inputs)
    res = ContractResult(inputs=inputs, stattrak=stattrak)
    if not inputs:
        return res
    res.avg_normalized = average_trade_up_float(inputs)
    res.cost = round(sum(i.price for i in inputs), 4)
    res.collection_odds = collection_odds(inputs, pool)
    probs = outcome_probabilities(inputs, pool, odds_model)
    t = inputs[0].skin.rarity.next_tier()
    look: dict[str, Skin] = {}
    for c in {i.skin.collection for i in inputs}:
        for s in pool(c, t):
            look[s.id] = s
    outs: list[Outcome] = []
    for sid, p in probs.items():
        s = look.get(sid) or (skin_by_id(sid) if skin_by_id else None)
        if s is None:
            continue
        f = outcome_float(res.avg_normalized, s)
        w = wear_from_float(f)
        g = price_fn(s, w, stattrak)
        priced = g is not None
        g = g or 0.0
        outs.append(Outcome(s, p, round(f, 8), w, round(g, 4),
                            net_after_fee(g, fee_rate), priced))
    outs.sort(key=lambda o: o.net_price, reverse=True)
    res.outcomes = outs
    res.unpriced_mass = round(sum(o.probability for o in outs if not o.priced), 6)
    res.ev_net = round(sum(o.probability * o.net_price for o in outs), 4)
    res.profit = round(res.ev_net - res.cost, 4)
    res.roi = round(res.ev_net / res.cost, 4) if res.cost > 0 else 0.0
    res.profit_chance = round(
        sum(o.probability for o in outs if o.net_price > res.cost), 6)
    v = sum(o.probability * (o.net_price - res.ev_net) ** 2 for o in outs)
    res.stddev = round(math.sqrt(max(v, 0.0)), 4)
    return res

def n_contracts_for_confidence(res, z: float = 2.0) -> Optional[int]:
    """Identical contracts before the edge outweighs the noise at ~z sigma.

    Assumes the PRICES are correct and only the roll is random -- it cannot
    see a bad price. Use the risk profile for that.
    """
    if res.profit <= 0 or res.stddev <= 0:
        return None
    return math.ceil((z * res.stddev / res.profit) ** 2)
