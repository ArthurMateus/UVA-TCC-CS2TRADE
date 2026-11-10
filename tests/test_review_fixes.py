"""Regression tests for the issues found in the code review / QA pass."""
from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import pkgutil
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import tradeup
from tradeup.data.loader import SkinDB
from tradeup.engine import _fee_cents, net_after_fee, steam_net_from_buyer_price, validate_contract
from tradeup.float_pricing import max_avg_preserving_wears, refine_row
from tradeup.models import InputItem, Rarity, Skin, wear_from_float
from tradeup.optimizer import explain, search
from tradeup.prices.bulk import BulkMarket, tier_filters
from tradeup.prices.csfloat import CSFloatSource, StaticFloatSource, bucket_down, category_for
from tradeup.prices.fetcher import AsyncPriceFetcher, fetch_sync, parse_retry_after
from tradeup.prices.steam import PriceBook, PriceCache, Quote, parse_price
from tradeup.pricing import PriceService, PricingPolicy
from tradeup.risk import explain_guaranteed, find_guaranteed

WEARS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]


# --- every module imports (dead/broken modules were shipping) --------------
@pytest.mark.parametrize("mod", [m.name for m in pkgutil.walk_packages(
    tradeup.__path__, "tradeup.")])
def test_all_modules_import(mod):
    importlib.import_module(mod)


# --- Steam fee: exact integer-cent algorithm -------------------------------
def test_steam_fee_matches_valve_integer_algorithm():
    for seller in range(1, 30001):
        buyer = seller + _fee_cents(seller)
        assert steam_net_from_buyer_price(buyer / 100) == pytest.approx(seller / 100), seller


def test_steam_fee_known_value():
    assert steam_net_from_buyer_price(10.00) == pytest.approx(8.70)
    assert net_after_fee(10.0, 0.02) == pytest.approx(9.8)


# --- guaranteed mode is net of fees ----------------------------------------
def _world(tmp_path, input_price, output_prices, haircut=0.0):
    skins, prices = [], {}
    skins.append({"id": "In|C", "name": "In", "rarity": 2, "collection": "C",
                  "min_float": 0.0, "max_float": 1.0})
    for w in WEARS:
        prices[f"In ({w})"] = {"lowest": input_price, "median": input_price, "volume": 500}
    for i, v in enumerate(output_prices):
        skins.append({"id": f"Out{i}|C", "name": f"Out{i}", "rarity": 3,
                      "collection": "C", "min_float": 0.0, "max_float": 1.0})
        for w in WEARS:
            prices[f"Out{i} ({w})"] = {"lowest": v, "median": v, "volume": 500}
    (tmp_path / "s.json").write_text(json.dumps(skins), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(prices), encoding="utf-8")
    db = SkinDB.load(tmp_path / "s.json")
    svc = PriceService(PriceBook.load(tmp_path / "p.json"),
                       PricingPolicy(min_volume=1, min_input_volume=1, haircut=haircut))
    return db, svc


def test_guaranteed_values_are_net_of_steam_fee(tmp_path):
    db, svc = _world(tmp_path, 0.80, [10.0, 30.0], haircut=0.03)
    g = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100)
    assert g
    r = g[0]
    assert r.worst_net == pytest.approx(steam_net_from_buyer_price(10.0 * 0.97))
    assert r.guaranteed_profit == pytest.approx(r.worst_net - 8.0)
    e = explain_guaranteed(r)
    assert all(o["net"] < o["gross"] for o in e["outcomes"])


def test_guaranteed_min_profit_uses_net(tmp_path):
    db, svc = _world(tmp_path, 0.80, [10.0, 30.0], haircut=0.03)
    assert find_guaranteed(db, svc, Rarity.MILSPEC, budget=100, min_profit=1.0) == []


def test_guaranteed_respects_custom_fee(tmp_path):
    db, svc = _world(tmp_path, 1.00, [12.0, 13.0])
    steam = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100)
    csfloat = find_guaranteed(db, svc, Rarity.MILSPEC, budget=100, fee_rate=0.02)
    assert csfloat[0].worst_net == pytest.approx(12.0 * 0.98)
    assert csfloat[0].worst_net > steam[0].worst_net


