import math

import pytest

from tradeup.data.loader import SkinDB
from tradeup.engine import (
    average_normalized,
    evaluate_contract,
    net_after_fee,
    outcome_probabilities,
    output_float,
    required_avg_norm_for_wear,
    steam_net_from_buyer_price,
)
from tradeup.models import InputItem, Rarity, Skin, wear_from_float


def mk(name, coll, rarity, mn, mx):
    return Skin(f"{name}|{coll}", name, rarity, coll, mn, mx)


# --- fees -------------------------------------------------------------
def test_steam_fee_is_built_on_top_not_taken_out():
    # buyer pays 11.50 -> seller receives 10.00
    assert steam_net_from_buyer_price(11.50) == pytest.approx(10.00, abs=0.01)
    # the naive *0.85 shortcut is wrong
    assert steam_net_from_buyer_price(10.00) > 8.50


def test_penny_minimum_fee():
    assert steam_net_from_buyer_price(0.03) == pytest.approx(0.01, abs=0.005)


def test_custom_fee_rate():
    assert net_after_fee(100.0, 0.02) == pytest.approx(98.0)


# --- float math -------------------------------------------------------
def test_normalization_depends_on_the_skins_own_range():
    a = InputItem(mk("A", "C", Rarity.MILSPEC, 0.00, 1.00), 0.10)
    b = InputItem(mk("B", "C", Rarity.MILSPEC, 0.00, 0.50), 0.10)
    assert a.normalized == pytest.approx(0.10)
    assert b.normalized == pytest.approx(0.20)


def test_output_float_projection():
    out = mk("Out", "C", Rarity.RESTRICTED, 0.10, 0.70)
    assert output_float(0.12, out) == pytest.approx(0.172)
    assert wear_from_float(0.172) == "Field-Tested"
    out2 = mk("Out2", "C", Rarity.RESTRICTED, 0.00, 0.50)
    assert output_float(0.12, out2) == pytest.approx(0.06)
    assert wear_from_float(0.06) == "Factory New"


def test_min_float_inputs_give_min_float_output():
    s = mk("In", "C", Rarity.MILSPEC, 0.10, 0.70)
    inputs = [InputItem(s, 0.10) for _ in range(10)]
    assert average_normalized(inputs) == pytest.approx(0.0)
    out = mk("Out", "C", Rarity.RESTRICTED, 0.06, 0.80)
    assert output_float(0.0, out) == pytest.approx(0.06)


def test_required_avg_norm_window():
    out = mk("Out", "C", Rarity.RESTRICTED, 0.00, 0.50)
    lo, hi = required_avg_norm_for_wear(out, "Factory New")
    assert lo == pytest.approx(0.0) and hi == pytest.approx(0.14)
    lo2, _ = required_avg_norm_for_wear(
        mk("X", "C", Rarity.RESTRICTED, 0.10, 0.70), "Factory New")
    assert math.isnan(lo2)


# --- odds -------------------------------------------------------------
def _pool_factory(pools):
    return lambda coll, rarity: pools.get((coll, int(rarity)), [])


def test_single_collection_is_uniform_under_both_models():
    outs = [mk(f"O{i}", "A", Rarity.RESTRICTED, 0, 1) for i in range(4)]
    inp = mk("I", "A", Rarity.MILSPEC, 0, 1)
    pool = _pool_factory({("A", int(Rarity.RESTRICTED)): outs})
    items = [InputItem(inp, 0.1) for _ in range(10)]
    for model in ("outcome_weighted", "per_input"):
        p = outcome_probabilities(items, pool, model)
        assert all(v == pytest.approx(0.25) for v in p.values())


def test_models_diverge_on_mixed_collections():
    a_out = [mk("A1", "A", Rarity.RESTRICTED, 0, 1),
             mk("A2", "A", Rarity.RESTRICTED, 0, 1)]
    b_out = [mk(f"B{i}", "B", Rarity.RESTRICTED, 0, 1) for i in range(4)]
    pool = _pool_factory({("A", int(Rarity.RESTRICTED)): a_out,
                          ("B", int(Rarity.RESTRICTED)): b_out})
    items = ([InputItem(mk("IA", "A", Rarity.MILSPEC, 0, 1), 0.1)] * 5 +
             [InputItem(mk("IB", "B", Rarity.MILSPEC, 0, 1), 0.1)] * 5)

    ow = outcome_probabilities(items, pool, "outcome_weighted")
    assert sum(ow[s.id] for s in a_out) == pytest.approx(1 / 3)

    pi = outcome_probabilities(items, pool, "per_input")
    assert sum(pi[s.id] for s in a_out) == pytest.approx(0.5)
    assert pi["A1|A"] == pytest.approx(0.25)

    for p in (ow, pi):
        assert sum(p.values()) == pytest.approx(1.0)


def test_dead_end_collection_contributes_no_outcome_mass():
    outs = [mk("A1", "A", Rarity.RESTRICTED, 0, 1)]
    pool = _pool_factory({("A", int(Rarity.RESTRICTED)): outs})
    items = ([InputItem(mk("IA", "A", Rarity.MILSPEC, 0, 1), 0.1)] * 2 +
             [InputItem(mk("IB", "B", Rarity.MILSPEC, 0, 1), 0.1)] * 8)
    p = outcome_probabilities(items, pool, "per_input")
    assert p == {"A1|A": pytest.approx(1.0)}


# --- full contract ----------------------------------------------------
def test_evaluate_contract_end_to_end():
    outs = [mk("Cheap", "A", Rarity.RESTRICTED, 0.00, 0.50),
            mk("Jackpot", "A", Rarity.RESTRICTED, 0.00, 0.50)]
    inp = mk("In", "A", Rarity.MILSPEC, 0.00, 1.00)
    pool = _pool_factory({("A", int(Rarity.RESTRICTED)): outs})
    items = [InputItem(inp, 0.05, price=1.00) for _ in range(10)]
    prices = {"Cheap": 5.0, "Jackpot": 40.0}
    res = evaluate_contract(items, pool, lambda s, w, st: prices.get(s.name))

    assert res.cost == pytest.approx(10.0)
    assert res.avg_normalized == pytest.approx(0.05)
    assert res.outcomes[0].wear == "Factory New"
    expected = (0.5 * steam_net_from_buyer_price(5.0)
                + 0.5 * steam_net_from_buyer_price(40.0))
    assert res.ev_net == pytest.approx(expected, abs=0.01)
    assert res.profit_chance == pytest.approx(0.5)
    assert res.unpriced_mass == 0.0


def test_unpriced_outcomes_are_tracked_not_silently_zeroed():
    outs = [mk("Known", "A", Rarity.RESTRICTED, 0, 0.5),
            mk("Unknown", "A", Rarity.RESTRICTED, 0, 0.5)]
    pool = _pool_factory({("A", int(Rarity.RESTRICTED)): outs})
    items = [InputItem(mk("In", "A", Rarity.MILSPEC, 0, 1), 0.05, 1.0)
             for _ in range(10)]
    res = evaluate_contract(
        items, pool, lambda s, w, st: 10.0 if s.name == "Known" else None)
    assert res.unpriced_mass == pytest.approx(0.5)


# --- data layer -------------------------------------------------------
def test_sample_db_loads_and_indexes():
    db = SkinDB.load()
    assert len(db) > 0
    colls = db.tradeable_collections(Rarity.CONSUMER)
    assert colls
    assert db.pool(colls[0], Rarity.INDUSTRIAL)
