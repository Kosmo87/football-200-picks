"""Elo / power ratings for NFL and NCAAF FBS.

v0.8: margin-of-victory scaling, neutral-site handling, preseason down-weighting,
and prior-season carryover with regression to the mean. Carryover also feeds an
"effective sample" so early-season teams aren't treated as unknowns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class EloConfig:
    initial: float = 1500.0
    k_factor: float = 20.0
    home_field: float = 55.0  # Elo points added to home team
    scale: float = 400.0
    # Fraction of a prior-season rating carried into the new season; the rest
    # regresses to `initial`. 538 used ~0.75 for the NFL.
    carry: float = 0.75
    # Preseason games are starters-lite: apply a fraction of K.
    preseason_k_mult: float = 0.40
    # Cap on how much prior-season credit counts toward the sample gate.
    max_prior_credit: float = 4.0
    # Each prior-season game is worth this much of a current-season game.
    prior_credit_per_game: float = 0.35


NFL_ELO = EloConfig(
    initial=1500.0, k_factor=20.0, home_field=55.0, carry=0.75
)
# College has larger home effects, more variance, and stickier talent gaps
NCAAF_ELO = EloConfig(
    initial=1500.0, k_factor=24.0, home_field=65.0, carry=0.72
)


@dataclass
class CompletedGame:
    event_id: str
    date: str
    home_id: str
    away_id: str
    home_name: str
    away_name: str
    home_abbr: str
    away_abbr: str
    home_score: int
    away_score: int
    neutral: bool = False
    season_type: int = 2  # 1 = preseason, 2 = regular, 3 = postseason


def mov_multiplier(margin: int, elo_diff_winner: float) -> float:
    """
    538-style margin-of-victory multiplier.

    Blowouts move ratings more, but the autocorrelation correction damps wins by
    teams that were already heavy favorites, so beating up a bad team repeatedly
    does not run a rating away.
    """
    m = abs(int(margin))
    if m <= 0:
        return 1.0
    return math.log(m + 1.0) * (2.2 / ((elo_diff_winner * 0.001) + 2.2))


@dataclass
class EloSystem:
    config: EloConfig
    ratings: Dict[str, float] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    abbrs: Dict[str, str] = field(default_factory=dict)
    games_played: Dict[str, int] = field(default_factory=dict)
    # Games a team played in the prior season, used for sample credit only
    prior_games: Dict[str, int] = field(default_factory=dict)
    seeded: bool = False

    def rating(self, team_id: str) -> float:
        return self.ratings.get(team_id, self.config.initial)

    def ensure_team(self, team_id: str, name: str = "", abbr: str = "") -> None:
        if team_id not in self.ratings:
            self.ratings[team_id] = self.config.initial
            self.games_played[team_id] = 0
        if name:
            self.names[team_id] = name
        if abbr:
            self.abbrs[team_id] = abbr

    # ---- prior-season carryover -------------------------------------------

    def seed_from(self, prior: "EloSystem", carry: Optional[float] = None) -> "EloSystem":
        """
        Start this season from last season's ratings, regressed to the mean.

        new = initial + carry * (prior_rating - initial)

        Teams with no prior rating simply start at `initial`. Prior game counts
        are retained so `effective_sample` can credit them.
        """
        c = self.config.carry if carry is None else carry
        base = self.config.initial
        for team_id, prior_rating in prior.ratings.items():
            self.ratings[team_id] = base + c * (prior_rating - base)
            self.games_played.setdefault(team_id, 0)
            self.prior_games[team_id] = prior.games_played.get(team_id, 0)
            if team_id in prior.names:
                self.names[team_id] = prior.names[team_id]
            if team_id in prior.abbrs:
                self.abbrs[team_id] = prior.abbrs[team_id]
        self.seeded = True
        return self

    def prior_credit(self, team_id: str) -> float:
        """Sample-gate credit earned from prior-season games (0 if unseeded)."""
        if not self.seeded:
            return 0.0
        n = self.prior_games.get(team_id, 0)
        if n <= 0:
            return 0.0
        return min(n * self.config.prior_credit_per_game, self.config.max_prior_credit)

    def effective_sample(self, team_id: str) -> float:
        """Current-season games plus capped prior-season credit."""
        return self.games_played.get(team_id, 0) + self.prior_credit(team_id)

    # ---- probabilities -----------------------------------------------------

    @staticmethod
    def expected_score(rating_a: float, rating_b: float, scale: float = 400.0) -> float:
        """P(A beats B) from Elo difference (A - B)."""
        return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / scale))

    def win_prob_home(self, home_id: str, away_id: str, neutral: bool = False) -> float:
        """Model P(home wins), including home-field advantage unless neutral."""
        hfa = 0.0 if neutral else self.config.home_field
        home_r = self.rating(home_id) + hfa
        away_r = self.rating(away_id)
        return self.expected_score(home_r, away_r, self.config.scale)

    def win_prob_side(
        self, home_id: str, away_id: str, side: str, neutral: bool = False
    ) -> float:
        p_home = self.win_prob_home(home_id, away_id, neutral=neutral)
        return p_home if side == "home" else (1.0 - p_home)

    # ---- updates -----------------------------------------------------------

    def update_game(self, game: CompletedGame) -> None:
        self.ensure_team(game.home_id, game.home_name, game.home_abbr)
        self.ensure_team(game.away_id, game.away_name, game.away_abbr)

        hfa = 0.0 if game.neutral else self.config.home_field
        home_r = self.rating(game.home_id) + hfa
        away_r = self.rating(game.away_id)
        exp_home = self.expected_score(home_r, away_r, self.config.scale)

        margin = game.home_score - game.away_score
        if margin > 0:
            score_home = 1.0
            elo_diff_winner = home_r - away_r
        elif margin < 0:
            score_home = 0.0
            elo_diff_winner = away_r - home_r
        else:
            score_home = 0.5
            elo_diff_winner = 0.0

        k = self.config.k_factor
        if game.season_type == 1:
            k *= self.config.preseason_k_mult

        delta = k * mov_multiplier(margin, elo_diff_winner) * (score_home - exp_home)
        self.ratings[game.home_id] = self.rating(game.home_id) + delta
        self.ratings[game.away_id] = self.rating(game.away_id) - delta
        self.games_played[game.home_id] = self.games_played.get(game.home_id, 0) + 1
        self.games_played[game.away_id] = self.games_played.get(game.away_id, 0) + 1

    def build(self, games: Iterable[CompletedGame]) -> "EloSystem":
        # Chronological order matters
        ordered = sorted(games, key=lambda g: (g.date or "", g.event_id or ""))
        for g in ordered:
            self.update_game(g)
        return self

    def top(self, n: int = 10) -> List[Tuple[str, float, int]]:
        items = [
            (tid, self.ratings[tid], self.games_played.get(tid, 0))
            for tid in self.ratings
        ]
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:n]


def config_for_league(league: str) -> EloConfig:
    if league.upper() == "NFL":
        return NFL_ELO
    return NCAAF_ELO


def build_league_elo(
    current_games: Iterable[CompletedGame],
    prior_games: Optional[Iterable[CompletedGame]] = None,
    league: str = "NFL",
) -> EloSystem:
    """
    Build a league's ratings, seeding from the prior season when available.

    Prior-season games run through their own Elo system first; those ratings are
    regressed to the mean and become this season's starting point.
    """
    cfg = config_for_league(league)
    system = EloSystem(config=cfg)
    prior_list = list(prior_games or [])
    if prior_list:
        prior_system = EloSystem(config=cfg).build(prior_list)
        system.seed_from(prior_system)
    return system.build(current_games)
