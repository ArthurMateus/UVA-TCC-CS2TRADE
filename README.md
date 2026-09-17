# CS2 Trade-Up Lab

A free, self-hosted trade-up analysis engine for Counter-Strike 2. It finds
contracts where **every outcome beats the cost** — not lottery tickets with a
3% chance of a payout.

```powershell
pip install -r requirements.txt
tradeup sync-db                      # skin database + float caps
tradeup sync-images                  # skin images (optional)
tradeup warm --rarity milspec --currency BRL
tradeup guaranteed --budget 200 --currency BRL
tradeup serve                        # http://localhost:8000
```

---

## What changed in v2, and why

### 1. Expected value was finding lottery tickets

The old default ranked by EV. That surfaced contracts like this:

```
cost 101.80   EV 118.55   ROI 116%     <- looks great
  M4A4 | Modern Hunter (WW)   50%   236.19
  M4A4 | Sheet Lightning (FT) 25%     1.00
  Glock-18 | Glockingbird(FT) 25%     0.81
```

99.6% of that EV is one Well-Worn skin. You lose money on half the rolls, and
the entire edge rests on a single illiquid price being correct. EV is the
right metric only if you can run a contract enough times for the average to
materialise. At R$100 a go, nobody can.

**v2 optimises for the single roll.** The headline metric is now
`P(win)` — how often *one* contract profits — and the default mode only
returns contracts that cannot lose at all.

### 2. Guaranteed contracts are a closed form, not a search

A contract is risk-free when the *cheapest possible outcome* still beats what
you paid. For a single-collection contract every outcome is equally likely,
so:

```
guaranteed  ⟺  min(outcome_net) > total_input_cost
            ⟺  cheapest_outcome × 0.8696 × (1−haircut) > n × input_price
            ⟺  input_price < cheapest_outcome × 0.8696 / n
```

That is a constraint you can solve directly. For each collection: look up the
cheapest next-tier outcome, divide by ~11.5, and any input below that price
makes the whole contract safe. **O(collections), ~3 ms** — versus the
combinatorial search the old optimizer ran.

These exist because input and output prices are set by different supply
dynamics. A Mil-Spec skin nobody wants can trade far below 1/11.5 of the
cheapest Restricted in its own collection.

```
tradeup guaranteed --budget 200

# ┃ Collection              ┃ Cost ┃ Worst ┃  Best ┃ Guaranteed ┃ Floor ROI ┃
# 1 │ The Militia Collection │ 2.70 │ 16.09 │ 31.90 │     +13.39 │      596% │
```

*Floor ROI* is the return on the **worst** roll, not the average.

### 3. Price fetching self-tunes instead of guessing

The old client used a fixed 18 requests/min. Too fast and you collect 429s,
each costing an exponential backoff — so throughput collapses below what a
slower fixed rate would have given. Too slow and a sweep takes six hours.
There is no correct constant: the limit varies by endpoint, time of day, and
how much credit your IP has recently spent.

v2 uses **AIMD** — the same congestion control TCP uses:

| event | response |
|---|---|
| 25 clean responses | rate `+2`/min |
| any 429 | rate `× 0.5`, freeze increases for 90 s |
| 5 consecutive 429s | circuit breaker opens, all traffic stops |

Plus **concurrency** (8 in flight by default) so latency stops dominating,
and a **negative cache** so names Steam has no listing for are never
re-fetched. Requests overlap; the shared token bucket keeps the aggregate
rate at whatever AIMD has settled on.

One subtlety worth knowing: concurrent requests return several 429s for a
*single* congestion event. Halving per response would collapse the rate to
the floor after one incident, so decreases are limited to one per 10-second
window — exactly as TCP reduces once per round trip.

### 4. Risk is visible, not buried in an average

Every contract now reports:

| metric | meaning |
|---|---|
| **P(win)** | chance a single roll profits |
| **worst case** | what you get on the cheapest outcome |
| **grade** | guaranteed · strong · even · risky · lottery |
| **expected loss** | `E[loss \| you lose]` |
| **risk/reward** | expected profit per unit of worst-case loss |
| **top outcome share** | >85% means the edge rests on one price |

`n*` is still reported, but it assumes the *prices are correct* and only the
roll is random. It cannot see a mispriced skin. That is why concentration is
tracked separately.

---

## Why Python, not a rewrite

You asked whether another language would be better. Honest answer: no, and
rewriting would cost more than it gains.

The bottleneck is **Steam's rate limit**, not compute. A guaranteed-contract
search over 86 collections takes ~3 ms in Python; the price fetch it depends
on takes minutes because the server throttles us. Rewriting in Go or Rust
would make a 3 ms step into a 0.3 ms step and change nothing that matters.

The engine also carries a lot of hard-won correctness — per-item odds, the
two-stage float rescale, the 13% fee, Souvenir rules, encoding. Rewriting
reintroduces every one of those bugs. The frontend is a single dependency-free
HTML file: no build step, no npm, loads instantly.

What *did* change is the architecture: async I/O, adaptive limiting, and a
closed-form search replacing a combinatorial one.

---

## The maths

### Float: rescaled twice, using each skin's own cap

Output wear is deterministic. Each input is normalised by **its own** float
cap, averaged, then rescaled into the **output's** cap:

```
trade_up_float = (float − min) / (max − min)
average        = mean of those over n inputs
outcome_float  = average × (out_max − out_min) + out_min
```

Averaging raw floats is the pre-Retakes model and is wrong whenever caps
differ. Verified to 8 decimals:

