"""
Does confidence-weighted staking help or hurt?

Walks the season game by game (ratings only ever see earlier games), keeps every
leg that passes the baseline gates at its recorded pregame price, then settles it
under three staking rules:

  flat    1 unit on everything — the control
  ladder  units from model win probability, the capper scheme
  kelly   units from edge and price together, quarter-Kelly scaled to 5U

  python units_test.py --league NCAAF
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
from typing import List, Optional

from backtest import historical_odds
from elo import EloSystem, build_multi_season, config_for_league
from espn import current_season_year, fetch_completed_games
from history_data import all_fbs_ids, load_seasons
from odds import american_to_decimal, side_implied_prob
from picks import compute_confidence

# The capper ladder: units from the model's win probability alone.
LADDER = [(0.60, 0.5), (0.70, 1.0), (0.80, 2.0), (0.90, 3.0), (0.95, 4.0), (1.01, 5.0)]

# Baseline gates, same as the live ledger.
MIN_EDGE_PP = 5.0
MIN_SAMPLE = {"NFL": 3, "NCAAF": 4}
MAX_LEG_ODDS = 600
MIN_LEG_ODDS = -350


def ladder_units(p: float) -> float:
    for ceiling, units in LADDER:
        if p < ceiling:
            return units
    return 5.0


def kelly_units(p: float, odds: int, fraction: float = 0.25, cap: float = 5.0) -> float:
    """
    Quarter-Kelly, expressed in the same 0.5–5U vocabulary.

    f* = (bp - q) / b, where b is decimal odds minus 1. Unlike the ladder this
    cannot recommend a stake on a price that offers no edge, because a fair or
    short price drives f* to zero or below regardless of how likely the win is.
    """
    b = american_to_decimal(odds) - 1.0
    if b <= 0:
        return 0.0
    f = (b * p - (1.0 - p)) / b
    if f <= 0:
        return 0.0
    return min(round(f * fraction * 20, 1), cap)


def settle(units: float, odds: int, won: bool) -> float:
    if units <= 0:
        return 0.0
    return units * (american_to_decimal(odds) - 1.0) if won else -units


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--season", type=int, default=None,
                    help="evaluate a finished season instead of the current one")
    args = ap.parse_args()
    league = args.league

    season = args.season or current_season_year()
    years = list(range(season - 5, season))
    history = load_seasons(league, years)
    top = all_fbs_ids(years + [season]) if league == "NCAAF" else set()
    cfg = config_for_league(league)

    if args.season:
        from history_data import load_season
        completed = load_season(league, args.season)
    else:
        completed = fetch_completed_games(league)
    completed.sort(key=lambda g: (g.date or "", g.event_id or ""))
    print(f"=== Unit sizing: {league} ===\n{len(completed)} finished games\n")

    print("Fetching pregame lines…")
    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        odds_list = list(ex.map(lambda g: historical_odds(league, g.event_id, "close"), completed))
    odds_by_event = {g.event_id: o for g, o in zip(completed, odds_list) if o}
    print(f"  {len(odds_by_event)} usable\n")

    base = build_multi_season(history, league=league, config=cfg, top_division=top)
    rows = []

    for day in sorted({g.date[:10] for g in completed if g.date}):
        training = [g for g in completed if g.date and g.date[:10] < day]
        elo = EloSystem(config=cfg, top_division=top).seed_from(base).build(training)

        for g in [x for x in completed if x.date and x.date[:10] == day]:
            odds = odds_by_event.get(g.event_id)
            if not odds or g.home_score == g.away_score:
                continue
            for side in ("home", "away"):
                price = odds.moneyline_home if side == "home" else odds.moneyline_away
                if price is None or not (MIN_LEG_ODDS <= price <= MAX_LEG_ODDS):
                    continue
                team_id = g.home_id if side == "home" else g.away_id
                opp_id = g.away_id if side == "home" else g.home_id
                p = elo.win_prob_side(g.home_id, g.away_id, side, neutral=g.neutral)
                implied = side_implied_prob(
                    price, odds.moneyline_home, odds.moneyline_away, side
                )
                edge_pp = (p - implied) * 100.0
                sample = min(elo.effective_sample(team_id), elo.effective_sample(opp_id))
                if edge_pp < MIN_EDGE_PP or sample < MIN_SAMPLE[league]:
                    continue
                home_won = g.home_score > g.away_score
                won = (side == "home") == home_won
                rows.append({
                    "p": p, "odds": price, "edge": edge_pp, "won": won,
                    "conf": compute_confidence(p - implied, price, sample)[0],
                })

    if not rows:
        print("No qualifying legs.")
        return 0

    print(f"{len(rows)} qualifying legs across the season\n")
    schemes = {
        "flat 1U": lambda r: 1.0,
        "ladder (win%)": lambda r: ladder_units(r["p"]),
        "quarter-Kelly": lambda r: kelly_units(r["p"], r["odds"]),
        # The ladder is the vocabulary people know, so keep it -- but never
        # stake more than the price justifies. A 75% shot at -300 is a coin the
        # market has already priced; the ladder alone would put 2U on it.
        "ladder, Kelly-capped": lambda r: min(
            ladder_units(r["p"]), kelly_units(r["p"], r["odds"])
        ),
    }

    print(f"  {'scheme':<16} {'staked':>8} {'return':>9} {'ROI':>8}  {'W-L':>7}")
    for name, size in schemes.items():
        staked = ret = 0.0
        w = l = 0
        for r in rows:
            u = size(r)
            if u <= 0:
                continue
            staked += u
            ret += settle(u, r["odds"], r["won"])
            w += r["won"]
            l += not r["won"]
        roi = ret / staked * 100 if staked else 0.0
        print(f"  {name:<16} {staked:>8.1f}u {ret:>+8.2f}u {roi:>+7.1f}%  {w:>3}-{l:<3}")

    print("\n  Where the ladder puts its money:")
    print(f"  {'units':>6} {'n':>4} {'avg odds':>9} {'won':>5} {'return':>9}")
    for _, u in LADDER:
        sub = [r for r in rows if ladder_units(r["p"]) == u]
        if not sub:
            continue
        ret = sum(settle(u, r["odds"], r["won"]) for r in sub)
        avg_odds = sum(r["odds"] for r in sub) / len(sub)
        print(f"  {u:>5}U {len(sub):>4} {avg_odds:>+9.0f} {sum(r['won'] for r in sub):>5} {ret:>+8.2f}u")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
