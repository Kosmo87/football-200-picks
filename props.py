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
import re
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

import keys  # noqa: F401  (loads ~/.football-picks.env)
from archive import ARCHIVE_DIR, append_ndjson, read_ndjson, utcnow
from playerlogs import load_player_logs
from espn import current_season_year

BASE = "https://api.sportsgameodds.com/v2"
STAT_ID = "passing_touchdowns"
PERIOD = "game"
BET_TYPE = "ou"


def seen_at_day() -> str:
    return utcnow()[:10]


def ledger_path() -> str:
    return os.path.join(ARCHIVE_DIR, "props_passing_tds.ndjson")


def _key() -> str:
    return os.environ.get("SGO_API_KEY", "").strip()


def _get(path: str, params: Dict[str, str]) -> dict:
    # requests, not urllib, for the same reason delivery.py uses it: this Mac's
    # Python.framework has no CA bundle configured, so urllib fails every HTTPS
    # call with CERTIFICATE_VERIFY_FAILED. requests carries certifi's.
    r = requests.get(f"{BASE}{path}", params=params,
                     headers={"x-api-key": _key()}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]}")
    return r.json()


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
                    # Numeric, not the string the API returns. "1.5" and 1.5
                    # group differently, and every later comparison — bucketing
                    # by line, settling over/under — is arithmetic.
                    "line": _num(b.get("overUnder") or odd.get("overUnder")),
                    "price": b.get("odds") or b.get("price"),
                    "odd_id": odd_id,
                })
    return rows



# ── Closing lines ─────────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _american_to_prob(odds) -> Optional[float]:
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return None
    return (-o) / ((-o) + 100) if o < 0 else 100 / (o + 100)


