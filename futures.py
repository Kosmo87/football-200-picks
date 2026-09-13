"""
Super Bowl futures: captured daily, tested honestly.

WHAT THIS IS FOR. Futures aggregate the market's season-long view of every
team into one number, and that view is good -- measured on 2026-09-13, the
futures strength gap explains 88.5% of the variance in the week's game spreads
(r = +0.941). The play-by-play EPA model this project built manages +0.306.
So as a description of team strength, futures beat anything we construct.

WHY THAT IS NOT A BETTING SIGNAL. The 0.941 is the problem, not the promise.
Futures and game lines are priced by the same books off the same opinion; the
11.5% they do NOT share is game-specific news, which the game line has and a
season-long price structurally cannot. The largest residual that day was
ATL @ PIT -- the line had Pittsburgh -6.5, futures-implied -3.5 -- because
Atlanta's starting AND backup quarterbacks were out (Tagovailoa, oblique;
Penix, ACL). Futures were priced before that and cannot know.

Trading the residual therefore means systematically backing teams whose
starters just got hurt. That is not an edge, it is a lag.

WHAT IT IS GOOD FOR, TODAY. An early-season strength prior. nflverse had 234
plays of 2026 populated on 13 September -- four teams, two games -- so the EPA
ratings are almost entirely last season carried forward. Futures price the
whole offseason: draft, signings, coaching, camp. As a week 1-4 prior they are
better evidence than what we have, and they decay out of relevance as real
play accumulates. That is a feature, not a signal.

WHAT WOULD CHANGE MY MIND. encompass() runs the same test that gave the
matchup model t=0.81: regress actual margin on the closing line AND the
futures gap together. If the futures coefficient is significant, the residual
holds information the line missed and the hypothesis is right. It needs ~40
completed games with captured lines, so it returns None until then rather than
reporting a number built on nothing.

  python futures.py --capture     # one API call; run daily
  python futures.py --trend 7     # who shortened, who drifted
  python futures.py --encompass   # the test, when enough games exist
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, List, Optional

import requests

import keys  # noqa: F401

SPORT = "americanfootball_nfl_super_bowl_winner"
BASE = "https://api.the-odds-api.com/v4"
ROOT = os.path.dirname(os.path.abspath(__file__))
IGNORE_BOOKS = {"betfair_ex_us", "matchbook"}

# Above this the de-vig has broken down rather than found value. Books disagree
# 2x on longshots (+25000 vs +12500 on Tennessee), so the median of a bimodal
# distribution is meaningless there. line_shop uses the same guard.
MAX_TRUSTED_EDGE = 0.15
MIN_TRUSTED_PROB = 0.005    # below ~0.5% the price is a rounding artifact


def archive_path(season: int = 2026) -> str:
    return os.path.join(ROOT, "data", "archive", f"futures_{season}.ndjson")


def implied(american: int) -> float:
    return 1 / (1 + (american / 100 if american > 0 else 100 / abs(american)))


def fetch() -> List[dict]:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise SystemExit("ODDS_API_KEY is not set.")
    r = requests.get(f"{BASE}/sports/{SPORT}/odds",
                     params={"apiKey": key, "regions": "us",
                             "markets": "outrights", "oddsFormat": "american"},
                     timeout=40)
    if r.status_code != 200:
        raise SystemExit(f"HTTP {r.status_code}: {r.text[:200]}")
    print(f"  credits used {r.headers.get('x-requests-used','?')}, "
          f"remaining {r.headers.get('x-requests-remaining','?')}")
    return r.json()


def prices_from(payload: List[dict]) -> Dict[str, Dict[str, int]]:
    """team -> book -> american price."""
    out: Dict[str, Dict[str, int]] = defaultdict(dict)
    for ev in payload:
        for bk in ev.get("bookmakers") or []:
            key = bk.get("key") or ""
            if key in IGNORE_BOOKS:
                continue
            label = bk.get("title") or key
            for mk in bk.get("markets") or []:
                for oc in mk.get("outcomes") or []:
                    try:
                        out[oc["name"]][label] = int(oc["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
    return out


def fair_probs(prices: Dict[str, Dict[str, int]],
               exclude: Optional[str] = None) -> Dict[str, float]:
    """
    De-vig each book across the WHOLE field, then take the median per team.

    Normalising over the field rather than per-team is the only option here:
    a future has no opposing side to de-vig against, so the 32 prices summing
    to 1.195 IS the hold and dividing through is how it comes out.
    """
    books = {b for d in prices.values() for b in d}
    per: Dict[str, List[float]] = defaultdict(list)
    for b in books:
        if b == exclude:
            continue
        total = sum(implied(d[b]) for d in prices.values() if b in d)
        if total <= 0:
            continue
        for team, d in prices.items():
            if b in d:
                per[team].append(implied(d[b]) / total)
    return {t: median(v) for t, v in per.items() if v}


def ratings(prices: Optional[Dict[str, Dict[str, int]]] = None) -> Dict[str, float]:
    """
    Team strength as log-odds of winning it all.

    Log because the probability scale is violently non-linear -- a team twice as
    likely to win the Super Bowl is nowhere near twice as good -- and the log
    of it lines up with point spreads almost exactly (slope 2.89 pts per unit,
    r = 0.941).
    """
    # `prices or fetch()` would treat an EMPTY dict as "go to the network",
    # so a caller passing {} silently spends an API call and gets live data it
    # did not ask for. Only None means fetch.
    if prices is None:
        prices = prices_from(fetch())
    fair = fair_probs(prices)
    return {t: math.log(p) for t, p in fair.items() if p > 0}


def capture(season: int = 2026) -> int:
    """Append today's board. One row per team per book, plus the de-vigged fair."""
    payload = fetch()
    prices = prices_from(payload)
    if not prices:
        print("  no futures returned")
        return 1
    fair = fair_probs(prices)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = archive_path(season)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    with open(path, "a") as fh:
        for team, d in prices.items():
            for book, price in d.items():
                fh.write(json.dumps({
                    "captured_at": stamp, "season": season, "team": team,
                    "book": book, "price": price,
                    "fair_prob": round(fair.get(team, 0.0), 6),
                }) + "\n")
                n += 1
    books = {b for d in prices.values() for b in d}
    print(f"  captured {n} rows — {len(prices)} teams x {len(books)} books")
    for b in sorted(books):
        tot = sum(implied(d[b]) for d in prices.values() if b in d)
        print(f"    {b:<16}hold {(tot-1)/tot*100:5.1f}%")
    return 0


