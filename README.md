# CS2 Trade-Up Lab

A free, self-hosted trade-up analysis engine for Counter-Strike 2. It pulls
live Steam Community Market prices, computes the exact output float, the
outcome probability distribution, and the fee-adjusted expected value of a
contract — then searches for the best recipes you can afford on a budget.

No API key, no subscription. The only rate limit is Steam's, and the price
layer is built around not tripping it.

---

## Quick start

```powershell
pip install -r requirements.txt

# 1. Skin database (one-off, fast)
python -m tradeup.cli sync-db

# 2. Prices -- BULK. ~60 requests, a few minutes, caches the whole market.
python -m tradeup.cli bulk-warm --currency BRL

# 3. Scan
python -m tradeup.cli scan --budget 20 --rarity consumer --currency BRL

# Web UI (point it at the real snapshot first)
$env:TRADEUP_PRICE_SNAPSHOT="data/price_snapshot.json"
uvicorn tradeup.api:app --reload          # http://localhost:8000
```

A synthetic sample dataset ships with the project so everything runs and all
tests pass offline. **The sample prices are fabricated** — the ROI figures you
see before running `bulk-warm` are meaningless.

```powershell
python -m pytest tests -q      # 47 tests
```

---

## Rate limiting: read this before fetching prices

Steam's `429 Too Many Requests` on the market is an **IP-level** limit, not
per-account. Two things make it worse than a normal rate limit:

* A ban commonly lasts **hours**.
* It **re-arms every time you hit it while banned**. Retrying keeps you locked out.

`priceoverview` returns **one item per request**, so a Mil-Spec sweep is ~5,600
requests. That is unwinnable at any pace. `bulk-warm` uses the market's own
paginated search endpoint — 100 items per request, ~56 requests for the same
data. That is the same endpoint the market web page calls, so it *is* the
efficient form of scraping; driving a browser at it would be strictly worse.

| | `priceoverview` | `bulk-warm` |
|---|---|---|
| Items per request | 1 | 100 |
| Lowest price | yes | yes |
| Median price | yes | **no** |
| 24h sales volume | yes | **no** — listing count instead |

`sell_listings` is how many are **listed right now** — supply, not sales. So the
intended workflow is two-stage:

1. `bulk-warm` prices the whole universe cheaply → `scan` shortlists recipes.
2. `warm-prices` on just those names gets true median + volume before you
   commit money — dozens of precise requests instead of thousands.

If you get banned:

```powershell
python -m tradeup.cli cooldown --currency BRL --wait
python diagnose_steam.py --currency BRL
```

A router reboot that changes your IP also clears it.

---

## The math it implements

### 1. Output float (normalized mapping)

The output wear is **deterministic**, not random. Each input float is
normalized inside *its own skin's* range, averaged, then projected onto the
*output skin's* range:

```
norm_i     = (float_i - min_i) / (max_i - min_i)
avg_norm   = sum(norm_i) / N
out_float  = out_min + avg_norm * (out_max - out_min)
```

The most misunderstood mechanic in trade-ups. A 0.04 float on a 0.00–0.08 skin
is **0.50** normalized; the same 0.04 on a 0.00–1.00 skin is **0.04**. Raw float
averaging is wrong whenever inputs have different ranges.

Consequences: **wide-range skins are better inputs** at the same absolute float,
and the *output's* own min/max — not your inputs' — decides the final wear
bracket. `required_avg_norm_for_wear()` inverts the formula so you can ask
"what average do I need to land this in Factory New?".

### 2. Outcome probabilities — and the honest caveat

Valve does not publish the outcome-selection rule and community calculators
disagree. Both models are implemented, and `--robust` (default on) keeps only
recipes profitable under **both**:

| Model | Rule | 5A / 5B mix (A has 2 outputs, B has 4) |
|---|---|---|
| `outcome_weighted` | `P(o in c) = n_c / sum(n_k * O_k)` | A gets 33% of the mass |
| `per_input` | pick an input (1/N), then one of its collection's outputs | A gets 50% |

Identical for single-collection contracts; they diverge sharply on
mixed-collection filler builds — exactly where "profitable" recipes appear. Any
tool that hides this is selling confidence it does not have.

### 3. Fees — the 15% is added on top, not taken out

Steam charges 5% (platform) + 10% (CS2 game fee), both computed on the
**seller's proceeds**, each with a $0.01 minimum:

```
buyer_pays  = seller_gets * 1.15
seller_gets = buyer_pays / 1.15   (about 86.96% of the listing)
```

The common `* 0.85` shortcut understates your take by ~2 percentage points and
silently kills marginal recipes. Pass `--fee 0.02` for CSFloat, `0.08` for
Skinport, etc.

### 4. Souvenir inputs (rule change, 22 May 2026)

Souvenir items are now valid trade-up inputs. All souvenir attributes are
stripped and the **output is always normal quality** — feeding ten Souvenirs in
does not produce a Souvenir out. The tool prices inputs with the `Souvenir `
prefix and *always* prices outputs without it; there is a test asserting the
output is never quoted as a Souvenir.

This matters most at Consumer and Industrial grade, where most supply comes
from souvenir-bearing tournament and map collections. Because the contract
destroys the provenance, there is no reason to pay a premium for it — souvenir
copies are often the cheapest way to fill a slot.

