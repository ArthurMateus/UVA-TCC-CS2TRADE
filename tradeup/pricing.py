"""Turns raw quotes into the numbers the EV model uses."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol
from .models import WEAR_BRACKETS, Skin
from .prices.steam import Quote

class PriceSource(Protocol):
    def get(self, market_hash_name: str) -> Optional[Quote]: ...

@dataclass(slots=True)
class PricingPolicy:
    """min_input_volume is stricter than min_volume on purpose: you must buy
    TEN copies of an input, but only sell ONE output."""
    min_volume: int = 3
    min_input_volume: int = 10
    buy_basis: str = "lowest"     # what you pay right now
    sell_basis: str = "median"    # what you can realistically get out for
    haircut: float = 0.03

class PriceService:
    def __init__(self, source: PriceSource, policy: PricingPolicy | None = None):
        self.source = source
        self.policy = policy or PricingPolicy()
    def buy_price(self, skin, wear, stattrak=False, souvenir=False):
        q = self.source.get(skin.market_hash_name(wear, stattrak, souvenir))
        if q is None: return None
        liq = _liquidity(q)
        if liq is not None and liq < self.policy.min_input_volume:
            return None
        return getattr(q, self.policy.buy_basis) or q.best
    def sell_price(self, skin, wear, stattrak=False):
        """OUTPUT price -- always normal quality, never Souvenir.

        Buyer-facing price after the policy haircut, NOT net of fees:
        convert with `engine.net_after_fee` before comparing with a cost."""
        q = self.source.get(skin.market_hash_name(wear, stattrak, False))
        if q is None: return None
        liq = _liquidity(q)
        if liq is not None and liq < self.policy.min_volume:
            return None
        v = getattr(q, self.policy.sell_basis) or q.best
        return None if v is None else v * (1.0 - self.policy.haircut)
    def price_fn(self):
        return lambda s, w, st: self.sell_price(s, w, st)

def _liquidity(q) -> Optional[int]:
    """24h sales when known, otherwise the listing-count proxy from bulk."""
    v = getattr(q, "volume", None)
    return v if v is not None else getattr(q, "listings", None)

def wear_window(skin, wear):
    b = next((x for x in WEAR_BRACKETS if x[0] == wear), None)
    if b is None: return None
    lo, hi = max(b[1], skin.min_float), min(b[2], skin.max_float)
    return (lo, hi) if hi > lo else None

def sourceable_float(skin, wear, percentile: float = 0.15):
    """The float you can realistically buy inside a wear tier."""
    w = wear_window(skin, wear)
    if w is None:
        return None
    p = min(max(percentile, 0.0), 1.0)
    # the wear's upper bound is exclusive: never return it, or the float
    # would fall into the NEXT wear while being priced as this one
    return min(w[0] + p * (w[1] - w[0]), w[1] - 1e-5)   # survives 5-decimal rounding
