"""Standalone Steam Market diagnostic. Run from the project root:

    python diagnose_steam.py --currency BRL

Answers three questions a progress bar cannot:
  1. Is Steam responding to *this* IP at all, and how fast?
  2. Is it rate-limiting (429) or soft-banning (persistent 429 / empty 200)?
  3. How far along is the cache, and is it still growing?

Safe to run while a warm-prices/bulk-warm job is going: it makes at most a
handful of requests and only reads the cache.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import httpx

CURRENCY = {"USD": 1, "GBP": 2, "EUR": 3, "BRL": 7, "RUB": 5, "CNY": 23}
URL = "https://steamcommunity.com/market/priceoverview/"

# Cheap, extremely liquid items that definitely exist on the market
PROBES = [
    "AK-47 | Elite Build (Field-Tested)",
    "MP9 | Storm (Field-Tested)",
    "P250 | Sand Dune (Field-Tested)",
]


def probe(currency: int, timeout: float, n: int) -> None:
    client = httpx.Client(
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": "cs2-tradeup-lab/1.0 (diagnostic)"},
    )
    print(f"\n--- Live requests (timeout={timeout}s) ---")
    for name in PROBES[:n]:
        t0 = time.monotonic()
        try:
            r = client.get(URL, params={
                "appid": 730, "currency": currency, "market_hash_name": name})
            dt = time.monotonic() - t0
            ra = r.headers.get("Retry-After")
            body = (r.text or "")[:90].replace("\n", " ")
            print(f"  {dt:6.2f}s  HTTP {r.status_code}"
                  f"{f'  Retry-After={ra}' if ra else ''}  {name}")
            print(f"          {body}")
            if r.status_code == 429:
                print("          -> RATE LIMITED. Stop all requests and wait;")
                print("             retrying while banned can extend the ban.")
            elif r.status_code == 200 and '"success":true' not in r.text.lower():
                print("          -> 200 but not a successful quote (soft-ban shape).")
        except httpx.TimeoutException:
            print(f"  {timeout:6.2f}s  TIMEOUT   {name}")
            print("          -> Steam accepted the connection but never replied.")
        except httpx.HTTPError as e:
            print(f"  {time.monotonic()-t0:6.2f}s  {type(e).__name__}: {e}")
        time.sleep(4)
    client.close()


def cache_report(path: Path, currency: int) -> None:
    print(f"\n--- Cache ({path}) ---")
    if not path.exists():
        print("  no cache file yet")
        return
    db = sqlite3.connect(str(path))
    total = db.execute("SELECT count(*) FROM prices WHERE currency=?",
                       (currency,)).fetchone()[0]
    priced = db.execute(
        "SELECT count(*) FROM prices WHERE currency=? AND median IS NOT NULL",
        (currency,)).fetchone()[0]
    print(f"  rows: {total}   with median: {priced}")
    if total and not priced:
        print("  (no medians -> this cache came from bulk-warm, which is expected)")
    row = db.execute(
        "SELECT name, ts FROM prices WHERE currency=? ORDER BY ts DESC LIMIT 1",
        (currency,)).fetchone()
    if row:
        age = time.time() - row[1]
        print(f"  newest: {row[0]}")
        print(f"  written {age:.0f}s ago"
              + ("   <-- STALLED: nothing new is being cached"
                 if age > 120 else "   (actively writing)"))
    recent = db.execute("SELECT count(*) FROM prices WHERE currency=? AND ts > ?",
                        (currency, time.time() - 300)).fetchone()[0]
    print(f"  written in last 5 min: {recent}  (~{recent/5:.1f}/min)")
    try:
        misses = db.execute("SELECT count(*) FROM misses WHERE currency=?",
                            (currency,)).fetchone()[0]
        print(f"  known-empty names cached: {misses}")
    except sqlite3.OperationalError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--cache", default="data/prices.sqlite")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--requests", type=int, default=2)
    a = ap.parse_args()
    cur = CURRENCY.get(a.currency.upper(), 1)
    print(f"Currency {a.currency.upper()} (id {cur})")
    cache_report(Path(a.cache), cur)
    probe(cur, a.timeout, a.requests)
    print("\nIf live requests succeed but the cache is stalled, the running job "
          "is stuck in a backoff sleep -- restart it, preferably with bulk-warm.")


if __name__ == "__main__":
    main()
