"""
NFL winners from team metrics. No Elo, and the betting line is never an input.

The market appears only as a scorecard beside each pick, so the model's record
can be read against the number it has to beat.

INPUTS, known before each game and rebuilt game by game so nothing leaks:

    margin    recency-weighted scoring margin (half-life 8 games)
    qb        the listed starter's own EPA per dropback, shrunk over 150
    home      home field; zero at a neutral site
    injuries  snap share of non-QB players on the final injury report

Weights are fitted on past seasons only. `--backtest` predicts each season from
the ones before it. The historical QB is who actually started, which a real
midweek pick does not always know, so the backtest flatters slightly.

    python nfl_model.py --backtest
    python nfl_model.py --picks 8
"""

from __future__ import annotations

import argparse
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache", "nfl_model")
RELEASE = "https://github.com/nflverse/nflverse-data/releases/download"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

FIRST_SEASON = 2016     # first data season; 2017 onward can train
FIRST_TEST = 2019
HALF_LIFE = 8.0         # games
OFFSEASON_GAMES = 6.0   # the summer decays a rating like this many games
QB_PRIOR = -0.02        # EPA per dropback for a QB with no record
QB_PRIOR_WEIGHT = 150.0 # dropbacks
QB_HALF_LIFE = 300.0    # dropbacks
SNAP_WINDOW = 8         # games a player's snap share is averaged over
RIDGE = 5.0

# How much of a player's snap share a report status takes off the field. Set
# before testing and not tuned: most Questionable players do play.
STATUS_WEIGHT = {"Out": 1.0, "Doubtful": 1.0, "Questionable": 0.2}

GROUPS = {
    "T": "ol", "G": "ol", "C": "ol", "OL": "ol", "OT": "ol", "OG": "ol",
    "WR": "wr", "TE": "te", "RB": "rb", "FB": "rb",
    "DE": "dl", "DT": "dl", "NT": "dl", "DL": "dl",
    "LB": "lb", "ILB": "lb", "OLB": "lb", "MLB": "lb",
    "CB": "db", "S": "db", "FS": "db", "SS": "db", "DB": "db",
}
OFFENSE_GROUPS = ("ol", "wr", "te", "rb")
DEFENSE_GROUPS = ("dl", "lb", "db")
ALL_GROUPS = OFFENSE_GROUPS + DEFENSE_GROUPS

CORE = ["margin", "qb"]
INJURY_TOTALS = ["inj_off", "inj_def"]
INJURY_GROUPS = [f"inj_{g}" for g in ALL_GROUPS]
PICK_INPUTS = CORE + INJURY_TOTALS

PBP_COLS = ["game_id", "posteam", "qb_dropback", "qb_epa", "passer_player_id",
            "rusher_player_id", "play_type"]


def _fetch(url: str, name: str, refresh: bool = False) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and not refresh:
        return path
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return path


def is_current(season: int) -> bool:
    """Still being played. A season's playoffs run into the next January."""
    now = pd.Timestamp.now()
    return season >= (now.year if now.month > 2 else now.year - 1)


def load_games(refresh: bool = True) -> pd.DataFrame:
    g = pd.read_csv(_fetch(GAMES_URL, "games.csv", refresh=refresh), low_memory=False)
    g = g[g.season >= FIRST_SEASON].copy()
    g["date"] = pd.to_datetime(g.gameday)
    return g.sort_values(["date", "gametime", "game_id"]).reset_index(drop=True)


