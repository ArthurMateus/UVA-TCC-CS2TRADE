"""Re-score scan results for liquidity, using your existing price cache.

    python -m tradeup.cli scan ... --json-out results.json
    python check_liquidity.py results.json --currency BRL

Standalone and read-only: it reads data/prices.sqlite directly, so it works
before you patch anything.

A trade-up EV built on the lowest listing assumes you can sell instantly at
that price. For a popular Field-Tested rifle that is roughly true. For an
unpopular skin in Well-Worn it is fiction -- three listings, no recent
sales, and a price one optimistic seller typed in.

This reports two things the EV number hides:

  REALIZATION   what you should expect to clear, given how thin the book is
  CONCENTRATION how much of the EV rides on a single illiquid outcome

Plus the only question that matters for a concentrated recipe: how wrong
can that one price be before the contract loses money?
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

CURRENCY = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}
CUR_SYM = {1: "$", 2: "£", 3: "€", 7: "R$", 5: "₽", 23: "¥"}

# (max_listings, label, realization factor)
TIERS = [(3, "dead", 0.55), (15, "thin", 0.78), (60, "ok", 0.93),
         (10 ** 9, "deep", 0.98)]
VOL_TIERS = [(1, "dead", 0.55), (5, "thin", 0.78), (25, "ok", 0.93),
             (10 ** 9, "deep", 0.98)]

MAX_EV_SHARE = 0.85       # above this, one outcome dominates the recipe
MIN_LISTINGS_BUY = 12     # need to buy ten copies


def classify(listings, volume):
    """-> (label, realization). 24h sales beat supply when available."""
    if volume is not None:
        for cap, label, f in VOL_TIERS:
            if volume < cap:
                return label, f
    if listings is None:
        return "unknown", 0.55
    for cap, label, f in TIERS:
        if listings < cap:
            return label, f
    return "deep", 0.98


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="JSON from scan --json-out")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--cache", default="data/prices.sqlite")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-ev-share", type=float, default=MAX_EV_SHARE)
    a = ap.parse_args()

    cur = CURRENCY.get(a.currency.upper(), 1)
    sym = CUR_SYM.get(cur, "")

    rp, cp = Path(a.results), Path(a.cache)
    if not rp.exists():
        print(f"! {rp} not found. Run scan with --json-out {rp} first.")
        sys.exit(1)
    if not cp.exists():
        print(f"! {cp} not found.")
        sys.exit(1)

    rows = json.loads(rp.read_text(encoding="utf-8"))
    db = sqlite3.connect(str(cp))

    def depth(name: str):
        r = db.execute(
            "SELECT volume, median FROM prices WHERE name=? AND currency=?",
            (name, cur)).fetchone()
        if not r:
            return None, None
        vol, med = r
        # with no median the cache came from bulk-warm: `volume` holds
        # SELL LISTINGS (supply), not 24h sales
        return (None, vol) if med is not None else (vol, None)

    print("=" * 100)
    print(f"LIQUIDITY RE-SCORE   {len(rows)} recipes   currency {a.currency.upper()}")
    print("=" * 100)
    print("\nEV assumes you sell at the listed price. These numbers assume you")
    print("have to find a real buyer.\n")

    hdr = (f"{'#':>3} {'target collection':<28}{'cost':>9}{'EV raw':>9}"
           f"{'EV adj':>9}{'profit':>9}{'ROI':>8}  {'top outcome share':<20}{'verdict'}")
    print(hdr)
    print("-" * 100)

    keep = []
    for i, r in enumerate(rows[:a.top], 1):
        cost = r["cost"]
        outs = r["outcomes"]
        ev_raw = sum(o["p_pct"] / 100 * o["net"] for o in outs)

        ev_adj, contribs, worst = 0.0, [], "deep"
        order = {"dead": 0, "unknown": 0, "thin": 1, "ok": 2, "deep": 3}
        for o in outs:
            name = f"{o['skin']} ({o['wear']})"
            listings, vol = depth(name)
            label, f = classify(listings, vol)
            if order[label] < order[worst]:
                worst = label
            c = o["p_pct"] / 100 * o["net"] * f
            ev_adj += c
            contribs.append((c, name, label, listings, vol, o))

        # can you even buy the inputs? need 10 copies of each distinct skin
        short = []
        for key in {x["skin"] for x in r["inputs"]}:
            need = sum(1 for x in r["inputs"] if x["skin"] == key)
            best = None
            for nm, vol, med in db.execute(
                    "SELECT name, volume, median FROM prices WHERE name LIKE ? "
                    "AND currency=?", (f"{key} (%", cur)):
                if best is None or (vol or 0) > (best[0] or 0):
                    best = (vol, med)
            if best is None:
                continue
            listings = None if best[1] is not None else best[0]
            vol24 = best[0] if best[1] is not None else None
            if vol24 is not None and vol24 >= need * 2:
                continue
            if listings is not None and listings < max(MIN_LISTINGS_BUY, need):
                short.append((key, listings, need))

        contribs.sort(reverse=True, key=lambda x: x[0])
        share = contribs[0][0] / ev_adj if ev_adj > 0 else 0.0
        top_label = contribs[0][2]
        profit = ev_adj - cost
        roi = ev_adj / cost if cost else 0
        concentrated = share > a.max_ev_share and top_label in ("dead", "thin", "unknown")

        verdict = ("REJECT" if profit <= 0 else
                   "NO SUPPLY" if short else
                   "RISKY" if concentrated else "ok")
        if verdict not in ("REJECT", "NO SUPPLY"):
            keep.append((i, r, contribs, ev_adj, profit, roi, concentrated))

        print(f"{i:>3} {r['target_collection'][:27]:<28}{cost:>9.2f}{ev_raw:>9.2f}"
              f"{ev_adj:>9.2f}{profit:>+9.2f}{roi*100:>7.1f}%  "
              f"{share*100:>5.1f}% {top_label:<13} {verdict}")
        for key, listed, need in short[:2]:
            print(f"      ! only {listed} listed of {key[:40]} -- need {need}")

    # ---- detail on the best surviving recipe --------------------------
    print("\n" + "=" * 100)
    if not keep:
        print("Nothing survives a liquidity haircut. Every recipe above depends")
        print("on selling into a market that may not absorb it.")
        db.close()
        return

    i, r, contribs, ev_adj, profit, roi, conc = keep[0]
    print(f"DETAIL  recipe #{i}  {r['target_collection']}")
    print("=" * 100)
    print(f"\n{'outcome':<40}{'P':>6}{'net':>10}{'listed':>8}{'24h':>6}"
          f"{'tier':>7}{'EV share':>10}")
    for c, name, label, listings, vol, o in contribs:
        sh = c / ev_adj * 100 if ev_adj else 0
        print(f"{name[:39]:<40}{o['p_pct']:>5.1f}%{o['net']:>10.2f}"
              f"{(listings if listings is not None else '-'):>8}"
              f"{(vol if vol is not None else '-'):>6}{label:>7}{sh:>9.1f}%")

    # break-even on the dominant outcome
    top_c, top_name, top_label, _, _, top_o = contribs[0]
    other = sum(o["p_pct"] / 100 * o["net"] for o in r["outcomes"]
                if f"{o['skin']} ({o['wear']})" != top_name)
    p, net = top_o["p_pct"] / 100, top_o["net"]
    if p > 0 and net > 0:
        be = (r["cost"] - other) / (p * net)
        print(f"\nBreak-even: you need {be*100:.1f}% of the listed price on")
        print(f"  {top_name}")
        if be > 0.9:
            print("  -> almost no margin for error. One stale listing and this loses.")
        elif be > 0.7:
            print("  -> some cushion, but verify that price manually.")
        else:
            print("  -> comfortable cushion.")

    print("\nInputs to buy (10 copies each -- check supply):")
    seen = set()
    for inp in r["inputs"]:
        wear = None
        for o in r["outcomes"]:
            pass
        key = inp["skin"]
        if key in seen:
            continue
        seen.add(key)
        n = sum(1 for x in r["inputs"] if x["skin"] == key)
        # find the listing count for any wear of this input
        best = None
        for row in db.execute(
                "SELECT name, volume, median FROM prices WHERE name LIKE ? "
                "AND currency=?", (f"{key} (%", cur)):
            nm, vol, med = row
            if best is None or (vol or 0) > (best[1] or 0):
                best = (nm, vol, med)
        if best:
            listings, vol = (None, best[1]) if best[2] is not None else (best[1], None)
            label, _ = classify(listings, vol)
            flag = ("  <- too few listed to buy 10"
                    if listings is not None and listings < MIN_LISTINGS_BUY else "")
            print(f"  {n:>2}x {key[:44]:<46}{sym}{inp['price']:>8.2f}  "
                  f"{label:<6}{flag}")
        else:
            print(f"  {n:>2}x {key[:44]:<46}{sym}{inp['price']:>8.2f}  no depth data")

    print("\n" + "=" * 100)
    print("Realization factors are blunt (dead 55%, thin 78%, ok 93%, deep 98%).")
    print("They encode 'fewer listings and no sales means you get less', which is")
    print("certain -- not a calibrated forecast. Use this to RANK, then confirm")
    print("the survivors with warm-prices for real 24h sales volume.")
    print("=" * 100)
    db.close()


if __name__ == "__main__":
    main()
