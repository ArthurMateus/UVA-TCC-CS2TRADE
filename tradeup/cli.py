"""Command line interface.

Recommended order:

    python -m tradeup.cli sync-db
    python -m tradeup.cli cooldown --currency BRL
    python -m tradeup.cli bulk-warm --currency BRL
    python -m tradeup.cli scan --budget 20 --rarity consumer --currency BRL

ENCODING: two separate Windows hazards are handled here.
1. Files -- every read/write pins `encoding="utf-8"`. Skin names contain
   characters (CJK, TM, star) that cp1252 cannot represent.
2. The console -- printing those same names to a cp1252 terminal raises
   UnicodeEncodeError too. `_safe_console()` reconfigures stdout to UTF-8,
   or falls back to character substitution when it cannot.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .data.loader import DB_PATH, SkinDB, SyncError, sync_from_csgo_api
from .engine import evaluate_contract, n_contracts_for_confidence, validate_contract
from .models import InputItem, Rarity
from .optimizer import contract_size, explain, robust_filter, search
from .prices.bulk import BulkMarket
from .prices.steam import PriceBook, SteamMarket
from .pricing import PriceService, PricingPolicy

app = typer.Typer(add_completion=False, help="CS2 trade-up analysis toolkit")


def _safe_console() -> Console:
    """A Console that never dies on a legacy Windows code page."""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if enc not in ("utf8", "utf8mb4"):
        try:  # Python 3.7+: retrofit the stream in place
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            return Console(legacy_windows=True, safe_box=True)
    return Console()


console = _safe_console()

RARITIES = {
    "consumer": Rarity.CONSUMER, "industrial": Rarity.INDUSTRIAL,
    "milspec": Rarity.MILSPEC, "mil-spec": Rarity.MILSPEC,
    "restricted": Rarity.RESTRICTED, "classified": Rarity.CLASSIFIED,
    "covert": Rarity.COVERT,
}
ALL_TIERS = (Rarity.CONSUMER, Rarity.INDUSTRIAL, Rarity.MILSPEC,
             Rarity.RESTRICTED, Rarity.CLASSIFIED, Rarity.COVERT)


def _price_service(offline, currency, ttl, policy) -> PriceService:
    if offline:
        return PriceService(PriceBook.load(offline), policy)
    return PriceService(SteamMarket(currency=currency, ttl_hours=ttl), policy)


def _tier_names(db: SkinDB, r: Rarity, souvenir: bool, include_outputs: bool) -> set[str]:
    """Every market_hash_name a scan at `r` could need."""
    names: set[str] = set()
    for s in (x for c in db.collections(r) for x in db.pool(c, r)):
        for w in s.reachable_wears():
            names.add(s.market_hash_name(w))
            if souvenir and s.souvenir_capable:
                names.add(s.market_hash_name(w, souvenir=True))
    nxt = r.next_tier()
    if include_outputs and nxt:  # outputs are always normal quality
        for s in (x for c in db.collections(nxt) for x in db.pool(c, nxt)):
            for w in s.reachable_wears():
                names.add(s.market_hash_name(w))
    return names


# ---------------------------------------------------------------------------
@app.command("sync-db")
def sync_db(
    out: Path = typer.Option(DB_PATH, help="Where to write skins.json"),
    url: Optional[str] = typer.Option(None, help="Override the source URL"),
    from_file: Optional[Path] = typer.Option(
        None, "--from-file", help="Parse a manually downloaded CSGO-API skins.json"),
    timeout: float = typer.Option(60.0),
):
    """Download and normalize the public skin/collection database."""
    try:
        db = sync_from_csgo_api(url=url, timeout=timeout, from_file=from_file)
        p = db.save(out)
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except UnicodeEncodeError as e:
        console.print(f"[red]Encoding error writing {out}: {e}\n"
                      "A text I/O call lost its encoding='utf-8'.[/red]")
        raise typer.Exit(1)
    except OSError as e:
        console.print(f"[red]Could not write {out}: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Saved {len(db)} skin/collection entries to {p}[/green]")
    t = Table(show_header=True, header_style="dim")
    for c, j in (("Tier", "left"), ("Collections", "right"), ("Skins", "right"),
                 ("Souvenir-capable", "right")):
        t.add_column(c, justify=j)
    for r in ALL_TIERS:
        skins = [s for s in db.skins if s.rarity == r]
        sv = sum(1 for s in skins if s.souvenir_capable)
        t.add_row(r.label, str(len(db.tradeable_collections(r))),
                  str(len(skins)), str(sv))
    console.print(t)
    console.print("[dim]Covert shows 0 collections: its outputs are knives/gloves, "
                  "which live in a flat pool rather than a collection.[/dim]")
    for w in db.quality_sanity():
        console.print(f"[yellow]Warning: {w}[/yellow]")
    console.print("[dim]Souvenir-capable is derived from Souvenir Package crate "
                  "membership. Run 'verify-souvenir' to confirm against the live "
                  "market before trusting it.[/dim]")


# ---------------------------------------------------------------------------
@app.command("cooldown")
def cooldown(
    currency: str = "USD",
    wait: bool = typer.Option(False, help="Poll until the ban clears"),
    poll: float = typer.Option(300.0, help="Seconds between polls when waiting"),
):
    """Check whether this IP is currently rate limited by Steam.

    The limit is per IP, not per account, and a ban can last many hours. It
    also re-arms every time you hit it while banned, so hammering it keeps
    you locked out. If this reports a ban, stop all requests and wait.
    """
    market = SteamMarket(currency=currency, rate=60, max_retries=1)
    probe = "AK-47 | Elite Build (Field-Tested)"
    try:
        while True:
            t0 = time.monotonic()
            try:
                r = market.client.get(market.URL, params={
                    "appid": 730, "currency": market.currency,
                    "market_hash_name": probe})
                dt = time.monotonic() - t0
                if r.status_code == 200 and '"success":true' in r.text.lower():
                    console.print(f"[green]OK -- not rate limited ({dt:.1f}s).[/green]")
                    raise typer.Exit(0)
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    console.print(f"[red]429 -- this IP is rate limited"
                                  f"{f' (Retry-After {ra}s)' if ra else ''}.[/red]")
                    console.print("[yellow]Stop all requests. Every attempt while "
                                  "banned can extend it. Bans commonly last hours; "
                                  "a router reboot that changes your IP also clears "
                                  "it.[/yellow]")
                else:
                    console.print(f"[yellow]HTTP {r.status_code} ({dt:.1f}s) -- "
                                  "not a clean success.[/yellow]")
            except typer.Exit:
                raise
            except Exception as e:
                console.print(f"[red]{type(e).__name__}: {e}[/red]")
            if not wait:
                raise typer.Exit(1)
            console.print(f"[dim]sleeping {poll:.0f}s...[/dim]")
            time.sleep(poll)
    finally:
        market.close()


# ---------------------------------------------------------------------------
@app.command("bulk-warm")
def bulk_warm(
    currency: str = "USD",
    rarity: Optional[str] = typer.Option(
        None, help="Only keep names for this tier (default: cache everything)"),
    souvenir: bool = typer.Option(True, help="Include Souvenir variants when filtering"),
    page_delay: float = typer.Option(6.0, help="Seconds between pages. Raise if 429s."),
    max_pages: int = typer.Option(0, help="Stop after N pages (0 = all)"),
    export: bool = typer.Option(True, help="Write data/price_snapshot.json at the end"),
):
    """Price the whole market in ~60 requests instead of ~5600.

    Uses Steam's paginated market search (100 items per request) -- the same
    endpoint the market web page calls -- rather than one-item-at-a-time
    priceoverview. This is the recommended way to fill an empty cache.

    Caveat: bulk data has the lowest listing price and a LISTING COUNT, but
    no true median and no 24h sales volume. Run `warm-prices` afterwards on
    the handful of names in your shortlist to get precise figures.
    """
    wanted: Optional[set[str]] = None
    if rarity:
        db = SkinDB.load()
        r = RARITIES[rarity.lower()]
        wanted = _tier_names(db, r, souvenir, include_outputs=True)
        console.print(f"Filtering to {len(wanted)} names for {r.label} "
                      "(plus its output tier).")

    market = BulkMarket(currency=currency, page_delay=page_delay)
    console.print("[dim]Walking the CS2 market 100 items per request. "
                  "Ctrl+C is safe - every page is committed as it arrives.[/dim]")
    t0 = time.monotonic()
    try:
        market.sweep(wanted=wanted, max_pages=max_pages,
                     progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted -- pages fetched so far are cached.[/yellow]")
    finally:
        market.close()

    s = market.stats
    t = Table(title=f"bulk-warm summary ({(time.monotonic()-t0)/60:.1f} min)")
    t.add_column("Metric")
    t.add_column("Count", justify="right")
    for label, v in (("pages fetched", s.pages), ("items seen", s.items),
                     ("quotes cached", s.written), ("rate limited", s.rate_limited),
                     ("retries", s.retries), ("errors", s.errors)):
        t.add_row(label, str(v))
    console.print(t)
    if s.rate_limited:
        console.print(f"[yellow]Hit {s.rate_limited} rate limits. Re-run with "
                      f"--page-delay {page_delay * 2:.0f}.[/yellow]")
    if export:
        n = market.cache.export(Path("data/price_snapshot.json"), market.currency)
        console.print(f"[green]{n} quotes -> data/price_snapshot.json[/green]")
    console.print("[dim]Bulk quotes have no median/volume. Run 'warm-prices' on "
                  "your shortlist before committing money.[/dim]")


# ---------------------------------------------------------------------------
def _heartbeat(market, total, counter, stop) -> None:
    """Render live progress from the client's own state, once a second."""
    with console.status("") as st:
        while not stop.is_set():
            s = market.state
            phase = s.phase
            if phase in ("backoff", "throttling") and s.elapsed > 2:
                phase = f"[yellow]{phase} {s.elapsed:.0f}s[/yellow]"
            elif phase == "requesting" and s.elapsed > 10:
                phase = f"[yellow]requesting {s.elapsed:.0f}s[/yellow]"
            st.update(
                f"[{counter['i']}/{total}] {phase} {s.detail} | "
                f"cached={market.stats['hits'] + market.stats['empty']} "
                f"new={market.stats['misses']} 429={market.stats['429s']} "
                f"timeout={market.stats['timeouts']} err={market.stats['errors']} "
                f"| {s.item[:44]}")
            stop.wait(1.0)


