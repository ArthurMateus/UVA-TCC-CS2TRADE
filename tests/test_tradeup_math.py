"""Trade-up math validated against known-good worked examples.

Every expected number here comes from an independently verified reference
calculation, so a regression in the float or odds model fails loudly.
"""
from __future__ import annotations

import pytest

from tradeup.engine import (
    average_trade_up_float,
    collection_odds,
    outcome_float,
    outcome_probabilities,
    trade_up_float,
    validate_contract,
)
from tradeup.models import InputItem, Rarity, Skin, wear_from_float


def mk(name, coll, rarity, mn, mx):
    return Skin(f"{name}|{coll}", name, rarity, coll, mn, mx)


def pool_of(mapping):
    return lambda c, r: mapping.get((c, int(r)), [])


# --- Float Caps and wear ---------------------------------------------
@pytest.mark.parametrize("name,mn,mx,expected", [
    ("AK-47 | Redline",      0.10, 0.70, ["Minimal Wear", "Field-Tested",
                                          "Well-Worn", "Battle-Scarred"]),
    ("MAC-10 | Sakkaku",     0.21, 0.79, ["Field-Tested", "Well-Worn",
                                          "Battle-Scarred"]),
    ("M4A1-S | Printstream", 0.00, 1.00, ["Factory New", "Minimal Wear",
                                          "Field-Tested", "Well-Worn",
                                          "Battle-Scarred"]),
    ("Desert Eagle | Blaze", 0.00, 0.08, ["Factory New", "Minimal Wear"]),
])
def test_float_cap_determines_available_conditions(name, mn, mx, expected):
    assert mk(name, "C", Rarity.MILSPEC, mn, mx).reachable_wears() == expected


def test_wear_brackets():
    assert wear_from_float(0.00) == "Factory New"
    assert wear_from_float(0.0699) == "Factory New"
    assert wear_from_float(0.07) == "Minimal Wear"
    assert wear_from_float(0.15) == "Field-Tested"
    assert wear_from_float(0.184218690) == "Field-Tested"   # screenshot 1
    assert wear_from_float(0.38) == "Well-Worn"
    assert wear_from_float(0.45) == "Battle-Scarred"
    assert wear_from_float(1.00) == "Battle-Scarred"


# --- Trade-up Float (Adjusted Float) ---------------------------------
def test_mac10_sakkaku_worked_example():
    """MAC-10 | Sakkaku, float 0.22798392176628113, cap 0.21-0.79."""
    v = trade_up_float(0.22798392176628113, 0.21, 0.79)
    assert v == pytest.approx(0.031007, abs=5e-6)


def test_p90_trigon_worked_example():
    """P90 | Trigon, float 0.749, cap 0.08-0.75 -> 0.99850746."""
    assert trade_up_float(0.749, 0.08, 0.75) == pytest.approx(0.99850746, abs=1e-8)


def test_same_raw_float_different_cap_gives_different_contribution():
    """The whole point of adjusting: raw float alone is not comparable."""
    wide = InputItem(mk("Wide", "C", Rarity.MILSPEC, 0.00, 1.00), 0.10)
    half = InputItem(mk("Half", "C", Rarity.MILSPEC, 0.00, 0.50), 0.10)
    assert wide.trade_up_float == pytest.approx(0.10)
    assert half.trade_up_float == pytest.approx(0.20)


def test_average_uses_adjusted_floats_not_raw_floats():
    """10x P90 Trigon: average trade-up float 0.99850746, NOT 0.749."""
    p90 = mk("P90 | Trigon", "Phoenix", Rarity.MILSPEC, 0.08, 0.75)
    inputs = [InputItem(p90, 0.749) for _ in range(10)]
    avg = average_trade_up_float(inputs)
    assert avg == pytest.approx(0.99850746, abs=1e-8)
    assert avg != pytest.approx(0.749, abs=1e-3)   # the outdated model


# --- Outcome Float ----------------------------------------------------
def test_outcome_float_differs_per_outcome_cap():
    """One contract, two outcomes, two different caps -> two floats.

    10x P90 | Trigon at 0.749 (cap 0.08-0.75) into the Phoenix collection.
    """
    avg = trade_up_float(0.749, 0.08, 0.75)   # full precision, not truncated
    awp = mk("AWP | Asiimov", "Phoenix", Rarity.RESTRICTED, 0.18, 1.00)
    aug = mk("AUG | Chameleon", "Phoenix", Rarity.RESTRICTED, 0.00, 0.50)
    assert outcome_float(avg, awp) == pytest.approx(0.998776119403, abs=1e-9)
    assert outcome_float(avg, aug) == pytest.approx(0.499253731343, abs=1e-9)
    assert wear_from_float(outcome_float(avg, awp)) == "Battle-Scarred"
    assert wear_from_float(outcome_float(avg, aug)) == "Battle-Scarred"


