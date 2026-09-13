"""
Parlays built only from legs that beat the market, split by league.

WHY THIS IS NOT THE USUAL PARLAY. Stacking multiplies the SIGN of the edge.
Six book-favourites parlayed multiply a 0.959 keep rate down to 0.778 -- a
-22% bet dressed up as +430. The same arithmetic run on legs that each clear
1.0000 multiplies UPWARD: three legs at 1.020, 1.016 and 1.011 combine to
1.048, a bigger edge than any leg in it.

So the leg filter is the whole product. A leg qualifies only when one book's
price beats the de-vigged consensus of the others -- that is a claim about
PRICE, not about who wins, and it is the only claim this project has ever
measured a positive keep rate on. Nothing here predicts a game.

RULES THAT COST MONEY IF SKIPPED:

  Per book. A parlay is built inside one book. Two +EV legs at two different
  books are two singles, and reporting them as a ticket is a bet nobody can
  place.

  Per league. NFL and college are separate boards with separate pricing.
  College is where the gaps actually appear -- 20 of 23 recorded opportunities
  -- because nobody prices Colgate carefully, and mixing the two hides that.

  One leg per game. Both sides of the same game cannot be parlayed, and two
  legs from one game are not independent even when the book allows it.

  Same slate. Legs are refused when they kick off more than MAX_SPAN_DAYS
  apart. Consensus gaps are measured against a live market; a number hung a
  week early is a placeholder, and pairing Monday night with next Saturday is
  seven days of drift on one ticket.

  python parlays.py --league NFL --window day
  python parlays.py --league NCAAF --window week
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import keys  # noqa: F401
import line_shop as LS

MAX_SPAN_DAYS = 3.0     # legs on one ticket must kick off within this of each other
MIN_LEAD = timedelta(minutes=10)   # a bet you cannot reach the counter for is not a bet
MIN_LEG_EDGE = 0.005    # below half a percent the gap is inside the noise of a de-vig
MARKETS = ("h2h", "spreads", "totals")


def _dec(a: int) -> float:
    return 1 + (a / 100 if a > 0 else 100 / abs(a))


def _american(d: float) -> int:
    return int(round((d - 1) * 100)) if d >= 2 else -int(round(100 / (d - 1)))


def window_bounds(window: str, now: Optional[datetime] = None):
    now = now or datetime.now(timezone.utc)
    now = now + MIN_LEAD
    if window == "day":
        # through the end of tonight's slate, not a rolling 24h: a "today"
        # ticket that quietly includes tomorrow afternoon is not a day parlay.
        end = (now - MIN_LEAD).replace(hour=23, minute=59, second=59) + timedelta(days=1)
    else:
        end = now + timedelta(days=7)
    return now, end


def find_legs(league: str, window: str = "day") -> List[dict]:
    """Every price beating its market's consensus, inside the window."""
    start, end = window_bounds(window)
    out: List[dict] = []
    for market in MARKETS:
        payload = LS.fetch_live(league, market, "us")
        for game, kick, prices in LS.games_from_live(payload, market):
            try:
                k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if not (start < k <= end):
                continue
            for r in LS.find_value(prices, min_edge=MIN_LEG_EDGE):
                keep = r["fair_prob"] / (1 / _dec(r["price"]))
                out.append(dict(r, game=game, kickoff=k, market=market,
                                league=league, keep=keep))
    return sorted(out, key=lambda r: -r["edge"])


def build(legs: List[dict], max_legs: int = 4) -> Dict[str, dict]:
    """
    Best parlay per book. Returns {} for a book with fewer than two legs --
    those are singles and are reported as singles.
    """
    by_book: Dict[str, List[dict]] = defaultdict(list)
    for l in legs:
        by_book[l["book"]].append(l)

    out: Dict[str, dict] = {}
    for book, rows in by_book.items():
        rows = sorted(rows, key=lambda r: -r["edge"])
        chosen: List[dict] = []
        for r in rows:
            if any(c["game"] == r["game"] for c in chosen):
                continue                      # one leg per game
            if chosen:
                span = max(abs((r["kickoff"] - c["kickoff"]).total_seconds())
                           for c in chosen) / 86400
                if span > MAX_SPAN_DAYS:
                    continue                  # different slate
            chosen.append(r)
            if len(chosen) == max_legs:
                break
        if len(chosen) < 2:
            continue
        d = 1.0
        keep = 1.0
        for c in chosen:
            d *= _dec(c["price"])
            keep *= c["keep"]
        out[book] = {"legs": chosen, "decimal": d, "american": _american(d),
                     "keep": keep, "ev": keep - 1, "hit": keep / d}
    return out


def report(league: str, window: str, legs: List[dict], parlays: Dict[str, dict]) -> None:
    print(f"\n{'='*66}\n{league} — {window.upper()} window — "
          f"{len(legs)} leg(s) beating consensus\n{'='*66}")
    if not legs:
        print("Nothing is mispriced. No ticket.")
        return
    if not parlays:
        print("No book has two qualifying legs on one slate. Singles only:\n")
        for l in legs:
            print(f"  {l['edge']*100:+5.2f}%  {l['side'][:32]:<33}{l['price']:>+6} "
                  f"{l['book']:<13}{l['kickoff'].strftime('%a %H:%M')}  {l['market']}")
        return
    for book, p in sorted(parlays.items(), key=lambda kv: -kv[1]["ev"]):
        print(f"\n--- {book}: {len(p['legs'])} legs")
        for l in p["legs"]:
            print(f"      {l['side'][:32]:<33}{l['price']:>+6}  keep {l['keep']:.4f}"
                  f"   {l['kickoff'].strftime('%a %H:%M')}  {l['market']}")
        print(f"    pays {p['american']:+d}   hits {p['hit']*100:.1f}%   "
              f"keep {p['keep']:.4f}   EV {p['ev']*100:+.2f}%")
        if p["hit"] < 0.2:
            print(f"    NOTE: loses {(1-p['hit'])*100:.0f}% of the time. "
                  "Positive EV, brutal variance — size it small.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=sorted(LS.SPORTS))
    ap.add_argument("--window", default="day", choices=("day", "week"))
    ap.add_argument("--max-legs", type=int, default=4)
    ap.add_argument("--both", action="store_true",
                    help="run NFL and college separately, in one pass")
    a = ap.parse_args()

    leagues = sorted(LS.SPORTS) if a.both else [a.league]
    for lg in leagues:
        legs = find_legs(lg, a.window)
        report(lg, a.window, legs, build(legs, a.max_legs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