# --- prices -----------------------------------------------------------------
@pytest.mark.parametrize("raw,val", [
    ("$12.34", 12.34), ("R$ 1.234,56", 1234.56), ("1 234,56 pуб.", 1234.56),
    ("1.234,56€", 1234.56), ("¥ 1,234.56", 1234.56), ("R$ 1.000,00", 1000.0),
    ("€1.234", 1234.0), ("R$ 3,41", 3.41), ("0,03€", 0.03), ("$1,204", 1204.0),
])
def test_parse_price_locales(raw, val):
    assert parse_price(raw) == pytest.approx(val)


def test_snapshot_records_currency(tmp_path):
    c = PriceCache(tmp_path / "c.sqlite")
    c.put(Quote("A (FN)", 1.0, 1.1, 5, 0.0), 7)
    c.export(tmp_path / "snap.json", 7)
    c.close()
    book = PriceBook.load(tmp_path / "snap.json")
    assert book.currency == 7 and len(book) == 1 and book.get("A (FN)").lowest == 1.0


def test_bulk_and_overview_fields_do_not_overwrite_each_other(tmp_path):
    c = PriceCache(tmp_path / "c.sqlite")
    c.put(Quote("A (FN)", 1.0, 1.2, 40, 1.0), 1)              # priceoverview
    c.put(Quote("A (FN)", 0.9, None, None, 2.0, 120), 1)       # bulk
    q = c.get("A (FN)", 1, 1e12)
    assert (q.lowest, q.median, q.volume, q.listings) == (0.9, 1.2, 40, 120)
    assert c.depth({"A (FN)"}, 1) == {"A (FN)": (120, 40)}
    c.close()


# --- fetcher robustness -----------------------------------------------------
def _fake_client(monkeypatch, handler):
    calls = {"n": 0}

    class Fake:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            calls["n"] += 1
            return handler(calls["n"], params, httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: Fake())
    return calls


def _fetcher(tmp_path, **kw):
    f = AsyncPriceFetcher(PriceCache(tmp_path / "f.sqlite"), currency=1, start_rate=6000.0,
                          base_backoff=0.01, server_error_delay=0.01, **kw)
    f.limiter.ceiling = 6000.0
    return f


def test_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert 20 < parse_retry_after(format_datetime(when, usegmt=True)) <= 31
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("garbage") == 0.0


def test_transient_500_is_not_negative_cached(tmp_path, monkeypatch):
    def h(n, p, req):
        if n == 1:
            return httpx.Response(500, text="", request=req)
        return httpx.Response(200, json={"success": True, "lowest_price": "$1.00",
                                         "median_price": "$1.10", "volume": "9"}, request=req)
    _fake_client(monkeypatch, h)
    f = _fetcher(tmp_path)
    st = asyncio.run(f.fetch(["A (FN)"]))
    assert st.ok == 1 and st.empty == 0
    assert f.cache.get("A (FN)", 1, 9999).lowest == 1.0


@pytest.mark.parametrize("body", [[1, 2, 3], "not json"])
def test_malformed_body_is_counted_not_lost(tmp_path, monkeypatch, body):
    def h(n, p, req):
        if isinstance(body, str):
            return httpx.Response(200, text=body, request=req)
        return httpx.Response(200, json=body, request=req)
    _fake_client(monkeypatch, h)
    f = _fetcher(tmp_path, max_attempts=2)
    st = asyncio.run(f.fetch(["A (FN)"]))
    assert st.errors >= 1 and st.gave_up == 1 and st.gave_up_names == ["A (FN)"]


def test_bad_volume_does_not_drop_the_quote(tmp_path, monkeypatch):
    _fake_client(monkeypatch, lambda n, p, req: httpx.Response(200, json={
        "success": True, "lowest_price": "$2.00", "volume": "abc"}, request=req))
    f = _fetcher(tmp_path)
    st = asyncio.run(f.fetch(["A (FN)"]))
    assert st.ok == 1 and f.cache.get("A (FN)", 1, 9999).volume is None


def test_breaker_halts_run_under_permanent_ban(tmp_path, monkeypatch):
    calls = _fake_client(monkeypatch, lambda n, p, req: httpx.Response(429, request=req))
    f = AsyncPriceFetcher(PriceCache(tmp_path / "b.sqlite"), currency=1, concurrency=3,
                          start_rate=6000.0, base_backoff=0.01, max_backoff=0.02,
                          breaker_trip_after=2, breaker_cooldown=0.05, breaker_max_trips=3)
    f.limiter.ceiling = 6000.0
    st = asyncio.run(f.fetch([f"I{i} (FN)" for i in range(50)]))
    assert st.halted
    assert calls["n"] < 50                # did not keep poking a banned IP
    assert st.gave_up == 50 - st.ok - st.empty


