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
    os.environ["TRADEUP_SKINS"] = str(d/"skins.json")
    os.environ["TRADEUP_PRICE_SNAPSHOT"] = str(d/"prices.json")

    import importlib
    import tradeup.web as w
    importlib.reload(w)
    return TestClient(w.app)

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
