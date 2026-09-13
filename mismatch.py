"""
Price moneylines off the spread, and bet where a book disagrees with itself.

THE IDEA. Every other scanner here compares one book's price against other
books' prices. This compares a book's MONEYLINE against its own SPREAD, using
what spreads historically convert to:

    P(favourite wins outright) = 1 / (1 + exp(-(-0.1510 + 0.1390 * spread)))

fitted on 2,708 college games, 2021-2025. It holds up across the range --
model against actual: 51.4/52.8, 61.6/62.5, 70.9/73.2, 87.4/89.7, 93.7/92.6,
98.1/98.0. The one soft spot is 9 to 13 points, where the model says 79.9% and
reality says 75.0%, so edges in that band are discounted.

WHY COLLEGE. It has the mismatches. 743 of 2,705 games since 2023 had a
favourite of 17+ points, winning 96.2%; the NFL had 24 such games in 27
seasons. A board that wants high-probability moneylines has to be a college
board, because the NFL does not produce them.

WHAT THIS IS NOT. A claim to know better than the market who wins. The spread
IS the market's opinion; this only checks the moneyline was converted from it
consistently. When a book hangs a 24-point favourite and prices the ML as
though it were an 18-point one, that is an internal inconsistency, not a
disagreement about the game.

  python mismatch.py --league NCAAF
  python mismatch.py --league NCAAF --parlay
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import keys  # noqa: F401
import line_shop as LS

ROOT = os.path.dirname(os.path.abspath(__file__))
CURVE = os.path.join(ROOT, "cache", "ml_curve.json")

# Fitted coefficients; refit with the block in this module's history.
B0, B1 = -0.1510, 0.1390

# Above this the book is not inconsistent, the data is wrong. Same guard, same
# reason, as line_shop's MAX_EDGE and confidence's MAX_TRUSTED_GAP.
MAX_EDGE = 0.12
# The 9-13 point band fits worst (model 79.9%, actual 75.0%), so estimates
# there are shrunk TOWARD 50% rather than subtracted from.
#
# Subtracting was a bug with a direction: it shaded a favourite down, which is
# conservative and fine, and shaded an underdog down too, which compounds the
# error instead of correcting it. It made West Virginia +10 read as a 12.6%
# side when 9-13 point college dogs actually win about 25%, and produced a
# -14.8pp "edge" that was entirely my own arithmetic.
SOFT_BAND = (9.0, 13.0)
SOFT_SHRINK = 0.25          # move a quarter of the way to a coin flip


def win_prob(spread: float) -> float:
    """P(this side wins outright) given the points it is laying (negative)."""
    return 1.0 / (1.0 + math.exp(-(B0 + B1 * -spread)))


def implied(american: int) -> float:
    return 1 / (1 + (american / 100 if american > 0 else 100 / abs(american)))


def _point(label: str) -> Optional[float]:
    try:
        return float(label.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return None


def scan(league: str = "NCAAF", days: int = 8,
         books: Optional[List[str]] = None, min_edge: float = 0.02) -> List[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)

    spreads = LS.fetch_live(league, "spreads", "us")
    mls = LS.fetch_live(league, "h2h", "us")

    # book -> team -> spread, per game
    by_game: Dict[str, dict] = {}
    for game, kick, prices in LS.games_from_live(spreads, "spreads", 2):
        d: Dict[str, Dict[str, float]] = defaultdict(dict)
        for book, sides in prices.items():
            for label, _price in sides.items():
                pt = _point(label)
                if pt is not None:
                    d[book][label.rsplit(" ", 1)[0]] = pt
        by_game[game] = {"kick": kick, "spreads": d}

    out: List[dict] = []
    for game, kick, prices in LS.games_from_live(mls, "h2h", 2):
        try:
            k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if not (now < k <= end):
            continue
        info = by_game.get(game)
        if not info:
            continue
        for book, sides in prices.items():
            if book in LS.IGNORE_BOOKS:
                continue
            if books and not any(b in book.lower() for b in books):
                continue
            book_spreads = info["spreads"].get(book) or {}
            for side, price in sides.items():
                sp = book_spreads.get(side)
                if sp is None:
                    continue
                p = win_prob(sp)
                if SOFT_BAND[0] <= abs(sp) <= SOFT_BAND[1]:
                    p = p + (0.5 - p) * SOFT_SHRINK
                edge = p - implied(price)
                if not (min_edge <= edge <= MAX_EDGE):
                    continue
                out.append({"game": game, "side": side, "book": book,
                            "spread": sp, "price": price, "chance": p,
                            "needs": implied(price), "edge": edge,
                            "kickoff": k})
    return sorted(out, key=lambda r: -r["edge"])


def build(rows: List[dict], max_legs: int = 4) -> Dict[str, dict]:
    """Best parlay per book from legs that clear. One leg per game."""
    per: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        per[r["book"]].append(r)
    out: Dict[str, dict] = {}
    for book, legs in per.items():
        legs = sorted(legs, key=lambda r: -r["edge"])
        chosen, seen = [], set()
        for l in legs:
            if l["game"] in seen:
                continue
            chosen.append(l)
            seen.add(l["game"])
            if len(chosen) == max_legs:
                break
        if len(chosen) < 2:
            continue
        d = ch = keep = 1.0
        for c in chosen:
            d *= 1 + (c["price"] / 100 if c["price"] > 0 else 100 / abs(c["price"]))
            ch *= c["chance"]
            keep *= c["chance"] / c["needs"]
        out[book] = {"legs": chosen, "decimal": d, "chance": ch, "keep": keep,
                     "american": int(round((d - 1) * 100)) if d >= 2
                     else -int(round(100 / (d - 1)))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NCAAF", choices=sorted(LS.SPORTS))
    ap.add_argument("--books", default="betmgm,fanduel")
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--parlay", action="store_true")
    a = ap.parse_args()
    bk = [b.strip().lower() for b in a.books.split(",") if b.strip()] or None
    rows = scan(a.league, books=bk, min_edge=a.min_edge)
    if not rows:
        print("No book's moneyline disagrees with its own spread by "
              f"{a.min_edge*100:.0f}pp or more.")
        return 0
    print(f"\n{len(rows)} moneyline(s) priced short of what the spread implies\n")
    print(f"{'side':<30}{'spread':>7}{'ML':>7}{'implies':>9}{'ML needs':>10}{'edge':>7}  book")
    for r in rows:
        print(f"{r['side'][:29]:<30}{r['spread']:>+7g}{r['price']:>+7}"
              f"{r['chance']*100:>8.1f}%{r['needs']*100:>9.1f}%{r['edge']*100:>+6.1f}  {r['book']}")
    if not a.parlay:
        return 0
    for book, p in sorted(build(rows).items(), key=lambda kv: -kv[1]["keep"]):
        print(f"\n--- {book}: {len(p['legs'])} legs, pays {p['american']:+d}")
        for l in p["legs"]:
            print(f"      {l['chance']*100:5.1f}%  {l['side']} {l['price']:+d}")
        print(f"    hits {p['chance']*100:.1f}%   keep {p['keep']:.4f}   "
              f"EV {(p['keep']-1)*100:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
