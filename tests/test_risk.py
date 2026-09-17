"""Risk metrics and guaranteed-profit search.

The point of this module is that maximising EV finds lottery tickets. These
tests pin the distinction between "profitable on average" and "profitable on
a single roll".
"""
from __future__ import annotations
import pytest
from tradeup.models import InputItem, Outcome, Rarity, Skin
from tradeup.risk import (STEAM_KEEP, RiskProfile, find_guaranteed,
                          max_input_price_for_guarantee, profile)
from tradeup.pricing import PriceService, PricingPolicy
from tradeup.prices.steam import PriceBook
from tradeup.data.loader import SkinDB


def sk(name, coll, r, mn=0.0, mx=1.0):
    return Skin(f"{name}|{coll}", name, r, coll, mn, mx)

def oc(name, p, net):
    s = sk(name, "C", Rarity.RESTRICTED)
    return Outcome(s, p, 0.3, "Field-Tested", net*1.03, net, True)


# --- the core distinction --------------------------------------------
def test_lottery_is_ev_positive_but_loses_on_most_rolls():
    """The exact shape the old optimizer ranked first."""
    outs = [oc("Jackpot", 0.50, 236.19), oc("Dud1", 0.25, 1.00), oc("Dud2", 0.25, 0.81)]
    p = profile(outs, cost=101.80)
    assert p.ev > p.cost                    # EV positive...
    assert p.win_chance == pytest.approx(0.50)
    assert not p.floor_positive             # ...but the floor is a wipeout
    assert p.worst_loss == pytest.approx(101.80 - 0.81, abs=0.01)
    assert p.top_ev_share > 0.95            # one outcome carries everything
    assert p.grade() in ("even", "risky", "lottery")


def test_floor_positive_contract_cannot_lose():
    outs = [oc(f"O{i}", 0.25, v) for i, v in enumerate([14.0, 15.5, 13.2, 16.8])]
    p = profile(outs, cost=10.0)
    assert p.floor_positive
    assert p.win_chance == pytest.approx(1.0)
    assert p.worst_loss == 0.0
    assert p.worst_net == 13.2
    assert p.grade() == "guaranteed"
    assert p.risk_reward == float("inf")    # nothing at risk


def test_grades_track_win_chance_not_ev():
    hi = profile([oc("a", 0.9, 20.0), oc("b", 0.1, 1.0)], cost=10.0)
    lo = profile([oc("a", 0.1, 900.0), oc("b", 0.9, 1.0)], cost=10.0)
    assert lo.ev > hi.ev                    # the lottery has far better EV
    assert hi.grade() == "strong" and lo.grade() == "lottery"


def test_median_is_probability_weighted():
    p = profile([oc("cheap", 0.8, 5.0), oc("rich", 0.2, 500.0)], cost=10.0)
    assert p.median_net == 5.0              # 80% of rolls give 5.00
    assert p.ev > 100                       # EV is dragged up by the tail


def test_expected_loss_conditions_on_losing():
    p = profile([oc("win", 0.5, 30.0), oc("lose", 0.5, 2.0)], cost=10.0)
    assert p.expected_loss == pytest.approx(8.0)     # E[loss | loss]
    assert p.downside_ev == pytest.approx(4.0)       # 0.5 * 8


def test_empty_and_zero_cost_are_safe():
    assert profile([], 10.0).n_outcomes == 0
    assert profile([oc("a", 1.0, 5.0)], 0.0).ev == 0.0


# --- the closed form --------------------------------------------------
def test_max_input_price_formula():
    """cheapest_outcome * keep * (1-haircut) / n."""
    v = max_input_price_for_guarantee(115.0, n_inputs=10, haircut=0.0)
    assert v == pytest.approx(10.0, abs=0.01)        # 115 * (1/1.15) / 10
    assert max_input_price_for_guarantee(0.0) == 0.0
    # a haircut lowers the ceiling
    assert max_input_price_for_guarantee(115.0, 10, haircut=0.10) < v


def test_covert_contracts_use_five_inputs():
    assert max_input_price_for_guarantee(115.0, n_inputs=5, haircut=0.0) == \
        pytest.approx(20.0, abs=0.01)


