"""Unattended price collection: one command, runs until you stop it.

    tradeup collect --currency BRL

Each ROUND:
  1. sweeps every requested rarity tier with Steam's paginated search,
     filtered server-side by rarity/quality tags (resumable checkpoints);
  2. exports the local snapshot (the web UI / CLI read it offline);
  3. pushes to Supabase ONLY the quotes that changed since the last push
     (price, median/volume, or listings by more than `listing_change`), plus
     a daily heartbeat row per item, so the history table grows with real
     market movement instead of copying 10k identical rows every round;
  4. sleeps `interval` and starts again.

Failure policy: a round that was rate-limited or stopped early does not
kill the process. The next sleep doubles (capped at `max_backoff`) and the
sweep resumes from its checkpoint. A failed Supabase push is retried next
round: nothing is marked as pushed until the database commit succeeds.
Ctrl+C stops cleanly at any point; everything fetched is already on disk.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from .prices.bulk import RARITY_TAG, BulkMarket
from .prices.steam import CURRENCY, PriceCache, Quote

log = logging.getLogger("tradeup.collect")

DAY = 86400.0


@dataclass
class RoundResult:
    number: int
    started: float
    seconds: float = 0.0
    pages: int = 0
    quotes: int = 0
    rate_limited: int = 0
    incomplete: bool = False
    exported: int = 0
    pushed: int = 0
    push_error: Optional[str] = None
    tiers: list = field(default_factory=list)


def plan_tiers(input_tiers: list[int], souvenir: bool = True) -> list[tuple[int, tuple]]:
    """Tiers to sweep for these INPUT tiers: each input tier (normal +
    souvenir) and its output tier (normal). Merged, sorted, de-duplicated."""
    want: dict[int, set] = {}
    for r in input_tiers:
        want.setdefault(r, set()).add("normal")
        if souvenir:
            want[r].add("souvenir")
        nxt = r + 1
        if nxt in RARITY_TAG:              # Covert -> knives: not a weapon-skin tag
            want.setdefault(nxt, set()).add("normal")
    order = ("normal", "souvenir", "stattrak")
    return [(t, tuple(q for q in order if q in qs)) for t, qs in sorted(want.items())]


class PushLedger:
    """Remembers what was last sent to Supabase, in the local SQLite cache."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS pushed(name TEXT, currency INT,
            lowest REAL, median REAL, volume INT, listings INT, ts REAL,
            PRIMARY KEY(name, currency))""")
        self.db.commit()

    def changed(self, currency: int, listing_change: float = 0.10,
                heartbeat: float = DAY) -> list[Quote]:
        now = time.time()
        rows = self.db.execute(
            """SELECT p.name, p.lowest, p.median, p.volume, p.ts, p.listings,
                      s.lowest, s.median, s.volume, s.listings, s.ts
               FROM prices p LEFT JOIN pushed s
                 ON s.name = p.name AND s.currency = p.currency
               WHERE p.currency = ?""", (currency,)).fetchall()
        out = []
        for (n, lo, me, vo, ts, li, plo, pme, pvo, pli, pts) in rows:
            if pts is None:
                out.append(Quote(n, lo, me, vo, ts, li)); continue
            if ts <= pts:
                continue                         # nothing newer than what we sent
            moved = (lo != plo or me != pme or vo != pvo
                     or _rel_change(li, pli) > listing_change
                     or now - pts >= heartbeat)
            if moved:
                out.append(Quote(n, lo, me, vo, ts, li))
        return out

    def mark(self, quotes: list[Quote], currency: int) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO pushed VALUES (?,?,?,?,?,?,?)",
            [(q.market_hash_name, currency, q.lowest, q.median, q.volume,
              q.listings, q.fetched_at) for q in quotes])
        self.db.commit()


def _rel_change(a: Optional[int], b: Optional[int]) -> float:
    if a == b:
        return 0.0
    if a is None or b is None:
        return 1.0
    return abs(a - b) / max(1, b)


LOCK_OFFSET = 4096


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """One collector per data folder.

    An OS-level lock on `path` (msvcrt on Windows, fcntl elsewhere): the OS
    drops it when the process exits or crashes, so a stale lock file can
    never block the next start. Two collectors writing the same SQLite file
    is what produced "database is locked" in the first place.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "InstanceLock":
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(LOCK_OFFSET)          # lock a byte past the PID text, so
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # it stays readable
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.seek(0)
                other = fh.read(32).strip(" \r\n\t\x00") or "?"
            except OSError:
                other = "?"
            fh.close()
            raise AlreadyRunning(f"another collector is already running (PID {other})")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        import os
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(LOCK_OFFSET)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self): return self.acquire()
    def __exit__(self, *a): self.release()


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    # a full disk must not spray "--- Logging error ---" tracebacks or stop
    # the loop: a failed log line is simply dropped
    logging.raiseExceptions = False
    h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


