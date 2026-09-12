"""
What the Elo model does not know: who is hurt, and what the weather is doing.

The ratings are built from final scores. That is all they are built from, so
they carry no information about who is actually going to play — and a rating
that thinks Atlanta is a 45% road dog because of how Atlanta played in August
is answering a question about a different team once both its quarterbacks are
ruled out. The market reprices in minutes. The model does not reprice at all.

That gap is the single most useful thing this module finds, and it is worth
being precise about what it is and is not:

  IT IS NOT A MODEL INPUT. There is no historical injury or weather archive to
  test a coefficient against -- ESPN's injury feed is a snapshot of today, so
  "how much is a missing quarterback worth in Elo points" cannot be measured
  here and will not be guessed. Anything asserting a number would be inventing
  one, which is how the disagreement bands got discovered the honest way and
  the standard the rest of the project is held to.

  IT IS A REASON TO LOOK. This project already found, by backtest, that the
  further the model strays from the price the more likely the model is the one
  that is wrong -- at 10+ points of claimed edge the picks won 25-38% against
  the 57-67% they forecast. Injuries are a MECHANISM for that. So a big claimed
  edge sitting next to a quarterback ruled out is not a new adjustment; it is
  the existing finding, with its cause named. Displayed, not applied.

Two sources, both free and unauthenticated:

  ESPN injuries   /football/<league>/injuries -- for the NFL this is ~800
                  current entries across all 32 teams. For college football it
                  is three rows, one of them dated 2020: the feed exists and is
                  empty. Said plainly rather than quietly returning nothing,
                  because "no injuries flagged" and "no injury data" are very
                  different facts and only one of them is reassuring.

  Open-Meteo      hourly forecast by lat/lon, 7 days out, no key. Venue city
                  comes from the scoreboard, geocoded once and cached. Indoor
                  venues are skipped rather than reported as calm and mild.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from espn import ESPN_NCAAF_BASE, ESPN_NFL, fetch_scoreboard

# NOT espn.HEADERS. The scoreboard wants a browser-shaped request -- Safari
# user-agent, espn.com Origin and Referer -- and the injuries path rejects
# exactly that with a 403 while serving a plain client a 200. Same host,
# opposite rules, so this endpoint gets its own minimal headers.
INJURY_HEADERS = {"User-Agent": "football-200-picks (+https://github.com/Kosmo87/football-200-picks)"}

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
VENUE_CACHE = os.path.join(CACHE_DIR, "venue_coords.json")

INJURY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/{path}/injuries"
LEAGUE_PATH = {"NFL": "nfl", "NCAAF": "college-football"}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Statuses that mean the player is not expected to be on the field.
#
# Questionable is excluded on purpose: it is the status teams use to say
# nothing, it covers roughly a third of every report, and treating it as "out"
# would flag every game every week, which is the same as flagging none of them.
#
# Injured Reserve is collected but never flagged, and the split matters more
# than it looks. IR was 121 of 209 NFL absences the first time this ran, which
# made "missing 5+ players" fire on all 32 teams and all 14 games -- a flag that
# is always on is not a flag. It is also the wrong news: a player who has been
# on IR for weeks is someone the team has already played without, so the Elo
# rating has absorbed him and the market priced him long ago. What neither has
# absorbed is THIS week's ruling. So the two are counted separately: Out and
# Doubtful are news, IR is background.
THIS_WEEK = {"Out", "Doubtful", "Suspension"}
LONG_TERM = {"Injured Reserve"}
SIDELINED = THIS_WEEK | LONG_TERM

# Positions where one absence changes how a team is priced. Quarterback is in a
# class of its own and is the only one that raises a flag by itself; the rest
# matter in aggregate, as a count of missing starters.
QB = {"QB"}
IMPACT_POSITIONS = {
    "QB", "RB", "WR", "TE", "OT", "OL", "T", "G", "C",
    "CB", "S", "LB", "DE", "DT", "EDGE", "K",
}

# Weather thresholds. Wind is the one with a real effect on football scoring --
# it moves kicking and the deep passing game -- and these are the conventional
# handicapping cutoffs, not fitted values. Nothing here adjusts a probability.
WIND_NOTE_MPH = 15
WIND_STRONG_MPH = 20
COLD_F = 25
HOT_F = 92
PRECIP_PCT = 50


# ── injuries ──────────────────────────────────────────────────────────────

def fetch_injuries(league: str) -> Dict[str, List[dict]]:
    """{team_id: [{player, position, status, detail, since}]} for sidelined players."""
    path = LEAGUE_PATH.get(league.upper())
    if not path:
        return {}
    # ~9 MB of JSON: every entry carries the athlete's links, headshot and
    # full team object. Fetched once per league per build, never per game.
    r = requests.get(INJURY_URL.format(path=path), headers=INJURY_HEADERS, timeout=40)
    r.raise_for_status()
    out: Dict[str, List[dict]] = {}
    for team in r.json().get("injuries") or []:
        tid = str(team.get("id") or "")
        rows = []
        for entry in team.get("injuries") or []:
            status = entry.get("status")
            if status not in SIDELINED:
                continue
            athlete = entry.get("athlete") or {}
            pos = ((athlete.get("position") or {}).get("abbreviation") or "").upper()
            if pos not in IMPACT_POSITIONS:
                continue
            rows.append({
                "player": athlete.get("displayName") or "",
                "position": pos,
                "status": status,
                "detail": (entry.get("details") or {}).get("type") or "",
                "since": entry.get("date") or "",
            })
        if rows and tid:
            # Quarterbacks first, then by how definite the absence is: the top of
            # this list is what a one-line summary has room to say.
            rows.sort(key=lambda r: (r["position"] not in QB, r["status"] != "Out"))
            out[tid] = rows
    return out


def team_summary(rows: List[dict]) -> dict:
    """One team's absences, reduced to what a badge can show."""
    news = [r for r in rows if r["status"] in THIS_WEEK]
    qbs = [r for r in news if r["position"] in QB]
    return {
        "count": len(news),
        "long_term": len([r for r in rows if r["status"] in LONG_TERM]),
        "qb_out": len(qbs),
        "qbs": [f"{r['player']} ({r['detail'] or r['status']})" for r in qbs],
        "players": news[:8],
    }