def test_outcome_float_respects_a_narrow_output_cap():
    """A 0.00-0.08 output can only ever be FN or MW, whatever you feed it."""
    blaze = mk("Desert Eagle | Blaze", "C", Rarity.CLASSIFIED, 0.00, 0.08)
    assert outcome_float(1.0, blaze) == pytest.approx(0.08)
    assert wear_from_float(outcome_float(1.0, blaze)) == "Minimal Wear"
    assert wear_from_float(outcome_float(0.0, blaze)) == "Factory New"


# --- Outcome Odds: per-item, post-Retakes -----------------------------
def test_collection_odds_are_proportional_to_input_count():
    """7 of 10 inputs from one collection -> 70% that collection."""
    c1 = mk("In1", "C1", Rarity.MILSPEC, 0, 1)
    c2 = mk("In2", "C2", Rarity.MILSPEC, 0, 1)
    outs = {("C1", int(Rarity.RESTRICTED)): [
                mk(f"A{i}", "C1", Rarity.RESTRICTED, 0, 1) for i in range(3)],
            ("C2", int(Rarity.RESTRICTED)): [
                mk(f"B{i}", "C2", Rarity.RESTRICTED, 0, 1) for i in range(3)]}
    items = [InputItem(c1, 0.1)] * 7 + [InputItem(c2, 0.1)] * 3
    odds = collection_odds(items, pool_of(outs))
    assert odds["C1"] == pytest.approx(0.70)
    assert odds["C2"] == pytest.approx(0.30)


def test_seven_inputs_three_outcomes_splits_evenly():
    """70% collection / 3 outcomes = 23.33% per skin."""
    c1 = mk("In1", "C1", Rarity.MILSPEC, 0, 1)
    c2 = mk("In2", "C2", Rarity.MILSPEC, 0, 1)
    outs = {("C1", int(Rarity.RESTRICTED)): [
                mk(f"A{i}", "C1", Rarity.RESTRICTED, 0, 1) for i in range(3)],
            ("C2", int(Rarity.RESTRICTED)): [
                mk(f"B{i}", "C2", Rarity.RESTRICTED, 0, 1) for i in range(3)]}
    items = [InputItem(c1, 0.1)] * 7 + [InputItem(c2, 0.1)] * 3
    p = outcome_probabilities(items, pool_of(outs))
    for i in range(3):
        assert p[f"A{i}|C1"] == pytest.approx(0.70 / 3)
        assert p[f"B{i}|C2"] == pytest.approx(0.30 / 3)
    assert sum(p.values()) == pytest.approx(1.0)


def test_single_input_single_outcome_collection_is_flat_ten_percent():
    """The Dragon Lore case: 1 of 10 inputs, 1 outcome -> exactly 10%.

    A collection offering two outcomes yields 5% each for the same single
    input. Under the legacy model both would be 5.26%.
    """
    knight = mk("M4A1-S | Knight", "Cobblestone", Rarity.CLASSIFIED, 0, 1)
    outs = {("Cobblestone", int(Rarity.COVERT)): [
        mk("AWP | Dragon Lore", "Cobblestone", Rarity.COVERT, 0, 1)]}
    items = [InputItem(knight, 0.1)]
    for i in range(9):
        c = mk(f"Filler{i}", f"C{i}", Rarity.CLASSIFIED, 0, 1)
        outs[(f"C{i}", int(Rarity.COVERT))] = [
            mk(f"X{i}", f"C{i}", Rarity.COVERT, 0, 1),
            mk(f"Y{i}", f"C{i}", Rarity.COVERT, 0, 1)]
        items.append(InputItem(c, 0.1))

    p = outcome_probabilities(items, pool_of(outs), "per_input")
    assert p["AWP | Dragon Lore|Cobblestone"] == pytest.approx(0.10)
    assert p["X0|C0"] == pytest.approx(0.05)
    assert sum(p.values()) == pytest.approx(1.0)

    legacy = outcome_probabilities(items, pool_of(outs), "outcome_weighted")
    assert legacy["AWP | Dragon Lore|Cobblestone"] == pytest.approx(0.0526, abs=1e-4)


def test_per_input_is_the_default_model():
    from tradeup.engine import DEFAULT_ODDS_MODEL
    assert DEFAULT_ODDS_MODEL == "per_input"


def test_dead_end_collection_contributes_no_outcome_mass():
    outs = {("A", int(Rarity.RESTRICTED)): [
        mk("A1", "A", Rarity.RESTRICTED, 0, 1)]}
    items = ([InputItem(mk("IA", "A", Rarity.MILSPEC, 0, 1), 0.1)] * 2
             + [InputItem(mk("IB", "B", Rarity.MILSPEC, 0, 1), 0.1)] * 8)
    assert outcome_probabilities(items, pool_of(outs)) == \
        {"A1|A": pytest.approx(1.0)}


# --- legality ---------------------------------------------------------
def test_float_outside_the_cap_is_rejected():
    ak = mk("AK-47 | Redline", "C", Rarity.CLASSIFIED, 0.10, 0.70)
    ok = validate_contract([InputItem(ak, 0.15)])
    assert not any("outside its cap" in p for p in ok)
    bad = validate_contract([InputItem(ak, 0.05)])   # impossible: below cap
    assert any("outside its cap" in p for p in bad)
