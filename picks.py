"""+EV tip selection: Elo model vs implied odds, confidence-gated 1–5 leg picks."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

from elo import EloSystem
from espn import Game
from odds import (
    combine_odds,
    edge_pp,
    fair_american_from_prob,
    side_implied_prob,
)

MIN_COMBINED_ODDS = 200
DEFAULT_MAX_SINGLE_LEG_ODDS = 600
DEFAULT_MIN_EDGE_PP = 5.0
DEFAULT_MIN_SAMPLE_NFL = 3
DEFAULT_MIN_SAMPLE_NCAAF = 4
DEFAULT_MAX_PARLAY_LEGS = 5


@dataclass
class ConfidenceConfig:
    """Tunable confidence / selection gates (UI sidebar)."""

    min_edge_pp: float = DEFAULT_MIN_EDGE_PP
    min_sample_games: int = DEFAULT_MIN_SAMPLE_NFL
    max_single_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS
    max_parlay_legs: int = DEFAULT_MAX_PARLAY_LEGS  # 1–5 (1 = singles only)
    min_combined_odds: int = MIN_COMBINED_ODDS
    high_confidence_mode: bool = False

    def apply_high_confidence_preset(self, league: str = "NFL") -> "ConfidenceConfig":
        """Raise gates and prefer stacking shorter prices (more legs allowed)."""
        self.high_confidence_mode = True
        self.min_edge_pp = max(self.min_edge_pp, 7.5)
        league_u = (league or "NFL").upper()
        floor = 5 if league_u == "NFL" else 6
        self.min_sample_games = max(self.min_sample_games, floor)
        self.max_single_leg_odds = min(self.max_single_leg_odds, 350)
        self.max_parlay_legs = max(self.max_parlay_legs, 5)
        return self


def default_min_sample(league: str) -> int:
    return DEFAULT_MIN_SAMPLE_NFL if (league or "").upper() == "NFL" else DEFAULT_MIN_SAMPLE_NCAAF


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
    sample_games: int = 0  # min completed games used in Elo for both teams
    team_sample: int = 0
    opp_sample: int = 0
    confidence: float = 0.0  # 0–100
    confidence_label: str = "Low"


@dataclass
class Pick:
    legs: List[Leg]
    combined_odds: int
    score: float  # ranking score
    label: str = ""
    combined_edge_pp: float = 0.0
    avg_confidence: float = 0.0
    confidence_label: str = "Low"


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


def confidence_label_from_score(score: float) -> str:
    if score >= 70:
        return "High"
    if score >= 40:
        return "Med"
    return "Low"


def compute_confidence(
    edge: float,
    odds_american: int,
    sample_games: int,
) -> Tuple[float, str]:
    """
    Confidence 0–100 from sample depth + edge size + price shortness.

    Formula (documented in README):
      sample_score  = min(sample_games / 8, 1) * 40
      edge_score    = min(max(edge_pp, 0) / 15, 1) * 35
      short_score   = f(odds) * 25
        dogs  (+): max(0, 1 - (odds - 100) / 500)   # +100→1.0, +600→0
        favs  (−): max(0, 1 - (|odds| - 100) / 200) # −100→1.0, −300→0
      confidence    = sample_score + edge_score + short_score

    Labels: High ≥70, Med ≥40, else Low.
    """
    epp = edge * 100.0
    sample_score = min(max(sample_games, 0) / 8.0, 1.0) * 40.0
    edge_score = min(max(epp, 0.0) / 15.0, 1.0) * 35.0
    if odds_american >= 0:
        short_frac = max(0.0, 1.0 - (odds_american - 100) / 500.0)
    else:
        short_frac = max(0.0, 1.0 - (abs(odds_american) - 100) / 200.0)
    short_score = short_frac * 25.0
    score = round(sample_score + edge_score + short_score, 1)
    score = max(0.0, min(100.0, score))
    return score, confidence_label_from_score(score)


def annotate_leg(
    game: Game,
    side: str,
    elo: EloSystem,
    max_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS,
) -> Optional[Leg]:
    team = game.home if side == "home" else game.away
    odds = game.odds.moneyline_home if side == "home" else game.odds.moneyline_away
    # Favorites down to -350 for multi-leg stacks; long dogs capped by max_leg_odds
    if odds is None or not (-350 <= odds <= max_leg_odds):
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
    opp = game.away if side == "home" else game.home
    team_sample = elo.games_played.get(team.id, 0)
    opp_sample = elo.games_played.get(opp.id, 0)
    sample = min(team_sample, opp_sample)
    conf, conf_label = compute_confidence(edge, odds, sample)
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
        sample_games=sample,
        team_sample=team_sample,
        opp_sample=opp_sample,
        confidence=conf,
        confidence_label=conf_label,
    )


def get_all_legs(
    games: List[Game],
    elo: EloSystem,
    max_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS,
) -> List[Leg]:
    legs: List[Leg] = []
    for g in games:
        for side in ("away", "home"):
            leg = annotate_leg(g, side, elo, max_leg_odds=max_leg_odds)
            if leg is not None:
                legs.append(leg)
    return legs


def passes_confidence_gates(leg: Leg, cfg: ConfidenceConfig) -> bool:
    if leg.edge * 100.0 < cfg.min_edge_pp:
        return False
    if leg.sample_games < cfg.min_sample_games:
        return False
    if leg.odds_american > cfg.max_single_leg_odds:
        return False
    # Still require strictly +EV
    if leg.edge <= 0:
        return False
    return True


def get_plus_ev_legs(
    games: List[Game],
    elo: EloSystem,
    cfg: Optional[ConfidenceConfig] = None,
) -> List[Leg]:
    cfg = cfg or ConfidenceConfig()
    legs = get_all_legs(games, elo, max_leg_odds=cfg.max_single_leg_odds)
    return [l for l in legs if passes_confidence_gates(l, cfg)]


def _avg_edge(legs: List[Leg]) -> float:
    if not legs:
        return 0.0
    return sum(l.edge for l in legs) / len(legs)


def _avg_confidence(legs: List[Leg]) -> float:
    if not legs:
        return 0.0
    return sum(l.confidence for l in legs) / len(legs)


def _rank_key(legs: List[Leg], combined: int) -> Tuple[float, float, float, int]:
    """
    Prefer higher avg confidence, higher avg edge, fewer legs, then
    milder combined odds (not juiciest lottery).
    """
    avg_c = _avg_confidence(legs)
    avg_e = _avg_edge(legs)
    n = len(legs)
    # Soft preference for fewer legs when confidence/edge comparable
    fewer_bonus = (6 - n) * 0.5
    # Mild preference against huge combined prices
    odds_penalty = max(0, combined - 500) / 5000.0
    score = avg_c + avg_e * 100.0 + fewer_bonus - odds_penalty
    return (score, avg_c, avg_e, -n)


def _make_pick(legs: List[Leg], combined: int) -> Pick:
    avg_e = _avg_edge(legs)
    avg_c = _avg_confidence(legs)
    score_tuple = _rank_key(legs, combined)
    n = len(legs)
    if n == 1:
        leg = legs[0]
        epp = edge_pp(leg.model_win_prob, leg.implied_prob)
        label = (
            f"Single: {leg.team_abbr} ML {leg.odds_american:+d} "
            f"(edge {epp:+.1f}pp, conf {leg.confidence:.0f}/{leg.confidence_label})"
        )
        combined_edge = epp
    else:
        parts = " + ".join(f"{l.team_abbr} ({l.odds_american:+d})" for l in legs)
        label = f"{n}-leg: {parts} → {combined:+d}"
        combined_edge = avg_e * 100.0
    return Pick(
        legs=list(legs),
        combined_odds=combined,
        score=score_tuple[0],
        label=label,
        combined_edge_pp=combined_edge,
        avg_confidence=avg_c,
        confidence_label=confidence_label_from_score(avg_c),
    )


def _candidate_combos(
    available: List[Leg],
    n_legs: int,
    min_combined: int,
    pool_cap: int,
) -> List[Tuple[float, List[Leg], int]]:
    """Enumerate non-overlapping n-leg combos that clear min_combined odds."""
    if n_legs < 1 or len(available) < n_legs:
        return []
    # Prefer higher confidence then edge when capping the search pool
    ranked = sorted(
        available,
        key=lambda l: (l.confidence, l.edge),
        reverse=True,
    )
    pool = ranked[:pool_cap]
    out: List[Tuple[float, List[Leg], int]] = []
    for combo in combinations(pool, n_legs):
        legs = list(combo)
        if legs_overlap(legs):
            continue
        combined = combine_odds([l.odds_american for l in legs])
        if combined >= min_combined:
            sc = _rank_key(legs, combined)[0]
            out.append((sc, legs, combined))
    out.sort(reverse=True, key=lambda x: x[0])
    return out


def build_picks(
    games: List[Game],
    elo: EloSystem,
    n: int = 3,
    cfg: Optional[ConfidenceConfig] = None,
) -> List[Pick]:
    """
    Build up to n independent +EV tips under confidence gates.

    Prefer fewer legs when they already clear +200 with high confidence;
    otherwise fill with 3/4/5-leg parlays from shorter +EV legs.
    Every leg must pass gates (edge, sample, max odds) and edge > 0.
    Ranked by avg confidence + avg edge (not juiciest combined odds).
    """
    cfg = cfg or ConfidenceConfig()
    max_legs = max(1, min(5, int(cfg.max_parlay_legs)))
    min_combined = int(cfg.min_combined_odds)

    plus_ev = sorted(
        get_plus_ev_legs(games, elo, cfg),
        key=lambda l: (l.confidence, l.edge),
        reverse=True,
    )
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

    # Pool size caps grow slightly for smaller n_legs
    pool_caps = {1: 80, 2: 50, 3: 40, 4: 28, 5: 22}

    for n_legs in range(1, max_legs + 1):
        if len(picks) >= n:
            break
        available = [l for l in plus_ev if can_use(l)]
        if len(available) < n_legs:
            continue

        if n_legs == 1:
            # Singles that clear min combined (+200)
            singles = [
                l
                for l in available
                if l.odds_american >= min_combined and can_use(l)
            ]
            singles.sort(key=lambda l: (l.confidence, l.edge), reverse=True)
            for leg in singles:
                if len(picks) >= n:
                    break
                if not can_use(leg):
                    continue
                picks.append(_make_pick([leg], leg.odds_american))
                mark_used(leg)
            continue

        candidates = _candidate_combos(
            available,
            n_legs,
            min_combined,
            pool_cap=pool_caps.get(n_legs, 25),
        )
        for _sc, legs_combo, comb in candidates:
            if len(picks) >= n:
                break
            if any(not can_use(l) for l in legs_combo):
                continue
            picks.append(_make_pick(legs_combo, comb))
            for l in legs_combo:
                mark_used(l)

    # Re-rank selected picks for display (confidence + edge)
    picks.sort(key=lambda p: (p.avg_confidence, p.combined_edge_pp), reverse=True)
    return picks[:n]


def summarize_board(
    games: List[Game],
    elo: EloSystem,
    cfg: Optional[ConfidenceConfig] = None,
) -> Dict:
    """Diagnostics for smoke tests / UI notes."""
    cfg = cfg or ConfidenceConfig()
    all_legs = get_all_legs(games, elo, max_leg_odds=cfg.max_single_leg_odds)
    gated = [l for l in all_legs if passes_confidence_gates(l, cfg)]
    plus_raw = [l for l in all_legs if l.edge > 0]
    return {
        "games": len(games),
        "legs": len(all_legs),
        "plus_ev_legs": len(plus_raw),
        "gated_legs": len(gated),
        "top_edges": sorted(gated or plus_raw, key=lambda l: l.edge, reverse=True)[:5],
        "cfg": {
            "min_edge_pp": cfg.min_edge_pp,
            "min_sample_games": cfg.min_sample_games,
            "max_single_leg_odds": cfg.max_single_leg_odds,
            "max_parlay_legs": cfg.max_parlay_legs,
            "min_combined_odds": cfg.min_combined_odds,
            "high_confidence_mode": cfg.high_confidence_mode,
        },
    }
