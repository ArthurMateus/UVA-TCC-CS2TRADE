"""Bulk price ingestion via Steam's paginated market search.

`/market/priceoverview/` returns ONE item per request. A tier sweep is
thousands of names against an endpoint whose rate limit is enforced per IP
and whose ban can last hours (and re-arms while you are banned). You cannot
win that race by tuning the delay.

`/market/search/render/?norender=1` returns a page of items per request --
the same endpoint the market web page itself calls.

IMPORTANT -- PAGE SIZE IS NOT GUARANTEED
Steam does not always honour `count`. It commonly caps the page at 10 rows
regardless of what you ask for, and will silently degrade under load. The
first version of this module assumed it got what it asked for and advanced
`start` by the REQUESTED size, which skipped every item between the rows it
actually received. That is a data-correctness bug, not just a slow sweep.

So: `start` always advances by the number of rows actually parsed, the
effective page size is detected from the first page, and the ETA is
recomputed from that. If Steam gives 10 rows per request, a full-market
sweep is ~3.5k requests and is NOT worth it -- use `queries=` to fetch only
the weapons you care about, or fall back to per-item pricing for a
shortlist.

Trade-off vs priceoverview, stated plainly:
  * search/render gives `sell_price` (lowest listing) and `sell_listings`
    (how many are listed right now).
  * priceoverview gives `lowest_price`, `median_price` and `volume`
    (units SOLD in 24h).
Bulk has no true median and no sales volume; `sell_listings` is a supply
count, not a liquidity measure. Confirm a shortlist before spending money.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Union

import httpx

from .steam import CURRENCY, ClientState, PriceCache, Quote, parse_price

APPID_CS2 = 730
SEARCH_URL = "https://steamcommunity.com/market/search/render/"

# What we ask for. Steam frequently gives less -- see the module docstring.
REQUESTED_PAGE_SIZE = 100

# If the effective page size is at or below this, a full sweep is impractical
# and the caller should be told to narrow the search instead.
SMALL_PAGE_WARN = 20

# Steam's internal tag names for weapon-skin rarities (verified against the
# live endpoint: "tag_Rarity_Mil_Spec_Weapon" matches nothing). Filtering by
# rarity server-side turns a ~35k-row catalogue walk into ~2k rows per tier.
RARITY_TAG = {
    0: "tag_Rarity_Common_Weapon",      # Consumer
    1: "tag_Rarity_Uncommon_Weapon",    # Industrial
    2: "tag_Rarity_Rare_Weapon",        # Mil-Spec
    3: "tag_Rarity_Mythical_Weapon",    # Restricted
    4: "tag_Rarity_Legendary_Weapon",   # Classified
    5: "tag_Rarity_Ancient_Weapon",     # Covert
}
QUALITY_TAG = {"normal": "tag_normal", "souvenir": "tag_tournament",
               "stattrak": "tag_strange"}


def tier_filters(rarity: int, qualities=("normal",)) -> dict:
    """Query params restricting search/render to one rarity and qualities."""
    return {"category_730_Rarity[]": [RARITY_TAG[int(rarity)]],
            "category_730_Quality[]": [QUALITY_TAG[q] for q in qualities]}


@dataclass(slots=True)
class BulkStats:
    pages: int = 0
    items: int = 0
    written: int = 0
    retries: int = 0
    rate_limited: int = 0
    errors: int = 0
    effective_page_size: Optional[int] = None
    total_count: Optional[int] = None
    queries_done: int = 0
    short_pages: int = 0          # pages smaller than the detected size
    incomplete: bool = False      # stopped before total_count was reached
    last_start: int = 0           # resume point (pass back as start_at)

    @property
    def estimated_pages_remaining(self) -> Optional[int]:
        if not self.effective_page_size or not self.total_count:
            return None
        seen = self.items
        left = max(self.total_count - seen, 0)
        return -(-left // self.effective_page_size)


class BulkMarket:
    """Paginated Steam market reader.

    `sweep()` walks the whole catalogue. `sweep_queries()` runs one paginated
    search per query string, which is dramatically cheaper when you only need
    a few weapons and Steam is capping the page size.
    """

    def __init__(
        self,
        currency: str = "USD",
        cache_path: Union[str, Path] = Path("data/prices.sqlite"),
        page_delay: float = 6.0,
        timeout: float = 30.0,
        max_retries: int = 6,
        max_backoff: float = 300.0,
        page_size: int = REQUESTED_PAGE_SIZE,
    ):
        self.currency = CURRENCY.get(currency.upper(), 1)
        self.cache = PriceCache(cache_path)
        self.page_delay = page_delay
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self.page_size = page_size
        self.state = ClientState()
        self.stats = BulkStats()
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                # honest UA: impersonating a browser buys nothing
                "User-Agent": "cs2-tradeup-lab/2.1 (academic research; low-rate)",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://steamcommunity.com/market/",
            },
        )

    def close(self) -> None:
        self.client.close()

    # -- one page ---------------------------------------------------------
    def _page(self, start: int, query: str = "",
              filters: Optional[dict] = None) -> Optional[dict]:
        backoff = min(15.0, self.max_backoff)
        for attempt in range(self.max_retries):
            self.state.set("requesting", f"start={start} attempt {attempt + 1}")
            params = {
                "query": query,
                "start": start,
                "count": self.page_size,
                "search_descriptions": 0,
                "sort_column": "name",
                "sort_dir": "asc",
                "appid": APPID_CS2,
                "norender": 1,
                "currency": self.currency,
            }
            if filters:
                params.update(filters)
            try:
                r = self.client.get(SEARCH_URL, params=params)
            except httpx.HTTPError as e:
                self.stats.errors += 1
                self.stats.retries += 1
                self.state.set("backoff", f"{type(e).__name__}, {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if r.status_code == 429:
                self.stats.rate_limited += 1
                self.stats.retries += 1
                ra = float(r.headers.get("Retry-After", 0) or 0)
                wait = min(max(ra, backoff), self.max_backoff)
                self.state.set("backoff", f"429, waiting {wait:.0f}s")
                time.sleep(wait + random.uniform(0, 3))
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if r.status_code != 200:
                self.stats.errors += 1
                self.stats.retries += 1
                self.state.set("backoff", f"HTTP {r.status_code}, {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            try:
                data = r.json()
            except ValueError:
                self.stats.errors += 1
                self.stats.retries += 1
                self.state.set("backoff", f"non-JSON body, {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if not isinstance(data, dict) or not data.get("success"):
                return None
            return data
        return None

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def parse_results(data: dict) -> Iterator[tuple[str, float, int]]:
        """Yield (market_hash_name, price, listing_count) from a page.

        `sell_price` is an integer in cents of the requested currency;
        `sell_price_text` is the localized string used as a fallback.
        """
        for row in data.get("results") or []:
            if not isinstance(row, dict):
                continue
            name = row.get("hash_name") or row.get("name")
            if not name:
                continue
            cents = row.get("sell_price")
            if cents is None:
                p = parse_price(row.get("sell_price_text") or "")
                if p is None:
                    continue
                cents = int(round(p * 100))
            try:
                price = float(cents) / 100.0
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            try:
                listings = int(row.get("sell_listings") or 0)
            except (TypeError, ValueError):
                listings = 0
            yield name, price, listings

    @staticmethod
    def row_count(data: dict) -> int:
        """How many rows the page actually contained (parsed or not).

        Distinct from the number we keep: a row without a usable price is
        still a position in the catalogue that `start` must step over.
        """
        rows = data.get("results")
        return len(rows) if isinstance(rows, list) else 0

    # -- pagination core --------------------------------------------------
    def _paginate(
        self,
        query: str,
        wanted: Optional[set[str]],
        max_pages: int,
        progress: Optional[Callable[[str], None]],
        label: str = "",
        filters: Optional[dict] = None,
        start_at: int = 0,
        on_checkpoint: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> None:
        start = max(0, start_at)
        total: Optional[int] = None
        pages_here = 0
        prev_first: Optional[str] = None

        while True:
            data = self._page(start, query, filters)
            if data is None:
                # a failed page mid-sweep is NOT the end of the catalogue
                if total is None or start < total:
                    self.stats.incomplete = True
                break
            self.stats.pages += 1
            pages_here += 1

            if total is None:
                try:
                    total = int(data.get("total_count") or 0)
                except (TypeError, ValueError):
                    total = 0
                if self.stats.total_count is None:
                    self.stats.total_count = total

            n_rows = self.row_count(data)
            if n_rows == 0:
                if total and start < total:
                    self.stats.incomplete = True
                break

            # Guard: a page identical to the previous one would loop forever
            # (seen when total_count is bogus or the catalogue reshuffles).
            row0 = data["results"][0]
            first = row0.get("hash_name") if isinstance(row0, dict) else None
            if first is not None and first == prev_first:
                self.stats.incomplete = True
                break
            prev_first = first

            # Detect the real page size from the first full-looking page.
            if self.stats.effective_page_size is None:
                self.stats.effective_page_size = n_rows
                if n_rows < self.page_size and progress:
                    progress(
                        f"NOTE: asked for {self.page_size} rows/page, Steam gave "
                        f"{n_rows}. Paging by {n_rows}."
                        + (f" A full sweep of {total} items needs about "
                           f"{-(-total // n_rows)} requests."
                           if total and n_rows <= SMALL_PAGE_WARN else ""))
            elif n_rows < self.stats.effective_page_size:
                self.stats.short_pages += 1

            batch = []
            for name, price, listings in self.parse_results(data):
                self.stats.items += 1
                if wanted is not None and name not in wanted:
                    continue
                # `listings` is SUPPLY, not 24h sales: kept in its own field
                # (Quote.listings), never mixed with sales volume.
                batch.append(Quote(name, price, None, None, time.time(), listings))
            self.cache.put_many(batch, self.currency)
            self.stats.written += len(batch)

            if progress:
                left = self.stats.estimated_pages_remaining
                progress(
                    f"{label}page {self.stats.pages} start={start}"
                    f"{f'/{total}' if total else ''} rows={n_rows} "
                    f"kept={self.stats.written} 429s={self.stats.rate_limited}"
                    + (f" ~{left} pages left" if left else ""))

            # CRITICAL: advance by what we actually received. Advancing by the
            # requested size skips every item Steam did not return.
            start += n_rows
            self.stats.last_start = start
            if on_checkpoint:
                on_checkpoint(start, total)

            if total and start >= total:
                break
            if max_pages and pages_here >= max_pages:
                break

            if self.page_delay > 0:
                self.state.set("throttling", f"page delay {self.page_delay:.0f}s")
                time.sleep(self.page_delay
                           + random.uniform(0, self.page_delay * 0.25))

    # -- public sweeps ----------------------------------------------------
    def sweep(
        self,
        wanted: Optional[set[str]] = None,
        max_pages: int = 0,
        progress: Optional[Callable[[str], None]] = None,
    ) -> BulkStats:
        """Walk the entire CS2 catalogue.

        `wanted` restricts what gets WRITTEN, but every page is still paged
        through -- the endpoint has no reliable exact-name filter. If Steam
        caps the page at 10 rows this is ~3.5k requests; prefer
        `sweep_queries()` in that case.
        """
        self._paginate("", wanted, max_pages, progress)
        self.cache.flush()
        self.state.set("idle")
        return self.stats

    def sweep_tier(
        self,
        rarity: int,
        qualities=("normal",),
        wanted: Optional[set[str]] = None,
        max_pages: int = 0,
        start_at: int = 0,
        on_checkpoint: Optional[Callable[[int, Optional[int]], None]] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> BulkStats:
        """Page through ONE rarity tier, filtered server-side.

        Mil-Spec/normal is ~2k rows (~200 pages at Steam's 10-row cap)
        instead of ~35k for the whole catalogue. Resumable: pass the last
        `stats.last_start` (or a stored checkpoint) as `start_at`.
        """
        self._paginate("", wanted, max_pages, progress,
                       label=f"[tier {rarity}] ",
                       filters=tier_filters(rarity, qualities),
                       start_at=start_at, on_checkpoint=on_checkpoint)
        self.cache.flush()
        self.state.set("idle")
        return self.stats

    def sweep_queries(
        self,
        queries: Iterable[str],
        wanted: Optional[set[str]] = None,
        max_pages_per_query: int = 0,
        progress: Optional[Callable[[str], None]] = None,
    ) -> BulkStats:
        """Run one paginated search per query string.

        Server-side filtering is the only real defence when Steam caps the
        page size: searching "AK-47" pages through a few hundred rows instead
        of 35,000. Pass the weapon names your tier actually needs.
        """
        qs = [q for q in queries if q]
        for i, q in enumerate(qs, 1):
            self.stats.queries_done = i
            self._paginate(q, wanted, max_pages_per_query, progress,
                           label=f"[{i}/{len(qs)}] {q!r} ")
            if self.page_delay > 0 and i < len(qs):
                time.sleep(self.page_delay
                           + random.uniform(0, self.page_delay * 0.25))
        self.cache.flush()
        self.state.set("idle")
        return self.stats
