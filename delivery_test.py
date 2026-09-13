"""
Tests for the message.

The bug these guard against shipped: a seven-leg ladder with six FanDuel
legs and one BetMGM leg, emailed as a recommendation. A parlay is built
inside one book, so that ticket could not be placed anywhere.
"""

from datetime import datetime, timezone, timedelta
import delivery as D


def _leg(book, game, chance, price=-200):
    return {"book": book, "game": game, "side": game.split(" @ ")[0],
            "chance": chance, "price": price,
            "needs": 1 / (1 + 100 / abs(price)), "gap": 0.0,
            "kickoff": datetime.now(timezone.utc) + timedelta(days=1),
            "market": "h2h"}


def test_mixed_books_are_refused_outright():
    rows = [_leg("fanduel", "A @ B", .9), _leg("betmgm", "C @ D", .9)]
    try:
        D.parlay_ladder(rows)
    except ValueError as e:
        assert "2 books" in str(e)
        return
    raise AssertionError("a mixed-book ladder must raise, not build a ticket")


def test_ladders_are_built_per_book():
    rows = [_leg("fanduel", "A @ B", .95), _leg("fanduel", "C @ D", .92),
            _leg("betmgm", "E @ F", .93), _leg("betmgm", "G @ H", .91)]
    lads = D.ladders_by_book(rows)
    assert set(lads) == {"fanduel", "betmgm"}
    for book, lad in lads.items():
        assert {l["book"] for l in lad["legs"]} == {book}


def test_a_book_with_one_leg_gets_no_parlay():
    rows = [_leg("fanduel", "A @ B", .95), _leg("fanduel", "C @ D", .92),
            _leg("betmgm", "E @ F", .93)]
    lads = D.ladders_by_book(rows)
    assert "betmgm" not in lads, "one leg is not a parlay"


def test_ladder_respects_the_floor():
    rows = [_leg("fanduel", f"{c} @ X", .9) for c in "ABCDEFGH"]
    lad = D.ladders_by_book(rows, floor=0.60)["fanduel"]
    assert lad["chance"] >= 0.60
    # adding one more 0.9 leg would drop below the floor
    assert lad["chance"] * 0.9 < 0.60 or len(lad["legs"]) == 8


def test_one_leg_per_game():
    rows = [_leg("fanduel", "A @ B", .95), _leg("fanduel", "A @ B", .93),
            _leg("fanduel", "C @ D", .92)]
    lad = D.ladders_by_book(rows)["fanduel"]
    assert len({l["game"] for l in lad["legs"]}) == len(lad["legs"])


def test_by_book_sorts_by_chance():
    rows = [_leg("fanduel", "A @ B", .80), _leg("fanduel", "C @ D", .95)]
    assert [r["chance"] for r in D.by_book(rows)["fanduel"]] == [.95, .80]


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
