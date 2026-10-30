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
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, List, Optional

import requests

import books as BOOKS
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
    """
    Both sides, whenever either one trips.

    The threshold is in absolute probability points, and on a moneyline that is
    structurally lopsided: a -3335 favourite sits 6.7pp off a 90.4% consensus
    while its own mirror is barely 3pp off, because the same vig is a much
    smaller share of a small number. So the side that trips is almost always the
    one the book has made MORE expensive -- which is the one side you would
    never back.

    Emitting only that side logged the fault and threw away the bet. Both sides
    go in, the way spreads already do, and `delta > 0` picks the half that can
    be acted on.
    """
    out = []
    for book, sides in prices.items():
        if len(prices) < MIN_PEERS + 1:
            continue
        fair = LS.fair_probs(prices, exclude=book)
        rows, tripped = [], False
        for side, price in sides.items():
            p = fair.get(side)
            if p is None:
                continue
            imp = 1 / (1 + (price / 100 if price > 0 else 100 / abs(price)))
            if abs(p - imp) >= ML_FAULT_PP:
                tripped = True
            rows.append({"market": "h2h", "book": book, "side": side,
                         "book_price": price, "book_implied": round(imp, 4),
                         "consensus": round(p, 4),
                         "delta": round(p - imp, 4),
                         "peers": len(prices) - 1})
        if tripped:
            out.extend(rows)
    return out


# Below these, a sign flip is more likely an ordinary line difference than an
# inverted one, because a small line is close to its own negative. Four points
# is wider than any spread move seen in this archive and narrower than the one
# genuine transposition in it (10.5).
TRANSPOSED_MIN_PTS = 4.0
TRANSPOSED_MIN_PP = 0.15

# Deliberately small — see bet_from. Size on an obvious error buys a void.
TRANSPOSED_MAX_UNITS = 1.0


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
        # A sign flip alone is not enough. Near a pick'em, ANY ordinary
        # difference crosses zero: Atlanta +1.5 against a -1.0 consensus is a
        # 2.5-point move, not an inverted line, and calling it an error
        # suppressed the alert on a bettable NFL number. A real transposition
        # has to be too big to be a move -- the Kansas/Arizona State one was
        # 10.5 points.
        if a * b < 0 and abs(a - b) >= TRANSPOSED_MIN_PTS:
            return "TRANSPOSED"
        return "STALE"
    imp, con = f["book_implied"], f["consensus"]
    # Same reasoning in probability: a coin-flip game straddles 50% on any
    # disagreement at all.
    if (imp - 0.5) * (con - 0.5) < 0 and abs(imp - con) >= TRANSPOSED_MIN_PP:
        return "TRANSPOSED"
    return "STALE"


BOOK_SPREADS = os.path.join(ROOT, "cache", "book_spreads.json")


def snapshot_book_spreads(payload: List[dict], league: str, stamp: str) -> int:
    """
    Persist MY_BOOKS' spreads, keyed the way a reader of board.json can use.

    Stored per league as rows of {home, away, commence_time, points: {book:
    home_point}} -- the HOME team's number, matching board.json's convention,
    so a consumer does not have to know which side the API happened to name
    first. `captured_at` travels with it because a spread has a shelf life and
    a stale number quoted as live is the kind of thing this project keeps
    finding in other people's data.
    """
    rows = []
    for ev in payload:
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        points = {}
        for bk in ev.get("bookmakers") or []:
            key = (bk.get("key") or "").lower()
            if key not in MY_BOOKS:
                continue
            for mk in bk.get("markets") or []:
                if mk.get("key") != "spreads":
                    continue
                for oc in mk.get("outcomes") or []:
                    if oc.get("name") == home and oc.get("point") is not None:
                        points[key] = float(oc["point"])
        if points:
            rows.append({"home": home, "away": away,
                         "commence_time": ev.get("commence_time", ""),
                         "points": points})

    try:
        with open(BOOK_SPREADS) as fh:
            store = json.load(fh)
    except Exception:
        store = {}
    if not isinstance(store, dict):
        store = {}
    store[league] = {"captured_at": stamp, "books": list(MY_BOOKS), "games": rows}
    os.makedirs(os.path.dirname(BOOK_SPREADS), exist_ok=True)
    with open(BOOK_SPREADS, "w") as fh:
        json.dump(store, fh, indent=1, sort_keys=True)
    print(f"[faults] snapshot {len(rows)} {league} game(s) at {', '.join(MY_BOOKS)}")
    return len(rows)


