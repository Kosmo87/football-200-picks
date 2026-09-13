"""
Books occasionally hang a broken number. Catch them, and learn if they're usable.

WHAT HAPPENED. On 2026-09-13 BetMGM had Kansas @ Arizona State inverted:
ASU +6 and +170 while four books had ASU -5.5 and -200/-225. The total was
identical to DraftKings and BetRivers at 55.5, which is the tell -- news that
moves a side eleven points moves the total too, so an untouched total means the
sides were transposed rather than repriced. It was live on MGM's site; the
price was placeable. It corrected roughly an hour later.

WHY THIS IS NOT OBVIOUSLY A FREE LUNCH. Three things, all real:

  The feed lags. After MGM's site had corrected, the-odds-api was still
  serving ASU +6. Detecting a fault in the feed does not mean it is still
  standing at the book, and that gap is exactly the window you need.

  Palpable-error clauses. Every book reserves the right to void an obvious
  mistake. An eleven-point inversion is the textbook case, so the realistic
  outcome of winning one of these is your stake back.

  Account flags. Taking obvious errors is what gets accounts limited, which
  costs more than the bet is worth to anyone holding a book for comps.

So this module DETECTS and LOGS. It does not recommend. Every hit is written
with a timestamp so that after a season we can answer the only question that
matters -- how often these appear, how long they stand, and whether any were
still live by the time we saw them. Until that is measured, a detector that
told you to bet would be guessing.

  python faults.py --scan
  python faults.py --history
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Optional

import keys  # noqa: F401
import line_shop as LS

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "data", "archive", "faults.ndjson")

# A book disagreeing with its peers by more than this is broken, not brave.
# Four points is larger than any legitimate disagreement seen on a spread;
# real differences between books run half a point to a point and a half.
# 4.0 only catches breakage. Genuine stale lines -- a book that agrees who is
# favoured and has not caught up on by how much -- run 1.5 to 3 points off, do
# not trip anyone's alarm, and are not voidable. That is the bet worth having,
# so the floor comes down to catch them and TRANSPOSED is separated out.
SPREAD_FAULT_PTS = 2.0
# On a moneyline, in de-vigged probability points.
ML_FAULT_PP = 0.06
MIN_PEERS = 3          # need a real consensus before calling anyone wrong


def _point(side_label: str) -> Optional[float]:
    """'Arizona State Sun Devils -5.5' -> -5.5"""
    try:
        return float(side_label.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return None


def spread_faults(prices: Dict[str, Dict[str, int]]) -> List[dict]:
    """Books whose own number is far from what everyone else is posting."""
    by_team: Dict[str, Dict[str, float]] = defaultdict(dict)
    for book, sides in prices.items():
        for label in sides:
            team = label.rsplit(" ", 1)[0]
            pt = _point(label)
            if pt is not None:
                by_team[team][book] = pt
    out = []
    for team, books in by_team.items():
        if len(books) < MIN_PEERS + 1:
            continue
        for book, pt in books.items():
            peers = [v for b, v in books.items() if b != book]
            if len(peers) < MIN_PEERS:
                continue
            con = median(peers)
            if abs(pt - con) >= SPREAD_FAULT_PTS:
                out.append({"market": "spreads", "book": book, "side": team,
                            "book_point": pt, "consensus_point": con,
                            "delta": pt - con, "peers": len(peers)})
    return out


def ml_faults(prices: Dict[str, Dict[str, int]]) -> List[dict]:
    out = []
    fair_all = LS.fair_probs(prices)
    for book, sides in prices.items():
        if len(prices) < MIN_PEERS + 1:
            continue
        fair = LS.fair_probs(prices, exclude=book)
        for side, price in sides.items():
            p = fair.get(side)
            if p is None:
                continue
            imp = 1 / (1 + (price / 100 if price > 0 else 100 / abs(price)))
            if abs(p - imp) >= ML_FAULT_PP:
                out.append({"market": "h2h", "book": book, "side": side,
                            "book_price": price, "book_implied": round(imp, 4),
                            "consensus": round(p, 4),
                            "delta": round(p - imp, 4),
                            "peers": len(prices) - 1})
    return out


def classify(f: dict) -> str:
    """
    TRANSPOSED  the book has the wrong team favoured -- an error, and errors
                get voided under a palpable-error clause.
    STALE       the book agrees on who is favoured and has not caught up on
                by how much. Legitimate, no void risk, and the actual prize.

    The test is the SIGN OF THE NUMBER, not the sign of the delta. An earlier
    version compared deltas and called everything transposed, because the two
    sides of any spread always mirror each other: Detroit -16.5 and New
    Orleans +16.5 is one coherent line, not an inversion.
    """
    if f["market"] == "spreads":
        a, b = f["book_point"], f["consensus_point"]
        if a * b < 0:
            return "TRANSPOSED"
        return "STALE"
    imp, con = f["book_implied"], f["consensus"]
    if (imp - 0.5) * (con - 0.5) < 0:
        return "TRANSPOSED"
    return "STALE"


def scan(leagues=("NFL", "NCAAF"), min_lead_min: int = 10,
         markets=("spreads", "h2h")) -> List[dict]:
    """
    Faults on games that have NOT started.

    The first run flagged Bovada and MyBookie five to seven points off on
    New Orleans @ Detroit -- a game already in progress. Those were live
    in-game lines being compared against a pregame consensus, which is not a
    comparison at all. A fault on a running game is also unbettable, so it is
    noise twice over.
    """
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    hits: List[dict] = []
    for league in leagues:
        for market, finder in (("spreads", spread_faults), ("h2h", ml_faults)):
            if market not in markets:
                continue
            payload = LS.fetch_live(league, market, "us")
            for game, kick, prices in LS.games_from_live(payload, market, MIN_PEERS + 1):
                try:
                    k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if (k - now).total_seconds() < min_lead_min * 60:
                    continue
                for f in finder(prices):
                    hits.append(dict(f, game=game, kickoff=kick, league=league,
                                     captured_at=stamp, kind=classify(f)))
    if hits:
        os.makedirs(os.path.dirname(ARCHIVE), exist_ok=True)
        with open(ARCHIVE, "a") as fh:
            for h in hits:
                fh.write(json.dumps(h) + "\n")
    return hits


def report(hits: List[dict]) -> None:
    if not hits:
        print("No book is more than "
              f"{SPREAD_FAULT_PTS:g} pts / {ML_FAULT_PP*100:.0f}pp from consensus.")
        return
    stale = [h for h in hits if h.get("kind") == "STALE"]
    print(f"\n{len(hits)} fault(s): {len(stale)} STALE, "
          f"{len(hits)-len(stale)} TRANSPOSED — LOGGED, NOT RECOMMENDED\n")
    for h in hits:
        if h["market"] == "spreads":
            detail = (f"{h['book_point']:+g} vs consensus "
                      f"{h['consensus_point']:+g}  ({h['delta']:+.1f} pts)")
        else:
            detail = (f"{h['book_price']:+d} implies {h['book_implied']*100:.1f}% "
                      f"vs {h['consensus']*100:.1f}%  ({h['delta']*100:+.1f}pp)")
        print(f"  [{h.get('kind','?'):<10}] {h['book']:<12}"
              f"{h['side'][:28]:<29}{detail}")
        print(f"  {'':<12}{h['game'][:60]}")
    print("\nThe feed lags the book. Verify on the site before believing any of "
          "this,\nand remember a palpable-error clause means winning one of "
          "these may pay nothing.")


def history() -> int:
    if not os.path.exists(ARCHIVE):
        print("No faults logged yet.")
        return 0
    rows = [json.loads(l) for l in open(ARCHIVE) if l.strip()]
    stamps = sorted({r["captured_at"] for r in rows})
    print(f"{len(rows)} fault row(s) across {len(stamps)} scan(s)")
    by_book: Dict[str, int] = defaultdict(int)
    for r in rows:
        by_book[r["book"]] += 1
    for b, n in sorted(by_book.items(), key=lambda kv: -kv[1]):
        print(f"  {b:<14}{n:>4}")
    # how long does one stand?
    seen: Dict[tuple, List[str]] = defaultdict(list)
    for r in rows:
        seen[(r["game"], r["book"], r["side"], r["market"])].append(r["captured_at"])
    if seen:
        spans = []
        for k, ts in seen.items():
            if len(ts) > 1:
                a = datetime.fromisoformat(min(ts))
                b = datetime.fromisoformat(max(ts))
                spans.append((b - a).total_seconds() / 3600)
        if spans:
            print(f"\nfaults seen in >1 scan: {len(spans)}, "
                  f"median span {median(spans):.1f}h")
        else:
            print("\nevery fault appeared in exactly one scan — they do not last")
    return 0


# ------------------------------------------------------------------- alerting

MY_BOOKS = ("betmgm", "fanduel")


def alert_text(hits: List[dict], mine_only: bool = True) -> Optional[str]:
    """
    A message short enough to read on a lock screen, leading with WHERE.

    The first thing needed is which app to open, because the window is about
    an hour and a line that has to be hunted for is a line that has moved.
    TRANSPOSED hits are excluded from alerting entirely: they are voidable,
    they vanish fastest, and waking someone for one is a false alarm with
    extra steps.
    """
    live = [h for h in hits if h.get("kind") == "STALE"]
    if mine_only:
        live = [h for h in live
                if any(b in h["book"].lower() for b in MY_BOOKS)]
    if not live:
        return None
    live.sort(key=lambda h: -abs(h.get("delta", 0)))
    top = live[0]
    if top["market"] == "spreads":
        what = (f"{top['side']} {top['book_point']:+g} "
                f"(market {top['consensus_point']:+g})")
    else:
        what = (f"{top['side']} {top['book_price']:+d} "
                f"({top['consensus']*100:.0f}% true)")
    when = top["kickoff"][5:16].replace("T", " ")
    more = f" +{len(live)-1} more" if len(live) > 1 else ""
    return (f"OPEN {top['book'].upper()}: {what}\n"
            f"{top['game'][:44]} {when}Z{more}")


def send_alert(hits: List[dict]) -> bool:
    """SMS when Twilio is configured, email otherwise. Never both."""
    body = alert_text(hits)
    if not body:
        return False
    print("\n--- ALERT ---\n" + body)
    import delivery
    sms_to = os.environ.get("SMS_TO", "").strip()
    if sms_to and os.environ.get("TWILIO_ACCOUNT_SID", "").strip():
        return delivery.send_sms(sms_to, body)
    to = os.environ.get("DELIVER_TO", "").strip()
    if not to:
        print("  no SMS_TO or DELIVER_TO — alert not sent")
        return False
    first = body.splitlines()[0]
    html = ("<div style='font-family:-apple-system,sans-serif'>"
            f"<h2 style='margin:0 0 8px'>{first}</h2>"
            f"<pre style='font-size:14px'>{body}</pre>"
            "<p style='color:#6b7280;font-size:12px'>Stale line — the book has "
            "not caught up to the market. Verify on the site; the feed lags, "
            "and these last about an hour.</p></div>")
    return delivery.send_email(to, first[:60], html)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--alert", action="store_true",
                    help="text/email when a STALE line appears at your books")
    ap.add_argument("--markets", default="spreads,h2h",
                    help="comma-separated; fewer markets costs fewer credits")
    a = ap.parse_args()
    if a.history:
        return history()
    if a.scan:
        mk = tuple(m.strip() for m in a.markets.split(",") if m.strip())
        hits = scan(markets=mk)
        report(hits)
        if a.alert:
            send_alert(hits)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
