"""
Walk-forward calibration: is the model's win probability trustworthy?

For every finished game with a recorded pregame line, rate both teams using ONLY
games that finished before that game's kickoff, then compare what the model said
would happen to what did. The market's de-vigged probability is scored the same
way as a control — a well-calibrated reference should land near the diagonal.

  python calibrate.py --league NCAAF
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from backtest import historical_odds
from elo import EloSystem, build_multi_season, config_for_league
from espn import current_season_year, fetch_completed_games
from history_data import all_fbs_ids, load_seasons
from odds import side_implied_prob

BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
           (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]


def bucket_of(p: float):
    pct = p * 100.0
    for lo, hi in BUCKETS:
        if lo <= pct < hi:
            return (lo, hi)
    return (90, 100)


def brier(pairs) -> float:
    """Mean squared error of probabilistic forecasts. Lower is better."""
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else float("nan")


def report(name: str, pairs, out):
    by_bucket: Dict = defaultdict(list)
    for p, outcome in pairs:
        by_bucket[bucket_of(p)].append((p, outcome))

    out.append(f"\n  {name} (n={len(pairs)}, Brier {brier(pairs):.4f})")
    out.append(f"  {'predicted':>12}  {'n':>4}  {'said':>6}  {'actual':>7}  {'gap':>7}")
    for b in BUCKETS:
        rows = by_bucket.get(b)
        if not rows:
            continue
        said = sum(p for p, _ in rows) / len(rows) * 100
        actual = sum(o for _, o in rows) / len(rows) * 100
        gap = actual - said
        flag = "  <-- overrates" if gap < -12 else ("  <-- underrates" if gap > 12 else "")
        out.append(
            f"  {b[0]:>5}-{b[1]:<3}%  {len(rows):>4}  {said:>5.1f}%  "
            f"{actual:>6.1f}%  {gap:>+6.1f}pp{flag}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--price", default="close", choices=("close", "open"))
    args = ap.parse_args()
    league = args.league

    print(f"=== Walk-forward calibration: {league} ===\n")
    season = current_season_year()
    years = list(range(season - 5, season))
    history = load_seasons(league, years)
    top = all_fbs_ids(years + [season]) if league == "NCAAF" else set()
    completed = fetch_completed_games(league)
    completed.sort(key=lambda g: (g.date or "", g.event_id or ""))
    hist_n = sum(len(v) for v in history.values())
    print(f"{len(completed)} finished games this season, "
          f"{hist_n} across {years[0]}-{years[-1]}")
    print(f"top division: {len(top) or 'n/a'} teams\n")

    print("Fetching pregame lines…")
    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        odds_list = list(
            ex.map(lambda g: historical_odds(league, g.event_id, args.price), completed)
        )
    odds_by_event = {
        g.event_id: o for g, o in zip(completed, odds_list) if o is not None
    }
    print(f"  {len(odds_by_event)} of {len(completed)} games have usable moneylines\n")

    # Walk forward: rebuild ratings from scratch through each date boundary so no
    # game is ever rated using its own result or anything later.
    cfg = config_for_league(league)
    model_pairs, market_pairs, edge_pairs = [], [], []
    seen_dates = sorted({g.date[:10] for g in completed if g.date})

    # Seed once from the chained history, then re-apply only this season's
    # earlier games for each date boundary.
    base = build_multi_season(history, league=league, config=cfg, top_division=top)

    for day in seen_dates:
        training = [g for g in completed if g.date and g.date[:10] < day]
        elo = EloSystem(config=cfg, top_division=top).seed_from(base)
        elo.build(training)

        for g in [x for x in completed if x.date and x.date[:10] == day]:
            odds = odds_by_event.get(g.event_id)
            if odds is None or g.home_score == g.away_score:
                continue
            home_won = 1.0 if g.home_score > g.away_score else 0.0
            p_model = elo.win_prob_home(g.home_id, g.away_id, neutral=g.neutral)
            p_market = side_implied_prob(
                odds.moneyline_home, odds.moneyline_home, odds.moneyline_away, "home"
            )
            model_pairs.append((p_model, home_won))
            market_pairs.append((p_market, home_won))
            edge_pairs.append((p_model - p_market, home_won, p_model, p_market))

    out: List[str] = []
    report("MODEL (Elo)", model_pairs, out)
    report("MARKET (de-vigged)", market_pairs, out)
    print("\n".join(out))

    print("\n=== Does a bigger claimed edge pay better? ===")
    print(f"  {'model edge':>14}  {'n':>4}  {'model said':>11}  {'actual':>7}")
    bands = [(-100, 0), (0, 5), (5, 10), (10, 20), (20, 100)]
    for lo, hi in bands:
        rows = [r for r in edge_pairs if lo <= (r[0] * 100) < hi]
        if not rows:
            continue
        said = sum(r[2] for r in rows) / len(rows) * 100
        actual = sum(r[1] for r in rows) / len(rows) * 100
        print(f"  {lo:>+5}..{hi:<+4}pp  {len(rows):>4}  {said:>10.1f}%  {actual:>6.1f}%")

    bm, bk = brier(model_pairs), brier(market_pairs)
    print(f"\n  Brier — model {bm:.4f} vs market {bk:.4f}")
    print(f"  {'Model beats' if bm < bk else 'Market beats'} the other by {abs(bm - bk):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