def test_fetch_sync_accepts_progress(tmp_path, monkeypatch):
    _fake_client(monkeypatch, lambda n, p, req: httpx.Response(200, json={
        "success": True, "lowest_price": "$1.00"}, request=req))
    seen = []
    st = fetch_sync(PriceCache(tmp_path / "s.sqlite"), ["A (FN)"], currency=1,
                    start_rate=6000.0, progress=lambda s, m: seen.append(m))
    assert st.ok == 1


# --- bulk pagination --------------------------------------------------------
def test_tier_filters_use_verified_steam_tags():
    f = tier_filters(2, ("normal", "souvenir"))
    assert f["category_730_Rarity[]"] == ["tag_Rarity_Rare_Weapon"]
    assert f["category_730_Quality[]"] == ["tag_normal", "tag_tournament"]


def test_bulk_failed_page_marks_sweep_incomplete(tmp_path):
    class Flaky:
        def get(self, url, params=None):
            req = httpx.Request("GET", url)
            if params["start"] >= 10:
                return httpx.Response(200, json={"success": False}, request=req)
            return httpx.Response(200, json={"success": True, "total_count": 25, "results": [
                {"hash_name": f"I{params['start']+i}", "sell_price": 100, "sell_listings": 3}
                for i in range(10)]}, request=req)
    m = BulkMarket(cache_path=tmp_path / "c.sqlite", page_delay=0, max_retries=1)
    m.client = Flaky()
    st = m.sweep()
    assert st.incomplete and st.written == 10 and st.last_start == 10


def test_bulk_repeated_page_stops(tmp_path):
    class Stuck:
        calls = 0
        def get(self, url, params=None):
            Stuck.calls += 1
            return httpx.Response(200, json={"success": True, "total_count": 10**6,
                "results": [{"hash_name": "Same", "sell_price": 1}]},
                request=httpx.Request("GET", url))
    m = BulkMarket(cache_path=tmp_path / "c.sqlite", page_delay=0)
    m.client = Stuck()
    st = m.sweep()
    assert st.incomplete and Stuck.calls == 2


def test_bulk_resumes_from_checkpoint(tmp_path):
    seen = []
    class Srv:
        def get(self, url, params=None):
            seen.append(params["start"])
            rows = [{"hash_name": f"I{params['start']+i}", "sell_price": 1}
                    for i in range(min(10, 30 - params["start"]))]
            return httpx.Response(200, json={"success": True, "total_count": 30,
                                             "results": rows}, request=httpx.Request("GET", url))
    m = BulkMarket(cache_path=tmp_path / "c.sqlite", page_delay=0)
    m.client = Srv()
    marks = []
    m.sweep_tier(2, start_at=20, on_checkpoint=lambda s, t: marks.append(s))
    assert seen == [20] and marks == [30]


# --- engine rules -----------------------------------------------------------
def _skin(name, r, coll="C", lo=0.0, hi=1.0):
    return Skin(f"{name}|{coll}", name, r, coll, lo, hi)


def test_validate_contract_requires_exact_count():
    s = _skin("A", Rarity.MILSPEC)
    assert any("exactly 10" in p for p in validate_contract([InputItem(s, 0.2)] * 9))
    assert not any("exactly" in p for p in validate_contract([InputItem(s, 0.2)] * 10))
    c = _skin("K", Rarity.COVERT)
    assert not any("exactly" in p for p in validate_contract([InputItem(c, 0.2)] * 5))


def test_validate_contract_rejects_top_tier():
    s = _skin("Knife", Rarity.RARE_SPECIAL)
    assert any("cannot be traded up" in p for p in validate_contract([InputItem(s, 0.2)] * 10))


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan")])
def test_wear_from_float_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        wear_from_float(bad)


# --- float-aware refinement -------------------------------------------------
def test_max_avg_preserving_wears():
    out = _skin("O", Rarity.RESTRICTED, lo=0.0, hi=1.0)
    # outcome in MW (0.07-0.15) on a 0-1 cap: average must stay below 0.15
    assert max_avg_preserving_wears([(out, "Minimal Wear")]) == pytest.approx(0.15, abs=1e-5)
    narrow = _skin("N", Rarity.RESTRICTED, lo=0.0, hi=0.5)
    # FT on a 0-0.5 cap: output = avg*0.5 < 0.38 -> avg < 0.76
    assert max_avg_preserving_wears([(out, "Battle-Scarred"), (narrow, "Field-Tested")]) \
        == pytest.approx(0.76, abs=1e-5)


