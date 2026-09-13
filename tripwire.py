"""
Spend credits when the market MOVES, not on a timer.

THE PROBLEM. A stale line stands about an hour. The odds API allows 500 credits
a month, a full fault scan costs 2, and hourly scanning all week is 1,456 --
so a fixed schedule can afford roughly three looks a day and will miss seven
faults in eight. Scanning more often is not affordable and scanning on a timer
spends most credits on boards that have not changed.

THE WAY OUT. ESPN publishes DraftKings' number for every upcoming game, free,
with no key and no quota -- 68 games on a September weekend. One book is not a
consensus and cannot find a fault on its own. But it is a perfect MOVEMENT
DETECTOR, and movement is the only thing that creates a stale line: the market
moves, a slow book does not follow, and for an hour its number is wrong.

So this polls the free feed as often as it likes, compares against the last
snapshot, and spends paid credits ONLY when something actually moved. Same
budget, every credit spent at the one moment it is worth something.

What it cannot do, stated plainly: if the whole market moves together, nothing
is stale and the triggered scan finds nothing. And a book that hangs a bad
number without any market move at all -- BetMGM's transposed Arizona State
record -- produces no movement to trip on. This narrows where the credits go;
it does not see everything.

  python tripwire.py --check          # free; triggers a scan if the line moved
  python tripwire.py --snapshot       # free; just record where things stand
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "cache", "tripwire.json")

SCOREBOARD = {
    "NCAAF": ("https://site.api.espn.com/apis/site/v2/sports/football/"
              "college-football/scoreboard", {"groups": 80, "limit": 200}),
    "NFL": ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
            "scoreboard", {"limit": 100}),
}

# A line that has moved this far has moved for a reason, and whoever has not
# followed is the opportunity. Half a point is noise between refreshes.
MOVE_PTS = 1.0


def _spread_from(details: str) -> Optional[Tuple[str, float]]:
    """'DEL -4.5' -> ('DEL', -4.5). ESPN writes the favourite's side."""
    if not details:
        return None
    parts = details.split()
    if len(parts) < 2:
        return None
    try:
        return parts[0], float(parts[-1])
    except ValueError:
        return None


def poll(days: int = 9) -> Dict[str, dict]:
    """
    Current DraftKings numbers, keyed by ESPN event id. Costs nothing.

    A date range is required. The bare scoreboard returns TODAY, which on a
    Sunday evening is a handful of finished games -- the first run of this saw
    six priced events and would have watched almost nothing. With the range it
    sees the whole upcoming slate, which on a September weekend is 68.
    """
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    span = f"{today:%Y%m%d}-{today + timedelta(days=days):%Y%m%d}"
    out: Dict[str, dict] = {}
    for league, (url, base) in SCOREBOARD.items():
        params = dict(base, dates=span)
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code != 200:
                continue
            events = r.json().get("events") or []
        except (requests.RequestException, ValueError):
            continue
        for ev in events:
            for comp in ev.get("competitions") or []:
                for o in comp.get("odds") or []:
                    got = _spread_from(o.get("details") or "")
                    if not got:
                        continue
                    side, pts = got
                    out[str(ev.get("id"))] = {
                        "league": league, "name": ev.get("shortName"),
                        "side": side, "spread": pts,
                        "total": o.get("overUnder"),
                    }
                    break
    return out


def load_state() -> Dict[str, dict]:
    if not os.path.exists(STATE):
        return {}
    try:
        return json.load(open(STATE))
    except ValueError:
        return {}


def save_state(snap: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(snap, open(STATE, "w"))


def movers(old: Dict[str, dict], new: Dict[str, dict]) -> list:
    """Games whose number has shifted since the last look."""
    out = []
    for eid, cur in new.items():
        was = old.get(eid)
        if not was or was.get("side") != cur.get("side"):
            continue                       # first sighting, or flipped favourite
        delta = cur["spread"] - was["spread"]
        if abs(delta) >= MOVE_PTS:
            out.append({"event": eid, "name": cur.get("name"),
                        "league": cur["league"], "from": was["spread"],
                        "to": cur["spread"], "delta": delta})
    return sorted(out, key=lambda r: -abs(r["delta"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--check", action="store_true",
                    help="poll, and exit 10 if a paid scan is warranted")
    ap.add_argument("--snapshot", action="store_true")
    a = ap.parse_args()

    new = poll()
    if not new:
        print("ESPN returned no priced games — nothing to compare.")
        return 0
    old = load_state()
    print(f"ESPN: {len(new)} priced games (free), {len(old)} in the last snapshot")

    if a.snapshot or not old:
        save_state(new)
        print("Snapshot saved. Nothing spent.")
        return 0

    moved = movers(old, new)
    save_state(new)
    if not moved:
        print(f"No line moved {MOVE_PTS:g}pt or more. No credits spent.")
        return 0

    print(f"\n{len(moved)} line(s) moved — the market shifted, so somebody is behind:")
    for m in moved[:8]:
        print(f"   {m['league']:<6}{str(m['name'])[:26]:<27}"
              f"{m['from']:+g} -> {m['to']:+g}  ({m['delta']:+.1f})")
    if a.check:
        print("\nWorth a paid scan.")
        return 10          # the workflow reads this and spends the credits
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
