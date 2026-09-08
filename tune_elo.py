"""
Fit rating parameters to history instead of guessing them.

Walks every season chronologically, predicting each game from ratings that only
know about earlier games, then scores those predictions with log-loss (which
punishes confident mistakes harder than Brier does). Coordinate descent over the
parameters that actually control calibration.

  python tune_elo.py --league NCAAF
"""

from __future__ import annotations

import argparse
import math
from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple

from elo import CompletedGame, EloConfig, EloSystem, config_for_league, mov_multiplier
from history_data import all_fbs_ids, load_seasons

EPS = 1e-9


def evaluate(
    seasons: Dict[int, List[CompletedGame]],
    cfg: EloConfig,
    top_division: Set[str],
    score_from: int,
    use_mov: bool = True,
) -> Tuple[float, float, int]:
    """Walk-forward log-loss and Brier. Predictions never see their own game."""
    system: Optional[EloSystem] = None
    ll = brier = 0.0
    n = 0

    for season in sorted(seasons):
        nxt = EloSystem(config=cfg, top_division=top_division)
        if system is not None:
            nxt.seed_from(system)
        system = nxt

        games = sorted(seasons[season], key=lambda g: (g.date or "", g.event_id or ""))
        for g in games:
            system.ensure_team(g.home_id, g.home_name, g.home_abbr)
            system.ensure_team(g.away_id, g.away_name, g.away_abbr)
            if g.home_score == g.away_score:
                continue

            p = system.win_prob_home(g.home_id, g.away_id, neutral=g.neutral)
            if season >= score_from:
                outcome = 1.0 if g.home_score > g.away_score else 0.0
                p_c = min(max(p, EPS), 1 - EPS)
                ll -= outcome * math.log(p_c) + (1 - outcome) * math.log(1 - p_c)
                brier += (p - outcome) ** 2
                n += 1

            # Update (mirrors EloSystem.update_game, with MOV optional)
            hfa = 0.0 if g.neutral else cfg.home_field
            home_r = system.rating(g.home_id) + hfa
            away_r = system.rating(g.away_id)
            exp_home = EloSystem.expected_score(home_r, away_r, cfg.scale)
            margin = g.home_score - g.away_score
            if margin > 0:
                s_home, diff_w = 1.0, home_r - away_r
            else:
                s_home, diff_w = 0.0, away_r - home_r
            k = cfg.k_factor * (cfg.preseason_k_mult if g.season_type == 1 else 1.0)
            mult = mov_multiplier(margin, diff_w) if use_mov else 1.0
            delta = k * mult * (s_home - exp_home)
            system.ratings[g.home_id] = system.rating(g.home_id) + delta
            system.ratings[g.away_id] = system.rating(g.away_id) - delta
            system.games_played[g.home_id] = system.games_played.get(g.home_id, 0) + 1
            system.games_played[g.away_id] = system.games_played.get(g.away_id, 0) + 1

    if not n:
        return float("inf"), float("inf"), 0
    return ll / n, brier / n, n


GRIDS = {
    "k_factor": [12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64],
    "home_field": [0, 20, 35, 45, 50, 55, 65, 80, 95],
    "scale": [120, 150, 180, 210, 250, 280, 310, 350, 400, 500],
    "carry": [0.45, 0.55, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0],
    "fcs_initial": [1500, 1300, 1150, 1000, 900, 800, 700, 600, 500, 400],
}


def tune(seasons, top_division, score_from, start: EloConfig, rounds: int = 6):
    best = start
    best_ll, best_br, n = evaluate(seasons, best, top_division, score_from)
    print(f"  start            log-loss {best_ll:.5f}  Brier {best_br:.5f}  (n={n})")

    for rnd in range(1, rounds + 1):
        improved = False
        for param, values in GRIDS.items():
            if param == "fcs_initial" and not top_division:
                continue  # NFL has no second division
            trials = []
            for v in values:
                cand = replace(best, **{param: float(v)})
                ll, br, _ = evaluate(seasons, cand, top_division, score_from)
                trials.append((ll, v, cand))
            trials.sort(key=lambda t: t[0])
            ll, v, cand = trials[0]
            if ll < best_ll - 1e-6:
                best_ll, best, improved = ll, cand, True
                print(f"  round {rnd}: {param:<12} -> {v:<7} log-loss {ll:.5f}")
        if not improved:
            print(f"  round {rnd}: no further improvement")
            break

    best_ll, best_br, n = evaluate(seasons, best, top_division, score_from)
    return best, best_ll, best_br, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--first", type=int, default=2021)
    ap.add_argument("--last", type=int, default=2025)
    ap.add_argument("--score-from", type=int, default=2022, help="skip burn-in seasons")
    args = ap.parse_args()

    years = list(range(args.first, args.last + 1))
    seasons = load_seasons(args.league, years)
    total = sum(len(v) for v in seasons.values())
    top = all_fbs_ids(years) if args.league == "NCAAF" else set()

    print(f"=== Tuning {args.league} on {total} games, {args.first}-{args.last} ===")
    print(f"    scoring from {args.score_from}; top division: {len(top) or 'n/a'} teams\n")

    baseline = config_for_league(args.league)
    print("Current shipped config:")
    ll0, br0, n0 = evaluate(seasons, baseline, top, args.score_from)
    print(f"  log-loss {ll0:.5f}  Brier {br0:.5f}  (n={n0})\n")

    print("Coordinate descent:")
    best, ll, br, n = tune(seasons, top, args.score_from, baseline)

    print(f"\n=== Fitted {args.league} config ===")
    for f in ("k_factor", "home_field", "scale", "carry", "fcs_initial", "initial"):
        cur, new = getattr(baseline, f), getattr(best, f)
        mark = "" if cur == new else f"   (was {cur})"
        print(f"  {f:<14} {new}{mark}")
    print(f"\n  log-loss {ll0:.5f} -> {ll:.5f}   ({(ll0 - ll) / ll0 * 100:+.1f}%)")
    print(f"  Brier    {br0:.5f} -> {br:.5f}   ({(br0 - br) / br0 * 100:+.1f}%)")

    no_mov_ll, no_mov_br, _ = evaluate(seasons, best, top, args.score_from, use_mov=False)
    print(f"\n  MOV multiplier on : log-loss {ll:.5f}")
    print(f"  MOV multiplier off: log-loss {no_mov_ll:.5f}  "
          f"({'keep MOV' if ll < no_mov_ll else 'DROP MOV'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
