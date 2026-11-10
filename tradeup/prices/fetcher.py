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
  halve immediately on a 429.

* A worker pool sharing one token bucket. `concurrency` workers pull names
  from a queue, so one item sleeping in backoff never blocks the others
  (the old fixed-size `gather` chunks did). The bucket's BURST is small (2
  tokens): after an idle period or a breaker pause the client resumes at
  the controlled rate instead of firing `rate` requests at once.

* A half-open circuit breaker. Repeated 429s in a row mean the IP is in
  penalty; a ban re-arms every time you hit it while banned. The breaker
  opens, waits, then lets exactly ONE probe through. If the probe is
  limited too, the cooldown doubles; after `max_trips` the run halts and
  reports, instead of hammering a banned IP all night.

* Honest accounting. Every name ends in exactly one bucket: ok, empty
  (Steam says it has no listings), or gave_up (retries exhausted, malformed
  body). Nothing disappears silently.

* A negative cache with a short TTL. `success:false` (or a 500 that repeats)
  means "no listings right now"; that is remembered for a day, not a week,
  because a transient 500 must not hide a real item.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, Optional

import httpx

APPID_CS2 = 730
PRICEOVERVIEW = "https://steamcommunity.com/market/priceoverview/"
USER_AGENT = "cs2-tradeup-lab/2.1 (academic research; low-rate)"


@dataclass
class FetchStats:
    requested: int = 0
    ok: int = 0
    empty: int = 0          # exists but no listings (success:false / repeated 500)
    cached: int = 0
    rate_limited: int = 0
    errors: int = 0
    timeouts: int = 0
    breaker_trips: int = 0
    gave_up: int = 0        # retries exhausted -- will be retried next run
    halted: bool = False    # breaker gave up on the whole run
    gave_up_names: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def per_min(self) -> float:
        e = self.elapsed / 60.0
        return (self.ok + self.empty) / e if e > 0 else 0.0


def parse_retry_after(value: Optional[str]) -> float:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).replace(",", "").replace(".", "").strip() or 0)
    except ValueError:
        return None