# ── weather ───────────────────────────────────────────────────────────────

def _load_venue_cache() -> dict:
    try:
        with open(VENUE_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_venue_cache(cache: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(VENUE_CACHE, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)


def geocode(city: str, state: str, cache: dict) -> Optional[dict]:
    """City to coordinates, once per venue, then cached to disk forever."""
    key = f"{city}|{state}".lower()
    if key in cache:
        return cache[key] or None
    try:
        r = requests.get(GEOCODE_URL, params={"name": city, "count": 5, "country": "US"},
                         timeout=20)
        results = r.json().get("results") or []
    except Exception:
        return None
    pick = None
    for res in results:
        # Prefer the match in the right state: there are a lot of Columbuses.
        if state and (res.get("admin1_code") == state or res.get("admin1") == state):
            pick = res
            break
    pick = pick or (results[0] if results else None)
    # A negative result is cached too, so a city ESPN spells its own way is not
    # re-queried every hour forever.
    cache[key] = {"lat": pick["latitude"], "lon": pick["longitude"]} if pick else None
    return cache[key]


def fetch_venues(league: str) -> Dict[str, dict]:
    """{event_id: {venue, city, state, indoor}} from the scoreboard."""
    urls = [ESPN_NFL] if league.upper() == "NFL" else [
        f"{ESPN_NCAAF_BASE}?dates="
        f"{(datetime.now(timezone.utc).date() + timedelta(days=i)).strftime('%Y%m%d')}"
        f"&groups=80&limit=120"
        for i in range(0, 9)
    ]
    out: Dict[str, dict] = {}
    for url in urls:
        try:
            data = fetch_scoreboard(url)
        except Exception:
            continue
        for ev in data.get("events") or []:
            comp = (ev.get("competitions") or [{}])[0]
            v = comp.get("venue") or {}
            addr = v.get("address") or {}
            if not v.get("fullName"):
                continue
            out[str(ev.get("id"))] = {
                "venue": v.get("fullName"),
                "city": addr.get("city") or "",
                "state": addr.get("state") or "",
                "indoor": bool(v.get("indoor")),
            }
    return out


def fetch_forecast(lat: float, lon: float, kickoff: str) -> Optional[dict]:
    """Conditions at the hour of kickoff, or None if it is outside the window."""
    try:
        k = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None
    if not timedelta(0) <= (k - datetime.now(timezone.utc)) <= timedelta(days=7):
        return None  # Open-Meteo's free forecast does not reach that far
    try:
        r = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,"
                      "wind_gusts_10m,snowfall",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "forecast_days": 8, "timezone": "UTC",
        }, timeout=25)
        h = r.json()["hourly"]
    except Exception:
        return None
    stamp = k.strftime("%Y-%m-%dT%H:00")
    try:
        i = h["time"].index(stamp)
    except (ValueError, KeyError):
        return None
    return {
        "temp_f": h["temperature_2m"][i],
        "wind_mph": h["wind_speed_10m"][i],
        "gust_mph": h["wind_gusts_10m"][i],
        "precip_pct": h["precipitation_probability"][i],
        "snow_in": h["snowfall"][i],
    }