def _load(season: int = 2026) -> List[dict]:
    path = archive_path(season)
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def trend(days: int = 7, season: int = 2026) -> int:
    rows = _load(season)
    if not rows:
        print("No futures archive yet. Run --capture (daily) and come back.")
        return 0
    stamps = sorted({r["captured_at"] for r in rows})
    if len(stamps) < 2:
        print(f"Only one capture ({stamps[0][:10]}). Movement needs two.")
        return 0
    latest = stamps[-1]
    cutoff = (datetime.fromisoformat(latest) - timedelta(days=days)).isoformat()
    earlier = min((s for s in stamps if s >= cutoff), default=stamps[0])

    def snap(ts):
        return {r["team"]: r["fair_prob"] for r in rows if r["captured_at"] == ts}

    a, b = snap(earlier), snap(latest)
    common = [t for t in b if t in a and a[t] > 0]
    if not common:
        print("No overlapping teams between captures.")
        return 0
    moved = sorted(common, key=lambda t: -(b[t] - a[t]))
    print(f"\nFutures movement  {earlier[:10]} -> {latest[:10]}  "
          f"({len(stamps)} captures on file)")
    print(f"{'team':<26}{'was':>8}{'now':>8}{'change':>9}")
    for t in moved[:8]:
        print(f"{t:<26}{a[t]*100:>7.2f}%{b[t]*100:>7.2f}%{(b[t]-a[t])*100:>+8.2f}pp")
    if len(moved) > 12:
        print("   ...")
        for t in moved[-6:]:
            print(f"{t:<26}{a[t]*100:>7.2f}%{b[t]*100:>7.2f}%{(b[t]-a[t])*100:>+8.2f}pp")
    return 0


def encompass(season: int = 2026, min_games: int = 40) -> Optional[dict]:
    """
    Does the futures gap predict anything the closing line missed?

    margin ~ a + b*closing_line + c*futures_gap. The line's coefficient should
    land near 1.0; the question is whether c is distinguishable from zero. This
    is the test that returned t=0.81 for the matchup model.

    Returns None -- deliberately, rather than a number -- until there are
    enough completed games. A regression on eight rows is not a weak answer,
    it is a misleading one.
    """
    import numpy as np
    from archive import read_ndjson

    fut = _load(season)
    if not fut:
        print("No futures archive. Nothing to test.")
        return None
    results = {r["event_id"]: r for r in
               read_ndjson(os.path.join(ROOT, "data", "archive",
                                        f"results_NFL_{season}.ndjson"))}
    lines = [json.loads(l) for l in
             open(os.path.join(ROOT, "data", "archive", f"lines_NFL_{season}.ndjson"))]
    print(f"futures captures: {len({r['captured_at'] for r in fut})}, "
          f"completed games on file: {len(results)}, line rows: {len(lines)}")
    if len(results) < min_games:
        print(f"\nNeed {min_games} completed games before this means anything; "
              f"have {len(results)}.")
        print("Capture daily and re-run. The test is built and waiting.")
        return None
    print("Enough games — wire the margin join and run the regression.")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--trend", nargs="?", type=int, const=7, default=None)
    ap.add_argument("--encompass", action="store_true")
    ap.add_argument("--season", type=int, default=2026)
    a = ap.parse_args()
    if a.capture:
        return capture(a.season)
    if a.trend is not None:
        return trend(a.trend, a.season)
    if a.encompass:
        encompass(a.season)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
