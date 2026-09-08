"""
Does taking the best available price beat the market, without predicting anything?

The premise is not that we know better than the books. It is that the books do
not agree with each other. Pinnacle prices tightly and is treated here as fair
value: strip its margin out and you have the best available estimate of the true
chance. Any other book offering a better price than that estimate is, by
definition, offering value -- no forecast required.

This settles whether that survives contact with real prices and real results
before any tool gets built on it.

  python shop_test.py --seasons 2021 2022 2023 2024 2025
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Dict, List

from cfb_lines import read_rows, solve_abbr_map
from history_data import load_season
from odds import american_to_decimal, de_vig_probs

SHARP = "PINNACLE"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+",
                    default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="only bet when edge over fair value exceeds this (e.g. 0.02)")
    args = ap.parse_args()

    # Outcomes, joined by ESPN event id -- the same id the line file uses.
    results = {}
    for yr in args.seasons:
        for g in load_season("NCAAF", yr):
            results[g.event_id] = g
    print(f"{len(results):,} games with final scores\n")

    rows = read_rows()
    abbr_map = solve_abbr_map(rows)

    ml: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if r.get("market_type") != "money_line":
            continue
        gid = str(r.get("game_id") or "")
        if gid not in results:
            continue
        try:
            odds = float(r["odds"])
        except (TypeError, ValueError):
            continue
        team = abbr_map.get((r.get("abbr") or "").strip())
        if team:
            ml[gid][r["book"]][team] = odds

    bets: List[dict] = []
    games_used = 0
    for gid, books in ml.items():
        g = results[gid]
        sharp = books.get(SHARP)
        if not sharp or len(sharp) != 2:
            continue
        if g.home_id not in sharp or g.away_id not in sharp:
            continue
        p_home, p_away = de_vig_probs(int(sharp[g.home_id]), int(sharp[g.away_id]))
        if p_home is None:
            continue
        games_used += 1
        fair = {g.home_id: p_home, g.away_id: p_away}
        home_won = g.home_score > g.away_score

        for book, sides in books.items():
            if book == SHARP:
                continue
            for team, price in sides.items():
                if team not in fair:
                    continue
                dec = american_to_decimal(int(price))
                p = fair[team]
                ev = p * (dec - 1) - (1 - p)
                if ev <= args.min_ev:
                    continue
                won = (team == g.home_id) == home_won
                bets.append({
                    "book": book, "ev": ev, "price": int(price),
                    "ret": (dec - 1) if won else -1.0, "won": won,
                })

    if not bets:
        print("no qualifying bets")
        return 1

    total = sum(b["ret"] for b in bets)
    won = sum(b["won"] for b in bets)
    print(f"{games_used:,} games priced by Pinnacle and at least one other book")
    print(f"{len(bets):,} bets where another book beat Pinnacle's fair price\n")
    print(f"  record   {won}-{len(bets)-won}  ({won/len(bets)*100:.1f}%)")
    print(f"  return   {total:+.1f} units on {len(bets):,} staked")
    print(f"  ROI      {total/len(bets)*100:+.2f}%")
    print(f"  expected {sum(b['ev'] for b in bets)/len(bets)*100:+.2f}%  "
          f"(what the edge said it should be)")

    print(f"\n  By size of the edge:")
    print(f"  {'edge':>12} {'bets':>7} {'ROI':>9} {'expected':>10}")
    for lo, hi in [(0, .01), (.01, .02), (.02, .04), (.04, .08), (.08, 1)]:
        sub = [b for b in bets if lo <= b["ev"] < hi]
        if len(sub) < 50:
            continue
        r = sum(b["ret"] for b in sub) / len(sub) * 100
        e = sum(b["ev"] for b in sub) / len(sub) * 100
        print(f"  {lo*100:>4.0f}-{hi*100:<5.0f}% {len(sub):>7} {r:>+8.2f}% {e:>+9.2f}%")

    print(f"\n  Best books to have an account with:")
    by_book: Dict[str, List[dict]] = defaultdict(list)
    for b in bets:
        by_book[b["book"]].append(b)
    ranked = sorted(by_book.items(), key=lambda kv: -len(kv[1]))[:8]
    print(f"  {'book':<30} {'bets':>7} {'ROI':>9}")
    for book, bs in ranked:
        print(f"  {book:<30} {len(bs):>7} "
              f"{sum(x['ret'] for x in bs)/len(bs)*100:>+8.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