class AdaptiveLimiter:
    """AIMD rate controller with a shared async token bucket.

    `rate` is requests per minute and moves on its own:
      * every `probe_after` clean responses, add `increase` rpm
      * every 429, multiply by `decrease` and freeze increases briefly
    `burst` caps how many tokens can accumulate while idle.
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
        burst: float = 2.0,
    ):
        self.rate = float(start)
        self.floor, self.ceiling = floor, ceiling
        self.increase, self.decrease = increase, decrease
        self.probe_after = probe_after
        # One decrease per congestion window: several concurrent 429s are one
        # incident, not several.
        self.decrease_window = decrease_window
        self.burst = max(1.0, burst)
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
                    self.burst, self._tokens + (now - self._last) * self.rate / 60.0)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * 60.0 / self.rate
            await asyncio.sleep(min(wait, 5.0) + random.uniform(0, 0.15))

    def drain(self) -> None:
        """Forget accumulated credit (after a pause, resume at the base rate)."""
        self._tokens = 0.0
        self._last = time.monotonic()

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
        self._frozen_until = now + 90.0
        if now - self._last_decrease < self.decrease_window:
            return                      # same congestion event, already halved
        self._last_decrease = now
        self.rate = max(self.floor, self.rate * self.decrease)
        self.history.append((now, self.rate))


class CircuitBreaker:
    """Stops all traffic after repeated rate limits, then probes once.

    closed -> (trip_after consecutive 429s) -> open for `cooldown`
    open   -> one caller becomes the probe (half-open); the rest wait
    probe ok      -> closed
    probe limited -> open again with the cooldown doubled
    `max_trips` trips in one run -> `exhausted`; the caller should halt.
    """

    def __init__(self, trip_after: int = 5, cooldown: float = 300.0,
                 max_trips: int = 4):
        self.trip_after, self.cooldown = trip_after, cooldown
        self.base_cooldown = cooldown
        self.max_trips = max_trips
        self._consecutive = 0
        self.open_until = 0.0
        self.trips = 0
        self.half_open = False
        self._need_probe = False

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    @property
    def exhausted(self) -> bool:
        return self.trips >= self.max_trips

    def _trip(self) -> None:
        self.open_until = time.monotonic() + self.cooldown
        self.trips += 1
        self._consecutive = 0
        self._need_probe = True

    def record(self, rate_limited: bool) -> None:
        if self.half_open:
            # the single probe decides
            self.half_open = False
            if rate_limited:
                self.cooldown = self.cooldown * 2
                self._trip()
            else:
                self.cooldown = self.base_cooldown
                self._consecutive = 0
            return
        if rate_limited:
            self._consecutive += 1
            if self._consecutive >= self.trip_after:
                self._trip()
        else:
            self._consecutive = 0

    async def wait_if_open(self, notify: Optional[Callable[[str], None]] = None,
                           limiter: Optional[AdaptiveLimiter] = None) -> None:
        """Block while open; afterwards let exactly one caller probe.

        asyncio is single-threaded, so the check-and-set of `_need_probe`
        below cannot interleave with another coroutine (no await between).
        """
        while True:
            if self.exhausted:                 # run is over; never block on it
                return False
            if self.is_open:
                left = self.open_until - time.monotonic()
                if notify:
                    notify(f"circuit open: pausing {left:.0f}s to let the limit reset")
                await asyncio.sleep(max(left, 0) + random.uniform(0.5, 1.5))
                if limiter:
                    limiter.drain()
                continue
            if self.half_open:                 # someone else is probing
                await asyncio.sleep(0.5)
                continue
            if self._need_probe:
                self._need_probe = False
                self.half_open = True          # this caller is the probe
                return True
            return False

    def release_probe(self) -> None:
        """The probe ended without an answer (timeout, error): elect another."""
        if self.half_open:
            self.half_open = False
            self._need_probe = True


class AsyncPriceFetcher:
    """Concurrent, adaptive, cache-aware Steam price client."""

    def __init__(
        self,
        cache,                      # tradeup.prices.steam.PriceCache
        currency: int = 1,
        concurrency: int = 6,
        start_rate: float = 20.0,
        ttl_hours: float = 8.0,
        miss_ttl_hours: float = 24.0,
        timeout: float = 20.0,
        base_backoff: float = 8.0,
        max_backoff: float = 120.0,
        max_attempts: int = 4,
        server_error_delay: float = 2.0,
        breaker_trip_after: int = 5,
        breaker_cooldown: float = 300.0,
        breaker_max_trips: int = 4,
    ):
        self.cache = cache
        self.currency = currency
        self.concurrency = max(1, concurrency)
        self.ttl = ttl_hours * 3600
        self.miss_ttl = miss_ttl_hours * 3600
        self.limiter = AdaptiveLimiter(start=start_rate)
        self.breaker = CircuitBreaker(breaker_trip_after, breaker_cooldown,
                                      breaker_max_trips)
        self.stats = FetchStats()
        self.timeout = timeout
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.max_attempts = max_attempts
        self.server_error_delay = server_error_delay
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def pending(self, names: Iterable[str]) -> list[str]:
        """Names that are genuinely missing -- the only ones worth fetching."""
        out = []
        for n in dict.fromkeys(names):          # de-duplicate, keep order
            if self.cache.get(n, self.currency, self.ttl) is not None:
                self.stats.cached += 1
                continue
            if self.cache.is_known_miss(n, self.currency, self.miss_ttl):
                self.stats.cached += 1
                continue
            out.append(n)
        return out

    def _give_up(self, name: str) -> None:
        self.stats.gave_up += 1
        if len(self.stats.gave_up_names) < 200:
            self.stats.gave_up_names.append(name)

    async def _one(self, client, name: str,
                   notify: Optional[Callable[[str], None]]) -> None:
        from .steam import Quote, parse_price

        backoff = self.base_backoff
        server_errors = 0
        for _ in range(self.max_attempts):
            if self._stop:
                self._give_up(name)                   # interrupted, not finished
                return
            is_probe = await self.breaker.wait_if_open(notify, self.limiter)
            if self.breaker.exhausted:
                self.stats.halted = True
                self._stop = True
                self._give_up(name)
                return
            await self.limiter.acquire()
            try:
                r = await client.get(PRICEOVERVIEW, params={
                    "appid": APPID_CS2, "currency": self.currency,
                    "market_hash_name": name})
            except httpx.TimeoutException:
                self.stats.timeouts += 1
                if is_probe:
                    self.breaker.release_probe()
                await asyncio.sleep(backoff)          # no slot held while sleeping
                backoff = min(backoff * 2, self.max_backoff)
                continue
            except httpx.HTTPError:
                if is_probe:
                    self.breaker.release_probe()
                self.stats.errors += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self.stats.requested += 1

            if r.status_code == 429:
                self.stats.rate_limited += 1
                self.limiter.on_rate_limit()
                was_trips = self.breaker.trips
                self.breaker.record(True)
                if self.breaker.trips > was_trips:
                    self.stats.breaker_trips += 1
                ra = parse_retry_after(r.headers.get("Retry-After"))
                await asyncio.sleep(max(ra, backoff) + random.uniform(0, backoff * 0.25))
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self.breaker.record(False)

            if r.status_code == 500:
                # Steam answers 500 both for names with no listings AND for
                # transient failures. Retry once quickly; a repeat is a miss.
                server_errors += 1
                if server_errors >= 2:
                    self.cache.put_miss(name, self.currency)
                    self.stats.empty += 1
                    self.limiter.on_success()
                    return
                await asyncio.sleep(self.server_error_delay)
                continue
            if r.status_code != 200:
                self.stats.errors += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            try:
                data = r.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                self.stats.errors += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            self.limiter.on_success()
            if not data.get("success"):
                self.cache.put_miss(name, self.currency)
                self.stats.empty += 1
                return

            lowest = parse_price(data.get("lowest_price"))
            median = parse_price(data.get("median_price"))
            if lowest is None and median is None:
                # success but no usable price: not worth caching as a quote
                self.cache.put_miss(name, self.currency)
                self.stats.empty += 1
                return
            self.cache.put(Quote(name, lowest, median, _to_int(data.get("volume")),
                                 time.time()), self.currency)
            self.stats.ok += 1
            return
        self._give_up(name)

    async def fetch(self, names: Iterable[str],
                    progress: Optional[Callable[[FetchStats, str], None]] = None
                    ) -> FetchStats:
        todo = self.pending(names)
        if not todo:
            return self.stats

        headers = {"User-Agent": USER_AGENT}
        limits = httpx.Limits(max_connections=self.concurrency * 2,
                              max_keepalive_connections=self.concurrency)
        notify = (lambda m: progress(self.stats, m)) if progress else None
        queue: asyncio.Queue[str] = asyncio.Queue()
        for n in todo:
            queue.put_nowait(n)
        done = 0

        async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers=headers, limits=limits) as client:

            async def worker() -> None:
                nonlocal done
                while not self._stop:
                    try:
                        n = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await self._one(client, n, notify)
                    except Exception:            # never lose a name silently
                        self.breaker.release_probe()
                        self.stats.errors += 1
                        self._give_up(n)
                    done += 1
                    if progress and (done % 25 == 0 or done == len(todo)):
                        progress(self.stats,
                                 f"{done}/{len(todo)} · {self.limiter.rate:.0f}/min "
                                 f"· {self.stats.per_min:.0f} actual")

            await asyncio.gather(*(worker() for _ in range(self.concurrency)))

        # anything never attempted because the run halted is reported too
        while not queue.empty():
            self._give_up(queue.get_nowait())
        flush = getattr(self.cache, "flush", None)
        if flush:
            flush()
        return self.stats


def fetch_sync(cache, names, currency: int = 1, **kw) -> FetchStats:
    """Blocking wrapper, for the CLI."""
    progress = kw.pop("progress", None)
    f = AsyncPriceFetcher(cache, currency=currency, **kw)
    return asyncio.run(f.fetch(names, progress))
