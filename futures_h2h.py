"""
Pick every NFL game by Super Bowl futures: the team the books give the better
chance of winning it all is the pick. How often does that team win?

Futures are the de-vigged median across books captured in
data/archive/futures_<season>.ndjson (futures.py --capture). Each game is
ranked with the LAST capture taken before its kickoff, and a game that
kicked off before the first capture is left out -- week 1's early Sunday
games were already in progress when the first one was taken.

The pick is priced at the last DraftKings moneyline archived before kickoff
(lines_NFL_<season>.ndjson), so the table can say what $100 a game returned,
not just the win rate.

--today ranks this week's remaining games with the latest capture. Captures
come from the odds API (seven books) or, when its credits are gone, Action
Network (DraftKings, FanDuel, BetMGM, Caesars, bet365).

    python futures_h2h.py            # graded games so far
    python futures_h2h.py --today    # this week's picks
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from collections import defaultdict
from typing import Dict, List, Optional

import espn

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "data", "archive")
SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
              "scoreboard?dates={year}&seasontype=2&week={week}")


def _ts(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def captures(season: int) -> List[tuple]:
    """[(captured_at, {team: fair_prob})], oldest first. One fair per team per capture."""
    by: Dict[str, Dict[str, float]] = defaultdict(dict)
    for line in open(os.path.join(ARCHIVE, f"futures_{season}.ndjson")):
        r = json.loads(line)
        by[r["captured_at"]][r["team"]] = r["fair_prob"]
    return sorted((_ts(k), v) for k, v in by.items())


def closing_ml(season: int) -> Dict[str, dict]:
    """event_id -> last archived line row before kickoff."""
    out: Dict[str, dict] = {}
    for line in open(os.path.join(ARCHIVE, f"lines_NFL_{season}.ndjson")):
        r = json.loads(line)
        if _ts(r["captured_at"]) >= _ts(r["kickoff"]) or r.get("ml_home") is None:
            continue
        cur = out.get(r["event_id"])
        if not cur or r["captured_at"] > cur["captured_at"]:
            out[r["event_id"]] = r
    return out


def games(season: int, weeks) -> List[dict]:
    out = []
    for w in weeks:
        d = espn.fetch_scoreboard(SCOREBOARD.format(year=season, week=w))
        for ev in d.get("events") or []:
            comp = ev["competitions"][0]
            side = {c["homeAway"]: c for c in comp["competitors"]}
            out.append({
                "week": w, "event_id": str(ev["id"]), "kickoff": ev["date"],
                "done": bool(comp["status"]["type"].get("completed")),
                "home": side["home"]["team"]["displayName"],
                "away": side["away"]["team"]["displayName"],
                "home_abbr": side["home"]["team"]["abbreviation"],
                "away_abbr": side["away"]["team"]["abbreviation"],
                "home_pts": int(side["home"].get("score") or 0),
                "away_pts": int(side["away"].get("score") or 0)})
    return out


def _profit(american: int) -> float:
    return american / 100 if american > 0 else 100 / -american


def graded(season: int, weeks) -> int:
    caps = captures(season)
    ml = closing_ml(season)
    rows = []
    for g in games(season, weeks):
        if not g["done"]:
            continue
        ko = _ts(g["kickoff"])
        before = [c for t, c in caps if t < ko]
        if not before:
            continue
        fut = before[-1]
        ph, pa = fut.get(g["home"]), fut.get(g["away"])
        if ph is None or pa is None:
            continue
        pick_home = ph >= pa
        won = (g["home_pts"] > g["away_pts"]) == pick_home
        tie = g["home_pts"] == g["away_pts"]
        line = ml.get(g["event_id"])
        price = None if not line else (line["ml_home"] if pick_home else line["ml_away"])
        fav_home = None if not line else line["ml_home"] < line["ml_away"]
        rows.append(dict(g, pick=g["home_abbr"] if pick_home else g["away_abbr"],
                         ratio=max(ph, pa) / min(ph, pa), won=won and not tie, tie=tie,
                         price=price, agrees=None if fav_home is None else fav_home == pick_home))

    print(f"NFL {season}: pick the team with the better Super Bowl odds "
          f"(de-vigged median across books, last capture before kickoff)\n")
    for w in sorted({r["week"] for r in rows}):
        wr = [r for r in rows if r["week"] == w]
        print(f"week {w}: {sum(r['won'] for r in wr)}-{sum(not r['won'] and not r['tie'] for r in wr)}")
        for r in sorted(wr, key=lambda r: -r["ratio"]):
            res = "TIE" if r["tie"] else ("won" if r["won"] else "LOST")
            pr = f"{r['price']:+d}" if r["price"] is not None else "  n/a"
            flag = "" if r["agrees"] in (True, None) else "   <- market favoured the other side"
            print(f"   {r['away_abbr']:>3} @ {r['home_abbr']:<3} pick {r['pick']:<3} "
                  f"{r['ratio']:5.1f}x  {pr:>5}  {r['away_pts']:>2}-{r['home_pts']:<2} {res}{flag}")

    n = len(rows)
    w = sum(r["won"] for r in rows)
    priced = [r for r in rows if r["price"] is not None and not r["tie"]]
    net = sum(100 * _profit(r["price"]) if r["won"] else -100 for r in priced)
    agree = [r for r in rows if r["agrees"] is not None]
    print(f"\ntotal {w}-{n - w - sum(r['tie'] for r in rows)}"
          f"{'-' + str(sum(r['tie'] for r in rows)) if any(r['tie'] for r in rows) else ''}"
          f" = {w / n * 100:.1f}%   ({n} games ranked; earlier kickoffs had no capture)")
    print(f"$100 on every pick at the pre-game DraftKings moneyline: {net:+,.0f} "
          f"over {len(priced)} bets ({net / len(priced):+.1f}%)")
    print(f"futures pick = moneyline favourite in {sum(r['agrees'] for r in agree)}"
          f" of {len(agree)} games")
    print("\nby how lopsided the futures gap was:")
    for lo, hi in ((1, 2), (2, 4), (4, 10), (10, 1e9)):
        b = [r for r in rows if lo <= r["ratio"] < hi]
        if b:
            bw = sum(r["won"] for r in b)
            bp = [r for r in b if r["price"] is not None and not r["tie"]]
            bn = sum(100 * _profit(r["price"]) if r["won"] else -100 for r in bp)
            label = f"{lo:g}-{hi:g}x" if hi < 1e9 else f"{lo:g}x+"
            print(f"   {label:>7}  {bw}-{len(b) - bw}  {bw / len(b) * 100:5.1f}%   $100 each: {bn:+,.0f}")
    return 0


def current_week(season: int) -> int:
    d = espn.fetch_scoreboard(SCOREBOARD.split("?")[0])
    return int((d.get("week") or {}).get("number") or 1)


def today(season: int, week: int) -> int:
    caps = captures(season)
    at, fut = caps[-1]
    ml = closing_ml(season)
    print(f"NFL week {week} picks, by Super Bowl odds captured {at:%Y-%m-%d %H:%M} UTC\n")
    for g in sorted(games(season, [week]), key=lambda g: g["kickoff"]):
        if g["done"]:
            continue
        h, a = fut.get(g["home"]), fut.get(g["away"])
        if h is None or a is None:
            continue
        pick = g["home_abbr"] if h >= a else g["away_abbr"]
        line = ml.get(g["event_id"])
        price = "" if not line else f"{line['ml_home'] if h >= a else line['ml_away']:+d}"
        fav = "" if not line or (line["ml_home"] < line["ml_away"]) == (h >= a) \
            else "   <- moneyline favours the other side"
        print(f"  {g['kickoff'][5:10]} {g['away_abbr']:>3} @ {g['home_abbr']:<3} pick {pick:<3} "
              f"{max(h, a) / min(h, a):5.1f}x  ML {price:>5}{fav}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--week", type=int, default=0, help="default: this week")
    a = ap.parse_args()
    a.week = a.week or current_week(a.season)
    if a.today:
        return today(a.season, a.week)
    return graded(a.season, range(1, a.week + 1))


if __name__ == "__main__":
    raise SystemExit(main())
