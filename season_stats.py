"""
Team season totals from ESPN's core API, including the defensive detail that
box scores leave out.

The per-game box score carries points, yards, turnovers and time of possession.
It does not carry sacks, hurries, tackles for loss or passes defended, and those
are exactly the columns that separate a defence that gets stops from one that
happens to face bad offences. One request per team per season.

Season totals are a schedule-weighted average, so they are a starting point for
matchup work rather than a rating. Opponent adjustment still belongs to
efficiency.py; this fills in the categories that module cannot see.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
from typing import Dict, List, Optional

from espn import HEADERS, fetch_scoreboard
from history_data import CACHE_DIR, all_fbs_ids, fbs_team_ids

CORE = {
    "NCAAF": ("http://sports.core.api.espn.com/v2/sports/football/leagues/"
              "college-football"),
    "NFL": "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl",
}

# The categories worth keeping. 'general' and 'miscellaneous' are mostly
# duplicates of the others with different names.
KEEP = ("passing", "rushing", "receiving", "defensive",
        "defensiveInterceptions", "scoring", "kicking", "returning", "punting")


def team_season_stats(league: str, season: int, team_id: str) -> Optional[dict]:
    url = f"{CORE[league]}/seasons/{season}/types/2/teams/{team_id}/statistics"
    try:
        data = fetch_scoreboard(url, retries=2)
    except Exception:
        return None
    cats = (data.get("splits") or {}).get("categories") or []
    if not cats:
        return None

    out: Dict[str, Dict[str, float]] = {}
    for c in cats:
        name = c.get("name")
        if name not in KEEP:
            continue
        vals = {}
        for s in c.get("stats") or []:
            v = s.get("value")
            if v is None:
                continue
            try:
                vals[s.get("name")] = float(v)
            except (TypeError, ValueError):
                continue
        if vals:
            out[name] = vals
    if not out:
        return None
    return {"team_id": team_id, "season": season, "league": league, "categories": out}


def load_season_stats(
    league: str, season: int, refresh: bool = False
) -> List[dict]:
    path = os.path.join(CACHE_DIR, f"teamstats_{league}_{season}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    if league == "NCAAF":
        team_ids = sorted(fbs_team_ids(season))
    else:
        team_ids = [str(i) for i in range(1, 35)]
    print(f"  fetching team season stats: {league} {season}, {len(team_ids)} teams…",
          flush=True)

    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for r in ex.map(
            lambda t: team_season_stats(league, season, t), team_ids) if r]

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f)
    print(f"  cached {len(rows)} team-seasons", flush=True)
    return rows


def defensive_table(rows: List[dict], names: Optional[Dict[str, str]] = None) -> List[dict]:
    """Flatten the defensive columns most useful for matchup work."""
    want = ("sacks", "tacklesForLoss", "hurries", "passesDefended",
            "interceptions", "totalTackles", "defensiveTouchdowns")
    out = []
    for r in rows:
        d = r["categories"].get("defensive", {})
        di = r["categories"].get("defensiveInterceptions", {})
        row = {"team_id": r["team_id"], "name": (names or {}).get(r["team_id"], r["team_id"])}
        for w in want:
            row[w] = d.get(w, di.get(w))
        out.append(row)
    return out