| case | expected | computed |
|---|---|---|
| MAC-10 \| Sakkaku 0.22798392 in 0.21–0.79 | 0.031007 | 0.031007 |
| 10× P90 \| Trigon 0.749 in 0.08–0.75 | 0.99850746 | 0.99850746 |
| → AWP \| Asiimov cap 0.18–1.00 | 0.998776119 | 0.998776119 |
| → AUG \| Chameleon cap 0.00–0.50 | 0.499253731 | 0.499253731 |

One contract produces a different float — and possibly a different wear — for
each outcome, because each output has its own cap. The UI shows the cap
beside every skin.

### Odds: per item

```
P(collection C) = inputs_from_C / n
P(skin s in C)  = (inputs_from_C / n) / outcomes_in_C
```

Seven of ten inputs from a 3-outcome collection → 70% collection, 23.33% per
skin. One M4A1-S | Knight among ten → a flat **10%** at Dragon Lore, because
Cobblestone has exactly one outcome above it. The legacy pre-Retakes model
returns 5.26% for everything and is kept only for comparison
(`--odds outcome_weighted`).

### Fee: 13% of what the buyer pays

Valve charges 5% + 10% **of the seller's proceeds**, each floor-rounded with
a $0.01 minimum. Because the fee is built on top rather than taken out:

```
buyer pays 271.60  →  seller receives 236.19     (13.04% lost)
list at 100.00     →  you receive 86.96
```

`× 0.85` understates your take by ~2 points and kills marginal recipes.
Use `--fee 0.02` for CSFloat, `0.08` for Skinport.

### Souvenir in, normal out

Souvenir items are legal inputs since 22 May 2026. Attributes are stripped and
the **output is always normal quality**. Souvenir copies are often the cheapest
way to fill a slot. `souvenir_capable` is derived from Souvenir Package crate
membership, not the upstream boolean (which is set on nearly every finish).

---

## Commands

| command | purpose |
|---|---|
| `sync-db` | Skin database, float caps, collections |
| `sync-images` | Image URLs from the same payload |
| `warm` | Fetch prices — concurrent, adaptive, resumable |
| `guaranteed` | Contracts that cannot lose. **Start here** |
| `scan` | Ranked search, filtered on chance of profit |
| `serve` | Web UI |

### Web UI

Two modes as a top-level toggle:

- **Guaranteed** — only floor-positive contracts. No losing roll exists.
- **Ranked** — the general search, with a *minimum chance of profit* slider.

Every card expands to show an **outcome spread bar**: each outcome as a dot,
your cost as an orange line. Dots left of the line are losing outcomes. For a
guaranteed contract there are none.

Float caps, market depth, images, and per-outcome profit are shown inline.

---

## Layout

```
cs2-tradeup-lab/
├── README.md  AGENTS.md  pyproject.toml  requirements.txt  LICENSE
├── data/
│   ├── sample_skins.json      TRACKED   offline fixtures
│   ├── sample_prices.json     TRACKED   fabricated, tests only
│   ├── skins.json             generated by sync-db
│   ├── images.json            generated by sync-images
│   ├── prices.sqlite          the price cache
│   └── price_snapshot.json    exported cache, used by scans
├── tradeup/
│   ├── models.py              Skin, InputItem, Outcome, wear brackets
│   ├── engine.py              float mapping, odds, fees, EV   [PURE]
│   ├── risk.py                risk profile + guaranteed search [NEW]
│   ├── pricing.py             quote → usable price policy
│   ├── optimizer.py           risk-aware heuristic search
│   ├── images.py              image index with placeholder fallback
│   ├── cli.py                 all commands
│   ├── web.py                 FastAPI, two modes
│   ├── static/index.html      single-file UI, no build step
│   ├── data/loader.py         skin DB + CSGO-API sync
│   └── prices/
│       ├── steam.py           cache, quotes, price parsing
│       ├── bulk.py            paginated bulk sweep
│       └── fetcher.py         async AIMD fetcher            [NEW]
└── tests/                     55 tests, all offline
```

`engine.py` imports only `models.py` and the stdlib. Prices arrive through a
callback, which is why the whole suite runs with no network.

---

## Install

```powershell
git clone https://github.com/YOUR-USERNAME/cs2-tradeup-lab.git
cd cs2-tradeup-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                          # 55 tests, offline, on bundled fixtures
```

Python 3.10+. A fresh clone runs and passes its tests with **zero network
access**. The bundled prices are fabricated — any ROI you see before `warm`
is meaningless.

**Windows:** skin names contain characters cp1252 cannot encode, so every
read and write pins `encoding="utf-8"` and the console is reconfigured at
startup. A static test fails if any text I/O omits an encoding.

---

## Rate limits: read before fetching

Steam's `429` is **per IP**, lasts hours, and **re-arms every time you hit it
while banned**. Retrying makes it worse.

`warm` handles this automatically — it starts conservative, speeds up while
clean, halves on a limit, and stops entirely if the limit is hit repeatedly.
If the circuit breaker trips, stop and come back later; a router reboot that
changes your IP also clears it.

Ctrl+C is always safe: every quote is committed as it arrives, and a resumed
run fetches only what is still missing.

---

## Reality check

Steam wallet funds cannot be withdrawn. A profitable Steam-only trade-up
compounds into more Steam credit, not money. Real profit needs a cash
marketplace, which changes the fee model (2–8% instead of 13%) and the
liquidity assumptions — both configurable.

Trade-ups consume the inputs permanently. A **guaranteed** contract is
guaranteed only if its prices are real: confirm the worst outcome's price
and listing depth before committing, especially for anything in Well-Worn.

## License

MIT. Not affiliated with Valve. Skin data from the unofficial
[CSGO-API](https://github.com/ByMykel/CSGO-API).
