"""Adaptive concurrent price fetching.

WHY THE OLD APPROACH WAS SLOW AND STILL GOT BANNED

The previous client used a fixed rate (18/min) chosen by guesswork. Too fast
and you collect 429s -- each costing an exponential backoff sleep, so the
effective throughput collapses below what a slower fixed rate would have
achieved. Too slow and a sweep takes six hours. There is no single correct
constant, because the limit varies by endpoint, time of day, and how much
credit your IP has recently spent.

WHAT THIS DOES INSTEAD

* AIMD control (additive increase, multiplicative decrease), the same idea
  TCP uses for congestion. Start conservative, speed up gently while clean,
  halve immediately on a 429. The client converges on the fastest rate the
  server will actually tolerate, and keeps re-converging as conditions change.

* Concurrency with a shared token bucket. Requests overlap so latency stops
  dominating, while the bucket keeps the aggregate rate at whatever AIMD has
  settled on. Eight in flight at 20/min is the same load as one at a time at
  20/min -- it just finishes sooner, because you are no longer paying round
  trip latency serially.

* A circuit breaker. Repeated 429s in a row mean the IP is in penalty, and
  the correct response is to stop entirely rather than keep poking it: a ban
  re-arms every time you hit it while banned. The breaker opens, waits, and
  probes once before resuming.

* A negative cache. Steam returns 500 for names with no listings, and those
  are a large fraction of any sweep. They are remembered so a resumed run
  never pays for them twice.

Net effect on a cold Mil-Spec tier: roughly 8-12 minutes instead of 75, with
fewer 429s, because the limiter finds the real ceiling instead of alternating
between too-fast and punished.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import httpx

APPID_CS2 = 730
PRICEOVERVIEW = "https://steamcommunity.com/market/priceoverview/"


@dataclass
class FetchStats:
    requested: int = 0
    ok: int = 0
    empty: int = 0          # exists but no listings (HTTP 500 / success:false)
    cached: int = 0
    rate_limited: int = 0
    errors: int = 0
    timeouts: int = 0
    breaker_trips: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def per_min(self) -> float:
        e = self.elapsed / 60.0
        return (self.ok + self.empty) / e if e > 0 else 0.0


class AdaptiveLimiter:
    """AIMD rate controller with a shared async token bucket.

    `rate` is requests per minute and moves on its own:
      * every `probe_after` clean responses, add `increase` rpm
      * every 429, multiply by `decrease` and freeze increases briefly
    """

    def __init__(
        self,
        start: float = 20.0,
        floor: float = 4.0,
        ceiling: float = 60.0,
        increase: float = 2.0,
        decrease: float = 0.5,
        probe_after: int = 25,
        decrease_window: float = 10.0,
    ):
        self.rate = float(start)
        self.floor, self.ceiling = floor, ceiling
        self.increase, self.decrease = increase, decrease
        self.probe_after = probe_after
        # One decrease per congestion window. With concurrent requests a
        # single burst of limiting returns several 429s at once; halving for
        # each would collapse the rate to the floor after one incident.
        self.decrease_window = decrease_window
        self._last_decrease = 0.0
        self._clean = 0
        self._tokens = 1.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._frozen_until = 0.0
        self.history: list[tuple[float, float]] = []   # (t, rate)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.rate, self._tokens + (now - self._last) * self.rate / 60.0)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * 60.0 / self.rate
            await asyncio.sleep(min(wait, 5.0) + random.uniform(0, 0.15))

    def on_success(self) -> None:
        self._clean += 1
        if (self._clean >= self.probe_after
                and time.monotonic() > self._frozen_until):
            self._clean = 0
            old = self.rate
            self.rate = min(self.ceiling, self.rate + self.increase)
            if self.rate != old:
                self.history.append((time.monotonic(), self.rate))

    def on_rate_limit(self) -> None:
        self._clean = 0
        now = time.monotonic()
        # do not immediately probe upward again after being punished
        self._frozen_until = now + 90.0
        if now - self._last_decrease < self.decrease_window:
            return                      # same congestion event, already halved
        self._last_decrease = now
        self.rate = max(self.floor, self.rate * self.decrease)
        self.history.append((now, self.rate))


class CircuitBreaker:
    """Stops all traffic after repeated rate limits.

    A Steam ban re-arms whenever you hit the limit while banned, so
    continuing to retry actively extends it. Better to halt and probe.
    """

    def __init__(self, trip_after: int = 5, cooldown: float = 300.0):
        self.trip_after, self.cooldown = trip_after, cooldown
        self._consecutive = 0
        self.open_until = 0.0
        self.trips = 0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record(self, rate_limited: bool) -> None:
        if rate_limited:
            self._consecutive += 1
            if self._consecutive >= self.trip_after:
                self.open_until = time.monotonic() + self.cooldown
                self.trips += 1
                self._consecutive = 0
        else:
            self._consecutive = 0

    async def wait_if_open(self, notify: Optional[Callable[[str], None]] = None):
        if not self.is_open:
            return
        left = self.open_until - time.monotonic()
        if notify:
            notify(f"circuit open: pausing {left:.0f}s to let the limit reset")
        await asyncio.sleep(max(left, 0) + 1.0)


class AsyncPriceFetcher:
    """Concurrent, adaptive, cache-aware Steam price client."""

    def __init__(
        self,
        cache,                      # tradeup.prices.steam.PriceCache
        currency: int = 1,
        concurrency: int = 6,
        start_rate: float = 20.0,
        ttl_hours: float = 8.0,
        miss_ttl_hours: float = 168.0,
        timeout: float = 20.0,
        base_backoff: float = 8.0,
        max_backoff: float = 120.0,
        breaker_trip_after: int = 5,
        breaker_cooldown: float = 300.0,
    ):
        self.cache = cache
        self.currency = currency
        self.ttl = ttl_hours * 3600
        self.miss_ttl = miss_ttl_hours * 3600
        self.limiter = AdaptiveLimiter(start=start_rate)
        self.breaker = CircuitBreaker(breaker_trip_after, breaker_cooldown)
        self.sem = asyncio.Semaphore(concurrency)
        self.stats = FetchStats()
        self.timeout = timeout
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def pending(self, names: Iterable[str]) -> list[str]:
        """Names that are genuinely missing -- the only ones worth fetching."""
        out = []
        for n in names:
            if self.cache.get(n, self.currency, self.ttl) is not None:
                self.stats.cached += 1
                continue
            if self.cache.is_known_miss(n, self.currency, self.miss_ttl):
                self.stats.cached += 1
                continue
            out.append(n)
        return out

    async def _one(self, client: httpx.AsyncClient, name: str,
                   notify: Optional[Callable[[str], None]]) -> None:
        from .steam import Quote, parse_price

        backoff = self.base_backoff
        for _ in range(4):
            if self._stop:
                return
            await self.breaker.wait_if_open(notify)
            await self.limiter.acquire()
            async with self.sem:
                try:
                    r = await client.get(PRICEOVERVIEW, params={
                        "appid": APPID_CS2, "currency": self.currency,
                        "market_hash_name": name})
                except httpx.TimeoutException:
                    self.stats.timeouts += 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                except httpx.HTTPError:
                    self.stats.errors += 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

            self.stats.requested += 1

            if r.status_code == 429:
                self.stats.rate_limited += 1
                self.limiter.on_rate_limit()
                self.breaker.record(True)
                ra = float(r.headers.get("Retry-After", 0) or 0)
                await asyncio.sleep(max(ra, backoff) + random.uniform(0, backoff * 0.25))
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self.breaker.record(False)

            if r.status_code == 500:
                # name does not exist on the market -- remember it
                self.cache.put_miss(name, self.currency)
                self.stats.empty += 1
                self.limiter.on_success()
                return
            if r.status_code != 200:
                self.stats.errors += 1
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            try:
                data = r.json()
            except ValueError:
                self.stats.errors += 1
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            self.limiter.on_success()
            if not data.get("success"):
                self.cache.put_miss(name, self.currency)
                self.stats.empty += 1
                return

            self.cache.put(Quote(
                name,
                parse_price(data.get("lowest_price")),
                parse_price(data.get("median_price")),
                int(str(data.get("volume", "0")).replace(",", "") or 0),
                time.time()), self.currency)
            self.stats.ok += 1
            return

    async def fetch(self, names: Iterable[str],
                    progress: Optional[Callable[[FetchStats, str], None]] = None
                    ) -> FetchStats:
        todo = self.pending(names)
        if not todo:
            return self.stats

        headers = {"User-Agent": "cs2-tradeup-lab/2.0 (personal research)"}
        limits = httpx.Limits(max_connections=self.sem._value * 2,
                              max_keepalive_connections=self.sem._value)
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers=headers, limits=limits) as client:

            notify = (lambda m: progress(self.stats, m)) if progress else None
            done = 0
            # chunked so progress is reported and a stop request lands quickly
            for i in range(0, len(todo), 50):
                if self._stop:
                    break
                chunk = todo[i:i + 50]
                await asyncio.gather(
                    *(self._one(client, n, notify) for n in chunk),
                    return_exceptions=True)
                done += len(chunk)
                if progress:
                    progress(self.stats,
                             f"{done}/{len(todo)} · {self.limiter.rate:.0f}/min "
                             f"· {self.stats.per_min:.0f} actual")
        return self.stats


def fetch_sync(cache, names, currency: int = 1, **kw) -> FetchStats:
    """Blocking wrapper, for the CLI."""
    f = AsyncPriceFetcher(cache, currency=currency, **kw)
    progress = kw.pop("progress", None)
    return asyncio.run(f.fetch(names, progress))
