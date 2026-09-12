"""
Headless build: fetch ESPN, rate teams, annotate every leg, write static JSON.

Outputs
  public/data/board.json    every upcoming game + both annotated sides
  public/data/history.json  the tracked-pick ledger (results, ROI, CLV)
  cache/prior_<LG>_<YR>.json  prior-season finals, so the season walk runs once

Pick selection itself lives in the browser (public/app.js) so the confidence
sliders stay interactive without a server. This script emits everything that
selection needs.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import context
from elo import (
    CompletedGame,
    EloSystem,
    build_multi_season,
    config_for_league,
)
from espn import (
    current_season_year,
    fetch_completed_games,
    fetch_season_completed,
    get_upcoming_games,
)
from history_data import all_fbs_ids, load_seasons
from odds import american_to_decimal, side_implied_prob
from picks import ConfidenceConfig, get_all_legs

LEAGUES = ("NFL", "NCAAF")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "public", "data")
CACHE_DIR = os.path.join(ROOT, "cache")

# The board ships a wide net; the browser tightens it with the sliders.
BOARD_MAX_LEG_ODDS = 1000
BOARD_MIN_LEG_ODDS = -450

# Ledger picks are logged under fixed baseline gates so the track record stays
# comparable run to run, regardless of what any visitor sets their sliders to.
TRACKED_GATES = {
    "min_edge_pp": 5.0,
    "max_single_leg_odds": 600,
    "max_parlay_legs": 5,
    "min_combined_odds": 200,
    "min_sample_games": {"NFL": 3, "NCAAF": 4},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, payload) -> None:
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Prior season (cached — last season's results never change)
# ---------------------------------------------------------------------------

def load_prior_season(league: str, season: int, refresh: bool = False) -> List[CompletedGame]:
    path = os.path.join(CACHE_DIR, f"prior_{league}_{season}.json")
    if not refresh:
        cached = _read_json(path, None)
        if cached:
            return [CompletedGame(**g) for g in cached]

    games = fetch_season_completed(league, season)
    if games:
        _write_json(path, [asdict(g) for g in games])
    return games


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

def serialize_leg(leg) -> Dict:
    return {
        "side": leg.side,
        "team_id": leg.team_id,
        "team_name": leg.team_name,
        "team_abbr": leg.team_abbr,
        "odds": leg.odds_american,
        "model_prob": round(leg.model_win_prob, 5),
        "implied_prob": round(leg.implied_prob, 5),
        # Six decimals, not two. edge_pp is derived from the two probabilities
        # above, which are kept to five -- so rounding it harder than its own
        # inputs made the stored number disagree with anything recomputed from
        # them by up to 0.005pp. The browser trusts this field; the Python
        # engine recomputes. They have to be the same number, because the gates
        # they feed (min_edge_pp is floored at 7.5) are decided at 0.1pp and a
        # leg sitting on the line went one way on the site and the other in the
        # ledger.
        "edge_pp": round(leg.edge * 100.0, 6),
        "fair_odds": leg.fair_odds,
        "sample": round(leg.sample_games, 2),
        "team_sample": round(leg.team_sample, 2),
        "opp_sample": round(leg.opp_sample, 2),
        "team_games": leg.team_games,
        "opp_games": leg.opp_games,
        "prior_credit": round(leg.prior_credit, 2),
    }


# How many prior seasons feed the chained ratings.
HISTORY_SEASONS = 5


def build_league(league: str, season: int, refresh_prior: bool):
    """Returns (board_dict, elo_system, completed_games)."""
    years = list(range(season - HISTORY_SEASONS, season))
    history = load_seasons(league, years, refresh=refresh_prior)
    # FCS opponents must not inherit an average FBS rating.
    top = all_fbs_ids(years + [season]) if league == "NCAAF" else set()
    cfg = config_for_league(league)

    completed = fetch_completed_games(league)
    base = build_multi_season(history, league=league, config=cfg, top_division=top)
    elo = EloSystem(config=cfg, top_division=top).seed_from(base).build(completed)
    prior = history.get(season - 1, [])
    upcoming = get_upcoming_games(league)

    legs = get_all_legs(
        upcoming,
        elo,
        max_leg_odds=BOARD_MAX_LEG_ODDS,
        min_leg_odds=BOARD_MIN_LEG_ODDS,
    )
    by_event: Dict[str, List] = {}
    for leg in legs:
        by_event.setdefault(leg.game.event_id, []).append(leg)

    games_out = []
    for game in upcoming:
        event_legs = by_event.get(game.event_id, [])
        if not event_legs:
            continue
        games_out.append(
            {
                "event_id": game.event_id,
                "name": game.name,
                "short_name": game.short_name,
                "kickoff": game.date,
                "status": game.status,
                "neutral": bool(getattr(game, "neutral", False)),
                "provider": game.odds.provider,
                "spread": game.odds.spread,
                "spread_details": game.odds.spread_details,
                "total": game.odds.total,
                "home": {
                    "id": game.home.id,
                    "name": game.home.name,
                    "abbr": game.home.abbreviation,
                    "elo": round(elo.rating(game.home.id), 1),
                    "moneyline": game.odds.moneyline_home,
                },
                "away": {
                    "id": game.away.id,
                    "name": game.away.name,
                    "abbr": game.away.abbreviation,
                    "elo": round(elo.rating(game.away.id), 1),
                    "moneyline": game.odds.moneyline_away,
                },
                "legs": [serialize_leg(l) for l in event_legs],
            }
        )

    # Who is hurt and what the weather is doing. Attached to the board rather
    # than folded into the ratings: there is no historical injury or weather
    # archive to test a coefficient against, so this is displayed and never
    # applied. See context.py.
    try:
        ctx = context.build_context(league, games_out)
        for g in games_out:
            g["context"] = ctx.get(str(g["event_id"]))
    except Exception as e:
        # The board was useful before any of this existed and stays useful when
        # ESPN's injury feed 500s.
        print(f"[board] {league} context unavailable: {e}")

    cfg = config_for_league(league)
    ratings = [
        {
            "team_id": tid,
            "name": elo.names.get(tid, tid),
            "abbr": elo.abbrs.get(tid, ""),
            "elo": round(rating, 1),
            "games": elo.games_played.get(tid, 0),
            "prior_games": elo.prior_games.get(tid, 0),
        }
        for tid, rating in sorted(elo.ratings.items(), key=lambda x: -x[1])
    ]

    return {
        "league": league,
        "games": games_out,
        "ratings": ratings,
        "meta": {
            "season": season,
            "prior_season": season - 1,
            "carryover": bool(history),
            "prior_games_used": sum(len(v) for v in history.values()),
            "history_seasons": f"{years[0]}-{years[-1]}",
            "top_division_teams": len(top),
            "completed_games": len(completed),
            "upcoming_games": len(upcoming),
            "rated_teams": len(elo.ratings),
            "elo": {
                "k_factor": cfg.k_factor,
                "home_field": cfg.home_field,
                "carry": cfg.carry,
                "initial": cfg.initial,
            },
            "default_min_sample": TRACKED_GATES["min_sample_games"][league],
        },
    }, elo, completed


# ---------------------------------------------------------------------------
# Ledger: log tracked picks, refresh closing odds, grade finals, score CLV
# ---------------------------------------------------------------------------

def tracked_config(league: str) -> ConfidenceConfig:
    return ConfidenceConfig(
        min_edge_pp=TRACKED_GATES["min_edge_pp"],
        min_sample_games=TRACKED_GATES["min_sample_games"][league],
        max_single_leg_odds=TRACKED_GATES["max_single_leg_odds"],
        max_parlay_legs=TRACKED_GATES["max_parlay_legs"],
        min_combined_odds=TRACKED_GATES["min_combined_odds"],
    )


def pick_id(league: str, event_id: str, team_id: str) -> str:
    return f"{league}:{event_id}:{team_id}"


def log_tracked_legs(history: Dict, league: str, board: Dict) -> int:
    """
    Record every leg that clears the baseline gates, once per game.

    Logged at the price first seen (`open_odds`); later runs refresh
    `close_odds` until kickoff so closing-line value can be measured.
    """
    from picks import passes_confidence_gates, Leg  # local import: gate logic only

    cfg = tracked_config(league)
    existing = {p["id"]: p for p in history["picks"]}
    added = 0
    now = utcnow()

    for game in board["games"]:
        for leg in game["legs"]:
            edge_pp = leg["edge_pp"]
            if edge_pp < cfg.min_edge_pp:
                continue
            if leg["sample"] < cfg.min_sample_games:
                continue
            if leg["odds"] > cfg.max_single_leg_odds:
                continue

            pid = pick_id(league, game["event_id"], leg["team_id"])
            opp = game["home"] if leg["side"] == "away" else game["away"]
            record = existing.get(pid)
            if record is None:
                history["picks"].append(
                    {
                        "id": pid,
                        "league": league,
                        "event_id": game["event_id"],
                        "matchup": game["short_name"],
                        "kickoff": game["kickoff"],
                        "team_id": leg["team_id"],
                        "team_abbr": leg["team_abbr"],
                        "team_name": leg["team_name"],
                        "opp_abbr": opp["abbr"],
                        "side": leg["side"],
                        "logged_at": now,
                        "open_odds": leg["odds"],
                        "open_implied": leg["implied_prob"],
                        "close_odds": leg["odds"],
                        "close_implied": leg["implied_prob"],
                        "model_prob": leg["model_prob"],
                        "edge_pp": edge_pp,
                        "sample": leg["sample"],
                        "status": "open",
                        "result": None,
                        "clv_pp": 0.0,
                        "graded_at": None,
                    }
                )
                added += 1
            elif record["status"] == "open":
                # Still pre-kickoff: this is the newest price we have seen
                record["close_odds"] = leg["odds"]
                record["close_implied"] = leg["implied_prob"]
                record["clv_pp"] = round(
                    (leg["implied_prob"] - record["open_implied"]) * 100.0, 2
                )
    return added


def grade_history(history: Dict, finals_by_event: Dict[str, CompletedGame]) -> int:
    """Settle open picks whose game has a final score."""
    graded = 0
    now = utcnow()
    for p in history["picks"]:
        if p["status"] != "open":
            continue
        final = finals_by_event.get(p["event_id"])
        if final is None:
            continue
        home_won = final.home_score > final.away_score
        tie = final.home_score == final.away_score
        picked_home = p["side"] == "home"
        if tie:
            p["status"] = "push"
        elif picked_home == home_won:
            p["status"] = "won"
        else:
            p["status"] = "lost"
        p["result"] = {
            "home_abbr": final.home_abbr,
            "away_abbr": final.away_abbr,
            "home_score": final.home_score,
            "away_score": final.away_score,
        }
        p["graded_at"] = now
        graded += 1
    return graded


def summarize_history(history: Dict) -> Dict:
    settled = [p for p in history["picks"] if p["status"] in ("won", "lost", "push")]
    wins = [p for p in settled if p["status"] == "won"]
    losses = [p for p in settled if p["status"] == "lost"]
    pushes = [p for p in settled if p["status"] == "push"]

    # Flat 1 unit per pick at the logged (open) price
    staked = len(wins) + len(losses)
    returns = sum(american_to_decimal(p["open_odds"]) - 1.0 for p in wins) - len(losses)
    roi = (returns / staked * 100.0) if staked else 0.0

    clv_scored = [p for p in settled if p.get("clv_pp") is not None]
    avg_clv = (
        sum(p["clv_pp"] for p in clv_scored) / len(clv_scored) if clv_scored else 0.0
    )
    beat_close = len([p for p in clv_scored if p["clv_pp"] > 0])

    by_league = {}
    for lg in LEAGUES:
        lg_settled = [p for p in settled if p["league"] == lg]
        lg_w = len([p for p in lg_settled if p["status"] == "won"])
        lg_l = len([p for p in lg_settled if p["status"] == "lost"])
        lg_staked = lg_w + lg_l
        lg_ret = (
            sum(
                american_to_decimal(p["open_odds"]) - 1.0
                for p in lg_settled
                if p["status"] == "won"
            )
            - lg_l
        )
        by_league[lg] = {
            "won": lg_w,
            "lost": lg_l,
            "units": round(lg_ret, 2),
            "roi_pct": round(lg_ret / lg_staked * 100.0, 1) if lg_staked else 0.0,
        }

    return {
        "open": len([p for p in history["picks"] if p["status"] == "open"]),
        "won": len(wins),
        "lost": len(losses),
        "pushed": len(pushes),
        "units": round(returns, 2),
        "roi_pct": round(roi, 1),
        "win_pct": round(len(wins) / staked * 100.0, 1) if staked else 0.0,
        "avg_clv_pp": round(avg_clv, 2),
        "beat_close_pct": round(beat_close / len(clv_scored) * 100.0, 1)
        if clv_scored
        else 0.0,
        "by_league": by_league,
    }


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build the static picks board.")
    ap.add_argument(
        "--refresh-prior",
        action="store_true",
        help="re-pull the prior season instead of using the cache",
    )
    ap.add_argument(
        "--leagues",
        default=",".join(LEAGUES),
        help="comma-separated leagues to build (default: NFL,NCAAF)",
    )
    args = ap.parse_args()

    _ensure_dirs()
    season = current_season_year()
    leagues = [l.strip().upper() for l in args.leagues.split(",") if l.strip()]

    history = _read_json(
        os.path.join(DATA_DIR, "history.json"), {"picks": [], "summary": {}}
    )
    history.setdefault("picks", [])

    board = {
        "generated_at": utcnow(),
        "season": season,
        "leagues": {},
        "errors": {},
    }
    finals_by_event: Dict[str, CompletedGame] = {}

    for league in leagues:
        print(f"[{league}] building…", flush=True)
        try:
            league_board, _elo, completed = build_league(
                league, season, args.refresh_prior
            )
        except Exception as e:  # a league outage should not sink the whole build
            print(f"[{league}] FAILED: {e}", flush=True)
            board["errors"][league] = str(e)
            continue

        board["leagues"][league] = league_board
        meta = league_board["meta"]
        print(
            f"[{league}] {meta['upcoming_games']} upcoming, "
            f"{meta['completed_games']} completed, "
            f"{meta['rated_teams']} rated, carryover={meta['carryover']}"
            f" ({meta['prior_games_used']} prior games)",
            flush=True,
        )

        for g in completed:
            finals_by_event[g.event_id] = g

        added = log_tracked_legs(history, league, league_board)
        print(f"[{league}] logged {added} new tracked picks", flush=True)

    graded = grade_history(history, finals_by_event)
    history["summary"] = summarize_history(history)
    history["updated_at"] = utcnow()
    print(f"[ledger] graded {graded} picks; summary={history['summary']}", flush=True)

    _write_json(os.path.join(DATA_DIR, "board.json"), board)
    _write_json(os.path.join(DATA_DIR, "history.json"), history)
    print(f"wrote {DATA_DIR}/board.json and history.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
