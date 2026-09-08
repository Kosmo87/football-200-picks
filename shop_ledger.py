"""
Log every flagged price gap, then grade it. A measured record, not a backtest.

The detector already finds books priced better than the market. What it could
not do was tell you whether those gaps actually pay, because a snapshot is
forgotten the moment it scrolls past. This writes each one down at the price it
was offered and settles it once the game finishes, so in a few weeks the answer
is a record rather than an argument.

Deliberately cheap. The free tier is 500 credits a month and one scan of one
market costs one credit, so scanning is rate-limited by elapsed time rather than
run on every hourly build: three scans a day across two leagues is about 180
credits a month, comfortably inside the allowance with room to spare.

  python shop_ledger.py --scan          # find and log (costs credits)
  python shop_ledger.py --grade         # settle finished games (free)
  python shop_ledger.py --report        # the running record (free)
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from archive import ARCHIVE_DIR, append_ndjson, read_ndjson, utcnow
from espn import current_season_year, fetch_completed_games
from line_shop import SPORTS, fetch_live, find_value, games_from_live
from odds import american_to_decimal

HOURS_BETWEEN_SCANS = 8.0
DEFAULT_LEAGUES = ("NCAAF", "NFL")
# Moneylines only: live checking found every gap there and none in spreads or
# totals, and each extra market costs a credit per scan.
MARKET = "h2h"


def ledger_path(season: Optional[int] = None) -> str:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    return os.path.join(ARCHIVE_DIR, f"shop_{season or current_season_year()}.ndjson")


def _norm(name: str) -> str:
    """Loose team-name key: 'Iowa State Cyclones' -> 'iowastatecyclones'."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def hours_since_last_scan(rows: List[dict]) -> float:
    stamps = [r.get("found_at") for r in rows if r.get("found_at")]
    if not stamps:
        return 1e9
    last = max(stamps)
    try:
        dt = datetime.fromisoformat(last)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def scan(leagues=DEFAULT_LEAGUES, min_edge: float = 0.03, force: bool = False) -> int:
    path = ledger_path()
    rows = read_ndjson(path)
    if not force:
        elapsed = hours_since_last_scan(rows)
        if elapsed < HOURS_BETWEEN_SCANS:
            print(f"[shop] last scan {elapsed:.1f}h ago, "
                  f"waiting until {HOURS_BETWEEN_SCANS}h — no credits spent")
            return 0

    # One row per game+book+side. A gap that persists across scans is the same
    # opportunity seen twice, not a second bet.
    seen = {(r["event_id"], r["book"], r["side"]) for r in rows}
    new: List[dict] = []
    now = utcnow()

    for league in leagues:
        try:
            payload = fetch_live(league, MARKET, "us")
        except SystemExit:
            raise
        except Exception as e:
            print(f"[shop] {league} fetch failed: {e}")
            continue

        for game, kickoff, prices in games_from_live(payload, MARKET):
            for o in find_value(prices, min_edge):
                # The event id is not in the flattened view, so rebuild a stable
                # key from the matchup and kickoff.
                event_id = f"{_norm(game)}|{kickoff}"
                key = (event_id, o["book"], o["side"])
                if key in seen:
                    continue
                seen.add(key)
                new.append({
                    "event_id": event_id, "league": league, "market": MARKET,
                    "game": game, "kickoff": kickoff, "found_at": now,
                    "book": o["book"], "side": o["side"], "price": o["price"],
                    "consensus_price": o["consensus_price"],
                    "fair_prob": round(o["fair_prob"], 5),
                    "edge": round(o["edge"], 5), "n_books": o["n_books"],
                    "status": "open", "result": None, "units": None,
                })

    append_ndjson(path, new)
    print(f"[shop] logged {len(new)} new opportunities "
          f"({len(rows) + len(new)} total in the ledger)")
    return len(new)


def grade(leagues=DEFAULT_LEAGUES) -> int:
    """Settle logged bets against finished games. Costs no credits."""
    path = ledger_path()
    rows = read_ndjson(path)
    openers = [r for r in rows if r["status"] == "open"]
    if not openers:
        print("[shop] nothing open to grade")
        return 0

    finals = []
    for league in leagues:
        try:
            finals.extend(fetch_completed_games(league))
        except Exception as e:
            print(f"[shop] {league} results unavailable: {e}")

    # Index finished games by each side's normalised name.
    by_team: Dict[str, list] = {}
    for g in finals:
        for name in (g.home_name, g.away_name):
            by_team.setdefault(_norm(name), []).append(g)

    graded = 0
    for r in openers:
        cands = by_team.get(_norm(r["side"]), [])
        match = None
        for g in cands:
            if not g.date or not r.get("kickoff"):
                continue
            try:
                gd = datetime.fromisoformat(g.date.replace("Z", "+00:00"))
                kd = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if abs((gd - kd).total_seconds()) < 6 * 3600:
                match = g
                break
        if match is None:
            continue

        picked_home = _norm(match.home_name) == _norm(r["side"])
        if match.home_score == match.away_score:
            r["status"], r["units"] = "push", 0.0
        else:
            won = (match.home_score > match.away_score) == picked_home
            r["status"] = "won" if won else "lost"
            r["units"] = (american_to_decimal(r["price"]) - 1) if won else -1.0
        r["result"] = f"{match.away_abbr} {match.away_score}-{match.home_score} {match.home_abbr}"
        r["graded_at"] = utcnow()
        graded += 1

    if graded:
        with open(path, "w") as f:
            import json
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"[shop] graded {graded}")
    return graded


def report() -> None:
    rows = read_ndjson(ledger_path())
    if not rows:
        print("Ledger is empty. Run --scan.")
        return
    settled = [r for r in rows if r["status"] in ("won", "lost", "push")]
    won = [r for r in settled if r["status"] == "won"]
    lost = [r for r in settled if r["status"] == "lost"]
    staked = len(won) + len(lost)
    units = sum(r["units"] or 0 for r in settled)

    print(f"\n  Line-shop ledger")
    print(f"  {'logged':<22}{len(rows)}")
    print(f"  {'still open':<22}{len([r for r in rows if r['status'] == 'open'])}")
    if not staked:
        print(f"\n  Nothing settled yet — first results land after kickoff.")
        return
    print(f"  {'record':<22}{len(won)}-{len(lost)}  ({len(won)/staked*100:.1f}%)")
    print(f"  {'units':<22}{units:+.2f} on {staked} staked")
    print(f"  {'return':<22}{units/staked*100:+.2f}%")
    print(f"  {'edge it claimed':<22}"
          f"{sum(r['edge'] for r in settled)/len(settled)*100:+.2f}%")

    print(f"\n  By book:")
    by_book: Dict[str, list] = {}
    for r in settled:
        by_book.setdefault(r["book"], []).append(r)
    for book, bs in sorted(by_book.items(), key=lambda kv: -len(kv[1]))[:8]:
        st = [b for b in bs if b["status"] in ("won", "lost")]
        if not st:
            continue
        u = sum(b["units"] for b in st)
        print(f"    {book:<22}{len(st):>4} bets  {u:>+7.2f}u  {u/len(st)*100:>+7.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--force", action="store_true", help="scan regardless of the timer")
    ap.add_argument("--min-edge", type=float, default=0.03)
    args = ap.parse_args()

    if args.scan:
        scan(min_edge=args.min_edge, force=args.force)
    if args.grade:
        grade()
    if args.report or not (args.scan or args.grade):
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
