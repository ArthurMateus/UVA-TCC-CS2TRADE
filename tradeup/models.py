"""Core domain models for CS2 trade-up contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

# Global wear brackets. A skin's own Float Cap decides which of these it can
# actually reach -- a 0.10-0.70 skin can never be Factory New.
WEAR_BRACKETS: list[tuple[str, float, float]] = [
    ("Factory New", 0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn", 0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]

WEAR_SHORT = {
    "Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT",
    "Well-Worn": "WW", "Battle-Scarred": "BS",
}


def wear_from_float(f: float) -> str:
    """Map an absolute float value to its wear condition."""
    for name, lo, hi in WEAR_BRACKETS:
        if lo <= f < hi:
            return name
    return "Battle-Scarred"


class Rarity(IntEnum):
    CONSUMER = 0
    INDUSTRIAL = 1
    MILSPEC = 2
    RESTRICTED = 3
    CLASSIFIED = 4
    COVERT = 5
    RARE_SPECIAL = 6  # knives / gloves

    @classmethod
    def parse(cls, raw: str) -> "Rarity":
        key = raw.strip().lower().replace("-", " ").replace("_", " ")
        key = key.replace(" grade", "").replace(" (rare)", "").strip()
        table = {
            "consumer": cls.CONSUMER, "industrial": cls.INDUSTRIAL,
            "mil spec": cls.MILSPEC, "milspec": cls.MILSPEC,
            "restricted": cls.RESTRICTED, "classified": cls.CLASSIFIED,
            "covert": cls.COVERT, "extraordinary": cls.RARE_SPECIAL,
            "contraband": cls.RARE_SPECIAL, "master": cls.RARE_SPECIAL,
            "rare special": cls.RARE_SPECIAL,
        }
        for k, v in table.items():
            if key.startswith(k):
                return v
        raise ValueError(f"Unknown rarity: {raw!r}")

    @property
    def label(self) -> str:
        return {0: "Consumer", 1: "Industrial", 2: "Mil-Spec", 3: "Restricted",
                4: "Classified", 5: "Covert", 6: "Rare Special"}[int(self)]

    def next_tier(self) -> Optional["Rarity"]:
        return Rarity(int(self) + 1) if int(self) < 6 else None


@dataclass(frozen=True, slots=True)
class Skin:
    """A weapon finish (paint kit), independent of wear and quality.

    `min_float`/`max_float` are the skin's Float Cap: the range it can ever
    be generated within. Not every skin spans 0.00-1.00, and the cap decides
    both which wear conditions exist and how the skin behaves in trade-ups.
    """

    id: str
    name: str            # e.g. "AK-47 | Redline"
    rarity: Rarity
    collection: str
    min_float: float     # Best Possible Float
    max_float: float     # Worst Possible Float
    stattrak_capable: bool = True
    souvenir_capable: bool = False

    def market_hash_name(
        self, wear: str, stattrak: bool = False, souvenir: bool = False
    ) -> str:
        """Steam market name for a specific quality + wear.

        StatTrak and Souvenir are mutually exclusive; StatTrak wins if both
        are somehow requested.
        """
        if stattrak:
            prefix = "StatTrak\u2122 "
        elif souvenir:
            prefix = "Souvenir "
        else:
            prefix = ""
        return f"{prefix}{self.name} ({wear})"

    def reachable_wears(self) -> list[str]:
        """Wear conditions this skin can physically exist in, given its cap."""
        return [n for n, lo, hi in WEAR_BRACKETS
                if self.min_float < hi and self.max_float > lo]

    @property
    def float_span(self) -> float:
        return self.max_float - self.min_float

    def clamp(self, f: float) -> float:
        return min(max(f, self.min_float), self.max_float)


@dataclass(slots=True)
class InputItem:
    """One of the n items fed into a contract.

    Since 22 May 2026 a contract may mix Souvenir and normal inputs freely.
    Souvenir attributes are stripped and the OUTPUT is always normal, so
    `souvenir` only affects what this input costs to buy.
    """

    skin: Skin
    float_value: float          # the raw Wear Rating on the item
    price: float = 0.0
    stattrak: bool = False
    souvenir: bool = False

    @property
    def trade_up_float(self) -> float:
        """This item's float rescaled onto the universal 0-1 axis.

        (float - min) / (max - min), using the skin's OWN Float Cap.
        """
        span = self.skin.float_span
        if span <= 0:
            return 0.0
        return (self.float_value - self.skin.min_float) / span

    # legacy alias
    @property
    def normalized(self) -> float:
        return self.trade_up_float

    @property
    def wear(self) -> str:
        return wear_from_float(self.float_value)


@dataclass(slots=True)
class Outcome:
    skin: Skin
    probability: float
    out_float: float            # Outcome Float, in the output skin's own cap
    wear: str
    gross_price: float = 0.0
    net_price: float = 0.0
    priced: bool = True


@dataclass(slots=True)
class ContractResult:
    inputs: list[InputItem] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    avg_normalized: float = 0.0          # Average Trade-up Float
    collection_odds: dict[str, float] = field(default_factory=dict)
    cost: float = 0.0
    ev_net: float = 0.0
    profit: float = 0.0
    roi: float = 0.0
    profit_chance: float = 0.0
    stddev: float = 0.0
    unpriced_mass: float = 0.0
    stattrak: bool = False

    @property
    def avg_trade_up_float(self) -> float:
        return self.avg_normalized

    @property
    def n_inputs(self) -> int:
        return len(self.inputs)

    @property
    def n_souvenir_inputs(self) -> int:
        return sum(1 for i in self.inputs if i.souvenir)
