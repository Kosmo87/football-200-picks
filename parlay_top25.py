"""
Can the top 25 college teams be parlayed into a sizeable return?

A fair question with an arithmetic answer, so this computes it from the current
board rather than arguing about it. parlay_math.py already showed the general
shape -- a parlay returns R^n where R is one leg's expected return, so it
amplifies an edge and amplifies a loss and cannot turn the second into the
first. What that leaves open is the specific plan: top-25 teams, this week,
mixing moneylines with totals and spreads.

Three things this measures, in the order they matter:

  1. What the ML parlay actually pays, priced off the market's own de-vigged
     probabilities so the answer does not depend on our model being any good.

  2. What the margin costs once compounded. Each leg is priced to more than
     100% with its opposite side; stacking legs multiplies that, and the
     "sizeable return" and the cost grow together.

  3. What a parlay gives up that singles do not. The one measured positive
     signal in this whole project is line shopping -- the same team priced
     longer at one book than at the market. A parlay has to sit at ONE book,
     so every leg takes that book's price instead of the best available. That
     forfeit is computed against the real gaps the scanner found.

  python parlay_top25.py [--legs 3] [--top 25]
"""

from __future__ import annotations

import argparse
import json
import os
from itertools import combinations
from typing import Dict, List, Optional

from odds import american_to_decimal, american_to_implied_prob, combine_odds

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(ROOT, "public", "data", "board.json")


def load_league(league: str = "NCAAF") -> dict:
    with open(BOARD) as f:
        return json.load(f)["leagues"][league]


def top25_legs(lg: dict, top: int = 25) -> List[dict]:
    """
    Every priced side belonging to a top-N rated team with a game this week.

    Ranked by our Elo, which is the only "top 25" this project has. The AP poll
    would give a different list and the argument below does not depend on which.
    """
    ranks = {r["team_id"]: i + 1 for i, r in enumerate(lg["ratings"][:top])}
    out = []
    for game in lg["games"]:
        for leg in game["legs"]:
            if leg["team_id"] not in ranks:
                continue
            out.append({
                **leg,
                "rank": ranks[leg["team_id"]],
                "matchup": game.get("short_name") or game["name"],
                "kickoff": game.get("kickoff"),
                "event_id": game["event_id"],
                "total": game.get("total"),
                "spread": game.get("spread"),
            })
    out.sort(key=lambda l: l["rank"])
    return out


def fair_prob(leg: dict) -> float:
    """
    The market's own probability with the margin taken out.
    
    Using this rather than our model is deliberate: it makes the conclusion
    independent of whether the Elo ratings are any good. If the parlay is bad
    at the market's own numbers, it is bad.
    """
    return leg["implied_prob"]


def describe_parlay(legs: List[dict]) -> dict:
    combined = combine_odds([l["odds"] for l in legs])
    dec = american_to_decimal(combined)
    # Independent legs: no shared game, so the product is the honest joint
    # probability. Same-game legs are handled separately below, because there
    # the product is NOT the joint probability.
    fair = 1.0
    priced = 1.0
    for l in legs:
        fair *= fair_prob(l)
        priced *= american_to_implied_prob(l["odds"])
    return {
        "legs": legs,
        "combined": combined,
        "payout": dec - 1,
        "fair_prob": fair,
        "priced_prob": priced,
        # EV per unit at the market's own de-vigged numbers.
        "ev": fair * (dec - 1) - (1 - fair),
        "breakeven": 1 / dec,
        # The margin, compounded across the legs.
        "hold": 1 - fair / priced if priced else 0.0,
    }


