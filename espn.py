"""ESPN scoreboard fetch helpers (upcoming + completed) with host fallback."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from elo import CompletedGame
from odds import parse_american_odds

# Prefer site.web.api; site.api is often Akamai-blocked (403) from cloud IPs
ESPN_HOSTS = (
    "https://site.web.api.espn.com",
    "https://site.api.espn.com",
)
ESPN_NFL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_NCAAF_BASE = (
    "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
)
PREFERRED_PROVIDER = "Draft Kings"  # normalized match vs "DraftKings"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}


@dataclass
class TeamInfo:
    id: str
    name: str
    abbreviation: str
    home_away: str


@dataclass
class OddsInfo:
    provider: str
    moneyline_home: Optional[int] = None
    moneyline_away: Optional[int] = None
    spread: Optional[float] = None
    spread_details: Optional[str] = None
    total: Optional[float] = None


@dataclass
class Game:
    event_id: str
    name: str
    short_name: str
    date: str
    status: str
    home: TeamInfo
    away: TeamInfo
    odds: OddsInfo


def _provider_matches(name: str, preferred: str) -> bool:
    a = "".join((name or "").lower().split())
    b = "".join((preferred or "").lower().split())
    return bool(a) and a == b


def _espn_url_candidates(url: str) -> list:
    candidates = [url]
    for host in ESPN_HOSTS:
        marker = "://"
        if marker not in url:
            continue
        rest = url.split(marker, 1)[1]
        path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
        alt = host + path
        if alt not in candidates:
            candidates.append(alt)
    return candidates


def fetch_scoreboard(url: str, retries: int = 3) -> dict:
    last_error = None
    candidates = _espn_url_candidates(url)
    for attempt in range(retries):
        for candidate in candidates:
            try:
                r = requests.get(candidate, headers=HEADERS, timeout=15)
                if r.status_code == 200:
                    return r.json()
                last_error = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    last_error = "HTTP 429 (rate limited)"
                if r.status_code in (401, 403):
                    continue
            except requests.Timeout:
                last_error = "request timed out"
            except requests.RequestException as e:
                last_error = str(e)
            except Exception as e:
                last_error = str(e)
        time.sleep(1.2 + attempt)
    raise RuntimeError(f"ESPN fetch failed after {retries} tries: {last_error}")


def _side_moneyline(ml_side: Optional[dict]) -> Optional[int]:
    if not ml_side or not isinstance(ml_side, dict):
        return None
    for key in ("close", "open"):
        nested = ml_side.get(key)
        if isinstance(nested, dict):
            parsed = parse_american_odds(nested.get("odds"))
            if parsed is not None:
                return parsed
    return parse_american_odds(ml_side.get("odds"))


def is_upcoming(status_type: dict) -> bool:
    state = (status_type.get("state") or "").lower()
    if state:
        return state == "pre"
    s = (status_type.get("description") or "").lower()
    return not any(
        x in s
        for x in (
            "final",
            "in progress",
            "halftime",
            "end of",
            "delayed",
            "postponed",
            "canceled",
            "cancelled",
        )
    )


def is_final(status_type: dict) -> bool:
    state = (status_type.get("state") or "").lower()
    if state == "post":
        return True
    if status_type.get("completed") is True:
        return True
    desc = (status_type.get("description") or "").lower()
    return "final" in desc


def _parse_competitors(comp: dict):
    competitors = {
        c["homeAway"]: c for c in comp.get("competitors", []) if "homeAway" in c
    }
    if "home" not in competitors or "away" not in competitors:
        return None, None
    return competitors["home"], competitors["away"]


def _team_info(c: dict, home_away: str) -> TeamInfo:
    team = c.get("team") or {}
    return TeamInfo(
        id=str(c.get("id") or team.get("id") or ""),
        name=team.get("displayName", home_away.title()),
        abbreviation=team.get("abbreviation", home_away.upper()),
        home_away=home_away,
    )


def parse_upcoming_game(event: dict) -> Optional[Game]:
    try:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        comp = competitions[0]
        status_type = (comp.get("status") or {}).get("type") or {}
        status = status_type.get("description", "Unknown")
        if not is_upcoming(status_type):
            return None

        home_c, away_c = _parse_competitors(comp)
        if not home_c or not away_c:
            return None
        home, away = _team_info(home_c, "home"), _team_info(away_c, "away")
        if not home.id or not away.id:
            return None

        odds_list = comp.get("odds") or []
        odds_data = None
        for o in odds_list:
            pname = (o.get("provider") or {}).get("name", "")
            if _provider_matches(pname, PREFERRED_PROVIDER):
                odds_data = o
                break
        if not odds_data and odds_list:
            odds_data = odds_list[0]
        if not odds_data:
            return None

        ml = odds_data.get("moneyline") or {}
        home_ml = _side_moneyline(ml.get("home"))
        away_ml = _side_moneyline(ml.get("away"))
        if home_ml is None:
            home_ml = parse_american_odds(
                (odds_data.get("homeTeamOdds") or {}).get("moneyLine")
            )
        if away_ml is None:
            away_ml = parse_american_odds(
                (odds_data.get("awayTeamOdds") or {}).get("moneyLine")
            )

        return Game(
            event_id=str(event.get("id")),
            name=event.get("name", ""),
            short_name=event.get("shortName", ""),
            date=event.get("date", ""),
            status=status,
            home=home,
            away=away,
            odds=OddsInfo(
                provider=(odds_data.get("provider") or {}).get("name", "Unknown"),
                moneyline_home=home_ml,
                moneyline_away=away_ml,
                spread=odds_data.get("spread"),
                spread_details=odds_data.get("details"),
                total=odds_data.get("overUnder"),
            ),
        )
    except Exception:
        return None


def parse_completed_game(event: dict) -> Optional[CompletedGame]:
    try:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        comp = competitions[0]
        status_type = (comp.get("status") or {}).get("type") or {}
        if not is_final(status_type):
            return None

        home_c, away_c = _parse_competitors(comp)
        if not home_c or not away_c:
            return None
        home, away = _team_info(home_c, "home"), _team_info(away_c, "away")
        if not home.id or not away.id:
            return None

        try:
            home_score = int(float(home_c.get("score")))
            away_score = int(float(away_c.get("score")))
        except (TypeError, ValueError):
            return None

        return CompletedGame(
            event_id=str(event.get("id")),
            date=event.get("date", ""),
            home_id=home.id,
            away_id=away.id,
            home_name=home.name,
            away_name=away.name,
            home_abbr=home.abbreviation,
            away_abbr=away.abbreviation,
            home_score=home_score,
            away_score=away_score,
        )
    except Exception:
        return None


def _dedupe_events(events: list) -> list:
    seen = set()
    unique = []
    for ev in events:
        eid = ev.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            unique.append(ev)
    return unique


def get_upcoming_games(league: str) -> List[Game]:
    events = []
    if league.upper() == "NFL":
        data = fetch_scoreboard(ESPN_NFL)
        events = data.get("events") or []
    else:
        today = datetime.now(timezone.utc).date()
        fetch_errors = []
        for i in range(0, 9):
            d = (today + timedelta(days=i)).strftime("%Y%m%d")
            url = f"{ESPN_NCAAF_BASE}?dates={d}&groups=80&limit=120"
            try:
                data = fetch_scoreboard(url)
                events.extend(data.get("events") or [])
            except Exception as e:
                fetch_errors.append(str(e))
                continue
        if not events and fetch_errors:
            raise RuntimeError(f"NCAAF scoreboard unavailable ({fetch_errors[0]})")
        events = _dedupe_events(events)

    games = []
    for ev in events:
        g = parse_upcoming_game(ev)
        if g and g.odds.moneyline_home is not None and g.odds.moneyline_away is not None:
            games.append(g)
    return games


def fetch_nfl_completed(include_preseason: bool = True, max_reg_weeks: int = 22) -> List[CompletedGame]:
    """Pull completed NFL games. Early season: preseason weeks bootstrap Elo."""
    events = []
    if include_preseason:
        for week in range(1, 5):
            url = f"{ESPN_NFL}?seasontype=1&week={week}"
            try:
                data = fetch_scoreboard(url, retries=2)
                events.extend(data.get("events") or [])
            except Exception:
                continue

    empty_streak = 0
    for week in range(1, max_reg_weeks + 1):
        url = f"{ESPN_NFL}?seasontype=2&week={week}"
        try:
            data = fetch_scoreboard(url, retries=2)
            week_events = data.get("events") or []
            finals = [
                e
                for e in week_events
                if is_final(
                    ((e.get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
                )
            ]
            events.extend(finals)
            if finals:
                empty_streak = 0
            else:
                empty_streak += 1
                # No regular-season finals yet (or mid-season gap) — stop after a few empty weeks
                if empty_streak >= 2:
                    break
        except Exception:
            empty_streak += 1
            if empty_streak >= 2:
                break

    games = []
    for ev in _dedupe_events(events):
        g = parse_completed_game(ev)
        if g:
            games.append(g)
    return games


def fetch_ncaaf_completed(lookback_days: int = 45) -> List[CompletedGame]:
    """Pull completed FBS games over a lookback window via dated scoreboards."""
    today = datetime.now(timezone.utc).date()
    events = []
    for i in range(0, lookback_days + 1):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        url = f"{ESPN_NCAAF_BASE}?dates={d}&groups=80&limit=200"
        try:
            data = fetch_scoreboard(url, retries=2)
            events.extend(data.get("events") or [])
        except Exception:
            continue
        # Light pacing to reduce rate-limit risk
        if i and i % 10 == 0:
            time.sleep(0.3)

    games = []
    for ev in _dedupe_events(events):
        g = parse_completed_game(ev)
        if g:
            games.append(g)
    return games


def fetch_completed_games(league: str) -> List[CompletedGame]:
    if league.upper() == "NFL":
        return fetch_nfl_completed()
    return fetch_ncaaf_completed()
