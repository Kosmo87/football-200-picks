#!/usr/bin/env python3
"""Smoke test: confidence filters, 3–5 leg paths, live NFL+NCAAF sample tips."""

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
from picks import (
    ConfidenceConfig,
    build_picks,
    compute_confidence,
    get_all_legs,
    summarize_board,
)


def demo_odds_math() -> None:
    print("=== Odds path (synthetic) ===")
    samples = [+200, +150, -110, -175, +145]
    for o in samples:
        raw = american_to_implied_prob(o)
        fair = fair_american_from_prob(raw)
        print(f"  {o:+d} → implied {raw*100:.1f}% → round-trip fair {fair:+d}")
    home, away = -175, +145
    imp_h = side_implied_prob(home, home, away, "home", use_devig=True)
    imp_a = side_implied_prob(away, home, away, "away", use_devig=True)
    print(
        f"  de-vig -175/+145 → home {imp_h*100:.1f}% / away {imp_a*100:.1f}% "
        f"(sum {(imp_h+imp_a)*100:.1f}%)"
    )


def demo_confidence_formula() -> None:
    print("\n=== Confidence formula (synthetic) ===")
    cases = [
        (0.08, +120, 6, "short +EV dog, decent sample"),
        (0.05, +550, 2, "lottery dog, thin sample"),
        (0.10, -110, 8, "short favorite chalk +EV"),
        (0.03, +200, 4, "borderline edge"),
    ]
    for edge, odds, sample, note in cases:
        score, label = compute_confidence(edge, odds, sample)
        print(
            f"  edge={edge*100:.0f}pp odds={odds:+d} sample={sample} "
            f"→ conf={score:.1f}/{label}  ({note})"
        )


def _fake_game(event_id: str, home_id: str, away_id: str, home_ml: int, away_ml: int):
    from espn import Game, OddsInfo, TeamInfo

    return Game(
        event_id=event_id,
        name=f"{away_id} @ {home_id}",
        short_name=f"{away_id}@{home_id}",
        date="2026-09-10T00:00:00Z",
        status="STATUS_SCHEDULED",
        home=TeamInfo(id=home_id, name=f"Home{home_id}", abbreviation=home_id, home_away="home"),
        away=TeamInfo(id=away_id, name=f"Away{away_id}", abbreviation=away_id, home_away="away"),
        odds=OddsInfo(
            provider="test",
            moneyline_home=home_ml,
            moneyline_away=away_ml,
        ),
    )


