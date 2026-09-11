"""Souvenir trade-up support (Valve rule change, 22 May 2026).

Souvenir items became valid contract inputs; souvenir attributes are stripped
and the output is ALWAYS normal quality. StatTrak still cannot mix with
Souvenir. Most Consumer/Industrial supply is souvenir-bearing, so excluding
it silently emptied the two cheapest tiers.
"""
import json

import pytest

from tradeup.data.loader import SkinDB, derive_qualities, parse_skins_payload
from tradeup.engine import evaluate_contract, validate_contract
from tradeup.models import InputItem, Rarity, Skin
from tradeup.optimizer import build_units
from tradeup.prices.steam import PriceBook
from tradeup.pricing import PriceService, PricingPolicy


def mk(name, coll, rarity, mn=0.0, mx=1.0, sv=True, st=False):
    return Skin(f"{name}|{coll}", name, rarity, coll, mn, mx, st, sv)


def test_market_hash_name_prefixes():
    s = mk("AK-47 | Redline", "C", Rarity.MILSPEC)
    assert s.market_hash_name("Field-Tested") == "AK-47 | Redline (Field-Tested)"
    assert s.market_hash_name("Field-Tested", souvenir=True) == \
        "Souvenir AK-47 | Redline (Field-Tested)"
    assert s.market_hash_name("Field-Tested", stattrak=True) == \
        "StatTrak\u2122 AK-47 | Redline (Field-Tested)"
    # mutually exclusive: StatTrak wins, never "StatTrak Souvenir"
    n = s.market_hash_name("Field-Tested", stattrak=True, souvenir=True)
    assert n.startswith("StatTrak") and "Souvenir" not in n


def test_souvenir_collections_are_not_filtered_out():
    """Regression: the loader used to drop any collection named *Souvenir*."""
    payload = [
        {"name": "MAG-7 | Counter Terrace", "min_float": 0.0, "max_float": 1.0,
         "rarity": {"name": "Consumer Grade"}, "souvenir": True, "stattrak": False,
         "crates": [{"name": "Cologne 2014 Cobblestone Souvenir Package"}],
         "collections": [{"name": "The Souvenir Cobblestone Collection"}]},
        {"name": "Five-SeveN | Kami", "min_float": 0.0, "max_float": 0.6,
         "rarity": {"name": "Industrial Grade"}, "souvenir": True, "stattrak": False,
         "crates": [{"name": "Cologne 2014 Cobblestone Souvenir Package"}],
         "collections": [{"name": "The Souvenir Cobblestone Collection"}]},
    ]
    db = parse_skins_payload(payload)
    assert len(db) == 2
    assert db.tradeable_collections(Rarity.CONSUMER) == \
        ["The Souvenir Cobblestone Collection"]
    assert db.skins[0].souvenir_capable is True


def test_souvenir_inputs_do_not_change_odds_float_or_output_price():
    """A souvenir input is identical to a normal one except for what it cost."""
    out = mk("Out", "A", Rarity.INDUSTRIAL, 0.0, 0.5, sv=False)
    inp = mk("In", "A", Rarity.CONSUMER, 0.0, 1.0)
    pool = lambda c, r: [out] if (c, int(r)) == ("A", int(Rarity.INDUSTRIAL)) else []

    normal = [InputItem(inp, 0.05, 1.0, souvenir=False) for _ in range(10)]
    souv = [InputItem(inp, 0.05, 0.5, souvenir=True) for _ in range(10)]

    seen = []

    def price_fn(skin, wear, st):
        seen.append((skin.name, wear, st))
        return 20.0

    a = evaluate_contract(normal, pool, price_fn)
    b = evaluate_contract(souv, pool, price_fn)

    assert a.avg_normalized == b.avg_normalized
    assert [o.probability for o in a.outcomes] == [o.probability for o in b.outcomes]
    assert a.ev_net == b.ev_net          # output priced identically
    assert b.cost < a.cost               # only the input cost differs
    assert b.profit > a.profit
    assert b.n_souvenir_inputs == 10
    assert all(st is False for _, _, st in seen)


def test_output_is_never_priced_as_souvenir_even_if_all_inputs_are():
    """Trade-up output is always NORMAL quality (Valve, 22 May 2026)."""
    from tradeup.prices.steam import Quote

    out = mk("Out", "A", Rarity.INDUSTRIAL, 0.0, 0.5, sv=True)
    inp = mk("In", "A", Rarity.CONSUMER, 0.0, 1.0, sv=True)
    pool = lambda c, r: [out] if (c, int(r)) == ("A", int(Rarity.INDUSTRIAL)) else []

    asked: list[str] = []

    class Src:
        def get(self, name):
            asked.append(name)
            return Quote(name, 9.0, 10.0, 100, 0.0)

    ps = PriceService(Src(), PricingPolicy(min_volume=1, haircut=0.0))
    items = [InputItem(inp, 0.05, 0.1, souvenir=True) for _ in range(10)]
    evaluate_contract(items, pool, ps.price_fn())

    assert asked, "price_fn was never called"
    assert all(not n.startswith("Souvenir ") for n in asked), \
        f"output was priced as a Souvenir: {asked}"


def test_mixing_souvenir_and_normal_is_legal_but_stattrak_is_not():
    s = mk("A", "C", Rarity.CONSUMER)
    st_skin = mk("B", "C", Rarity.CONSUMER, sv=False, st=True)
    mixed = [InputItem(s, 0.1, souvenir=i % 2 == 0) for i in range(10)]
    assert validate_contract(mixed) == []

    bad = mixed[:9] + [InputItem(st_skin, 0.1, stattrak=True)]
    assert any("StatTrak" in p for p in validate_contract(bad))

    wrong = [InputItem(s, 0.1, souvenir=True),
             InputItem(mk("C", "C", Rarity.MILSPEC), 0.1)]
    assert any("multiple rarities" in p for p in validate_contract(wrong))

    no_sv = mk("D", "C", Rarity.CONSUMER, sv=False)
    assert any("no Souvenir variant" in p
               for p in validate_contract([InputItem(no_sv, 0.1, souvenir=True)]))