Guides that say souvenirs cannot enter trade-ups predate this patch. Use
`--no-souvenir` to model the old rules.

**Which skins actually have a Souvenir variant?** Souvenir and StatTrak are
mutually exclusive and come from different sources (cases vs tournament
packages), so `souvenir_capable` is derived from **crate membership**, not from
the upstream `souvenir` boolean — that flag is set on nearly every finish and
would claim ~1400 Souvenir listings exist. `sync-db` warns if the derived rate
looks implausible. To check against the live market:

```powershell
python -m tradeup.cli verify-souvenir --rarity consumer --currency BRL
```

**Pricing caveat:** a Souvenir's value is dominated by its gold stickers (event,
teams, MVP autograph), which the market hash name does not capture. Two
identical-looking Souvenirs can differ by orders of magnitude. That is tolerable
here only because souvenirs are bought as fodder at the `lowest` listing — never
read the `median` of a Souvenir name as a real valuation.

### 5. Expected value, and what it does not tell you

```
EV_net = sum(P(o) * net_price(o))
profit = EV_net - sum(input_costs)
ROI    = EV_net / cost
```

Positive EV is necessary, not sufficient. Also reported:

- **P(win)** — how often a *single* contract beats your cost. Many +EV trade-ups
  win 8% of the time: a good lottery ticket, not income.
- **sigma** — outcome standard deviation.
- **n\*** — how many identical contracts before the edge outweighs the noise at
  ~2 sigma. If `n* = 340` at R$200 each, that is a bankroll problem, not a
  strategy.
- **Unpriced mass** — probability on outcomes with no price data. Recipes above
  10% are dropped, since an unpriced outcome scores as zero.

### 6. Price realism

The default policy is deliberately pessimistic, because the classic way these
calculators lie is using `lowest_price` on both sides:

- **Inputs** at `lowest` — what you actually pay.
- **Outputs** at `median` — the lowest listing is often one outlier you cannot
  reproduce ten times.
- **Haircut** (3%) on outputs.
- **Output volume floor** (`--min-volume`, default 3).
- **Input volume floor** (`--min-input-volume`, default 10) — you need *ten*
  copies. Souvenir listings are often near-unique, so a R$0.40 quote backed by
  two daily sales is not a real price.
- **Float percentile** (0.15) — "with some hunting I can source an item in the
  bottom 15% of this wear bracket". Use `0.0` only if you will pay float-sniper
  premiums; `0.5` for a lazy market-order assumption.

---

## Layout

```
cs2-tradeup-lab/
  README.md
  requirements.txt
  pytest.ini
  .gitignore
  diagnose_steam.py              standalone connectivity/cache diagnostic
  data/
    sample_skins.json            bundled offline fixtures
    sample_prices.json           (fabricated -- not real market data)
  tradeup/
    __init__.py
    models.py                    Skin, InputItem, wear brackets, rarity ladder
    engine.py                    float mapping, odds, fees, EV, validation
    pricing.py                   quote -> usable price policy
    optimizer.py                 budget-constrained recipe search
    cli.py                       typer CLI (all commands)
    api.py                       FastAPI + single-page UI
    data/
      __init__.py
      loader.py                  skin DB indexing + CSGO-API sync
    prices/
      __init__.py
      steam.py                   per-item client: cache, backoff, heartbeat
      bulk.py                    100-items-per-request sweep
  tests/
    test_engine.py               fees, normalization, both odds models, EV
    test_souvenir.py             souvenir rules, quality derivation, liquidity
    test_sync_and_encoding.py    sync failures, UTF-8 on Windows, static guard
    test_bulk.py                 pagination, filtering, 429 recovery
```

The engine is pure — prices are injected via a callback, so swapping Steam for
another marketplace means implementing one `get(name) -> Quote` method.

---

## Commands

| Command | What it does |
|---|---|
| `sync-db` | Download/normalize the skin database. `--from-file` for manual download. |
| `bulk-warm` | Price the market 100 items per request. **Start here.** |
| `cooldown` | Is this IP rate limited? `--wait` polls until clear. |
| `warm-prices` | Precise per-item median + volume. Slow; use on a shortlist. |
| `verify-souvenir` | Do `Souvenir X` listings really exist? |
| `scan` | Find the best recipes under a budget. |
| `eval` | Score a hand-built contract from JSON. |

---

## Roadmap

1. **5-Covert gold contracts** — the engine handles N=5; needs the knife/glove
   pool modelled as a pseudo-collection. This is why `sync-db` reports 0
   tradeable Covert collections.
2. **Float-specific pricing** — today a 0.001 FN and a 0.069 FN share one price.
3. **Alternative price sources** — the `PriceSource` protocol makes this a small
   class; a cash marketplace also changes the fee model favourably.
4. **Historical backtest** to check whether recipes that scored well last month
   actually paid.

---

## Reality check

Steam wallet funds cannot be withdrawn. A "profitable" Steam-only trade-up
compounds into more Steam credit, not money. If the goal is actual profit, the
sell side has to be a cash marketplace, which changes both the fee model (2–8%
instead of 15%) and the liquidity assumptions — both configurable here.

Trade-ups consume the inputs permanently. Treat **n\*** as the honest headline
number, not ROI.
