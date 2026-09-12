"""
Price a parlay you are actually considering, and rank every swap.

The question this answers is "which leg should I change", and the answer is
almost never the one that feels weak. A parlay's expectation reduces to one
product:

    EV + 1  =  product over legs of ( fair_prob / priced_prob )

Call that ratio a leg's KEEP RATE: the share of a unit that survives the book's
margin on that leg. It follows that

  * the expectation does not care how big a favourite a leg is. A -3000 leg and
    a -110 leg contribute identically if their keep rates match.
  * the leg to drop is the one with the WORST keep rate, which in practice is
    usually a heavy favourite -- books take a wider cut on lopsided lines
    because almost nobody shops them.
  * no swap can rescue a long parlay, because leg count sets the floor. Six
    legs at the best keep rate available is still about -20%.

So the tool prints the keep rate per leg, the best available replacement for
each, and the ceiling by leg count -- and, separately, the legs that are +EV as
singles at their best book, which is the thing a parlay structurally cannot use.

  python parlay_price.py --legs "Oregon,Texas A&M,Oklahoma,Penn State,Alabama,BYU"
  python parlay_price.py --legs "..." --quote 216     # check the offered price
  python parlay_price.py --singles                    # just the +EV singles

Costs one credit per league scanned.
"""

from __future__ import annotations

import argparse
import json
import os
from statistics import median
from typing import Dict, List, Optional

import keys  # noqa: F401  (loads ~/.football-picks.env)
from line_shop import fetch_live, games_from_live
from odds import american_to_decimal, american_to_implied_prob, combine_odds

LEAGUES = ("NCAAF", "NFL")
# Where percentage edge behaves and where a real account can bet. Same limits
# the scanner uses: at +3000 a one-point pricing difference reads as a huge
# "edge" and is usually a data error.
MIN_PRICE, MAX_PRICE = -700, 600


def scan(leagues=LEAGUES) -> List[dict]:
    """Every priced side, de-vigged against its own opposite number."""
    rows: List[dict] = []
    for league in leagues:
        try:
            payload = fetch_live(league, "h2h", "us")
        except SystemExit:
            raise
        except Exception as e:
            print(f"[parlay] {league} prices unavailable: {e}")
            continue
        for game, kickoff, prices in games_from_live(payload, "h2h"):
            sides: Dict[str, List[tuple]] = {}
            for book, s in prices.items():
                for name, price in s.items():
                    sides.setdefault(name, []).append((book, price))
            if len(sides) != 2:
                continue
            names = list(sides)
            for i, name in enumerate(names):
                mine = [p for _, p in sides[name]]
                theirs = [p for _, p in sides[names[1 - i]]]
                med, omed = median(mine), median(theirs)
                a, b = american_to_implied_prob(med), american_to_implied_prob(omed)
                best = max(mine)
                rows.append({
                    "league": league, "team": name, "game": game, "kickoff": kickoff,
                    "median": med, "best": best, "n_books": len(mine),
                    "book": next(bk for bk, p in sides[name] if p == best),
                    "priced": a,
                    # Two-way de-vig: no assumed hold, both real prices.
                    "fair": a / (a + b),
                    "hold": (a + b) - 1,
                    "keep": (a / (a + b)) / a,
                    "keep_best": (a / (a + b)) / american_to_implied_prob(best),
                })
    return rows


def match(rows: List[dict], name: str) -> Optional[dict]:
    """
    Resolve a typed team name to one priced side.

    A team can appear twice -- this week's game and next week's -- so ties break
    toward the earliest kickoff, which is what someone naming a team today
    means. Ambiguity is reported rather than guessed at silently.
    """
    key = name.strip().lower()
    hits = [r for r in rows if r["team"].lower().startswith(key)]
    if not hits:
        hits = [r for r in rows if key in r["team"].lower()]
    if not hits:
        return None
    hits.sort(key=lambda r: r["kickoff"] or "")
    if len(hits) > 1:
        others = ", ".join(f"{h['game'][:30]} ({h['median']:+.0f})" for h in hits[1:])
        print(f"  note: '{name}' also matches {others} — using the earliest, "
              f"{hits[0]['game'][:40]}")
    return hits[0]