def _float_world(tmp_path):
    skins = [{"id": "In|C", "name": "In", "rarity": 2, "collection": "C",
              "min_float": 0.0, "max_float": 1.0},
             {"id": "Out|C", "name": "Out", "rarity": 3, "collection": "C",
              "min_float": 0.0, "max_float": 1.0}]
    prices = {f"In ({w})": {"lowest": 1.0, "median": 1.0, "volume": 500} for w in WEARS}
    prices["In (Factory New)"] = {"lowest": 5.0, "median": 5.0, "volume": 500}
    prices.update({f"Out ({w})": {"lowest": p, "median": p, "volume": 500}
                   for w, p in zip(WEARS, [40.0, 20.0, 8.0, 5.0, 4.0])})
    (tmp_path / "s.json").write_text(json.dumps(skins), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(prices), encoding="utf-8")
    db = SkinDB.load(tmp_path / "s.json")
    svc = PriceService(PriceBook.load(tmp_path / "p.json"),
                       PricingPolicy(min_volume=1, min_input_volume=1, haircut=0.0))
    return db, svc


def _first_row(db, svc):
    rows = [explain(c) for c in search(db, svc, Rarity.MILSPEC, budget=100,
                                       min_roi=0.0, min_win_chance=0.0)]
    # the contract that relies on Minimal Wear inputs landing a Minimal Wear output
    return next(r for r in rows if r["outcomes"][0]["wear"] == "Minimal Wear")


def test_float_check_reprices_inputs_at_the_needed_float(tmp_path):
    db, svc = _float_world(tmp_path)
    row = _first_row(db, svc)
    # cheap MW listings are all high-float; low floats cost 3x
    src = StaticFloatSource({"In (Minimal Wear)":
                             [(1.0, 0.149)] * 20 + [(3.0, 0.08)] * 20})
    c = refine_row(row, db, src, svc, basis="premium")
    assert c["status"] == "ok"
    assert c["max_avg_float"] == pytest.approx(0.15, abs=1e-5)
    assert all(f <= 0.15 for i in c["inputs"] for f in i["floats"])
    assert c["real_cost"] >= c["est_cost"]


def test_float_check_reports_infeasible_contracts(tmp_path):
    db, svc = _float_world(tmp_path)
    row = _first_row(db, svc)
    src = StaticFloatSource({"In (Minimal Wear)": [(1.0, 0.149)] * 3})
    c = refine_row(row, db, src, svc)
    assert c["status"] == "infeasible" and "only 3 of 10" in c["reason"]


def test_float_check_csfloat_basis_needs_rate(tmp_path):
    db, svc = _float_world(tmp_path)
    c = refine_row(_first_row(db, svc), db, StaticFloatSource({}), svc, basis="csfloat")
    assert c["status"] == "unavailable"


def test_csfloat_parsing_and_params():
    payload = {"data": [
        {"id": "1", "price": 250, "item": {"float_value": 0.12}},
        {"id": "2", "price": 199, "item": {"float_value": 0.14}},
        {"id": "3", "price": "bad", "item": {}},
    ]}
    got = CSFloatSource._parse(payload)
    assert [(x.price, x.float_value) for x in got] == [(1.99, 0.14), (2.5, 0.12)]
    p = CSFloatSource._params("Souvenir X (Field-Tested)", 0.15, 0.2)
    assert p["category"] == 3 and p["max_float"] == "0.200" and p["sort_by"] == "lowest_price"
    assert category_for("StatTrak™ X (Field-Tested)") == 2
    assert bucket_down(0.14999) == pytest.approx(0.149)


def test_csfloat_client_uses_cache(tmp_path):
    from tradeup.prices.csfloat import FloatListingCache
    calls = []

    def handler(request):
        calls.append(request.url.params.get("max_float"))
        return httpx.Response(200, json={"data": [
            {"id": "a", "price": 100, "item": {"float_value": 0.1}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    src = CSFloatSource("key", cache=FloatListingCache(tmp_path / "f.sqlite"),
                        client=client, min_interval=0)
    a = src.cheapest("X (Minimal Wear)", min_float=0.07, max_float=0.1499, n=1)
    b = src.cheapest("X (Minimal Wear)", min_float=0.07, max_float=0.1495, n=1)
    assert a == b and len(calls) == 1 and calls[0] == "0.149"