# --- end-to-end search ------------------------------------------------
def _world(tmp_path, input_price, output_prices, coll="C"):
    """Build a one-collection world with fixed input and output prices."""
    import json
    skins, prices = [], {}
    wears = ["Factory New", "Minimal Wear", "Field-Tested",
             "Well-Worn", "Battle-Scarred"]
    skins.append({"id": f"In|{coll}", "name": "In", "rarity": 2,
                  "collection": coll, "min_float": 0.0, "max_float": 1.0,
                  "stattrak_capable": True, "souvenir_capable": False})
    for w in wears:
        prices[f"In ({w})"] = {"lowest": input_price, "median": input_price,
                               "volume": 500}
    for i, v in enumerate(output_prices):
        skins.append({"id": f"Out{i}|{coll}", "name": f"Out{i}", "rarity": 3,
                      "collection": coll, "min_float": 0.0, "max_float": 1.0,
                      "stattrak_capable": True, "souvenir_capable": False})
        for w in wears:
            prices[f"Out{i} ({w})"] = {"lowest": v, "median": v, "volume": 500}
    sp = tmp_path/"s.json"; pp = tmp_path/"p.json"
    sp.write_text(json.dumps(skins), encoding="utf-8")
    pp.write_text(json.dumps(prices), encoding="utf-8")
    db = SkinDB.load(sp)
    svc = PriceService(PriceBook.load(pp),
                       PricingPolicy(min_volume=1, min_input_volume=1, haircut=0.0))
    return db, svc


def test_finds_a_real_guarantee(tmp_path):
    # 10 inputs at 1.00 = 10.00; cheapest output 30 -> net ~26. Guaranteed.
    db, svc = _world(tmp_path, 1.00, [30.0, 40.0, 50.0])
    g = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100)
    assert len(g) == 1
    r = g[0]
    assert r.cost == pytest.approx(10.0)
    assert r.worst_net > r.cost
    assert r.guaranteed_profit > 0
    assert r.n_inputs == 10
    assert len(r.outcomes) == 3


def test_rejects_when_the_floor_does_not_clear(tmp_path):
    # cheapest output 10 -> net 8.70 < cost 10.00. Not guaranteed.
    db, svc = _world(tmp_path, 1.00, [10.0, 500.0])
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100) == []


def test_one_worthless_outcome_kills_the_guarantee(tmp_path):
    """This is the whole point: a single bad outcome is disqualifying."""
    db, svc = _world(tmp_path, 1.00, [0.50, 900.0, 900.0])
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100) == []


def test_budget_is_respected(tmp_path):
    db, svc = _world(tmp_path, 5.00, [300.0, 400.0])
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100)      # 50 <= 100
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=40) == []  # 50 > 40


def test_min_profit_filter(tmp_path):
    db, svc = _world(tmp_path, 1.00, [12.0, 13.0])
    g = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100, min_profit=0.0)
    assert g and g[0].guaranteed_profit < 5
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100, min_profit=5.0) == []


def test_unpriced_outcome_blocks_a_guarantee(tmp_path):
    """An unpriced outcome scores as zero elsewhere; never call that safe."""
    import json
    db, svc = _world(tmp_path, 1.00, [30.0, 40.0])
    # remove one output's prices entirely
    svc.source.data = {k: v for k, v in svc.source.data.items()
                       if not k.startswith("Out1")}
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100,
                           require_all_outcomes_priced=True) == []


def test_results_sorted_by_guaranteed_profit(tmp_path):
    import json
    skins, prices = [], {}
    wears = ["Factory New", "Field-Tested", "Battle-Scarred"]
    for ci, (coll, inp, out) in enumerate([("A", 1.0, 30.0), ("B", 1.0, 60.0)]):
        skins.append({"id": f"In{ci}|{coll}", "name": f"In{ci}", "rarity": 2,
                      "collection": coll, "min_float": 0.0, "max_float": 1.0,
                      "stattrak_capable": True, "souvenir_capable": False})
        skins.append({"id": f"Out{ci}|{coll}", "name": f"Out{ci}", "rarity": 3,
                      "collection": coll, "min_float": 0.0, "max_float": 1.0,
                      "stattrak_capable": True, "souvenir_capable": False})
        for w in wears:
            prices[f"In{ci} ({w})"] = {"lowest": inp, "median": inp, "volume": 99}
            prices[f"Out{ci} ({w})"] = {"lowest": out, "median": out, "volume": 99}
    sp = tmp_path/"s2.json"; pp = tmp_path/"p2.json"
    sp.write_text(json.dumps(skins), encoding="utf-8")
    pp.write_text(json.dumps(prices), encoding="utf-8")
    db = SkinDB.load(sp)
    svc = PriceService(PriceBook.load(pp),
                       PricingPolicy(min_volume=1, min_input_volume=1, haircut=0.0))
    g = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100)
    assert len(g) == 2
    assert g[0].guaranteed_profit >= g[1].guaranteed_profit
    assert g[0].collection == "B"
