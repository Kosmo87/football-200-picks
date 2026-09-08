"""
Multi-season history: completed games plus FBS membership, cached on disk.

One prior season is not enough to fit a rating model, and treating every
unrecognised opponent as an average team is what made the college ratings
useless. This module supplies both missing pieces.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Dict, Iterable, List, Set

import requests

from elo import CompletedGame
from espn import HEADERS, fetch_season_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")

FBS_GROUP_URL = (
    "http://sports.core.api.espn.com/v2/sports/football/leagues/college-football/"
    "seasons/{season}/types/2/groups/80/teams?limit=300"
)


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def fbs_team_ids(season: int, refresh: bool = False) -> Set[str]:
    """Team ids in ESPN's FBS group for a season. Everything else is FCS."""
    path = _cache_path(f"fbs_{season}.json")
    if not refresh:
        cached = _read(path)
        if cached:
            return set(cached)
    try:
        data = requests.get(
            FBS_GROUP_URL.format(season=season), headers=HEADERS, timeout=30
        ).json()
    except Exception:
        return set()
    ids = []
    for item in data.get("items") or []:
        m = re.search(r"/teams/(\d+)", item.get("$ref", ""))
        if m:
            ids.append(m.group(1))
    if ids:
        _write(path, sorted(ids))
    return set(ids)


def all_fbs_ids(seasons: Iterable[int]) -> Set[str]:
    out: Set[str] = set()
    for s in seasons:
        out |= fbs_team_ids(s)
    return out


def load_season(league: str, season: int, refresh: bool = False) -> List[CompletedGame]:
    """Completed games for one season, cached (past seasons never change)."""
    path = _cache_path(f"season_{league}_{season}.json")
    if not refresh:
        cached = _read(path)
        if cached:
            return [CompletedGame(**g) for g in cached]
    games = fetch_season_completed(league, season)
    if games:
        _write(path, [asdict(g) for g in games])
    return games


def load_seasons(
    league: str, seasons: Iterable[int], refresh: bool = False
) -> Dict[int, List[CompletedGame]]:
    return {s: load_season(league, s, refresh=refresh) for s in seasons}
