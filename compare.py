"""
Two routes to the same payout, priced side by side.

    python compare.py --league NFL --target 300
    python compare.py --league NCAAF --target 500 --pool 12

THE QUESTION THIS SETTLES. There are two ways to be paid +300: stack
moneylines the models like, or take a teaser off the ladder. They are not
comparable on the face of it -- one is a forecast, the other is a structure --
so this prices both against the same target and shows every number that
differs, including the one that matters most: whether the chance being quoted
comes from a model or from a measurement.

WHY EVERY PARLAY IS SHOWN TWICE. Its chance on our own models, and its chance
on the market's de-vigged prices. When those two agree the parlay is honest
and the price is short. When they disagree the parlay looks wonderful, and the
disagreement IS the reason -- the ledger has graded claims of that size at 5-16
(-41%) past ten points and 1-9 (-75%) past twenty. So both are printed, and
the gap between them is printed too, because a route whose edge comes entirely
from disagreement is the one this project keeps proving wrong.

The teaser line needs no such warning. Its chance is measured from finished
games rather than forecast from ratings, which is the whole reason it survived
its backtest while every model here failed.
"""

from __future__ import annotations

import argparse
import datetime
import itertools
import json
import math
import os
from typing import Dict, List, Optional

import boxscores
import efficiency
import teaser as T

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(ROOT, "public", "data", "board.json")

# Margin-to-probability needs the spread of final margins around the
# prediction: about 13.2 points in the NFL, wider in college where blowouts
# are routine. Fitted loosely from the same box-score data the ratings use.
MARGIN_SD = {"NFL": 13.2, "NCAAF": 16.5}

# A rating built on two games is not a rating. FCS opponents and teams with
# almost no history are refused outright rather than quietly given the benefit
# of a league-average prior.
MIN_RATED_GAMES = 6


def _dec(american: int) -> float:
    return 1 + (american / 100.0 if american > 0 else 100.0 / -american)


def _american(dec: float) -> int:
    return int(round((dec - 1) * 100)) if dec >= 2 else -int(round(100 / (dec - 1)))


