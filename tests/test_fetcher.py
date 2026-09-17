"""Adaptive rate limiting and the negative cache."""
from __future__ import annotations
import asyncio, time
import httpx, pytest
from tradeup.prices.fetcher import AdaptiveLimiter, AsyncPriceFetcher, CircuitBreaker
from tradeup.prices.steam import PriceCache

def test_limiter_paces_requests():
    async def go():
        lim = AdaptiveLimiter(start=600.0, ceiling=600.0)
        t0 = time.monotonic()
        for _ in range(20): await lim.acquire()
        return time.monotonic() - t0
    el = asyncio.run(go())
    assert 1.2 < el < 4.0          # 20 at 600/min ~ 2s

def test_additive_increase_after_clean_responses():
    lim = AdaptiveLimiter(start=60.0, ceiling=200, probe_after=3, increase=10)
    for _ in range(9): lim.on_success()
    assert lim.rate == pytest.approx(90.0)

def test_multiplicative_decrease_on_limit():
    lim = AdaptiveLimiter(start=60.0, floor=10)
    lim.on_rate_limit()
    assert lim.rate == pytest.approx(30.0)

def test_one_decrease_per_congestion_window():
    """Concurrent requests return several 429s for ONE incident.

    Halving per response would collapse to the floor after one burst.
    """
    lim = AdaptiveLimiter(start=800.0, floor=20, decrease_window=10.0)
    for _ in range(6): lim.on_rate_limit()
    assert lim.rate == pytest.approx(400.0)      # exactly one halving
    lim._last_decrease -= 11                      # a separate, later incident
    lim.on_rate_limit()
    assert lim.rate == pytest.approx(200.0)

def test_rate_is_frozen_after_punishment():
    lim = AdaptiveLimiter(start=100.0, probe_after=2, increase=10)
    lim.on_rate_limit()
    r = lim.rate
    for _ in range(50): lim.on_success()
    assert lim.rate == pytest.approx(r)           # no probing while frozen

def test_rate_never_leaves_its_bounds():
    lim = AdaptiveLimiter(start=30.0, floor=10, ceiling=40, probe_after=1, increase=100)
    for _ in range(10): lim.on_success()
    assert lim.rate <= 40
    for _ in range(20):
        lim._last_decrease = 0
        lim.on_rate_limit()
    assert lim.rate >= 10

def test_breaker_opens_after_consecutive_limits():
    b = CircuitBreaker(trip_after=3, cooldown=5)
    for _ in range(3): b.record(True)
    assert b.is_open and b.trips == 1

def test_breaker_streak_resets_on_success():
    b = CircuitBreaker(trip_after=3)
    b.record(True); b.record(True); b.record(False); b.record(True); b.record(True)
    assert not b.is_open

def test_negative_cache_skips_known_dead_names(tmp_path):
    c = PriceCache(tmp_path/"n.sqlite")
    c.put_miss("Ghost (Factory New)", 7)
    f = AsyncPriceFetcher(c, currency=7)
    assert f.pending(["Ghost (Factory New)", "Real (Factory New)"]) == \
        ["Real (Factory New)"]
    assert f.stats.cached == 1

def test_fetch_stores_quotes_and_misses(tmp_path, monkeypatch):
    class Fake:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            req = httpx.Request("GET", url)
            if "Ghost" in params["market_hash_name"]:
                return httpx.Response(500, text="", request=req)
            return httpx.Response(200, json={"success": True,
                "lowest_price": "R$ 3,41", "median_price": "R$ 3,60",
                "volume": "1,204"}, request=req)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: Fake())

    cache = PriceCache(tmp_path/"f.sqlite")
    f = AsyncPriceFetcher(cache, currency=7, concurrency=4, start_rate=6000.0)
    f.limiter.ceiling = 6000.0
    st = asyncio.run(f.fetch([f"I{i} (FN)" for i in range(10)] + ["Ghost (FN)"]))
    assert st.ok == 10 and st.empty == 1
    q = cache.get("I3 (FN)", 7, 9999)
    assert (q.lowest, q.median, q.volume) == (3.41, 3.60, 1204)   # BRL parsed
    assert cache.is_known_miss("Ghost (FN)", 7, 9999)
    # a resumed run has nothing left to do
    assert AsyncPriceFetcher(cache, currency=7).pending(["I3 (FN)", "Ghost (FN)"]) == []
