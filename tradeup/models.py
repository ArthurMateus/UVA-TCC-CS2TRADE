"""Core domain models for CS2 trade-up contracts."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

WEAR_BRACKETS = [("Factory New",0.00,0.07),("Minimal Wear",0.07,0.15),
                 ("Field-Tested",0.15,0.38),("Well-Worn",0.38,0.45),
                 ("Battle-Scarred",0.45,1.00)]
WEAR_SHORT = {"Factory New":"FN","Minimal Wear":"MW","Field-Tested":"FT",
              "Well-Worn":"WW","Battle-Scarred":"BS"}

def wear_from_float(f: float) -> str:
    for n, lo, hi in WEAR_BRACKETS:
        if lo <= f < hi:
            return n
    return "Battle-Scarred"

class Rarity(IntEnum):
    CONSUMER=0; INDUSTRIAL=1; MILSPEC=2; RESTRICTED=3
    CLASSIFIED=4; COVERT=5; RARE_SPECIAL=6

    @classmethod
    def parse(cls, raw: str) -> "Rarity":
        k = raw.strip().lower().replace("-"," ").replace("_"," ")
        k = k.replace(" grade","").replace(" (rare)","").strip()
        t = {"consumer":cls.CONSUMER,"industrial":cls.INDUSTRIAL,
             "mil spec":cls.MILSPEC,"milspec":cls.MILSPEC,
             "restricted":cls.RESTRICTED,"classified":cls.CLASSIFIED,
             "covert":cls.COVERT,"extraordinary":cls.RARE_SPECIAL,
             "contraband":cls.RARE_SPECIAL,"master":cls.RARE_SPECIAL,
             "rare special":cls.RARE_SPECIAL}
        for key, v in t.items():
            if k.startswith(key):
                return v
        raise ValueError(f"Unknown rarity: {raw!r}")

    @property
    def label(self) -> str:
        return {0:"Consumer",1:"Industrial",2:"Mil-Spec",3:"Restricted",
                4:"Classified",5:"Covert",6:"Rare Special"}[int(self)]

    def next_tier(self) -> Optional["Rarity"]:
        return Rarity(int(self)+1) if int(self) < 6 else None

@dataclass(frozen=True, slots=True)
class Skin:
    """A weapon finish. min_float/max_float are its Float Cap."""
    id: str; name: str; rarity: Rarity; collection: str
    min_float: float; max_float: float
    stattrak_capable: bool = True
    souvenir_capable: bool = False

    def market_hash_name(self, wear, stattrak=False, souvenir=False) -> str:
        # StatTrak and Souvenir are mutually exclusive; StatTrak wins
        p = "StatTrak\u2122 " if stattrak else ("Souvenir " if souvenir else "")
        return f"{p}{self.name} ({wear})"

    def reachable_wears(self) -> list[str]:
        return [n for n, lo, hi in WEAR_BRACKETS
                if self.min_float < hi and self.max_float > lo]

    @property
    def float_span(self) -> float:
        return self.max_float - self.min_float

@dataclass(slots=True)
class InputItem:
    skin: Skin; float_value: float; price: float = 0.0
    stattrak: bool = False; souvenir: bool = False

    @property
    def trade_up_float(self) -> float:
        s = self.skin.float_span
        return 0.0 if s <= 0 else (self.float_value - self.skin.min_float) / s

    @property
    def normalized(self) -> float:
        return self.trade_up_float

@dataclass(slots=True)
class Outcome:
    skin: Skin; probability: float; out_float: float; wear: str
    gross_price: float = 0.0; net_price: float = 0.0; priced: bool = True

@dataclass(slots=True)
class ContractResult:
    inputs: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    avg_normalized: float = 0.0
    collection_odds: dict = field(default_factory=dict)
    cost: float = 0.0; ev_net: float = 0.0; profit: float = 0.0; roi: float = 0.0
    profit_chance: float = 0.0; stddev: float = 0.0
    unpriced_mass: float = 0.0; stattrak: bool = False

    @property
    def n_souvenir_inputs(self) -> int:
        return sum(1 for i in self.inputs if i.souvenir)
