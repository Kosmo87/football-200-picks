"""
The calibration test at real scale: 27 seasons of NFL, not 73 games.

nflverse supplies the scores and the closing lines together, so no ESPN call is
needed and no sample is lost to deleted odds. Elo is rebuilt walk-forward across
every season and each game is predicted from games that finished before it, then
scored against the market's own de-vigged number on the same game.

  python nfl_calibrate.py
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from typing import Dict, List

from elo import EloConfig, EloSystem
from nflverse_data import load_games
from odds import de_vig_probs

EPS = 1e-9
BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
           (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]


def brier(pairs):
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else float("nan")


def logloss(pairs):
    tot = 0.0
    for p, o in pairs:
        p = min(max(p, EPS), 1 - EPS)
        tot -= o * math.log(p) + (1 - o) * math.log(1 - p)
    return tot / len(pairs) if pairs else float("nan")


def report(name, pairs):
    by = defaultdict(list)
    for p, o in pairs:
        pct = p * 100
        for lo, hi in BUCKETS:
            if lo <= pct < hi:
                by[(lo, hi)].append((p, o))
                break
        else:
            by[(90, 100)].append((p, o))
    print(f"\n  {name}  (n={len(pairs):,}  Brier {brier(pairs):.4f}  "
          f"logloss {logloss(pairs):.4f})")
    print(f"  {'predicted':>12} {'n':>6} {'said':>7} {'actual':>8} {'gap':>8}")
    for b in BUCKETS:
        rows = by.get(b)
        if not rows:
            continue
        said = sum(p for p, _ in rows) / len(rows) * 100
        act = sum(o for _, o in rows) / len(rows) * 100
        gap = act - said
        flag = "  <--" if abs(gap) > 5 else ""
        print(f"  {b[0]:>5}-{b[1]:<3}% {len(rows):>6} {said:>6.1f}% "
              f"{act:>7.1f}% {gap:>+7.1f}pp{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-season", type=int, default=2005,
                    help="start scoring here; earlier seasons are burn-in")
    ap.add_argument("--k", type=float, default=None)
    ap.add_argument("--hfa", type=float, default=None)
    args = ap.parse_args()

    games = sorted(load_games(), key=lambda g: (g.season, g.week, g.date))
    print(f"{len(games):,} games, {games[0].season}-{games[-1].season}")

    from elo import NFL_ELO
    cfg = EloConfig(
        initial=NFL_ELO.initial,
        k_factor=args.k if args.k is not None else NFL_ELO.k_factor,
        home_field=args.hfa if args.hfa is not None else NFL_ELO.home_field,
        scale=NFL_ELO.scale, carry=NFL_ELO.carry,
    )
    print(f"Elo: K={cfg.k_factor} HFA={cfg.home_field} carry={cfg.carry} "
          f"scale={cfg.scale}\n")

    model_pairs, market_pairs, both = [], [], []
    system = None
    season = None

    for g in games:
        if g.season != season:
            nxt = EloSystem(config=cfg)
            if system is not None:
                nxt.seed_from(system)
            system, season = nxt, g.season

        system.ensure_team(g.home_team, g.home_team, g.home_team)
        system.ensure_team(g.away_team, g.away_team, g.away_team)

        if g.home_score != g.away_score and g.season >= args.from_season:
            outcome = 1.0 if g.margin > 0 else 0.0
            p_model = system.win_prob_home(g.home_team, g.away_team, neutral=g.neutral)
            model_pairs.append((p_model, outcome))
            if g.home_moneyline and g.away_moneyline:
                ph, pa = de_vig_probs(g.home_moneyline, g.away_moneyline)
                if ph is not None:
                    market_pairs.append((ph, outcome))
                    both.append((p_model, ph, outcome))

        # update
        from elo import CompletedGame
        system.update_game(CompletedGame(
            event_id=g.game_id, date=g.date,
            home_id=g.home_team, away_id=g.away_team,
            home_name=g.home_team, away_name=g.away_team,
            home_abbr=g.home_team, away_abbr=g.away_team,
            home_score=g.home_score, away_score=g.away_score,
            neutral=g.neutral, season_type=2,
        ))

    report("MODEL (Elo)", model_pairs)
    if market_pairs:
        report("MARKET (de-vigged closing moneyline)", market_pairs)

    if both:
        m_only = [(p, o) for p, _, o in both]
        k_only = [(q, o) for _, q, o in both]
        print(f"\n  Head to head on the same {len(both):,} games")
        print(f"    model  Brier {brier(m_only):.4f}  logloss {logloss(m_only):.4f}")
        print(f"    market Brier {brier(k_only):.4f}  logloss {logloss(k_only):.4f}")
        gap = brier(m_only) - brier(k_only)
        print(f"    {'market' if gap > 0 else 'MODEL'} better by {abs(gap):.4f}")

        print(f"\n  Does a bigger claimed edge pay?")
        print(f"    {'model edge':>14} {'n':>6} {'said':>8} {'actual':>8}")
        for lo, hi in [(-100, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 100)]:
            sub = [(p, q, o) for p, q, o in both if lo <= (p - q) * 100 < hi]
            if len(sub) < 20:
                continue
            said = sum(p for p, _, _ in sub) / len(sub) * 100
            act = sum(o for _, _, o in sub) / len(sub) * 100
            print(f"    {lo:>+4}..{hi:<+5}pp {len(sub):>6} {said:>7.1f}% {act:>7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
