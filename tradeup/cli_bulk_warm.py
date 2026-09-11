"""Replacement `bulk-warm` command for tradeup/cli.py.

Copy the two functions below over the existing `bulk_warm` in
`tradeup/cli.py`. No new imports are needed beyond what that file already
has (`BulkMarket`, `SkinDB`, `RARITIES`, `_tier_names`, `console`, `Table`,
`time`, `Path`, `typer`, `Optional`).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

# from .data.loader import SkinDB
# from .prices.bulk import BulkMarket
# console = _safe_console(); RARITIES = {...}; _tier_names(...) already exist


def _weapon_terms(db, r, souvenir: bool) -> list[str]:
    """Distinct weapon names for a tier, usable as server-side search terms.

    'AK-47 | Elite Build' -> 'AK-47'. Searching per weapon pages through a
    few hundred rows instead of the whole 35k catalogue, which is what makes
    this viable when Steam caps the page size.
    """
    terms: set[str] = set()
    tiers = [r] + ([r.next_tier()] if r.next_tier() else [])
    for tier in tiers:
        for c in db.collections(tier):
            for s in db.pool(c, tier):
                head = s.name.split("|")[0].strip()
                if head:
                    terms.add(head)
    return sorted(terms)


@app.command("bulk-warm")
def bulk_warm(
    currency: str = "USD",
    rarity: Optional[str] = typer.Option(
        None, help="Only keep names for this tier (also enables targeted search)"),
    souvenir: bool = typer.Option(True, help="Include Souvenir variants when filtering"),
    targeted: bool = typer.Option(
        True, help="Search per weapon name instead of walking the whole catalogue. "
                   "Requires --rarity. Far fewer requests when Steam caps page size."),
    page_delay: float = typer.Option(6.0, help="Seconds between pages. Raise if 429s."),
    page_size: int = typer.Option(100, help="Rows to request per page. Steam may give fewer."),
    max_pages: int = typer.Option(0, help="Stop after N pages per search (0 = all)"),
    export: bool = typer.Option(True, help="Write data/price_snapshot.json at the end"),
):
    """Price the market using Steam's paginated search (many items per request).

    Steam does not always honour the requested page size -- it often returns
    10 rows regardless. This command detects the real page size and pages by
    it, so nothing is skipped. When the page is small, a full-catalogue sweep
    is thousands of requests, so pass --rarity and keep --targeted on: it
    searches per weapon name and fetches only what your tier needs.

    Caveat: bulk data has the lowest listing price and a LISTING COUNT, but
    no true median and no 24h sales volume. Run `warm-prices` afterwards on
    your shortlist before committing money.
    """
    wanted: Optional[set[str]] = None
    terms: list[str] = []
    if rarity:
        db = SkinDB.load()
        r = RARITIES[rarity.lower()]
        wanted = _tier_names(db, r, souvenir, include_outputs=True)
        console.print(f"Filtering to {len(wanted)} names for {r.label} "
                      "(plus its output tier).")
        if targeted:
            terms = _weapon_terms(db, r, souvenir)
            console.print(f"Targeted search over {len(terms)} weapon names: "
                          f"{', '.join(terms[:6])}"
                          + (" ..." if len(terms) > 6 else ""))
    elif targeted:
        console.print("[yellow]--targeted needs --rarity; falling back to a full "
                      "catalogue sweep. That is ~3.5k requests if Steam caps the "
                      "page at 10 rows.[/yellow]")

    market = BulkMarket(currency=currency, page_delay=page_delay, page_size=page_size)
    console.print("[dim]Ctrl+C is safe - every page is committed as it arrives.[/dim]")
    t0 = time.monotonic()
    try:
        if terms:
            market.sweep_queries(terms, wanted=wanted,
                                 max_pages_per_query=max_pages,
                                 progress=lambda m: console.print(f"[dim]{m}[/dim]"))
        else:
            market.sweep(wanted=wanted, max_pages=max_pages,
                         progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted -- pages fetched so far are cached.[/yellow]")
    finally:
        market.close()

    s = market.stats
    t = Table(title=f"bulk-warm summary ({(time.monotonic()-t0)/60:.1f} min)")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    rows = [("requests", s.pages), ("items seen", s.items),
            ("quotes cached", s.written),
            ("rows per page (actual)", s.effective_page_size or "?"),
            ("catalogue size", s.total_count or "?"),
            ("rate limited", s.rate_limited), ("retries", s.retries),
            ("errors", s.errors)]
    if s.queries_done:
        rows.insert(0, ("searches run", s.queries_done))
    for label, v in rows:
        t.add_row(label, str(v))
    console.print(t)

    eff = s.effective_page_size
    if eff and eff < page_size:
        console.print(f"[yellow]Steam returned {eff} rows per request, not "
                      f"{page_size}. Paging stepped by {eff}, so nothing was "
                      "skipped -- but a full sweep is expensive at that size. "
                      "Use --rarity with --targeted.[/yellow]")
    if s.rate_limited:
        console.print(f"[yellow]Hit {s.rate_limited} rate limits. Re-run with "
                      f"--page-delay {page_delay * 2:.0f}.[/yellow]")
    if wanted:
        missing = len(wanted) - s.written
        if missing > 0:
            console.print(f"[dim]{missing} of {len(wanted)} wanted names were not "
                          "seen. Some have no live listing; re-run or confirm "
                          "individually with warm-prices.[/dim]")
    if export:
        n = market.cache.export(Path("data/price_snapshot.json"), market.currency)
        console.print(f"[green]{n} quotes -> data/price_snapshot.json[/green]")
    console.print("[dim]Bulk quotes have no median/volume. Run 'warm-prices' on "
                  "your shortlist before committing money.[/dim]")
