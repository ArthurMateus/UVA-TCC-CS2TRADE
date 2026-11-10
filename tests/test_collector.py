"""Unattended collector: rounds, back-off, incremental Supabase push."""
from __future__ import annotations

import time

import pytest

from tradeup.collector import Collector, PushLedger, plan_tiers
from tradeup.prices.bulk import BulkStats
from tradeup.prices.steam import PriceBook, PriceCache, Quote


def test_plan_merges_inputs_and_outputs():
    assert plan_tiers([2, 3]) == [(2, ("normal", "souvenir")),
                                  (3, ("normal", "souvenir")),
                                  (4, ("normal",))]
    # Covert outputs are knives/gloves: no weapon-skin tier to sweep
    assert plan_tiers([5], souvenir=False) == [(5, ("normal",))]


class FakeMarket:
    """Writes one quote per tier per call; can simulate a rate-limited tier."""
    prices = {}
    fail_tier = None
    calls = []

    def __init__(self, currency, cache_path, page_delay):
        self.cache = PriceCache(cache_path)
        self.cur = 7
        self.stats = BulkStats()

    def sweep_tier(self, tier, quals, max_pages=0, start_at=0, on_checkpoint=None, progress=None):
        FakeMarket.calls.append((tier, start_at))
        if tier == FakeMarket.fail_tier:
            self.stats.rate_limited += 1
            self.stats.incomplete = True
            return self.stats
        price = FakeMarket.prices.get(tier, 1.0)
        self.cache.put(Quote(f"T{tier} (FN)", price, None, None, time.time(), 50), self.cur)
        self.stats.pages += 1
        self.stats.written += 1
        if on_checkpoint:
            on_checkpoint(10, 10)
        return self.stats

    def close(self):
        self.cache.close()


class FakeStore:
    pushed = []
    fail = False

    def __init__(self, dsn): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

    def put_many(self, quotes, currency):
        if FakeStore.fail:
            raise ConnectionError("db down")
        FakeStore.pushed.append([q.market_hash_name for q in quotes])
        return len(quotes)


@pytest.fixture(autouse=True)
def reset():
    FakeMarket.prices, FakeMarket.fail_tier, FakeMarket.calls = {}, None, []
    FakeStore.pushed, FakeStore.fail = [], False


def _collector(tmp_path, **kw):
    sleeps = []
    cps = {}
    c = Collector([2], currency="BRL", cache_path=tmp_path / "c.sqlite",
                  snapshot_path=tmp_path / "snap.json",
                  checkpoint=lambda k, s, t: cps.__setitem__(k, {"next_start": s, "total": t}),
                  load_checkpoints=lambda: dict(cps),
                  dsn="postgres://fake", store_factory=FakeStore,
                  market_factory=FakeMarket, sleep=sleeps.append, say=lambda m: None,
                  interval=600, **kw)
    return c, sleeps


def test_rounds_export_and_push_only_changes(tmp_path):
    c, sleeps = _collector(tmp_path)
    c.run_forever(max_rounds=2)
    # round 1 pushes both tiers; round 2 sees identical prices -> pushes nothing
    assert FakeStore.pushed == [["T2 (FN)", "T3 (FN)"]] or \
        sorted(FakeStore.pushed[0]) == ["T2 (FN)", "T3 (FN)"]
    assert len(FakeStore.pushed) == 1
    assert sleeps == [600]
    assert PriceBook.load(tmp_path / "snap.json").currency == 7

    FakeMarket.prices = {3: 2.5}                       # one price moves
    c.run_forever(max_rounds=1)
    assert FakeStore.pushed[-1] == ["T3 (FN)"]


def test_finished_tiers_restart_from_the_top(tmp_path):
    c, _ = _collector(tmp_path)
    c.run_forever(max_rounds=2)
    assert all(start == 0 for _, start in FakeMarket.calls)