def load_qb_games(seasons: Iterable[int]) -> pd.DataFrame:
    """Dropbacks and summed QB EPA per passer per game, cached per season."""
    out = []
    os.makedirs(CACHE, exist_ok=True)
    for y in seasons:
        slim = os.path.join(CACHE, f"qb_{y}.parquet")
        if os.path.exists(slim) and not is_current(y):
            out.append(pd.read_parquet(slim))
            continue
        path = _fetch(f"{RELEASE}/pbp/play_by_play_{y}.csv.gz", f"pbp_{y}.csv.gz",
                      refresh=is_current(y))
        d = pd.read_csv(path, usecols=lambda c: c in PBP_COLS, low_memory=False)
        d = d[(d.qb_dropback == 1) & d.qb_epa.notna() & d.play_type.isin(["pass", "run"])]
        d = d.assign(qb=d.passer_player_id.fillna(d.rusher_player_id))
        q = (d[d.qb.notna()].groupby(["game_id", "qb"])
             .agg(epa=("qb_epa", "sum"), dropbacks=("qb_epa", "size")).reset_index())
        q.to_parquet(slim)
        out.append(q)
    return pd.concat(out, ignore_index=True)


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z ]", "", str(name).lower())
    return re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n).replace(" ", "")


TEAM_ALIAS = {"GNB": "GB", "KAN": "KC", "LAR": "LA", "LVR": "LV", "NOR": "NO",
              "NWE": "NE", "SFO": "SF", "TAM": "TB", "OAK": "LV", "RAI": "LV",
              "RAM": "LA", "SDG": "LAC", "SD": "LAC", "STL": "LA"}


def _team(t) -> str:
    return TEAM_ALIAS.get(t, t)


def load_snaps(seasons: Iterable[int]) -> pd.DataFrame:
    frames = []
    for y in seasons:
        p = _fetch(f"{RELEASE}/snap_counts/snap_counts_{y}.csv", f"snaps_{y}.csv",
                   refresh=is_current(y))
        d = pd.read_csv(p, low_memory=False)
        d["share"] = d[["offense_pct", "defense_pct"]].max(axis=1)
        frames.append(d[["game_id", "season", "week", "player", "team", "share"]])
    s = pd.concat(frames, ignore_index=True)
    s["team"] = s.team.map(_team)
    s["key"] = s.player.map(_norm)
    return s


def load_injuries(seasons: Iterable[int]) -> pd.DataFrame:
    frames = []
    for y in seasons:
        p = _fetch(f"{RELEASE}/injuries/injuries_{y}.csv", f"injuries_{y}.csv",
                   refresh=is_current(y))
        d = pd.read_csv(p, low_memory=False)
        frames.append(d[["season", "week", "team", "full_name", "position", "report_status"]])
    i = pd.concat(frames, ignore_index=True)
    i["team"] = i.team.map(_team)
    i["key"] = i.full_name.map(_norm)
    i["weight"] = i.report_status.map(STATUS_WEIGHT)
    i["group"] = i.position.map(GROUPS)   # QBs and specialists map to nothing
    return i[i.weight.notna() & i.group.notna()]


def injury_features(games: pd.DataFrame, snaps: pd.DataFrame,
                    injuries: pd.DataFrame) -> Dict[Tuple[str, str], Dict[str, float]]:
    """
    (game_id, team) -> snap share missing per position group.

    A player's share is the mean of his last SNAP_WINDOW games BEFORE this one,
    so a game can never see its own snaps.
    """
    order = {g: i for i, g in enumerate(games.game_id)}
    snaps = snaps.assign(o=snaps.game_id.map(order)).dropna(subset=["o"])
    hist: Dict[Tuple[str, str], List[Tuple[int, float]]] = {}
    for r in snaps.sort_values("o").itertuples():
        hist.setdefault((r.team, r.key), []).append((int(r.o), float(r.share)))

    by_week: Dict[Tuple[int, int, str], list] = {}
    for r in injuries.itertuples():
        by_week.setdefault((int(r.season), int(r.week), r.team), []).append(r)

    out = {}
    for g in games.itertuples():
        for team in (g.home_team, g.away_team):
            row = {x: 0.0 for x in ALL_GROUPS}
            at = order[g.game_id]
            for inj in by_week.get((int(g.season), int(g.week), _team(team)), []):
                past = [s for o, s in hist.get((_team(team), inj.key), []) if o < at]
                if past:
                    row[inj.group] += inj.weight * float(np.mean(past[-SNAP_WINDOW:]))
            out[(g.game_id, team)] = row
    return out


