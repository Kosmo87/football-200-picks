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

import keys  # noqa: F401  (loads ~/.football-picks.env)
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from archive import ARCHIVE_DIR, append_ndjson, read_ndjson, utcnow
from espn import current_season_year, fetch_completed_games
from line_shop import SPORTS, fetch_live, find_value, games_from_live
from odds import american_to_decimal, american_to_implied_prob

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



# ── What you actually bet ─────────────────────────────────────────────────
# The scan records what it FOUND. That is the right thing to measure for
# "does line shopping work", and the wrong thing to measure for "how am I
# doing" — the two diverge the first time a gap is skipped, and after a month
# the ledger is describing a strategy nobody followed.
#
# So placement is recorded separately and deliberately. A row with no stake is
# a flagged opportunity; a row with one is a bet. The report keeps them apart
# rather than averaging a thing you did with a thing you did not.

def _label(r: dict) -> str:
    return f"{r['side']} {r['price']:+d} {r['book']}"


def show(only_open: bool = True) -> List[dict]:
    """Numbered listing, so placing a bet does not mean typing a uuid."""
    rows = read_ndjson(ledger_path())
    shown = [r for r in rows if r["status"] == "open"] if only_open else rows
    shown.sort(key=lambda r: r.get("kickoff") or "")
    if not shown:
        print("nothing open")
        return shown
    print(f"\n  {'#':>3}  {'side':28s} {'price':>6s} {'book':13s} {'edge':>6s} "
          f"{'stake':>6s}  kickoff")
    for i, r in enumerate(shown, 1):
        st = f"{r['stake']:.2f}u" if r.get("stake") else "—"
        print(f"  {i:>3}  {r['side'][:28]:28s} {r['price']:+6d} {r['book'][:13]:13s} "
              f"{r['edge']*100:5.1f}% {st:>6s}  {str(r.get('kickoff'))[:16]}")
    print(f"\n  place one with:  python shop_ledger.py --place N --stake 1")
    return shown


def place(index: int, stake: float) -> int:
    """Record that a flagged gap was actually bet, at a real stake."""
    rows = read_ndjson(ledger_path())
    shown = [r for r in rows if r["status"] == "open"]
    shown.sort(key=lambda r: r.get("kickoff") or "")
    if not 1 <= index <= len(shown):
        print(f"no open bet #{index} — there are {len(shown)}")
        return 1
    target = shown[index - 1]
    if stake <= 0:
        print("stake must be positive")
        return 1

    for r in rows:
        if r is target or (r["event_id"] == target["event_id"]
                           and r["side"] == target["side"]
                           and r["book"] == target["book"]):
            r["stake"] = float(stake)
            r["placed_at"] = utcnow()
            print(f"  placed {stake:.2f}u on {_label(r)} — {r['game']}")
            break

    with open(ledger_path(), "w") as f:
        import json
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return 0


