"""
Weekly polls: captured going forward, because history is not available.

WHAT HAPPENED HERE, SO IT DOES NOT HAPPEN AGAIN. The first version of this
file fetched ESPN's rankings with a `week` parameter and tested three seasons
of college games with it. The result looked excellent -- backing the ranked
team against an unranked one went 56.0% ATS over 664 games, z = +3.10, which
would have been the first positive finding in this project.

It was contamination. ESPN's rankings path IGNORES season and week entirely
and always returns the CURRENT poll: all sixteen weeks of "2025" came back
byte-identical, topped by Texas, Georgia, Notre Dame, Indiana and Miami --
which is today's board, not 2025's. Every historical game had been labelled
with a poll published after it finished, so the test was asking "did teams
that are good NOW beat expectations THEN", and the answer to that is yes for
reasons that cannot be bet.

Every parameter form was tried -- season, week, seasontype, dates -- and all
five return the same current poll. There is no historical poll behind this
endpoint. The check that caught it is cheap and worth repeating on any dated
feed: ask for two different weeks and confirm the answers differ.

WHAT THIS FILE DOES NOW. Captures the current poll, weekly, so a real history
accumulates from here. Testing whether polls carry information the price has
missed needs either that history or a source that actually serves dated polls
(collegefootballdata.com has one behind a free key). Until then the question
is open, and this file makes no claim about it.

TWO HEADER REGIMES. The rankings path 403s on espn.py's browser-shaped headers
and returns 200 to a plain client, exactly as /injuries does. Sending nothing
is what works.

  python rankings.py --capture    # run weekly
  python rankings.py --show
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
RANK_URL = ("https://site.api.espn.com/apis/site/v2/sports/football/"
            "college-football/rankings")


ARCHIVE = os.path.join(ROOT, "data", "archive", "rankings.ndjson")


def fetch_current() -> Tuple[str, Dict[str, int]]:
    """
    (poll name, {team_id: rank}) for whatever poll is current.

    No season or week is sent, because they are ignored -- pretending
    otherwise is what produced a contaminated backtest. Bare requests.get:
    espn.HEADERS makes this path 403.
    """
    r = requests.get(RANK_URL, timeout=25)
    if r.status_code != 200:
        raise SystemExit(f"rankings HTTP {r.status_code}")
    polls = r.json().get("rankings") or []
    if not polls:
        raise SystemExit("no polls returned")
    poll = polls[0]
    ranks = {}
    for t in poll.get("ranks") or []:
        tid = str((t.get("team") or {}).get("id") or "")
        if tid:
            ranks[tid] = int(t.get("current") or 0)
    return (poll.get("shortName") or poll.get("name") or "poll"), ranks


def capture() -> int:
    """Append today's poll. Weekly; the history has to be built forward."""
    from datetime import datetime, timezone
    name, ranks = fetch_current()
    if not ranks:
        print("  empty poll — nothing written")
        return 1
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(ARCHIVE), exist_ok=True)
    prev = None
    if os.path.exists(ARCHIVE):
        rows = [json.loads(l) for l in open(ARCHIVE) if l.strip()]
        if rows:
            last = max(r["captured_at"] for r in rows)
            prev = {r["team_id"]: r["rank"] for r in rows if r["captured_at"] == last}
    if prev == ranks:
        print(f"  {name} unchanged since the last capture — not written again")
        return 0
    with open(ARCHIVE, "a") as fh:
        for tid, rk in ranks.items():
            fh.write(json.dumps({"captured_at": stamp, "poll": name,
                                 "team_id": tid, "rank": rk}) + "\n")
    print(f"  captured {name}: {len(ranks)} teams at {stamp[:10]}")
    return 0


def show() -> int:
    name, ranks = fetch_current()
    print(f"\n{name}")
    for tid, rk in sorted(ranks.items(), key=lambda kv: kv[1])[:25]:
        print(f"  {rk:>2}. team {tid}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--capture", action="store_true", help="append the current poll")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.capture:
        return capture()
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
