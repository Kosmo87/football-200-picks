"""
Wind unders, on paper until the forecast proves it can find them.

THE FINDING. In 5,205 outdoor NFL games, 1999-2025 (cache/nflverse_games.csv),
the under against the closing total went:

    wind          games   under
    0-10 mph      3,309   48.8%
    10-15 mph     1,212   53.8%
    15-20 mph       497   57.3%
    20+ mph         187   56.0%

At 15 mph and up that is 384-290, 57.0%, +8.8% at -110 -- and it held in
both halves, 57.2% for 1999-2014 and 56.4% for 2015-2025, which nothing else
this project has tested managed. The market does shade the total for wind,
about a point (45.0 to 43.9), and the games still land 1.5-2.5 under it.
Spreads show nothing: dogs cover 50-51% at every wind speed.

WHY IT IS ON PAPER. The archive's wind is what was MEASURED at kickoff. A bet
is placed on a FORECAST. Some of those windy afternoons were not forecast
windy, and the part of the 57% that came from them is not available to
anyone. Only a forward test on the forecast can say how much is left. Also:
only 15 of 27 seasons cleared 52.4%, and about 25 games a season qualify.

WHAT THIS LOGS. Every outdoor NFL game the hourly build sees, not only the
windy ones, so the threshold can be revisited without having to wait another
season:

    first   the first snapshot inside 24 hours of kickoff
    close   the last snapshot before kickoff -- the bet the rule makes

Each snapshot is Open-Meteo's forecast for the kickoff hour plus ESPN's
posted total (DraftKings). ESPN drops the total once a game starts, so the
last pre-kickoff run is the closing number.

    python windtest.py --log         # snapshot this week's outdoor games
    python windtest.py --settle      # grade finished games
    python windtest.py --record      # the paper record, rule and control
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import context
from espn import ESPN_NFL, fetch_scoreboard

ROOT = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.join(ROOT, "data", "archive", "wind_paper.ndjson")

# The backtest's line, carried forward unchanged. Sustained wind, not gusts:
# the archive's number is sustained, and swapping in gusts would test a
# different rule than the one that was measured.
WIND_MPH = 15.0

# The `first` snapshot is taken inside this window, which is roughly when a
# bet off the forecast would actually be made.
FIRST_WINDOW = timedelta(hours=24)

PRICE = -110


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ko(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def rows() -> List[dict]:
    if not os.path.exists(PAPER):
        return []
    with open(PAPER) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def save(rs: List[dict]) -> None:
    os.makedirs(os.path.dirname(PAPER), exist_ok=True)
    with open(PAPER, "w") as fh:
        for r in sorted(rs, key=lambda r: (r["kickoff"], r["event_id"])):
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def upcoming(scoreboard: dict) -> List[dict]:
    """Outdoor NFL games not yet started: id, kickoff, venue, posted total."""
    out = []
    for ev in scoreboard.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        if (ev.get("status") or {}).get("type", {}).get("state") != "pre":
            continue
        v = comp.get("venue") or {}
        if v.get("indoor"):
            continue
        addr = v.get("address") or {}
        odds = (comp.get("odds") or [{}])[0]
        total = odds.get("overUnder")
        out.append({
            "event_id": str(ev.get("id")),
            "kickoff": ev.get("date"),
            "game": ev.get("shortName") or ev.get("name") or "",
            "venue": v.get("fullName") or "",
            "city": addr.get("city") or "",
            "state": addr.get("state") or "",
            "total": float(total) if total is not None else None,
            "book": (odds.get("provider") or {}).get("name"),
        })
    return out


def snapshot(game: dict, weather: Optional[dict], at: datetime) -> dict:
    w = weather or {}
    return {"at": at.isoformat(timespec="seconds"), "total": game["total"],
            "book": game.get("book"), "wind_mph": w.get("wind_mph"),
            "gust_mph": w.get("gust_mph"), "temp_f": w.get("temp_f"),
            "precip_pct": w.get("precip_pct")}


def record_snapshots(games: List[dict], forecasts: Dict[str, Optional[dict]],
                     ledger: List[dict], now: Optional[datetime] = None) -> int:
    """
    Fold one run's observations into the ledger. Returns rows touched.

    A snapshot without both a forecast and a total is skipped rather than
    written half-empty: a `close` with no total would grade against nothing,
    and one with no wind would quietly drop the game from the rule.
    """
    now = now or _now()
    by_id = {r["event_id"]: r for r in ledger}
    touched = 0
    for g in games:
        ko = _ko(g["kickoff"])
        if ko <= now or ko - now > FIRST_WINDOW:
            continue
        w = forecasts.get(g["event_id"])
        if not w or w.get("wind_mph") is None or g["total"] is None:
            continue
        snap = snapshot(g, w, now)
        r = by_id.get(g["event_id"])
        if r is None:
            r = {k: g[k] for k in ("event_id", "kickoff", "game", "venue")}
            r.update(first=snap, status="pending")
            ledger.append(r)
            by_id[g["event_id"]] = r
        if r.get("status") != "pending":
            continue
        r["close"] = snap
        r["kickoff"] = g["kickoff"]  # a flexed game moves; grade the real one
        touched += 1
    return touched


def grade_row(r: dict, home: int, away: int, at: Optional[str] = None) -> dict:
    """Under at both snapshots' totals. A push is recorded, not dropped."""
    pts = home + away
    r["final_total"] = pts
    for key in ("first", "close"):
        snap = r.get(key) or {}
        if snap.get("total") is None:
            continue
        d = pts - snap["total"]
        snap["under"] = "push" if d == 0 else ("won" if d < 0 else "lost")
    r["status"] = "graded"
    r["graded_at"] = at or _now().isoformat(timespec="seconds")
    return r


