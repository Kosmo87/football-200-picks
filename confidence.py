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
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import keys  # noqa: F401
import line_shop as LS


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
    a = ap.parse_args()
    bk = [b.strip().lower() for b in a.books.split(",") if b.strip()] or None
    report(scan(a.league, a.min_chance, a.days, bk), a.min_chance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
