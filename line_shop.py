"""
Find sportsbooks offering a better price than the rest of the market.

No forecast involved. If one book has a team at +150 while the rest of the
market has it at +125, taking +150 pays more on exactly the same outcome. The
only thing to establish is that the gap is real and available right now, which
is why this reads live prices rather than a historical file.

How fair value is decided
-------------------------
Every posted price includes the book's margin: both sides of a game add up to
more than 100%, and the surplus is the house's cut. Strip that out for each book
separately, then take the median across books. The median is deliberate -- one
book with a stale or mistaken number should not drag the estimate toward itself,
which is precisely the number we are trying to catch.

A book is flagged when its price implies a lower chance than that consensus, by
enough to clear the threshold.

  export ODDS_API_KEY=...
  python line_shop.py --sport NCAAF            # 1 credit
  python line_shop.py --demo                   # no key, no credits
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from statistics import median
from typing import Dict, List, Optional

import requests

from odds import american_to_decimal, american_to_implied_prob

BASE = "https://api.the-odds-api.com/v4"
SPORTS = {"NCAAF": "americanfootball_ncaaf", "NFL": "americanfootball_nfl"}

# Books that will not be flagged as offering value, because a lone outlier at a
# book nobody can bet is noise. Adjust to the accounts actually held.
IGNORE_BOOKS = {"betfair_ex_us", "matchbook"}

# Sanity limits, all learned from running the detector over historical prices.
#
# Longshots are the trap. At +3000 a side is priced near 3%, and a book quoting
# +11400 on the same team implies under 1% -- arithmetically a 268% "edge",
# practically a data error or a number nobody can actually bet. Percentage edge
# is a terrible unit at long odds, so the price range is limited to where it
# behaves.
MAX_PRICE = 600      # no longshot lottery tickets
MIN_PRICE = -400     # no laying four to one
MAX_EDGE = 0.15      # above this it is a mistake, not an opportunity
MIN_BOOKS = 5        # a consensus needs enough books to be one


def fair_probs(book_prices: Dict[str, Dict[str, int]]) -> Dict[str, float]:
    """
    Consensus no-vig probability per side.

    Each book is de-vigged on its own first. Averaging raw prices instead would
    fold every book's margin into the estimate and make everything look fair.
    """
    per_side: Dict[str, List[float]] = defaultdict(list)
    for prices in book_prices.values():
        if len(prices) < 2:
            continue
        raw = {s: american_to_implied_prob(p) for s, p in prices.items()}
        total = sum(raw.values())
        if total <= 0:
            continue
        for side, p in raw.items():
            per_side[side].append(p / total)
    return {s: median(v) for s, v in per_side.items() if v}


def find_value(
    book_prices: Dict[str, Dict[str, int]],
    min_edge: float,
    max_edge: float = MAX_EDGE,
    min_books: int = MIN_BOOKS,
) -> List[dict]:
    if len(book_prices) < min_books:
        return []
    fair = fair_probs(book_prices)
    if len(fair) < 2:
        return []
    out = []
    for book, prices in book_prices.items():
        if book in IGNORE_BOOKS:
            continue
        for side, price in prices.items():
            p = fair.get(side)
            if p is None:
                continue
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue
            dec = american_to_decimal(price)
            ev = p * (dec - 1) - (1 - p)
            if ev > max_edge:
                continue  # too good to be true, and therefore not true
            if ev >= min_edge:
                out.append({
                    "book": book, "side": side, "price": price,
                    "fair_prob": p, "edge": ev,
                    "consensus_price": _to_american(p),
                    "n_books": len(book_prices),
                })
    return sorted(out, key=lambda x: -x["edge"])


def _to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        return 0
    dec = 1 / p
    return int(round((dec - 1) * 100)) if dec >= 2 else int(round(-100 / (dec - 1)))


def fmt(o: int) -> str:
    return f"+{o}" if o > 0 else str(o)


# ---------------------------------------------------------------------------

def fetch_live(sport: str, market: str, regions: str) -> List[dict]:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("ODDS_API_KEY is not set. Get a free key at the-odds-api.com "
                 "and: export ODDS_API_KEY=...")
    r = requests.get(
        f"{BASE}/sports/{SPORTS[sport]}/odds",
        params={"apiKey": key, "regions": regions, "markets": market,
                "oddsFormat": "american"},
        timeout=40,
    )
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:200]}")
    print(f"  credits used {r.headers.get('x-requests-used','?')}, "
          f"remaining {r.headers.get('x-requests-remaining','?')}\n")
    return r.json()


def games_from_live(payload: List[dict], market: str):
    for ev in payload:
        prices: Dict[str, Dict[str, int]] = {}
        for bk in ev.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                if mk.get("key") != market:
                    continue
                sides = {}
                for oc in mk.get("outcomes") or []:
                    name = oc.get("name")
                    if market == "spreads":
                        name = f"{name} {oc.get('point'):+g}"
                    elif market == "totals":
                        name = f"{name} {oc.get('point'):g}"
                    sides[name] = int(oc.get("price"))
                if len(sides) >= 2:
                    prices[bk.get("key")] = sides
        if len(prices) >= 3:
            yield f"{ev.get('away_team')} @ {ev.get('home_team')}", ev.get("commence_time"), prices


def report(rows, min_edge, stake):
    if not rows:
        print("  Nothing out of line right now. Every book agrees within the threshold.")
        return
    print(f"  {'GAME':<40}{'BET':<26}{'BOOK':<16}{'PRICE':>8}{'MARKET':>8}{'EDGE':>8}")
    print(f"  {'-'*104}")
    for g, when, o in rows:
        gain = stake * o["edge"]
        print(f"  {g[:39]:<40}{o['side'][:25]:<26}{o['book'][:15]:<16}"
              f"{fmt(o['price']):>8}{fmt(o['consensus_price']):>8}{o['edge']*100:>7.1f}%")
        print(f"  {'':<40}    everyone else says {fmt(o['consensus_price'])} · "
              f"{o['n_books']} books priced it · "
              f"${stake:.0f} here is worth about ${gain:+.2f} more than fair")
    print(f"\n  {len(rows)} priced better than the market by {min_edge*100:.0f}%+")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", default="NCAAF", choices=list(SPORTS))
    ap.add_argument("--market", default="h2h", choices=("h2h", "spreads", "totals"))
    ap.add_argument("--regions", default="us")
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--max-edge", type=float, default=MAX_EDGE,
                    help="ignore anything above this; it is an error, not an edge")
    ap.add_argument("--min-books", type=int, default=MIN_BOOKS)
    ap.add_argument("--stake", type=float, default=100.0)
    ap.add_argument("--demo", action="store_true",
                    help="run the same maths over the historical file; no key needed")
    args = ap.parse_args()

    if args.demo:
        return demo(args)

    print(f"\n{args.sport} {args.market}, {args.regions} books — 1 credit")
    payload = fetch_live(args.sport, args.market, args.regions)
    rows = []
    for game, when, prices in games_from_live(payload, args.market):
        for o in find_value(prices, args.min_edge, args.max_edge, args.min_books):
            rows.append((game, when, o))
    rows.sort(key=lambda r: -r[2]["edge"])
    report(rows, args.min_edge, args.stake)
    return 0


def demo(args) -> int:
    """Same detector, run over the historical multi-book file."""
    from cfb_lines import read_rows, solve_abbr_map
    print("\nDemo mode: running the detector over historical multi-book prices.")
    print("No key used, no credits spent.\n")

    rows = read_rows()
    abbr_map = solve_abbr_map(rows)
    id_to_abbr = {tid: a for a, tid in abbr_map.items()}
    by_game = defaultdict(lambda: defaultdict(dict))
    desc = {}
    for r in rows:
        if r.get("market_type") != "money_line":
            continue
        try:
            season = int(float(r.get("season") or 0))
            odds = int(float(r["odds"]))
        except (TypeError, ValueError):
            continue
        if season < 2015:
            continue
        team = abbr_map.get((r.get("abbr") or "").strip())
        if not team:
            continue
        by_game[r["game_id"]][r["book"]][team] = odds
        desc[r["game_id"]] = r.get("game_desc", "")

    found = []
    for gid, books in by_game.items():
        usable = {b: s for b, s in books.items() if len(s) == 2}
        if len(usable) < 5:
            continue
        for o in find_value(usable, args.min_edge, args.max_edge, args.min_books):
            o["side"] = id_to_abbr.get(o["side"], o["side"])
            found.append((desc.get(gid, gid)[:39], None, o))
    found.sort(key=lambda r: -r[2]["edge"])

    print(f"  {len(by_game):,} games, {len(found):,} prices beat the consensus "
          f"by {args.min_edge*100:.0f}%+\n")
    report(found[:12], args.min_edge, args.stake)
    print("""
  Historical prices are not timestamped together, so some of these are stale
  rather than real. That is exactly what live mode fixes: every price in one
  response was quoted at the same moment.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