def price_ticket(legs: List[dict], quote: Optional[int]) -> None:
    keep = 1.0
    fair = 1.0
    dec = 1.0
    for r in legs:
        keep *= r["keep"]
        fair *= r["fair"]
        dec *= american_to_decimal(r["median"])

    print(f"\n  {'leg':26s}{'price':>7}{'fair':>8}{'hold':>7}{'keep':>8}  game")
    for r in sorted(legs, key=lambda r: r["keep"], reverse=True):
        print(f"  {r['team'][:26]:26s}{r['median']:>+7.0f}{r['fair']*100:>7.1f}%"
              f"{r['hold']*100:>6.1f}%{r['keep']*100:>7.2f}%  {r['game'][:34]}")

    combined = combine_odds([r["median"] for r in legs])
    print(f"\n  combined at median prices : {combined:+d}  ({dec:.3f}x)")
    print(f"  all legs win (de-vigged)  : {fair*100:.1f}%")
    print(f"  break-even at that price  : {1/dec*100:.1f}%")
    print(f"  EV                        : {(keep-1)*100:+.1f}%   "
          f"(= the product of the keep rates)")
    print(f"  loses the whole ticket    : {(1-fair)*100:.0f}% of the time")
    if quote is not None:
        qd = american_to_decimal(quote)
        print(f"\n  you were quoted {quote:+d} ({qd:.3f}x), which is "
              f"{(qd/dec-1)*100:+.1f}% against median pricing")
        print(f"  EV at the quoted price    : {(fair*(qd-1)-(1-fair))*100:+.1f}%")


def rank_swaps(legs: List[dict], rows: List[dict]) -> None:
    """Which leg to change, worst keep rate first."""
    own_games = {r["game"] for r in legs}
    own_teams = {r["team"] for r in legs}
    pool = [r for r in rows
            if MIN_PRICE <= r["median"] <= MAX_PRICE
            and r["game"] not in own_games and r["team"] not in own_teams]
    if not pool:
        return
    base = 1.0
    for r in legs:
        base *= r["keep"]

    print(f"\n  Every swap, worst leg first. The gain is the difference in keep")
    print(f"  rate and nothing else, which is why none of them is large.\n")
    print(f"  {'drop':24s}{'keep':>8}   {'add':24s}{'new EV':>9}{'gain':>9}")
    for d in sorted(legs, key=lambda r: r["keep"]):
        b = max(pool, key=lambda c: c["keep"])
        ev = base / d["keep"] * b["keep"]
        print(f"  {d['team'][:24]:24s}{d['keep']*100:>7.2f}%   {b['team'][:24]:24s}"
              f"{(ev-1)*100:>+8.2f}%{(ev-base)*100:>+8.2f}pp")


def ceiling(rows: List[dict], max_legs: int = 6) -> None:
    """The best parlay available at any leg count: leg COUNT sets the floor."""
    pool = sorted([r for r in rows if MIN_PRICE <= r["median"] <= MAX_PRICE],
                  key=lambda r: -r["keep"])
    seen, pick = set(), []
    for r in pool:
        if r["game"] in seen:
            continue
        seen.add(r["game"])
        pick.append(r)
        if len(pick) == max_legs:
            break
    print(f"\n  The ceiling — the best legs on the board, by leg count. No choice")
    print(f"  of teams beats this, so leg count is the only real lever.\n")
    print(f"  {'legs':>5}{'combined':>10}{'EV':>9}")
    for n in range(1, len(pick) + 1):
        k = 1.0
        for r in pick[:n]:
            k *= r["keep"]
        print(f"  {n:>5}{combine_odds([r['median'] for r in pick[:n]]):>+10d}"
              f"{(k-1)*100:>+8.1f}%")


def show_singles(rows: List[dict], limit: int = 12) -> None:
    """
    The legs that are +EV on their own, at the best book of nine.

    This is the part a parlay cannot have. A ticket sits at one book, so every
    leg takes that book's price instead of the best available -- which is
    exactly the edge being measured here.
    """
    pos = [r for r in rows if r["keep_best"] > 1.0
           and MIN_PRICE <= r["median"] <= MAX_PRICE]
    pos.sort(key=lambda r: -r["keep_best"])
    print(f"\n  +EV as singles, at the best book of {max(r['n_books'] for r in rows)}:")
    print(f"  {len(pos)} of {len(rows)} priced sides qualify.\n")
    print(f"  {'leg':26s}{'median':>8}{'best':>7}  {'book':14s}{'EV':>8}  kickoff")
    for r in pos[:limit]:
        print(f"  {r['team'][:26]:26s}{r['median']:>+8.0f}{r['best']:>+7d}  "
              f"{r['book'][:13]:14s}{(r['keep_best']-1)*100:>+7.1f}%  {r['kickoff'][:16]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legs", help="comma-separated team names")
    ap.add_argument("--quote", type=int, help="the combined price you were offered")
    ap.add_argument("--singles", action="store_true", help="only the +EV singles")
    ap.add_argument("--league", action="append", choices=list(LEAGUES),
                    help="limit the scan (saves a credit)")
    args = ap.parse_args()

    rows = scan(args.league or LEAGUES)
    if not rows:
        print("No prices available.")
        return 1

    if args.legs:
        names = [n for n in (x.strip() for x in args.legs.split(",")) if n]
        legs, missing = [], []
        for n in names:
            r = match(rows, n)
            (legs.append(r) if r else missing.append(n))
        if missing:
            print(f"\n  no live market for: {', '.join(missing)}")
        if legs:
            price_ticket(legs, args.quote)
            rank_swaps(legs, rows)
    if args.legs or not args.singles:
        ceiling(rows)
    show_singles(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
