"""
Payout-target solver: "I want +300 — what is the best chance of getting it?"

THE TRADE-OFF THIS TOOL EXISTS TO SHOW. Probability and payout are not two
knobs. They are one knob with a label on each end: a price of +300 pays 4x, so
25% is what it takes to break even and roughly what the ticket will really win.
There is no 80%-at-+300 ticket to find, at any book, in any sport. If one
existed it would be arbitraged inside a minute.

So this does not hunt for a high percentage at a big payout. It answers the
question that does have an answer: given a payout you want, which route gets
there with the LEAST given away, and how close to fair is it?

    python target.py --league NCAAF --target 300
    python target.py --league NFL --target 300 --tickets 3

Three routes are priced for every target:

  SINGLE      one side that already pays the target. Always the least vig,
              because vig compounds per leg -- a parlay pays the hold twice.
  PARLAY      the fewest moneyline legs that reach it, chosen to give away as
              little as possible. Probabilities are the market's own de-vigged
              numbers, NOT the Elo model's: the model has been graded against
              closing lines and loses, the market has not.
  TEASER      a 6- or 10-point ticket from the qualifying windows, priced off
              JOINT_6PT / JOINT_10PT in teaser.py -- measured rates, and the
              only route here that is ever priced better than fair, because it
              is the one bet whose probability does not come from the price.

The honest output is usually "the single is closest to fair, the parlay costs
you two points, and the teaser beats both if your book's ladder is generous".
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import teaser as T
from odds import american_to_decimal, de_vig_probs

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(ROOT, "public", "data", "board.json")

# A book's teaser ladder, which is the difference between this bet existing and
# not. Only two of these are verified; the rest are the common ladder and are
# printed as assumptions so nobody bets one believing it was checked.
#
#   2 legs at -134  FanDuel, checked on the slip 2026-09-13 (books.py)
#   6 legs at +750  quoted by the user 2026-09-16
# -134 at two legs, not the -120 the common ladder shows: that is FanDuel's
# real price, checked on the slip. It was carried as -120 with the true number
# only mentioned in a note, which priced the ticket better than it is.
LADDER_6PT = {2: -134, 3: 160, 4: 265, 5: 450, 6: 750}
LADDER_10PT = {4: 160, 5: 220, 6: 310}

# WHICH BOOK, AND HOW WE KNOW. A teaser price without a book attached is not a
# price: ladders differ, and the same 4-leg 6-pointer is +265 at one shop and
# +240 at the next. Anything that reaches a public video has to be able to say
# where its number came from, so the provenance travels with the price rather
# than living in a commit message.
#
# `book: None` means the price is real but the book was never written down --
# an honest gap, printed as such, not quietly upgraded to verified.
LADDER_SOURCE = {
    ("6pt", 2): {"book": "FanDuel", "how": "checked on the slip", "on": "2026-09-13"},
    ("6pt", 6): {"book": None, "how": "quoted from your book", "on": "2026-09-16"},
    ("10pt", 6): {"book": None, "how": "quoted from your book", "on": "2026-09-16"},
}
ASSUMED = {"book": None, "how": "common ladder, not checked anywhere", "on": None}


def price_source(points: int, legs: int) -> dict:
    """Where a ladder price came from, and whether a book was recorded."""
    return LADDER_SOURCE.get((f"{points}pt", legs), ASSUMED)


def source_line(points: int, legs: int) -> str:
    """One readable clause for a page, a report or a video."""
    src = price_source(points, legs)
    if src["book"]:
        on = f", {src['on']}" if src.get("on") else ""
        return f"{src['book']} \u2014 {src['how']}{on}"
    if src is ASSUMED:
        return src["how"]
    return f"{src['how']} \u2014 book not recorded"


VERIFIED = {k: source_line(int(k[0][:-2]), k[1]) for k in LADDER_SOURCE}

# How many candidate legs the parlay search considers, best-priced first. The
# board carries 60-90 sides; every 5-subset of all of them is millions, and the
# ones that matter are the cheapest few dozen by hold.
CANDIDATE_LEGS = 24


@dataclass
class Leg:
    event_id: str
    matchup: str
    kickoff: str
    abbr: str
    odds: int
    prob: float          # de-vigged market probability

    @property
    def decimal(self) -> float:
        return american_to_decimal(self.odds)

    @property
    def hold(self) -> float:
        """What the book keeps on this leg, as a fraction."""
        return 1.0 - self.prob * self.decimal


@dataclass
class Ticket:
    route: str
    legs: List[Leg]
    decimal: float
    prob: float
    note: str = ""
    ladder_assumed: bool = False

    @property
    def american(self) -> int:
        d = self.decimal
        return int(round((d - 1) * 100)) if d >= 2 else -int(round(100 / (d - 1)))

    @property
    def breakeven(self) -> float:
        return 1.0 / self.decimal

    @property
    def ev(self) -> float:
        return self.prob * (self.decimal - 1) - (1.0 - self.prob)


def load_legs(league: str, path: str = BOARD) -> List[Leg]:
    """Moneyline sides from the board, priced by the market rather than the model."""
    with open(path) as fh:
        board = json.load(fh)
    lg = (board.get("leagues") or {}).get(league.upper())
    if not lg:
        raise SystemExit(f"{league} is not in {path}")
    out: List[Leg] = []
    for g in lg.get("games") or []:
        home_ml = (g.get("home") or {}).get("moneyline")
        away_ml = (g.get("away") or {}).get("moneyline")
        hp, ap = de_vig_probs(home_ml, away_ml)
        if hp is None or ap is None:
            continue
        for leg in g.get("legs") or []:
            prob = hp if leg["side"] == "home" else ap
            out.append(Leg(str(g["event_id"]), g.get("short_name") or "",
                           g.get("kickoff") or "", leg["team_abbr"],
                           int(leg["odds"]), float(prob)))
    return out


def best_single(legs: List[Leg], target: int) -> Optional[Ticket]:
    want = american_to_decimal(target)
    ok = [l for l in legs if l.decimal >= want]
    if not ok:
        return None
    # Highest probability, which is also the shortest price that still pays the
    # target -- there is nothing to trade off on a single leg.
    best = max(ok, key=lambda l: l.prob)
    return Ticket("single", [best], best.decimal, best.prob)


def best_parlay(legs: List[Leg], target: int, max_legs: int = 5,
                exclude: Tuple[str, ...] = ()) -> Optional[Ticket]:
    """
    The fewest legs that reach the target, giving away as little as possible.

    Probability is not really a choice here: any route to +300 lands near 25%
    because that is what the price means. What IS a choice is how much hold is
    paid on the way, so the search maximises the product of de-vigged
    probabilities -- which is the same as minimising total hold.
    """
    want = american_to_decimal(target)
    avail = [l for l in legs if l.event_id not in exclude]
    # Both ends of the board, not just the cheapest legs. Ranking by hold alone
    # fills the pool with heavy favourites -- a -5000 side keeps almost nothing
    # back -- and no pair of those reaches a big payout, so the search returned
    # a 9.6% ticket on a board that had a 24% one. A route needs a long leg to
    # carry the payout and a cheap leg to keep the probability.
    by_hold = sorted(avail, key=lambda l: -(l.prob * l.decimal))[:CANDIDATE_LEGS]
    by_payout = sorted(avail, key=lambda l: -l.decimal)[:CANDIDATE_LEGS]
    pool = list({id(l): l for l in by_hold + by_payout}.values())
    best: Optional[Ticket] = None
    for n in range(2, max_legs + 1):
        for combo in itertools.combinations(pool, n):
            if len({l.event_id for l in combo}) != n:
                continue        # one leg per game: two sides of one game is not a parlay
            dec = 1.0
            p = 1.0
            for l in combo:
                dec *= l.decimal
                p *= l.prob
            if dec < want:
                continue
            t = Ticket("parlay", list(combo), dec, p)
            if best is None or t.prob > best.prob:
                best = t
        if best is not None:
            break               # fewer legs is always cheaper; stop at the first count that works
    return best


def teaser_tickets(target: int, league: str) -> List[Ticket]:
    """
    Every teaser leg count whose ladder price clears the target.

    NFL only. The bands and the joint rates are measured on NFL margins; the
    same windows in college win 70.9% per leg against the 72.4% that -110 needs
    (see the college study in the findings), so quoting these numbers for a
    college ticket would be lending it a result it does not have.
    """
    if league.upper() != "NFL":
        return []
    want = american_to_decimal(target)
    out: List[Ticket] = []
    for pts, ladder, joint in ((6, LADDER_6PT, T.JOINT_6PT),
                               (10, LADDER_10PT, T.JOINT_10PT)):
        for n, price in sorted(ladder.items()):
            if n not in joint:
                continue
            dec = american_to_decimal(price)
            if dec < want:
                continue
            key = (f"{pts}pt", n)
            out.append(Ticket(
                f"{pts}-pt teaser, {n} legs", [], dec, joint[n],
                note=VERIFIED.get(key, "ladder price ASSUMED — check the slip"),
                ladder_assumed=key not in VERIFIED,
            ))
    return sorted(out, key=lambda t: -t.ev)


def describe(t: Ticket) -> str:
    legs = " + ".join(f"{l.abbr} {l.odds:+d}" for l in t.legs)
    body = f"  {t.route:22} {t.american:+6}  wins {t.prob*100:5.1f}%  "
    body += f"needs {t.breakeven*100:5.1f}%  EV {t.ev*100:+6.1f}%"
    if legs:
        body += f"\n      {legs}"
    if t.note:
        body += f"\n      {t.note}"
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--target", type=int, default=300,
                    help="payout you want, American (default: %(default)s)")
    ap.add_argument("--tickets", type=int, default=1,
                    help="how many non-overlapping tickets to build")
    ap.add_argument("--max-legs", type=int, default=5)
    ap.add_argument("--frontier", action="store_true",
                    help="what every payout target is really worth")
    ap.add_argument("--certainty", type=float, default=None,
                    help="the other direction: name a win chance (0.80) and "
                         "see the best payout that actually buys")
    a = ap.parse_args()

    legs = load_legs(a.league)
    print(f"{len(legs)} priced sides on the {a.league} board\n")

    if a.frontier:
        print("WHAT EACH PAYOUT IS WORTH (market probabilities, best route)")
        print(f"  {'target':>8}{'best chance':>13}{'break-even':>12}{'route':>10}")
        for tgt in (100, 150, 200, 300, 500, 750, 1000):
            cands = [x for x in [best_single(legs, tgt),
                                 best_parlay(legs, tgt, a.max_legs)] if x]
            cands += teaser_tickets(tgt, a.league)
            if not cands:
                continue
            best = max(cands, key=lambda t: t.prob)
            print(f"  {tgt:>+8}{best.prob*100:>12.1f}%{best.breakeven*100:>11.1f}%"
                  f"{best.route.split(',')[0]:>10}")
        print()

    if a.certainty is not None:
        # The question asked the other way round, which is the honest way to
        # ask it: a win chance is not something a bettor picks either. It is
        # bought, and this is the price.
        want = a.certainty
        rows = []
        for l in legs:
            if l.prob >= want:
                rows.append(Ticket("single", [l], l.decimal, l.prob))
        for pts, ladder, joint in ((6, LADDER_6PT, T.JOINT_6PT),
                                   (10, LADDER_10PT, T.JOINT_10PT)):
            for n, price in ladder.items():
                if n in joint and joint[n] >= want and a.league.upper() == "NFL":
                    rows.append(Ticket(f"{pts}-pt teaser, {n} legs", [],
                                       american_to_decimal(price), joint[n]))
        print(f"BEST PAYOUT AT {want*100:.0f}% OR BETTER")
        if not rows:
            best_any = max(legs, key=lambda l: l.prob)
            print(f"  Nothing on the board wins {want*100:.0f}% of the time. The "
                  f"most likely single side is {best_any.abbr} at "
                  f"{best_any.odds:+d}, {best_any.prob*100:.1f}%.")
            print(f"  A {want*100:.0f}% ticket is worth about "
                  f"{int(round(100/want - 100)):+d} if priced fairly — that is what "
                  f"certainty costs, and no book pays more for it.")
        else:
            for t in sorted(rows, key=lambda t: -t.decimal)[:6]:
                print(describe(t))
        print()

    used: List[str] = []
    for i in range(a.tickets):
        cands = [x for x in [best_single([l for l in legs if l.event_id not in used], a.target),
                             best_parlay(legs, a.target, a.max_legs, tuple(used))] if x]
        cands += teaser_tickets(a.target, a.league)
        if not cands:
            print(f"Nothing on the board reaches {a.target:+d}.")
            return 0
        print(f"TICKET {i+1} — target {a.target:+d}")
        for t in sorted(cands, key=lambda t: -t.prob):
            print(describe(t))
        pick = max((t for t in cands if t.legs), key=lambda t: t.prob, default=None)
        if pick:
            used.extend(l.event_id for l in pick.legs)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