def report(league: str, n_legs: int, top: int) -> None:
    lg = load_league(league)
    legs = top25_legs(lg, top)
    if not legs:
        print(f"No top-{top} {league} teams have a priced game on the board.")
        return

    print(f"\n{'=' * 78}\nTop-{top} {league} teams with a game on the board\n{'=' * 78}")
    print(f"  {'#':>3} {'team':26s}{'odds':>7}{'market':>9}{'fair':>9}{'edge':>8}  matchup")
    for l in legs:
        print(f"  {l['rank']:>3} {l['team_name'][:26]:26s}{l['odds']:>+7d}"
              f"{american_to_implied_prob(l['odds'])*100:>8.1f}%{l['implied_prob']*100:>8.1f}%"
              f"{l['edge_pp']:>+7.1f}pp  {l['matchup']}")

    # The favourites-only version, which is what "parlay the top 25" means: a
    # top-25 team is usually a short price, and short prices pay nothing.
    favs = [l for l in legs if l["odds"] < 0]
    print(f"\n  {len(favs)} of {len(legs)} are favourites "
          f"(median price {sorted(l['odds'] for l in favs)[len(favs)//2]:+d})"
          if favs else "\n  none are favourites")

    print(f"\n{'=' * 78}\nEvery independent {n_legs}-leg parlay of them\n{'=' * 78}")
    cands = []
    for combo in combinations(legs, n_legs):
        if len({l["event_id"] for l in combo}) != n_legs:
            continue  # same game: not independent, handled below
        cands.append(describe_parlay(list(combo)))
    if not cands:
        print("  Not enough independent games.")
        return

    cands.sort(key=lambda c: -c["ev"])
    print(f"  {'parlay':44s}{'pays':>8}{'hits':>8}{'need':>8}{'EV':>9}")
    for c in cands[:5]:
        label = " + ".join(f"{l['team_abbr']}" for l in c["legs"])
        print(f"  {label[:44]:44s}{c['combined']:>+8d}{c['fair_prob']*100:>7.1f}%"
              f"{c['breakeven']*100:>7.1f}%{c['ev']*100:>+8.1f}%")
    print(f"  {'...':44s}")
    for c in cands[-2:]:
        label = " + ".join(f"{l['team_abbr']}" for l in c["legs"])
        print(f"  {label[:44]:44s}{c['combined']:>+8d}{c['fair_prob']*100:>7.1f}%"
              f"{c['breakeven']*100:>7.1f}%{c['ev']*100:>+8.1f}%")

    best = cands[0]
    positive = [c for c in cands if c["ev"] > 0]
    print(f"\n  {len(cands)} combinations · {len(positive)} with positive EV at the "
          f"market's own numbers")
    print(f"  best pays {best['combined']:+d} "
          f"({best['payout']:.2f}u on 1u) and still returns "
          f"{best['ev']*100:+.1f}% per unit")
    print(f"  compounded margin on the best one: {best['hold']*100:.1f}%")

    # Same game, mixed markets: the part the general math does not cover.
    print(f"\n{'=' * 78}\nMixing markets on the same team\n{'=' * 78}")
    print("""  "Team wins" and "team goes over the total" are not independent -- a team
  putting up points is more likely to do both. The true joint probability is
  therefore HIGHER than the product of the two, which is precisely why no book
  sells a same-game parlay at the product: their same-game price removes the
  correlation and adds margin on top. Spread and moneyline on the same team are
  nearly the same bet and are usually refused outright.

  So mixing markets on one team does not multiply the payout. It buys a
  re-priced single with extra juice.""")

    # The forfeit. This is the number that decides it.
    print(f"\n{'=' * 78}\nWhat a parlay gives up\n{'=' * 78}")
    shop = _shop_gaps()
    if shop:
        print("  The one measured positive signal here is line shopping. Real gaps the")
        print("  scanner logged, best book against consensus:\n")
        print(f"  {'side':28s}{'best':>7}{'consensus':>11}{'extra':>8}")
        for r in shop[:6]:
            extra = (american_to_implied_prob(r["consensus_price"])
                     / american_to_implied_prob(r["price"]) - 1)
            print(f"  {r['side'][:28]:28s}{r['price']:>+7d}"
                  f"{r['consensus_price']:>+11d}{extra*100:>+7.1f}%")
        legs3 = shop[:3]
        if len(legs3) == 3:
            best_dec = 1.0
            cons_dec = 1.0
            for r in legs3:
                best_dec *= american_to_decimal(r["price"])
                cons_dec *= american_to_decimal(r["consensus_price"])
            print(f"\n  Those three as singles, each at its own best book: {best_dec:.1f}x")
            print(f"  The same three as one parlay, at one book:          {cons_dec:.1f}x")
            print(f"  -> {(1 - cons_dec / best_dec) * 100:.0f}% of the payout surrendered, "
                  f"because a parlay cannot shop its legs.")
    else:
        print("  (no logged price gaps yet — run shop_ledger.py --scan)")

    print(f"""
  That is the whole trade. A parlay is the one bet shape that cannot use the
  only edge this project has actually measured.""")


def _shop_gaps() -> List[dict]:
    """The price gaps the scanner logged, biggest first."""
    from archive import read_ndjson
    from espn import current_season_year
    path = os.path.join(ROOT, "data", "archive", f"shop_{current_season_year()}.ndjson")
    rows = [r for r in read_ndjson(path) if r.get("consensus_price")]
    rows.sort(key=lambda r: -r.get("edge", 0))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF")
    ap.add_argument("--legs", type=int, default=3)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    report(args.league, args.legs, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
