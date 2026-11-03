"""
Every Kalshi gap the watch sees, graded, so the thresholds rest on results.

WHY. The per-sport bars in faults.KALSHI_MIN_EDGE_BY came from one morning's
spread of book prices. The question that actually sets a threshold is the one
this answers: at each size of gap, in each sport, did buying at Kalshi's price
make money?

HOW. Two stages, because two jobs touch it:

  observe()  every fast scan (fault-watch.yml). One row per outcome --
             league, game, side, ticker -- holding the FIRST sighting and the
             BEST one (largest edge), in cache/kalshi_gaps.json. A row is a
             few hundred bytes and is dropped three days after kickoff, by
             which time grade() has settled it.
  grade()    the board build (build-board.yml), which restores the same cache
             and commits. Reads each finished market's result from Kalshi's
             public endpoint and appends the graded row to
             data/archive/kalshi_gaps.ndjson, once per outcome.

Since 2026-11-03 each live row also holds `path`, every change of price or
consensus before kickoff, and `last`, the most recent sighting before kickoff.
Graded rows gain `close` from `last`, for judging a price against where the
market finished. Paths go to their own file, kalshi_gaps_paths.ndjson, so the
results archive stays small, and so a wait-N-minutes or last-hour rule can be
replayed instead of guessed.

Grading buys ONE contract at the best-seen price plus fee: a win pays
1 - cost, a loss costs `cost`. That is the realised return of the rule "buy
when the gap is this big", per dollar of contract.

    python kalshi_gaps.py --grade
    python kalshi_gaps.py --report
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(ROOT, "cache", "kalshi_gaps.json")
ARCHIVE = os.path.join(ROOT, "data", "archive", "kalshi_gaps.ndjson")
MARKET = "https://api.elections.kalshi.com/trade-api/v2/markets/{}"
KEEP_AFTER_KICK = timedelta(days=3)
BUCKETS = (-1.0, 0.0, 0.01, 0.02, 0.03, 0.04, 0.06)   # edge, lower bounds
# A last sighting further from kickoff than this is not a closing price: the
# game fell out of the feed, or the watch was down.
CLOSE_WITHIN = timedelta(hours=2)


def _key(r: dict) -> str:
    return f"{r['league']}|{r['game']}|{r['side']}|{r['ticker']}"


def _snap(r: dict, stamp: str) -> dict:
    return {"at": stamp, "price": r["kalshi_price"], "fee": r["fee"],
            "consensus": r["consensus"], "edge": r["edge"], "size": r["size"]}


def _load(path: str) -> dict:
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _close(e: dict) -> Optional[dict]:
    """The row's last sighting, if it is near enough to kickoff to be the close."""
    last = e.get("last")
    if not last:
        return None
    try:
        at = datetime.fromisoformat(str(last["at"]).replace("Z", "+00:00"))
        kick = datetime.fromisoformat(e["kickoff"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return None
    return last if kick - at <= CLOSE_WITHIN else None


def closes(path: str = LIVE, archive: str = ARCHIVE) -> dict:
    """{(ticker, "YES"|"NO"): closing snap}, graded rows first, live rows on top."""
    out = {}
    # A nicety on a push: an unreadable archive must not cost the push itself.
    try:
        with open(archive) as fh:
            for l in fh:
                if l.strip():
                    r = json.loads(l)
                    if r.get("close"):
                        out[(r["ticker"], r["how"])] = r["close"]
    except (OSError, ValueError, KeyError):
        pass
    for e in _load(path).values():
        c = _close(e)
        if c:
            out[(e["ticker"], e["how"])] = c
    return out


def observe(rows: List[dict], stamp: str, path: str = LIVE,
            now: Optional[datetime] = None) -> int:
    """Fold one scan's gaps into the live log. Returns rows held."""
    log = _load(path)
    now = now or datetime.now(timezone.utc)
    cutoff = now - KEEP_AFTER_KICK
    log = {k: v for k, v in log.items()
           if datetime.fromisoformat(v["kickoff"].replace("Z", "+00:00")) > cutoff}
    for r in rows:
        k = _key(r)
        snap = _snap(r, stamp)
        if k not in log:
            log[k] = {"league": r["league"], "game": r["game"], "side": r["side"],
                      "ticker": r["ticker"], "how": r["how"], "kickoff": r["kickoff"],
                      "first": snap, "best": snap, "seen": 1}
        else:
            e = log[k]
            e["seen"] += 1
            if r["edge"] > e["best"]["edge"]:
                e["best"] = snap
        e = log[k]
        # Pre-kickoff only: a live price would read as the close and as a move.
        if datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00")) > now:
            e["last"] = snap
            point = [int(now.timestamp()), r["kalshi_price"], round(r["consensus"], 3)]
            trail = e.setdefault("path", [])
            if not trail or trail[-1][1:] != point[1:]:
                trail.append(point)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(log, fh)
    return len(log)


def _result(ticker: str) -> str:
    r = requests.get(MARKET.format(ticker), timeout=20)
    r.raise_for_status()
    return (r.json().get("market") or {}).get("result") or ""


def grade(path: str = LIVE, archive: str = ARCHIVE, result=_result,
          paths: Optional[str] = None) -> int:
    """Settle finished outcomes into the archive. Each outcome is written once."""
    log = _load(path)
    done = set()
    if os.path.exists(archive):
        with open(archive) as fh:
            done = {_key(json.loads(l)) for l in fh if l.strip()}
    now = datetime.now(timezone.utc)
    new = []
    for k, e in log.items():
        if k in done:
            continue
        kick = datetime.fromisoformat(e["kickoff"].replace("Z", "+00:00"))
        if now - kick < timedelta(hours=3):
            continue
        try:
            res = result(e["ticker"])
        except Exception:
            continue
        if res not in ("yes", "no"):
            continue                       # unsettled, or a scalar (void) result
        # A NO-route row holds the opponent's market: it wins on "no".
        won = (res == "yes") == (e["how"] == "YES")
        cost = e["best"]["price"] + e["best"]["fee"]
        new.append(dict(e, result=res, won=won, close=_close(e),
                        pnl=round((1 - cost) if won else -cost, 4),
                        graded_at=now.isoformat(timespec="seconds")))
    if new:
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        paths = paths or os.path.splitext(archive)[0] + "_paths.ndjson"
        # Paths first: a row in the archive is never graded again, so a crash
        # here must not leave a graded outcome with its path lost.
        with open(paths, "a") as fh:
            for row in new:
                if row.get("path"):
                    fh.write(json.dumps({"key": _key(row), "kickoff": row["kickoff"],
                                         "path": row["path"]}) + "\n")
        with open(archive, "a") as fh:
            for row in new:
                fh.write(json.dumps({k: v for k, v in row.items()
                                     if k not in ("path", "last")}) + "\n")
    print(f"[kalshi_gaps] graded {len(new)}; {len(log)} live")
    return len(new)


def report(archive: str = ARCHIVE) -> str:
    """By sport and by best-seen edge: count, win rate vs price, return per $1."""
    if not os.path.exists(archive):
        return "No graded Kalshi gaps yet."
    rows = [json.loads(l) for l in open(archive) if l.strip()]
    by: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        e = r["best"]["edge"]
        lo = max(b for b in BUCKETS if e >= b) if e >= BUCKETS[0] else BUCKETS[0]
        by[(r["league"], lo)].append(r)
    out = [f"{len(rows)} graded Kalshi outcomes\n",
           f"{'sport':7} {'edge >=':>8} {'n':>5} {'won':>6} {'paid':>6} {'return/$':>9}"]
    for (lg, lo), rs in sorted(by.items()):
        n = len(rs)
        won = sum(r["won"] for r in rs) / n
        paid = sum(r["best"]["price"] + r["best"]["fee"] for r in rs) / n
        ret = sum(r["pnl"] for r in rs) / sum(r["best"]["price"] + r["best"]["fee"] for r in rs)
        out.append(f"{lg:7} {lo*100:7.0f}pp {n:5} {won*100:5.1f}% {paid*100:5.1f}c {ret*100:+8.1f}%")
    out += _close_report(rows)
    return "\n".join(out)


def _close_report(rows: List[dict]) -> List[str]:
    """Bets the rule would make, against the close: the read that needs no wins."""
    rows = [r for r in rows if r.get("close") and r["best"]["edge"] > 0]
    if not rows:
        return ["\nVs the close: no graded gaps with a closing price yet."]
    out = ["\nVs the close, positive gaps, cost at the best sighting",
           f"{'edge':>8} {'n':>5} {'cost':>6} {'books':>6} {'kalshi':>7} {'vs books':>9}"]
    for lo, hi in ((0.0, 0.01), (0.01, 0.03), (0.03, 0.06), (0.06, 9.0)):
        rs = [r for r in rows if lo < r["best"]["edge"] <= hi]
        if not rs:
            continue
        n = len(rs)
        cost = sum(r["best"]["price"] + r["best"]["fee"] for r in rs) / n
        books = sum(r["close"]["consensus"] for r in rs) / n
        kalshi = sum(r["close"]["price"] for r in rs) / n
        out.append(f"{lo*100:6.0f}pp+ {n:5} {cost*100:5.1f}c {books*100:5.1f}% "
                   f"{kalshi*100:6.1f}c {(books - cost)*100:+8.1f}c")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.grade:
        grade()
    if a.report or not a.grade:
        print(report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
