"""Show the trade-up math working, with real numbers instead of green dots.

    python verify_math.py

Runs every worked example against YOUR installed tradeup package and prints
expected vs computed side by side. Use this to confirm a patch actually
landed -- `pytest` tells you pass/fail, this shows you the arithmetic.

Exit code 0 if everything matches, 1 otherwise.
"""
from __future__ import annotations

import sys

try:
    from tradeup.engine import (
        DEFAULT_ODDS_MODEL,
        collection_odds,
        outcome_float,
        outcome_probabilities,
        steam_net_from_buyer_price,
        trade_up_float,
    )
    from tradeup.models import InputItem, Rarity, Skin, wear_from_float
except ImportError as e:
    print(f"Cannot import the tradeup package: {e}")
    print("Run this from the project root (the folder containing tradeup/).")
    sys.exit(1)

OK, BAD = 0, 0


def check(label: str, got, want, tol: float = 1e-6, unit: str = "") -> None:
    global OK, BAD
    if isinstance(want, float):
        good = abs(got - want) <= tol
        g, w = f"{got:.9f}", f"{want:.9f}"
    else:
        good = got == want
        g, w = str(got), str(want)
    mark = "PASS" if good else "FAIL"
    if good:
        OK += 1
    else:
        BAD += 1
    print(f"   [{mark}] {label:<44} {g:>14}{unit}   expected {w}{unit}")


def mk(name, coll, rarity, mn, mx):
    return Skin(f"{name}|{coll}", name, rarity, coll, mn, mx)


def pool_of(m):
    return lambda c, r: m.get((c, int(r)), [])


print("=" * 96)
print("TRADE-UP MATH VERIFICATION")
print("=" * 96)

# ---------------------------------------------------------------- floats
print("\n1. TRADE-UP FLOAT   (raw float rescaled onto 0-1 by the skin's own cap)")
print("   formula:  (float - min) / (max - min)\n")

print("   MAC-10 | Sakkaku    float 0.22798392176628113   cap 0.21 - 0.79")
v1 = trade_up_float(0.22798392176628113, 0.21, 0.79)
check("(0.22798392 - 0.21) / (0.79 - 0.21)", v1, 0.031006758, tol=1e-6)

print("\n   P90 | Trigon        float 0.749                 cap 0.08 - 0.75")
v2 = trade_up_float(0.749, 0.08, 0.75)
check("(0.749 - 0.08) / (0.75 - 0.08)", v2, 0.998507463, tol=1e-8)

print("\n   Same raw float, different caps -> different contribution:")
wide = InputItem(mk("Wide", "C", Rarity.MILSPEC, 0.00, 1.00), 0.10)
half = InputItem(mk("Half", "C", Rarity.MILSPEC, 0.00, 0.50), 0.10)
check("float 0.10 in cap 0.00-1.00", wide.trade_up_float, 0.10)
check("float 0.10 in cap 0.00-0.50", half.trade_up_float, 0.20)

# ------------------------------------------------------- average + output
print("\n2. AVERAGE TRADE-UP FLOAT   (10x P90 | Trigon @ 0.749)")
p90 = mk("P90 | Trigon", "Phoenix", Rarity.MILSPEC, 0.08, 0.75)
inputs = [InputItem(p90, 0.749) for _ in range(10)]
avg = sum(i.trade_up_float for i in inputs) / len(inputs)
check("average of ten adjusted floats", avg, 0.998507463, tol=1e-8)
print(f"          the OUTDATED model would average the raw floats -> 0.749")
print(f"          this model gives {avg:.9f} -- a completely different result")

print("\n3. OUTCOME FLOAT   (average rescaled into each OUTPUT skin's cap)")
print("   formula:  avg * (out_max - out_min) + out_min\n")
awp = mk("AWP | Asiimov", "Phoenix", Rarity.RESTRICTED, 0.18, 1.00)
aug = mk("AUG | Chameleon", "Phoenix", Rarity.RESTRICTED, 0.00, 0.50)
fa, fg = outcome_float(avg, awp), outcome_float(avg, aug)
check("AWP | Asiimov    cap 0.18-1.00", fa, 0.998776119, tol=1e-8)
check("AUG | Chameleon  cap 0.00-0.50", fg, 0.499253731, tol=1e-8)
check("AWP wear", wear_from_float(fa), "Battle-Scarred")
check("AUG wear", wear_from_float(fg), "Battle-Scarred")
print("          one contract, two outcomes, two different floats")

