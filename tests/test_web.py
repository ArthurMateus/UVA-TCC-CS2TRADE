"""Web API: both modes, and the risk filters that make them useful."""
from __future__ import annotations
import json, os
import pytest
from fastapi.testclient import TestClient

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("w")
    wears = ["Factory New","Minimal Wear","Field-Tested","Well-Worn","Battle-Scarred"]
    skins, prices = [], {}
    def add(name, r, coll, price):
        skins.append({"id":f"{name}|{coll}","name":name,"rarity":r,"collection":coll,
            "min_float":0.0,"max_float":1.0,"stattrak_capable":True,
            "souvenir_capable":False})
        for w in wears:
            prices[f"{name} ({w})"] = {"lowest":price,"median":price,"volume":500}
    # guaranteed: cheap in, uniform good out
    for i in range(2): add(f"GIn{i}", 2, "Guaranteed", 1.00)
    for i in range(3): add(f"GOut{i}", 3, "Guaranteed", 30.0 + i)
    # lottery: cheap in, one jackpot + duds
    for i in range(2): add(f"LIn{i}", 2, "Lottery", 2.00)
    add("Jackpot", 3, "Lottery", 900.0)
    for i in range(3): add(f"LDud{i}", 3, "Lottery", 1.00)

    (d/"skins.json").write_text(json.dumps(skins), encoding="utf-8")
    (d/"prices.json").write_text(json.dumps(prices), encoding="utf-8")
    saved = {k: os.environ.get(k) for k in ("TRADEUP_SKINS", "TRADEUP_PRICE_SNAPSHOT",
                                             "TRADEUP_CACHE")}
    os.environ["TRADEUP_SKINS"] = str(d/"skins.json")
    os.environ["TRADEUP_PRICE_SNAPSHOT"] = str(d/"prices.json")
    os.environ["TRADEUP_CACHE"] = str(d/"none.sqlite")

    import importlib
    import tradeup.web as w
    importlib.reload(w)
    yield TestClient(w.app)
    for k, v in saved.items():           # do not leak env into other test modules
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

def test_meta_and_index(client):
    m = client.get("/api/meta").json()
    assert m["skins"] == 11 and m["snapshot_exists"]
    r = client.get("/")
    assert r.status_code == 200 and "Trade-Up Browser" in r.text

def test_guaranteed_only_returns_floor_positive(client):
    j = client.get("/api/guaranteed", params={"budget":200,"rarity":"milspec",
                   "min_input_volume":1}).json()
    assert j["count"] >= 1
    for r in j["results"]:
        assert all(o["profits"] for o in r["outcomes"])
        assert r["worst_net"] > r["cost"]
        assert r["win_chance_pct"] == 100.0
        assert r["grade"] == "guaranteed"

def test_lottery_never_appears_in_guaranteed(client):
    j = client.get("/api/guaranteed", params={"budget":200,"rarity":"milspec",
                   "min_input_volume":1}).json()
    assert "Lottery" not in [r["target_collection"] for r in j["results"]]

def test_scan_win_chance_filter(client):
    lo = client.get("/api/scan", params={"budget":200,"rarity":"milspec",
         "min_win_chance":0,"min_roi":0,"min_input_volume":1,"top":50}).json()
    hi = client.get("/api/scan", params={"budget":200,"rarity":"milspec",
         "min_win_chance":50,"min_roi":0,"min_input_volume":1,"top":50}).json()
    assert hi["count"] <= lo["count"]
    assert all(r["win_chance_pct"] >= 50 for r in hi["results"])

def test_scan_reports_downside(client):
    j = client.get("/api/scan", params={"budget":200,"rarity":"milspec",
        "min_win_chance":0,"min_roi":0,"min_input_volume":1}).json()
    for r in j["results"]:
        assert "worst_loss" in r and "grade" in r and "win_chance_pct" in r
        assert r["grade"] in ("guaranteed","strong","even","risky","lottery")

def test_budget_is_never_exceeded(client):
    for b in (5, 12, 200):
        for ep in ("/api/guaranteed", "/api/scan"):
            j = client.get(ep, params={"budget":b,"rarity":"milspec",
                "min_win_chance":0,"min_roi":0,"min_input_volume":1}).json()
            assert all(r["cost"] <= b for r in j["results"]), (ep, b)

def test_outcomes_carry_float_caps_and_images(client):
    j = client.get("/api/guaranteed", params={"budget":200,"rarity":"milspec",
                   "min_input_volume":1}).json()
    for r in j["results"]:
        for o in r["outcomes"]:
            assert o["min_float"] is not None and o["max_float"] is not None
            assert "image" in o
        for i in r["inputs"]:
            assert i["min_float"] is not None and "image" in i