def close(league: str = "NFL") -> int:
    """
    Freeze the consensus price for lines about to start, and record CLV.

    Same reasoning as the moneyline ledger. Win/loss on a handful of props is
    noise; whether the price taken beat where the market closed is readable far
    sooner, and for line shopping it IS the claim. Cameron Ward over 1.5 ran
    +170 at Bovada and +200 at ESPN BET on the same market at the same moment —
    which of those was right is answerable, but only before kickoff. Afterwards
    the market is gone and that line can never be asked again.

    Costs one scan (about 14 entities against a 2,500 monthly allowance), and
    only in an hour when something is actually about to start.
    """
    rows = read_ndjson(ledger_path())
    now = datetime.now(timezone.utc)

    due = []
    for r in rows:
        if r.get("close_price") is not None or r.get("actual_tds") is not None:
            continue
        try:
            k = datetime.fromisoformat(str(r["starts_at"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if timedelta(0) < (k - now) <= timedelta(hours=1):
            due.append(r)
    if not due:
        print("nothing closing in the next hour")
        return 0

    try:
        live = scan(league)
    except Exception as e:
        print(f"  live prices unavailable: {e}")
        return 0

    # Consensus per player/side/LINE. The line matters: two books quoting
    # over 1.5 and over 2.5 are not pricing the same bet, and averaging them
    # would invent a number neither offered.
    buckets: Dict[tuple, List[float]] = {}
    for r in live:
        pr = _american_to_prob(r.get("price"))
        if pr is None:
            continue
        buckets.setdefault((r["player_id"], r["side"], str(r.get("line"))), []).append(pr)

    closed = 0
    for r in due:
        key = (r["player_id"], r["side"], str(r.get("line")))
        probs = sorted(buckets.get(key, []))
        if not probs:
            continue
        mid = probs[len(probs) // 2]
        taken = _american_to_prob(r.get("price"))
        if taken is None:
            continue
        r["close_prob"] = round(mid, 5)
        r["close_books"] = len(probs)
        r["closed_at"] = utcnow()
        # Probability points by which the taken price was cheaper than the
        # close. Positive means the market moved toward you.
        r["clv_pp"] = round((mid - taken) * 100, 3)
        closed += 1

    if closed:
        with open(ledger_path(), "w") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"closed {closed} of {len(due)} due")
    return closed


# ── Grading ───────────────────────────────────────────────────────────────
# Free, and the half that makes collection worth anything. ESPN's box scores
# already come down for the board, so settling "did he throw over 1.5" costs
# nothing and needs no subscription.

def _norm_name(s: str) -> str:
    """Fold a name for comparison: case, punctuation and suffixes all go."""
    s = re.sub(r"[^a-z ]", " ", str(s).lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return " ".join(s.split())


def _player_from_odd_id(entity: str) -> str:
    """
    SGO names a player CAMERON_WARD_1_NFL. The trailing number disambiguates
    two players sharing a name, and the league is already known — so the name
    is everything before them.
    """
    parts = str(entity).split("_")
    while parts and (parts[-1].isdigit() or parts[-1] in ("NFL", "NCAAF")):
        parts.pop()
    return _norm_name(" ".join(parts))


def grade(league: str = "NFL") -> int:
    """Settle logged prop lines against finished box scores. Costs nothing."""
    path = ledger_path()
    rows = read_ndjson(path)
    open_rows = [r for r in rows if r.get("actual_tds") is None]
    if not open_rows:
        print("nothing ungraded")
        return 0

    logs = load_player_logs(league, current_season_year())
    # One statline per player per game, keyed by name and calendar date. Date
    # rather than event_id because the two feeds do not share game ids, and a
    # quarterback plays at most once a day.
    by_key: Dict[str, float] = {}
    for l in logs:
        if l.get("category") != "passing":
            continue
        tds = (l.get("stats") or {}).get("passingTouchdowns")
        if tds is None:
            continue
        day = str(l.get("date", ""))[:10]
        by_key[f"{_norm_name(l.get('player'))}|{day}"] = float(tds)

    graded = 0
    for r in open_rows:
        day = str(r.get("starts_at") or "")[:10]
        key = f"{_player_from_odd_id(r.get('player_id'))}|{day}"
        actual = by_key.get(key)
        if actual is None:
            continue
        r["actual_tds"] = actual
        line = float(r.get("line") or 0)
        # A half-point line cannot push; a whole number can, and a push is not a
        # loss. Recording it as one would understate every under.
        if actual == line:
            r["result"] = "push"
        elif r.get("side") == "over":
            r["result"] = "won" if actual > line else "lost"
        else:
            r["result"] = "won" if actual < line else "lost"
        r["graded_at"] = utcnow()
        graded += 1

    if graded:
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"graded {graded} of {len(open_rows)} ungraded")
    return graded


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
    # Closing line value, reported before results and separately from them.
    # For line shopping CLV *is* the claim — "this book is longer than the
    # market" is answered by where the market closed, not by whether the pass
    # was thrown. It also reads far sooner: a handful of settled props is noise,
    # while a handful of closes is already a signal.
    closed = [r for r in rows if r.get("clv_pp") is not None]
    if closed:
        beat = [r for r in closed if r["clv_pp"] > 0]
        avg = sum(r["clv_pp"] for r in closed) / len(closed)
        print(f"\n  Closing line value (the price, not the result)")
        print(f"    closed          {len(closed)}")
        print(f"    beat the close  {len(beat)} of {len(closed)} ({len(beat)/len(closed)*100:.0f}%)")
        print(f"    average CLV     {avg:+.2f} probability points")
        best = max(closed, key=lambda r: r["clv_pp"])
        print(f"    best            {best['player_id']} {best['side']} {best['line']} "
              f"@ {best['price']} ({best['book']}) {best['clv_pp']:+.2f} pp")

    graded = [r for r in rows if r.get("result")]
    if graded:
        won = [r for r in graded if r["result"] == "won"]
        push = [r for r in graded if r["result"] == "push"]
        staked = len(graded) - len(push)
        print(f"\n  Settled")
        print(f"    graded          {len(graded)}"
              + (f", {len(push)} push" if push else ""))
        if staked:
            print(f"    hit rate        {len(won)}/{staked} ({len(won)/staked*100:.0f}%)")
            print(f"    (a hit rate means nothing without the prices it was taken at —"
                  f" CLV above is the number to watch first)")

    if len(days) < 14:
        print(f"\n  Too little history to test anything yet — {len(days)} day(s) in. "
              f"Keep it running; a model needs weeks, not runs.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true", help="fetch and log (spends objects)")
    ap.add_argument("--report", action="store_true", help="what has been collected")
    ap.add_argument("--grade", action="store_true", help="settle finished games (free)")
    ap.add_argument("--close", action="store_true",
                    help="freeze the closing consensus for lines starting within the hour")
    ap.add_argument("--league", default="NFL")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, store nothing")
    args = ap.parse_args()

    # Every action is opt-in; bare `props.py` reports. The earlier shape let
    # --grade fall through into a scan, which silently spent objects and logged
    # a duplicate batch — an argument parser that does something you did not ask
    # for is worse than one that refuses.
    acted = False
    if args.close:
        if not _key():
            print("SGO_API_KEY is not set."); return 1
        close(args.league)
        acted = True
    if args.grade:
        grade(args.league)
        acted = True
    if args.report:
        report()
        acted = True
    if not (args.scan or args.dry_run):
        if not acted:
            report()
        return 0

    if not _key():
        print("SGO_API_KEY is not set. Get a free key at sportsgameodds.com "
              "and add SGO_API_KEY to ~/.football-picks.env (or the repo secrets).")
        return 1

    try:
        rows = scan(args.league)
    except Exception as e:
        print(f"  {args.league}: {e}")
        return 1

    print(f"{len(rows)} passing-TD prices across "
          f"{len({r['player_id'] for r in rows})} quarterback(s)")
    for r in rows[:8]:
        print(f"  {r['matchup']:12s} {r['player_id']:22s} {r['side']:5s} "
              f"{r['line']} @ {r['price']}  ({r['book']})")
    if args.dry_run:
        print("\n(dry run: nothing stored)")
        return 0

    # One observation per line per book per day. The collector is meant to run
    # once a day near opening; running it twice by hand should not double the
    # dataset, and an accidental second scan already did exactly that. Keeping
    # the first is deliberate — it is the opening price, which is the one the
    # daily schedule exists to capture.
    existing = read_ndjson(ledger_path())
    have = {(r.get("odd_id"), r.get("book"), str(r.get("seen_at", ""))[:10]) for r in existing}
    today = str(seen_at_day())
    fresh = [r for r in rows if (r["odd_id"], r["book"], today) not in have]
    skipped = len(rows) - len(fresh)

    if fresh:
        append_ndjson(ledger_path(), fresh)
    print(f"\nlogged {len(fresh)} to {ledger_path()}"
          + (f" ({skipped} already recorded today)" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
