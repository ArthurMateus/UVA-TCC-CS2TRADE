# CS2 Trade-Up Lab

## Uso rápido (Windows)

| Passo | O que fazer |
|---|---|
| 1. Instalar (uma vez) | Dois cliques em **`instalar.bat`**: cria o ambiente, instala tudo, baixa o catálogo de skins e cria o `.env` |
| 2. Configurar o banco (opcional) | No `.env`, troque `[SUA-SENHA]` em `DATABASE_URL` pela senha do banco Supabase |
| 3. Coletar preços (deixe rodando) | Dois cliques em **`coletar.bat`**. Varre a Steam em rodadas, salva tudo localmente e envia ao Supabase só o que mudou. Ctrl+C para parar; ao reabrir ele continua de onde parou |
| 4. Ver os resultados | Dois cliques em **`abrir_site.bat`** (abre http://127.0.0.1:8000) |

Pelo terminal, os mesmos passos são `tradeup setup`, `tradeup collect` e
`tradeup serve`. A moeda padrão é BRL (troque com `TRADEUP_CURRENCY=USD` no
`.env`). Uma rodada completa de todos os tiers leva cerca de 2 horas (6 s
entre páginas, para não ser bloqueado pela Steam), e a próxima começa 60 min
depois: `tradeup collect --interval 120 --rarities milspec,restricted` ajusta
isso. Log em `data/collect.log`. No Windows, desative a suspensão automática
(Configurações → Sistema → Energia) enquanto estiver coletando.

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
# 1 │ The Militia Collection │ 2.70 │ 13.99 │ 27.75 │     +11.29 │      518% │
```

*Floor ROI* is the return on the **worst** roll, not the average. Every value
is **net of the marketplace fee** (Steam's 5%+10% by default, `--fee` for
others). Up to v2.0 this mode showed buyer prices as "net", inflating the
guaranteed profit; the sample above was 16.09 / +13.39 before the fix.

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
| cooldown over | ONE probe request (half-open); limited again = cooldown doubles |
| 4 breaker trips | the run halts and reports what is left |

Plus a **worker pool** (6 workers by default) pulling names from a queue, so
one item in backoff never blocks the others, and a token bucket with a
**burst of 2**, so the client never fires a volley after an idle period or a
pause. A **negative cache** (24 h) remembers names with no listing; a single
HTTP 500 is retried before being treated as "no listing", since Steam also
returns 500 for transient failures. Every name ends as *ok*, *empty* or
*gave up* -- nothing disappears silently.

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
| `setup` | First run: catalogue + images in one download |
| `collect` | **Unattended loop**: sweep all tiers in rounds, refresh the snapshot, push changed quotes to Supabase, sleep, repeat |
| `bulk-warm` | Prices for a whole tier via **paginated search, filtered by rarity**. Resumable checkpoints. **Start here** |
| `warm` | Prices + 24 h sales volume (priceoverview) — concurrent, adaptive, resumable |
| `guaranteed` | Contracts that cannot lose (net of fees) |
| `scan` | Ranked search, filtered on chance of profit |
| `refine-floats` | Re-price the best contracts at the **input float they really need** (CSFloat) |
| `db-push` | Mirror catalogue + quotes into Supabase (Postgres) |
| `serve` | Web UI |

### Pagination: fewer requests, no gaps

Steam's `search/render` caps pages at **10 rows** no matter what `count`
asks for, so a full-catalogue sweep is ~3.5k requests. `bulk-warm` filters
server-side with Steam's own tags (`category_730_Rarity[]=tag_Rarity_Rare_Weapon`
for Mil-Spec, `category_730_Quality[]=tag_normal|tag_tournament`): Mil-Spec
normal quality is ~2.1k rows (~207 requests) instead of ~35k. `start`
advances by rows actually received, every page is committed and
checkpointed (`data/sweep_checkpoints.json`), a failed or repeated page marks
the sweep *incomplete* instead of ending silently, and re-running resumes.

The web API paginates too: `limit` (max 100) / `offset`, with `total` and
`next_offset` in every response. A scan is computed once per parameter set
(bounded LRU, single-flight), so pages and filters never recompute it.

### Float-aware input prices

Steam prices are per **wear**, but the search assumes each input has a low
float inside its wear (`--float-percentile`, 15%) while paying the wear's
cheapest listing -- usually a *high* float. That understates the cost exactly
on contracts whose profit depends on landing a better output wear.

`refine-floats` (or the *Float check* box in the UI) closes the gap for the
shortlist: it computes the loosest average float that keeps every outcome in
its wear, spreads that slack over the inputs, asks CSFloat for the cheapest
listings under each float cap, and re-evaluates the contract with those
**real floats and prices**. Default basis `premium` = Steam price × CSFloat
float premium (same market and currency as the scan); `--basis csfloat`
uses CSFloat's own prices. Contracts whose inputs are not listed under the
cap are reported as *infeasible*. Needs `CSFLOAT_API_KEY` in `.env`.

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
│   ├── config.py              paths anchored at the repo, .env loading
│   ├── float_pricing.py       float-aware second pass          [NEW]
│   ├── liquidity.py           liquidity tiers (not yet wired into scans)
│   ├── web.py                 FastAPI, two modes, paginated
│   ├── static/index.html      single-file UI, no build step
│   ├── data/loader.py         skin DB + CSGO-API sync
│   └── prices/
│       ├── steam.py           cache, quotes, price parsing
│       ├── bulk.py            paginated sweep, rarity tags, checkpoints
│       ├── fetcher.py         async AIMD fetcher, worker pool, half-open breaker
│       ├── csfloat.py         float-aware listings (CSFloat API)   [NEW]
│       └── pg_store.py        Supabase/Postgres mirror             [NEW]
├── supabase/schema.sql        tables, RLS, triggers (idempotent)
└── tests/                     ~200 tests, all offline
```

`engine.py` imports only `models.py` and the stdlib. Prices arrive through a
callback, which is why the whole suite runs with no network.

---

## Install

```powershell
git clone https://github.com/ArthurMateus/UVA-TCC-CS2TRADE.git
cd UVA-TCC-CS2TRADE
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                          # ~200 tests, offline, on bundled fixtures
copy .env.example .env          # then fill in the keys you have
```

Optional extras: `pip install -e ".[supabase]"` for `db-push` and
`TRADEUP_PRICE_SOURCE=supabase`.

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

`warm` and `bulk-warm` handle this automatically — it starts conservative, speeds up while
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
