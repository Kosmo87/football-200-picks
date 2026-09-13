"""
Tests for the book capability table.

The failure this guards against already happened: the scanner recommended a
6-point teaser at BetMGM, a book whose slip offers Singles and Parlays only.
Nothing in the odds feed could have caught it, so it is caught here.
"""

import books as B


def test_known_absent_product_is_refused():
    ok, why = B.teaser_playable("BetMGM", 2, -121)
    assert ok is False and "does not offer teasers" in why


def test_feed_labels_match_entries():
    # the feed says "BetMGM"/"FanDuel"; the table is keyed lowercase
    for label in ("BetMGM", "betmgm", "FanDuel", "fanduel", "LowVig.ag"):
        assert B.get(label) is not None, label


def test_known_bad_price_is_refused():
    ok, why = B.teaser_playable("FanDuel", 2, -121)
    assert ok is False and "-134" in why


def test_worse_is_more_negative():
    """-134 must read as worse than -121, not better."""
    assert B.teaser_playable("FanDuel", 2, -121)[0] is False
    assert B.teaser_playable("FanDuel", 2, -140)[0] is True  # -134 clears -140


def test_unverified_leg_count_stays_eligible():
    """FanDuel's 3-leg price was never checked — do not refuse it silently."""
    ok, why = B.teaser_playable("FanDuel", 3, 147)
    assert ok is True and "unverified" in why


def test_unknown_book_is_not_blocked():
    ok, why = B.teaser_playable("SomeNewBook", 2, -121)
    assert ok is True and "not in the table" in why


def test_offers_teasers_defaults_open():
    assert B.offers_teasers("DraftKings") is True     # None = unchecked
    assert B.offers_teasers("BetMGM") is False        # checked and absent
    assert B.offers_teasers("nonsense") is True


def test_mine_resolves():
    names = {b.key for b in B.mine()}
    assert names == set(B.MINE)


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