@app.command("warm-prices")
def warm_prices(
    rarity: str = typer.Option("restricted"),
    currency: str = "USD",
    ttl: float = 8.0,
    include_outputs: bool = True,
    souvenir: bool = typer.Option(True, help="Also fetch Souvenir input variants"),
    rate: int = typer.Option(18, help="Requests per minute. Lower it if you see 429s."),
    timeout: float = typer.Option(20.0, help="Per-request timeout in seconds"),
    limit: int = typer.Option(0, help="Stop after N names (0 = all). Good for a test run."),
    verbose: bool = typer.Option(False, help="Log every network fetch as it happens"),
):
    """Precise per-item prices (median + 24h volume) for one tier.

    SLOW: one request per name. Prefer `bulk-warm` to fill an empty cache,
    and use this only to confirm a shortlist before committing money.
    """
    db = SkinDB.load()
    r = RARITIES[rarity.lower()]
    market = SteamMarket(currency=currency, ttl_hours=ttl, rate=rate, timeout=timeout)

    ordered = sorted(_tier_names(db, r, souvenir, include_outputs))
    if limit:
        ordered = ordered[:limit]

    # Count what is genuinely missing so the ETA reflects real work
    todo = [n for n in ordered
            if market.cache.get(n, market.currency, market.ttl) is None
            and not market.cache.is_known_miss(n, market.currency, market.miss_ttl)]
    console.print(f"{len(ordered)} names; {len(ordered) - len(todo)} already cached, "
                  f"{len(todo)} to fetch at {rate}/min "
                  f"(~{len(todo)/max(rate,1):.0f} min). "
                  "Ctrl+C is safe: progress is cached and resumes.")
    if not todo:
        console.print("[green]Nothing to fetch -- cache is already complete.[/green]")

    counter = {"i": 0}
    stop = threading.Event()
    hb = None
    if not verbose:
        hb = threading.Thread(target=_heartbeat,
                              args=(market, len(ordered), counter, stop), daemon=True)
        hb.start()

    t0 = time.monotonic()
    try:
        for i, n in enumerate(ordered, 1):
            counter["i"] = i
            fresh = False
            s0 = t0
            if verbose:
                fresh = (market.cache.get(n, market.currency, market.ttl) is None
                         and not market.cache.is_known_miss(
                             n, market.currency, market.miss_ttl))
                if fresh:
                    console.print(f"[dim][{i}/{len(ordered)}] fetching {n}[/dim]")
                    s0 = time.monotonic()
            market.get(n)
            if verbose and fresh:
                console.print(f"[dim]    done in {time.monotonic()-s0:.1f}s[/dim]")
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted -- everything fetched so far is "
                      "cached. Re-run to resume.[/yellow]")
    finally:
        stop.set()
        if hb:
            hb.join(timeout=2)

    mins = (time.monotonic() - t0) / 60
    n_export = market.cache.export(Path("data/price_snapshot.json"), market.currency)
    market.close()

    t = Table(title=f"warm-prices summary ({mins:.1f} min)")
    t.add_column("Metric")
    t.add_column("Count", justify="right")
    for k, label in (("hits", "from cache"), ("misses", "fetched"),
                     ("empty", "no listing"), ("429s", "rate limited"),
                     ("timeouts", "timed out"), ("errors", "errors"),
                     ("gave_up", "gave up after retries")):
        t.add_row(label, str(market.stats[k]))
    console.print(t)
    console.print(f"[green]{n_export} quotes -> data/price_snapshot.json[/green]")
    if market.stats["429s"] > max(market.stats["misses"], 1) * 0.05:
        console.print(f"[yellow]Over 5% of fetches were rate limited. Re-run with "
                      f"--rate {max(6, rate // 2)}, or use bulk-warm.[/yellow]")


