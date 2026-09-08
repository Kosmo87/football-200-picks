"""
The Odds API ingester — historical closing lines into the same archive.

The free tier is 500 credits and the historical endpoint costs
`10 x markets x regions` per call, so every request is deliberate. Two things
make 500 go a long way:

  * One call returns the whole slate for a sport at that timestamp, not one
    game. A single 10-credit request can pick up fifty-odd college games.
  * Empty responses are not charged, so a mistimed request costs nothing.

The planner therefore takes one snapshot per game day, timed just before that
day's FIRST kickoff. Every game on the card is still pre-game at that moment,
which is the property that matters -- a snapshot taken mid-afternoon would catch
the early games already in play, and a live price is not a closing price.

Budget is enforced before each call rather than after, and the key is read from
the environment and never written anywhere.

  export ODDS_API_KEY=...
  python odds_api.py --plan                 # costs nothing, shows the schedule
  python odds_api.py --league NCAAF --season 2025 --max-credits 300
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from archive import ARCHIVE_DIR, append_ndjson, read_ndjson, utcnow
from history_data import load_season

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = {"NCAAF": "americanfootball_ncaaf", "NFL": "americanfootball_nfl"}

# Spreads first: it is the market where the efficiency model showed real signal.
# Each extra market multiplies the cost of every call.
DEFAULT_MARKETS = "spreads"
DEFAULT_REGIONS = "us"
CREDITS_PER_CALL = 10  # x markets x regions

# Snapshot this far before the day's first kickoff.
LEAD = timedelta(minutes=20)


def api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("ODDS_API_KEY is not set. export ODDS_API_KEY=... and retry.")
    return key


def archive_path(league: str, season: int) -> str:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    return os.path.join(ARCHIVE_DIR, f"oddsapi_{league}_{season}.ndjson")


def plan_snapshots(league: str, season: int) -> List[Tuple[str, int]]:
    """
    One timestamp per game day, 20 minutes before that day's first kickoff.

    Returns [(iso_timestamp, games_that_day)], busiest days first so a small
    budget is spent where it buys the most games.
    """
    games = load_season(league, season)
    by_day: Dict[str, List[datetime]] = defaultdict(list)
    for g in games:
        if not g.date:
            continue
        try:
            dt = datetime.fromisoformat(g.date.replace("Z", "+00:00"))
        except ValueError:
            continue
        by_day[dt.date().isoformat()].append(dt)

    out = []
    for day, kicks in by_day.items():
        first = min(kicks) - LEAD
        out.append((first.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    len(kicks)))
    out.sort(key=lambda t: -t[1])
    return out


def fetch_snapshot(league: str, iso_ts: str, markets: str, regions: str) -> Tuple[Optional[dict], Dict[str, str]]:
    url = f"{BASE}/historical/sports/{SPORT_KEY[league]}/odds"
    params = {
        "apiKey": api_key(), "regions": regions, "markets": markets,
        "oddsFormat": "american", "date": iso_ts,
    }
    r = requests.get(url, params=params, timeout=40)
    headers = {
        "used": r.headers.get("x-requests-used", "?"),
        "remaining": r.headers.get("x-requests-remaining", "?"),
        "last_cost": r.headers.get("x-requests-last", "?"),
    }
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:160]}")
        return None, headers
    return r.json(), headers


def flatten(league: str, season: int, payload: dict, markets: str) -> List[dict]:
    """One row per game per bookmaker, matching the archive's shape."""
    rows = []
    snapshot_ts = payload.get("timestamp")
    for ev in payload.get("data") or []:
        base = {
            "source": "the-odds-api",
            "league": league, "season": season,
            "snapshot": snapshot_ts,
            "captured_at": utcnow(),
            "odds_event_id": ev.get("id"),
            "kickoff": ev.get("commence_time"),
            "home_team": ev.get("home_team"),
            "away_team": ev.get("away_team"),
        }
        for bk in ev.get("bookmakers") or []:
            row = dict(base, bookmaker=bk.get("key"), bookmaker_updated=bk.get("last_update"))
            for mk in bk.get("markets") or []:
                key = mk.get("key")
                for oc in mk.get("outcomes") or []:
                    name = oc.get("name")
                    side = ("home" if name == ev.get("home_team")
                            else "away" if name == ev.get("away_team") else name)
                    if key == "spreads":
                        row[f"spread_{side}"] = oc.get("point")
                        row[f"spread_odds_{side}"] = oc.get("price")
                    elif key == "h2h":
                        row[f"ml_{side}"] = oc.get("price")
                    elif key == "totals":
                        row["total"] = oc.get("point")
                        row[f"{str(name).lower()}_odds"] = oc.get("price")
            rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="NCAAF", choices=("NFL", "NCAAF"))
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--markets", default=DEFAULT_MARKETS)
    ap.add_argument("--regions", default=DEFAULT_REGIONS)
    ap.add_argument("--max-credits", type=int, default=100,
                    help="hard ceiling for this run; checked before every call")
    ap.add_argument("--plan", action="store_true",
                    help="show the schedule and cost without spending anything")
    args = ap.parse_args()

    per_call = CREDITS_PER_CALL * len(args.markets.split(",")) * len(args.regions.split(","))
    schedule = plan_snapshots(args.league, args.season)

    path = archive_path(args.league, args.season)
    done = {r.get("snapshot") for r in read_ndjson(path)}

    affordable = args.max_credits // per_call
    print(f"{args.league} {args.season}: {len(schedule)} game days")
    print(f"  markets={args.markets} regions={args.regions} -> {per_call} credits/call")
    print(f"  budget {args.max_credits} -> {affordable} calls\n")

    todo = schedule[:affordable]
    covered = sum(n for _, n in todo)
    print(f"  {'timestamp':<26}{'games that day':>15}")
    for ts, n in todo[:10]:
        print(f"  {ts:<26}{n:>15}")
    if len(todo) > 10:
        print(f"  … {len(todo)-10} more")
    print(f"\n  would capture ~{covered} games for {len(todo)*per_call} credits")

    if args.plan:
        print("\n(--plan: nothing fetched, no credits spent)")
        return 0

    spent = 0
    written = 0
    for ts, n_games in todo:
        if spent + per_call > args.max_credits:
            print(f"\nbudget reached ({spent}/{args.max_credits})")
            break
        payload, hdr = fetch_snapshot(args.league, ts, args.markets, args.regions)
        spent += per_call
        if payload is None:
            continue
        rows = flatten(args.league, args.season, payload, args.markets)
        if not rows:
            print(f"  {ts}: empty (not charged)")
            continue
        if payload.get("timestamp") in done:
            print(f"  {ts}: snapshot already stored, skipping")
            continue
        append_ndjson(path, rows)
        done.add(payload.get("timestamp"))
        written += len(rows)
        print(f"  {ts}: {len(rows)} rows  "
              f"[used {hdr['used']}, remaining {hdr['remaining']}]")

    print(f"\nwrote {written} rows to {path}")
    print(f"credits spent this run: ~{spent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
