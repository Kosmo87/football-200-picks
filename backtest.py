"""
Replay the engine against a past slate — what it *would* have picked, and how
those picks actually finished.

Honesty rules, both of which are the whole point:
  1. Ratings are built only from games that finished strictly BEFORE the slate's
     first kickoff. No result from the slate informs the picks.
  2. Prices are the pregame DraftKings moneylines recorded on ESPN's pickcenter
     (close by default, open on request) — not anything computed after the fact.

  python backtest.py --league NCAAF --start 2026-09-04 --end 2026-09-06
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from elo import CompletedGame, build_league_elo
from espn import (
    ESPN_NCAAF_BASE,
    ESPN_NFL,
    Game,
    OddsInfo,
    PREFERRED_PROVIDER,
    _parse_competitors,
    _provider_matches,
    _side_moneyline,
    _team_info,
    current_season_year,
    fetch_completed_games,
    fetch_scoreboard,
    parse_completed_game,
)
from odds import american_to_decimal, parse_american_odds
from picks import ConfidenceConfig, build_picks, get_all_legs, passes_confidence_gates

SUMMARY_URLS = {
    "NFL": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "NCAAF": (
        "https://site.web.api.espn.com/apis/site/v2/sports/football/"
        "college-football/summary"
    ),
}

# Same baseline gates the live ledger tracks, so backtest and forward test are
# measuring the same engine.
TRACKED_MIN_SAMPLE = {"NFL": 3, "NCAAF": 4}


def scoreboard_url(league: str, day: date) -> str:
    d = day.strftime("%Y%m%d")
    if league == "NFL":
        return f"{ESPN_NFL}?dates={d}"
    return f"{ESPN_NCAAF_BASE}?dates={d}&groups=80&limit=200"


def historical_odds(league: str, event_id: str, price: str) -> Optional[OddsInfo]:
    """
    Pregame moneylines for a finished game, from ESPN's pickcenter.

    The scoreboard drops odds once a game finals, but the per-event summary keeps
    the provider's open and close numbers.
    """
    keys = ("close", "open") if price == "close" else ("open", "close")
    try:
        data = fetch_scoreboard(f"{SUMMARY_URLS[league]}?event={event_id}", retries=2)
    except Exception:
        return None

    entries = data.get("pickcenter") or []
    preferred = [
        o
        for o in entries
        if _provider_matches((o.get("provider") or {}).get("name", ""), PREFERRED_PROVIDER)
    ]
    for entry in preferred + entries:
        ml = entry.get("moneyline") or {}
        home = _side_moneyline_ordered(ml.get("home"), keys)
        away = _side_moneyline_ordered(ml.get("away"), keys)
        if home is None:
            home = parse_american_odds((entry.get("homeTeamOdds") or {}).get("moneyLine"))
        if away is None:
            away = parse_american_odds((entry.get("awayTeamOdds") or {}).get("moneyLine"))
        if home is not None and away is not None:
            return OddsInfo(
                provider=(entry.get("provider") or {}).get("name", "Unknown"),
                moneyline_home=home,
                moneyline_away=away,
                spread=entry.get("spread"),
                spread_details=entry.get("details"),
                total=entry.get("overUnder"),
            )
    return None


def _side_moneyline_ordered(ml_side, keys) -> Optional[int]:
    if not isinstance(ml_side, dict):
        return None
    for key in keys:
        nested = ml_side.get(key)
        if isinstance(nested, dict):
            parsed = parse_american_odds(nested.get("odds"))
            if parsed is not None:
                return parsed
    return _side_moneyline(ml_side)


def load_slate(
    league: str, start: date, end: date, price: str
) -> Tuple[List[Game], Dict[str, CompletedGame]]:
    """Finished games in the window, rebuilt as pre-kickoff Games with pregame odds."""
    events = []
    day = start
    while day <= end:
        try:
            data = fetch_scoreboard(scoreboard_url(league, day), retries=2)
            events.extend(data.get("events") or [])
        except Exception as e:
            print(f"  ! {day}: {e}")
        day += timedelta(days=1)

    finals: Dict[str, CompletedGame] = {}
    shells = []
    for ev in events:
        final = parse_completed_game(ev)
        if not final:
            continue
        finals[final.event_id] = final
        comp = (ev.get("competitions") or [{}])[0]
        home_c, away_c = _parse_competitors(comp)
        if not home_c or not away_c:
            continue
        shells.append(
            (
                str(ev.get("id")),
                ev.get("name", ""),
                ev.get("shortName", ""),
                ev.get("date", ""),
                _team_info(home_c, "home"),
                _team_info(away_c, "away"),
                bool(comp.get("neutralSite")),
            )
        )

    def build(shell):
        eid, name, short, when, home, away, neutral = shell
        odds = historical_odds(league, eid, price)
        if odds is None:
            return None
        return Game(
            event_id=eid,
            name=name,
            short_name=short,
            date=when,
            status="Scheduled",
            home=home,
            away=away,
            odds=odds,
            neutral=neutral,
        )

    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        games = [g for g in ex.map(build, shells) if g is not None]

    return games, finals


def grade(pick, finals: Dict[str, CompletedGame]) -> Tuple[str, float, List[str]]:
    """Settle a pick: every leg must win for a parlay to pay."""
    detail = []
    all_won = True
    any_missing = False
    for leg in pick.legs:
        final = finals.get(leg.game.event_id)
        if final is None:
            any_missing = True
            detail.append(f"{leg.team_abbr}: no final")
            continue
        home_won = final.home_score > final.away_score
        tie = final.home_score == final.away_score
        won = (not tie) and ((leg.side == "home") == home_won)
        mark = "✓" if won else ("–" if tie else "✗")
        detail.append(
            f"{mark} {leg.team_abbr} {leg.odds_american:+d} "
            f"({final.away_abbr} {final.away_score}–{final.home_score} {final.home_abbr})"
        )
        if not won:
            all_won = False

    if any_missing:
        return "void", 0.0, detail
    if all_won:
        return "won", american_to_decimal(pick.combined_odds) - 1.0, detail
    return "lost", -1.0, detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--start", required=True, help="slate start, YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="slate end, YYYY-MM-DD")
    ap.add_argument("--price", default="close", choices=("close", "open"))
    ap.add_argument("--picks", type=int, default=3, help="tips per slate (default 3)")
    args = ap.parse_args()

    league = args.league
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    cutoff = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)

    print(f"=== Backtest {league} {start} → {end} ({args.price} lines) ===\n")

    print("Loading slate…")
    games, finals = load_slate(league, start, end, args.price)
    print(f"  {len(finals)} finished games, {len(games)} with pregame moneylines\n")
    if not games:
        print("Nothing to replay.")
        return 1

    print("Rating teams on pre-slate data only…")
    from build_board import load_prior_season

    prior = load_prior_season(league, current_season_year() - 1)
    completed = fetch_completed_games(league)
    training = [
        g
        for g in completed
        if g.date and datetime.fromisoformat(g.date.replace("Z", "+00:00")) < cutoff
    ]
    dropped = len(completed) - len(training)
    elo = build_league_elo(training, prior_games=prior, league=league)
    print(f"  {len(prior)} prior-season games + {len(training)} finished before {start}")
    print(f"  excluded {dropped} games at/after the cutoff (no look-ahead)")
    print(f"  {len(elo.ratings)} teams rated\n")

    cfg = ConfidenceConfig(min_sample_games=TRACKED_MIN_SAMPLE[league])
    legs = get_all_legs(games, elo, max_leg_odds=cfg.max_single_leg_odds)
    gated = [l for l in legs if passes_confidence_gates(l, cfg)]
    print(
        f"Board: {len(legs)} priced sides · {len([l for l in legs if l.edge > 0])} +EV "
        f"· {len(gated)} pass gates\n"
    )

    picks = build_picks(games, elo, n=args.picks, cfg=cfg)
    if not picks:
        print("No qualifying tips for this slate.")
        return 0

    total = 0.0
    results = []
    print(f"--- What it would have picked ({len(picks)} tips, 1u each) ---\n")
    for i, pick in enumerate(picks, 1):
        status, units, detail = grade(pick, finals)
        total += units
        results.append(status)
        print(f"#{i}  {pick.label}")
        print(
            f"    {pick.combined_odds:+d} · conf {pick.avg_confidence:.0f}"
            f"/{pick.confidence_label} · edge {pick.combined_edge_pp:+.1f}pp"
        )
        for d in detail:
            print(f"    {d}")
        print(f"    → {status.upper()}  {units:+.2f}u\n")

    won = results.count("won")
    lost = results.count("lost")
    staked = won + lost
    print("--- Result ---")
    print(f"  {won}–{lost} · {total:+.2f}u on {staked}u staked", end="")
    print(f" · ROI {total / staked * 100:+.1f}%" if staked else "")

    # Every gated leg, flat 1u. Three tips is far too small to read; the full
    # qualifying pool is the honest sample for "does this find edges at all".
    print("\n--- All qualifying legs (flat 1u each) ---")
    pool_units = 0.0
    pool_w = pool_l = 0
    rows = []
    for leg in sorted(gated, key=lambda l: -l.edge):
        final = finals.get(leg.game.event_id)
        if final is None or final.home_score == final.away_score:
            continue
        won = (leg.side == "home") == (final.home_score > final.away_score)
        u = (american_to_decimal(leg.odds_american) - 1.0) if won else -1.0
        pool_units += u
        pool_w += int(won)
        pool_l += int(not won)
        rows.append(
            f"  {'✓' if won else '✗'} {leg.team_abbr:<6} {leg.odds_american:+5d}  "
            f"edge {leg.edge * 100:+5.1f}pp  conf {leg.confidence:3.0f}  "
            f"{final.away_abbr} {final.away_score}–{final.home_score} {final.home_abbr}"
            f"   {u:+.2f}u"
        )
    for r in rows:
        print(r)
    staked_pool = pool_w + pool_l
    if staked_pool:
        print(
            f"  → {pool_w}–{pool_l} · {pool_units:+.2f}u on {staked_pool}u"
            f" · ROI {pool_units / staked_pool * 100:+.1f}%"
        )
        # Did the model's probabilities mean anything? Compare its average
        # predicted win rate to what actually happened.
        exp = sum(l.model_win_prob for l in gated if finals.get(l.game.event_id))
        n = len([l for l in gated if finals.get(l.game.event_id)])
        if n:
            print(
                f"  model predicted {exp / n * 100:.1f}% avg win rate, "
                f"actual {pool_w / staked_pool * 100:.1f}%"
            )

    # Leg-level view: parlays hide whether the model was actually right often.
    leg_w = leg_l = 0
    for pick in picks:
        for leg in pick.legs:
            f = finals.get(leg.game.event_id)
            if not f or f.home_score == f.away_score:
                continue
            if (leg.side == "home") == (f.home_score > f.away_score):
                leg_w += 1
            else:
                leg_l += 1
    if leg_w + leg_l:
        print(f"  legs: {leg_w}–{leg_l} ({leg_w / (leg_w + leg_l) * 100:.0f}% of sides correct)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
