"""
Wong teaser scanner: the only structure in this project that survived its test.

WHAT THIS EXPLOITS, AND WHAT IT DOES NOT.

It does not predict games. Every prediction model in this repo has been graded
against the closing line and lost -- the play-by-play matchup model finished at
a t-stat of 0.81, meaning the price already contained everything it knew. This
scanner assumes the line is right and profits from the SHAPE of the outcome
distribution instead.

NFL margins are not smooth. Measured over 6,967 regular-season games:

    3 points ..... 15.0% of all games
    7 points ......  9.1%
    together ..... 24.1%

A teaser buys 6 points of spread at a fixed price. Those 6 points are worth
wildly different amounts depending on WHERE they start. Dragging a side from
-7.5 to -1.5 crosses both spikes and picks up a quarter of the distribution.
Buying from -3.5 to +2.5 crosses nothing that matters and is the worst band on
the board despite feeling safer.

So the qualifying condition is the NUMBER, never the team. Splitting the
qualifying legs every way available shows no team-quality signal at all:

    favourites -8.5..-6.5 .... 73.34%
    underdogs  +1.5..+2.5 .... 74.09%
    home legs ................ 73.17%
    away legs ................ 74.27%

All inside one standard error of each other. Selecting on offence or defence
rank does not strengthen a teaser; it pulls you out of the band, which is the
entire edge. This module therefore ignores ratings on purpose.

THE EDGE IS THE JUICE. A 2-team 6-point teaser needs 72.37% per leg at -110 and
75.18% at -130. The window delivers 73.61% +/- 1.04. That clears -110, sits
inside the noise at -120, and is clearly dead at -130 -- so the price decides
whether the bet exists, not the matchup. The scanner's real job is to refuse
anything priced above break-even, and `max_price()` is the number to act on.

    python teaser.py                 # scan the live board
    python teaser.py --calibrate     # re-derive the bands from nflverse
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

TEASE_POINTS = 6.0

# Empirical win rates, pushes excluded from the denominator, 1999-2025 regular
# season. Re-derive with --calibrate; these are pasted from that run so the
# scanner does not need the play-by-play cache present to size a bet.
@dataclass(frozen=True)
class Band:
    lo: float            # exclusive
    hi: float            # inclusive
    rate: float          # win rate given a decision
    stderr: float
    n: int
    label: str

BANDS: Tuple[Band, ...] = (
    Band(-8.5, -7.5, 0.7238, 0.0235, 362, "fav -8.5..-7.5"),
    Band(-7.5, -6.5, 0.7379, 0.0157, 782, "fav -7.5..-6.5"),
    Band( 1.5,  2.5, 0.7409, 0.0171, 660, "dog +1.5..+2.5"),
)

# Teased lines that land on a whole number push 2.51% of the time; half-point
# teased lines pushed exactly 0 times in 1,104 observations. Most books grade a
# push inside a 2-team teaser as a loss, so this is charged against the leg.
PUSH_RATE_INTEGER = 0.0251
PUSH_RATE_HALF = 0.0

IGNORE_BOOKS = {"betfair_ex_us", "matchbook"}   # exchanges do not offer teasers


def band_for(spread: float) -> Optional[Band]:
    """The qualifying band a side's ORIGINAL spread falls in, if any."""
    for b in BANDS:
        if b.lo < spread <= b.hi:
            return b
    return None


def leg_probability(band: Band, teased: float, push_is_loss: bool = True) -> float:
    """
    Probability this leg wins, charged for push risk.

    `band.rate` is conditional on a decision, so the push mass has to be put
    back before it can be taken away again under the book's grading rule.
    """
    push = PUSH_RATE_INTEGER if float(teased).is_integer() else PUSH_RATE_HALF
    p_win = (1.0 - push) * band.rate
    if push_is_loss:
        return p_win
    return p_win / (1.0 - push) if push < 1.0 else p_win


def payout(price: int) -> float:
    """Profit per unit risked, from an American price."""
    return price / 100.0 if price > 0 else 100.0 / abs(price)


