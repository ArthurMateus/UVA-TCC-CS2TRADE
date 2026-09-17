"""Command line interface.

    tradeup sync-db                       skin database
    tradeup sync-images                   skin images
    tradeup warm --rarity milspec         prices (adaptive, concurrent)
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
from .prices.fetcher import AsyncPriceFetcher
from .prices.steam import CURRENCY, PriceBook, PriceCache
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

RARITIES = {"consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
            "milspec": Rarity.MILSPEC, "mil-spec": Rarity.MILSPEC,
            "restricted": Rarity.RESTRICTED, "classified": Rarity.CLASSIFIED,
            "covert": Rarity.COVERT}
ALL = (Rarity.CONSUMER, Rarity.INDUSTRIAL, Rarity.MILSPEC,
       Rarity.RESTRICTED, Rarity.CLASSIFIED, Rarity.COVERT)
SNAPSHOT = Path("data/price_snapshot.json")
CACHE = Path("data/prices.sqlite")


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


def offline_service(policy: PricingPolicy) -> PriceService:
    if not SNAPSHOT.exists():
        console.print(f"[red]No {SNAPSHOT}. Run: tradeup warm --rarity milspec[/red]")
        raise typer.Exit(1)
    return PriceService(PriceBook.load(SNAPSHOT), policy)


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
         currency: str = "USD",
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
    r = RARITIES[rarity.lower()]
    cur = CURRENCY.get(currency.upper(), 1)
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
                 ("breaker trips", f.breaker.trips),
                 ("throughput", f"{s.per_min:.0f}/min"),
                 ("final rate", f"{f.limiter.rate:.0f}/min")):
        t.add_row(k, str(v))
    console.print(t)
    if f.breaker.trips:
        console.print("[yellow]The circuit breaker tripped: your IP was being "
                      "limited hard. Re-run later, or lower --rate.[/yellow]")
    if export:
        n = cache.export(SNAPSHOT, cur)
        console.print(f"[green]{n} quotes -> {SNAPSHOT}[/green]")


# ---------------------------------------------------------------------------
@app.command("guaranteed")
def guaranteed(budget: float = typer.Option(..., help="Max total input cost"),
               rarity: str = "milspec",
               currency: str = "USD",
               souvenir: bool = True,
               float_percentile: float = 0.15,
               haircut: float = 0.03,
               min_profit: float = 0.0,
               min_input_volume: int = typer.Option(5, help="Min listings per input"),
               top: int = 15,
               json_out: Optional[Path] = None):
    """Contracts where EVERY outcome beats the cost.

    There is no losing roll in these -- only a smaller win. They exist
    because input and output prices are set by different supply dynamics,
    so a collection's cheapest next-tier skin can be worth more than ten of
    its own unwanted inputs.
    """
    db = SkinDB.load()
    r = RARITIES[rarity.lower()]
    prices = offline_service(PricingPolicy(min_volume=1,
                                           min_input_volume=min_input_volume,
                                           haircut=haircut))
    found = find_guaranteed(db, prices, r, budget=budget,
                            float_percentile=float_percentile, souvenir=souvenir,
                            haircut=haircut, min_profit=min_profit,
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
         currency: str = "USD",
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
    db = SkinDB.load()
    r = RARITIES[rarity.lower()]
    prices = offline_service(PricingPolicy(min_volume=min_volume,
                                           min_input_volume=min_input_volume,
                                           haircut=haircut))
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


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Start the web UI."""
    import uvicorn
    console.print(f"[green]http://{host}:{port}[/green]")
    uvicorn.run("tradeup.web:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
