"""Simple Elo / power ratings for NFL and NCAAF FBS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class EloConfig:
    initial: float = 1500.0
    k_factor: float = 20.0
    home_field: float = 55.0  # Elo points added to home team
    scale: float = 400.0


NFL_ELO = EloConfig(initial=1500.0, k_factor=20.0, home_field=55.0)
# College has larger home effects and more variance
NCAAF_ELO = EloConfig(initial=1500.0, k_factor=24.0, home_field=65.0)


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


@dataclass
class EloSystem:
    config: EloConfig
    ratings: Dict[str, float] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    abbrs: Dict[str, str] = field(default_factory=dict)
    games_played: Dict[str, int] = field(default_factory=dict)

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

    @staticmethod
    def expected_score(rating_a: float, rating_b: float, scale: float = 400.0) -> float:
        """P(A beats B) from Elo difference (A - B)."""
        return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / scale))

    def win_prob_home(self, home_id: str, away_id: str) -> float:
        """Model P(home wins) including home-field advantage."""
        home_r = self.rating(home_id) + self.config.home_field
        away_r = self.rating(away_id)
        return self.expected_score(home_r, away_r, self.config.scale)

    def win_prob_side(self, home_id: str, away_id: str, side: str) -> float:
        p_home = self.win_prob_home(home_id, away_id)
        return p_home if side == "home" else (1.0 - p_home)

    def update_game(self, game: CompletedGame) -> None:
        self.ensure_team(game.home_id, game.home_name, game.home_abbr)
        self.ensure_team(game.away_id, game.away_name, game.away_abbr)

        # Neutral-site not detected here; apply HFA for all scoreboard home teams.
        home_r = self.rating(game.home_id) + self.config.home_field
        away_r = self.rating(game.away_id)
        exp_home = self.expected_score(home_r, away_r, self.config.scale)

        if game.home_score > game.away_score:
            score_home = 1.0
        elif game.home_score < game.away_score:
            score_home = 0.0
        else:
            score_home = 0.5

        k = self.config.k_factor
        delta = k * (score_home - exp_home)
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