def demo_multileg_paths() -> None:
    """Synthetic boards: (A) 2-leg clears +200; (B) short prices need 3–5 legs."""
    print("\n=== Multi-leg construction (synthetic 3–5 paths) ===")

    def board(n_games: int, away_ml: int, home_ml: int, rating_gap: int = 160):
        elo = EloSystem(config=config_for_league("NFL"))
        pairs = []
        for i in range(n_games):
            hid, aid = f"H{i}", f"A{i}"
            elo.ensure_team(hid, f"Home{i}", hid)
            elo.ensure_team(aid, f"Away{i}", aid)
            elo.ratings[aid] = 1500 + rating_gap // 2 + i
            elo.ratings[hid] = 1500 - rating_gap // 2 - i
            elo.games_played[hid] = 6
            elo.games_played[aid] = 6
            pairs.append(_fake_game(f"E{i}", hid, aid, home_ml=home_ml, away_ml=away_ml))
        return pairs, elo

    # Board A: +115 dogs → 2-leg already clears +200 (prefer fewer legs)
    pairs_a, elo_a = board(12, away_ml=+115, home_ml=-135, rating_gap=180)
    cfg = ConfidenceConfig(
        min_edge_pp=3.0,
        min_sample_games=3,
        max_single_leg_odds=250,
        max_parlay_legs=5,
        min_combined_odds=200,
    )
    print("  -- Board A (+115 dogs; 2-leg should win) --")
    for max_legs in (1, 2, 5):
        cfg.max_parlay_legs = max_legs
        picks = build_picks(pairs_a, elo_a, n=3, cfg=cfg)
        kinds = [len(p.legs) for p in picks]
        odds = ", ".join(f"{p.combined_odds:+d}" for p in picks) or "none"
        print(f"  max_legs={max_legs}: tips={len(picks)} leg_counts={kinds} odds=[{odds}]")

    # Board B: near-even +EV (~+105) so 2-leg < +200; need 3+ legs
    # +105 * +105 ≈ +215? Wait +105 decimal 2.05^2 = 4.2025 → +320. Actually 2 short dogs often clear.
    # Use slight favorites / pick'em: -105 and -105 can't be both away.
    # Use +100 dogs: decimal 2.0^2 = 4.0 → +300 still clears. Need closer to -110 favorites.
    # Model favors home slight favorites priced -110 with large +EV so we pick home sides.
    pairs_b, elo_b = board(16, away_ml=+125, home_ml=-145, rating_gap=40)
    # Bump home ratings so home is +EV at -120
    for i in range(16):
        elo_b.ratings[f"H{i}"] = 1560 + i
        elo_b.ratings[f"A{i}"] = 1440 - i
    print("  -- Board B (short -145 favorites; 2-leg fails +200, need 3+) --")
    # Check what 2-leg combine to
    from odds import combine_odds as _c
    print(f"  2x -145 combine → {_c([-145, -145]):+d}; 3x → {_c([-145]*3):+d}; "
          f"4x → {_c([-145]*4):+d}; 5x → {_c([-145]*5):+d}")
    for max_legs in (1, 2, 3, 4, 5):
        cfg.max_parlay_legs = max_legs
        picks = build_picks(pairs_b, elo_b, n=3, cfg=cfg)
        kinds = [len(p.legs) for p in picks]
        odds = ", ".join(f"{p.combined_odds:+d}" for p in picks) or "none"
        confs = ", ".join(f"{p.avg_confidence:.0f}" for p in picks)
        print(
            f"  max_legs={max_legs}: tips={len(picks)} leg_counts={kinds} "
            f"odds=[{odds}] conf=[{confs}]"
        )
        for i, p in enumerate(picks, 1):
            print(
                f"    Tip#{i} {len(p.legs)}-leg → {p.combined_odds:+d} "
                f"avg_edge={p.combined_edge_pp:+.1f}pp "
                f"avg_conf={p.avg_confidence:.0f}/{p.confidence_label}"
            )

    # Board C: heavy chalk -250 → need 4 legs (3-leg ~+174)
    pairs_c, elo_c = board(20, away_ml=+200, home_ml=-250, rating_gap=40)
    for i in range(20):
        elo_c.ratings[f"H{i}"] = 1620 + i
        elo_c.ratings[f"A{i}"] = 1380 - i
    print("  -- Board C (short -250 chalk; need 4+ legs for +200) --")
    print(f"  3x -250 → {_c([-250]*3):+d}; 4x → {_c([-250]*4):+d}; 5x → {_c([-250]*5):+d}")
    for max_legs in (3, 4, 5):
        cfg.max_parlay_legs = max_legs
        picks = build_picks(pairs_c, elo_c, n=3, cfg=cfg)
        kinds = [len(p.legs) for p in picks]
        odds = ", ".join(f"{p.combined_odds:+d}" for p in picks) or "none"
        print(f"  max_legs={max_legs}: tips={len(picks)} leg_counts={kinds} odds=[{odds}]")
        for i, p in enumerate(picks, 1):
            print(
                f"    Tip#{i} {len(p.legs)}-leg → {p.combined_odds:+d} "
                f"avg_edge={p.combined_edge_pp:+.1f}pp "
                f"avg_conf={p.avg_confidence:.0f}/{p.confidence_label}"
            )

    # Board D: -320 chalk → 4-leg fails (+196), need 5 legs
    pairs_d, elo_d = board(25, away_ml=+250, home_ml=-320, rating_gap=40)
    for i in range(25):
        elo_d.ratings[f"H{i}"] = 1650 + i
        elo_d.ratings[f"A{i}"] = 1350 - i
    print("  -- Board D (-320 chalk; need 5 legs for +200) --")
    print(f"  4x -320 → {_c([-320]*4):+d}; 5x → {_c([-320]*5):+d}")
    for max_legs in (4, 5):
        cfg.max_parlay_legs = max_legs
        picks = build_picks(pairs_d, elo_d, n=2, cfg=cfg)
        kinds = [len(p.legs) for p in picks]
        odds = ", ".join(f"{p.combined_odds:+d}" for p in picks) or "none"
        print(f"  max_legs={max_legs}: tips={len(picks)} leg_counts={kinds} odds=[{odds}]")


