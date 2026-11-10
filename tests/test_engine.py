"""Float mapping, per-item odds, and fees -- verified against worked examples."""
from __future__ import annotations
import pytest
from tradeup.engine import (DEFAULT_ODDS_MODEL, average_trade_up_float,
    collection_odds, evaluate_contract, net_after_fee, outcome_float,
    outcome_probabilities, steam_net_from_buyer_price, trade_up_float,
    validate_contract)
from tradeup.models import InputItem, Rarity, Skin, wear_from_float

def mk(n,c,r,mn=0.0,mx=1.0): return Skin(f"{n}|{c}",n,r,c,mn,mx)
def pool_of(m): return lambda c,r: m.get((c,int(r)),[])

# --- fees -------------------------------------------------------------
def test_fee_is_added_on_top_not_taken_out():
    assert steam_net_from_buyer_price(11.50) == pytest.approx(10.00, abs=0.01)
    assert steam_net_from_buyer_price(271.60) == pytest.approx(236.19, abs=0.01)

def test_thirteen_percent_of_the_buyer_price():
    for b in (10.0, 50.0, 100.0, 271.60):
        kept = steam_net_from_buyer_price(b) / b
        assert kept == pytest.approx(0.8696, abs=0.002)   # 13.04% lost

def test_the_naive_shortcut_is_wrong():
    assert steam_net_from_buyer_price(10.0) > 10.0 * 0.85

def test_penny_minimum():
    assert steam_net_from_buyer_price(0.03) == pytest.approx(0.01, abs=0.005)

def test_alternate_marketplace_fee():
    assert net_after_fee(100.0, 0.02) == pytest.approx(98.0)

# --- float ------------------------------------------------------------
def test_worked_examples():
    assert trade_up_float(0.22798392176628113, 0.21, 0.79) == pytest.approx(0.031007, abs=5e-6)
    assert trade_up_float(0.749, 0.08, 0.75) == pytest.approx(0.99850746, abs=1e-8)

def test_outcome_float_per_output_cap():
    avg = trade_up_float(0.749, 0.08, 0.75)
    assert outcome_float(avg, mk("A","C",Rarity.RESTRICTED,0.18,1.00)) == \
        pytest.approx(0.998776119, abs=1e-8)
    assert outcome_float(avg, mk("B","C",Rarity.RESTRICTED,0.00,0.50)) == \
        pytest.approx(0.499253731, abs=1e-8)

def test_same_raw_float_different_caps():
    a = InputItem(mk("A","C",Rarity.MILSPEC,0.0,1.0), 0.10)
    b = InputItem(mk("B","C",Rarity.MILSPEC,0.0,0.5), 0.10)
    assert (a.trade_up_float, b.trade_up_float) == (pytest.approx(0.10), pytest.approx(0.20))

def test_average_uses_adjusted_not_raw():
    s = mk("P90 | Trigon","Phoenix",Rarity.MILSPEC,0.08,0.75)
    avg = average_trade_up_float([InputItem(s,0.749)]*10)
    assert avg == pytest.approx(0.99850746, abs=1e-8)
    assert avg != pytest.approx(0.749, abs=1e-3)

@pytest.mark.parametrize("mn,mx,expected",[
    (0.10,0.70,["Minimal Wear","Field-Tested","Well-Worn","Battle-Scarred"]),
    (0.21,0.79,["Field-Tested","Well-Worn","Battle-Scarred"]),
    (0.00,1.00,["Factory New","Minimal Wear","Field-Tested","Well-Worn","Battle-Scarred"]),
    (0.00,0.08,["Factory New","Minimal Wear"]),
])
def test_float_cap_limits_conditions(mn,mx,expected):
    assert mk("X","C",Rarity.MILSPEC,mn,mx).reachable_wears() == expected

def test_wear_boundaries():
    assert wear_from_float(0.0699)=="Factory New"
    assert wear_from_float(0.07)=="Minimal Wear"
    assert wear_from_float(0.184218690)=="Field-Tested"
    assert wear_from_float(1.00)=="Battle-Scarred"

# --- odds -------------------------------------------------------------
def test_per_input_is_the_default():
    assert DEFAULT_ODDS_MODEL == "per_input"

