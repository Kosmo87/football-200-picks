"""
NFL closing lines from nflverse — free, keyless, and 27 seasons deep.

Every question in this project has been answered on 20-to-100 game samples,
because ESPN deletes betting lines once a game finals. The nflverse project
publishes a single CSV holding 7,000+ NFL games back to 1999 with the closing
spread, total, both moneylines and the juice on every side. No key, no quota, no
credits: it is a file on GitHub.

That makes the paid-API question much smaller than it looked. This covers NFL
history outright; CollegeFootballData covers NCAAF for a free key; a paid tier
is only needed for player props and live lines.

The spread sign convention is verified against outcomes on load rather than
taken from documentation, because getting it backwards would silently invert
every backtest and still look plausible.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from history_data import CACHE_DIR

GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"


@dataclass
class NflGame:
    game_id: str
    season: int
    week: int
    date: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    neutral: bool
    spread_line: Optional[float]      # positive = home favoured (verified below)
    total_line: Optional[float]
    home_moneyline: Optional[int]
    away_moneyline: Optional[int]
    home_spread_odds: Optional[int]
    away_spread_odds: Optional[int]
    over_odds: Optional[int]
    under_odds: Optional[int]

    @property
    def margin(self) -> int:
        """Home minus away, the same orientation as spread_line."""
        return self.home_score - self.away_score

    @property
    def total(self) -> int:
        return self.home_score + self.away_score


def _f(v) -> Optional[float]:
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _i(v) -> Optional[int]:
    f = _f(v)
    return int(f) if f is not None else None


def load_games(refresh: bool = False) -> List[NflGame]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "nflverse_games.csv")
    if refresh or not os.path.exists(path):
        r = requests.get(GAMES_CSV, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
        r.raise_for_status()
        with open(path, "w") as f:
            f.write(r.text)

    with open(path) as f:
        rows = list(csv.DictReader(f))

    out = []
    for r in rows:
        hs, as_ = _i(r.get("home_score")), _i(r.get("away_score"))
        if hs is None or as_ is None:
            continue  # not played yet
        out.append(NflGame(
            game_id=r.get("game_id", ""),
            season=int(r["season"]), week=int(r.get("week") or 0),
            date=r.get("gameday", ""),
            home_team=r.get("home_team", ""), away_team=r.get("away_team", ""),
            home_score=hs, away_score=as_,
            neutral=str(r.get("location", "")).lower() == "neutral",
            spread_line=_f(r.get("spread_line")), total_line=_f(r.get("total_line")),
            home_moneyline=_i(r.get("home_moneyline")),
            away_moneyline=_i(r.get("away_moneyline")),
            home_spread_odds=_i(r.get("home_spread_odds")),
            away_spread_odds=_i(r.get("away_spread_odds")),
            over_odds=_i(r.get("over_odds")), under_odds=_i(r.get("under_odds")),
        ))
    return out


def verify_spread_orientation(games: List[NflGame]) -> Dict[str, float]:
    """
    Work out which way spread_line points, from the data.

    If positive means the home side is favoured, spread_line should track the
    home margin. If it means the opposite, the correlation is negative. A
    backtest built on the wrong assumption inverts every result and still looks
    entirely reasonable, so this is checked rather than assumed.
    """
    pairs = [(g.spread_line, g.margin) for g in games if g.spread_line is not None]
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs) / n
    vx = sum((x - mx) ** 2 for x in (p[0] for p in pairs)) / n
    vy = sum((y - my) ** 2 for y in (p[1] for p in pairs)) / n
    corr = cov / (vx ** 0.5 * vy ** 0.5)
    mae_as_is = sum(abs(x - y) for x, y in pairs) / n
    mae_flipped = sum(abs(-x - y) for x, y in pairs) / n
    return {
        "n": n, "corr": corr,
        "mae_positive_means_home_favoured": mae_as_is,
        "mae_flipped": mae_flipped,
        "orientation": "positive = home favoured" if mae_as_is < mae_flipped
                       else "positive = away favoured",
    }
