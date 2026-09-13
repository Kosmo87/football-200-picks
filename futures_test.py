"""
Tests for the futures capture.

The de-vig here is not the one used everywhere else in this project. A game
has two sides that sum to more than 1 and you divide through. A future has 32
outcomes and no opposing side, so the normalisation runs across the whole field
-- getting that wrong silently rescales every team.
"""

import math
import futures as F


def test_implied_matches_known_prices():
    assert abs(F.implied(100) - 0.5) < 1e-9
    assert abs(F.implied(-110) - 110 / 210) < 1e-9
    assert abs(F.implied(600) - 1 / 7) < 1e-9


def test_devig_normalises_across_the_field():
    """Two books, different holds, must produce the same fair probabilities."""
    prices = {
        "A": {"tight": 100, "loose": 120},
        "B": {"tight": 100, "loose": 120},
    }
    fair = F.fair_probs(prices)
    assert abs(fair["A"] - 0.5) < 1e-9
    assert abs(fair["B"] - 0.5) < 1e-9, "a symmetric field must come out 50/50"


def test_hold_does_not_leak_into_fair():
    """A book with a 20% hold and one with none must agree after de-vigging."""
    novig = {"X": {"n": 200}, "Y": {"n": 200}, "Z": {"n": 200}}      # sums to 1.0
    juiced = {"X": {"j": 150}, "Y": {"j": 150}, "Z": {"j": 150}}     # sums to 1.2
    a, b = F.fair_probs(novig), F.fair_probs(juiced)
    for t in ("X", "Y", "Z"):
        assert abs(a[t] - b[t]) < 1e-9, "hold leaked into the estimate"
        assert abs(a[t] - 1 / 3) < 1e-9


def test_leave_one_out_excludes_the_judged_book():
    # two books only: the median cannot shrug the outlier off, so exclusion
    # has to move the number. With three books and two agreeing it would not,
    # which is the median doing its job rather than a bug.
    prices = {"A": {"b1": 100, "outlier": 300},
              "B": {"b1": 100, "outlier": -150}}
    with_it = F.fair_probs(prices)["A"]
    without = F.fair_probs(prices, exclude="outlier")["A"]
    assert with_it != without, "exclude must actually change the consensus"
    assert abs(without - 0.5) < 1e-9


def test_median_is_robust_when_books_agree():
    """Three books, two agreeing: one outlier must NOT move the consensus."""
    prices = {"A": {"b1": 100, "b2": 100, "outlier": 300},
              "B": {"b1": 100, "b2": 100, "outlier": -150}}
    assert abs(F.fair_probs(prices)["A"] - 0.5) < 1e-9


def test_empty_dict_never_hits_the_network():
    """`prices or fetch()` treated {} as falsy and silently spent a credit."""
    assert F.ratings({}) == {}


def test_ratings_are_monotone_in_probability():
    prices = {"strong": {"b": 200}, "mid": {"b": 500}, "weak": {"b": 2000}}
    r = F.ratings(prices)
    assert r["strong"] > r["mid"] > r["weak"]


def test_ratings_are_log_scaled():
    """A team 2x as likely is a fixed rating step, not 2x the rating."""
    prices = {"a": {"b": 100}, "b": {"b": 300}}      # 50% vs 25% before de-vig
    r = F.ratings(prices)
    assert abs((r["a"] - r["b"]) - math.log(2)) < 1e-9


def test_longshot_guard_constants_are_sane():
    # the Tennessee case: +25000 at one book, +12500 at another
    assert F.implied(25000) < F.MIN_TRUSTED_PROB
    assert F.MAX_TRUSTED_EDGE == 0.15


def test_empty_input_does_not_explode():
    assert F.fair_probs({}) == {}


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
