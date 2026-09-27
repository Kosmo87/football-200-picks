"""
Back-test the NFL games where Super Bowl futures and the moneyline disagree,
on every season with both, betting either side.

RESULT (2006-2025, 2026-09-27): the moneyline favourite LOSES in these games
until midseason. Weeks 1-9, 513 games, the futures side (the underdog) returns
+14.8% per bet +/- 5.6, positive in 14 of 20 seasons; from week 10, when a
preseason price is stale, the favourite is right and the futures side -5.5%.
The week buckets were chosen once and not swept. It is a paper candidate, not
a bet: 2022 and 2025 lost, and 2026 opened 0-2 (-$200) on it.

The futures are the LAST PRESEASON Super Bowl price in the sportsoddshistory
archive (now at covers.com), one book-consensus column per season, taken in
late July to early September. There are no in-season snapshots, so a team is
ranked once, before week 1, and never re-ranked. That is weaker than
futures_h2h.py, which re-ranks from our own daily captures: by week 10 a
preseason price is stale, and more games disagree with the line because of
it. The table is split by week for that reason -- weeks 1-4 are the part
closest to what the live board does.

Moneylines are nflverse closing prices (cache/nflverse_games.csv), 2006 on.

    python futures_history.py
"""

from __future__ import annotations

import io
import json
import os
from collections import defaultdict
from typing import Dict, Optional

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
URL = "https://www.covers.com/sportsoddshistory/nfl-main/?y={year}&sa=nfl&a=sb"
MONTHS = ("Jul", "Aug", "Sep")

NAMES = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "Seattle Seahawks": "SEA", "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Oakland Raiders": "OAK", "Las Vegas Raiders": "LV",
    "San Diego Chargers": "SD", "Los Angeles Chargers": "LAC", "St. Louis Rams": "STL",
    "Los Angeles Rams": "LA", "Washington Redskins": "WAS", "Washington Football Team": "WAS",
    "Washington Commanders": "WAS", "Washington": "WAS",
}


def _implied(a: float) -> float:
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def preseason(year: int) -> Optional[Dict[str, float]]:
    """abbr -> de-vigged Super Bowl probability from the last Jul-Sep column."""
    path = os.path.join(CACHE, f"sb_futures_{year}.json")
    if os.path.exists(path):
        return json.load(open(path)) or None
    html = requests.get(URL.format(year=year), headers={"User-Agent": "Mozilla/5.0"},
                        timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    cols = list(t.columns.get_level_values(1))
    t.columns = cols
    pre = [c for c in cols if str(c)[:3] in MONTHS]
    out: Dict[str, float] = {}
    if pre:
        col = pre[-1]
        raw = {}
        for team, price in zip(t["Team"], t[col]):
            abbr = NAMES.get(str(team).strip())
            try:
                raw[abbr] = _implied(float(str(price).replace("+", "").replace(",", "")))
            except ValueError:
                continue
        raw.pop(None, None)
        tot = sum(raw.values())
        out = {k: v / tot for k, v in raw.items()} if len(raw) >= 28 else {}
        out = dict(out, _column=col) if out else {}
    json.dump(out, open(path, "w"))
    return out or None


def _profit(a: float) -> float:
    return a / 100 if a > 0 else 100 / -a


def main() -> int:
    g = pd.read_csv(os.path.join(CACHE, "nflverse_games.csv"))
    g = g[(g.game_type == "REG") & g.home_moneyline.notna() & g.result.notna()]
    by = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0, "dog": 0.0})
    seasons = []
    for year in sorted(g.season.unique()):
        fut = preseason(int(year))
        if not fut:
            continue
        seasons.append((int(year), fut["_column"]))
        # The snapshot is sometimes dated after the opener (2019: Sep 8, three
        # days after GB @ CHI). A game played before it is graded by a price
        # that already knew the result, so it is left out.
        snap = pd.Timestamp(f"{fut['_column']} {int(year)}")
        for r in g[(g.season == year) & (pd.to_datetime(g.gameday) > snap)].itertuples():
            ph, pa = fut.get(r.home_team), fut.get(r.away_team)
            if ph is None or pa is None or r.home_moneyline == r.away_moneyline:
                continue
            fav_home = r.home_moneyline < r.away_moneyline
            if (ph >= pa) == fav_home:
                continue                      # futures and line agree
            if r.result == 0:
                continue                      # tie: stake returned
            price = r.home_moneyline if fav_home else r.away_moneyline
            dog_price = r.away_moneyline if fav_home else r.home_moneyline
            won = (r.result > 0) == fav_home
            net = 100 * _profit(price) if won else -100
            dog = -100 if won else 100 * _profit(dog_price)
            wk = "1-4" if r.week <= 4 else ("5-9" if r.week <= 9 else "10+")
            for key in (("season", int(year)), ("week", wk), ("all", "")):
                b = by[key]
                b["n"] += 1; b["w"] += won; b["net"] += net; b["dog"] += dog

    print("NFL: games where preseason Super Bowl odds and the closing moneyline")
    print("disagree. $100 a game on the moneyline FAVOURITE, or on the FUTURES side")
    print("(which is the underdog, at its plus price). W-L is the favourite's.\n")
    print(f"{'season':>6} {'snapshot':>9} {'games':>6} {'W-L':>8} {'win%':>6} {'fav net':>8} {'fut net':>8}")
    for year, col in seasons:
        b = by[("season", year)]
        if b["n"]:
            print(f"{year:>6} {col:>9} {b['n']:>6} {b['w']:>4}-{b['n'] - b['w']:<3} "
                  f"{b['w'] / b['n'] * 100:5.1f}% {b['net']:>+8,.0f} {b['dog']:>+8,.0f}")
    print(f"\n{'weeks':>6} {'games':>16} {'W-L':>8} {'win%':>6} {'fav net':>8} {'per bet':>8}"
          f" {'fut net':>8} {'per bet':>8}")
    for wk in ("1-4", "5-9", "10+", ""):
        b = by[("week", wk)] if wk else by[("all", "")]
        print(f"{wk or 'all':>6} {b['n']:>16} {b['w']:>4}-{b['n'] - b['w']:<3} "
              f"{b['w'] / b['n'] * 100:5.1f}% {b['net']:>+8,.0f} {b['net'] / b['n']:>+7.1f}%"
              f" {b['dog']:>+8,.0f} {b['dog'] / b['n']:>+7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
