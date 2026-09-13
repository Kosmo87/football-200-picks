"""
Tests for the teaser scanner. The arithmetic, not the data.

The bands come from measurement and will move as seasons are added; what must
never move is the pricing. A bug in max_price() recommends a bet the edge does
not support, which is exactly the failure this project keeps finding.
"""

import math
import teaser as T


def test_band_boundaries_are_half_open():
    # lo exclusive, hi inclusive -- a -7.5 favourite belongs to ONE band
    assert T.band_for(-7.5).label == "fav -8.5..-7.5"
    assert T.band_for(-7.0).label == "fav -7.5..-6.5"
    assert T.band_for(-6.5).label == "fav -7.5..-6.5"
    assert T.band_for(-6.0) is None          # crosses neither key number
    assert T.band_for(-8.5) is None          # lo is exclusive
    assert T.band_for(2.5).label == "dog +1.5..+2.5"
    assert T.band_for(1.5) is None
    assert T.band_for(3.0) is None           # already past 3, buys nothing


def test_the_trap_band_is_excluded():
    """-3.5 teased to +2.5 feels safe and is the worst band measured."""
    assert T.band_for(-3.5) is None
    assert T.band_for(-3.0) is None


def test_payout_round_trip():
    for price in (-130, -120, -110, 100, 150, 250):
        assert abs(T.payout(price) - T.payout(T.to_american(T.payout(price)))) < 0.02


def test_to_american_rounds_against_the_bettor():
    # 0.8333 profit is exactly -120; never report -119
    assert T.to_american(100 / 120) == -120
    assert T.to_american(0.83) == -121      # worse than -120, must round down


def test_integer_teased_line_is_charged_for_pushes():
    b = T.band_for(-7.0)                    # teases to -1.0, an integer
    p_int = T.leg_probability(b, -1.0)
    p_half = T.leg_probability(b, -0.5)
    assert p_half > p_int
    assert abs(p_int - (1 - T.PUSH_RATE_INTEGER) * b.rate) < 1e-12


def test_max_price_is_actually_break_even():
    """EV at the reported max price must be >= 0, and negative just past it."""
    for probs in ([0.7379, 0.7409], [0.7238, 0.7238], [0.74, 0.74, 0.74]):
        mp = T.max_price(probs)
        assert T.teaser_ev(probs, mp) >= -1e-9, f"{mp} is not break-even"
        worse = mp - 5 if mp < 0 else mp - 10
        assert T.teaser_ev(probs, worse) < 0, f"{worse} should be negative"


def test_known_result_two_legs_need_better_than_120():
    """The headline finding: the window clears -110 and dies at -130."""
    r, _ = T.combined_rate(T.BANDS)
    probs = [r, r]
    assert T.teaser_ev(probs, -110) > 0
    assert T.teaser_ev(probs, -130) < 0
    assert -125 < T.max_price(probs) < -115


def test_stacking_multiplies_the_sign_of_the_edge():
    """
    The asymmetry that makes teasers different from parlays.

    Stacking -EV legs multiplies the hold and gets worse with every leg. But
    these legs clear break-even, so stacking multiplies an edge ABOVE 1.0 and
    gets better -- provided the offered price keeps up with the added leg.
    A 3-team teaser at the common +160 beats a 2-teamer at -120; the same 3
    legs at +150 are already dead.
    """
    r, _ = T.combined_rate(T.BANDS)
    assert T.teaser_ev([r, r, r], 160) > T.teaser_ev([r, r], -120)
    assert T.teaser_ev([r, r, r], 150) < 0
    assert T.max_price([r, r, r]) == 151


def test_stacking_non_qualifying_legs_still_decays():
    """The parlay result, kept as a contrast: bad legs compound downward."""
    bad = 0.68                              # a -6.0 favourite, buys nothing
    assert T.teaser_ev([bad, bad], -120) < T.teaser_ev([bad], -120)
    assert T.teaser_ev([bad, bad, bad], 160) < 0


def test_one_leg_per_game_per_book():
    b = T.band_for(-7.0)
    legs = [T.Leg("A @ B", "", "B", -7.0, b, "DK"),
            T.Leg("A @ B", "", "A", 2.0, T.band_for(2.0), "DK"),
            T.Leg("C @ D", "", "D", -7.0, b, "DK")]
    out = T.by_book(legs)["DK"]
    assert len(out) == 2, "both sides of one game cannot be teased together"
    assert {l.game for l in out} == {"A @ B", "C @ D"}


def test_books_are_kept_separate():
    b = T.band_for(-7.0)
    legs = [T.Leg("A @ B", "", "B", -7.0, b, "DK"),
            T.Leg("C @ D", "", "D", -7.0, b, "FD")]
    out = T.by_book(legs)
    assert set(out) == {"DK", "FD"}
    assert all(len(v) == 1 for v in out.values())


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{fails} failure(s)")
    raise SystemExit(1 if fails else 0)
