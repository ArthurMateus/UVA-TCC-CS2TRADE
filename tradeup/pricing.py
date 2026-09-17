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
        if q.volume is not None and q.volume < self.policy.min_input_volume:
            return None
        return getattr(q, self.policy.buy_basis) or q.best
    def sell_price(self, skin, wear, stattrak=False):
        """OUTPUT price -- always normal quality, never Souvenir."""
        q = self.source.get(skin.market_hash_name(wear, stattrak, False))
        if q is None: return None
        if q.volume is not None and q.volume < self.policy.min_volume:
            return None
        v = getattr(q, self.policy.sell_basis) or q.best
        return None if v is None else v * (1.0 - self.policy.haircut)
    def price_fn(self):
        return lambda s, w, st: self.sell_price(s, w, st)

def wear_window(skin, wear):
    b = next((x for x in WEAR_BRACKETS if x[0] == wear), None)
    if b is None: return None
    lo, hi = max(b[1], skin.min_float), min(b[2], skin.max_float)
    return (lo, hi) if hi > lo else None

def sourceable_float(skin, wear, percentile: float = 0.15):
    """The float you can realistically buy inside a wear tier."""
    w = wear_window(skin, wear)
    return None if w is None else w[0] + percentile * (w[1] - w[0])
