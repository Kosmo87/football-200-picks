"""
What happens if the +200 target goes away and selection follows confidence?

Builds every priced side for a season walk-forward (ratings only ever see
earlier games), then runs several selection rules over the same rows so the
comparison is like for like.

  python strategy_test.py --league NCAAF --season 2025
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures

from backtest import historical_odds
from elo import EloSystem, build_multi_season, config_for_league
from espn import current_season_year, fetch_completed_games
from history_data import all_fbs_ids, load_season, load_seasons
from odds import american_to_decimal, side_implied_prob
from staking import ladder_units, stake_units


def build_rows(league, season, is_current):
    years = list(range(season - 5, season))
    history = load_seasons(league, years)
    top = all_fbs_ids(years + [season]) if league == "NCAAF" else set()
    cfg = config_for_league(league)
    completed = fetch_completed_games(league) if is_current else load_season(league, season)
    completed.sort(key=lambda g: (g.date or "", g.event_id or ""))

    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        odds_list = list(ex.map(lambda g: historical_odds(league, g.event_id, "close"), completed))
    odds_by_event = {g.event_id: o for g, o in zip(completed, odds_list) if o}

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
                if price is None:
                    continue
                tid = g.home_id if side == "home" else g.away_id
                oid = g.away_id if side == "home" else g.home_id
                p = elo.win_prob_side(g.home_id, g.away_id, side, neutral=g.neutral)
                imp = side_implied_prob(price, odds.moneyline_home, odds.moneyline_away, side)
                rows.append({
                    "day": day, "p": p, "odds": price, "edge": (p - imp) * 100.0,
                    "sample": min(elo.effective_sample(tid), elo.effective_sample(oid)),
                    "won": (side == "home") == (g.home_score > g.away_score),
                })
    return rows, len(completed), len(odds_by_event)


def settle(units, odds, won):
    return units * (american_to_decimal(odds) - 1.0) if won else -units


def run(name, rows, keep, size):
    picked = [r for r in rows if keep(r)]
    staked = ret = 0.0
    w = l = 0
    for r in picked:
        u = size(r)
        if u <= 0:
            continue
        staked += u
        ret += settle(u, r["odds"], r["won"])
        w += r["won"]; l += not r["won"]
    roi = ret / staked * 100 if staked else 0.0
    avg = sum(r["odds"] for r in picked) / len(picked) if picked else 0
    print(f"  {name:<34} {len(picked):>4} {avg:>+7.0f} {staked:>7.1f}u {ret:>+8.2f}u "
          f"{roi:>+7.1f}%  {w:>3}-{l:<3}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--season", type=int, default=None)
    args = ap.parse_args()

    season = args.season or current_season_year()
    is_current = args.season is None
    min_sample = 3 if args.league == "NFL" else 4

    rows, n_games, n_odds = build_rows(args.league, season, is_current)
    print(f"\n=== {args.league} {season}: {n_odds} of {n_games} games had lines, "
          f"{len(rows)} priced sides ===\n")
    print(f"  {'strategy':<34} {'n':>4} {'avg px':>7} {'staked':>8} {'return':>9} "
          f"{'ROI':>8}  {'W-L':>7}")

    ok = lambda r: r["sample"] >= min_sample

    # What ships today: +EV underdogs long enough to clear +200.
    run("current: +EV, +200 target", rows,
        lambda r: ok(r) and r["edge"] >= 5 and 200 <= r["odds"] <= 600,
        lambda r: stake_units(r["p"], r["odds"]))

    # Drop the odds floor, keep the edge requirement.
    run("+EV, any price", rows,
        lambda r: ok(r) and r["edge"] >= 5 and -350 <= r["odds"] <= 600,
        lambda r: stake_units(r["p"], r["odds"]))

    # Confidence first: back what the model is most sure of, price be damned.
    for floor in (0.70, 0.80, 0.90):
        run(f"confidence only: model >= {floor*100:.0f}%", rows,
            lambda r, f=floor: ok(r) and r["p"] >= f,
            lambda r: ladder_units(r["p"]))

    # Confidence AND a price that pays for it.
    for floor in (0.70, 0.80):
        run(f"model >= {floor*100:.0f}% and +EV", rows,
            lambda r, f=floor: ok(r) and r["p"] >= f and r["edge"] >= 2,
            lambda r: stake_units(r["p"], r["odds"]))

    # The control worth beating: back every favourite the market names.
    run("every market favourite", rows,
        lambda r: ok(r) and r["odds"] < 0,
        lambda r: 1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