class _Decayed:
    """A weighted mean whose old values fade by half every HALF_LIFE updates."""

    def __init__(self):
        self.v, self.w = 0.0, 0.0

    def value(self) -> float:
        return (self.v * self.w) / (self.w + 2.0)  # two games of zero as a prior

    def add(self, x: float):
        d = 0.5 ** (1 / HALF_LIFE)
        w = self.w * d
        self.v = (self.v * w + x) / (w + 1)
        self.w = w + 1

    def offseason(self):
        self.w *= 0.5 ** (OFFSEASON_GAMES / HALF_LIFE)


def build_features(games: pd.DataFrame, qb_games: pd.DataFrame,
                   inj: Optional[dict] = None) -> pd.DataFrame:
    """One row per game with home-minus-away inputs, using only earlier games."""
    margin: Dict[str, _Decayed] = {}
    qb: Dict[str, Tuple[float, float]] = {}
    season_of: Dict[str, int] = {}
    by_game = {g: x for g, x in qb_games.groupby("game_id")}
    inj = inj or {}

    def qb_rating(qid) -> float:
        v, w = qb.get(qid, (0.0, 0.0))
        return (v * w + QB_PRIOR * QB_PRIOR_WEIGHT) / (w + QB_PRIOR_WEIGHT)

    rows = []
    for g in games.itertuples():
        h, a = g.home_team, g.away_team
        for t in (h, a):
            m = margin.setdefault(t, _Decayed())
            if season_of.get(t) != g.season:
                m.offseason()
                season_of[t] = g.season
        f = {"game_id": g.game_id, "season": g.season, "week": g.week,
             "gameday": g.gameday, "gametime": g.gametime, "home": h, "away": a,
             "home_qb": g.home_qb_name, "away_qb": g.away_qb_name,
             "result": g.result, "ml_h": g.home_moneyline, "ml_a": g.away_moneyline,
             "spread": g.spread_line,
             "homefld": 0.0 if g.location == "Neutral" else 1.0,
             "margin": margin[h].value() - margin[a].value(),
             "qb": qb_rating(g.home_qb_id) - qb_rating(g.away_qb_id)}
        hi, ai = inj.get((g.game_id, h), {}), inj.get((g.game_id, a), {})
        for x in ALL_GROUPS:
            f[f"inj_{x}"] = hi.get(x, 0.0) - ai.get(x, 0.0)
        f["inj_off"] = sum(f[f"inj_{x}"] for x in OFFENSE_GROUPS)
        f["inj_def"] = sum(f[f"inj_{x}"] for x in DEFENSE_GROUPS)
        rows.append(f)
        if pd.isna(g.result):
            continue
        margin[h].add(float(g.result))
        margin[a].add(-float(g.result))
        x = by_game.get(g.game_id)
        if x is not None:
            for q in x.itertuples():
                v, w = qb.get(q.qb, (0.0, 0.0))
                w2 = w * 0.5 ** (q.dropbacks / QB_HALF_LIFE)
                qb[q.qb] = ((v * w2 + q.epa) / (w2 + q.dropbacks), w2 + q.dropbacks)
    F = pd.DataFrame(rows)
    F["market"] = market_prob(F.ml_h, F.ml_a)
    return F