def settle() -> int:
    from espn import fetch_completed_games
    ledger = rows()
    pending = [r for r in ledger if r.get("status") == "pending"]
    if not pending:
        return 0
    finals = {str(g.event_id): g for g in fetch_completed_games("NFL")}
    n = 0
    for r in pending:
        g = finals.get(r["event_id"])
        if g is None:
            continue
        grade_row(r, g.home_score, g.away_score)
        n += 1
    if n:
        save(ledger)
    return n


def tally(ledger: List[dict], snap: str = "close",
          lo: float = WIND_MPH, hi: float = 999.0) -> dict:
    """Under record for graded games whose `snap` forecast falls in [lo, hi)."""
    w = l = p = 0
    for r in ledger:
        s = r.get(snap) or {}
        wind = s.get("wind_mph")
        if r.get("status") != "graded" or wind is None or not lo <= wind < hi:
            continue
        res = s.get("under")
        w += res == "won"
        l += res == "lost"
        p += res == "push"
    win = 100.0 / -PRICE
    units = w * win - l
    return {"won": w, "lost": l, "push": p, "units": round(units, 2),
            "pct": round(100.0 * w / (w + l), 1) if w + l else None}


def run_log() -> int:
    games = upcoming(fetch_scoreboard(ESPN_NFL))
    cache = context._load_venue_cache()
    now = _now()
    forecasts: Dict[str, Optional[dict]] = {}
    for g in games:
        if not timedelta(0) < _ko(g["kickoff"]) - now <= FIRST_WINDOW or not g["city"]:
            continue
        coords = context.geocode(g["city"], g["state"], cache)
        if coords:
            forecasts[g["event_id"]] = context.fetch_forecast(
                coords["lat"], coords["lon"], g["kickoff"])
    context._save_venue_cache(cache)
    ledger = rows()
    n = record_snapshots(games, forecasts, ledger, now)
    if n:
        save(ledger)
    windy = [g for g in games if (forecasts.get(g["event_id"]) or {}).get("wind_mph", 0) >= WIND_MPH]
    print(f"  snapshotted {n} outdoor game(s) inside {FIRST_WINDOW.total_seconds()/3600:.0f}h")
    for g in windy:
        f = forecasts[g["event_id"]]
        print(f"    WINDY  {g['game']:<12} {f['wind_mph']:.0f} mph (gust {f['gust_mph']:.0f})"
              f"  under {g['total']}  [paper]")
    return 0


def run_record() -> int:
    ledger = rows()
    graded = [r for r in ledger if r.get("status") == "graded"]
    print(f"\n  Wind unders, paper. {len(ledger)} outdoor games logged, {len(graded)} graded.")
    print(f"  Backtest to beat: 57.0% at >= {WIND_MPH:.0f} mph observed; 52.4% breaks even.\n")
    bands = [(0, 10), (10, WIND_MPH), (WIND_MPH, 20), (20, 999)]
    print(f"  {'forecast wind':<16}{'at close':>16}{'at first':>16}")
    for lo, hi in bands:
        cells = []
        for snap in ("close", "first"):
            t = tally(ledger, snap, lo, hi)
            cells.append(f"{t['won']}-{t['lost']}" + (f" {t['pct']:.0f}%" if t["pct"] is not None else ""))
        name = f"{lo:.0f}-{hi:.0f} mph" if hi < 999 else f"{lo:.0f}+ mph"
        print(f"  {name:<16}{cells[0]:>16}{cells[1]:>16}")
    rule = tally(ledger)
    print(f"\n  RULE (close forecast >= {WIND_MPH:.0f} mph): {rule['won']}-{rule['lost']}"
          + (f"-{rule['push']}" if rule["push"] else "")
          + f", {rule['units']:+.2f}u at {PRICE}")
    if rule["won"] + rule["lost"] < 50:
        print("  (under 50 settled: this cannot tell 57% from 52% yet)")
    for r in sorted(ledger, key=lambda x: x["kickoff"]):
        c = r.get("close") or {}
        if (c.get("wind_mph") or 0) >= WIND_MPH:
            print(f"    {r['kickoff'][:10]}  {r['game']:<12} {c['wind_mph']:.0f} mph  "
                  f"u{c.get('total')}  {r.get('final_total', '-')}  {c.get('under', 'pending')}")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    if a.settle:
        print(f"  graded {settle()} game(s)")
    if a.log:
        run_log()
    if a.record or not (a.log or a.settle):
        run_record()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
