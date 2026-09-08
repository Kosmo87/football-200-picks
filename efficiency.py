"""
Opponent-adjusted offensive and defensive ratings from box scores.

Elo answers "who is better". A totals or spread model needs "how many points
will actually be scored", which is a different question and needs the scoring
itself rather than the result.

The adjustment is the whole point. Forty points against the worst defence in the
league is not the same evidence as forty against the best, and a raw scoring
average cannot tell them apart — it just rewards whoever drew the softest
schedule. Ratings are solved iteratively: a team's offence is judged against the
defences it actually faced, those defences are re-judged against the offences
they faced, and the pass repeats until the numbers stop moving.

Small samples are shrunk toward the league average, so a team with two games does
not outrank the field on a fluke.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from boxscores import TeamGame

# Games' worth of league-average prior mixed into every rating. Higher means
# more conservative early in a season.
SHRINK_GAMES = 4.0
ITERATIONS = 12


@dataclass
class Ratings:
    league_ppg: float                  # average points by one team in one game
    offense: Dict[str, float]          # points a team generates vs an average defence
    defense: Dict[str, float]          # points a team allows vs an average offence
    pace: Dict[str, float]             # plays per game
    league_pace: float
    home_edge: float                   # points added to the home side
    games: Dict[str, int]

    def off(self, t: str) -> float:
        return self.offense.get(t, self.league_ppg)

    def deff(self, t: str) -> float:
        return self.defense.get(t, self.league_ppg)

    def played(self, t: str) -> int:
        return self.games.get(t, 0)

    def expected_points(self, team: str, opp: str, is_home: bool, neutral: bool = False) -> float:
        """
        Multiplicative: a team's offence scaled by how leaky the defence is.

        Additive models let a great offence and a great defence cancel to the
        league mean; multiplying keeps the interaction, which is what a total
        is made of.
        """
        base = self.off(team) * (self.deff(opp) / self.league_ppg)
        if not neutral:
            base += self.home_edge / 2.0 if is_home else -self.home_edge / 2.0
        return max(base, 0.0)

    def expected_total(self, home: str, away: str, neutral: bool = False) -> float:
        return (self.expected_points(home, away, True, neutral)
                + self.expected_points(away, home, False, neutral))

    def expected_margin(self, home: str, away: str, neutral: bool = False) -> float:
        """Positive means the home side is favoured, in points."""
        return (self.expected_points(home, away, True, neutral)
                - self.expected_points(away, home, False, neutral))


def _shrink(total: float, n: float, prior: float, k: float = SHRINK_GAMES) -> float:
    return (total + prior * k) / (n + k) if (n + k) > 0 else prior


def build_ratings(rows: Iterable[TeamGame], shrink: float = SHRINK_GAMES) -> Ratings:
    rows = list(rows)
    if not rows:
        return Ratings(24.0, {}, {}, {}, 130.0, 2.0, {})

    lg_ppg = sum(r.points for r in rows) / len(rows)
    lg_pace = sum(r.plays for r in rows) / len(rows) if any(r.plays for r in rows) else 130.0

    # Home edge is solved after the ratings, not before: teams schedule weaker
    # non-conference opponents at home, so a raw home-minus-away average charges
    # that opponent quality to home field and roughly triples the number.
    home_edge = 0.0

    by_team: Dict[str, List[TeamGame]] = defaultdict(list)
    for r in rows:
        by_team[r.team_id].append(r)

    teams = list(by_team)
    offense = {t: lg_ppg for t in teams}
    defense = {t: lg_ppg for t in teams}

    for _ in range(ITERATIONS):
        new_off, new_def = {}, {}
        for t in teams:
            games = by_team[t]
            # Scored, with each game's opponent defence divided out.
            o_tot = sum(
                r.points / max(defense.get(r.opp_id, lg_ppg) / lg_ppg, 0.35)
                for r in games
            )
            # Allowed, with each game's opponent offence divided out.
            d_tot = sum(
                r.opp_points / max(offense.get(r.opp_id, lg_ppg) / lg_ppg, 0.35)
                for r in games
            )
            n = float(len(games))
            new_off[t] = _shrink(o_tot, n, lg_ppg, shrink)
            new_def[t] = _shrink(d_tot, n, lg_ppg, shrink)

        # Re-centre so the average team sits exactly at the league mean; without
        # this the iteration is free to drift both scales together.
        mo = sum(new_off.values()) / len(new_off)
        md = sum(new_def.values()) / len(new_def)
        offense = {t: v * lg_ppg / mo for t, v in new_off.items()}
        defense = {t: v * lg_ppg / md for t, v in new_def.items()}

    # With opponent strength already divided out, whatever margin is left on
    # home sides is home field. Averaging the residual isolates it.
    residuals = []
    for r in rows:
        if r.neutral or not r.is_home:
            continue
        exp_home = offense.get(r.team_id, lg_ppg) * (defense.get(r.opp_id, lg_ppg) / lg_ppg)
        exp_away = offense.get(r.opp_id, lg_ppg) * (defense.get(r.team_id, lg_ppg) / lg_ppg)
        residuals.append((r.points - r.opp_points) - (exp_home - exp_away))
    home_edge = sum(residuals) / len(residuals) if residuals else 2.0

    pace = {
        t: _shrink(sum(r.plays for r in gs), float(len(gs)), lg_pace, shrink)
        for t, gs in by_team.items()
    }
    return Ratings(
        league_ppg=lg_ppg, offense=offense, defense=defense, pace=pace,
        league_pace=lg_pace, home_edge=home_edge,
        games={t: len(gs) for t, gs in by_team.items()},
    )