# ---------------------------------------------------------------------------
@app.command("verify-souvenir")
def verify_souvenir(
    rarity: str = typer.Option("consumer"),
    currency: str = "USD",
    offline: Optional[Path] = typer.Option(None, help="Use a price snapshot JSON"),
    sample: int = typer.Option(40, help="How many skins to probe"),
    rate: int = typer.Option(10),
):
    """Check whether 'Souvenir <skin>' listings actually exist on the market.

    The skin database can only infer which finishes have a Souvenir variant.
    This asks the market directly for a sample and reports the hit rate.
    """
    db = SkinDB.load()
    r = RARITIES[rarity.lower()]
    policy = PricingPolicy(min_input_volume=1)
    prices = (PriceService(PriceBook.load(offline), policy) if offline
              else PriceService(SteamMarket(currency=currency, rate=rate), policy))

    cands = [s for c in db.collections(r) for s in db.pool(c, r) if s.souvenir_capable]
    if not cands:
        console.print(f"[yellow]No {r.label} skin is marked Souvenir-capable.[/yellow]")
        raise typer.Exit(0)

    probe = cands[:: max(1, len(cands) // max(sample, 1))][:sample]
    console.print(f"Probing {len(probe)} of {len(cands)} Souvenir-capable "
                  f"{r.label} skins...")

    hit = miss = 0
    t = Table(title="Souvenir listing check")
    for c in ("Skin", "Wear", "Souvenir price", "Normal price"):
        t.add_column(c, justify="left" if c == "Skin" else "right")
    with console.status("") as st:
        for i, s in enumerate(probe, 1):
            wears = s.reachable_wears()
            if not wears:
                continue
            wear = wears[len(wears) // 2]
            sv = prices.buy_price(s, wear, souvenir=True)
            nm = prices.buy_price(s, wear, souvenir=False)
            if sv:
                hit += 1
                if hit <= 15:
                    t.add_row(s.name[:38], wear, f"{sv:.2f}",
                              f"{nm:.2f}" if nm else "-")
            else:
                miss += 1
            st.update(f"[{i}/{len(probe)}] found={hit} missing={miss}")
    if hit:
        console.print(t)
    pct = hit / max(hit + miss, 1)
    console.print(f"[{'green' if pct > 0.5 else 'yellow'}]"
                  f"{hit}/{hit+miss} ({pct:.0%}) had a real Souvenir listing.[/]")
    if pct < 0.5:
        console.print("[yellow]The Souvenir flag is over-reporting. Scans stay "
                      "correct (unpriced units are skipped) but warm-prices "
                      "wastes requests. Consider --no-souvenir.[/yellow]")


# ---------------------------------------------------------------------------
@app.command()
def scan(
    budget: float = typer.Option(..., help="Max total input cost per contract"),
    rarity: str = typer.Option("restricted"),
    currency: str = "USD",
    offline: Optional[Path] = typer.Option(None, help="Use a price snapshot JSON"),
    db_path: Optional[Path] = typer.Option(None),
    odds: str = typer.Option("outcome_weighted", help="outcome_weighted | per_input"),
    fee: Optional[float] = typer.Option(None, help="Marketplace fee, e.g. 0.02"),
    stattrak: bool = False,
    souvenir: bool = typer.Option(True, help="Allow Souvenir inputs (legal since 22 May 2026)"),
    float_percentile: float = typer.Option(0.15, help="How good a float you can source"),
    min_roi: float = typer.Option(1.05, help="1.05 = require +5% expected return"),
    min_volume: int = typer.Option(3, help="Min 24h sales for an OUTPUT"),
    min_input_volume: int = typer.Option(10, help="Min 24h sales for an INPUT"),
    haircut: float = typer.Option(0.03, help="Assume you sell this much under median"),
    robust: bool = typer.Option(True, help="Require profit under BOTH odds models"),
    top: int = 15,
    json_out: Optional[Path] = None,
):
    """Find the best trade-ups you can build for a given budget."""
    db = SkinDB.load(db_path)
    policy = PricingPolicy(min_volume=min_volume,
                           min_input_volume=min_input_volume, haircut=haircut)
    prices = _price_service(offline, currency, 8.0, policy)
    r = RARITIES[rarity.lower()]

    cands = search(db, prices, r, budget=budget, odds_model=odds, fee_rate=fee,
                   stattrak=stattrak, souvenir=souvenir,
                   float_percentile=float_percentile, min_roi=min_roi,
                   top_n=top * 3,
                   progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    if robust:
        cands = robust_filter(cands, db, prices, fee_rate=fee)
    cands = cands[:top]

    if not cands:
        console.print("[yellow]No candidate cleared the filters. Loosen --min-roi, "
                      "raise --budget, or lower --min-input-volume.[/yellow]")
        raise typer.Exit(0)

    t = Table(title=f"Top trade-ups - {r.label} -> {r.next_tier().label} - "
                    f"budget {budget:.2f} {currency}")
    for c in ("#", "Target collection", "Filler", "k", "Sv", "Cost", "EV net",
              "Profit", "ROI", "P(win)", "sd", "n*"):
        t.add_column(c, justify="left" if c in ("Target collection", "Filler") else "right")
    for i, c in enumerate(cands, 1):
        res = c.result
        t.add_row(str(i), c.target_collection[:32], (c.filler_collection or "-")[:20],
                  str(c.k_target), str(res.n_souvenir_inputs),
                  f"{res.cost:.2f}", f"{res.ev_net:.2f}",
                  f"[{'green' if res.profit > 0 else 'red'}]{res.profit:+.2f}[/]",
                  f"{res.roi*100:.1f}%", f"{res.profit_chance*100:.1f}%",
                  f"{res.stddev:.2f}", str(n_contracts_for_confidence(res) or "-"))
    console.print(t)
    console.print("[dim]Sv = Souvenir inputs. Attributes are stripped; the output "
                  "is always normal quality. n* = contracts needed before the edge "
                  "beats the noise.[/dim]")

    best = cands[0].result
    d = Table(title="Best recipe - outcome breakdown")
    for c in ("Output", "Wear", "Float", "P", "Gross", "Net"):
        d.add_column(c)
    for o in best.outcomes:
        d.add_row(o.skin.name, o.wear, f"{o.out_float:.4f}",
                  f"{o.probability*100:.2f}%", f"{o.gross_price:.2f}",
                  f"{o.net_price:.2f}" + ("" if o.priced else " [red](no data)[/red]"))
    console.print(d)
    console.print("Shopping list: " + ", ".join(
        f"{'Souvenir ' if i.souvenir else ''}{i.skin.name} "
        f"({'%.4f' % i.float_value}) @{i.price:.2f}" for i in best.inputs))

    if json_out:
        Path(json_out).write_text(
            json.dumps([explain(c) for c in cands], indent=2, ensure_ascii=False),
            encoding="utf-8")
        console.print(f"[green]Wrote {json_out}[/green]")


# ---------------------------------------------------------------------------
@app.command("eval")
def eval_recipe(
    recipe: Path = typer.Argument(
        ..., help="JSON: [{name, collection, float, price, souvenir}, ...]"),
    offline: Optional[Path] = None,
    currency: str = "USD",
    odds: str = "outcome_weighted",
    fee: Optional[float] = None,
    stattrak: bool = False,
):
    """Score a hand-built contract."""
    db = SkinDB.load()
    prices = _price_service(offline, currency, 8.0, PricingPolicy())
    items = json.loads(Path(recipe).read_text(encoding="utf-8"))
    inputs = []
    for it in items:
        sid = f"{it['name']}|{it.get('collection')}"
        skin = db.by_id(sid) or db.by_name(it["name"])
        if skin is None:
            raise typer.BadParameter(f"Unknown skin: {sid}")
        inputs.append(InputItem(skin, float(it["float"]), float(it.get("price", 0)),
                                stattrak, bool(it.get("souvenir", False))))
    for p in validate_contract(inputs):
        console.print(f"[yellow]Rule warning: {p}[/yellow]")
    need = contract_size(inputs[0].skin.rarity)
    if len(inputs) != need:
        console.print(f"[yellow]Warning: {len(inputs)} inputs, "
                      f"contract expects {need}[/yellow]")
    res = evaluate_contract(inputs, db.pool, prices.price_fn(), odds_model=odds,
                            fee_rate=fee, stattrak=stattrak, skin_by_id=db.by_id)
    payload = explain(type("C", (), {
        "result": res, "target_collection": inputs[0].skin.collection,
        "filler_collection": None, "k_target": len(inputs)})())
    console.print_json(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()