def close(leagues=DEFAULT_LEAGUES) -> int:
    """
    Freeze the consensus price for bets about to start. Costs one scan.

    WHY THIS MATTERS MORE THAN WIN/LOSS. These are moneyline underdogs around
    +400: four losses in five is what being RIGHT looks like. Seventeen of them
    settled is indistinguishable from noise, and hundreds would take a season.

    But line shopping does not claim to predict games. It claims this book is
    priced longer than the market — and the direct test of that is whether the
    price taken beat where the market closed. Take +450, watch it close +385,
    and the edge was real whether or not the team won. That reads in weeks.

    So the closing consensus is captured while it still exists. After kickoff
    the market is gone and the question can never be answered for that bet.
    """
    rows = read_ndjson(ledger_path())
    now = datetime.now(timezone.utc)

    # Bets starting within the hour that have not been closed yet. Closer than
    # that risks missing them entirely between runs; further out is not "close".
    due = []
    for r in rows:
        if r.get("close_price") is not None or r["status"] != "open":
            continue
        try:
            k = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if timedelta(0) < (k - now) <= timedelta(hours=1):
            due.append(r)
    if not due:
        print("[shop] nothing closing in the next hour")
        return 0

    live_by_league: Dict[str, list] = {}
    for league in {r["league"] for r in due}:
        try:
            live_by_league[league] = games_from_live(fetch_live(SPORTS[league]))
        except Exception as e:
            print(f"[shop] {league} live prices unavailable: {e}")

    closed = 0
    for r in due:
        for g in live_by_league.get(r["league"], []):
            if g.get("event_id") != r.get("event_id"):
                continue
            # The consensus across books at kickoff, not the price at one book:
            # the claim was "longer than the market", so the market is the
            # comparison.
            prices = [b["price"] for b in g.get("sides", {}).get(r["side"], [])]
            if not prices:
                continue
            mid = sorted(prices)[len(prices) // 2]
            r["close_price"] = mid
            r["close_books"] = len(prices)
            r["closed_at"] = utcnow()
            # CLV in probability points: how much cheaper the taken price was
            # than the close. Positive means the market moved toward you.
            r["clv_pp"] = round(
                (american_to_implied_prob(mid) - american_to_implied_prob(r["price"])) * 100, 3)
            closed += 1
            break

    if closed:
        with open(ledger_path(), "w") as f:
            import json
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"[shop] closed {closed}")
    return closed


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

    placed = [r for r in rows if r.get("stake")]
    print(f"\n  Line-shop ledger")
    print(f"  {'logged':<22}{len(rows)}")
    print(f"  {'still open':<22}{len([r for r in rows if r['status'] == 'open'])}")
    print(f"  {'of those, bet':<22}{len(placed)}"
          + ("  (nothing placed yet — the figures below describe what the scan"
             " found, not what you did)" if not placed else ""))
    # CLV is reported before win/loss, and separately from it, because it
    # answers the question the ledger is actually asking. A price that beat the
    # close was a good bet whether or not it won; seventeen results are noise at
    # +400, but seventeen closing lines are evidence.
    closed = [r for r in rows if r.get("clv_pp") is not None]
    if closed:
        beat = [r for r in closed if r["clv_pp"] > 0]
        avg = sum(r["clv_pp"] for r in closed) / len(closed)
        print(f"\n  Closing line value   (the price, not the result)")
        print(f"  {'  closed':<22}{len(closed)}")
        print(f"  {'  beat the close':<22}{len(beat)} of {len(closed)}"
              f"  ({len(beat)/len(closed)*100:.0f}%)")
        print(f"  {'  average CLV':<22}{avg:+.2f} probability points")
        if len(closed) < 30:
            print(f"  {'':<22}(thin — CLV reads long before results do, but not this early)")

    if not staked:
        print(f"\n  Nothing settled yet — first results land after kickoff.")
        return
    print(f"  {'record':<22}{len(won)}-{len(lost)}  ({len(won)/staked*100:.1f}%)")
    print(f"  {'units':<22}{units:+.2f} on {staked} staked")
    print(f"  {'return':<22}{units/staked*100:+.2f}%")
    print(f"  {'edge it claimed':<22}"
          f"{sum(r['edge'] for r in settled)/len(settled)*100:+.2f}%")

    # Your actual book, if any of it was actually bet.
    settled_placed = [r for r in settled if r.get("stake")]
    if settled_placed:
        pu = sum((r["units"] or 0) * r["stake"] for r in settled_placed)
        st = sum(r["stake"] for r in settled_placed
                 if r["status"] in ("won", "lost"))
        print(f"\n  What you actually bet")
        print(f"  {'  settled bets':<22}{len(settled_placed)}")
        print(f"  {'  units':<22}{pu:+.2f} on {st:.2f}u staked")
        if st:
            print(f"  {'  return':<22}{pu/st*100:+.2f}%")

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
    ap.add_argument("--close", action="store_true",
                    help="freeze the closing consensus for bets starting within the hour")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--force", action="store_true", help="scan regardless of the timer")
    ap.add_argument("--min-edge", type=float, default=0.03)
    ap.add_argument("--list", action="store_true", help="numbered listing of open gaps")
    ap.add_argument("--place", type=int, metavar="N", help="record bet N as actually placed")
    ap.add_argument("--stake", type=float, default=1.0, help="units staked, with --place")
    args = ap.parse_args()

    if args.scan:
        scan(min_edge=args.min_edge, force=args.force)
    if args.list:
        show()
        return 0
    if args.place is not None:
        return place(args.place, args.stake)
    if args.close:
        close()
    if args.grade:
        grade()
    if args.report or not (args.scan or args.grade or args.close):
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
