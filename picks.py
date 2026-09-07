"""+EV tip selection: Elo model vs implied odds, independent +200 picks."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Set

from elo import EloSystem
from espn import Game
from odds import (
    combine_odds,
    edge_pp,
    fair_american_from_prob,
    side_implied_prob,
)

MIN_ODDS = 200
MAX_SINGLE_ODDS = 900
MIN_EDGE = 0.0  # require model_win_prob > implied (strictly edge > 0 after float)


@dataclass
class Leg:
    game: Game
    side: str
    team_name: str
    team_abbr: str
    team_id: str
    odds_american: int
    model_win_prob: float = 0.0
    implied_prob: float = 0.0
    edge: float = 0.0  # probability points (e.g. 0.04 = 4pp)
    fair_odds: int = 0


@dataclass
class Pick:
    legs: List[Leg]
    combined_odds: int
    score: float  # ranking score (avg edge)
    label: str = ""
    combined_edge_pp: float = 0.0


def _opp_id(leg: Leg) -> str:
    return leg.game.home.id if leg.side == "away" else leg.game.away.id


def legs_overlap(legs: List[Leg]) -> bool:
    teams: Set[str] = set()
    games: Set[str] = set()
    for leg in legs:
        if leg.team_id in teams or leg.game.event_id in games:
            return True
        teams.add(leg.team_id)
        teams.add(_opp_id(leg))
        games.add(leg.game.event_id)
    return False


def annotate_leg(game: Game, side: str, elo: EloSystem) -> Optional[Leg]:
    team = game.home if side == "home" else game.away
    odds = game.odds.moneyline_home if side == "home" else game.odds.moneyline_away
    if odds is None or not (-200 <= odds <= MAX_SINGLE_ODDS):
        return None

    model_p = elo.win_prob_side(game.home.id, game.away.id, side)
    implied = side_implied_prob(
        odds,
        game.odds.moneyline_home,
        game.odds.moneyline_away,
        side,
        use_devig=True,
    )
    edge = model_p - implied
    return Leg(
        game=game,
        side=side,
        team_name=team.name,
        team_abbr=team.abbreviation,
        team_id=team.id,
        odds_american=odds,
        model_win_prob=model_p,
        implied_prob=implied,
        edge=edge,
        fair_odds=fair_american_from_prob(model_p),
    )


def get_all_legs(games: List[Game], elo: EloSystem) -> List[Leg]:
    legs: List[Leg] = []
    for g in games:
        for side in ("away", "home"):
            leg = annotate_leg(g, side, elo)
            if leg is not None:
                legs.append(leg)
    return legs


def get_plus_ev_legs(games: List[Game], elo: EloSystem) -> List[Leg]:
    legs = get_all_legs(games, elo)
    return [l for l in legs if l.edge > MIN_EDGE]


def _avg_edge(legs: List[Leg]) -> float:
    if not legs:
        return 0.0
    return sum(l.edge for l in legs) / len(legs)


def build_picks(games: List[Game], elo: EloSystem, n: int = 3) -> List[Pick]:
    """
    Build up to n independent +EV tips.
    Prefer singles (+200+), then 2-leg, then 3-leg parlays.
    Every leg must have edge > 0; combined American odds >= +200.
    Ranked by average edge.
    """
    plus_ev = sorted(get_plus_ev_legs(games, elo), key=lambda l: l.edge, reverse=True)
    if not plus_ev:
        return []

    picks: List[Pick] = []
    used_teams: Set[str] = set()
    used_games: Set[str] = set()

    def can_use(leg: Leg) -> bool:
        if leg.team_id in used_teams or leg.game.event_id in used_games:
            return False
        if _opp_id(leg) in used_teams:
            return False
        return True

    def mark_used(leg: Leg) -> None:
        used_teams.add(leg.team_id)
        used_teams.add(_opp_id(leg))
        used_games.add(leg.game.event_id)

    # 1. Singles that clear +200
    for leg in plus_ev:
        if len(picks) >= n:
            break
        if leg.odds_american >= MIN_ODDS and can_use(leg):
            edge_pts = edge_pp(leg.model_win_prob, leg.implied_prob)
            picks.append(
                Pick(
                    legs=[leg],
                    combined_odds=leg.odds_american,
                    score=leg.edge + 0.05,  # slight singles preference
                    label=(
                        f"Single: {leg.team_abbr} ML {leg.odds_american:+d} "
                        f"(edge {edge_pts:+.1f}pp)"
                    ),
                    combined_edge_pp=edge_pts,
                )
            )
            mark_used(leg)

    # 2. Independent 2-leg parlays
    if len(picks) < n:
        available = [l for l in plus_ev if can_use(l)]
        best_parlays = []
        for a, b in combinations(available, 2):
            if legs_overlap([a, b]):
                continue
            combined = combine_odds([a.odds_american, b.odds_american])
            if combined >= MIN_ODDS:
                sc = _avg_edge([a, b])
                best_parlays.append((sc, [a, b], combined))
        best_parlays.sort(reverse=True, key=lambda x: x[0])

        for sc, legs_pair, comb in best_parlays:
            if len(picks) >= n:
                break
            if any(not can_use(l) for l in legs_pair):
                continue
            label = " + ".join(
                f"{l.team_abbr} ({l.odds_american:+d})" for l in legs_pair
            )
            picks.append(
                Pick(
                    legs=legs_pair,
                    combined_odds=comb,
                    score=sc,
                    label=f"2-leg: {label} → {comb:+d}",
                    combined_edge_pp=sc * 100.0,
                )
            )
            for l in legs_pair:
                mark_used(l)

    # 3. Last resort: 3-leg parlays
    if len(picks) < n:
        available = [l for l in plus_ev if can_use(l)]
        best_parlays = []
        pool = available[:40]
        for combo in combinations(pool, 3):
            if legs_overlap(list(combo)):
                continue
            odds = [l.odds_american for l in combo]
            combined = combine_odds(odds)
            if combined >= MIN_ODDS:
                sc = _avg_edge(list(combo))
                best_parlays.append((sc, list(combo), combined))
        best_parlays.sort(reverse=True, key=lambda x: x[0])

        for sc, legs3, comb in best_parlays:
            if len(picks) >= n:
                break
            if any(not can_use(l) for l in legs3):
                continue
            label = " + ".join(f"{l.team_abbr} ({l.odds_american:+d})" for l in legs3)
            picks.append(
                Pick(
                    legs=legs3,
                    combined_odds=comb,
                    score=sc,
                    label=f"3-leg: {label} → {comb:+d}",
                    combined_edge_pp=sc * 100.0,
                )
            )
            for l in legs3:
                mark_used(l)

    return picks[:n]


def summarize_board(games: List[Game], elo: EloSystem) -> Dict:
    """Diagnostics for smoke tests / UI notes."""
    all_legs = get_all_legs(games, elo)
    plus = [l for l in all_legs if l.edge > MIN_EDGE]
    return {
        "games": len(games),
        "legs": len(all_legs),
        "plus_ev_legs": len(plus),
        "top_edges": sorted(plus, key=lambda l: l.edge, reverse=True)[:5],
    }