def run_league(league: str, cfg: ConfidenceConfig) -> dict:
    print(f"\n=== {league} live smoke ===")
    out = {
        "league": league,
        "completed": 0,
        "upcoming": 0,
        "plus_ev": 0,
        "gated": 0,
        "tips": 0,
        "tip_details": [],
        "sample_edges": [],
        "error": None,
    }
    try:
        completed = fetch_completed_games(league)
        upcoming = get_upcoming_games(league)
        elo = EloSystem(config=config_for_league(league)).build(completed)
        summary = summarize_board(upcoming, elo, cfg=cfg)
        picks = build_picks(upcoming, elo, n=3, cfg=cfg)

        out["completed"] = len(completed)
        out["upcoming"] = len(upcoming)
        out["plus_ev"] = summary["plus_ev_legs"]
        out["gated"] = summary["gated_legs"]
        out["tips"] = len(picks)

        print(f"  Completed games (Elo): {len(completed)}")
        print(f"  Upcoming w/ ML:        {len(upcoming)}")
        print(f"  Rated teams:           {len(elo.ratings)}")
        print(f"  +EV sides (raw):       {summary['plus_ev_legs']}")
        print(f"  Pass confidence gates: {summary['gated_legs']}")
        print(
            f"  Gates: min_edge={cfg.min_edge_pp}pp sample≥{cfg.min_sample_games} "
            f"max_leg≤{cfg.max_single_leg_odds:+d} max_legs={cfg.max_parlay_legs}"
        )
        print(f"  Tips selected:         {len(picks)}")

        if elo.ratings:
            print("  Top 5 Elo:")
            for tid, rating, gp in elo.top(5):
                name = elo.names.get(tid) or elo.abbrs.get(tid) or tid
                print(f"    {name:28s} {rating:7.1f}  (n={gp})")

        legs = get_all_legs(upcoming, elo, max_leg_odds=cfg.max_single_leg_odds)
        top = sorted(legs, key=lambda l: l.edge, reverse=True)[:8]
        print("  Sample edges (top by edge):")
        for leg in top:
            epp = edge_pp(leg.model_win_prob, leg.implied_prob)
            line = (
                f"    {leg.team_abbr:5s} {leg.odds_american:+4d}  "
                f"model={leg.model_win_prob*100:5.1f}%  "
                f"impl={leg.implied_prob*100:5.1f}%  "
                f"edge={epp:+5.1f}pp  conf={leg.confidence:4.0f}/{leg.confidence_label:4s}  "
                f"sample={leg.sample_games}  fair={leg.fair_odds:+d}"
            )
            print(line)
            out["sample_edges"].append(
                {
                    "team": leg.team_abbr,
                    "odds": leg.odds_american,
                    "model": round(leg.model_win_prob, 4),
                    "implied": round(leg.implied_prob, 4),
                    "edge_pp": round(epp, 2),
                    "confidence": leg.confidence,
                    "sample": leg.sample_games,
                }
            )

        for i, pick in enumerate(picks, 1):
            detail = {
                "n_legs": len(pick.legs),
                "combined_odds": pick.combined_odds,
                "avg_edge_pp": round(pick.combined_edge_pp, 2),
                "avg_confidence": round(pick.avg_confidence, 1),
                "label": pick.confidence_label,
                "legs": [
                    {
                        "team": l.team_abbr,
                        "odds": l.odds_american,
                        "edge_pp": round(edge_pp(l.model_win_prob, l.implied_prob), 2),
                        "confidence": l.confidence,
                        "sample": l.sample_games,
                    }
                    for l in pick.legs
                ],
            }
            out["tip_details"].append(detail)
            print(
                f"  Tip #{i}: {len(pick.legs)}-leg → {pick.combined_odds:+d} | "
                f"avg_edge={pick.combined_edge_pp:+.1f}pp | "
                f"avg_conf={pick.avg_confidence:.0f}/{pick.confidence_label}"
            )
            print(f"           {pick.label}")
            for leg in pick.legs:
                print(
                    f"      {leg.team_name} {leg.odds_american:+d} "
                    f"edge={edge_pp(leg.model_win_prob, leg.implied_prob):+.1f}pp "
                    f"conf={leg.confidence:.0f}/{leg.confidence_label} "
                    f"sample={leg.sample_games}"
                )
    except Exception as e:
        out["error"] = str(e)
        print(f"  ERROR: {e}")
        traceback.print_exc()
    return out


def main() -> None:
    print("Football Tip Engine smoke test (v0.7)")
    demo_odds_math()
    demo_confidence_formula()
    demo_multileg_paths()

    results = []
    for league in ("NFL", "NCAAF"):
        cfg = ConfidenceConfig(
            min_edge_pp=5.0,
            min_sample_games=3 if league == "NFL" else 4,
            max_single_leg_odds=600,
            max_parlay_legs=5,
            min_combined_odds=200,
        )
        results.append(run_league(league, cfg))

    # Also exercise high-confidence preset on NFL if data loaded
    print("\n=== NFL high-confidence preset smoke ===")
    try:
        completed = fetch_completed_games("NFL")
        upcoming = get_upcoming_games("NFL")
        elo = EloSystem(config=config_for_league("NFL")).build(completed)
        cfg_hi = ConfidenceConfig().apply_high_confidence_preset("NFL")
        picks_hi = build_picks(upcoming, elo, n=3, cfg=cfg_hi)
        summary_hi = summarize_board(upcoming, elo, cfg=cfg_hi)
        print(
            f"  gated={summary_hi['gated_legs']} tips={len(picks_hi)} "
            f"leg_counts={[len(p.legs) for p in picks_hi]}"
        )
        for i, p in enumerate(picks_hi, 1):
            print(
                f"  Tip#{i}: {len(p.legs)}-leg {p.combined_odds:+d} "
                f"edge={p.combined_edge_pp:+.1f}pp conf={p.avg_confidence:.0f}"
            )
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n=== Summary ===")
    for r in results:
        tip_bits = []
        for t in r.get("tip_details") or []:
            tip_bits.append(
                f"{t['n_legs']}leg@{t['combined_odds']:+d}/e{t['avg_edge_pp']:+.1f}/c{t['avg_confidence']:.0f}"
            )
        print(
            f"  {r['league']}: completed={r['completed']} upcoming={r['upcoming']} "
            f"+EV={r['plus_ev']} gated={r['gated']} tips={r['tips']}"
            + (f" [{', '.join(tip_bits)}]" if tip_bits else "")
            + (f" ERR={r['error']}" if r["error"] else "")
        )


if __name__ == "__main__":
    main()
