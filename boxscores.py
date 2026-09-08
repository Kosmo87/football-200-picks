"""
Team box scores per game, cached.

Elo learns one bit from a game: who won. A box score carries the drive-level
facts that actually generate points — plays run, yards gained, turnovers, time
of possession — which is what a totals or props model needs. ESPN keeps these
for every finished game, including the ones whose betting lines it has dropped.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from espn import HEADERS, fetch_scoreboard
from history_data import CACHE_DIR, load_season

SUMMARY = {
    "NFL": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "NCAAF": ("https://site.web.api.espn.com/apis/site/v2/sports/football/"
              "college-football/summary"),
}


@dataclass
class TeamGame:
    event_id: str
    date: str
    team_id: str
    opp_id: str
    is_home: bool
    neutral: bool
    points: int
    opp_points: int
    plays: int              # rush attempts + pass attempts
    yards: int
    pass_yards: int
    rush_yards: int
    turnovers: int
    first_downs: int
    third_down_att: int
    third_down_conv: int
    possession_sec: int


def _int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _eff(v):
    """'8-14' -> (8, 14)."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(v or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _clock(v) -> int:
    """'30:26' -> seconds."""
    m = re.match(r"^\s*(\d+):(\d+)\s*$", str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def _stats_map(team_block) -> Dict[str, str]:
    return {s.get("name"): s.get("displayValue") for s in team_block.get("statistics") or []}


def parse_summary(league: str, event_id: str, date: str, payload: dict) -> List[TeamGame]:
    box = payload.get("boxscore") or {}
    teams = box.get("teams") or []
    if len(teams) != 2:
        return []

    header = payload.get("header") or {}
    comps = (header.get("competitions") or [{}])[0]
    neutral = bool(comps.get("neutralSite"))
    score_by_id, home_by_id = {}, {}
    for c in comps.get("competitors") or []:
        tid = str((c.get("team") or {}).get("id") or c.get("id") or "")
        score_by_id[tid] = _int(c.get("score"))
        home_by_id[tid] = c.get("homeAway") == "home"
    if len(score_by_id) != 2:
        return []

    ids = [str((t.get("team") or {}).get("id") or "") for t in teams]
    if any(i not in score_by_id for i in ids):
        return []

    out = []
    for i, block in enumerate(teams):
        tid, oid = ids[i], ids[1 - i]
        s = _stats_map(block)
        rush_att = _int(s.get("rushingAttempts"))
        _, pass_att = _eff(s.get("completionAttempts", "0-0").replace("/", "-"))
        td_conv, td_att = _eff(s.get("thirdDownEff"))
        out.append(TeamGame(
            event_id=event_id, date=date, team_id=tid, opp_id=oid,
            is_home=home_by_id.get(tid, False), neutral=neutral,
            points=score_by_id[tid], opp_points=score_by_id[oid],
            plays=rush_att + pass_att,
            yards=_int(s.get("totalYards")),
            pass_yards=_int(s.get("netPassingYards")),
            rush_yards=_int(s.get("rushingYards")),
            turnovers=_int(s.get("turnovers")),
            first_downs=_int(s.get("firstDowns")),
            third_down_att=td_att, third_down_conv=td_conv,
            possession_sec=_clock(s.get("possessionTime")),
        ))
    return out


def load_boxscores(league: str, season: int, refresh: bool = False) -> List[TeamGame]:
    path = os.path.join(CACHE_DIR, f"box_{league}_{season}.json")
    if not refresh and os.path.exists(path):
        try:
            with open(path) as f:
                return [TeamGame(**r) for r in json.load(f)]
        except (OSError, ValueError):
            pass

    games = load_season(league, season)
    print(f"  fetching {len(games)} box scores for {league} {season}…", flush=True)

    def one(g):
        try:
            data = fetch_scoreboard(f"{SUMMARY[league]}?event={g.event_id}", retries=2)
        except Exception:
            return []
        return parse_summary(league, g.event_id, g.date, data)

    rows: List[TeamGame] = []
    with futures.ThreadPoolExecutor(max_workers=10) as ex:
        for i, res in enumerate(ex.map(one, games), 1):
            rows.extend(res)
            if i % 200 == 0:
                print(f"    {i}/{len(games)}…", flush=True)

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump([asdict(r) for r in rows], f)
    print(f"  cached {len(rows)} team-games", flush=True)
    return rows