def test_bad_rarity_is_rejected(client):
    assert client.get("/api/scan", params={"budget":10,"rarity":"nope"}).status_code == 400


@pytest.mark.parametrize("bad", [
    {"float_percentile": 5}, {"float_percentile": -1}, {"haircut": 1.5},
    {"haircut": -1}, {"fee_rate": -1}, {"limit": 0}, {"limit": 10**6},
    {"budget": "inf"}, {"budget": -5}, {"offset": -1}, {"currency": "XXX"},
])
def test_web_rejects_invalid_params(client, bad):
    for ep in ("/api/scan", "/api/guaranteed"):
        p = {"budget": 200, "rarity": "milspec", **bad}
        assert client.get(ep, params=p).status_code in (400, 422), (ep, bad)

def test_scan_is_paginated(client):
    base = {"budget": 200, "rarity": "milspec", "min_win_chance": 0, "min_roi": 0,
            "min_input_volume": 1, "sort": "profit"}
    full = client.get("/api/scan", params={**base, "limit": 100}).json()
    assert full["total"] >= 3
    p1 = client.get("/api/scan", params={**base, "limit": 2}).json()
    assert len(p1["results"]) == 2 and p1["next_offset"] == 2
    p2 = client.get("/api/scan", params={**base, "limit": 2, "offset": 2}).json()
    got = [r["cost"] for r in p1["results"] + p2["results"]]
    assert got == [r["cost"] for r in full["results"][:len(got)]]
    last = client.get("/api/scan", params={**base, "limit": 2,
                                           "offset": full["total"] - 1}).json()
    assert last["next_offset"] is None

def test_guaranteed_budget_filter_reuses_one_computation(client):
    import tradeup.web as w
    w._cache.clear()
    for b in (5, 12, 50, 200):
        client.get("/api/guaranteed", params={"budget": b, "min_input_volume": 1})
    assert len([k for k in w._cache]) == 1

def test_web_cache_is_bounded(client):
    import tradeup.web as w
    for i in range(w.CACHE_ENTRIES + 5):
        client.get("/api/scan", params={"budget": 100 + i, "min_input_volume": 1})
    assert len(w._cache) <= w.CACHE_ENTRIES

def test_float_check_without_key_is_reported_not_fatal(client, monkeypatch):
    import tradeup.config as cfg
    monkeypatch.setattr(cfg, "CSFLOAT_API_KEY", None)
    j = client.get("/api/guaranteed", params={"budget": 200, "min_input_volume": 1,
                                              "float_check": True}).json()
    assert "CSFLOAT_API_KEY" in j.get("note", "")


def _keys(j):
    return {(r["target_collection"], r["filler_collection"], r["cost"]) for r in j["results"]}

def test_more_budget_never_hides_contracts(client):
    base = {"rarity": "milspec", "min_win_chance": 0, "min_roi": 0,
            "min_input_volume": 1, "limit": 100}
    prev = set()
    for b in (5, 12, 50, 200):
        cur = _keys(client.get("/api/scan", params={**base, "budget": b}).json())
        assert prev <= cur, b
        prev = cur

def test_ranked_inputs_carry_their_wear_and_float_dependence(client):
    from tradeup.models import wear_from_float
    j = client.get("/api/scan", params={"budget": 200, "min_win_chance": 0, "min_roi": 0,
                                        "min_input_volume": 1, "float_percentile": 1}).json()
    assert j["results"]
    for r in j["results"]:
        assert "float_sensitive" in r and "float_slack" in r and r["price_basis"] == "wear"
        for i in r["inputs"]:
            assert i["wear"] == wear_from_float(i["float"])   # p=1 stays inside the wear

def test_scan_rejects_non_finite_min_profit(client):
    assert client.get("/api/scan", params={"min_profit": "nan"}).status_code in (400, 422)
    assert client.get("/api/scan", params={"max_cost": "inf"}).status_code in (400, 422)

def test_float_check_note_even_without_results(client, monkeypatch):
    import tradeup.config as cfg
    monkeypatch.setattr(cfg, "CSFLOAT_API_KEY", None)
    j = client.get("/api/scan", params={"budget": 0.01, "float_check": True}).json()
    assert j["total"] == 0 and "CSFLOAT_API_KEY" in j.get("note", "")

def test_meta_hides_empty_tiers_and_reports_basis(client):
    m = client.get("/api/meta").json()
    assert all(t["collections"] > 0 for t in m["tiers"])
    assert m["price_basis"] == "wear" and "snapshot_updated_at" in m
