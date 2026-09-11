"""Glue between a price source and the trade-up engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .models import WEAR_BRACKETS, Skin
from .prices.steam import Quote


class PriceSource(Protocol):
    """Anything that can answer 'what is this market_hash_name worth?'.

    The engine never talks to Steam directly, so swapping in another
    marketplace is a matter of implementing this one method.
    """

    def get(self, market_hash_name: str) -> Optional[Quote]: ...


@dataclass(slots=True)
class PricingPolicy:
    """How raw market quotes become the numbers used in the EV model.

    min_volume:
        Ignore OUTPUT quotes thinner than this. An "EV-positive" trade-up
        whose jackpot skin sells twice a week is not a real edge -- you will
        sit on inventory or dump below median.
    min_input_volume:
        Same idea for INPUTS, but stricter: you need to buy ten copies.
        Souvenir listings in particular are often near-unique, so a R$0.40
        quote backed by two sales is not a price you can act on.
    buy_basis / sell_basis:
        'lowest' is what you pay to acquire an input right now.
        'median' is what you can realistically get out for. Using `lowest`
        on both sides is the most common way these tools lie to you.
    haircut:
        Extra safety margin applied to output prices (0.05 = assume you sell
        5% under the reference price).

    NOTE: after `bulk-warm`, quotes carry `lowest` + a LISTING COUNT in the
    volume column and no median. `sell_price` then falls back to `best`.
    Confirm a shortlist with `warm-prices` before committing money.
    """

    min_volume: int = 3
    min_input_volume: int = 10
    buy_basis: str = "lowest"
    sell_basis: str = "median"
    haircut: float = 0.03


class PriceService:
    def __init__(self, source: PriceSource, policy: PricingPolicy | None = None):
        self.source = source
        self.policy = policy or PricingPolicy()

    def _quote(self, name: str) -> Optional[Quote]:
        return self.source.get(name)

    def buy_price(
        self, skin: Skin, wear: str, stattrak: bool = False, souvenir: bool = False
    ) -> Optional[float]:
        """Cost to acquire one copy. `souvenir=True` prices the Souvenir variant."""
        q = self._quote(skin.market_hash_name(wear, stattrak, souvenir))
        if q is None:
            return None
        if q.volume is not None and q.volume < self.policy.min_input_volume:
            return None  # cannot reliably buy ten of these
        return getattr(q, self.policy.buy_basis) or q.best

    def sell_price(
        self, skin: Skin, wear: str, stattrak: bool = False
    ) -> Optional[float]:
        """Proceeds for the OUTPUT, which is always normal quality."""
        q = self._quote(skin.market_hash_name(wear, stattrak, souvenir=False))
        if q is None:
            return None
        if q.volume is not None and q.volume < self.policy.min_volume:
            return None
        v = getattr(q, self.policy.sell_basis) or q.best
        return None if v is None else v * (1.0 - self.policy.haircut)

    # engine-compatible callback
    def price_fn(self):
        return lambda skin, wear, st: self.sell_price(skin, wear, st)


def wear_window(skin: Skin, wear: str) -> Optional[tuple[float, float]]:
    """Intersection of a wear bracket with the skin's own float range."""
    b = next((x for x in WEAR_BRACKETS if x[0] == wear), None)
    if b is None:
        return None
    lo, hi = max(b[1], skin.min_float), min(b[2], skin.max_float)
    return (lo, hi) if hi > lo else None


def sourceable_float(skin: Skin, wear: str, percentile: float = 0.15) -> Optional[float]:
    """The float you can realistically buy inside a wear tier.

    `percentile=0.15` means "with some listing hunting I can find an item in
    the bottom 15% of this wear bracket". Set it to 0.0 only if you are
    actually willing to pay float-sniper premiums, and 0.5 for a lazy
    market-order assumption.
    """
    w = wear_window(skin, wear)
    if w is None:
        return None
    lo, hi = w
    return lo + percentile * (hi - lo)
