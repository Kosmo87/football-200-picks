#!/usr/bin/env python3
"""Smoke test: load games, build Elo, print sample edges/picks."""

from __future__ import annotations

import traceback

from elo import EloSystem, config_for_league
from espn import fetch_completed_games, get_upcoming_games
from odds import (
    american_to_implied_prob,
    edge_pp,
    fair_american_from_prob,
    side_implied_prob,
)
from picks import build_picks, get_all_legs, summarize_board


def demo_odds_math() -> None:
    print("=== Odds path (synthetic) ===")
    samples = [+200, +150, -110, -175, +145]
    for o in samples:
        raw = american_to_implied_prob(o)
        fair = fair_american_from_prob(raw)
        print(f"  {o:+d} → implied {raw*100:.1f}% → round-trip fair {fair:+d}")
    # de-vig example
    home, away = -175, +145
    imp_h = side_implied_prob(home, home, away, "home", use_devig=True)
    imp_a = side_implied_prob(away, home, away, "away", use_devig=True)
    print(
        f"  de-vig -175/+145 → home {imp_h*100:.1f}% / away {imp_a*100:.1f}% "
        f"(sum { (imp_h+imp_a)*100:.1f}%)"
    )


def demo_elo_path() -> None:
    print("\n=== Elo path (synthetic) ===")
    from elo import CompletedGame

    elo = EloSystem(config=config_for_league("NFL"))
    games = [
        CompletedGame("1", "2026-08-01", "H1", "A1", "Home1", "Away1", "H1", "A1", 24, 17),
        CompletedGame("2", "2026-08-08", "H1", "A2", "Home1", "Away2", "H1", "A2", 10, 27),
        CompletedGame("3", "2026-08-15", "H2", "A1", "Home2", "Away1", "H2", "A1", 21, 20),
    ]
    elo.build(games)
    p = elo.win_prob_home("H1", "A1")
    print(f"  After 3 games: H1 Elo={elo.rating('H1'):.1f} A1={elo.rating('A1'):.1f}")
    print(f"  P(H1 beats A1 at home)={p*100:.1f}% fair={fair_american_from_prob(p):+d}")


def run_league(league: str) -> dict:
    print(f"\n=== {league} live smoke ===")
    out = {
        "league": league,
        "completed": 0,
        "upcoming": 0,
        "plus_ev": 0,
        "tips": 0,
        "sample_edges": [],
        "error": None,
    }
    try:
        completed = fetch_completed_games(league)
        upcoming = get_upcoming_games(league)
        elo = EloSystem(config=config_for_league(league)).build(completed)
        summary = summarize_board(upcoming, elo)
        picks = build_picks(upcoming, elo, n=3)

        out["completed"] = len(completed)
        out["upcoming"] = len(upcoming)
        out["plus_ev"] = summary["plus_ev_legs"]
        out["tips"] = len(picks)

        print(f"  Completed games (Elo): {len(completed)}")
        print(f"  Upcoming w/ ML:        {len(upcoming)}")
        print(f"  Rated teams:           {len(elo.ratings)}")
        print(f"  +EV sides:             {summary['plus_ev_legs']}")
        print(f"  Tips selected:         {len(picks)}")

        if elo.ratings:
            print("  Top 5 Elo:")
            for tid, rating, gp in elo.top(5):
                name = elo.names.get(tid) or elo.abbrs.get(tid) or tid
                print(f"    {name:28s} {rating:7.1f}  (n={gp})")

        legs = get_all_legs(upcoming, elo)
        top = sorted(legs, key=lambda l: l.edge, reverse=True)[:8]
        print("  Sample edges (top by edge):")
        for leg in top:
            epp = edge_pp(leg.model_win_prob, leg.implied_prob)
            line = (
                f"    {leg.team_abbr:5s} {leg.odds_american:+4d}  "
                f"model={leg.model_win_prob*100:5.1f}%  "
                f"impl={leg.implied_prob*100:5.1f}%  "
                f"edge={epp:+5.1f}pp  fair={leg.fair_odds:+d}"
            )
            print(line)
            out["sample_edges"].append(
                {
                    "team": leg.team_abbr,
                    "odds": leg.odds_american,
                    "model": round(leg.model_win_prob, 4),
                    "implied": round(leg.implied_prob, 4),
                    "edge_pp": round(epp, 2),
                }
            )

        for i, pick in enumerate(picks, 1):
            print(
                f"  Tip #{i}: {pick.label} | avg_edge={pick.combined_edge_pp:+.1f}pp"
            )
            for leg in pick.legs:
                print(
                    f"      {leg.team_name} {leg.odds_american:+d} "
                    f"edge={edge_pp(leg.model_win_prob, leg.implied_prob):+.1f}pp"
                )
    except Exception as e:
        out["error"] = str(e)
        print(f"  ERROR: {e}")
        traceback.print_exc()
    return out


def main() -> None:
    print("Football Tip Engine smoke test (v0.6)")
    demo_odds_math()
    demo_elo_path()
    results = [run_league("NFL"), run_league("NCAAF")]
    print("\n=== Summary ===")
    for r in results:
        print(
            f"  {r['league']}: completed={r['completed']} upcoming={r['upcoming']} "
            f"+EV={r['plus_ev']} tips={r['tips']}"
            + (f" ERR={r['error']}" if r["error"] else "")
        )


if __name__ == "__main__":
    main()