def to_american(profit: float) -> int:
    """Inverse of payout(), rounded toward the bettor's disadvantage."""
    if profit >= 1.0:
        return int(math.floor(profit * 100))
    return -int(math.ceil(100.0 / profit))


def teaser_ev(leg_probs: Iterable[float], price: int) -> float:
    """EV per unit risked. Legs are treated as independent."""
    p = 1.0
    for q in leg_probs:
        p *= q
    return p * payout(price) - (1.0 - p)


def max_price(leg_probs: Iterable[float]) -> int:
    """
    The worst price at which this combination is still break-even.

    This is the number to carry to the book: "play only at X or better". It is
    a THRESHOLD, not a quote, so it rounds toward the bettor -- to_american()
    rounds the other way and would hand back the one price that is already
    negative. A fair -120.61 is reported as -120 (acceptable) and never -121.
    """
    p = 1.0
    for q in leg_probs:
        p *= q
    if p <= 0 or p >= 1:
        return 0
    profit = (1.0 - p) / p
    if profit < 1.0:
        return -int(math.floor(100.0 / profit))   # less juice is better
    return int(math.ceil(profit * 100.0))          # more plus-money is better


def combined_rate(bands: Iterable[Band]) -> Tuple[float, float]:
    """Pooled win rate and standard error across bands, for reporting."""
    bs = list(bands)
    n = sum(b.n for b in bs)
    if not n:
        return 0.0, 0.0
    r = sum(b.rate * b.n for b in bs) / n
    return r, math.sqrt(r * (1 - r) / n)


# ---------------------------------------------------------------- live board

import keys  # noqa: F401  -- importing fills ODDS_API_KEY from the key file

BASE = "https://api.the-odds-api.com/v4"
SPORTS = {"NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf"}


@dataclass
class Leg:
    game: str
    kickoff: str
    side: str
    spread: float
    band: Band
    book: str

    @property
    def teased(self) -> float:
        return self.spread + TEASE_POINTS

    @property
    def prob(self) -> float:
        return leg_probability(self.band, self.teased)

    @property
    def push_risk(self) -> bool:
        return float(self.teased).is_integer()


def fetch_spreads(league: str, regions: str = "us",
                  books: Optional[Iterable[str]] = None) -> List[dict]:
    """
    Spreads for a league, from the books that can actually take the bet.

    `books` asks the API for named bookmakers instead of a whole region. It
    bills the same -- one credit, since a bookmaker list counts as one region
    -- and the point is not the credit: a teaser has to be built inside ONE
    book, so a leg that only qualifies somewhere without an account is not a
    leg. Pass None to see the whole field.
    """
    import requests
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("ODDS_API_KEY is not set. export ODDS_API_KEY=...")
    params = {"apiKey": key, "markets": "spreads", "oddsFormat": "american"}
    if books:
        params["bookmakers"] = ",".join(books)
    else:
        params["regions"] = regions
    r = requests.get(
        f"{BASE}/sports/{SPORTS[league]}/odds",
        params=params,
        timeout=40,
    )
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:200]}")
    print(f"  credits used {r.headers.get('x-requests-used','?')}, "
          f"remaining {r.headers.get('x-requests-remaining','?')}")
    return r.json()