def test_rate_limited_round_backs_off_and_continues(tmp_path):
    FakeMarket.fail_tier = 2
    c, sleeps = _collector(tmp_path)
    res = c.run_forever(max_rounds=3)
    assert all(r.incomplete for r in res)
    assert sleeps == [1200, 2400]                       # doubling back-off
    FakeMarket.fail_tier = None
    res = c.run_forever(max_rounds=2)
    assert not res[-1].incomplete


def test_failed_push_is_retried_next_round(tmp_path):
    FakeStore.fail = True
    c, _ = _collector(tmp_path)
    r = c.run_forever(max_rounds=1)[0]
    assert r.push_error and "db down" in r.push_error
    FakeStore.fail = False
    c.run_forever(max_rounds=1)
    assert sorted(FakeStore.pushed[-1]) == ["T2 (FN)", "T3 (FN)"]   # nothing was lost


def test_ledger_heartbeat_and_listing_threshold(tmp_path):
    cache = PriceCache(tmp_path / "l.sqlite")
    led = PushLedger(cache.db)
    cache.put(Quote("A", 1.0, None, None, 1000.0, 100), 7); cache.flush()
    first = led.changed(7)
    led.mark(first, 7)
    cache.put(Quote("A", 1.0, None, None, 2000.0, 105), 7); cache.flush()   # +5%: noise
    assert led.changed(7, heartbeat=1e12) == []
    cache.put(Quote("A", 1.0, None, None, 3000.0, 150), 7); cache.flush()   # +50%
    assert [q.market_hash_name for q in led.changed(7, heartbeat=1e12)] == ["A"]
    cache.close()


def test_without_dsn_it_collects_locally(tmp_path):
    c, _ = _collector(tmp_path)
    c.dsn = None
    r = c.run_forever(max_rounds=1)[0]
    assert r.exported == 2 and r.pushed == 0 and FakeStore.pushed == []


def test_only_one_collector_at_a_time(tmp_path):
    from tradeup.collector import AlreadyRunning, InstanceLock
    first = InstanceLock(tmp_path / "collect.lock").acquire()
    with pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "collect.lock").acquire()
    first.release()
    InstanceLock(tmp_path / "collect.lock").acquire().release()   # free again


def test_busy_database_waits_instead_of_failing(tmp_path):
    """Two processes on one SQLite file: the second write waits for the first."""
    import sqlite3, threading
    path = tmp_path / "busy.sqlite"
    PriceCache(path).close()
    holder = sqlite3.connect(str(path), check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")                  # hold the write lock
    t = threading.Timer(1.0, holder.commit)
    t.start()
    c = PriceCache(path)
    c.put(Quote("A (FN)", 1.0, None, None, time.time(), 3), 7)
    c.flush()                                          # would raise "database is locked"
    assert c.get("A (FN)", 7, 1e9).lowest == 1.0
    c.close(); t.join(); holder.close()


def test_disk_full_keeps_the_previous_file(tmp_path, monkeypatch):
    import errno, os
    from tradeup.fsutil import atomic_write_text
    p = tmp_path / "cp.json"
    atomic_write_text(p, '{"ok": 1}')

    def full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError):
        atomic_write_text(p, '{"new": 2}')
    assert p.read_text(encoding="utf-8") == '{"ok": 1}'     # not truncated
    assert [x.name for x in tmp_path.iterdir()] == ["cp.json"]   # no temp left behind


def test_checkpoint_failure_does_not_abort_the_round(tmp_path):
    c, _ = _collector(tmp_path)

    def broken(k, s, t):
        raise OSError(28, "No space left on device")
    c.checkpoint = broken
    r = c.run_forever(max_rounds=1)[0]
    assert not r.incomplete and r.quotes == 2 and r.pushed == 2


def test_low_disk_skips_the_round_and_backs_off(tmp_path):
    c, sleeps = _collector(tmp_path, free_mb=lambda p: 50.0)
    r = c.run_forever(max_rounds=2)
    assert all(x.incomplete and x.quotes == 0 for x in r)
    assert FakeMarket.calls == [] and sleeps == [1200]