def market_prob(ml_h, ml_a) -> np.ndarray:
    """De-vigged home win probability. Scorecard only, never a model input."""
    def imp(m):
        m = np.asarray(m, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(m < 0, -m / (100 - m), 100 / (m + 100))
    h, a = imp(ml_h), imp(ml_a)
    return h / (h + a)


def logit(X: np.ndarray, y: np.ndarray, ridge: float = RIDGE) -> Tuple[np.ndarray, np.ndarray]:
    """Ridge logistic by Newton. Column 0 is not penalised. Returns (b, Hessian)."""
    b = np.zeros(X.shape[1])
    P = np.eye(X.shape[1]) * ridge
    P[0, 0] = 0
    for _ in range(30):
        p = 1 / (1 + np.exp(-X @ b))
        w = p * (1 - p) + 1e-9
        H = X.T @ (X * w[:, None]) + P
        b = b + np.linalg.solve(H, X.T @ (y - p) - P @ b)
    return b, H


def _design(d: pd.DataFrame, cols, mu, sd) -> np.ndarray:
    return np.c_[d.homefld.values, ((d[cols] - mu) / sd).fillna(0).values]


def walk_forward(F: pd.DataFrame, cols: List[str], start: int = FIRST_TEST) -> pd.Series:
    """Home win probability for each finished game, fitted on earlier seasons."""
    out = pd.Series(index=F.index, dtype=float)
    played = F[F.result.notna() & (F.result != 0) & (F.season > FIRST_SEASON)]
    for s in sorted(x for x in F.season.unique() if x >= start):
        tr, te = played[played.season < s], F[(F.season == s) & F.result.notna()]
        if te.empty:
            continue
        mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)
        b, _ = logit(_design(tr, cols, mu, sd), (tr.result > 0).astype(float).values)
        out.loc[te.index] = 1 / (1 + np.exp(-_design(te, cols, mu, sd) @ b))
    return out


def _score(T: pd.DataFrame, p: pd.Series) -> Tuple[float, float]:
    y = (T.result > 0).values
    q = p.loc[T.index].values
    return float(((q > .5) == y).mean()), float(((q - y) ** 2).mean())


def _pay(ml: float, won: bool) -> float:
    return (ml / 100 if ml > 0 else 100 / -ml) if won else -1.0


def backtest(F: pd.DataFrame, start: int = FIRST_TEST):
    T = F[(F.season >= start) & F.result.notna() & (F.result != 0)]
    se = 100 * math.sqrt(.25 / len(T))
    print(f"{len(T)} games, {start}-{int(T.season.max())}. Winner % margin of error ~±{se:.1f}\n")
    a, b = _score(T, pd.Series(F.market, index=F.index).fillna(.5))
    print(f"  {'market favorite (scorecard)':40s} {a*100:5.1f}%  Brier {b:.4f}")
    sets = {"margin + QB + home (core)": CORE,
            "core + injuries, off/def": CORE + INJURY_TOTALS,
            "core + injuries, by position": CORE + INJURY_GROUPS}
    preds = {}
    for name, cols in sets.items():
        preds[name] = walk_forward(F, cols, start)
        a, b = _score(T, preds[name])
        print(f"  {name:40s} {a*100:5.1f}%  Brier {b:.4f}")

    print("\nInjury gap alone, plus home:")
    for c in INJURY_TOTALS + INJURY_GROUPS:
        a, b = _score(T, walk_forward(F, [c], start))
        print(f"  {c:40s} {a*100:5.1f}%  Brier {b:.4f}")

    tr = F[F.result.notna() & (F.result != 0) & (F.season > FIRST_SEASON)]
    cols = CORE + INJURY_GROUPS
    mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)
    b, H = logit(_design(tr, cols, mu, sd), (tr.result > 0).astype(float).values)
    se_b = np.sqrt(np.diag(np.linalg.inv(H)))
    print("\nFitted weights (per standard deviation; injuries should come out negative):")
    for n, x, s in zip(["home"] + cols, b, se_b):
        print(f"  {n:12s} {x:+.3f}  t={x/s:+.1f}")

    # Does the injury gap say anything the closing line does not?
    S = T[T.spread.notna()].copy()
    S["hurt"] = S.inj_off + S.inj_def
    S["resid"] = S.result - S.spread
    X = np.c_[np.ones(len(S)), S.inj_off, S.inj_def]
    beta, *_ = np.linalg.lstsq(X, S.resid, rcond=None)
    e = S.resid - X @ beta
    cov = (e @ e / (len(S) - 3)) * np.linalg.inv(X.T @ X)
    print("\nMargin minus closing spread, on the injury gap (points per unit of snap share):")
    for n, x, s in zip(["inj_off", "inj_def"], beta[1:], np.sqrt(np.diag(cov))[1:]):
        print(f"  {n:12s} {x:+.2f}  t={x/s:+.1f}")
    print("\nSide with the healthier team, at the closing spread:")
    for lo in (0.5, 1.0, 1.5):
        x = S[(S.hurt.abs() >= lo) & (S.resid != 0)]
        healthy_home = x.hurt < 0
        cover = (healthy_home == (x.resid > 0)).mean()
        print(f"  gap >= {lo:.1f} starters: {len(x):4d} games, covers {cover*100:.1f}% "
              f"±{100*math.sqrt(.25/max(len(x),1)):.1f}  (52.4% breaks even)")

    # A healthier-team bet does not hold up: it is flat on 2013-2018, the seasons
    # this window never saw (gap >= 1 starter: 50.1% of 615; t=-0.1 on the margin).
    best = "core + injuries, off/def"
    p = preds[best].loc[T.index]
    M = T[T.market.notna()].copy()
    M["p"] = p.loc[M.index]
    M["hw"] = M.result > 0
    M["ret"] = [_pay(r.ml_h if r.p > .5 else r.ml_a, r.hw == (r.p > .5)) for r in M.itertuples()]
    dis = M[(M.p > .5) != (M.market > .5)]
    agr = M[(M.p > .5) == (M.market > .5)]
    print(f"\n{best}, flat moneyline:")
    print(f"  agrees with favorite   {len(agr):4d} games, wins {((agr.p>.5)==agr.hw).mean()*100:.1f}%, "
          f"return {agr.ret.mean()*100:+.1f}%")
    print(f"  disagrees              {len(dis):4d} games, wins {((dis.p>.5)==dis.hw).mean()*100:.1f}%, "
          f"return {dis.ret.mean()*100:+.1f}%")
    lg = lambda v: np.log(np.clip(v, .01, .99) / (1 - np.clip(v, .01, .99)))
    X = np.c_[np.ones(len(M)), lg(M.market.values), lg(M.p.values)]
    b, H = logit(X, M.hw.astype(float).values, ridge=0.0)
    s = np.sqrt(np.diag(np.linalg.inv(H)))
    print(f"  win ~ market + model: market t={b[1]/s[1]:.1f}, model t={b[2]/s[2]:.1f}")