# Kalshi is compared on these too. Daily sports, so two days of schedule is
# the whole board; football's week needs seven.
KALSHI_ONLY = ("NBA", "NHL", "NCAAB")


def scan(leagues=("NFL", "NCAAF"), min_lead_min: int = 10,
         markets=("spreads", "h2h"), source: str = "odds-api",
         archive: Optional[str] = None) -> List[dict]:
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
        an_games = None
        if source == "an":
            import an_feed
            an_games = an_feed.games(league)
            hits.extend(kalshi_hits(league, an_games, now, min_lead_min, stamp))
        for market, finder in (("spreads", spread_faults), ("h2h", ml_faults)):
            if market not in markets:
                continue
            if an_games is not None:
                # Free and unmetered (an_feed.py); no book-spread snapshot,
                # that stays with the paid scan whose payload it already has.
                for game, kick, prices in an_feed.games_from_an(an_games, market, MIN_PEERS + 1):
                    k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
                    if (k - now).total_seconds() < min_lead_min * 60:
                        continue
                    for f in finder(prices):
                        hits.append(dict(f, game=game, kickoff=kick, league=league,
                                         captured_at=stamp, kind=classify(f), source="an"))
                continue
            payload = LS.fetch_live(league, market, "us")
            if market == "spreads":
                # Keep the spreads at the books with an account before the
                # payload is thrown away. This scan is the only call that
                # already pays for them, and the hourly board rebuild cannot
                # afford a credit of its own -- without this the teaser
                # shortlist is stuck reading whoever ESPN quotes, which is a
                # book the user does not hold and a different half point.
                try:
                    snapshot_book_spreads(payload, league, stamp)
                except Exception as e:
                    print(f"[faults] could not snapshot {league} spreads: {e}")
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
    if source == "an":
        import an_feed
        for league in KALSHI_ONLY:
            try:
                an_games = an_feed.games(league, 2)
            except Exception as e:
                print(f"[faults] {league} lines unavailable: {e}")
                continue
            hits.extend(kalshi_hits(league, an_games, now, min_lead_min, stamp))
    if hits:
        archive = archive or ARCHIVE
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        with open(archive, "a") as fh:
            for h in hits:
                fh.write(json.dumps(h) + "\n")
    return hits


# Kalshi, after its fee, against the books' de-vigged chance. Measured on the
# first look (2026-10-30, 61 matched games): the widest gap was +1.0pp and
# most sat at -1 to -2pp, so three points is a real dislocation, not noise.
KALSHI_MIN_EDGE = 0.03
# Outside 10-90c the books' de-vig (proportional) is the weak part of the
# comparison: it hands longshots too much of the margin, and every Kalshi
# longshot then looks a cent or two cheap. Those gaps are the method's.
KALSHI_PRICE_RANGE = (0.10, 0.90)
# Contracts sitting at the price, in dollars. Less than this and the "price"
# is one stray order that a real stake would walk straight through.
KALSHI_MIN_DEPTH = 50.0


def kalshi_hits(league: str, an_games: List[dict], now: datetime,
                min_lead_min: int, stamp: str) -> List[dict]:
    """
    Kalshi contracts cheaper than the books say they should be.

    Kept apart from the book finders because it is a different shape: Kalshi
    is the one venue being judged and is never a peer in the consensus, so a
    bad Kalshi print cannot drag the standard it is measured against. A failure
    here must not cost the book scan its run, so it is caught and reported.
    """
    import kalshi_feed
    try:
        rows = kalshi_feed.gaps(league, an_games)
    except Exception as e:
        print(f"[faults] Kalshi {league} unavailable: {e}")
        return []
    lo, hi = KALSHI_PRICE_RANGE
    out = []
    for r in rows:
        k = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
        if (k - now).total_seconds() < min_lead_min * 60:
            continue
        if r["edge"] < KALSHI_MIN_EDGE or not (lo <= r["kalshi_price"] <= hi):
            continue
        if r["size"] * r["kalshi_price"] < KALSHI_MIN_DEPTH:
            continue
        out.append(dict(r, captured_at=stamp, kind="KALSHI", source="kalshi"))
    return out


