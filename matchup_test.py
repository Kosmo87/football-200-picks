"""
Does the matchup model beat the closing line? Walk-forward, on a real holdout.

The standard this project holds a model to, and the one every previous attempt
has failed: not "does it predict games" but "does it predict them better than
the price already does". A model can be a much better forecaster than a coin
and still be unbettable, because the market is not a coin.

Method, in the order it matters:

  1. Ratings for a game use only plays that finished before its kickoff.
     Walk-forward by construction -- leakage here is the easiest way to build
     something that looks brilliant and cannot bet.

  2. Coefficients are fitted on TRAINING seasons and never refitted on the test
     seasons. Fitting on everything and reporting on everything is how a
     backtest flatters itself.

  3. The comparison is against the CLOSING spread from nflverse, which is the
     hardest number in football to beat and the one actually available.

  4. The bar for betting is 52.4% against the spread at -110, not 50%.

Home-field advantage is fitted here rather than taken from the play-by-play
regression, because per-play EPA does not contain it: home offences average
0.009 EPA/play LESS than away offences, since home teams lead more often and
leading teams run more. Home advantage is real and lives in the scoreboard; it
does not show up in efficiency.

  python matchup_test.py
  python matchup_test.py --prior-weights 0 0.25 0.5 1.0
"""

from __future__ import annotations

import argparse
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import matchup
import pbp_data
from nflverse_data import load_games

TRAIN = list(range(2017, 2023))
TEST = list(range(2023, 2026))
PUSH_EPS = 1e-9


def build_table(plays: pd.DataFrame, seasons: List[int], prior_weight: float,
                ridge: float) -> pd.DataFrame:
    """One row per game: matchup features, closing spread, actual margin."""
    games = [g for g in load_games() if g.season in seasons and g.spread_line is not None]
    games = [g for g in games if g.home_score is not None and g.week is not None]
    by_week: Dict[tuple, List] = {}
    for g in games:
        by_week.setdefault((g.season, g.week), []).append(g)

    rows = []
    for (season, week), wk in sorted(by_week.items()):
        r = matchup.rate(plays, season, week, prior_weight=prior_weight, ridge=ridge)
        if not r.offence:
            continue
        for g in wk:
            f = matchup.matchup_features(r, g.home_team, g.away_team)
            rows.append({
                "season": season, "week": week,
                "home": g.home_team, "away": g.away_team,
                "margin": g.home_score - g.away_score,
                "spread": g.spread_line,          # positive = home favoured
                **{c: f[c] for c in matchup.FEATURE_COLS},
            })
    return pd.DataFrame(rows)


def fit(train: pd.DataFrame) -> np.ndarray:
    """Least squares: actual margin from the matchup features plus an intercept.

    The intercept IS home-field advantage -- every row predicts the home margin,
    so the constant is what the home team gets for being home."""
    X = np.column_stack([np.ones(len(train))] + [train[c].to_numpy() for c in matchup.FEATURE_COLS])
    y = train["margin"].to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def predict(df: pd.DataFrame, beta: np.ndarray) -> np.ndarray:
    X = np.column_stack([np.ones(len(df))] + [df[c].to_numpy() for c in matchup.FEATURE_COLS])
    return X @ beta


def evaluate(test: pd.DataFrame, beta: np.ndarray, thresholds=(0.0, 1.0, 2.0, 3.0)) -> None:
    pred = predict(test, beta)
    # spread_line is positive when the home team is favoured, and so is a
    # positive margin, so the market's own margin forecast is the spread itself.
    market = test["spread"].to_numpy(dtype=float)
    actual = test["margin"].to_numpy(dtype=float)

    print(f"\n  Predicting the margin ({len(test)} games, {test.season.min()}-{test.season.max()})")
    print(f"    {'model mean abs error':<28}{np.abs(pred - actual).mean():.3f}")
    print(f"    {'market mean abs error':<28}{np.abs(market - actual).mean():.3f}")
    better = np.abs(pred - actual).mean() < np.abs(market - actual).mean()
    print(f"    -> the {'MODEL' if better else 'market'} is the better forecaster")

    print(f"\n  Betting against the closing spread")
    print(f"    {'disagree by':>12}{'bets':>7}{'won':>6}{'lost':>6}{'push':>6}{'ATS%':>8}{'vs 52.4%':>10}")
    for t in thresholds:
        edge = pred - market                     # positive: model likes the home side
        take = np.abs(edge) > t
        if take.sum() == 0:
            continue
        side = np.sign(edge[take])               # +1 back home, -1 back away
        cover = np.sign(actual[take] - market[take])
        won = int(((side == cover) & (cover != 0)).sum())
        lost = int(((side != cover) & (cover != 0)).sum())
        push = int((cover == 0).sum())
        n = won + lost
        pct = won / n * 100 if n else 0.0
        print(f"    {t:>11.1f}{int(take.sum()):>7}{won:>6}{lost:>6}{push:>6}"
              f"{pct:>7.1f}%{pct - 52.38:>+9.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prior-weights", nargs="*", type=float, default=[0.5])
    ap.add_argument("--ridge", type=float, default=matchup.DEFAULT_RIDGE)
    args = ap.parse_args()

    plays = pbp_data.load(range(2016, 2026))
    if plays.empty:
        print("no play-by-play cached; run pbp_data.py first")
        return 1

    for pw in args.prior_weights:
        print("=" * 74)
        print(f"prior-season weight {pw}   ridge {args.ridge:g}")
        print("=" * 74)
        table = build_table(plays, TRAIN + TEST, pw, args.ridge)
        train = table[table.season.isin(TRAIN)]
        test = table[table.season.isin(TEST)]
        if train.empty or test.empty:
            print("  not enough data")
            continue
        beta = fit(train)
        print(f"  fitted on {len(train)} games {min(TRAIN)}-{max(TRAIN)}, "
              f"held out {len(test)} games {min(TEST)}-{max(TEST)}")
        print(f"    home-field advantage {beta[0]:+.2f} points")
        for c, b in zip(matchup.FEATURE_COLS, beta[1:]):
            print(f"    {c:<20}{b:+.2f} points per EPA/play of edge")
        evaluate(test, beta)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