def test_build_units_adds_souvenir_variants(tmp_path):
    skin = mk("MAG-7 | Counter Terrace", "Coll", Rarity.CONSUMER, 0.0, 1.0)
    out = mk("Out", "Coll", Rarity.INDUSTRIAL, 0.0, 0.5, sv=False)

    class DB:
        def tradeable_collections(self, r): return ["Coll"]
        def pool(self, c, r): return [skin] if r == Rarity.CONSUMER else [out]
        def by_id(self, i): return None

    snap = tmp_path / "p.json"
    snap.write_text(json.dumps({
        "MAG-7 | Counter Terrace (Field-Tested)":
            {"lowest": 1.00, "median": 1.10, "volume": 500},
        "Souvenir MAG-7 | Counter Terrace (Field-Tested)":
            {"lowest": 0.40, "median": 0.45, "volume": 300},
    }), encoding="utf-8")
    ps = PriceService(PriceBook.load(snap), PricingPolicy(min_input_volume=10))

    off = build_units(DB(), ps, Rarity.CONSUMER, souvenir=False)
    on = build_units(DB(), ps, Rarity.CONSUMER, souvenir=True)
    assert len(on["Coll"]) == len(off["Coll"]) + 1
    cheapest = min(on["Coll"], key=lambda u: u.price)
    assert cheapest.souvenir is True and cheapest.price == 0.40
    assert cheapest.label.startswith("Souvenir ")

    # StatTrak contracts must never pull in souvenir units
    assert build_units(DB(), ps, Rarity.CONSUMER, souvenir=True, stattrak=True) == {}


def test_illiquid_inputs_are_rejected(tmp_path):
    """You need TEN copies; a one-off souvenir listing is not a real price."""
    skin = mk("Rare Souvenir Thing", "Coll", Rarity.CONSUMER, 0.0, 1.0)

    class DB:
        def tradeable_collections(self, r): return ["Coll"]
        def pool(self, c, r):
            return [skin] if r == Rarity.CONSUMER else [
                mk("Out", "Coll", Rarity.INDUSTRIAL, 0.0, 0.5, sv=False)]
        def by_id(self, i): return None

    snap = tmp_path / "p.json"
    snap.write_text(json.dumps({
        "Souvenir Rare Souvenir Thing (Field-Tested)":
            {"lowest": 0.05, "median": 0.05, "volume": 2},
    }), encoding="utf-8")
    ps = PriceService(PriceBook.load(snap), PricingPolicy(min_input_volume=10))
    assert build_units(DB(), ps, Rarity.CONSUMER, souvenir=True) == {}


# --- quality derivation (crate-based, not the blanket flag) -----------
def test_souvenir_capability_comes_from_crates_not_the_flag():
    """Regression: upstream sets `souvenir: true` on ~every finish."""
    # case-distributed finish: StatTrak yes, Souvenir no -- even though the
    # upstream payload claims souvenir=True
    assert derive_qualities({
        "stattrak": True, "souvenir": True,
        "crates": [{"name": "Fracture Case"}]}) == (True, False)

    # tournament package finish: Souvenir yes, StatTrak no
    assert derive_qualities({
        "stattrak": True, "souvenir": True,
        "crates": [{"name": "Cologne 2014 Cobblestone Souvenir Package"}]}) \
        == (False, True)

    # appears in both a case and a souvenir package -> both allowed
    assert derive_qualities({"crates": [
        {"name": "Fracture Case"},
        {"name": "Berlin 2019 Dust II Souvenir Package"}]}) == (True, True)

    # no crate data -> fall back to flags, but never claim both at once
    assert derive_qualities(
        {"stattrak": True, "souvenir": True, "crates": []}) == (True, False)
    assert derive_qualities({"stattrak": False, "souvenir": True}) == (False, True)


def test_parse_payload_does_not_mark_everything_souvenir():
    payload = [
        {"name": f"Case Gun {i}", "min_float": 0.0, "max_float": 1.0,
         "rarity": {"name": "Consumer Grade"}, "stattrak": True, "souvenir": True,
         "crates": [{"name": "Fracture Case"}],
         "collections": [{"name": "The Fracture Collection"}]}
        for i in range(8)
    ] + [
        {"name": "Souvenir Gun", "min_float": 0.0, "max_float": 1.0,
         "rarity": {"name": "Consumer Grade"}, "stattrak": True, "souvenir": True,
         "crates": [{"name": "Cologne 2014 Cobblestone Souvenir Package"}],
         "collections": [{"name": "The Cobblestone Collection"}]}
    ]
    db = parse_skins_payload(payload)
    sv = [s for s in db.skins if s.souvenir_capable]
    assert len(sv) == 1 and sv[0].name == "Souvenir Gun"
    assert db.quality_sanity() == []   # plausible ratio -> no warning


def test_quality_sanity_flags_an_all_souvenir_database():
    db = SkinDB([
        Skin(f"S{i}|C", f"S{i}", Rarity.CONSUMER, "C", 0.0, 1.0, True, True)
        for i in range(20)
    ])
    warnings = db.quality_sanity()
    assert any("Souvenir-capable" in w for w in warnings)
    assert any("mutually exclusive" in w for w in warnings)
