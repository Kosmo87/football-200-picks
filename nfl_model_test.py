"""Offline checks on nfl_model: nothing here touches the network."""

from __future__ import annotations

import numpy as np
import pandas as pd

import nfl_model as M


def _games():
    rows = []
    for i, (h, a, res) in enumerate([("A", "B", 10.0), ("B", "A", -7.0), ("A", "C", 3.0),
                                     ("C", "B", 14.0)]):
        rows.append(dict(game_id=f"g{i}", season=2020, week=i + 1, gameday=f"2020-09-{i+10}",
                         gametime="13:00", home_team=h, away_team=a, result=res,
                         home_moneyline=-150, away_moneyline=130, spread_line=3.0,
                         location="Home", home_qb_id=f"q{h}", away_qb_id=f"q{a}",
                         home_qb_name=f"Q{h}", away_qb_name=f"Q{a}"))
    return pd.DataFrame(rows)


def _qb():
    return pd.DataFrame([dict(game_id="g0", qb="qA", epa=20.0, dropbacks=40.0),
                         dict(game_id="g0", qb="qB", epa=-10.0, dropbacks=40.0)])


def test_first_game_knows_nothing_about_itself():
    f = M.build_features(_games(), _qb())
    assert f.loc[0, "margin"] == 0.0
    assert f.loc[0, "qb"] == 0.0


def test_later_results_never_change_earlier_inputs():
    g = _games()
    before = M.build_features(g, _qb())
    g.loc[3, "result"] = -40.0
    after = M.build_features(g, _qb())
    pd.testing.assert_series_equal(before.margin[:4], after.margin[:4])


def test_margin_and_qb_point_at_the_better_team():
    f = M.build_features(_games(), _qb())
    assert f.loc[1, "margin"] < 0          # game 2: B hosts the team that beat it
    assert f.loc[1, "qb"] < 0


def test_unplayed_games_do_not_update_ratings():
    g = _games()
    g.loc[2, "result"] = np.nan
    with_it = M.build_features(g, _qb())
    without_it = M.build_features(g.drop(index=2).reset_index(drop=True), _qb())
    assert with_it.loc[3, "margin"] == without_it.loc[2, "margin"]


def test_injury_share_excludes_this_game_and_weights_status():
    g = _games()
    snaps = pd.DataFrame([
        dict(game_id="g0", team="A", key="pat", share=1.0),
        dict(game_id="g1", team="A", key="pat", share=0.5),
        dict(game_id="g2", team="A", key="pat", share=0.0),   # his own game: unseen
    ])
    inj = pd.DataFrame([dict(season=2020, week=3, team="A", key="pat", group="wr",
                             weight=M.STATUS_WEIGHT["Out"])])
    out = M.injury_features(g, snaps, inj)
    assert abs(out[("g2", "A")]["wr"] - 0.75) < 1e-9
    assert out[("g2", "C")]["wr"] == 0.0


def test_unknown_player_adds_nothing():
    inj = pd.DataFrame([dict(season=2020, week=1, team="A", key="nobody", group="ol", weight=1.0)])
    empty = pd.DataFrame(columns=["game_id", "team", "key", "share"])
    assert M.injury_features(_games(), empty, inj)[("g0", "A")]["ol"] == 0.0


def test_names_join_across_sources():
    assert M._norm("A.J. Green Jr.") == M._norm("AJ Green")
    assert M._team("LAR") == "LA" and M._team("BUF") == "BUF"


def test_market_prob_removes_the_vig():
    assert abs(float(M.market_prob([-110], [-110])[0]) - 0.5) < 1e-9
    assert float(M.market_prob([-200], [170])[0]) > 0.6


def test_logit_learns_direction_and_walk_forward_leaves_no_future():
    rng = np.random.default_rng(1)
    n = 800
    x = rng.normal(size=n)
    F = pd.DataFrame(dict(season=np.repeat([2017, 2018, 2019, 2020], n // 4), homefld=1.0, x=x,
                          result=np.where(x + rng.normal(size=n) > 0, 3.0, -3.0)))
    p = M.walk_forward(F, ["x"], start=2019)
    assert p[F.season < 2019].isna().all()
    assert p[F.season >= 2019].notna().all()
    T = F[F.season >= 2019]
    assert ((p[T.index] > .5) == (T.result > 0)).mean() > .6
    # Changing the last season's outcomes cannot move a prediction for it.
    G = F.copy()
    G.loc[G.season == 2020, "result"] *= -1
    assert np.allclose(M.walk_forward(G, ["x"], 2019).dropna(), p.dropna())


def test_picks_take_the_likelier_side():
    F = M.build_features(_games(), _qb())
    F = pd.concat([F.assign(season=y, game_id=[f"{y}{i}" for i in F.game_id]) for y in (2017, 2018)]
                  + [F.assign(season=2019, result=np.nan)], ignore_index=True)
    U = M.picks(F, 4, season=2019, cols=M.CORE)
    assert len(U) == 1
    assert 0.5 <= float(U.win_pct.iloc[0]) <= 1.0


if __name__ == "__main__":
    bad = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_"):
            try:
                fn()
                print(f"  ok   {n}")
            except Exception as e:
                bad += 1
                print(f"  FAIL {n}: {type(e).__name__}: {e}")
    print(f"\n{bad} failure(s)")
    raise SystemExit(1 if bad else 0)
