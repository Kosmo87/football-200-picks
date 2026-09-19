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

THE HISTORY DOES EXIST -- ON A DIFFERENT HOST. The paragraph above is about
site.api.espn.com, and it is still true of that path. But ESPN's CORE api
serves genuinely dated polls:

  sports.core.api.espn.com/v2/sports/football/leagues/college-football/
      seasons/{year}/types/{2}/weeks/{week}/rankings/{1 = AP, 2 = coaches}

Weeks differ from one another, carry the right team records, and are dated the
Sunday they were released. The preseason poll is seasontype 1 week 1; regular
seasontype 2 starts at week 2, and week 1 there is a 404. No key is needed,
and collegefootballdata.com is not required after all.

fetch_poll() below reads that host. THE TRAP IT REPLACES IS STILL LIVE: a poll
dated Sunday describes the weekend that just ENDED, so selecting teams with
"the poll on or before this weekend" hands the bettor Sunday's poll for
Saturday's games. That mistake returned an 11-0 season and +$85,000 the first
time it was run. Polls must be filtered STRICTLY BEFORE the slate opens.

WHAT --capture STILL DOES. Appends the current poll weekly. Kept because it
records what we saw at the time, which no backfill can prove.

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

# The core host, which honours season and week. AP is poll 1, coaches is 2.
CORE_URL = ("https://sports.core.api.espn.com/v2/sports/football/leagues/"
            "college-football/seasons/{year}/types/{stype}/weeks/{week}/rankings/{poll}")

# Preseason lives under seasontype 1; regular-season week 1 does not exist.
POLL_WEEKS = [(1, 1)] + [(2, w) for w in range(2, 18)]


def fetch_poll(year: int, stype: int, week: int, poll: int = 1) -> Optional[dict]:
    """
    One dated poll: {"released": date, "name": str, "ranks": {team_id: rank}}.

    `released` is the date the poll came out, and it is the field that keeps
    this honest -- it is a SUNDAY, at the end of the weekend it judges, so a
    caller picking a poll for a slate must require released < first kickoff.
    Returned rather than dropped so the caller cannot forget it exists.
    """
    url = CORE_URL.format(year=year, stype=stype, week=week, poll=poll)
    r = requests.get(url, timeout=25)
    if r.status_code != 200:
        return None
    d = r.json()
    ranks = {}
    for rk in d.get("ranks") or []:
        tid = str((rk.get("team") or {}).get("$ref", "")).split("/")[-1].split("?")[0]
        if tid:
            ranks[tid] = int(rk.get("current") or 0)
    if not ranks:
        return None
    from datetime import datetime
    return {"released": datetime.strptime(d["date"], "%Y-%m-%dT%H:%MZ").date(),
            "name": d.get("shortName") or d.get("name") or "poll",
            "week": week, "ranks": ranks}


def season_polls(year: int, poll: int = 1) -> List[dict]:
    """Every poll of one season, oldest first."""
    out = []
    for stype, week in POLL_WEEKS:
        got = fetch_poll(year, stype, week, poll)
        if got:
            out.append(got)
    return sorted(out, key=lambda p: p["released"])


def poll_in_effect(polls: List[dict], kickoff_date) -> Optional[dict]:
    """
    The last poll released STRICTLY BEFORE the slate opens.

    Strict on purpose, and the whole reason this function exists rather than
    being written inline at each call site: `<=` silently admits the Sunday
    poll that already knows Saturday's results.
    """
    live = [p for p in polls if p["released"] < kickoff_date]
    return live[-1] if live else None


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
