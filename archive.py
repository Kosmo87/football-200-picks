"""
Capture everything ESPN knows about a game, before and after it is played.

ESPN deletes betting lines once a game finals -- roughly 92% of finished games
carry none -- which is why every question in this project has been answered on
20-to-100 game samples. Nothing recovers the past, but the hourly build already
looks at upcoming games while their lines are still there, so from now on the
data is kept.

Two append-only NDJSON logs, chosen over a database so the archive travels with
the repo, versions itself, and needs no credentials in CI:

  data/archive/lines_<LEAGUE>_<SEASON>.ndjson
      One record each time a price moves. Written only on change -- a line that
      has not moved since the last run tells us nothing new, and storing hourly
      duplicates would be ~200 records per game instead of ~20.

  data/archive/results_<LEAGUE>_<SEASON>.ndjson
      One record per finished game: final score, full team box score, and every
      player's line. Written once, when the game finals.

Together these give what no free source will sell back later: opening price,
every move, the closing price, and the box score that settles it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from boxscores import SUMMARY, parse_summary
from espn import (
    ESPN_NCAAF_BASE,
    ESPN_NFL,
    _dedupe_events,
    current_season_year,
    fetch_scoreboard,
    is_final,
    parse_american_odds,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(ROOT, "data", "archive")

# Fields that define "the line moved". Anything else (logos, links, deep links)
# changes constantly and would make every run look like a movement.
LINE_FIELDS = (
    "ml_home", "ml_away",
    "spread", "spread_odds_home", "spread_odds_away",
    "total", "over_odds", "under_odds",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(kind: str, league: str, season: int) -> str:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    return os.path.join(ARCHIVE_DIR, f"{kind}_{league}_{season}.ndjson")


def read_ndjson(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def append_ndjson(path: str, records: List[dict]) -> None:
    if not records:
        return
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Pre-game: line snapshots
# ---------------------------------------------------------------------------

def _side_price(block: Optional[dict], key: str = "close") -> Optional[int]:
    """A price from ESPN's nested close/open shape."""
    if not isinstance(block, dict):
        return None
    for k in (key, "open"):
        nested = block.get(k)
        if isinstance(nested, dict):
            v = parse_american_odds(nested.get("odds"))
            if v is not None:
                return v
    return None


def _side_line(block: Optional[dict], key: str = "close") -> Optional[float]:
    """A handicap ('-3.5', 'o44.5') from the same shape."""
    if not isinstance(block, dict):
        return None
    for k in (key, "open"):
        nested = block.get(k)
        if isinstance(nested, dict):
            raw = str(nested.get("line", "")).strip().lstrip("ou")
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


def extract_lines(odds_block: dict) -> Dict:
    ml = odds_block.get("moneyline") or {}
    ps = odds_block.get("pointSpread") or {}
    tot = odds_block.get("total") or {}

    spread = odds_block.get("spread")
    try:
        spread = float(spread) if spread is not None else _side_line(ps.get("home"))
    except (TypeError, ValueError):
        spread = None
    total = odds_block.get("overUnder")
    try:
        total = float(total) if total is not None else _side_line(tot.get("over"))
    except (TypeError, ValueError):
        total = None

    return {
        "ml_home": _side_price(ml.get("home")),
        "ml_away": _side_price(ml.get("away")),
        "spread": spread,
        "spread_odds_home": _side_price(ps.get("home")),
        "spread_odds_away": _side_price(ps.get("away")),
        "total": total,
        "over_odds": _side_price(tot.get("over")),
        "under_odds": _side_price(tot.get("under")),
    }


def _upcoming_events(league: str) -> List[dict]:
    if league == "NFL":
        return (fetch_scoreboard(ESPN_NFL).get("events") or [])
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    events = []
    for i in range(0, 9):
        d = (today + timedelta(days=i)).strftime("%Y%m%d")
        try:
            data = fetch_scoreboard(f"{ESPN_NCAAF_BASE}?dates={d}&groups=80&limit=200")
        except Exception:
            continue
        events.extend(data.get("events") or [])
    return _dedupe_events(events)


