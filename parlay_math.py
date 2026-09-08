"""
Does parlaying a pile of heavy favourites turn bad bets into a good one?

The intuition is that each -1000 leg is unbettable on its own, but stacked ten
deep the payout becomes attractive. This checks that arithmetic directly, using
the market's own implied probabilities, so the answer does not depend on our
model being any good.
"""

from odds import american_to_decimal, american_to_implied_prob, combine_odds

VIG_NOTE = """
A price contains the book's margin. -1000 implies 90.91%, but a fair line on the
same game is a little longer than that, because the two sides together are
priced to more than 100%. The gap is small per leg and it is the whole story.
"""


def parlay_row(odds: int, n: int, true_edge: float = 0.0):
    """
    n legs at the same price. `true_edge` shifts the real win rate away from the
    implied one: 0.0 trusts the line exactly, -0.01 means each leg is one point
    worse than it looks (roughly the vig on a heavy favourite).
    """
    implied = american_to_implied_prob(odds)
    true_p = max(min(implied + true_edge, 0.999999), 0.000001)
    combined = combine_odds([odds] * n)
    dec = american_to_decimal(combined)
    hit = true_p ** n
    ev = hit * (dec - 1.0) - (1.0 - hit)
    breakeven = 1.0 / dec
    return implied, true_p, combined, dec, hit, breakeven, ev


print("=" * 78)
print("Parlaying heavy favourites")
print(VIG_NOTE.strip())
print("=" * 78)

for odds in (-1000, -500, -300):
    print(f"\nEach leg at {odds:+d}  (implies {american_to_implied_prob(odds)*100:.2f}%)")
    print(f"  {'legs':>4} {'pays':>8} {'need':>8} {'hits (fair)':>12} {'EV fair':>9} "
          f"{'hits (-1pp)':>12} {'EV -1pp':>9}")
    for n in (1, 3, 5, 10, 15):
        _, _, comb, dec, hit_f, be, ev_f = parlay_row(odds, n, 0.0)
        _, _, _, _, hit_v, _, ev_v = parlay_row(odds, n, -0.01)
        print(f"  {n:>4} {comb:>+8d} {be*100:>7.1f}% {hit_f*100:>11.1f}% "
              f"{ev_f*100:>+8.1f}% {hit_v*100:>11.1f}% {ev_v*100:>+8.1f}%")

print("\n" + "=" * 78)
print("Why: the margin compounds")
print("=" * 78)
print("""
A parlay multiplies decimal odds, so it multiplies each leg's expected return
too. Write one leg's return as R. Then an n-leg parlay returns R^n.

  R > 1  (a real edge)      ->  R^n grows       parlays amplify an edge
  R = 1  (a fair price)     ->  R^n = 1         parlays change nothing
  R < 1  (the usual case)   ->  R^n shrinks     parlays amplify the loss

There is no value of n that turns R < 1 into R^n > 1. Stacking legs cannot
manufacture edge; it can only compound whatever each leg already had.
""")

for hold in (0.02, 0.03, 0.05):
    r = 1.0 - hold
    print(f"  {hold*100:.0f}% margin per leg -> keep {r:.2f} per leg:  "
          + "  ".join(f"{n}legs {r**n*100:.0f}%" for n in (1, 5, 10, 15)))

print("""
The 10-15 leg version also loses two bets in three even when every leg is a 91%
favourite: 0.909^10 is 39%. The wins are memorable and the losses are quiet,
which is what makes the strategy feel better than it is.
""")
