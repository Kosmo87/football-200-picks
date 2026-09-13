"""
The board ranked by how likely each bet is to WIN, not by how much it pays.

WHY THIS EXISTS SEPARATELY. Every other scanner here ranks by edge, which
surfaces longshots: the 2026 ledger averages a 24.3% win probability, so three
tickets in four lose. That is defensible arithmetic and miserable to live with,
and "I would rather risk 100 to win 60 than 60 on a 40% shot" is a legitimate
way to bet that the edge-first view actively hides.

So this ranks by win probability and shows the price beside it, rather than
choosing for you.

THE ONE NUMBER THAT STILL MATTERS. A price is a required win rate. -167 needs
62.5%, -250 needs 71.4%, -500 needs 83.3%. A bet only makes money when its real
chance beats that, so both are shown together: CHANCE is the de-vigged
consensus, NEEDS is what the price demands, and GAP is the difference. A
positive gap is a bet whose confidence is not already fully charged for.

Sorted by confidence, so the most likely winners are at the top whatever the
gap says -- the ranking is yours, the arithmetic is just printed honestly.
"""

from __future__ import annotations

import argparse
import itertools
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import keys  # noqa: F401
import line_shop as LS


# A price this far from the market consensus is a fault, not an opportunity.
# On 2026-09-13 BetMGM's feed had Kansas @ Arizona State transposed: ASU at
# +170 on the moneyline while three books had -200 to -225, and +6 on the
# spread while six books had -5.5. De-vigged, that reads as a 27-point edge,
# and the ticket builder duly put it at the top of BetMGM's board with a keep
# rate of 1.73. line_shop has carried this guard for the same reason; the
# confidence board did not, because it was never meant to judge prices -- but
# anything that FEEDS a ticket has to reject a broken row.
MAX_TRUSTED_GAP = 0.15


def implied(american: int) -> float:
    return 1 / (1 + (american / 100 if american > 0 else 100 / abs(american)))


def risk_to_win(american: int, win: float = 60.0) -> str:
    """'risk 100 to win 60' phrasing, which is how the question was asked."""
    if american < 0:
        return f"risk {abs(american):.0f} to win 100"
    return f"risk 100 to win {american:.0f}"