def snapshot_lines(league: str, season: Optional[int] = None) -> Tuple[int, int]:
    """Append a record for every line that has moved. Returns (seen, written)."""
    season = season or current_season_year()
    path = _path("lines", league, season)

    # Last known state per (event, provider): only movement is worth a row.
    last: Dict[Tuple[str, str], Dict] = {}
    for rec in read_ndjson(path):
        last[(rec.get("event_id"), rec.get("provider"))] = rec

    now = utcnow()
    new: List[dict] = []
    seen = 0

    for ev in _upcoming_events(league):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = (comp.get("status") or {}).get("type") or {}
        if is_final(status):
            continue

        teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        home, away = teams.get("home"), teams.get("away")
        if not home or not away:
            continue

        for ob in comp.get("odds") or []:
            provider = (ob.get("provider") or {}).get("name", "Unknown")
            lines = extract_lines(ob)
            if all(lines[f] is None for f in LINE_FIELDS):
                continue
            seen += 1

            key = (str(ev.get("id")), provider)
            prev = last.get(key)
            if prev and all(prev.get(f) == lines[f] for f in LINE_FIELDS):
                continue  # unchanged since the last run

            rec = {
                "event_id": str(ev.get("id")),
                "league": league,
                "season": season,
                "captured_at": now,
                "kickoff": ev.get("date"),
                "short_name": ev.get("shortName"),
                "neutral": bool(comp.get("neutralSite")),
                "home_id": str((home.get("team") or {}).get("id") or ""),
                "away_id": str((away.get("team") or {}).get("id") or ""),
                "home_abbr": (home.get("team") or {}).get("abbreviation"),
                "away_abbr": (away.get("team") or {}).get("abbreviation"),
                "provider": provider,
                **lines,
            }
            new.append(rec)
            last[key] = rec

    append_ndjson(path, new)
    return seen, len(new)


# ---------------------------------------------------------------------------
# Post-game: results and box scores
# ---------------------------------------------------------------------------

def _player_lines(payload: dict) -> List[dict]:
    """Every player's statline, flattened. This is what a props model needs."""
    out = []
    for team_block in (payload.get("boxscore") or {}).get("players") or []:
        team_id = str((team_block.get("team") or {}).get("id") or "")
        for cat in team_block.get("statistics") or []:
            keys = cat.get("keys") or []
            for ath in cat.get("athletes") or []:
                info = ath.get("athlete") or {}
                vals = ath.get("stats") or []
                if not vals:
                    continue
                out.append({
                    "team_id": team_id,
                    "player_id": str(info.get("id") or ""),
                    "player": info.get("displayName"),
                    "position": (info.get("position") or {}).get("abbreviation"),
                    "category": cat.get("name"),
                    "stats": dict(zip(keys, vals)),
                })
    return out


def archive_results(league: str, season: Optional[int] = None, limit: int = 120) -> int:
    """
    Store finished games that are not already archived.

    Driven by the games we have lines for, so the archive stays self-consistent:
    every result row can be joined to its own line history.
    """
    season = season or current_season_year()
    lines_path = _path("lines", league, season)
    res_path = _path("results", league, season)

    have = {r.get("event_id") for r in read_ndjson(res_path)}
    wanted = {r.get("event_id") for r in read_ndjson(lines_path)} - have
    if not wanted:
        return 0

    written = 0
    for event_id in sorted(wanted)[:limit]:
        try:
            payload = fetch_scoreboard(f"{SUMMARY[league]}?event={event_id}", retries=2)
        except Exception:
            continue

        header = payload.get("header") or {}
        comp = (header.get("competitions") or [{}])[0]
        status = (comp.get("status") or {}).get("type") or {}
        if not status.get("completed"):
            continue  # not played yet; try again on a later run

        date = header.get("competitions", [{}])[0].get("date") or ""
        team_games = parse_summary(league, event_id, date, payload)
        if not team_games:
            continue

        append_ndjson(res_path, [{
            "event_id": event_id,
            "league": league,
            "season": season,
            "date": date,
            "archived_at": utcnow(),
            "teams": [t.__dict__ for t in team_games],
            "players": _player_lines(payload),
        }])
        written += 1
    return written


def run(leagues=("NFL", "NCAAF")) -> None:
    season = current_season_year()
    for league in leagues:
        try:
            seen, wrote = snapshot_lines(league, season)
            print(f"[archive] {league}: {seen} priced games, {wrote} line moves stored",
                  flush=True)
        except Exception as e:
            print(f"[archive] {league} lines FAILED: {e}", flush=True)
        try:
            n = archive_results(league, season)
            print(f"[archive] {league}: {n} results archived", flush=True)
        except Exception as e:
            print(f"[archive] {league} results FAILED: {e}", flush=True)


if __name__ == "__main__":
    run()