class Collector:
    def __init__(
        self,
        input_tiers: list[int],
        currency: str = "BRL",
        cache_path: Path = Path("data/prices.sqlite"),
        snapshot_path: Optional[Path] = None,
        checkpoint: Optional[Callable[[str, int, Optional[int]], None]] = None,
        load_checkpoints: Optional[Callable[[], dict]] = None,
        dsn: Optional[str] = None,
        souvenir: bool = True,
        page_delay: float = 6.0,
        interval: float = 3600.0,
        max_backoff: float = 4 * 3600.0,
        listing_change: float = 0.10,
        max_pages: int = 0,
        market_factory=BulkMarket,
        store_factory=None,
        push_catalogue: Optional[Callable[[object], int]] = None,
        sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = print,
        min_free_mb: float = 200.0,
        free_mb: Optional[Callable[[Path], float]] = None,
    ):
        self.currency_code = currency.upper()
        self.currency = CURRENCY[self.currency_code]
        self.plan = plan_tiers(input_tiers, souvenir)
        self.cache_path = Path(cache_path)
        self.snapshot_path = snapshot_path
        self.checkpoint = checkpoint
        self.load_checkpoints = load_checkpoints or (lambda: {})
        self.dsn = dsn
        self.page_delay = page_delay
        self.interval = interval
        self.max_backoff = max_backoff
        self.listing_change = listing_change
        self.max_pages = max_pages
        self.market_factory = market_factory
        self.store_factory = store_factory
        self.push_catalogue = push_catalogue
        self.sleep = sleep
        self.say = say
        self.min_free_mb = min_free_mb
        if free_mb is None:
            from .fsutil import free_mb
        self.free_mb = free_mb
        self._checkpoint_warned = False
        self.rounds = 0
        self._stop = False
        self._catalogue_pushed = False

    def stop(self) -> None:
        self._stop = True

    def _info(self, msg: str) -> None:
        log.info(msg)
        self.say(msg)

    # -- one round -------------------------------------------------------
    def sweep(self, res: RoundResult) -> None:
        cps = self.load_checkpoints()
        market = self.market_factory(currency=self.currency_code,
                                     cache_path=self.cache_path,
                                     page_delay=self.page_delay)
        try:
            for tier, quals in self.plan:
                if self._stop:
                    break
                key = f"steam_search:{self.currency}:{tier}:{'+'.join(quals)}"
                cp = cps.get(key) or {}
                start = int(cp.get("next_start") or 0)
                if cp.get("total") and start >= int(cp["total"]):
                    start = 0
                before = (market.stats.pages, market.stats.written)
                market.stats.incomplete = False
                market.sweep_tier(
                    tier, quals, max_pages=self.max_pages, start_at=start,
                    on_checkpoint=(lambda s, t, k=key: self._safe_checkpoint(k, s, t))
                    if self.checkpoint else None)
                done = not market.stats.incomplete
                res.tiers.append({"tier": tier, "qualities": quals, "from": start,
                                  "pages": market.stats.pages - before[0],
                                  "quotes": market.stats.written - before[1],
                                  "complete": done})
                res.incomplete |= not done
                self._info(f"  tier {tier} {'+'.join(quals)}: "
                           f"{market.stats.pages - before[0]} pages, "
                           f"{market.stats.written - before[1]} quotes"
                           + ("" if done else " (incomplete, will resume)"))
                if market.stats.rate_limited and not done:
                    break                      # stop hammering; back off as a whole
        finally:
            res.pages = market.stats.pages
            res.quotes = market.stats.written
            res.rate_limited = market.stats.rate_limited
            market.close()

    def _safe_checkpoint(self, key: str, start: int, total: Optional[int]) -> None:
        """A checkpoint that cannot be saved (disk full, AV lock) only costs
        resume precision; it must never abort a sweep that is going fine."""
        try:
            self.checkpoint(key, start, total)
        except OSError as e:
            if not self._checkpoint_warned:
                self._checkpoint_warned = True
                self._info(f"  warning: could not save checkpoint ({e}); continuing")

    def export_and_push(self, res: RoundResult) -> None:
        with PriceCache(self.cache_path) as cache:
            if self.snapshot_path:
                try:
                    res.exported = cache.export(self.snapshot_path, self.currency)
                except OSError as e:               # keep going: the push can still work
                    self._info(f"  warning: snapshot not written ({e})")
            if not self.dsn or self.store_factory is None:
                return
            ledger = PushLedger(cache.db)
            todo = ledger.changed(self.currency, self.listing_change)
            try:
                with self.store_factory(self.dsn) as store:
                    if not self._catalogue_pushed and self.push_catalogue:
                        n = self.push_catalogue(store)      # skins/collections, once
                        self._catalogue_pushed = True
                        self._info(f"  catalogue: {n} skins upserted")
                    res.pushed = store.put_many(todo, self.currency) if todo else 0
                ledger.mark(todo, self.currency)       # only after a successful commit
            except Exception as e:                     # retried next round
                res.push_error = f"{type(e).__name__}: {e}"

    def run_round(self) -> RoundResult:
        self.rounds += 1
        res = RoundResult(self.rounds, time.time())
        self._info(f"round {res.number} started ({len(self.plan)} tiers, "
                   f"{self.currency_code})")
        free = self.free_mb(self.cache_path)
        if free < self.min_free_mb:
            res.incomplete = True
            res.seconds = 0.0
            self._info(f"round {res.number} skipped: only {free:.0f} MB free on disk "
                       f"(need {self.min_free_mb:.0f} MB). Free some space; retrying later.")
            return res
        self._checkpoint_warned = False
        try:
            self.sweep(res)
        except Exception as e:                         # never kill the loop
            res.incomplete = True
            log.exception("sweep failed")
            self.say(f"  sweep failed: {type(e).__name__}: {e}")
        try:
            self.export_and_push(res)
        except Exception as e:
            res.push_error = f"{type(e).__name__}: {e}"
        res.seconds = time.time() - res.started
        self._info(f"round {res.number} done in {res.seconds/60:.1f} min: "
                   f"{res.quotes} quotes, {res.rate_limited} rate-limits, "
                   f"snapshot {res.exported}, pushed {res.pushed}"
                   + (f", PUSH FAILED ({res.push_error})" if res.push_error else ""))
        return res

    # -- the loop ----------------------------------------------------------
    def next_sleep(self, res: RoundResult, streak: int) -> float:
        if res.incomplete:
            return min(self.max_backoff, self.interval * (2 ** max(0, streak)))
        return self.interval

    def run_forever(self, max_rounds: int = 0) -> list[RoundResult]:
        results: list[RoundResult] = []
        streak = 0
        while not self._stop:
            res = self.run_round()
            results.append(res)
            streak = streak + 1 if res.incomplete else 0
            if max_rounds and len(results) >= max_rounds:
                break
            wait = self.next_sleep(res, streak)
            self._info(f"sleeping {wait/60:.0f} min "
                       + ("(backing off after an incomplete round)" if res.incomplete else ""))
            self.sleep(wait)
        return results
