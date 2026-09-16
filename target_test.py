"""
Tests for the payout-target solver. The arithmetic and the refusals.

What must never break: a ticket cannot use both sides of one game, the routes
must be priced against the payout actually demanded, and the teaser route must
not be offered for college — its bands and joint rates are NFL measurements,
and lending them to a college ticket would hand it a result it does not have.
"""

import json
import os
import tempfile

import target as G


def leg(abbr, odds, prob, event_id="1"):
    return G.Leg(event_id, f"{abbr} game", "2026-09-20T17:00Z", abbr, odds, prob)


def check(name, got, want):
    ok = abs(got - want) < 1e-6 if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main():
    r = []

    # A fair ticket has zero EV, which is the anchor for every other number.
    t = G.Ticket("single", [leg("A", 300, 0.25)], 4.0, 0.25)
    r.append(check("fair ticket EV is zero", t.ev, 0.0))
    r.append(check("break-even is one over the payout", t.breakeven, 0.25))
    r.append(check("american round-trips", t.american, 300))

    # The single route takes the likeliest side that still pays the target, and
    # refuses when nothing reaches it rather than offering a near miss.
    legs = [leg("A", 250, 0.30, "1"), leg("B", 400, 0.22, "2"), leg("C", 150, 0.42, "3")]
    t = G.best_single(legs, 200)
    r.append(check("single clears the target", t.legs[0].abbr, "A"))
    r.append(check("single refuses an unreachable target", G.best_single(legs, 900), None))

    # Both sides of one game is not a parlay: it is a bet against yourself, and
    # the payout would look real.
    same = [leg("HOME", 120, 0.52, "9"), leg("AWAY", 110, 0.48, "9")]
    r.append(check("one leg per game", G.best_parlay(same, 200, 3), None))

    # Fewer legs is always cheaper, because hold compounds per leg. Two legs
    # that reach the target must beat any three-leg route to the same payout.
    pool = [leg("A", -110, 0.52, "1"), leg("B", -110, 0.52, "2"),
            leg("C", -110, 0.52, "3"), leg("D", -110, 0.52, "4")]
    t = G.best_parlay(pool, 250, 4)
    r.append(check("stops at the fewest legs that reach it", len(t.legs), 2))

    # College gets no teaser route. The bands are NFL margins: the same windows
    # win 70.9% per leg in college where -110 needs 72.4%.
    r.append(check("no teaser route for college", G.teaser_tickets(300, "NCAAF"), []))
    nfl = G.teaser_tickets(300, "NFL")
    r.append(check("NFL teaser routes exist at +300", len(nfl) > 0, True))
    r.append(check("every teaser route clears the target",
                   all(t.decimal >= 4.0 for t in nfl), True))
    r.append(check("unverified ladder prices are flagged",
                   any(t.ladder_assumed for t in G.teaser_tickets(200, "NFL")), True))

    # The measured joint rates are what price the teaser, not the product of
    # the per-leg rate — that distinction is the whole reason they were measured.
    import teaser as T
    six = next(t for t in G.teaser_tickets(700, "NFL") if t.route.startswith("6-pt"))
    r.append(check("teaser priced off the measured joint rate", six.prob, T.JOINT_6PT[6]))

    # load_legs reads the market's de-vigged numbers, not the model's.
    board = {"leagues": {"NFL": {"games": [{
        "event_id": "77", "short_name": "A @ B", "kickoff": "2026-09-20T17:00Z",
        "home": {"moneyline": -200, "abbr": "B"}, "away": {"moneyline": 170, "abbr": "A"},
        "legs": [{"side": "home", "team_abbr": "B", "odds": -200},
                 {"side": "away", "team_abbr": "A", "odds": 170}],
    }]}}}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "board.json")
        with open(p, "w") as fh:
            json.dump(board, fh)
        got = G.load_legs("NFL", p)
    r.append(check("both sides load", len(got), 2))
    r.append(check("de-vigged probabilities sum to one",
                   round(sum(l.prob for l in got), 6), 1.0))

    print(f"\n  {sum(r)} passed, {len(r) - sum(r)} failed")
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
