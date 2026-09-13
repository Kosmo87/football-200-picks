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
    # point AND price per (team, book): the number tells you the edge, the
    # price tells you the stake, and defaulting the price to -110 quietly
    # mis-sizes every bet a book juices differently.
    by_team: Dict[str, Dict[str, float]] = defaultdict(dict)
    price_of: Dict[tuple, int] = {}
    for book, sides in prices.items():
        for label, price in sides.items():
            team = label.rsplit(" ", 1)[0]
            pt = _point(label)
            if pt is not None:
                by_team[team][book] = pt
                price_of[(team, book)] = int(price)
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
                            "price": price_of.get((team, book), -110),
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


def _cover_prob(book_point: float, true_point: float, sd: float = 13.0) -> float:
    """
    Chance the side you are backing covers the number the BOOK is offering,
    given the market thinks the true line is `true_point`.

    Both are quoted from that side's perspective, negative meaning laying
    points. sd 13.0 is the NFL margin spread; college runs nearer 16.5, which
    makes this the conservative of the two.
    """
    from statistics import NormalDist
    return 1 - NormalDist(-true_point, sd).cdf(-book_point)


def bet_from(f: dict) -> Optional[dict]:
    """
    Turn a fault into the one thing to place, or nothing.

    The side to back is the one where the book's number is BETTER FOR YOU than
    the market's -- more points taken, or fewer laid. That is delta > 0, and it
    is exactly one of the two sides. Reporting both, as the raw scan does, is
    a fault report; a bet needs the half of it you can act on.

    DraftKings at Oklahoma -23.5 against a -21.5 market reads as two rows.
    Only one is a bet: New Mexico +23.5, because +23.5 is two points better
    than the +21.5 available everywhere else. Backing Oklahoma at -23.5 is
    laying two points MORE than you should.
    """
    if f["market"] != "spreads" or f.get("delta", 0) <= 0:
        return None
    bp, cp = f["book_point"], f["consensus_point"]
    p = _cover_prob(bp, cp)
    price = f.get("price", -110)
    imp = 1 / (1 + (price / 100 if price > 0 else 100 / abs(price)))
    edge = p - imp
    if edge <= 0:
        return None
    b = (price / 100 if price > 0 else 100 / abs(price))
    kelly = (b * p - (1 - p)) / b
    units = max(0.5, round(kelly / 4 / 0.01 * 2) / 2)   # quarter-Kelly, 1U = 1%
    return {"side": f["side"], "point": bp, "price": price, "book": f["book"],
            "market_point": cp, "gain": bp - cp, "chance": p, "edge": edge,
            "units": min(units, 3.0), "game": f["game"], "kickoff": f["kickoff"]}


def alert_text(hits: List[dict], mine_only: bool = True,
               unit_dollars: float = 100.0) -> Optional[str]:
    """
    A complete instruction, short enough for a lock screen.

    WHERE first, then WHAT, then HOW MUCH -- in that order because the window
    is about an hour and anything requiring a decision has already cost you
    the line. Nothing here needs working out.

    TRANSPOSED hits never alert: voidable under a palpable-error clause, they
    vanish fastest, and waking someone for one is a false alarm with extra
    steps.
    """
    bets = []
    for h in hits:
        if h.get("kind") != "STALE":
            continue
        if mine_only and not any(b in h["book"].lower() for b in MY_BOOKS):
            continue
        bet = bet_from(h)
        if bet:
            bets.append(bet)
    if not bets:
        return None
    bets.sort(key=lambda b: -b["edge"])
    t = bets[0]
    risk = t["units"] * unit_dollars
    profit = risk * (t["price"] / 100 if t["price"] > 0 else 100 / abs(t["price"]))
    when = t["kickoff"][5:16].replace("T", " ")
    price_s = f"+{t['price']}" if t["price"] > 0 else str(t["price"])
    more = f"\n(+{len(bets)-1} more)" if len(bets) > 1 else ""
    return (f"BET NOW at {t['book'].upper()}\n"
            f"{t['side']} {t['point']:+g} {price_s}\n"
            f"market {t['market_point']:+g} — {abs(t['gain']):.1f} pts better\n"
            f"risk ${risk:.0f} to win ${profit:.0f}  ({t['units']:g}U)\n"
            f"{t['chance']*100:.0f}% to cover\n"
            f"{t['game'][:40]} {when}Z{more}")


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
