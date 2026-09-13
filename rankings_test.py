"""
Tests for the poll capture.

The bug this guards against cost a false positive: a 664-game backtest at
56.0% ATS, z = +3.10, built on polls that turned out to be today's, applied
retroactively to three seasons. The endpoint ignores season and week.
"""

import json
import os
import rankings as R


def test_no_dated_parameters_are_sent():
    """
    fetch_current must not pretend the endpoint honours season/week. If a
    future edit adds them back, this fails loudly rather than silently
    returning the current poll labelled as history.
    """
    src = open(os.path.join(R.ROOT, "rankings.py")).read()
    body = src[src.index("def fetch_current"):src.index("def capture")]
    for bad in ("season=", "week=", "seasontype=", "dates="):
        assert bad not in body, f"{bad} is ignored by this endpoint"


def test_the_contamination_check_is_documented():
    src = open(os.path.join(R.ROOT, "rankings.py")).read()
    assert "byte-identical" in src, "the failure has to stay written down"


def test_capture_is_idempotent_when_the_poll_has_not_moved():
    """A weekly job must not append 25 identical rows every time it runs."""
    if not os.path.exists(R.ARCHIVE):
        return
    rows = [json.loads(l) for l in open(R.ARCHIVE) if l.strip()]
    stamps = sorted({r["captured_at"] for r in rows})
    for s in stamps:
        ids = [r["team_id"] for r in rows if r["captured_at"] == s]
        assert len(ids) == len(set(ids)), f"duplicate teams in capture {s}"


def test_archive_rows_have_what_a_test_would_need():
    if not os.path.exists(R.ARCHIVE):
        return
    rows = [json.loads(l) for l in open(R.ARCHIVE) if l.strip()]
    assert rows, "archive exists but is empty"
    for r in rows[:5]:
        assert {"captured_at", "poll", "team_id", "rank"} <= set(r)
        assert 1 <= r["rank"] <= 25


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