print("\n4. FLOAT CAPS -> AVAILABLE CONDITIONS")
for name, mn, mx in [("AK-47 | Redline", 0.10, 0.70),
                     ("MAC-10 | Sakkaku", 0.21, 0.79),
                     ("M4A1-S | Printstream", 0.00, 1.00),
                     ("Desert Eagle | Blaze", 0.00, 0.08)]:
    w = mk(name, "C", Rarity.MILSPEC, mn, mx).reachable_wears()
    short = {"Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT",
             "Well-Worn": "WW", "Battle-Scarred": "BS"}
    print(f"   {name:<24} {mn:.2f}-{mx:.2f}   "
          f"{' '.join(short[x] for x in w)}")

# ----------------------------------------------------------------- odds
print("\n5. OUTCOME ODDS   (per item -- each input carries 1/n)")
print(f"   default model in your build: {DEFAULT_ODDS_MODEL!r}", end="")
print("   <- correct" if DEFAULT_ODDS_MODEL == "per_input"
      else "   <- WRONG, should be 'per_input'")

print("\n   Case A: 7 of 10 inputs from a collection with 3 outcomes")
c1 = mk("In1", "C1", Rarity.MILSPEC, 0, 1)
c2 = mk("In2", "C2", Rarity.MILSPEC, 0, 1)
outs = {("C1", int(Rarity.RESTRICTED)):
            [mk(f"A{i}", "C1", Rarity.RESTRICTED, 0, 1) for i in range(3)],
        ("C2", int(Rarity.RESTRICTED)):
            [mk(f"B{i}", "C2", Rarity.RESTRICTED, 0, 1) for i in range(3)]}
items = [InputItem(c1, 0.1)] * 7 + [InputItem(c2, 0.1)] * 3
co = collection_odds(items, pool_of(outs))
p = outcome_probabilities(items, pool_of(outs))
check("P(collection C1)", co["C1"] * 100, 70.0, tol=1e-6, unit="%")
check("P(each of its 3 skins)", p["A0|C1"] * 100, 23.333333, tol=1e-4, unit="%")
check("probabilities sum to 1", sum(p.values()), 1.0)

print("\n   Case B: 1 M4A1-S | Knight (Cobblestone, 1 outcome) + 9 fillers")
knight = mk("M4A1-S | Knight", "Cobblestone", Rarity.CLASSIFIED, 0, 1)
o2 = {("Cobblestone", int(Rarity.COVERT)):
          [mk("AWP | Dragon Lore", "Cobblestone", Rarity.COVERT, 0, 1)]}
it2 = [InputItem(knight, 0.1)]
for i in range(9):
    o2[(f"C{i}", int(Rarity.COVERT))] = [
        mk(f"X{i}", f"C{i}", Rarity.COVERT, 0, 1),
        mk(f"Y{i}", f"C{i}", Rarity.COVERT, 0, 1)]
    it2.append(InputItem(mk(f"F{i}", f"C{i}", Rarity.CLASSIFIED, 0, 1), 0.1))

cur = outcome_probabilities(it2, pool_of(o2), "per_input")
check("AWP | Dragon Lore", cur["AWP | Dragon Lore|Cobblestone"] * 100,
      10.0, tol=1e-6, unit="%")
check("each 2-outcome collection skin", cur["X0|C0"] * 100, 5.0,
      tol=1e-6, unit="%")
try:
    old = outcome_probabilities(it2, pool_of(o2), "outcome_weighted")
    print(f"          legacy model would say "
          f"{old['AWP | Dragon Lore|Cobblestone']*100:.2f}% for everything "
          "-- that is the pre-Retakes rule")
except Exception:
    pass

# ----------------------------------------------------------------- fees
print("\n6. STEAM FEE   (15% is added ON TOP of the seller's cut)")
check("buyer pays 11.50 -> seller gets", steam_net_from_buyer_price(11.50),
      10.0, tol=0.01)
naive = 10.0 * 0.85
real = steam_net_from_buyer_price(10.0)
print(f"          list at 10.00: you keep {real:.2f}, "
      f"not {naive:.2f} (the x0.85 shortcut is wrong by {real-naive:.2f})")

print("\n" + "=" * 96)
print(f"{OK} passed, {BAD} failed")
if BAD:
    print("A failure means a patch did not land. Re-copy the files in question.")
print("=" * 96)
sys.exit(1 if BAD else 0)