# ── flags ─────────────────────────────────────────────────────────────────

def weather_flags(w: dict) -> List[dict]:
    flags = []
    gust = w.get("gust_mph") or 0
    wind = w.get("wind_mph") or 0
    if max(wind, gust) >= WIND_STRONG_MPH:
        flags.append({"kind": "wind", "severity": "high",
                      "text": f"Wind {wind:.0f} mph, gusting {gust:.0f} — "
                              f"kicking and the deep ball suffer"})
    elif max(wind, gust) >= WIND_NOTE_MPH:
        flags.append({"kind": "wind", "severity": "low",
                      "text": f"Breezy: {wind:.0f} mph, gusting {gust:.0f}"})
    if (w.get("snow_in") or 0) > 0:
        flags.append({"kind": "snow", "severity": "high",
                      "text": f"Snow in the forecast ({w['snow_in']:.2f} in/hr)"})
    elif (w.get("precip_pct") or 0) >= PRECIP_PCT:
        flags.append({"kind": "rain", "severity": "low",
                      "text": f"{w['precip_pct']:.0f}% chance of rain"})
    t = w.get("temp_f")
    if t is not None and t <= COLD_F:
        flags.append({"kind": "cold", "severity": "low", "text": f"{t:.0f}°F at kickoff"})
    elif t is not None and t >= HOT_F:
        flags.append({"kind": "heat", "severity": "low", "text": f"{t:.0f}°F at kickoff"})
    return flags


def injury_flags(side: str, abbr: str, summary: dict) -> List[dict]:
    """
    Flags for one team's absences.

    The quarterback flag says what it means and no more: the model does not know
    this. It does not say which way to bet, because the honest answer is that
    the market already moved and the model has not.
    """
    flags = []
    if summary["qb_out"]:
        who = ", ".join(summary["qbs"][:2])
        flags.append({
            "kind": "qb_out", "severity": "high", "side": side,
            "text": f"{abbr} quarterback out: {who}. The ratings are built from "
                    f"final scores and do not know this; the price does.",
        })
    # Measured before it was chosen: the median team has 2 players ruled out
    # this week and the worst has 8, so 5 catches the top fifth rather than
    # everybody.
    if summary["count"] >= 5:
        flags.append({
            "kind": "injuries", "severity": "low", "side": side,
            "text": f"{abbr} ruled out {summary['count']} rotation players this week",
        })
    return flags


