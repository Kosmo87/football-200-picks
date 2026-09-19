"""
Tests for the top-N poll parlay. The selection rule and the bank, not the poll.

Two things here would cost real money if they broke. The first is the poll
cutoff: a poll released ON the slate already knows the results, and admitting
one turned a losing backtest into a fake 11-0 season and +$85,000. The second
is the opponent filter, which is the entire edge -- excluding only other top-4
teams instead of every ranked team is the difference between 42-4 and 36-13.
"""

import datetime
import json
import os
import tempfile

import ranked as R


def _poll(released, ranks, name="AP Top 25"):
    return {"released": datetime.date.fromisoformat(released), "name": name,
            "week": 1, "ranks": ranks}


def _game(event_id, home, away, home_id, away_id, kickoff,
          home_odds=-300, away_odds=250):
    return {"event_id": event_id, "short_name": f"{away} @ {home}", "kickoff": kickoff,
            "home": {"id": home_id, "abbr": home}, "away": {"id": away_id, "abbr": away},
            "legs": [{"team_id": home_id, "team_abbr": home, "side": "home",
                      "odds": home_odds, "implied_prob": 0.75},
                     {"team_id": away_id, "team_abbr": away, "side": "away",
                      "odds": away_odds, "implied_prob": 0.29}]}


def _soon(days=2):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")


def _board(games):
    return {"leagues": {"NCAAF": {"games": games}}}


def test_a_poll_released_on_the_slate_is_refused():
    """
    The bug that produced +$85,000. AP polls are dated the Sunday at the END
    of the weekend they judge, so 'released <= kickoff' hands the bettor a
    ranking built from the games being bet.
    """
    kick = datetime.date(2025, 9, 27)
    polls = [_poll("2025-09-21", {"1": 1}), _poll("2025-09-28", {"2": 1})]
    got = R.rankings.poll_in_effect(polls, kick)
    assert got is not None
    assert got["released"] == datetime.date(2025, 9, 21), \
        "must take the poll from BEFORE the slate, not the one that judged it"


def test_no_poll_before_the_slate_means_no_ticket():
    polls = [_poll("2025-09-28", {"1": 1})]
    assert R.rankings.poll_in_effect(polls, datetime.date(2025, 9, 27)) is None


def test_ranked_opponents_are_excluded_not_just_top_four():
    """
    The whole edge. #4 vs #9 is a real game and the old rule let it through
    as a free leg; nine of ten top-5 losses in 2025 were against ranked teams.
    """
    ko = _soon()
    board = _board([
        _game("1", "OSU", "CUPCAKE", "194", "999", ko),          # unranked foe: in
        _game("2", "UGA", "LSU", "61", "99", ko),                # #9 foe: out
        _game("3", "TEX", "BAMA", "251", "333", ko),             # #1 vs #5: out
    ])
    polls = [_poll("2025-01-01", {"194": 1, "251": 2, "61": 3, "333": 4, "99": 9})]
    t = R.build_ticket(board, depth=4, polls=polls)
    assert t is not None
    assert [l["team"] for l in t["legs"]] == ["OSU"], "only the unranked matchup qualifies"
    whys = " ".join(s["why"] for s in t["skipped"])
    assert "ranked #9" in whys and "both teams are top 4" in whys


def test_depth_is_respected():
    ko = _soon()
    board = _board([_game("1", "OSU", "CUPCAKE", "194", "999", ko),
                    _game("2", "IU", "PATSY", "84", "998", ko)])
    polls = [_poll("2025-01-01", {"194": 1, "84": 7})]
    assert len(R.build_ticket(board, depth=4, polls=polls)["legs"]) == 1
    assert len(R.build_ticket(board, depth=7, polls=polls)["legs"]) == 2


def test_an_impossible_price_is_never_bet():
    """A -1 moneyline is a data artifact; paying it 100-to-1 faked a +40%."""
    ko = _soon()
    board = _board([_game("1", "OSU", "CUPCAKE", "194", "999", ko, home_odds=-1)])
    polls = [_poll("2025-01-01", {"194": 1})]
    assert R.build_ticket(board, depth=4, polls=polls) is None


def test_futures_are_not_this_week():
    board = _board([_game("1", "OSU", "CUPCAKE", "194", "999", "2099-01-01T00:00Z")])
    polls = [_poll("2025-01-01", {"194": 1})]
    assert R.build_ticket(board, depth=4, polls=polls) is None


def test_price_is_the_product_of_the_legs():
    ko = _soon()
    board = _board([_game("1", "OSU", "CUPCAKE", "194", "999", ko, home_odds=-200),
                    _game("2", "IU", "PATSY", "84", "998", ko, home_odds=-200)])
    polls = [_poll("2025-01-01", {"194": 1, "84": 2})]
    t = R.build_ticket(board, depth=4, polls=polls)
    assert abs(t["decimal"] - 2.25) < 1e-9
    assert t["price"] == 125


def _with_ledger(rows, fn):
    original = R.PAPER
    with tempfile.TemporaryDirectory() as d:
        R.PAPER = os.path.join(d, "paper.ndjson")
        with open(R.PAPER, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        try:
            return fn()
        finally:
            R.PAPER = original


def test_bank_follows_the_stated_staking_rule():
    """
    $100 to a 1.1004 return leaves $10.04 banked and stakes $105.02 next --
    the rule as specified, with the $100 going home rather than riding.
    """
    rows = [{"slate": "2025-09-06", "depth": 4, "decimal": 1.1004, "result": "won"}]
    s = _with_ledger(rows, R.bank_state)
    assert abs(s["bank"] - 10.04) < 0.01
    assert abs(s["next_stake"] - 105.02) < 0.01
    assert s["net"] > 0 and s["pocket"] == 0


def test_a_loss_costs_the_hundred_and_the_half_bank():
    rows = [{"slate": "2025-09-06", "depth": 4, "decimal": 1.1004, "result": "won"},
            {"slate": "2025-09-13", "depth": 4, "decimal": 2.0, "result": "lost"}]
    s = _with_ledger(rows, R.bank_state)
    assert abs(s["bank"] - 5.02) < 0.01, "half the bank rode and is gone"
    assert s["pocket"] == 100.0
    assert abs(s["net"] - -94.98) < 0.01


def test_pending_tickets_do_not_move_the_bank():
    rows = [{"slate": "2025-09-06", "depth": 4, "decimal": 5.0, "result": "pending"}]
    s = _with_ledger(rows, R.bank_state)
    assert s["settled"] == 0 and s["bank"] == 0 and s["next_stake"] == 100.0


def test_one_ticket_per_slate():
    t = {"slate": "2025-09-06", "depth": 4, "decimal": 2.0, "legs": [], "price": 100}
    def run():
        first = R.log_ticket(t)
        second = R.log_ticket(t)
        return first, second, len(R.paper_rows())
    first, second, n = _with_ledger([], run)
    assert "logged" in first and second == "already logged"
    assert n == 1, "re-running the weekly job must not double the ledger"


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
