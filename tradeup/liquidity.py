"""Liquidity modelling -- the difference between a listed price and a sale.

A trade-up EV built on `lowest_price` assumes you can sell instantly at the
current cheapest listing. For a popular Field-Tested rifle that is roughly
true. For an unpopular skin in Well-Worn it is fiction: there may be three
listings, no buyer has touched it in a week, and the "price" is whatever one
optimistic seller typed in.

Two distinct problems, handled separately:

1. REALIZATION -- what fraction of the listed price you actually get.
   To sell into a thin book you must undercut the current lowest, and with
   almost no sales history there is no evidence anyone pays that number.

2. CONCENTRATION -- how much of the EV rides on a single outcome.
   A contract where 99% of expected value comes from one illiquid skin is
   not an edge with variance; it is a bet that one questionable price is
   real. `n*` does not capture this, because `n*` assumes the prices are
   correct and only the roll is random.

Both are reported explicitly rather than silently folded into one number,
so you can see WHY a recipe was downgraded.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Sequence


class LiquidityTier(IntEnum):
    DEAD = 0        # essentially unsellable at the listed price
    THIN = 1        # sellable, but expect to undercut and wait
    OK = 2          # normal retail depth
    DEEP = 3        # sell instantly at market

    @property
    def label(self) -> str:
        return {0: "dead", 1: "thin", 2: "ok", 3: "deep"}[int(self)]


@dataclass(frozen=True, slots=True)
class LiquidityPolicy:
    """How listing depth and sales volume translate into a haircut.

    `listings` is SELL LISTINGS (supply) -- what bulk-warm gives you.
    `volume` is 24h SALES -- what warm-prices gives you, and far stronger
    evidence. When both are present, volume wins.

    Realization factors are deliberately blunt. They are not calibrated
    against measured fill data; they encode the ordering "fewer listings and
    no sales means you get less", which is the part that is certain. Treat
    a liquidity-adjusted EV as a ranking signal, not a forecast.
    """

    # sell-listing thresholds (supply-based, weaker evidence)
    dead_listings: int = 3
    thin_listings: int = 15
    ok_listings: int = 60

    # 24h sales thresholds (much stronger evidence)
    dead_volume: int = 1
    thin_volume: int = 5
    ok_volume: int = 25

    # what fraction of the listed price you realistically clear
    realization: tuple[float, float, float, float] = (0.55, 0.78, 0.93, 0.98)

    # inputs: you must BUY ten copies, so supply matters directly
    min_listings_to_buy_ten: int = 12

    # concentration guard
    max_ev_share: float = 0.85       # one outcome may not exceed this share of EV
    concentration_exempt_tier: LiquidityTier = LiquidityTier.OK

    def tier(self, listings: Optional[int], volume: Optional[int] = None
             ) -> LiquidityTier:
        """Classify an item. `volume` (24h sales) overrides `listings`."""
        if volume is not None:
            if volume < self.dead_volume:
                return LiquidityTier.DEAD
            if volume < self.thin_volume:
                return LiquidityTier.THIN
            if volume < self.ok_volume:
                return LiquidityTier.OK
            return LiquidityTier.DEEP
        if listings is None:
            # no information at all: assume the worst, do not assume the best
            return LiquidityTier.DEAD
        if listings < self.dead_listings:
            return LiquidityTier.DEAD
        if listings < self.thin_listings:
            return LiquidityTier.THIN
        if listings < self.ok_listings:
            return LiquidityTier.OK
        return LiquidityTier.DEEP

    def realization_factor(self, listings: Optional[int],
                           volume: Optional[int] = None) -> float:
        """Fraction of the listed price you should expect to clear."""
        return self.realization[int(self.tier(listings, volume))]

    def can_buy_ten(self, listings: Optional[int],
                    volume: Optional[int] = None, n: int = 10) -> bool:
        """Is there enough supply to actually assemble the contract?

        With sales volume we can be a bit more generous: an item that sells
        20 a day restocks even if only 6 are listed right now.
        """
        if volume is not None and volume >= n * 2:
            return True
        if listings is None:
            return False
        return listings >= max(self.min_listings_to_buy_ten, n)


@dataclass(slots=True)
class LiquidityReport:
    """Why a recipe was or was not downgraded."""

    ev_raw: float = 0.0              # EV using listed prices
    ev_adjusted: float = 0.0         # EV after realization haircuts
    profit_adjusted: float = 0.0
    roi_adjusted: float = 0.0
    top_ev_share: float = 0.0        # largest single-outcome share of EV
    top_outcome: str = ""
    top_outcome_tier: LiquidityTier = LiquidityTier.DEAD
    worst_tier: LiquidityTier = LiquidityTier.DEEP
    concentrated: bool = False       # EV depends on one illiquid outcome
    unbuyable_inputs: list[str] = None

    def __post_init__(self) -> None:
        if self.unbuyable_inputs is None:
            self.unbuyable_inputs = []

    @property
    def haircut_pct(self) -> float:
        if self.ev_raw <= 0:
            return 0.0
        return (1.0 - self.ev_adjusted / self.ev_raw) * 100.0

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.concentrated:
            out.append(
                f"{self.top_ev_share*100:.0f}% of EV comes from "
                f"{self.top_outcome} ({self.top_outcome_tier.label}). "
                "The recipe is a bet on that one price being real.")
        if self.worst_tier <= LiquidityTier.DEAD:
            out.append("at least one outcome has effectively no market")
        if self.unbuyable_inputs:
            out.append("not enough listings to buy 10 of: "
                       + ", ".join(self.unbuyable_inputs[:3]))
        return out


def assess(
    outcomes: Sequence,          # tradeup.models.Outcome
    policy: LiquidityPolicy,
    depth: dict[str, tuple[Optional[int], Optional[int]]],
    cost: float,
) -> LiquidityReport:
    """Re-price a contract's outcomes for liquidity.

    `depth` maps market_hash_name -> (sell_listings, 24h_volume). Either may
    be None; volume is preferred when present.
    """
    rep = LiquidityReport()
    if not outcomes:
        return rep

    rep.ev_raw = sum(o.probability * o.net_price for o in outcomes)

    contribs: list[tuple[float, object, LiquidityTier]] = []
    ev_adj = 0.0
    worst = LiquidityTier.DEEP
    for o in outcomes:
        name = o.skin.market_hash_name(o.wear)
        listings, volume = depth.get(name, (None, None))
        tier = policy.tier(listings, volume)
        worst = min(worst, tier)
        adj = o.net_price * policy.realization_factor(listings, volume)
        c = o.probability * adj
        ev_adj += c
        contribs.append((c, o, tier))

    rep.ev_adjusted = ev_adj
    rep.profit_adjusted = ev_adj - cost
    rep.roi_adjusted = (ev_adj / cost) if cost > 0 else 0.0
    rep.worst_tier = worst

    if ev_adj > 0:
        c, o, tier = max(contribs, key=lambda x: x[0])
        rep.top_ev_share = c / ev_adj
        rep.top_outcome = o.skin.market_hash_name(o.wear)
        rep.top_outcome_tier = tier
        rep.concentrated = (
            rep.top_ev_share > policy.max_ev_share
            and tier < policy.concentration_exempt_tier)
    return rep


def breakeven_realization(outcomes: Sequence, cost: float,
                          target: str) -> Optional[float]:
    """What fraction of `target`'s listed price makes the contract break even.

    Answers the only question that matters for a concentrated recipe: how
    wrong can that one price be before this loses money?
    """
    other = sum(o.probability * o.net_price
                for o in outcomes if o.skin.market_hash_name(o.wear) != target)
    hit = [o for o in outcomes if o.skin.market_hash_name(o.wear) == target]
    if not hit:
        return None
    p, net = hit[0].probability, hit[0].net_price
    if p <= 0 or net <= 0:
        return None
    return (cost - other) / (p * net)
