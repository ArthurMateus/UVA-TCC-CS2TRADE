"""Command line interface.

    tradeup sync-db                       skin database
    tradeup sync-images                   skin images
    tradeup bulk-warm --rarity milspec    prices, paginated by tier (fast, resumable)
    tradeup warm --rarity milspec         prices + 24h volume (adaptive, concurrent)
    tradeup refine-floats --budget 200    re-price the shortlist by input float (CSFloat)
    tradeup guaranteed --budget 200       contracts that cannot lose
    tradeup scan --budget 200             ranked by chance of profit
    tradeup serve                         web UI

Windows: skin names contain characters cp1252 cannot encode, so the console
is reconfigured to UTF-8 at startup and every file write pins an encoding.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .data.loader import DB_PATH, SkinDB, SyncError, fetch_skins_payload, sync_from_csgo_api
from .images import IMAGES_PATH, build_from_payload
from .images import save as save_images
from .models import Rarity
from .optimizer import explain, search
from . import config
from .fsutil import atomic_write_text
from .prices.bulk import BulkMarket
from .prices.fetcher import AsyncPriceFetcher
from .prices.steam import CURRENCY, PriceBook, PriceCache, Quote
from .pricing import PriceService, PricingPolicy
from .risk import explain_guaranteed, find_guaranteed

app = typer.Typer(add_completion=False, help="CS2 trade-up analysis toolkit")


def _console() -> Console:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if enc not in ("utf8", "utf8mb4"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            return Console(legacy_windows=True, safe_box=True)
    return Console()


console = _console()

RARITIES = config.RARITIES
ALL = config.TRADEABLE
SNAPSHOT = config.SNAPSHOT
CACHE = config.CACHE_DB
ODDS = ("per_input", "outcome_weighted")
SORTS = ("win", "safety", "profit", "roi", "ev", "cost")


def parse_rarity(raw: str) -> Rarity:
    r = RARITIES.get(raw.lower())
    if r is None:
        console.print(f"[red]Unknown rarity {raw!r}. Use one of: "
                      f"{', '.join(sorted(set(RARITIES)))}[/red]")
        raise typer.Exit(2)
    return r


def parse_currency(raw: str) -> int:
    c = CURRENCY.get(raw.upper())
    if c is None:
        console.print(f"[red]Unknown currency {raw!r}. Use one of: "
                      f"{', '.join(CURRENCY)}[/red]")
        raise typer.Exit(2)
    return c


def require(cond: bool, msg: str) -> None:
    if not cond:
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(2)


def tier_names(db: SkinDB, r: Rarity, souvenir: bool) -> set[str]:
    """Every market_hash_name a scan at this tier can need."""
    names: set[str] = set()
    for s in (x for c in db.collections(r) for x in db.pool(c, r)):
        for w in s.reachable_wears():
            names.add(s.market_hash_name(w))
            if souvenir and s.souvenir_capable:
                names.add(s.market_hash_name(w, souvenir=True))
    nxt = r.next_tier()
    if nxt:                               # outputs are always normal quality
        for s in (x for c in db.collections(nxt) for x in db.pool(c, nxt)):
            for w in s.reachable_wears():
                names.add(s.market_hash_name(w))
    return names


def offline_service(policy: PricingPolicy, currency: Optional[str] = None) -> PriceService:
    if not SNAPSHOT.exists():
        console.print(f"[red]No {SNAPSHOT}. Run: tradeup bulk-warm --rarity milspec[/red]")
        raise typer.Exit(1)
    book = PriceBook.load(SNAPSHOT)
    if currency and book.currency is not None and book.currency != parse_currency(currency):
        console.print(f"[red]The snapshot is in currency id {book.currency}, not "
                      f"{currency}. Re-run warm/bulk-warm with --currency {currency}.[/red]")
        raise typer.Exit(1)
    return PriceService(book, policy)


def _load_checkpoints() -> dict:
    try:
        return json.loads(config.CHECKPOINTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_checkpoint(key: str, start: int, total: Optional[int]) -> None:
    cp = _load_checkpoints()
    cp[key] = {"next_start": start, "total": total, "updated": time.time()}
    config.CHECKPOINTS.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config.CHECKPOINTS, json.dumps(cp, indent=1))


# ---------------------------------------------------------------------------
@app.command("sync-db")
def sync_db(out: Path = typer.Option(DB_PATH), url: Optional[str] = None,
            from_file: Optional[Path] = typer.Option(None, "--from-file"),
            timeout: float = 60.0):
    """Download and normalize the skin/collection database."""
    try:
        db = sync_from_csgo_api(url=url, timeout=timeout, from_file=from_file)
        p = db.save(out)
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except OSError as e:
        console.print(f"[red]Could not write {out}: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Saved {len(db)} entries to {p}[/green]")
    t = Table(show_header=True, header_style="dim")
    for c in ("Tier", "Collections", "Skins", "Souvenir-capable"):
        t.add_column(c, justify="left" if c == "Tier" else "right")
    for r in ALL:
        sk = [s for s in db.skins if s.rarity == r]
        t.add_row(r.label, str(len(db.tradeable_collections(r))), str(len(sk)),
                  str(sum(1 for s in sk if s.souvenir_capable)))
    console.print(t)
    console.print("[dim]Covert shows 0: its outputs are knives/gloves, a flat "
                  "pool rather than a collection.[/dim]")
    for w in db.quality_sanity():
        console.print(f"[yellow]Warning: {w}[/yellow]")


@app.command("sync-images")
def sync_images(out: Path = typer.Option(IMAGES_PATH),
                from_file: Optional[Path] = typer.Option(None, "--from-file")):
    """Extract skin image URLs from the same payload as sync-db."""
    try:
        raw = (json.loads(Path(from_file).read_text(encoding="utf-8"))
               if from_file else fetch_skins_payload())
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    m = build_from_payload(raw)
    if not m:
        console.print("[yellow]No image URLs found -- schema may have changed.[/yellow]")
        raise typer.Exit(1)
    console.print(f"[green]Saved {len(m)} image URLs to {save_images(m, out)}[/green]")


# ---------------------------------------------------------------------------
@app.command("warm")
def warm(rarity: str = typer.Option("milspec"),
         currency: str = config.DEFAULT_CURRENCY,
         souvenir: bool = True,
         concurrency: int = typer.Option(6, help="Parallel requests in flight"),
         rate: float = typer.Option(20.0, help="Starting requests/min; it self-tunes"),
         ttl: float = typer.Option(8.0, help="Hours before a quote is stale"),
         limit: int = typer.Option(0, help="Stop after N names (0 = all)"),
         export: bool = True):
    """Fetch prices concurrently, with an adaptive rate limit.

    The limiter starts conservative and speeds up while responses stay clean,
    halving on a 429 (once per congestion window, not once per response). It
    converges on the fastest rate Steam will actually tolerate instead of
    alternating between too-slow and punished.

    Ctrl+C is safe: every quote is committed as it arrives.
    """
    db = SkinDB.load()
    r = parse_rarity(rarity)
    cur = parse_currency(currency)
    require(concurrency >= 1 and rate > 0 and ttl > 0,
            "concurrency, rate and ttl must be positive")
    cache = PriceCache(CACHE)
    names = sorted(tier_names(db, r, souvenir))
    if limit:
        names = names[:limit]

    f = AsyncPriceFetcher(cache, currency=cur, concurrency=concurrency,
                          start_rate=rate, ttl_hours=ttl)
    todo = f.pending(names)
    console.print(f"{len(names)} names for {r.label}; {len(names)-len(todo)} cached, "
                  f"[bold]{len(todo)}[/bold] to fetch. Ctrl+C is safe.")
    if not todo:
        console.print("[green]Cache already complete.[/green]")
        if export:
            n = cache.export(SNAPSHOT, cur)
            console.print(f"[green]{n} quotes -> {SNAPSHOT}[/green]")
        return

    t0 = time.monotonic()
    with console.status("") as st:
        def prog(s, msg):
            st.update(f"{msg} · ok={s.ok} empty={s.empty} 429={s.rate_limited} "
                      f"err={s.errors + s.timeouts}")
        try:
            asyncio.run(f.fetch(todo, prog))
        except KeyboardInterrupt:
            f.stop()
            console.print("\n[yellow]Interrupted -- everything fetched is cached.[/yellow]")

    s = f.stats
    mins = (time.monotonic() - t0) / 60
    t = Table(title=f"warm summary ({mins:.1f} min)")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    for k, v in (("priced", s.ok), ("no listing", s.empty),
                 ("already cached", s.cached), ("rate limited", s.rate_limited),
                 ("timeouts", s.timeouts), ("errors", s.errors),
                 ("gave up (retry next run)", s.gave_up),
                 ("breaker trips", f.breaker.trips),
                 ("throughput", f"{s.per_min:.0f}/min"),
                 ("final rate", f"{f.limiter.rate:.0f}/min")):
        t.add_row(k, str(v))
    console.print(t)
    if f.breaker.trips:
        console.print("[yellow]The circuit breaker tripped: your IP was being "
                      "limited hard. Re-run later, or lower --rate.[/yellow]")
    if s.halted:
        console.print("[red]Run halted: the IP stayed rate-limited after several "
                      "probes. Everything fetched so far is cached.[/red]")
    if export:
        n = cache.export(SNAPSHOT, cur)
        console.print(f"[green]{n} quotes -> {SNAPSHOT}[/green]")
    cache.close()


@app.command("bulk-warm")
def bulk_warm(
    rarity: str = typer.Option("milspec", help="Input tier; its output tier is swept too"),
    currency: str = config.DEFAULT_CURRENCY,
    souvenir: bool = typer.Option(True, help="Also sweep Souvenir listings of the input tier"),
    page_delay: float = typer.Option(6.0, help="Seconds between pages. Raise if 429s."),
    max_pages: int = typer.Option(0, help="Stop after N pages per tier (0 = all)"),
    resume: bool = typer.Option(True, help="Continue from the last checkpoint"),
    export: bool = typer.Option(True, help="Write the price snapshot at the end"),
):
    """Price a whole tier with Steam's paginated search, filtered by rarity.

    The search is restricted server-side with Steam's rarity/quality tags,
    so Mil-Spec is ~2k rows (~200 requests at Steam's 10-row page cap)
    instead of ~35k for the full catalogue. Each page is committed and
    checkpointed: Ctrl+C and re-run continues where it stopped.

    Bulk rows carry the lowest ask and the LISTING COUNT, not 24h sales.
    Confirm a shortlist with `warm` (priceoverview) before spending money.
    """
    r = parse_rarity(rarity)
    cur = parse_currency(currency)
    require(page_delay >= 0 and max_pages >= 0, "page-delay and max-pages must be >= 0")
    nxt = r.next_tier()
    plan = [(r, ("normal", "souvenir") if souvenir else ("normal",))]
    if nxt is not None and nxt in ALL:
        plan.append((nxt, ("normal",)))          # outputs are always normal quality

    market = BulkMarket(currency=currency, cache_path=CACHE, page_delay=page_delay)
    cps = _load_checkpoints() if resume else {}
    console.print("[dim]Ctrl+C is safe - every page is committed and checkpointed.[/dim]")
    t0 = time.monotonic()
    try:
        for tier, quals in plan:
            key = f"steam_search:{cur}:{int(tier)}:{'+'.join(quals)}"
            cp = cps.get(key) or {}
            start = int(cp.get("next_start") or 0)
            if cp.get("total") and start >= int(cp["total"]):
                start = 0                        # finished before: refresh from the top
            console.print(f"[bold]{tier.label}[/bold] ({', '.join(quals)}) from row {start}")
            market.sweep_tier(int(tier), quals, max_pages=max_pages, start_at=start,
                              on_checkpoint=lambda s, t, k=key: _save_checkpoint(k, s, t),
                              progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted -- re-run to resume from the checkpoint.[/yellow]")
    finally:
        market.close()

    s = market.stats
    t = Table(title=f"bulk-warm summary ({(time.monotonic()-t0)/60:.1f} min)")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    for label, v in (("requests", s.pages), ("items seen", s.items),
                     ("quotes cached", s.written),
                     ("rows per page (actual)", s.effective_page_size or "?"),
                     ("rate limited", s.rate_limited), ("retries", s.retries),
                     ("errors", s.errors), ("complete", "no" if s.incomplete else "yes")):
        t.add_row(label, str(v))
    console.print(t)
    if s.incomplete:
        console.print("[yellow]The sweep stopped early (failed page or repeated page). "
                      "Re-run to resume from the checkpoint.[/yellow]")
    if export:
        with PriceCache(CACHE) as c:
            n = c.export(SNAPSHOT, cur)
        console.print(f"[green]{n} quotes -> {SNAPSHOT}[/green]")


# ---------------------------------------------------------------------------
@app.command("guaranteed")
def guaranteed(budget: float = typer.Option(..., help="Max total input cost"),
               rarity: str = "milspec",
               currency: str = config.DEFAULT_CURRENCY,
               souvenir: bool = True,
               float_percentile: float = 0.15,
               haircut: float = 0.03,
               min_profit: float = 0.0,
               min_input_volume: int = typer.Option(5, help="Min listings per input"),
               fee: Optional[float] = typer.Option(None, help="Omit for Steam's 13%"),
               top: int = 15,
               json_out: Optional[Path] = None):
    """Contracts where EVERY outcome beats the cost (net of marketplace fees).

    There is no losing roll in these -- only a smaller win. They exist
    because input and output prices are set by different supply dynamics,
    so a collection's cheapest next-tier skin can be worth more than ten of
    its own unwanted inputs.
    """
    r = parse_rarity(rarity)
    _validate_common(budget, float_percentile, haircut, fee, top)
    db = SkinDB.load()
    prices = offline_service(PricingPolicy(min_volume=1,
                                           min_input_volume=min_input_volume,
                                           haircut=haircut), currency)
    found = find_guaranteed(db, prices, r, budget=budget,
                            float_percentile=float_percentile, souvenir=souvenir,
                            haircut=haircut, fee_rate=fee, min_profit=min_profit,
                            progress=lambda m: None)
    if not found:
        console.print("[yellow]No guaranteed contracts at this budget.[/yellow]")
        console.print("[dim]The cheapest outcome never covers 10 inputs. Try a "
                      "different rarity, raise the budget, or use `scan` and "
                      "accept some risk.[/dim]")
        raise typer.Exit(0)

    t = Table(title=f"Guaranteed profit - {r.label} -> {r.next_tier().label} "
                    f"- budget {budget:.2f} {currency}")
    for c in ("#", "Collection", "Cost", "Worst", "Best", "Guaranteed", "Floor ROI"):
        t.add_column(c, justify="left" if c == "Collection" else "right")
    for i, g in enumerate(found[:top], 1):
        t.add_row(str(i), g.collection[:34], f"{g.cost:.2f}",
                  f"{g.worst_net:.2f}", f"{g.best_net:.2f}",
                  f"[green]+{g.guaranteed_profit:.2f}[/green]",
                  f"{g.guaranteed_roi*100:.0f}%")
    console.print(t)
    console.print("[dim]Guaranteed = the WORST outcome still profits. "
                  "Floor ROI is the return on the worst roll.[/dim]")

    best = found[0]
    e = explain_guaranteed(best)
    i = e["inputs"][0]
    console.print(f"\n[bold]{best.collection}[/bold]")
    console.print(f"Buy {i['count']}x [cyan]{i['skin']}[/cyan] ({i['wear']}) "
                  f"@ {i['price']:.2f} = {e['cost']:.2f}   "
                  f"float cap {i['min_float']}-{i['max_float']}")
    d = Table(show_header=True, header_style="dim")
    for c in ("Outcome", "Wear", "Float", "Chance", "Net", "Profit"):
        d.add_column(c, justify="left" if c == "Outcome" else "right")
    for o in e["outcomes"]:
        d.add_row(o["skin"][:32], o["wear"], f"{o['float']:.4f}",
                  f"{o['p_pct']:.1f}%", f"{o['net']:.2f}",
                  f"[green]+{o['net']-e['cost']:.2f}[/green]")
    console.print(d)

    if json_out:
        Path(json_out).write_text(
            json.dumps([explain_guaranteed(g) for g in found[:top]],
                       indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[green]Wrote {json_out}[/green]")


@app.command("scan")
def scan(budget: float = typer.Option(...),
         rarity: str = "milspec",
         currency: str = config.DEFAULT_CURRENCY,
         odds: str = typer.Option("per_input", help="per_input | outcome_weighted"),
         fee: Optional[float] = typer.Option(None, help="Omit for Steam's 13%"),
         souvenir: bool = True,
         float_percentile: float = 0.15,
         min_win_chance: float = typer.Option(50.0, help="Percent. The key filter."),
         min_roi: float = 105.0,
         min_volume: int = 3,
         min_input_volume: int = 5,
         haircut: float = 0.03,
         sort: str = typer.Option("win", help="win|safety|profit|roi|ev|cost"),
         top: int = 15,
         json_out: Optional[Path] = None):
    """Ranked search, filtered on chance of profit.

    Defaults deliberately reject lottery tickets: a contract with a 3% chance
    of a big payout is only rational if you can run it hundreds of times.
    Set --min-win-chance 0 to see everything.
    """
    r = parse_rarity(rarity)
    _validate_common(budget, float_percentile, haircut, fee, top)
    require(odds in ODDS, f"--odds must be one of {', '.join(ODDS)}")
    require(sort in SORTS, f"--sort must be one of {', '.join(SORTS)}")
    db = SkinDB.load()
    prices = offline_service(PricingPolicy(min_volume=min_volume,
                                           min_input_volume=min_input_volume,
                                           haircut=haircut), currency)
    cands = search(db, prices, r, budget=budget, odds_model=odds, fee_rate=fee,
                   souvenir=souvenir, float_percentile=float_percentile,
                   min_roi=min_roi/100.0, min_win_chance=min_win_chance/100.0,
                   top_n=top*4,
                   progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    if not cands:
        console.print("[yellow]Nothing cleared the filters.[/yellow]")
        console.print("[dim]Lower --min-win-chance, raise --budget, or try "
                      "`tradeup guaranteed` for zero-risk contracts.[/dim]")
        raise typer.Exit(0)

    rows = [explain(c) for c in cands]
    keys = {"win": "win_chance_pct", "safety": "guaranteed_profit",
            "profit": "profit", "roi": "roi_pct", "ev": "ev_net", "cost": "cost"}
    rows.sort(key=lambda x: x.get(keys.get(sort, "win_chance_pct"), 0),
              reverse=(sort != "cost"))

    t = Table(title=f"{r.label} -> {r.next_tier().label} - budget {budget:.2f} {currency}")
    for c in ("#", "Collection", "Grade", "Cost", "P(win)", "Worst", "Profit", "ROI"):
        t.add_column(c, justify="left" if c in ("Collection", "Grade") else "right")
    colour = {"guaranteed": "green", "strong": "cyan", "even": "yellow",
              "risky": "red", "lottery": "bold red"}
    for i, x in enumerate(rows[:top], 1):
        g = x["grade"]
        worst = -x["worst_loss"] if x["worst_loss"] > 0 else x["guaranteed_profit"]
        t.add_row(str(i), x["target_collection"][:26],
                  f"[{colour.get(g,'white')}]{g}[/]",
                  f"{x['cost']:.2f}", f"{x['win_chance_pct']:.0f}%",
                  f"{worst:+.2f}",
                  f"[{'green' if x['profit']>0 else 'red'}]{x['profit']:+.2f}[/]",
                  f"{x['roi_pct']:.0f}%")
    console.print(t)
    console.print("[dim]Worst = what you get on the worst roll, relative to cost. "
                  "Grade is based on P(win), not EV.[/dim]")

    if json_out:
        Path(json_out).write_text(json.dumps(rows[:top], indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        console.print(f"[green]Wrote {json_out}[/green]")


@app.command("refine-floats")
def refine_floats(budget: float = typer.Option(..., help="Max total input cost"),
                  mode: str = typer.Option("scan", help="scan | guaranteed"),
                  rarity: str = "milspec",
                  currency: str = config.DEFAULT_CURRENCY,
                  basis: str = typer.Option("premium", help="premium | csfloat"),
                  usd_rate: Optional[float] = typer.Option(
                      None, help="Snapshot currency per 1 USD (basis=csfloat only)"),
                  top: int = typer.Option(10, help="How many contracts to re-price"),
                  min_win_chance: float = 0.0,
                  float_percentile: float = 0.15,
                  haircut: float = 0.03,
                  fee: Optional[float] = None,
                  json_out: Optional[Path] = None):
    """Re-price the best contracts with FLOAT-AWARE input prices (CSFloat).

    The first pass prices inputs at the cheapest listing of the wear while
    assuming a low float. This asks CSFloat for the cheapest listings under
    the loosest float cap that still keeps every outcome in its wear, then
    re-evaluates the contract with those real floats and prices. Needs
    CSFLOAT_API_KEY. Costs ~2 API calls per distinct input (cached 2h).
    """
    from .float_pricing import refine_rows
    from .prices.csfloat import CSFloatError, CSFloatSource, FloatListingCache

    r = parse_rarity(rarity)
    _validate_common(budget, float_percentile, haircut, fee, top)
    require(mode in ("scan", "guaranteed"), "--mode must be scan or guaranteed")
    require(basis in ("premium", "csfloat"), "--basis must be premium or csfloat")
    require(basis != "csfloat" or (usd_rate or 0) > 0, "--basis csfloat needs --usd-rate")
    if not config.CSFLOAT_API_KEY:
        console.print("[red]CSFLOAT_API_KEY is not set. Create a key in your CSFloat "
                      "account (Profile -> Developers) and add it to .env[/red]")
        raise typer.Exit(1)
    db = SkinDB.load()
    policy = PricingPolicy(min_volume=1 if mode == "guaranteed" else 3,
                           min_input_volume=5, haircut=haircut)
    prices = offline_service(policy, currency)
    if mode == "guaranteed":
        rows = [explain_guaranteed(g) for g in find_guaranteed(
            db, prices, r, budget=budget, float_percentile=float_percentile,
            haircut=haircut, fee_rate=fee)]
    else:
        rows = [explain(c) for c in search(
            db, prices, r, budget=budget, fee_rate=fee, float_percentile=float_percentile,
            min_win_chance=min_win_chance / 100.0, top_n=top)]
    if not rows:
        console.print("[yellow]Nothing to refine at these settings.[/yellow]")
        raise typer.Exit(0)

    cache = FloatListingCache(CACHE)
    src = CSFloatSource(config.CSFLOAT_API_KEY, cache=cache)
    try:
        with console.status("Asking CSFloat for float-aware prices..."):
            refine_rows(rows, db, src, prices, top=top, fee_rate=fee,
                        basis=basis, usd_rate=usd_rate)
    except CSFloatError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        src.close(); cache.close()

    t = Table(title=f"Float check - {r.label} - {basis} basis ({src.requests} API calls)")
    for c in ("#", "Collection", "Est. cost", "Real cost", "Δ cost", "Est. profit",
              "Real profit", "Real P(win)", "Status"):
        t.add_column(c, justify="left" if c in ("Collection", "Status") else "right")
    for i, row in enumerate(rows[:top], 1):
        c = row.get("float_check") or {}
        if c.get("status") != "ok":
            t.add_row(str(i), row["target_collection"][:26], f"{row['cost']:.2f}",
                      "-", "-", f"{row['profit']:+.2f}", "-", "-",
                      f"[yellow]{c.get('status')}: {str(c.get('reason',''))[:40]}[/yellow]")
            continue
        colour = "green" if c["real_profit"] > 0 else "red"
        t.add_row(str(i), row["target_collection"][:26], f"{c['est_cost']:.2f}",
                  f"{c['real_cost']:.2f}", f"{c['cost_delta_pct']:+.1f}%",
                  f"{row['profit']:+.2f}", f"[{colour}]{c['real_profit']:+.2f}[/]",
                  f"{c['real_win_chance_pct']:.0f}%", c["real_grade"])
    console.print(t)
    console.print("[dim]Real = inputs bought at the float the contract needs. "
                  "Δ cost is how much the wear-level price understated it.[/dim]")
    if json_out:
        Path(json_out).write_text(json.dumps(rows[:top], indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        console.print(f"[green]Wrote {json_out}[/green]")


@app.command("db-push")
def db_push(currency: str = config.DEFAULT_CURRENCY, catalogue: bool = True):
    """Mirror the skin catalogue and the latest cached quotes into Supabase.

    Needs DATABASE_URL in .env (Supabase -> Project Settings -> Database).
    Quotes go to `price_quotes` (append-only history); a trigger keeps
    `price_latest` current, and the web app can read `price_best` with
    TRADEUP_PRICE_SOURCE=supabase.
    """
    from .images import ImageIndex
    from .prices.pg_store import PgPriceStore

    cur = parse_currency(currency)
    if not config.DATABASE_URL:
        console.print("[red]DATABASE_URL is not set (see .env.example).[/red]")
        raise typer.Exit(1)
    with PriceCache(CACHE) as cache:
        rows = cache.db.execute(
            "SELECT name,lowest,median,volume,ts,listings FROM prices WHERE currency=?",
            (cur,)).fetchall()
    quotes = [Quote(*r) for r in rows]
    try:
        with PgPriceStore(config.DATABASE_URL) as st:
            if catalogue:
                db = SkinDB.load()
                imgs = ImageIndex.load().map
                n = st.push_catalogue(db.skins, imgs)
                console.print(f"[green]{n} skins upserted[/green]")
            n = st.put_many(quotes, cur)
    except Exception as e:
        console.print(f"[red]Supabase push failed: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{n} quotes appended to price_quotes ({currency}).[/green]")


def _setup(force: bool = False) -> None:
    """Skin catalogue + image URLs from ONE download of the CSGO-API payload."""
    from .data.loader import parse_skins_payload
    if DB_PATH.exists() and IMAGES_PATH.exists() and not force:
        return
    console.print("Downloading the skin catalogue (CSGO-API, ~1 request)...")
    try:
        raw = fetch_skins_payload()
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    db = parse_skins_payload(raw)
    db.save(DB_PATH)
    imgs = build_from_payload(raw)
    save_images(imgs, IMAGES_PATH)
    console.print(f"[green]{len(db)} skins, {len(imgs)} images saved.[/green]")
    for w in db.quality_sanity():
        console.print(f"[yellow]Warning: {w}[/yellow]")


@app.command("setup")
def setup(force: bool = typer.Option(False, help="Download again even if present")):
    """First run: download the skin catalogue and images (one request)."""
    _setup(force=force)
    console.print("[green]Ready.[/green] Next: [bold]tradeup collect[/bold] "
                  "(leave it running) and [bold]tradeup serve[/bold] (web UI).")


@app.command("collect")
def collect(
    rarities: str = typer.Option(
        "all", help="Input tiers, comma separated (e.g. milspec,restricted) or 'all'"),
    currency: str = config.DEFAULT_CURRENCY,
    interval: float = typer.Option(60.0, help="Minutes to wait between rounds"),
    page_delay: float = typer.Option(6.0, help="Seconds between Steam pages"),
    souvenir: bool = typer.Option(True, help="Also sweep Souvenir listings of input tiers"),
    push: bool = typer.Option(True, help="Push changed quotes to Supabase (needs DATABASE_URL)"),
    rounds: int = typer.Option(0, help="Stop after N rounds (0 = run until Ctrl+C)"),
    max_pages: int = typer.Option(0, help="Pages per tier per round (0 = whole tier)"),
):
    """Run unattended: sweep Steam prices in rounds and keep Supabase current.

    One command, leave the computer running. Every round sweeps the chosen
    tiers (paginated, filtered by rarity, resumable), refreshes the local
    snapshot, pushes ONLY changed quotes to Supabase, then sleeps. Rate
    limits back the loop off automatically (up to 4 h). Ctrl+C stops it;
    progress is kept. Log: data/collect.log.
    """
    from .collector import AlreadyRunning, Collector, InstanceLock, setup_logging

    lock = InstanceLock(config.DATA_DIR / "collect.lock")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        console.print(f"[red]Já existe uma coleta rodando ({e}). Feche a outra "
                      "janela do coletar.bat antes de abrir uma nova.[/red]")
        raise typer.Exit(1)
    _setup()                                  # first run: fetch the catalogue
    cur = parse_currency(currency)
    require(interval >= 1 and page_delay >= 2,
            "--interval must be >= 1 min and --page-delay >= 2 s (Steam bans fast clients)")
    if rarities.strip().lower() == "all":
        tiers = [int(r) for r in ALL]
    else:
        tiers = sorted({int(parse_rarity(x.strip())) for x in rarities.split(",") if x.strip()})

    dsn = config.DATABASE_URL if push else None
    store_factory = push_catalogue = None
    if dsn:
        from .images import ImageIndex
        from .prices.pg_store import PgPriceStore
        store_factory = PgPriceStore

        def push_catalogue(store) -> int:
            return store.push_catalogue(SkinDB.load().skins, ImageIndex.load().map)
    elif push:
        console.print("[yellow]DATABASE_URL is not set: collecting locally only "
                      "(snapshot + SQLite). Add it to .env to push to Supabase.[/yellow]")

    setup_logging(config.DATA_DIR / "collect.log")
    col = Collector(tiers, currency=currency, cache_path=CACHE, snapshot_path=SNAPSHOT,
                    checkpoint=_save_checkpoint, load_checkpoints=_load_checkpoints,
                    dsn=dsn, store_factory=store_factory, push_catalogue=push_catalogue,
                    souvenir=souvenir, page_delay=page_delay, interval=interval * 60,
                    max_pages=max_pages,
                    say=lambda m: console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {m}"))
    plan = ", ".join(f"{Rarity(t).label}({'+'.join(q)})" for t, q in col.plan)
    console.print(f"[bold]collect[/bold] {currency.upper()} · tiers: {plan}")
    console.print(f"Round every {interval:.0f} min after the previous one ends · "
                  f"Supabase: {'on' if dsn else 'off'} · Ctrl+C to stop")
    try:
        col.run_forever(max_rounds=rounds)
    except KeyboardInterrupt:
        col.stop()
        console.print("\n[yellow]Stopped. Everything fetched is saved; the next "
                      "`collect` resumes from the checkpoints.[/yellow]")
    finally:
        lock.release()


def _validate_common(budget, float_percentile, haircut, fee, top) -> None:
    import math
    require(math.isfinite(budget) and budget > 0, "--budget must be a positive number")
    require(0.0 <= float_percentile <= 1.0, "--float-percentile must be in [0, 1]")
    require(0.0 <= haircut < 1.0, "--haircut must be in [0, 1)")
    require(fee is None or 0.0 <= fee < 1.0, "--fee must be in [0, 1)")
    require(top >= 1, "--top must be >= 1")


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Start the web UI."""
    import uvicorn
    console.print(f"[green]http://{host}:{port}[/green]")
    uvicorn.run("tradeup.web:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