def picks(F: pd.DataFrame, week: int, season: Optional[int] = None,
          cols: Optional[List[str]] = None) -> pd.DataFrame:
    """Fit on every finished game and pick one week."""
    cols = cols or PICK_INPUTS
    season = season or int(F.season.max())
    tr = F[F.result.notna() & (F.result != 0) & (F.season > FIRST_SEASON)]
    mu, sd = tr[cols].mean(), tr[cols].std().replace(0, 1)
    b, _ = logit(_design(tr, cols, mu, sd), (tr.result > 0).astype(float).values)
    U = F[(F.season == season) & (F.week == week)].copy()
    U["p_home"] = 1 / (1 + np.exp(-_design(U, cols, mu, sd) @ b))
    U["pick"] = np.where(U.p_home > .5, U.home, U.away)
    U["win_pct"] = np.where(U.p_home > .5, U.p_home, 1 - U.p_home)
    U["market_pct"] = np.where(U.p_home > .5, U.market, 1 - U.market)
    return U


def load_all(refresh_games: bool = True) -> pd.DataFrame:
    games = load_games(refresh=refresh_games)
    played = sorted(games[games.result.notna()].season.unique())
    inj = injury_features(games, load_snaps(played), load_injuries(played))
    return build_features(games, load_qb_games(played), inj)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--picks", type=int, metavar="WEEK")
    a = ap.parse_args()
    F = load_all()
    if a.backtest:
        backtest(F)
    if a.picks:
        U = picks(F, a.picks)
        for r in U.itertuples():
            print(f"{r.gameday} {r.away:>3} @ {r.home:<3}  {r.pick:<3} {r.win_pct*100:3.0f}%"
                  f"   market {r.market_pct*100:3.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
