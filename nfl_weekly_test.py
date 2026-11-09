"""Offline checks on nfl_weekly: no network, no push."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

import nfl_weekly as W

MU = pd.Series({"margin": 0.0, "qb": 0.0, "inj_off": 0.0, "inj_def": 0.0})
SD = pd.Series({"margin": 1.0, "qb": 0.1, "inj_off": 1.0, "inj_def": 1.0})
B = np.array([0.1, 0.05, 1.0, -0.1, -0.1])


def _row(**kw):
    base = dict(home="KC", away="NYJ", pick="KC", home_qb="Patrick Mahomes",
                away_qb="Justin Fields", homefld=1.0, margin=7.2, qb=0.09,
                inj_off=0.0, inj_def=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_reason_names_the_biggest_push_first():
    assert W.reasons(_row(), B, MU, SD).startswith("Mahomes over Fields at QB (0.09")


def test_reason_reads_from_the_away_pick():
    r = _row(pick="NYJ", margin=-7.2, qb=-0.09)
    assert W.reasons(r, B, MU, SD).startswith("Fields over Mahomes"), W.reasons(r, B, MU, SD)
    assert "despite being on the road" not in W.reasons(r, B, MU, SD)   # 0.1 < AGAINST_MIN


def test_reason_says_despite_when_a_factor_pulls_hard():
    r = _row(inj_def=3.0)        # home missing three starters on defense
    assert "(despite KC missing 3.0 starters' worth of defensive snaps)" in W.reasons(r, B, MU, SD)


def test_suffix_is_not_a_surname():
    assert W._qb_last("Michael Penix Jr.") == "Penix"
    assert W._qb_last(float("nan")) == "?"


def _pick(gid, pick, mkt, gameday="2026-11-15", week=10):
    return {"season": 2026, "week": week, "game_id": gid, "gameday": gameday,
            "away": "NYJ", "home": "KC", "pick": pick, "win_pct": 0.7,
            "market_pct": mkt, "reason": "r", "made_at": "x"}


def _games(results):
    return pd.DataFrame([dict(game_id=g, result=r,
                              home_score=None if r != r else 20 + max(r, 0),
                              away_score=None if r != r else 20 + max(-r, 0))
                         for g, r in results])


def test_grade_marks_wins_ties_and_the_favorite():
    rows = [_pick("a", "KC", 0.8), _pick("b", "NYJ", 0.4), _pick("c", "KC", 0.6),
            _pick("d", "KC", 0.6)]
    g = W.grade(rows, _games([("a", 7.0), ("b", 3.0), ("c", 0.0), ("d", float("nan"))]))
    assert g[0]["won"] is True and g[0]["fav_won"] is True
    assert g[1]["won"] is False and g[1]["fav_won"] is True     # NYJ underdog, KC won
    assert g[2]["won"] is None and "fav_won" not in g[2]
    assert g[3]["result"] is None
    text = W.results_text(g, g)
    assert text.startswith("NFL RESULTS Week 10: 1-1-1"), text
    assert "Picked the underdog: 0-1" in text
    assert "not final yet" in text


def test_picks_text_flags_underdog_picks():
    t = W.picks_text([_pick("a", "NYJ", 0.45), _pick("b", "KC", None)])
    assert "(mkt 45%, underdog)" in t and "(mkt n/a)" in t
    assert t.startswith("NFL PICKS Week 10")


def test_upcoming_week_is_the_next_seven_days():
    F = pd.DataFrame([dict(season=2026, week=9, gameday="2026-11-08", result=3.0),
                      dict(season=2026, week=10, gameday="2026-11-12", result=np.nan),
                      dict(season=2026, week=11, gameday="2026-11-19", result=np.nan)])
    assert W.upcoming_week(F, date(2026, 11, 12)) == (2026, 10)
    assert W.upcoming_week(F, date(2026, 11, 20)) is None


def test_results_wait_for_last_scores_then_send_once(monkey=None):
    d = tempfile.mkdtemp()
    old = (W.LOG, W.SENT, W._push)
    sent = []
    try:
        W.LOG, W.SENT = os.path.join(d, "log.ndjson"), os.path.join(d, "sent.json")
        W._push = lambda text: sent.append(text) or True
        import json
        with open(W.LOG, "w") as f:
            for r in (_pick("a", "KC", 0.8, "2026-11-15"), _pick("b", "KC", 0.8, "2026-11-16")):
                f.write(json.dumps(r) + "\n")
        import nfl_model as M
        real = M.load_games
        M.load_games = lambda refresh=False: _games([("a", 7.0), ("b", float("nan"))])
        try:
            early = datetime(2026, 11, 17, 13, tzinfo=timezone.utc)
            W.do_results(None, early, send=True)
            assert not sent, "waits while Monday night is not final"
            late = datetime(2026, 11, 17, 19, tzinfo=timezone.utc)
            W.do_results(None, late, send=True)
            assert len(sent) == 1 and "not final yet" in sent[0]
            W.do_results(None, late, send=True)
            assert len(sent) == 1, "a week's results go out once"
        finally:
            M.load_games = real
    finally:
        W.LOG, W.SENT, W._push = old


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:
                fails += 1
                print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failure(s)")
    raise SystemExit(1 if fails else 0)
