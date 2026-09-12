"""
Opponent-adjusted matchup ratings from play-by-play.

Elo answers "who is better" from final scores. This answers a narrower and more
specific question -- how many points per play does this offence generate, and
how many does this defence concede -- and splits both by pass and run, because
that is the dimension along which a matchup can be lopsided in a way two similar
overall ratings cannot express.

THE ADJUSTMENT IS THE POINT. Raw EPA per play rewards whoever drew the softest
schedule. A team's offence is therefore judged against the defences it actually
faced and those defences against the offences they faced, solved together rather
than in sequence. Concretely, every play is one row of

    epa  ~  offence(team) + defence(opponent) + home

and the whole system is solved at once by ridge regression. Ridge rather than
plain least squares for two reasons: the design matrix is rank deficient (only
the difference between an offence and a defence rating is identified, not their
level), and the penalty doubles as the shrinkage a small sample needs -- a team
with two games is pulled toward the league mean instead of being allowed to
lead the league on a fluke.

CARRY-OVER. Rosters and coaching staffs mostly survive the off-season, so last
season's ratings are real evidence about this one. Prior-season plays are
included at a reduced weight, and that weight is a parameter rather than an
assumption: matchup_test.py fits it against closing lines instead of guessing.

Nothing here is used by the live board. It has to beat the closing line first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Ridge penalty, in units of "plays". Larger shrinks small samples harder.
# 1500 is roughly one team-season of plays on one side of the ball, so a team
# with a full season of evidence is shrunk about halfway and a team with two
# games a great deal more.
DEFAULT_RIDGE = 1500.0

PLAY_TYPES = ("pass", "run")


@dataclass
class Ratings:
    """Points per play above league average, by team and phase."""

    offence: Dict[Tuple[str, str], float] = field(default_factory=dict)
    defence: Dict[Tuple[str, str], float] = field(default_factory=dict)
    home_edge: float = 0.0
    league_mean: Dict[str, float] = field(default_factory=dict)
    plays: Dict[str, int] = field(default_factory=dict)

    def off(self, team: str, play_type: str) -> float:
        return self.offence.get((team, play_type), 0.0)

    def deff(self, team: str, play_type: str) -> float:
        return self.defence.get((team, play_type), 0.0)


def solve_phase(plays: pd.DataFrame, ridge: float) -> Tuple[Dict[str, float], Dict[str, float], float]:
    """
    Ridge-solve epa ~ offence + defence + home for one play type.

    Returns (offence ratings, defence ratings, home edge), all as deviations
    from the league mean for that play type, in points per play. A positive
    defence rating means that defence ALLOWS more than average, so a good
    defence is negative -- kept in the natural sign of the regression rather
    than flipped, because every consumer below subtracts it.
    """
    teams = sorted(set(plays["posteam"]) | set(plays["defteam"]))
    idx = {t: i for i, t in enumerate(teams)}
    n, k = len(plays), len(teams)
    if n == 0 or k == 0:
        return {}, {}, 0.0

    # Design: [offence dummies | defence dummies | home], target is EPA centred.
    y = plays["epa"].to_numpy(dtype=float)
    mean = float(y.mean())
    y = y - mean

    rows = np.arange(n)
    off_col = plays["posteam"].map(idx).to_numpy()
    def_col = plays["defteam"].map(idx).to_numpy() + k
    home = (plays["posteam"] == plays["home_team"]).to_numpy(dtype=float)

    # X'X and X'y built directly: the matrix is all-indicator plus one dense
    # column, so accumulating the normal equations is far cheaper than forming
    # a 300k x 65 design.
    p = 2 * k + 1
    xtx = np.zeros((p, p))
    xty = np.zeros(p)
    w = plays["weight"].to_numpy(dtype=float) if "weight" in plays else np.ones(n)

    np.add.at(xtx, (off_col, off_col), w)
    np.add.at(xtx, (def_col, def_col), w)
    np.add.at(xtx, (off_col, def_col), w)
    np.add.at(xtx, (def_col, off_col), w)
    np.add.at(xtx, (off_col, p - 1), w * home)
    np.add.at(xtx, (p - 1, off_col), w * home)
    np.add.at(xtx, (def_col, p - 1), w * home)
    np.add.at(xtx, (p - 1, def_col), w * home)
    xtx[p - 1, p - 1] = float((w * home * home).sum())

    np.add.at(xty, off_col, w * y)
    np.add.at(xty, def_col, w * y)
    xty[p - 1] = float((w * home * y).sum())

    # Penalise the team terms, not the home term.
    pen = np.full(p, ridge)
    pen[p - 1] = 1e-6
    beta = np.linalg.solve(xtx + np.diag(pen), xty)

    offence = {t: float(beta[i]) for t, i in idx.items()}
    defence = {t: float(beta[i + k]) for t, i in idx.items()}
    return offence, defence, float(beta[p - 1])


def rate(
    plays: pd.DataFrame,
    season: int,
    week: int,
    prior_weight: float = 0.5,
    ridge: float = DEFAULT_RIDGE,
) -> Ratings:
    """
    Ratings from everything that had finished before (season, week).

    Walk-forward by construction: a play is only used to predict a game that
    kicked off after it. Getting this wrong is the single easiest way to build
    a model that looks brilliant and cannot bet.
    """
    current = plays[(plays.season == season) & (plays.week < week)]
    prior = plays[plays.season == season - 1] if prior_weight > 0 else plays.iloc[:0]

    frames = []
    if len(current):
        cur = current.copy()
        cur["weight"] = 1.0
        frames.append(cur)
    if len(prior):
        pr = prior.copy()
        pr["weight"] = prior_weight
        frames.append(pr)
    if not frames:
        return Ratings()
    data = pd.concat(frames, ignore_index=True)

    out = Ratings()
    for pt in PLAY_TYPES:
        sub = data[data.play_type == pt]
        if sub.empty:
            continue
        off, deff, home = solve_phase(sub, ridge)
        for t, v in off.items():
            out.offence[(t, pt)] = v
        for t, v in deff.items():
            out.defence[(t, pt)] = v
        out.home_edge = home
        out.league_mean[pt] = float(sub.epa.mean())
    for t, cnt in data[data.season == season].groupby("posteam").size().items():
        out.plays[t] = int(cnt)
    return out


def matchup_features(r: Ratings, home: str, away: str) -> Dict[str, float]:
    """
    The four numbers a game reduces to, plus the asymmetry between them.

    Each is "points per play this offence should generate against this
    defence": the offence's rating minus the defence's, so a good offence
    against a leaky defence is large and positive.
    """
    f = {}
    for pt in PLAY_TYPES:
        f[f"home_{pt}"] = r.off(home, pt) - r.deff(away, pt)
        f[f"away_{pt}"] = r.off(away, pt) - r.deff(home, pt)
    f["home_edge_pass"] = f["home_pass"] - f["away_pass"]
    f["home_edge_run"] = f["home_run"] - f["away_run"]
    # One overall number, weighted to the pass because roughly 58% of plays are
    # passes and passing EPA varies far more between teams than rushing does.
    f["home_edge"] = 0.58 * f["home_edge_pass"] + 0.42 * f["home_edge_run"]
    f["home_field"] = 1.0
    return f


FEATURE_COLS = ["home_edge_pass", "home_edge_run"]
