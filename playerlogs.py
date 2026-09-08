"""
Per-game player statlines for whole seasons, cached.

Two ways to get this from ESPN. The per-athlete gamelog endpoint works but costs
one request per player per season -- ten thousand or more for a college season.
A game summary returns every player who appeared, so the same coverage costs one
request per game. That is the route taken here, and it is the same call the box
score loader already makes.

Season totals are derived by summing, never fetched separately, so the totals can
never disagree with the games they came from. For props the per-game rows matter
more than the totals anyway: "over 240.5 passing yards" is a question about the
distribution of his games, not his average.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from archive import _player_lines
from boxscores import SUMMARY
from espn import fetch_scoreboard
from history_data import CACHE_DIR, load_season

# Stats worth summing across a season. Everything else is a rate or a "longest",
# which cannot be added up and is recomputed from the totals instead.
COUNTING = {
    "completions", "passingAttempts", "passingYards", "passingTouchdowns",
    "interceptions", "rushingAttempts", "rushingYards", "rushingTouchdowns",
    "receptions", "receivingYards", "receivingTouchdowns",
    "sacks", "totalTackles", "soloTackles", "assistTackles",
    "fumbles", "fumblesLost", "fumblesRecovered",
}


def _num(v) -> Optional[float]:
    """ESPN mixes '31/47', '1,204', '-' and plain numbers in the same columns."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _explode(stats: Dict[str, str]) -> Dict[str, float]:
    """Flatten combined columns so 'completions/passingAttempts' becomes two."""
    out: Dict[str, float] = {}
    for key, raw in stats.items():
        if "/" in key and isinstance(raw, str) and "/" in raw:
            names = key.split("/")
            parts = raw.split("/")
            if len(names) == len(parts):
                for n, p in zip(names, parts):
                    v = _num(p)
                    if v is not None:
                        out[n] = v
                continue
        v = _num(raw)
        if v is not None:
            out[key] = v
    return out


def load_player_logs(league: str, season: int, refresh: bool = False) -> List[dict]:
    path = os.path.join(CACHE_DIR, f"players_{league}_{season}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    games = load_season(league, season)
    print(f"  fetching player logs: {league} {season}, {len(games)} games…", flush=True)

    def one(g):
        try:
            payload = fetch_scoreboard(f"{SUMMARY[league]}?event={g.event_id}", retries=2)
        except Exception:
            return []
        rows = []
        for p in _player_lines(payload):
            opp = g.away_id if p["team_id"] == g.home_id else g.home_id
            rows.append({
                "event_id": g.event_id, "date": g.date, "season": season,
                "team_id": p["team_id"], "opp_id": opp,
                "is_home": p["team_id"] == g.home_id,
                "player_id": p["player_id"], "player": p["player"],
                "position": p["position"], "category": p["category"],
                "stats": _explode(p["stats"]),
            })
        return rows

    rows: List[dict] = []
    with futures.ThreadPoolExecutor(max_workers=10) as ex:
        for i, res in enumerate(ex.map(one, games), 1):
            rows.extend(res)
            if i % 250 == 0:
                print(f"    {i}/{len(games)}…", flush=True)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f)
    print(f"  cached {len(rows)} player-games", flush=True)
    return rows


def season_totals(rows: Iterable[dict], category: Optional[str] = None) -> List[dict]:
    """Sum the counting stats per player. Derived, so it cannot drift."""
    agg: Dict[tuple, dict] = {}
    for r in rows:
        if category and r["category"] != category:
            continue
        key = (r["player_id"], r["category"])
        e = agg.setdefault(key, {
            "player_id": r["player_id"], "player": r["player"],
            "position": r["position"], "team_id": r["team_id"],
            "category": r["category"], "games": 0, "stats": defaultdict(float),
        })
        e["games"] += 1
        for k, v in r["stats"].items():
            if k in COUNTING:
                e["stats"][k] += v
    out = []
    for e in agg.values():
        e["stats"] = dict(e["stats"])
        out.append(e)
    return out


def game_distribution(rows: Iterable[dict], player_id: str, stat: str) -> List[float]:
    """Every game's value for one stat — the input a prop line is judged against."""
    return [
        r["stats"][stat] for r in rows
        if r["player_id"] == player_id and stat in r["stats"]
    ]
