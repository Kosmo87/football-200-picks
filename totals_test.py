"""
Can the efficiency model predict how many points get scored?

Walk-forward over a season: every game is predicted from box scores that closed
before it, then scored against what actually happened. Two questions, in order,
because the second is meaningless without the first.

  1. Does it beat the naive baselines at predicting the total at all?
  2. On games where a closing total survives, does it beat the market?

  python totals_test.py --league NCAAF --season 2025
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
from collections import defaultdict
from statistics import mean
from typing import Dict, List

from boxscores import load_boxscores
from efficiency import build_ratings
from espn import HEADERS, fetch_scoreboard
from history_data import load_season

SUMMARY = ("https://site.web.api.espn.com/apis/site/v2/sports/football/"
           "college-football/summary?event=")


def market_lines(event_id: str):
    """Closing total and spread, on games that still carry them. Spread is
    ESPN's home-team line, so -7 means the home side laid seven."""
    try:
        data = fetch_scoreboard(SUMMARY + event_id, retries=1)
    except Exception:
        return (None, None)
    total = spread = None
    for o in data.get("pickcenter") or []:
        if total is None and o.get("overUnder") is not None:
            try:
                total = float(o["overUnder"])
            except (TypeError, ValueError):
                pass
        if spread is None and o.get("spread") is not None:
            try:
                spread = float(o["spread"])
            except (TypeError, ValueError):
                pass
    return (total, spread)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF")
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--prior", type=int, default=1, help="prior seasons to seed with")
    args = ap.parse_args()

    rows = load_boxscores(args.league, args.season)
    prior_rows = []
    for yr in range(args.season - args.prior, args.season):
        try:
            prior_rows.extend(load_boxscores(args.league, yr))
        except Exception:
            pass

    # One row per game (home perspective), in date order.
    by_event: Dict[str, List] = defaultdict(list)
    for r in rows:
        by_event[r.event_id].append(r)
    games = []
    for eid, pair in by_event.items():
        if len(pair) != 2:
            continue
        home = next((p for p in pair if p.is_home), pair[0])
        away = next((p for p in pair if p is not home), pair[1])
        games.append((home.date, eid, home, away))
    games.sort()
    print(f"\n=== {args.league} {args.season}: {len(games)} games, "
          f"{len(prior_rows)//2} prior-season games ===\n")

    preds = []
    for i, (date, eid, home, away) in enumerate(games):
        train = prior_rows + [r for r in rows if r.date and r.date < date]
        if len(train) < 200:
            continue
        rt = build_ratings(train)
        actual = home.points + away.points
        pred = rt.expected_total(home.team_id, away.team_id, home.neutral)
        pred_margin = rt.expected_margin(home.team_id, away.team_id, home.neutral)
        preds.append({
            "eid": eid, "actual": actual, "pred": pred,
            "lg": rt.league_ppg * 2,
            "actual_margin": home.points - away.points, "pred_margin": pred_margin,
            "n_home": rt.played(home.team_id), "n_away": rt.played(away.team_id),
        })

    if not preds:
        print("not enough data")
        return 1

    mae = lambda f: mean(abs(f(p)) for p in preds)
    print(f"Predicting the total, {len(preds)} games (walk-forward)")
    print(f"  {'method':<34} {'MAE':>7} {'RMSE':>7}")
    rmse = lambda f: (mean(f(p) ** 2 for p in preds)) ** 0.5
    for name, fn in [
        ("league average total", lambda p: p["lg"] - p["actual"]),
        ("efficiency model", lambda p: p["pred"] - p["actual"]),
    ]:
        print(f"  {name:<34} {mae(fn):>7.2f} {rmse(fn):>7.2f}")

    print(f"\nPredicting the margin, same games")
    print(f"  {'method':<34} {'MAE':>7}")
    print(f"  {'always pick the home team by 0':<34} "
          f"{mean(abs(p['actual_margin']) for p in preds):>7.2f}")
    print(f"  {'efficiency model':<34} "
          f"{mean(abs(p['pred_margin'] - p['actual_margin']) for p in preds):>7.2f}")

    # Only now, the market — on whatever games still carry a closing number.
    print(f"\nFetching closing lines…")
    with futures.ThreadPoolExecutor(max_workers=10) as ex:
        mkts = list(ex.map(lambda p: market_lines(p["eid"]), preds))
    paired = [(p, m[0]) for p, m in zip(preds, mkts) if m[0]]
    spread_pairs = [(p, m[1]) for p, m in zip(preds, mkts) if m[1] is not None]
    print(f"  {len(paired)} totals, {len(spread_pairs)} spreads\n")
    if not paired:
        return 0

    print(f"Model vs market on those {len(paired)} games")
    print(f"  {'method':<34} {'MAE':>7}")
    print(f"  {'market closing total':<34} "
          f"{mean(abs(m - p['actual']) for p, m in paired):>7.2f}")
    print(f"  {'efficiency model':<34} "
          f"{mean(abs(p['pred'] - p['actual']) for p, m in paired):>7.2f}")

    # The market's spread is stated from the home side and negative when the
    # home team is favoured, so the margin it implies is its negation.
    if spread_pairs:
        print(f"\nModel vs market on the MARGIN, {len(spread_pairs)} games")
        print(f"  {'method':<34} {'MAE':>7}")
        print(f"  {'market closing spread':<34} "
              f"{mean(abs(-s - p['actual_margin']) for p, s in spread_pairs):>7.2f}")
        print(f"  {'efficiency model':<34} "
              f"{mean(abs(p['pred_margin'] - p['actual_margin']) for p, s in spread_pairs):>7.2f}")

        print(f"\n  Against the spread where the model disagrees (flat 1u, -110):")
        print(f"  {'disagreement':>14} {'n':>4} {'won':>5} {'ROI':>8}")
        for lo, hi in [(0, 3), (3, 6), (6, 10), (10, 100)]:
            sub = [(p, s) for p, s in spread_pairs if lo <= abs(p["pred_margin"] - (-s)) < hi]
            if len(sub) < 3:
                continue
            w = 0
            for p, s in sub:
                implied = -s
                # Back the home side when we make it better than the line says.
                took_home = p["pred_margin"] > implied
                covered = p["actual_margin"] > implied
                w += int(took_home == covered)
            n = len(sub)
            roi = (w * (100 / 110) - (n - w)) / n * 100
            print(f"  {lo:>6}-{hi:<3} pts {n:>4} {w:>5} {roi:>+7.1f}%")

    print(f"\n  Betting the side the model disagrees with on the TOTAL (flat 1u, -110):")
    print(f"  {'disagreement':>14} {'n':>4} {'won':>5} {'ROI':>8}")
    for lo, hi in [(0, 3), (3, 6), (6, 10), (10, 100)]:
        sub = [(p, m) for p, m in paired if lo <= abs(p["pred"] - m) < hi]
        if len(sub) < 3:
            continue
        w = sum(
            1 for p, m in sub
            if (p["pred"] > m and p["actual"] > m) or (p["pred"] < m and p["actual"] < m)
        )
        n = len(sub)
        roi = (w * (100 / 110) - (n - w)) / n * 100
        print(f"  {lo:>6}-{hi:<3} pts {n:>4} {w:>5} {roi:>+7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