def scan(league: str, min_chance: float, days: int = 8,
         only_books: List[str] = None, min_books: int = 2) -> List[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    out: List[dict] = []
    for market in ("h2h", "spreads", "totals"):
        payload = LS.fetch_live(league, market, "us")
        for game, kick, prices in LS.games_from_live(payload, market, min_books):
            try:
                k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if not (now < k <= end):
                continue
            fair = LS.fair_probs(prices)
            n_books = len(prices)
            # One row per (book, side), NOT one per side.
            #
            # Collapsing to the best price across books reads sensibly and is
            # wrong the moment the output is grouped by book: a team priced
            # better at BetMGM vanished from the FanDuel list entirely, even
            # though FanDuel was quoting it. Alabama was on FanDuel at -1800
            # the whole time and simply never appeared, because MGM had -1200.
            #
            # A per-book list has to be COMPLETE for that book, or a parlay
            # built from it is missing legs the reader can actually place.
            for book, sides in prices.items():
                if book in LS.IGNORE_BOOKS:
                    continue
                # A price at a book without your money in it is not a bet.
                if only_books and not any(b in book.lower() for b in only_books):
                    continue
                for side, price in sides.items():
                    p = fair.get(side)
                    if p is None or p < min_chance:
                        continue
                    if abs(p - implied(price)) > MAX_TRUSTED_GAP:
                        continue          # transposed or stale: not a bet
                    out.append({
                        "side": side, "price": price, "book": book, "game": game,
                        "chance": p, "needs": implied(price),
                        "gap": p - implied(price), "market": market, "kickoff": k,
                        "n_books": n_books,
                    })
    return sorted(out, key=lambda r: -r["chance"])


def report(rows: List[dict], min_chance: float) -> None:
    if not rows:
        print(f"Nothing on the board is {min_chance*100:.0f}% or better.")
        return
    print(f"\n{'bet':<34}{'price':>7}{'CHANCE':>8}{'NEEDS':>7}{'GAP':>7}  {'book':<12}books")
    for r in rows:
        # Fewer books behind a number means a shakier CHANCE, and that has to
        # be visible rather than implied by its absence.
        thin = " thin" if r.get("n_books", 9) < 4 else ""
        print(f"{r['side'][:33]:<34}{r['price']:>+7}{r['chance']*100:>7.1f}%"
              f"{r['needs']*100:>6.1f}%{r['gap']*100:>+6.1f}  {r['book']:<12}"
              f"{r.get('n_books','?'):>2}bk{thin}")
    good = [r for r in rows if r["gap"] > 0]
    print(f"\n{len(rows)} bet(s) at {min_chance*100:.0f}%+ confidence; "
          f"{len(good)} of them priced better than their chance.")
    if not good:
        print("None of the confident bets are priced short of their own odds — "
              "the confidence is already in the number.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=sorted(LS.SPORTS))
    ap.add_argument("--min-chance", type=float, default=0.625,
                    help="lowest win probability to show (default .625 = -167)")
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--books", default="", help="comma-separated, e.g. betmgm,fanduel")
    ap.add_argument("--build", action="store_true",
                    help="best ticket per book at each payout level")
    a = ap.parse_args()
    bk = [b.strip().lower() for b in a.books.split(",") if b.strip()] or None
    rows = scan(a.league, a.min_chance, a.days, bk)
    if not a.build:
        report(rows, a.min_chance)
        return 0
    per: Dict[str, List[dict]] = {}
    for r in rows:
        per.setdefault(r["book"], []).append(r)
    for book, book_rows in sorted(per.items(), key=lambda kv: -len(kv[1])):
        print(f"\n=== {book.upper()} — best ticket at each payout")
        print(f"{'pays':>8}{'legs':>6}{'CHANCE':>9}{'keep':>9}   legs")
        for f in frontier(book_rows):
            d = f["decimal"]
            amer = (d - 1) * 100 if d >= 2 else -100 / (d - 1)
            names = ", ".join(l["side"].split()[-1] for l in f["legs"])
            print(f"{amer:>+8.0f}{len(f['legs']):>6}{f['chance']*100:>8.1f}%"
                  f"{f['keep']:>9.4f}   {names[:44]}")
    return 0




# ------------------------------------------------------------- construction

def efficiency(chance: float, price: int) -> float:
    """
    Payout gained per unit of probability sacrificed.

        ln(decimal) / -ln(chance)

    This is the number that decides which legs belong on a ticket, and it is
    not the win rate. A leg multiplies the payout by its decimal odds and the
    chance by its probability, so what matters is the RATIO of what it adds to
    what it costs -- both of which compound, which is why logs.

    Above 1.0 the payout outruns the risk. Below it the leg costs more than it
    brings. Measured on the FanDuel board of 2026-09-13, the range is brutal:

        Virginia   -310   74.8%   0.963
        Alabama   -1800   89.3%   0.478
        Notre Dame -10000 96.0%   0.244

    So the most likely winner on the board is the worst possible leg. Notre
    Dame multiplies the payout by 1.01 and the chance by 0.96 -- four points of
    win probability surrendered to buy one point of payout. Sorting a parlay by
    win rate stacks exactly these.
    """
    if not 0 < chance < 1:
        return 0.0
    d = 1 + (price / 100 if price > 0 else 100 / abs(price))
    return math.log(d) / -math.log(chance)


def best_parlay(rows: List[dict], min_decimal: float = 2.0,
                max_legs: int = 6) -> Optional[dict]:
    """
    The highest-chance ticket that still pays at least `min_decimal`.

    Exhaustive over combinations up to max_legs, because the greedy answer is
    wrong and wrong by a lot. Targeting +100 on the 2026-09-13 FanDuel board:

        greedy by win rate .... 16 legs, 24.9%
        optimal ...............  2 legs, 46.4%

    Nearly double the win rate for the same payout. Greedy is only optimal for
    a FIXED leg count; the moment the target is a payout it collapses, because
    it spends probability on favourites that add almost no odds.

    One leg per game, one book -- callers must pass a single book's rows.
    """
    books = {r["book"] for r in rows}
    if len(books) > 1:
        raise ValueError(f"best_parlay got {len(books)} books: {sorted(books)}")
    pool = sorted(rows, key=lambda r: -efficiency(r["chance"], r["price"]))[:14]
    best = None
    for n in range(1, min(max_legs, len(pool)) + 1):
        for combo in itertools.combinations(pool, n):
            if len({c["game"] for c in combo}) != n:
                continue                       # one leg per game
            d = 1.0
            ch = 1.0
            keep = 1.0
            for c in combo:
                d *= 1 + (c["price"] / 100 if c["price"] > 0
                          else 100 / abs(c["price"]))
                ch *= c["chance"]
                keep *= c["chance"] / c["needs"]
            if d < min_decimal:
                continue
            if best is None or ch > best["chance"]:
                best = {"legs": list(combo), "chance": ch, "decimal": d,
                        "keep": keep, "profit_per_100": (d - 1) * 100}
    return best


def frontier(rows: List[dict], targets=(1.5, 2.0, 3.0, 5.0, 10.0),
             max_legs: int = 6) -> List[dict]:
    """
    Best achievable chance at each payout level.

    The honest shape of the trade-off: you cannot pick both, so this prints
    what each payout actually costs in win probability rather than implying
    some combination escapes it.
    """
    out = []
    for t in targets:
        p = best_parlay(rows, t, max_legs)
        if p:
            out.append(dict(p, target=t))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