def build_context(league: str, games: List[dict]) -> Dict[str, dict]:
    """
    {event_id: context} for a league's upcoming games.

    Every source is optional. A failure here must not take the board down: the
    board was useful before any of this existed and has to stay useful when
    ESPN's injury feed 500s.
    """
    injuries: Dict[str, List[dict]] = {}
    injury_error = None
    try:
        injuries = fetch_injuries(league)
    except Exception as e:
        injury_error = str(e)
        print(f"[context] {league} injuries unavailable: {e}")

    # Coverage is measured, not assumed. Labelling college "espn" because the
    # feed answered would imply the league is covered when it returns three
    # rows for a hundred and fifty teams -- and "no injuries flagged" reads as
    # reassurance while "no injury data" reads as a warning. They are different
    # facts and the page has to be able to tell them apart.
    playing = {str(g[side]["id"]) for g in games for side in ("home", "away")}
    covered = len(playing & set(injuries))
    if injury_error:
        coverage, source = 0.0, "unavailable"
    elif not playing:
        coverage, source = 0.0, "empty"
    else:
        coverage = covered / len(playing)
        source = "espn" if coverage >= 0.5 else "sparse" if covered else "empty"
    injury_meta = {
        "source": source,
        "teams_covered": covered,
        "teams_playing": len(playing),
        "error": injury_error,
    }

    venues: Dict[str, dict] = {}
    try:
        venues = fetch_venues(league)
    except Exception as e:
        print(f"[context] {league} venues unavailable: {e}")

    cache = _load_venue_cache()
    out: Dict[str, dict] = {}
    for game in games:
        eid = str(game.get("event_id"))
        home_id, away_id = str(game["home"]["id"]), str(game["away"]["id"])
        home = team_summary(injuries.get(home_id, []))
        away = team_summary(injuries.get(away_id, []))

        flags = injury_flags("home", game["home"]["abbr"], home)
        flags += injury_flags("away", game["away"]["abbr"], away)

        venue = venues.get(eid) or {}
        weather = None
        if venue and not venue.get("indoor") and venue.get("city"):
            coords = geocode(venue["city"], venue.get("state", ""), cache)
            if coords:
                weather = fetch_forecast(coords["lat"], coords["lon"], game.get("kickoff"))
                if weather:
                    flags += weather_flags(weather)

        out[eid] = {
            "injuries": {"home": home, "away": away, **injury_meta},
            "venue": venue or None,
            "weather": weather,
            "flags": flags,
        }
    _save_venue_cache(cache)
    return out


def main() -> int:
    """Print what the sources say for one league, to eyeball before trusting it."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NFL", choices=sorted(LEAGUE_PATH))
    args = ap.parse_args()

    board = json.load(open(os.path.join(ROOT, "public", "data", "board.json")))
    games = board["leagues"][args.league]["games"]
    ctx = build_context(args.league, games)
    flagged = 0
    for g in games:
        c = ctx.get(str(g["event_id"])) or {}
        if not c.get("flags"):
            continue
        flagged += 1
        print(f"\n{g['short_name']}  {g['kickoff']}")
        w = c.get("weather")
        if w:
            print(f"   {w['temp_f']:.0f}°F  wind {w['wind_mph']:.0f}/{w['gust_mph']:.0f} mph"
                  f"  precip {w['precip_pct']:.0f}%   ({(c.get('venue') or {}).get('venue')})")
        for f in c["flags"]:
            print(f"   [{f['severity']:4s}] {f['text']}")
    meta = (next(iter(ctx.values()), {}).get("injuries") or {})
    print(f"\n{flagged} of {len(games)} games flagged · injury data: "
          f"{meta.get('source')} "
          f"({meta.get('teams_covered')} of {meta.get('teams_playing')} teams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