def model_legs(league: str, board: Dict, floor: float = 0.60) -> List[Dict]:
    """
    Every side this week that BOTH models put above the floor.

    Two models rather than one, and the LOWER of the two is what a leg is
    judged on: they are built from different data -- points scored against
    points expected per play -- and a side only one of them likes is a side
    where one of them is wrong.
    """
    lg = (board.get("leagues") or {}).get(league) or {}
    rows = boxscores.load_boxscores(league, 2026) + boxscores.load_boxscores(league, 2025)
    eff = efficiency.build_ratings(rows)
    sd = MARGIN_SD.get(league, 14.0)
    cutoff = T.slate_end()

    out: List[Dict] = []
    for g in lg.get("games") or []:
        try:
            ko = datetime.datetime.fromisoformat(str(g["kickoff"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ko > cutoff:
            continue                      # this week's slate only
        hid, aid = g["home"]["id"], g["away"]["id"]
        if hid not in eff.games or aid not in eff.games:
            continue
        if min(eff.games[hid], eff.games[aid]) < MIN_RATED_GAMES:
            continue
        margin = eff.expected_margin(hid, aid, bool(g.get("neutral")))
        p_home = 1 - 0.5 * (1 + math.erf((0 - margin) / (sd * math.sqrt(2))))
        for leg in g.get("legs") or []:
            side = leg["side"]
            box = p_home if side == "home" else 1 - p_home
            elo = leg["model_prob"]
            lo = min(box, elo)
            if lo < floor:
                continue
            out.append({
                "team": leg["team_abbr"], "event": str(g["event_id"]),
                "game": g.get("short_name") or "", "odds": int(leg["odds"]),
                "dec": _dec(int(leg["odds"])), "box": box, "elo": elo,
                "model": lo, "market": leg["implied_prob"],
                "gap": lo - leg["implied_prob"],
            })
    return sorted(out, key=lambda l: -l["model"])


def best_parlays(legs: List[Dict], target: int, pool: int = 12,
                 max_legs: int = 8, top: int = 4) -> List[Dict]:
    """
    Every arrangement of the best `pool` legs that reaches the target.

    Ranked by the models' own probability, which is what was asked for -- with
    the market's number carried alongside so the two can be compared rather
    than confused.
    """
    want = _dec(target)
    pool_legs = legs[:pool]
    found: List[Dict] = []
    for n in range(2, max_legs + 1):
        for combo in itertools.combinations(pool_legs, n):
            if len({c["event"] for c in combo}) != n:
                continue              # one leg per game: never both sides
            dec = 1.0
            p_model = 1.0
            p_market = 1.0
            for c in combo:
                dec *= c["dec"]
                p_model *= c["model"]
                p_market *= c["market"]
            if dec < want:
                continue
            found.append({
                "legs": list(combo), "dec": dec, "n": n,
                "p_model": p_model, "p_market": p_market,
                "needs": 1 / dec,
                "ev_model": p_model * (dec - 1) - (1 - p_model),
                "ev_market": p_market * (dec - 1) - (1 - p_market),
                "gap": max(c["gap"] for c in combo),
            })
    # The BEST route at each leg count, fewest first, rather than the best
    # routes overall. Legs are a cost the percentages do not show -- another
    # line to get down at one book, another push, another late scratch -- so
    # the useful answer is "three legs gets you there at 49%, five gets you
    # there at 15%", not three variations on the same five-leg ticket.
    by_count: Dict[int, Dict] = {}
    for f in found:
        best = by_count.get(f["n"])
        if best is None or f["p_model"] > best["p_model"]:
            by_count[f["n"]] = f
    return [by_count[n] for n in sorted(by_count)][:top]


def mixed_pool(board: Dict, points: int) -> List[Dict]:
    """
    Every qualifying leg from both leagues at one tease size, best first.

    Books let a football teaser mix NFL and college, which is worth using --
    but only as filler. A college leg wins 70.6% at six points against the
    NFL's 73.6%, and 77.2% against 79.2% at ten, so every college leg added
    ahead of an available NFL one makes the ticket worse. Sorted by measured
    rate, the order takes care of itself.
    """
    out = []
    for lg in ("NFL", "NCAAF"):
        t = ((board.get("leagues") or {}).get(lg) or {}).get("teasers") or {}
        block = t if points == 6 else (t.get("ten") or {})
        rate = block.get("per_leg_rate")
        for leg in block.get("legs") or []:
            if rate:
                out.append(dict(leg, league=lg, rate=rate, points=points))
    return sorted(out, key=lambda l: -l["rate"])


def mixed_ticket(board: Dict, points: int, n: int, price: int) -> Optional[Dict]:
    """
    The best n-leg ticket at one tease size, across both leagues.

    Priced by MULTIPLYING each leg's own measured rate rather than reading a
    joint rate off the table: the joints were measured within one league, and
    a ticket spanning two has no measured joint. Multiplying is the
    conservative choice -- the NFL's measured joints came in slightly above
    independence, so this understates a same-league ticket rather than
    flattering a mixed one.
    """
    pool = mixed_pool(board, points)
    if len(pool) < n:
        return None
    legs = pool[:n]
    p = 1.0
    for l in legs:
        p *= l["rate"]
    dec = _dec(price)
    return {"points": points, "n": n, "price": price, "dec": dec, "legs": legs,
            "p": p, "needs": 1 / dec, "ev": p * (dec - 1) - (1 - p),
            "leagues": sorted({l["league"] for l in legs}),
            "independent": True}


def teaser_routes(league: str, board: Dict, target: int) -> List[Dict]:
    """Ladder tickets that clear the target, priced off measured joint rates."""
    lg = (board.get("leagues") or {}).get(league) or {}
    t = lg.get("teasers") or {}
    want = _dec(target)
    out = []
    for pts, block in (t.get("ladder") or {}).items():
        pool = ((t.get("ten") if pts == "10" else t) or {}).get("legs") or []
        for n_str, price in (block.get("prices") or {}).items():
            n = int(n_str)
            p = (block.get("joint") or {}).get(n_str)
            if p is None or len(pool) < n:
                continue
            dec = _dec(int(price))
            if dec < want:
                continue
            out.append({"points": int(pts), "n": n, "price": int(price),
                        "dec": dec, "p": float(p), "needs": 1 / dec,
                        "ev": p * (dec - 1) - (1 - p),
                        "legs": pool[:n]})
    return sorted(out, key=lambda r: -r["ev"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=("NFL", "NCAAF"))
    ap.add_argument("--target", type=int, default=300)
    ap.add_argument("--pool", type=int, default=12, help="best N legs to arrange")
    ap.add_argument("--max-legs", type=int, default=8)
    ap.add_argument("--floor", type=float, default=0.60,
                    help="both models must put a leg above this")
    a = ap.parse_args()

    with open(BOARD) as fh:
        board = json.load(fh)

    legs = model_legs(a.league, board, a.floor)
    print(f"\n{len(legs)} {a.league} sides this week that both models put above "
          f"{a.floor*100:.0f}%; arranging the best {min(a.pool, len(legs))}\n")
    print(f"  {'leg':6}{'price':>7}{'models':>9}{'market':>9}{'gap':>8}")
    for l in legs[:a.pool]:
        print(f"  {l['team']:6}{l['odds']:+7}{l['model']*100:8.1f}%"
              f"{l['market']*100:8.1f}%{l['gap']*100:+7.1f}pp")

    print(f"\n{'='*66}\nMONEYLINE ROUTES TO {a.target:+d}\n{'='*66}")
    for r in best_parlays(legs, a.target, a.pool, a.max_legs):
        print(f"\n  {r['n']} legs, pays {_american(r['dec']):+d}")
        # Leg by leg, because a ticket's number hides which leg is carrying the
        # risk. Two models and the price, side by side: where all three agree
        # the leg is solid and cheap; where the models run ahead of the price
        # that leg is both the reason the payout exists and the reason to
        # doubt it.
        print(f"      {'leg':6}{'price':>7}{'box':>8}{'elo':>8}{'market':>9}{'gap':>9}")
        for c in r["legs"]:
            flag = "  <- carries it" if c["gap"] > 0.10 else ""
            print(f"      {c['team']:6}{c['odds']:+7}{c['box']*100:7.1f}%"
                  f"{c['elo']*100:7.1f}%{c['market']*100:8.1f}%{c['gap']*100:+8.1f}pp{flag}")
        print(f"    all of them landing: models {r['p_model']*100:5.1f}%  -> EV {r['ev_model']*100:+7.1f}%")
        print(f"                         market {r['p_market']*100:5.1f}%  -> EV {r['ev_market']*100:+7.1f}%")
        print(f"    needs {r['needs']*100:.1f}% to break even")

    print(f"\n{'='*66}\nTEASER ROUTES TO {a.target:+d}\n{'='*66}")
    routes = teaser_routes(a.league, board, a.target)
    if not routes:
        print("\n  none: the ladder tops out below this payout.")
    for r in routes:
        print(f"\n  {r['n']} legs at {r['points']} points, pays {r['price']:+d}")
        print(f"      {'leg':16}{'from':>7}{'to':>8}{'wins':>8}")
        for l in r["legs"]:
            print(f"      {l['team_abbr'] + ' ' + l.get('matchup', ''):16}"
                  f"{l['spread']:+7g}{l['teased']:+8g}{l['prob']*100:7.1f}%")
        print(f"    all of them landing: {r['p']*100:5.1f}% measured  -> EV {r['ev']*100:+7.1f}%")
        print(f"    needs {r['needs']*100:.1f}% to break even")

    print(f"\n{'='*66}")
    print("The moneyline EV on our models is a forecast; the teaser EV is a")
    print("measurement. Where a parlay's edge comes from a big disagreement,")
    print("the ledger has graded those at 5-16 past ten points and 1-9 past")
    print("twenty. Read the gap column before the EV column.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