def qualifying_legs(payload: List[dict], within_days: int = 8) -> List[Leg]:
    """
    Every side, at every book, whose own number sits in a qualifying band.

    Per book rather than pooled: a teaser is built inside one book, so a leg
    that only qualifies at DraftKings is no use in a FanDuel teaser. Books
    disagree by a half point often enough that this is the difference between
    a leg existing and not.

    Windowed, because the feed returns the whole remaining season -- 212 events
    in September. A number hung on a January game is a placeholder nobody has
    bet into, and pooling it with this week's slate produced a board with the
    same team qualifying six times.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) + timedelta(days=within_days)
    out: List[Leg] = []
    for ev in payload:
        ct = ev.get("commence_time", "")
        try:
            if datetime.fromisoformat(ct.replace("Z", "+00:00")) > cutoff:
                continue
        except ValueError:
            continue
        game = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        for bk in ev.get("bookmakers") or []:
            if bk.get("key") in IGNORE_BOOKS:
                continue
            for mk in bk.get("markets") or []:
                if mk.get("key") != "spreads":
                    continue
                for oc in mk.get("outcomes") or []:
                    pt = oc.get("point")
                    if pt is None:
                        continue
                    b = band_for(float(pt))
                    if b:
                        out.append(Leg(game, ev.get("commence_time", ""),
                                       oc.get("name", "?"), float(pt), b,
                                       bk.get("title") or bk.get("key")))
    return out


SNAPSHOT_MAX_AGE_H = 12.0


def _book_spreads(league: str):
    """
    The freshest spreads from a book the user holds, if any are recent enough.

    Written by faults.py out of a scan it was paying for anyway. That scan only
    runs when a line moved or a floor slot came round, so this can be hours old
    -- hence the age check and the age travelling to the page. An old number
    quoted as the current one is how a band gets claimed that no longer exists.
    """
    from datetime import datetime, timezone
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "cache", "book_spreads.json")) as fh:
            store = json.load(fh)
        block = store.get(league) or {}
        captured = block.get("captured_at") or ""
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(captured)).total_seconds() / 3600.0
        if age_h > SNAPSHOT_MAX_AGE_H:
            return {}, None, None
    except Exception:
        return {}, None, None

    # One book, not a blend: a teaser is built inside a single book, so mixing
    # FanDuel's number on one leg with BetMGM's on another describes a ticket
    # nobody can place. Preference goes to a book that actually sells teasers.
    import books as BOOKS
    order = sorted(block.get("books") or [],
                   key=lambda k: (not BOOKS.offers_teasers(k), k))
    for key in order:
        lines = [(g, g["points"][key]) for g in block.get("games") or []
                 if key in (g.get("points") or {})]
        if lines:
            book = BOOKS.get(key)
            return lines, (book.name if book else key), captured
    return {}, None, None


def _home_spread(game: dict, book_lines, book_name):
    """
    (home spread, where it came from) for one board game.

    The two feeds do not name teams alike -- "App State Mountaineers" against
    "Appalachian State Mountaineers" -- so the match runs through the same
    comparison the grader uses rather than an equality test that would silently
    fall back to ESPN for half the board.
    """
    if book_lines:
        from teamnames import same_team
        home = (game.get("home") or {}).get("name") or ""
        away = (game.get("away") or {}).get("name") or ""
        for row, point in book_lines:
            if same_team(home, row["home"]) and same_team(away, row["away"]):
                return point, book_name
    esp = game.get("spread")
    return (None, None) if esp is None else (float(esp), game.get("provider"))


def candidates_from_board(games: List[dict], within_days: int = 8,
                          league: str = "NFL",
                          points: float = TEASE_POINTS) -> dict:
    """
    Qualifying legs read off one book's spread, for the static site.

    WHY A SECOND PATH INTO THE SAME BANDS. `qualifying_legs` is the real
    scanner: per book, straight from the odds API, and it costs a credit per
    scan. The site rebuilds hourly and cannot spend a credit each time, so this
    reads a number already paid for -- the snapshot faults.py keeps of the
    books the user holds, and ESPN's quote only when that is missing or stale.

    That makes this a SHORTLIST, not a quote. The band is decided by a half
    point, and books disagree by a half point often enough that a leg here can
    fail to qualify at the book in hand. The Sunday email keeps the per-book
    scan; this says which games to look at and what price to refuse.

    Returned as plain dicts because it is written straight into board.json.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) + timedelta(days=within_days)
    book_lines, book_name, captured = _book_spreads(league)
    legs: List[dict] = []
    for g in games:
        try:
            ko = datetime.fromisoformat(str(g.get("kickoff", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ko > cutoff:
            continue
        # The board stores the HOME team's spread; the away side is its mirror.
        home_spread, source = _home_spread(g, book_lines, book_name)
        if home_spread is None:
            continue
        for side in ("home", "away"):
            spread = float(home_spread) if side == "home" else -float(home_spread)
            if not qualifies(spread, points):
                continue
            teased = spread + points
            push = PUSH_RATE_INTEGER if float(teased).is_integer() else PUSH_RATE_HALF
            band = band_for(spread) if points < 10 else None
            if band is not None and league.upper() != "NCAAF":
                # The 6-point NFL bands are measured individually and differ by
                # a point and a half, so the per-band rate is used where it
                # exists. Everything else is priced off its pooled window rate.
                prob = leg_probability(band, teased)
            else:
                prob = (1.0 - push) * leg_rate(league, points)[0]
            legs.append({
                "event_id": str(g.get("event_id", "")),
                "matchup": g.get("short_name") or g.get("name") or "",
                "kickoff": g.get("kickoff", ""),
                "team_abbr": (g.get(side) or {}).get("abbr", ""),
                "team_name": (g.get(side) or {}).get("name", ""),
                "spread": spread,
                "source": source,
                "teased": teased,
                "band": window_label(spread, points),
                "prob": round(prob, 4),
                "push_risk": float(teased).is_integer(),
            })
    # One leg per game: both sides of the same game cannot share a teaser, and
    # only one of them can be in a band anyway unless the spread is tiny.
    best: Dict[str, dict] = {}
    for l in legs:
        k = l["event_id"]
        if k not in best or l["prob"] > best[k]["prob"]:
            best[k] = l
    legs = sorted(best.values(), key=lambda l: (-l["prob"], l["kickoff"]))

    rate, se = leg_rate(league, points)
    out = {
        "points": points,
        "legs": legs,
        "per_leg_rate": round(rate, 4),
        "per_leg_stderr": round(se, 4),
        "provider": book_name or (games[0].get("provider") if games else None),
        "provider_is_mine": bool(book_name),
        "captured_at": captured,
    }
    if len(legs) >= 2:
        probs = [l["prob"] for l in legs]
        # Both ends of the range, because any two legs are playable and the
        # price that makes the best pair a bet can leave the worst pair a loss.
        out["max_price_best"] = max_price(probs[:2])
        out["max_price_worst"] = max_price(probs[-2:])
        # And the same pair priced one standard error down. The band rate is an
        # estimate off 1,804 games, so a threshold quoted off the point estimate
        # alone says -121 is a bet when the measurement cannot separate -121
        # from a loss. This is the number that survives the error bar, and it is
        # why the section quotes -110 as the rule.
        out["max_price_best_se"] = max_price([p - se for p in probs[:2]])
        # And whether the bet can actually be placed where the user holds an
        # account. A shortlist of qualifying numbers is not a bet if no book in
        # hand sells the product or prices it inside the threshold, and that is
        # a fact about the accounts, not the games -- so it travels with the
        # board rather than being left for the reader to remember.
        import books as BOOKS
        out["at_my_books"] = [
            {
                "book": b.name,
                "playable": playable,
                "reason": reason,
                "quoted": b.teaser_price(2),
            }
            for b in BOOKS.mine()
            for playable, reason in [
                BOOKS.teaser_playable(b.key, 2, out["max_price_best_se"])
            ]
        ]
        out["playable"] = any(x["playable"] for x in out["at_my_books"])
    if len(legs) >= 3:
        # Three legs, priced the same way. Worth stating because a book can
        # price a longer teaser relatively better, and the 2-leg price being
        # refused says nothing about the 3-leg one -- FanDuel's is simply not
        # checked yet, so this is the number to ask for at the slip.
        probs = [l["prob"] for l in legs][:3]
        se = out["per_leg_stderr"]
        out["max_price_3"] = max_price(probs)
        out["max_price_3_se"] = max_price([p - se for p in probs])
        out["at_my_books_3"] = [
            {
                "book": b.name,
                "playable": playable,
                "reason": reason,
                "quoted": b.teaser_price(3),
            }
            for b in BOOKS.mine()
            for playable, reason in [
                BOOKS.teaser_playable(b.key, 3, out["max_price_3_se"])
            ]
        ]
    return out


def by_book(legs: Iterable[Leg]) -> Dict[str, List[Leg]]:
    d: Dict[str, List[Leg]] = {}
    for l in legs:
        d.setdefault(l.book, []).append(l)
    # one leg per game per book: the same side cannot appear twice in a teaser
    for bk, ls in d.items():
        seen: Dict[str, Leg] = {}
        for l in ls:
            if l.game not in seen or l.prob > seen[l.game].prob:
                seen[l.game] = l
        d[bk] = sorted(seen.values(), key=lambda x: -x.prob)
    return d


def report(books: Dict[str, List[Leg]], min_legs: int = 2) -> None:
    r, se = combined_rate(BANDS)
    print(f"\nwindow calibration: {r*100:.2f}% per leg +/- {se*100:.2f}pp "
          f"(n={sum(b.n for b in BANDS):,})")
    print("break-even juice:  -110 needs 72.37%   -120 needs 73.86%   "
          "-130 needs 75.18%")
    print("\nLegs inside a tier are INTERCHANGEABLE -- the edge is the number,")
    print("not the team. Pick any two from the best tier you can get.\n")

    any_book = False
    for book, legs in sorted(books.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(legs) < min_legs:
            continue
        any_book = True
        print(f"=== {book} — {len(legs)} qualifying legs")
        tiers: Dict[float, List[Leg]] = {}
        for l in legs:
            tiers.setdefault(round(l.prob, 4), []).append(l)
        for prob in sorted(tiers, reverse=True):
            group = tiers[prob]
            risk = "  (push risk)" if group[0].push_risk else ""
            print(f"  {prob*100:.1f}% tier — {group[0].band.label}{risk}")
            for l in group:
                print(f"      {l.side:<24}{l.spread:>+6.1f} -> {l.teased:>+5.1f}"
                      f"   {l.kickoff[:10]}   {l.game}")
        top = sorted(tiers, reverse=True)[0]
        if len(tiers[top]) < 2:
            probs = [top, sorted(tiers, reverse=True)[1]]
        else:
            probs = [top, top]
        mp = max_price(probs)
        print(f"  --> two legs at {probs[0]*100:.1f}%/{probs[1]*100:.1f}% "
              f"hit {probs[0]*probs[1]*100:.1f}%")
        print(f"  --> PLAY ONLY AT {mp:+d} OR BETTER"
              f"   (3-leg: {max_price(probs + [probs[0]]):+d})")
        for price in (-110, -120, -130):
            ev = teaser_ev(probs, price)
            print(f"        {price}: EV {ev*100:+6.2f}%  {'+EV' if ev > 0 else 'no'}")
        print()
    if not any_book:
        print("No book offers two qualifying legs. No teaser this week.")


# ---------------------------------------------------------------- calibration

# Measured probability that EVERY leg of an n-leg ticket lands, from every
# in-week combination of qualifying legs, 1999-2025. Re-derive with
# --calibrate-joint.
#
# WHY THESE ARE MEASURED AND NOT MULTIPLIED. Multiplying the per-leg rate
# assumes the legs are independent, and the first check of that looked like it
# failed badly: one ticket per week, 91 of them, came in at 12.1% against the
# 15.9% the product predicted, which reads as legs fighting each other -- a
# blowout week feeds teased favourites and kills teased dogs. Testing every
# in-week combination instead put it at 15.6%, so the 91-ticket sample was
# noise, and at two legs (3,497 pairs) measured and multiplied agree to half a
# point. Independence survives; the point is that it was checked, and the
# numbers below are the ones to price with.
#
# The leg counts matter more than the bands do. The standard payout ladder asks
# for LESS per leg as legs are added -- 72.4% at two legs and -110, 70.0% at
# six and +750 -- while the legs keep winning about 73%. That is why the long
# end of the ladder is where this bet lives, and why evaluating only the
# two-leg version for months made it look barely worth making.
JOINT_6PT = {2: 0.5370, 3: 0.4008, 4: 0.3032, 5: 0.2247, 6: 0.1556}
JOINT_10PT = {2: 0.6301, 3: 0.5050, 4: 0.4092, 5: 0.3378, 6: 0.2868}
# Windows for a 10-point tease, which are NOT the 6-point ones: ten points from
# -12.5..-8.5 lands on -2.5..+1.5 and from +1.5..+3.5 lands on +11.5..+13.5.
# Swept over ten ranges, so treat the choice as fitted rather than found.
BANDS_10PT = ((-12.5, -8.5), (1.5, 3.5))
JOINT_SAMPLE = "in-week combinations, NFL regular season 1999-2025"

# The same windows measured on COLLEGE games, 2023-2025 (the seasons with box
# scores cached). They are weaker at every leg count, which is the whole point
# of keeping them separate: 70.6% per leg against the 72.4% a -110 two-teamer
# needs, so a college teaser is a way to REACH a payout, not an edge. Quoting
# the NFL numbers on a college ticket would lend it a result it does not have.
#
# Bucketed by month rather than by week -- the college line file carries no
# usable week column -- so these combinations mix games a fortnight apart and
# sit closer to independent draws than a single Saturday's slate would. Read
# them as the best available estimate, not as a slate-level measurement.
JOINT_6PT_NCAAF = {2: 0.5051, 3: 0.3534, 4: 0.2422, 5: 0.1630, 6: 0.1079}
JOINT_10PT_NCAAF = {2: 0.5929, 3: 0.4550, 4: 0.3498, 5: 0.2697, 6: 0.2088}


# Pooled per-leg rate for the same windows measured on college games, with its
# standard error. One number rather than per-band: the college sample is 538
# qualifying legs against the NFL's 1,804, and splitting it three ways would be
# reporting noise as structure.
NCAAF_LEG_RATE = (0.7063, 0.0196)

# Ten-point windows pay better per leg because ten points cross more: a -7
# favourite teased to +3 crosses 3, 0 and 7. Pooled over BANDS_10PT, pushes
# charged as losses, same samples as the joint rates above.
NFL_LEG_RATE_10PT = (0.7918, 0.0071)      # n = 3,310 legs, 1999-2025
NCAAF_LEG_RATE_10PT = (0.7723, 0.0147)    # n =   817 legs, 2023-2025


def leg_rate(league: str, points: float = TEASE_POINTS) -> Tuple[float, float]:
    """(rate, standard error) per qualifying leg, for this league and tease."""
    college = league.upper() == "NCAAF"
    if points >= 10:
        return NCAAF_LEG_RATE_10PT if college else NFL_LEG_RATE_10PT
    return NCAAF_LEG_RATE if college else combined_rate(BANDS)


def qualifies(spread: float, points: float = TEASE_POINTS) -> bool:
    """Is this side's number in a window worth teasing at that many points?"""
    if points >= 10:
        return any(lo < spread <= hi for lo, hi in BANDS_10PT)
    return band_for(spread) is not None


def window_label(spread: float, points: float = TEASE_POINTS) -> str:
    if points >= 10:
        for lo, hi in BANDS_10PT:
            if lo < spread <= hi:
                return f"{'fav' if hi <= 0 else 'dog'} {lo:+g}..{hi:+g}"
        return ""
    b = band_for(spread)
    return b.label if b else ""


def joint_rates(league: str, points: float) -> Dict[int, float]:
    """The measured ticket rates for a league and a tease size."""
    if league.upper() == "NCAAF":
        return JOINT_10PT_NCAAF if points >= 10 else JOINT_6PT_NCAAF
    return JOINT_10PT if points >= 10 else JOINT_6PT


def calibrate_joint(points: float = TEASE_POINTS, max_legs: int = 6,
                    path: str = "cache/nflverse_games.csv") -> Dict[int, float]:
    """
    Re-derive JOINT_6PT / JOINT_10PT: the chance an n-leg ticket lands.

    Every in-week combination rather than one ticket a week, because the weekly
    sample is 91 tickets and cannot separate a real edge from nothing. The
    combinations overlap, so this is a better point estimate and NOT a bigger
    sample -- the uncertainty still comes from the ~450 weeks underneath.
    """
    import csv as _csv
    import itertools as _it
    from collections import defaultdict
    bands = BANDS_10PT if points >= 10 else tuple((b.lo, b.hi) for b in BANDS)
    weeks = defaultdict(list)
    with open(path) as fh:
        for r in _csv.DictReader(fh):
            if r.get("game_type") != "REG":
                continue
            try:
                sl, res = float(r["spread_line"]), float(r["result"])
                wk = f"{r['season']}-{int(float(r['week'])):02d}"
            except (TypeError, ValueError, KeyError):
                continue
            for s, d in ((-sl, res), (sl, -res)):
                if any(lo < s <= hi for lo, hi in bands):
                    weeks[wk].append((d + s + points) > 0)
    out = {}
    for n in range(2, max_legs + 1):
        tot = hit = 0
        for outs in weeks.values():
            if len(outs) < n:
                continue
            for combo in _it.combinations(outs, n):
                tot += 1
                hit += all(combo)
        if tot:
            out[n] = round(hit / tot, 4)
            print(f"  {n} legs: {out[n]*100:5.2f}%  ({tot:,} combinations)")
    return out


def calibrate(path: str = "cache/nflverse_games.csv") -> List[Band]:
    """
    Re-derive the bands from completed games. Pushes are excluded from the
    denominator so the rate means "given a decision", which is what
    leg_probability() expects.
    """
    import pandas as pd
    import numpy as np
    df = pd.read_csv(path, low_memory=False)
    d = df.dropna(subset=["result", "spread_line"]).query("game_type=='REG'")
    rows = []
    for _, g in d.iterrows():
        rows.append((-g.spread_line, g.result))
        rows.append(( g.spread_line, -g.result))
    S = pd.DataFrame(rows, columns=["sp", "margin"])
    S["res"] = S.margin + S.sp + TEASE_POINTS

    print(f"{len(d):,} games, {d.season.min()}-{d.season.max()}  "
          f"({len(S):,} side-observations)\n")
    out = []
    for b in BANDS:
        sub = S[(S.sp > b.lo) & (S.sp <= b.hi)]
        dec = sub[sub.res != 0]
        w = float(dec.res.gt(0).mean())
        se = float(np.sqrt(w * (1 - w) / len(dec)))
        drift = (w - b.rate) * 100
        print(f"{b.label:<16} n={len(dec):>5}  {w*100:6.2f}% +/-{se*100:.2f}  "
              f"(stored {b.rate*100:.2f}, drift {drift:+.2f}pp)")
        out.append(Band(b.lo, b.hi, round(w, 4), round(se, 4), len(dec), b.label))
    r, se = combined_rate(out)
    print(f"\npooled: {r*100:.2f}% +/- {se*100:.2f}pp")
    print(f"break-even -110 {math.sqrt(110/210)*100:.2f}%  "
          f"-120 {math.sqrt(120/220)*100:.2f}%  -130 {math.sqrt(130/230)*100:.2f}%")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=sorted(SPORTS))
    ap.add_argument("--calibrate", action="store_true",
                    help="re-derive bands from nflverse and exit")
    ap.add_argument("--regions", default="us")
    ap.add_argument("--books", default=",".join(__import__("books").MINE),
                    help="comma-separated bookmaker keys, or 'all' for the "
                         "whole US field (default: %(default)s)")
    ap.add_argument("--days", type=int, default=8,
                    help="only games kicking off within this many days")
    a = ap.parse_args()

    if a.calibrate:
        calibrate()
        return 0

    books = None if a.books.strip().lower() == "all" else [
        b.strip() for b in a.books.split(",") if b.strip()]
    payload = fetch_spreads(a.league, a.regions, books)
    legs = qualifying_legs(payload, within_days=a.days)
    if not legs:
        print("No side on the board sits in a qualifying band.")
        return 0
    report(by_book(legs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