def test_seven_of_ten_gives_seventy_percent():
    c1,c2 = mk("I1","C1",Rarity.MILSPEC), mk("I2","C2",Rarity.MILSPEC)
    outs = {("C1",3):[mk(f"A{i}","C1",Rarity.RESTRICTED) for i in range(3)],
            ("C2",3):[mk(f"B{i}","C2",Rarity.RESTRICTED) for i in range(3)]}
    items = [InputItem(c1,0.1)]*7 + [InputItem(c2,0.1)]*3
    assert collection_odds(items, pool_of(outs))["C1"] == pytest.approx(0.70)
    p = outcome_probabilities(items, pool_of(outs))
    assert p["A0|C1"] == pytest.approx(0.70/3)
    assert sum(p.values()) == pytest.approx(1.0)

def test_single_outcome_collection_is_flat_ten_percent():
    """The Dragon Lore case. Legacy model says 5.26% for everything."""
    knight = mk("M4A1-S | Knight","Cobblestone",Rarity.CLASSIFIED)
    outs = {("Cobblestone",5):[mk("AWP | Dragon Lore","Cobblestone",Rarity.COVERT)]}
    items = [InputItem(knight,0.1)]
    for i in range(9):
        outs[(f"C{i}",5)] = [mk(f"X{i}",f"C{i}",Rarity.COVERT),
                             mk(f"Y{i}",f"C{i}",Rarity.COVERT)]
        items.append(InputItem(mk(f"F{i}",f"C{i}",Rarity.CLASSIFIED),0.1))
    cur = outcome_probabilities(items, pool_of(outs), "per_input")
    assert cur["AWP | Dragon Lore|Cobblestone"] == pytest.approx(0.10)
    assert cur["X0|C0"] == pytest.approx(0.05)
    old = outcome_probabilities(items, pool_of(outs), "outcome_weighted")
    assert old["AWP | Dragon Lore|Cobblestone"] == pytest.approx(0.0526, abs=1e-4)

def test_dead_end_collection_has_no_mass():
    outs = {("A",3):[mk("A1","A",Rarity.RESTRICTED)]}
    items = [InputItem(mk("IA","A",Rarity.MILSPEC),0.1)]*2 + \
            [InputItem(mk("IB","B",Rarity.MILSPEC),0.1)]*8
    assert outcome_probabilities(items, pool_of(outs)) == {"A1|A": pytest.approx(1.0)}

# --- contract ---------------------------------------------------------
def test_evaluate_end_to_end():
    outs = {("A",3):[mk("Cheap","A",Rarity.RESTRICTED,0,0.5),
                     mk("Rich","A",Rarity.RESTRICTED,0,0.5)]}
    items = [InputItem(mk("In","A",Rarity.MILSPEC),0.05,1.0)]*10
    pr = {"Cheap":5.0,"Rich":40.0}
    res = evaluate_contract(items, pool_of(outs), lambda s,w,st: pr.get(s.name))
    assert res.cost == pytest.approx(10.0)
    assert res.profit_chance == pytest.approx(0.5)
    assert res.unpriced_mass == 0.0

def test_unpriced_outcomes_are_tracked():
    outs = {("A",3):[mk("Known","A",Rarity.RESTRICTED,0,0.5),
                     mk("Unknown","A",Rarity.RESTRICTED,0,0.5)]}
    items = [InputItem(mk("In","A",Rarity.MILSPEC),0.05,1.0)]*10
    res = evaluate_contract(items, pool_of(outs),
                            lambda s,w,st: 10.0 if s.name=="Known" else None)
    assert res.unpriced_mass == pytest.approx(0.5)

def test_float_outside_cap_is_rejected():
    ak = mk("AK-47 | Redline","C",Rarity.CLASSIFIED,0.10,0.70)
    assert any("outside its cap" in p for p in validate_contract([InputItem(ak,0.05)]))
    assert not any("outside its cap" in p for p in validate_contract([InputItem(ak,0.15)]))

def test_stattrak_cannot_mix_with_souvenir():
    a = Skin("a|C","a",Rarity.MILSPEC,"C",0,1,True,True)
    items = [InputItem(a,0.1,stattrak=True), InputItem(a,0.1,souvenir=True)]
    assert any("StatTrak" in p for p in validate_contract(items))
