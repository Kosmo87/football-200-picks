"""
Log QB passing-touchdown over/unders, so a backtest becomes possible later.

There is no props model yet and this does not pretend to be one. It is the
collector that has to run first: SportsGameOdds' free tier serves LIVE lines
only, so the history a model would be tested against does not exist anywhere
and can only be accumulated from today forward. Every week this does not run is
a week that cannot be backtested.

ONE MARKET, ON PURPOSE. The free tier allows 2,500 objects a month. A full NFL
slate across nine books is thousands of prop lines on its own, so a scan of
everything would spend the month in a single Sunday. This asks only for
passing touchdowns, filtered before anything is stored:

    oddID = passing_touchdowns-{playerID}-game-ou-over

An oddID is {statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}, and for a
player prop the entity is a player rather than home/away/all.

WHAT IS STORED. The line, both prices, the book, and the moment it was seen —
not a judgement. Grading comes later from the box scores playerlogs.py already
pulls for free, which is the half of this that costs nothing and works today.

  python props.py --scan          # fetch and log (spends objects)
  python props.py --report        # what has been collected (free)
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List

import keys  # noqa: F401  (loads ~/.football-picks.env)
from archive import ARCHIVE_DIR, append_ndjson, read_ndjson, utcnow

BASE = "https://api.sportsgameodds.com/v2"
STAT_ID = "passing_touchdowns"
PERIOD = "game"
BET_TYPE = "ou"


def ledger_path() -> str:
    return os.path.join(ARCHIVE_DIR, "props_passing_tds.ndjson")


def _key() -> str:
    return os.environ.get("SGO_API_KEY", "").strip()


def _get(path: str, params: Dict[str, str]) -> dict:
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": _key()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def scan(league: str = "NFL") -> List[dict]:
    """Every passing-TD over/under currently offered, one row per book."""
    data = _get("/events", {"leagueID": league, "oddsAvailable": "true"})
    events = data.get("data") or data.get("events") or []
    seen_at = utcnow()
    rows: List[dict] = []

    for ev in events:
        event_id = ev.get("eventID") or ev.get("id")
        start = ((ev.get("status") or {}).get("startsAt")
                 or ev.get("startsAt") or ev.get("commence_time"))
        teams = ev.get("teams") or {}
        matchup = "/".join(
            str((teams.get(side) or {}).get("names", {}).get("short")
                or (teams.get(side) or {}).get("teamID") or "?")
            for side in ("away", "home"))

        for odd_id, odd in (ev.get("odds") or {}).items():
            # The entity segment is a player id, so the match is on the ends
            # rather than the whole string.
            parts = odd_id.split("-")
            if len(parts) != 5:
                continue
            stat, entity, period, bet_type, side = parts
            if stat != STAT_ID or period != PERIOD or bet_type != BET_TYPE:
                continue

            # byBookmaker carries each book's own number; the top level is a
            # consensus. Both are recorded, because a line shopper needs the
            # spread between books and a model needs one reference.
            books = odd.get("byBookmaker") or {}
            for book, b in books.items():
                rows.append({
                    "seen_at": seen_at, "league": league, "event_id": event_id,
                    "starts_at": start, "matchup": matchup,
                    "player_id": entity, "side": side, "book": book,
                    "line": b.get("overUnder") or odd.get("overUnder"),
                    "price": b.get("odds") or b.get("price"),
                    "odd_id": odd_id,
                })
    return rows


def report() -> None:
    rows = read_ndjson(ledger_path())
    if not rows:
        print("Nothing collected yet. Run --scan.")
        return
    players = {r.get("player_id") for r in rows}
    books = {r.get("book") for r in rows}
    days = {str(r.get("seen_at", ""))[:10] for r in rows}
    print(f"{len(rows)} observations")
    print(f"  {len(players)} players, {len(books)} books, {len(days)} day(s)")
    print(f"  first: {min(days)}   latest: {max(days)}")
    print(f"  file: {ledger_path()}")
    # The point of collecting is a backtest, and a backtest needs closing lines
    # over many games. Say plainly how far off that is rather than implying the
    # data is ready.
    if len(days) < 14:
        print(f"\n  Too little history to test anything yet — {len(days)} day(s) in. "
              f"Keep it running; a model needs weeks, not runs.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true", help="fetch and log (spends objects)")
    ap.add_argument("--report", action="store_true", help="what has been collected")
    ap.add_argument("--league", default="NFL")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, store nothing")
    args = ap.parse_args()

    if args.report or not (args.scan or args.dry_run):
        report()
        return 0

    if not _key():
        print("SGO_API_KEY is not set. Get a free key at sportsgameodds.com "
              "and add SGO_API_KEY to ~/.football-picks.env (or the repo secrets).")
        return 1

    try:
        rows = scan(args.league)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print(f"  {args.league}: HTTP {e.code} {body}")
        return 1

    print(f"{len(rows)} passing-TD prices across "
          f"{len({r['player_id'] for r in rows})} quarterback(s)")
    for r in rows[:8]:
        print(f"  {r['matchup']:12s} {r['player_id']:22s} {r['side']:5s} "
              f"{r['line']} @ {r['price']}  ({r['book']})")
    if args.dry_run:
        print("\n(dry run: nothing stored)")
        return 0

    append_ndjson(ledger_path(), rows)
    print(f"\nlogged to {ledger_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
