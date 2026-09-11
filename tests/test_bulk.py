"""Bulk market ingestion.

The critical property: Steam does not always honour the requested `count`.
It commonly returns 10 rows regardless. Advancing `start` by the REQUESTED
size then skips everything Steam did not return -- a silent data-loss bug.
Pagination must step by the number of rows actually received.
"""
from __future__ import annotations

import time

import httpx
import pytest

from tradeup.prices.bulk import REQUESTED_PAGE_SIZE, BulkMarket


def _catalogue(n: int) -> list[str]:
    return [f"Item {i:04d} (Field-Tested)" for i in range(n)]


def _server(names: list[str], rows_per_page: int):
    """A fake Steam that ignores `count` and returns `rows_per_page`."""

    class Fake:
        calls = 0

        def get(self, url, params=None):
            Fake.calls += 1
            q = (params.get("query") or "").lower()
            hits = [n for n in names if q in n.lower()] if q else names
            start = params["start"]
            rows = hits[start:start + rows_per_page]
            return httpx.Response(200, json={
                "success": True, "total_count": len(hits),
                "results": [{"hash_name": n, "sell_price": 100 + i,
                             "sell_listings": 5} for i, n in enumerate(rows)]},
                request=httpx.Request("GET", url))

    return Fake()


def test_no_items_are_skipped_when_steam_caps_the_page(tmp_path):
    """Regression: this is the bug that silently lost ~90% of the market."""
    names = _catalogue(250)
    m = BulkMarket(currency="BRL", cache_path=tmp_path / "c.sqlite", page_delay=0)
    m.client = _server(names, rows_per_page=10)   # asked 100, gets 10

    notes: list[str] = []
    stats = m.sweep(progress=notes.append)

    assert stats.effective_page_size == 10
    assert stats.items == 250
    missing = [n for n in names if m.cache.get(n, 7, 99999) is None]
    assert not missing, f"{len(missing)} items skipped, e.g. {missing[:3]}"
    assert stats.pages == 25          # 250/10, not 250/100
    assert any("Steam gave 10" in n for n in notes)


def test_full_page_size_is_used_when_honoured(tmp_path):
    names = _catalogue(250)
    m = BulkMarket(currency="BRL", cache_path=tmp_path / "f.sqlite", page_delay=0)
    m.client = _server(names, rows_per_page=REQUESTED_PAGE_SIZE)
    stats = m.sweep()
    assert stats.effective_page_size == 100
    assert stats.pages == 3
    assert stats.items == 250
    assert not [n for n in names if m.cache.get(n, 7, 99999) is None]


def test_row_count_counts_unpriced_rows_too(tmp_path):
    """A row with no usable price still occupies a catalogue position."""
    page = {"success": True, "total_count": 3, "results": [
        {"hash_name": "A (FT)", "sell_price": 100, "sell_listings": 1},
        {"hash_name": "B (FT)", "sell_price": None, "sell_price_text": ""},
        {"hash_name": "C (FT)", "sell_price": 0},
    ]}
    assert BulkMarket.row_count(page) == 3          # step start by 3
    assert len(list(BulkMarket.parse_results(page))) == 1   # but keep 1


def test_parse_results_handles_real_payload_shapes():
    page = {"success": True, "results": [
        {"name": "AK-47 | Elite Build",
         "hash_name": "AK-47 | Elite Build (Field-Tested)",
         "sell_price": 341, "sell_price_text": "R$ 3,41", "sell_listings": 1420},
        {"hash_name": "Souvenir MP9 | Storm (Minimal Wear)",
         "sell_price": 89, "sell_listings": 37},
        {"name": "Fallback | Text (Factory New)", "sell_price": None,
         "sell_price_text": "R$ 12,50", "sell_listings": 9},
        "not-a-dict",
    ]}
    rows = list(BulkMarket.parse_results(page))
    assert ("AK-47 | Elite Build (Field-Tested)", 3.41, 1420) in rows
    assert ("Souvenir MP9 | Storm (Minimal Wear)", 0.89, 37) in rows
    assert ("Fallback | Text (Factory New)", 12.50, 9) in rows
    assert len(rows) == 3


def test_targeted_queries_cost_far_fewer_requests(tmp_path):
    names = ([f"AK-47 | Skin {i} (FT)" for i in range(25)]
             + [f"MP9 | Skin {i} (FT)" for i in range(200)])
    names.sort()

    m = BulkMarket(currency="BRL", cache_path=tmp_path / "q.sqlite", page_delay=0)
    m.client = _server(names, rows_per_page=10)
    stats = m.sweep_queries(["AK-47"])

    assert stats.written == 25
    assert stats.pages == 3                       # vs 23 for a full sweep
    assert stats.queries_done == 1
    assert m.cache.get("AK-47 | Skin 7 (FT)", 7, 99999) is not None


def test_wanted_filter_restricts_writes_not_paging(tmp_path):
    names = _catalogue(100)
    want = {names[i] for i in range(0, 100, 10)}
    m = BulkMarket(currency="BRL", cache_path=tmp_path / "w.sqlite", page_delay=0)
    m.client = _server(names, rows_per_page=10)
    stats = m.sweep(wanted=want)
    assert stats.items == 100          # paged through everything
    assert stats.written == len(want)  # kept only what was asked for


def test_sweep_recovers_from_429(tmp_path):
    calls = {"n": 0}

    class Limited:
        def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(429, headers={"Retry-After": "1"}, text="",
                                      request=httpx.Request("GET", url))
            return httpx.Response(200, json={
                "success": True, "total_count": 1,
                "results": [{"hash_name": "OK | Item (FN)", "sell_price": 500,
                             "sell_listings": 10}]},
                request=httpx.Request("GET", url))

    m = BulkMarket(currency="BRL", cache_path=tmp_path / "r.sqlite",
                   page_delay=0, max_backoff=2)
    m.client = Limited()
    t0 = time.monotonic()
    stats = m.sweep()
    assert stats.rate_limited == 2 and stats.written == 1
    assert time.monotonic() - t0 < 30      # backoff is capped


def test_sweep_stops_on_non_json_body(tmp_path):
    """Steam serves HTML during outages; do not loop forever on it."""

    class Html:
        def get(self, url, params=None):
            return httpx.Response(200, text="<html>maintenance</html>",
                                  request=httpx.Request("GET", url))

    m = BulkMarket(currency="BRL", cache_path=tmp_path / "h.sqlite",
                   page_delay=0, max_retries=2, max_backoff=1)
    m.client = Html()
    stats = m.sweep()
    assert stats.written == 0 and stats.errors >= 1


def test_empty_page_terminates_the_sweep(tmp_path):
    class Empty:
        def get(self, url, params=None):
            return httpx.Response(200, json={"success": True, "total_count": 999,
                                             "results": []},
                                  request=httpx.Request("GET", url))

    m = BulkMarket(currency="BRL", cache_path=tmp_path / "e.sqlite", page_delay=0)
    m.client = Empty()
    stats = m.sweep()
    assert stats.pages == 1 and stats.written == 0