def report(hits: List[dict]) -> None:
    if not hits:
        print("No book is more than "
              f"{SPREAD_FAULT_PTS:g} pts / {ML_FAULT_PP*100:.0f}pp from consensus.")
        return
    n = defaultdict(int)
    for h in hits:
        n[h.get("kind")] += 1
    print(f"\n{len(hits)} fault(s): {n['STALE']} STALE, {n['TRANSPOSED']} TRANSPOSED"
          + (f", {n['KALSHI']} KALSHI" if n["KALSHI"] else "")
          + " — LOGGED, NOT RECOMMENDED\n")
    for h in hits:
        if h["market"] == "kalshi":
            detail = (f"{h['kalshi_price']*100:.0f}c+{h['fee']*100:.0f}c vs "
                      f"{h['consensus']*100:.1f}%  ({h['edge']*100:+.1f}pp)")
        elif h["market"] == "spreads":
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



# ------------------------------------------------------------------- grading

def _bet_key(f: dict) -> tuple:
    """What makes two rows the same bet. A fault seen in three consecutive
    scans is one opportunity, not three."""
    return (f.get("game"), f.get("book"), f.get("side"), f.get("market"),
            f.get("book_point"), f.get("book_price"))


def grade(leagues=("NFL", "NCAAF")) -> int:
    """
    Settle every logged fault against the finished game. Costs no credits.

    Two questions are kept apart on purpose, because they have different
    answers and only one of them is ours to measure:

      did the NUMBER win    — arithmetic, settled here from the final score
      did the BOOK pay      — a decision someone at the book makes, knowable
                              only if the bet was actually placed

    Conflating them is how a palpable-error clause hides: a transposed line
    that wins the number and pays nothing looks identical to one that lost,
    and the difference is the entire question about whether these are usable.
    So `status` records the number and `honoured` records the book, and
    `honoured` stays null until a human says otherwise.
    """
    from espn import fetch_completed_games
    from teamnames import same_team

    rows = [json.loads(l) for l in open(ARCHIVE)] if os.path.exists(ARCHIVE) else []
    rows = [r for r in rows if r]
    if not rows:
        print("[faults] nothing logged yet")
        return 0

    # One row per opportunity: the earliest sighting, which is the price that
    # was actually available first.
    best: Dict[tuple, dict] = {}
    for r in rows:
        k = _bet_key(r)
        if k not in best or r["captured_at"] < best[k]["captured_at"]:
            best[k] = r
    opps = [r for r in best.values() if not r.get("graded_at")]
    if not opps:
        print("[faults] nothing open to grade")
        return 0

    finals = []
    for lg in leagues:
        try:
            finals.extend(fetch_completed_games(lg))
        except Exception as e:
            print(f"[faults] {lg} results unavailable: {e}")

    graded = 0
    for r in opps:
        side = r.get("side") or ""
        match = None
        for g in finals:
            if same_team(side, g.home_name) or same_team(side, g.away_name):
                # Both teams must be in the matched event. Matching on one name
                # settled a calibration table against the wrong fixtures once.
                other = r.get("game", "").replace(side, "").replace("@", "").strip()
                if not other or same_team(other, g.home_name) or same_team(other, g.away_name):
                    match = g
                    break
        if not match:
            continue

        backing_home = same_team(side, match.home_name)
        mine = match.home_score if backing_home else match.away_score
        theirs = match.away_score if backing_home else match.home_score
        margin = mine - theirs

        if r["market"] == "spreads":
            # The book's number, from the side being backed. Covering means the
            # margin beats laying that many points.
            adj = margin + (r.get("book_point") or 0)
            status = "won" if adj > 0 else "lost" if adj < 0 else "push"
            price = r.get("price", -110)
        else:
            status = "won" if margin > 0 else "lost" if margin < 0 else "push"
            price = r.get("book_price", -110)

        b = (price / 100) if price > 0 else (100 / abs(price))
        r["final"] = f"{match.away_abbr} {match.away_score}-{match.home_score} {match.home_abbr}"
        r["margin"] = margin
        r["status"] = status
        r["units"] = 0.0 if status == "push" else (round(b, 4) if status == "won" else -1.0)
        r["graded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        r.setdefault("honoured", None)      # only a person can answer this
        graded += 1

    if graded:
        # Rewrite in place: the archive is the record, and a grade belongs on
        # the row it grades rather than in a second file that can drift from it.
        by_key = {_bet_key(r): r for r in best.values()}
        out = []
        for line in open(ARCHIVE):
            if not line.strip():
                continue
            row = json.loads(line)
            g = by_key.get(_bet_key(row))
            if g and g.get("graded_at") and not row.get("graded_at"):
                row.update({k: g[k] for k in
                            ("final", "margin", "status", "units", "graded_at", "honoured")})
            out.append(json.dumps(row))
        with open(ARCHIVE, "w") as fh:
            fh.write("\n".join(out) + "\n")
    print(f"[faults] graded {graded}; {len(opps) - graded} still unplayed or unmatched")
    return 0


def ledger() -> int:
    """What the faults have actually done, with void kept apart from loss."""
    if not os.path.exists(ARCHIVE):
        print("No faults logged yet.")
        return 0
    rows = [json.loads(l) for l in open(ARCHIVE) if l.strip()]
    best: Dict[tuple, dict] = {}
    for r in rows:
        k = _bet_key(r)
        if k not in best or r["captured_at"] < best[k]["captured_at"]:
            best[k] = r
    opps = list(best.values())

    # Only the half you can act on. The mirrored row is the same fault seen
    # from the side that is worse for you, and betting it would be the error.
    actionable = [r for r in opps if r.get("delta", 0) > 0]

    print(f"\n  Fault ledger")
    print(f"  logged              {len(rows)} rows -> {len(opps)} opportunities -> "
          f"{len(actionable)} actionable")
    scans = sorted({r["captured_at"] for r in rows})
    print(f"  scans               {len(scans)}"
          + (f"  ({scans[0][:10]} to {scans[-1][:10]})" if scans else ""))

    for kind in ("STALE", "TRANSPOSED"):
        k = [r for r in actionable if r.get("kind") == kind]
        if not k:
            continue
        g = [r for r in k if r.get("status") in ("won", "lost", "push")]
        w = sum(1 for r in g if r["status"] == "won")
        u = sum(r.get("units", 0) for r in g)
        print(f"\n  {kind}")
        print(f"    found             {len(k)}")
        print(f"    settled           {len(g)}" + (f"  ({w}-{len(g)-w})" if g else ""))
        if g:
            print(f"    units             {u:+.2f} on {len(g)} at 1u")
        placed = [r for r in k if r.get("honoured") is not None]
        if placed:
            paid = sum(1 for r in placed if r["honoured"])
            print(f"    actually placed   {len(placed)}, honoured {paid}, "
                  f"voided {len(placed) - paid}")
        else:
            print(f"    actually placed   0 — so nothing here says whether a book would pay")

    ungraded = [r for r in actionable if not r.get("graded_at")]
    if ungraded:
        print(f"\n  awaiting kickoff    {len(ungraded)}")
        for r in sorted(ungraded, key=lambda x: x.get("kickoff", ""))[:8]:
            print(f"    {r.get('kickoff','')[:16]}  {r.get('kind'):11} {r.get('book'):11} "
                  f"{str(r.get('side'))[:26]:26} {r.get('book_point') or r.get('book_price')}")
    print()
    return 0


def settle(spec: str) -> int:
    """
    Record whether a book actually paid: --settle "<book>:<side>:honoured|voided".

    Manual because it cannot be anything else. No feed reports that a ticket was
    graded away under a palpable-error clause, and this is the only number that
    decides whether TRANSPOSED finds are worth chasing.
    """
    try:
        book, side, verdict = spec.split(":", 2)
    except ValueError:
        print('Use --settle "book:side:honoured" or "...:voided"')
        return 1
    if verdict not in ("honoured", "voided"):
        print("The verdict must be honoured or voided.")
        return 1
    rows = [json.loads(l) for l in open(ARCHIVE) if l.strip()]
    n = 0
    for r in rows:
        if r.get("book") == book and side.lower() in str(r.get("side", "")).lower():
            r["honoured"] = (verdict == "honoured")
            n += 1
    with open(ARCHIVE, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"marked {n} row(s) {verdict}")
    return 0


# ------------------------------------------------------------------- alerting

# The books the user actually holds, from the one registry that knows their
# products and prices. Re-declaring the pair here is how it drifted last time:
# books.py knew FanDuel prices a 2-team teaser at -134 and this file did not.
MY_BOOKS = BOOKS.MINE


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

    # A transposition gets the SMALLER stake, despite the bigger edge, which is
    # the opposite of what Kelly says and is deliberate. Kelly prices the game;
    # it does not price the counterparty. A book reviewing an obvious error
    # voids the ticket that made it worth reviewing, and quietly pays the one
    # that did not — so size here buys a void, and the account along with it.
    kind = f.get("kind") or classify(f)
    if kind == "TRANSPOSED":
        units = min(units, TRANSPOSED_MAX_UNITS)

    return {"kind": kind,
            "side": f["side"], "point": bp, "price": price, "book": f["book"],
            "market_point": cp, "gain": bp - cp, "chance": p, "edge": edge,
            "units": min(units, 3.0), "game": f["game"], "kickoff": f["kickoff"]}


def kalshi_bet(f: dict) -> Optional[dict]:
    """
    A Kalshi gap as a bet. Same quarter-Kelly and 3U cap as bet_from.

    A contract costs `c` (price + fee) and pays $1, so it is a bet at odds
    (1-c)/c and Kelly's fraction reduces to (p - c) / (1 - c).
    """
    if f.get("market") != "kalshi" or f.get("edge", 0) <= 0:
        return None
    p, c = f["consensus"], f["kalshi_price"] + f["fee"]
    kelly = (p - c) / (1 - c)
    units = max(0.5, round(kelly / 4 / 0.01 * 2) / 2)
    return {"kind": "KALSHI", "book": "kalshi", "side": f["side"],
            "kalshi_price": f["kalshi_price"], "cost": c, "how": f["how"],
            "ticker": f["ticker"], "size": f["size"], "chance": p,
            "edge": f["edge"], "units": min(units, 3.0),
            "game": f["game"], "kickoff": f["kickoff"], "placed": f.get("placed")}


def place_kalshi(hits: List[dict]) -> None:
    """
    Order every new Kalshi bet, writing the result onto its hit for the push.

    Each one goes in edge order, so if the daily cap runs out it is the
    smallest gap that misses.
    """
    import kalshi_trade
    ks = [h for h in hits if h.get("kind") == "KALSHI"]
    for h in sorted(ks, key=lambda h: -h["edge"]):
        bet = kalshi_bet(h)
        if not bet:
            continue
        h["placed"] = kalshi_trade.place(bet)
        print(f"  kalshi {h['league']} {h['side']}: {h['placed']}")
        if h["placed"].startswith("PLACED"):
            # Its own push, so a placed bet is never folded into another
            # alert's "(+N more)" -- and then left out of that alert.
            bet["placed"] = h["placed"]
            _deliver(_kalshi_text(bet, kalshi_trade.UNIT_DOLLARS))
            h["pushed"] = True


def _kalshi_text(t: dict, unit_dollars: float) -> str:
    risk = t["units"] * unit_dollars
    n = round(risk / t["cost"], 2)        # Kalshi trades hundredths of a contract
    side = t["ticker"].rsplit("-", 1)[-1]
    placed = t.get("placed") or ""
    head = "BET PLACED at KALSHI" if placed.startswith("PLACED") else "BET NOW at KALSHI"
    return (f"{head}\n" + (f"{placed}\n" if placed else "") +
            f"{t['side']} to win: buy {t['how']} {side} at {t['kalshi_price']*100:.0f}c\n"
            f"books {t['chance']*100:.1f}% vs {t['cost']*100:.0f}c with fee — "
            f"{t['edge']*100:.1f}pp\n"
            f"{n:g} contracts ${n * t['cost']:.2f} to win ${n * (1 - t['cost']):.2f}"
            f"  ({t['units']:g}U)\n"
            f"{t['size']:,.0f} at that price\n"
            f"{t['game'][:40]} {t['kickoff'][5:16].replace('T', ' ')}Z")


def alert_text(hits: List[dict], mine_only: bool = True,
               unit_dollars: float = 100.0) -> Optional[str]:
    """
    A complete instruction, short enough for a lock screen.

    WHERE first, then WHAT, then HOW MUCH -- in that order because the window
    is about an hour and anything requiring a decision has already cost you
    the line. Nothing here needs working out.

    TRANSPOSED hits alert too, and lead when present. They used to be silenced
    as "voidable", which had the arithmetic backwards: a voided bet RETURNS THE
    STAKE. It is a bet that never happened, not a loss, so the break-even
    honour rate is zero and any rate above it is profitable. The ten-and-a-half
    points on the one real example were worth +41% at -110 in college and +51%
    in the NFL; at a 10% honour rate that is still +4% an attempt.

    They also vanish fastest, which makes an alert more useful for them, not
    less. What they cost is not money but the account -- books limit people who
    pick off errors -- so the text says which kind it is and leaves the decision
    to whoever is holding the phone.
    """
    bets = []
    for h in hits:
        if h.get("kind") == "KALSHI":
            if h.get("pushed"):
                continue
            # Kalshi is an account the user holds, so mine_only lets it through.
            bet = kalshi_bet(h)
            if bet:
                bets.append(bet)
            continue
        if h.get("kind") not in ("STALE", "TRANSPOSED"):
            continue
        if mine_only and not any(b in h["book"].lower() for b in MY_BOOKS):
            continue
        bet = bet_from(h)
        if bet:
            bets.append(bet)
    if not bets:
        return None
    # A transposition is worth an order of magnitude more than a stale point or
    # two, so it leads whenever one is present.
    bets.sort(key=lambda b: (b.get("kind") != "TRANSPOSED", -b["edge"]))
    t = bets[0]
    more = f"\n(+{len(bets)-1} more)" if len(bets) > 1 else ""
    if t["kind"] == "KALSHI":
        import kalshi_trade
        return _kalshi_text(t, kalshi_trade.UNIT_DOLLARS) + more
    risk = t["units"] * unit_dollars
    profit = risk * (t["price"] / 100 if t["price"] > 0 else 100 / abs(t["price"]))
    when = t["kickoff"][5:16].replace("T", " ")
    price_s = f"+{t['price']}" if t["price"] > 0 else str(t["price"])
    head = "WRONG TEAM FAVOURED" if t.get("kind") == "TRANSPOSED" else "BET NOW"
    tail = ("\nmay be voided as an obvious error — a void returns the stake"
            if t.get("kind") == "TRANSPOSED" else "")
    return (f"{head} at {t['book'].upper()}\n"
            f"{t['side']} {t['point']:+g} {price_s}\n"
            f"market {t['market_point']:+g} — {abs(t['gain']):.1f} pts better\n"
            f"risk ${risk:.0f} to win ${profit:.0f}  ({t['units']:g}U)\n"
            f"{t['chance']*100:.0f}% to cover\n"
            f"{t['game'][:40]} {when}Z{tail}{more}")


def _alert_key(h: dict) -> str:
    """One fault = one book's number on one side of one game. A new number is a new fault."""
    # Kalshi rows carry neither: its ask ticks a cent every few minutes, and a
    # new alert per cent would be the same gap pushed all afternoon.
    num = h.get("book_point", h.get("book_price"))
    return f"{h['league']}|{h['game']}|{h['market']}|{h['book']}|{h['side']}|{num}"


def new_only(hits: List[dict], state_path: str) -> List[dict]:
    """
    Drop hits already alerted, and remember the rest.

    The fast scan runs every two minutes and a transposed line stands about an
    hour, so without this one error is thirty texts. Keys expire with the game:
    anything whose kickoff has passed is forgotten, which keeps the file small.
    """
    try:
        seen = json.load(open(state_path))
    except Exception:
        seen = {}
    now = datetime.now(timezone.utc).isoformat()
    seen = {k: v for k, v in seen.items() if v > now}
    fresh = [h for h in hits if _alert_key(h) not in seen]
    for h in fresh:
        seen[_alert_key(h)] = h["kickoff"].replace("Z", "+00:00")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    json.dump(seen, open(state_path, "w"))
    return fresh


def maybe_recap(state_path: str, now: Optional[datetime] = None) -> bool:
    """
    The Monday Kalshi recap, sent by the first fast scan after 9am ET Monday.

    Riding the five-minute watch rather than a weekly cron of its own: GitHub
    managed 4-5 of 288 scheduled runs a day here, and a once-a-week schedule
    has nothing to fall back on. The sent marker lives in the alerted-set the
    watch already caches, keyed by the Monday and expiring a week on, so
    new_only's pruning keeps it exactly as long as it is needed.
    """
    now = now or datetime.now(timezone.utc)
    import kalshi_trade
    et = kalshi_trade._et(now)
    if et.weekday() != 0 or et.hour < 9:
        return False
    key = f"recap|{et:%Y-%m-%d}"
    try:
        seen = json.load(open(state_path))
    except Exception:
        seen = {}
    if key in seen:
        return False
    try:
        text = kalshi_trade.recap(now)
    except Exception as e:
        print(f"[faults] recap failed: {type(e).__name__}")
        return False
    _deliver(text)
    seen[key] = (now + timedelta(days=7)).isoformat()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    json.dump(seen, open(state_path, "w"))
    return True


def send_push(topic: str, body: str) -> bool:
    """
    ntfy.sh push: lands on the lock screen in seconds, free, no account.

    Chosen over SMS on 2026-09-27: Twilio needs a bought number and days of
    carrier verification before a US text is delivered, and these lines stand
    about an hour. The topic name IS the credential -- anyone who knows it can
    read or forge alerts -- so it lives in secrets, never in the repo, and is
    never printed.
    """
    title, _, rest = body.partition("\n")
    transposed = title.startswith("WRONG TEAM")
    placed = title.startswith("BET PLACED") or title.startswith("KALSHI WEEK")
    try:
        r = requests.post(f"https://ntfy.sh/{topic}", data=rest.encode("utf-8"),
                          headers={"Title": title,
                                   "Priority": "urgent" if transposed else "high",
                                   "Tags": ("rotating_light" if transposed else
                                            "white_check_mark" if placed else "moneybag")},
                          timeout=20)
    except requests.RequestException as e:
        print(f"  push failed: {e}")
        return False
    print(f"  push -> ntfy: HTTP {r.status_code}")
    return r.ok


def send_alert(hits: List[dict]) -> bool:
    """Push when NTFY_TOPIC is set, else SMS when Twilio is, else email. Never two."""
    body = alert_text(hits)
    if not body:
        return False
    return _deliver(body)


def _deliver(body: str) -> bool:
    """One message through the configured route: push, else SMS, else email."""
    print("\n--- ALERT ---\n" + body)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        return send_push(topic, body)
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
    ap.add_argument("--grade", action="store_true",
                    help="settle logged faults against finished games")
    ap.add_argument("--ledger", action="store_true",
                    help="what the faults have done, void kept apart from loss")
    ap.add_argument("--settle", metavar="BOOK:SIDE:VERDICT",
                    help="record that a book honoured or voided a placed bet")
    ap.add_argument("--alert", action="store_true",
                    help="text/email when a STALE line appears at your books")
    ap.add_argument("--markets", default="spreads,h2h",
                    help="comma-separated; fewer markets costs fewer credits")
    ap.add_argument("--source", choices=("odds-api", "an"), default="odds-api",
                    help="an = Action Network: free, so it can run every few minutes")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one clearly-labelled sample through the real alert route")
    ap.add_argument("--fast", action="store_true",
                    help="the frequent free scan: --source an, alert only on faults "
                         "not already alerted, log to cache/ instead of the archive, "
                         "no grading")
    a = ap.parse_args()
    if a.settle:
        return settle(a.settle)
    if a.grade:
        return grade()
    if a.ledger:
        return ledger()
    if a.history:
        return history()
    if a.test_alert:
        # Through send_alert(), not send_push(), so the test exercises the same
        # routing and secrets a real fault would.
        sample = {"market": "spreads", "book": "betmgm", "side": "TEST — not a real line",
                  "book_point": 5.0, "consensus_point": -5.5, "delta": 10.5, "price": -110,
                  "peers": 5, "game": "Test @ Alert", "league": "NFL",
                  "kickoff": "2099-01-01T00:00:00Z", "kind": "TRANSPOSED"}
        return 0 if send_alert([sample]) else 1
    if a.fast:
        # Logs to cache/, not data/archive/: CI commits the archive, and a
        # local job appending to it every two minutes would collide with that.
        hits = scan(source="an", archive=os.path.join(ROOT, "cache", "faults_fast.ndjson"))
        fresh = new_only(hits, os.path.join(ROOT, "cache", "faults_alerted.json"))
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"{stamp} {len(hits)} fault(s), {len(fresh)} new")
        if fresh:
            report(fresh)
            place_kalshi(fresh)
            send_alert(fresh)
        maybe_recap(os.path.join(ROOT, "cache", "faults_alerted.json"))
        return 0
    if a.scan:
        mk = tuple(m.strip() for m in a.markets.split(",") if m.strip())
        hits = scan(markets=mk)
        report(hits)
        if a.alert:
            send_alert(hits)
        grade()          # costs no credits, and keeps the ledger current
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
