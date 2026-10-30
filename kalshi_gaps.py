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
from typing import Dict, List

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(ROOT, "cache", "kalshi_gaps.json")
ARCHIVE = os.path.join(ROOT, "data", "archive", "kalshi_gaps.ndjson")
MARKET = "https://api.elections.kalshi.com/trade-api/v2/markets/{}"
KEEP_AFTER_KICK = timedelta(days=3)
BUCKETS = (-1.0, 0.0, 0.01, 0.02, 0.03, 0.04, 0.06)   # edge, lower bounds


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


def observe(rows: List[dict], stamp: str, path: str = LIVE) -> int:
    """Fold one scan's gaps into the live log. Returns rows held."""
    log = _load(path)
    cutoff = datetime.now(timezone.utc) - KEEP_AFTER_KICK
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(log, fh)
    return len(log)


def _result(ticker: str) -> str:
    r = requests.get(MARKET.format(ticker), timeout=20)
    r.raise_for_status()
    return (r.json().get("market") or {}).get("result") or ""


def grade(path: str = LIVE, archive: str = ARCHIVE, result=_result) -> int:
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
        new.append(dict(e, result=res, won=won,
                        pnl=round((1 - cost) if won else -cost, 4),
                        graded_at=now.isoformat(timespec="seconds")))
    if new:
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        with open(archive, "a") as fh:
            for row in new:
                fh.write(json.dumps(row) + "\n")
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
    return "\n".join(out)


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
