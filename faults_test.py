"""
Tests for the stale-line detector.

Two mistakes it already made and must not repeat: calling a normal mirrored
spread a transposition, and reporting both sides of a fault as if both were
bets.
"""

import faults as F


def test_only_the_side_that_gains_is_a_bet():
    """
    DraftKings at Oklahoma -23.5 against a -21.5 market is two rows and one
    bet. New Mexico +23.5 is two points better than the market; Oklahoma
    -23.5 is two points WORSE.
    """
    good = {"kind": "STALE", "market": "spreads", "side": "New Mexico Lobos",
            "book_point": 23.5, "consensus_point": 21.5, "delta": 2.0,
            "price": -110, "book": "betmgm", "game": "A @ B", "kickoff": "x"}
    bad = dict(good, side="Oklahoma Sooners", book_point=-23.5,
               consensus_point=-21.5, delta=-2.0)
    assert F.bet_from(good) is not None
    assert F.bet_from(bad) is None, "laying MORE points than the market is not a bet"


def test_mirrored_line_is_stale_not_transposed():
    """Detroit -16.5 and New Orleans +16.5 is one coherent line."""
    assert F.classify({"market": "spreads", "book_point": -16.5,
                       "consensus_point": -21.5}) == "STALE"


def test_flipped_favourite_is_transposed():
    assert F.classify({"market": "spreads", "book_point": 6.0,
                       "consensus_point": -5.5}) == "TRANSPOSED"


def test_moneyline_transposition_uses_the_50pct_line():
    assert F.classify({"market": "h2h", "book_implied": 0.40,
                       "consensus": 0.64}) == "TRANSPOSED"
    assert F.classify({"market": "h2h", "book_implied": 0.58,
                       "consensus": 0.64}) == "STALE"


BASE = {"kind": "STALE", "market": "spreads", "side": "X", "price": -110,
        "book": "betmgm", "game": "A @ B", "kickoff": "x"}


def _bet(gain):
    return F.bet_from(dict(BASE, book_point=3.0 + gain, consensus_point=3.0,
                           delta=gain))


def test_small_gains_do_not_beat_the_juice():
    """
    Half a point is worth about 1.5 points of win probability and -110 costs
    2.4, so it is not a bet. The break-even gain is ~0.8 points, which is why
    the 2.0-point scan threshold has headroom rather than sitting on the edge.
    """
    assert _bet(0.50) is None
    assert _bet(0.75) is None
    assert _bet(1.00) is not None


def test_more_points_gained_means_a_bigger_stake():
    small, big = _bet(1.0), _bet(3.0)
    assert big["units"] > small["units"]
    assert big["chance"] > small["chance"]


def test_stake_is_capped():
    huge = F.bet_from({"kind": "STALE", "market": "spreads", "side": "X",
                       "book_point": 14.0, "consensus_point": 3.0, "delta": 11.0,
                       "price": -110, "book": "betmgm", "game": "A @ B",
                       "kickoff": "x"})
    assert huge["units"] <= 3.0, "no single stale line justifies the bankroll"


def test_transposed_never_alerts():
    hits = [{"kind": "TRANSPOSED", "book": "betmgm", "market": "spreads",
             "side": "X", "book_point": 6, "consensus_point": -5.5,
             "delta": 11.5, "price": -110, "game": "A @ B", "kickoff": "x"}]
    assert F.alert_text(hits) is None


def test_other_books_never_alert():
    hits = [{"kind": "STALE", "book": "draftkings", "market": "spreads",
             "side": "X", "book_point": 23.5, "consensus_point": 21.5,
             "delta": 2.0, "price": -110, "game": "A @ B", "kickoff": "x"}]
    assert F.alert_text(hits) is None
    assert F.alert_text(hits, mine_only=False) is not None


def test_alert_fits_one_sms_segment():
    hits = [{"kind": "STALE", "book": "betmgm", "market": "spreads",
             "side": "New Mexico Lobos", "book_point": 23.5,
             "consensus_point": 21.5, "delta": 2.0, "price": -110,
             "game": "New Mexico Lobos @ Oklahoma Sooners",
             "kickoff": "2026-09-19T23:30:00Z"}]
    body = F.alert_text(hits)
    assert body.startswith("BET NOW at BETMGM")
    assert len(body) <= 320, f"{len(body)} chars is more than two segments"


# ---------------------------------------------------------------- ml mirroring

def test_ml_logs_both_sides_not_just_the_dear_one():
    """
    A book with a big favourite trips the threshold on the favourite and not on
    the dog, because the same vig is a smaller share of a small number. Logging
    only the side that tripped records the fault and discards the bet.
    """
    prices = {
        "betrivers": {"Miami": -3335, "Wake Forest": 1400},
        "bookA":     {"Miami": -1000, "Wake Forest": 650},
        "bookB":     {"Miami": -1050, "Wake Forest": 660},
        "bookC":     {"Miami": -980,  "Wake Forest": 640},
    }
    rows = F.ml_faults(prices)
    br = [r for r in rows if r["book"] == "betrivers"]
    sides = {r["side"] for r in br}
    assert sides == {"Miami", "Wake Forest"}, f"only logged {sides}"
    # And exactly one of them is the half worth acting on.
    actionable = [r for r in br if r["delta"] > 0]
    assert len(actionable) == 1, f"{len(actionable)} actionable sides"


def test_ml_silent_when_nothing_trips():
    prices = {
        "betrivers": {"Miami": -1000, "Wake Forest": 650},
        "bookA":     {"Miami": -1000, "Wake Forest": 650},
        "bookB":     {"Miami": -1010, "Wake Forest": 655},
        "bookC":     {"Miami": -990,  "Wake Forest": 645},
    }
    assert F.ml_faults(prices) == []


def test_pickem_sign_flip_is_stale_not_transposed():
    """
    Atlanta +1.5 against a -1.0 consensus crosses zero, but it is a 2.5-point
    move on a near-pick'em, not an inverted line. Calling it TRANSPOSED
    suppressed the alert on a bettable NFL number, because transposed hits
    never alert.
    """
    f = {"market": "spreads", "book_point": 1.5, "consensus_point": -1.0}
    assert F.classify(f) == "STALE"


def test_big_sign_flip_is_still_transposed():
    """Kansas +5.0 against a -5.5 consensus: 10.5 points, too big to be a move."""
    f = {"market": "spreads", "book_point": 5.0, "consensus_point": -5.5}
    assert F.classify(f) == "TRANSPOSED"


def test_moneyline_coinflip_straddle_is_stale():
    f = {"market": "h2h", "book_implied": 0.52, "consensus": 0.46}
    assert F.classify(f) == "STALE"


def test_moneyline_real_inversion_is_transposed():
    """BetMGM had Kansas at 64.9% where the market said 35.7%."""
    f = {"market": "h2h", "book_implied": 0.6491, "consensus": 0.357}
    assert F.classify(f) == "TRANSPOSED"


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok   {n}")
            except AssertionError as e:
                f += 1; print(f"  FAIL {n}: {e}")
    print(f"\n{f} failure(s)")
    raise SystemExit(1 if f else 0)
