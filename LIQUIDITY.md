# Liquidity: the gap between a listed price and a sale

## Your #1 result, decomposed

```
M4A4 | Modern Hunter (WW)      50%   net 236.19   ->  99.6% of the EV
M4A4 | Sheet Lightning (FT)    25%   net   1.00   ->   0.2%
Glock-18 | Glockingbird (FT)   25%   net   0.81   ->   0.2%
```

Strip the jackpot and EV collapses from 118.55 to **0.45** against a cost of
101.80. That is not a trade-up with variance -- it is a single bet that one
Well-Worn price is real.

Break-even needs **85.8%** of that listed price. A Well-Worn skin nobody
wants does not clear 85.8% of an optimistic listing.

Sensitivity:

| you realize | EV | profit | ROI |
|---|---|---|---|
| 100% of listed | 118.55 | +16.75 | 116.5% |
| 80% | 94.93 | −6.87 | 93.2% |
| 60% | 71.31 | −30.49 | 70.0% |

The reference tool in your screenshot shows the same thing from the other
side: at its API price the contract is −47.65, and only becomes +37.45 when
the output price is overridden upward. Same contract, same float math --
the entire disagreement is what that one skin actually sells for.

## Two separate problems

**Realization.** To sell into a thin book you must undercut, and with no
sales history there is no evidence anyone pays the listed number.

**Concentration.** `n*` already tells you how many contracts before the edge
beats the noise -- but `n*` assumes the prices are CORRECT and only the roll
is random. It cannot see that 99.6% of your EV rests on one questionable
number. That needs its own guard.

## Run it now

```powershell
python -m tradeup.cli scan --budget 200 --rarity milspec --currency BRL `
  --odds per_input `
  --min-roi 1.0 --top 25 --json-out results.json

python check_liquidity.py results.json --currency BRL
```

Standalone and read-only -- no patching needed. Output:

```
  # target collection          cost   EV raw   EV adj   profit     ROI  top share  verdict
  1 The Militia Collection   101.80   118.55    92.56    -9.24   90.9%  99.5% thin  REJECT
      ! only 7 listed of PP-Bizon | Modern Hunter -- need 5
  2 The Spy Tech Collection     1.80     2.04     2.00    +0.20  111.2% 68.1% deep  ok
```

Note the second warning: your Militia recipe needs 5x PP-Bizon | Modern
Hunter and only 7 are listed. Even if the output price were real, buying the
inputs would move the market against you.

## Tiers

Using sell listings (what bulk-warm gives you):

| listings | tier | you realize |
|---|---|---|
| < 3 | dead | 55% |
| < 15 | thin | 78% |
| < 60 | ok | 93% |
| 60+ | deep | 98% |

With 24h sales volume (from `warm`) the thresholds are 1 / 5 / 25 and
volume always wins -- an item selling 30/day is liquid even if only 6 are
listed right now.

**These factors are blunt.** They are not calibrated against measured fill
data; they encode "fewer listings and no sales means you get less", which is
the part that is certain. Use them to RANK, not to forecast.

## Permanent integration

`tradeup/liquidity.py` is the reusable module -- `LiquidityPolicy`,
`assess()`, `breakeven_realization()`. To wire it into `scan` itself, in
`optimizer.py` after building each candidate:

```python
from .liquidity import LiquidityPolicy, assess

rep = assess(res.outcomes, LiquidityPolicy(), depth_map, res.cost)
if rep.profit_adjusted <= 0 or rep.concentrated:
    continue
```

where `depth_map` maps market_hash_name -> (listings, volume) from the cache.

## The real fix

Liquidity haircuts are a proxy. The actual answer is 24h sales volume:

```powershell
python -m tradeup.cli warm --rarity milspec --currency BRL --rate 10
```

That is slow, but you only need it for the ~20 names in your shortlist, not
all 5,596. Run the scan, take the survivors, confirm those.
