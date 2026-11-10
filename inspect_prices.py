"""Audit the price cache you have actually collected.

Run from the project root:

    python inspect_prices.py --currency BRL
    python inspect_prices.py --currency BRL --rarity milspec
    python inspect_prices.py --currency BRL --rarity milspec --missing 30

Standalone: reads data/prices.sqlite and data/skins.json directly, so it
works even if the package code is mid-patch. Read-only -- safe to run while
a bulk-warm job is going.

It answers, in order:
  1. Is there data, is it in the right currency, and is it fresh?
  2. What fraction of each tier can actually be priced?
  3. Which specific names are still missing?
  4. Do the cached numbers look like real prices?
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

CURRENCY = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}
CUR_SYM = {1: "$", 2: "£", 3: "€", 7: "R$", 5: "₽", 23: "¥"}

WEARS = [("Factory New", 0.00, 0.07), ("Minimal Wear", 0.07, 0.15),
         ("Field-Tested", 0.15, 0.38), ("Well-Worn", 0.38, 0.45),
         ("Battle-Scarred", 0.45, 1.00)]

TIERS = {0: "Consumer", 1: "Industrial", 2: "Mil-Spec", 3: "Restricted",
         4: "Classified", 5: "Covert", 6: "Rare Special"}


def reachable_wears(mn: float, mx: float) -> list[str]:
    return [n for n, lo, hi in WEARS if mn < hi and mx > lo]


def hash_name(name: str, wear: str, souvenir: bool = False) -> str:
    return f"{'Souvenir ' if souvenir else ''}{name} ({wear})"


def bar(frac: float, width: int = 28) -> str:
    filled = int(round(frac * width))
    return "#" * filled + "." * (width - filled)


def load_skins(path: Path) -> list[dict]:
    if not path.exists():
        print(f"  ! {path} not found -- run: python -m tradeup.cli sync-db")
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--cache", default="data/prices.sqlite")
    ap.add_argument("--skins", default="data/skins.json")
    ap.add_argument("--rarity", default=None,
                    help="consumer|industrial|milspec|restricted|classified")
    ap.add_argument("--missing", type=int, default=0,
                    help="List N missing market names for the chosen tier")
    ap.add_argument("--souvenir", action="store_true", default=True)
    ap.add_argument("--no-souvenir", dest="souvenir", action="store_false")
    a = ap.parse_args()

    cur = CURRENCY.get(a.currency.upper(), 1)
    sym = CUR_SYM.get(cur, "")
    cache = Path(a.cache)

    print("=" * 68)
    print(f"PRICE CACHE AUDIT   currency={a.currency.upper()} (id {cur})")
    print("=" * 68)

    if not cache.exists():
        print(f"\n! {cache} does not exist. Nothing has been cached yet.")
        return
    db = sqlite3.connect(str(cache))

    # -- 1. contents -----------------------------------------------------
    total = db.execute("SELECT count(*) FROM prices").fetchone()[0]
    here = db.execute("SELECT count(*) FROM prices WHERE currency=?",
                      (cur,)).fetchone()[0]
    print(f"\n1. CONTENTS")
    print(f"   rows total ......... {total}")
    print(f"   rows in {a.currency.upper():<4} ........ {here}")

    if total and not here:
        others = db.execute(
            "SELECT currency, count(*) FROM prices GROUP BY currency").fetchall()
        print("   ! nothing in this currency. Found:")
        for c, n in others:
            name = next((k for k, v in CURRENCY.items() if v == c), f"id{c}")
            print(f"       {name} ({c}): {n} rows")
        print("   -> re-run with the currency you actually fetched.")
        return
    if not here:
        print("   ! cache is empty.")
        return

    med = db.execute(
        "SELECT count(*) FROM prices WHERE currency=? AND median IS NOT NULL",
        (cur,)).fetchone()[0]
    print(f"   with median ........ {med}"
          + ("   (from `tradeup warm`)" if med else
             "   (0 -> all from bulk-warm, which is expected)"))
    try:
        misses = db.execute("SELECT count(*) FROM misses WHERE currency=?",
                            (cur,)).fetchone()[0]
        print(f"   known-empty names .. {misses}")
    except sqlite3.OperationalError:
        pass

    newest = db.execute(
        "SELECT name, ts FROM prices WHERE currency=? ORDER BY ts DESC LIMIT 1",
        (cur,)).fetchone()
    oldest = db.execute(
        "SELECT ts FROM prices WHERE currency=? ORDER BY ts ASC LIMIT 1",
        (cur,)).fetchone()
    if newest:
        age_new = (time.time() - newest[1]) / 3600
        age_old = (time.time() - oldest[0]) / 3600
        print(f"   newest entry ....... {age_new:.1f} h old")
        print(f"   oldest entry ....... {age_old:.1f} h old")
        if age_old > 24:
            print("   ! some entries are over a day old; prices drift.")

    # -- 2. do the numbers look real? ------------------------------------
    print(f"\n2. SANITY  (are these plausible {a.currency.upper()} prices?)")
    lo, hi, avg = db.execute(
        "SELECT min(lowest), max(lowest), avg(lowest) FROM prices "
        "WHERE currency=? AND lowest IS NOT NULL", (cur,)).fetchone()
    print(f"   cheapest ........... {sym}{lo:.2f}")
    print(f"   dearest ............ {sym}{hi:,.2f}")
    print(f"   mean ............... {sym}{avg:.2f}")
    zero = db.execute("SELECT count(*) FROM prices WHERE currency=? AND "
                      "(lowest IS NULL OR lowest<=0)", (cur,)).fetchone()[0]
    if zero:
        print(f"   ! {zero} rows have no usable price")
    if cur == 7 and avg < 3:
        print("   ! mean looks low for BRL -- possibly USD data mislabelled.")

    print("\n   sample (most recently written):")
    for n, l, m, v in db.execute(
            "SELECT name, lowest, median, volume FROM prices WHERE currency=? "
            "ORDER BY ts DESC LIMIT 6", (cur,)):
        mtxt = f"{sym}{m:.2f}" if m is not None else "  -  "
        print(f"     {sym}{l:>9.2f}  med {mtxt:>9}  n={v or 0:<6} {n[:44]}")

    # -- 3. coverage per tier --------------------------------------------
    skins = load_skins(Path(a.skins))
    if not skins:
        db.close()
        return

    cached = {r[0] for r in db.execute(
        "SELECT name FROM prices WHERE currency=? AND lowest > 0", (cur,))}

    want_tiers = ([k for k, v in TIERS.items() if v.lower().replace("-", "")
                   == a.rarity.lower().replace("-", "")] if a.rarity
                  else [0, 1, 2, 3, 4, 5])

    print(f"\n3. COVERAGE  (share of market names your scans can price)")
    print(f"   {'tier':<12}{'have':>7}{'need':>7}{'':>3}{'':<30}")
    tier_missing: dict[int, list[str]] = {}
    for t in sorted(want_tiers):
        names: set[str] = set()
        for s in skins:
            if s["rarity"] != t:
                continue
            for w in reachable_wears(s["min_float"], s["max_float"]):
                names.add(hash_name(s["name"], w))
                if a.souvenir and s.get("souvenir_capable"):
                    names.add(hash_name(s["name"], w, souvenir=True))
        if not names:
            continue
        have = names & cached
        tier_missing[t] = sorted(names - have)
        frac = len(have) / len(names)
        print(f"   {TIERS[t]:<12}{len(have):>7}{len(names):>7}  "
              f"{bar(frac)} {frac*100:5.1f}%")

    # a tier is only scannable if its OUTPUT tier is priced too
    def coverage(tier: int) -> tuple[int, int]:
        names = set()
        for s in skins:
            if s["rarity"] != tier:
                continue
            for w in reachable_wears(s["min_float"], s["max_float"]):
                names.add(hash_name(s["name"], w))
        return len(names & cached), len(names)

    print(f"\n   scan readiness (a tier needs its OUTPUT tier priced too):")
    for t in sorted(want_tiers):
        nxt = t + 1
        if nxt not in TIERS or t not in tier_missing:
            continue
        have_o, need_o = coverage(nxt)
        if not need_o:
            continue
        ofrac = have_o / need_o
        verdict = ("READY" if ofrac > 0.8 else
                   "partial" if ofrac > 0.3 else "not yet")
        print(f"     {TIERS[t]:<12} -> {TIERS[nxt]:<12} "
              f"outputs {ofrac*100:5.1f}%   {verdict}")

    # -- 4. what is missing ----------------------------------------------
    if a.missing and a.rarity:
        t = want_tiers[0]
        miss = tier_missing.get(t, [])
        print(f"\n4. MISSING from {TIERS[t]} ({len(miss)} names, showing "
              f"{min(a.missing, len(miss))})")
        for n in miss[:a.missing]:
            print(f"     {n}")
        if miss:
            print("\n   Missing names usually have no live listing at all.")
            print("   Confirm one by searching it on the Steam market.")

    print("\n" + "=" * 68)
    if med:
        print("Median data present -- volume filters mean 24h SALES, as intended.")
    else:
        print("No medians: this is bulk data, so the volume column holds")
        print("SELL LISTINGS (supply), not 24h sales.")
        print("  --min-input-volume 10  still reads well: 'at least 10 listed',")
        print("                         which is exactly what you need to buy 10.")
        print("  --min-volume 3         is weaker than intended for outputs; it")
        print("                         means 3 listed, not 3 sold per day.")
        print("Confirm a shortlist with `tradeup warm` before committing money.")
    print("=" * 68)
    db.close()


if __name__ == "__main__":
    main()
