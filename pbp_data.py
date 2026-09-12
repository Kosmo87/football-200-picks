"""
NFL play-by-play, reduced to the handful of columns a matchup model needs.

nflverse publishes every play of every game back to 1999 as one gzipped CSV per
season: free, keyless, about 19 MB and 372 columns. Almost all of that is
irrelevant here, and parsing it repeatedly is slow, so each season is reduced
once to a small parquet of per-play EPA with the context needed to split it --
who had the ball, who was defending, pass or run, which week.

The full files are cached but not committed (see .gitignore): they are
re-downloadable from a public URL at any time, which is the same rule the other
derived caches follow.

WHY EPA. Elo is built from final scores, so it knows that a team won by ten and
nothing about how. Expected points added is per-play and already denominated in
points, which makes it both a finer measurement and one that converts back to a
margin without an arbitrary scaling constant.

  python pbp_data.py --seasons 2016 2025     # fetch and reduce
"""

from __future__ import annotations

import argparse
import os
from typing import Iterable, List, Optional

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{}.csv.gz"

# Everything the matchup features are built from, and nothing else.
KEEP = [
    "game_id", "season", "week", "season_type",
    "posteam", "defteam", "home_team", "away_team",
    "play_type", "epa", "success", "yards_gained",
    "down", "ydstogo", "shotgun", "no_huddle",
    "pass_location", "run_location", "air_yards",
]


def raw_path(season: int) -> str:
    return os.path.join(CACHE, f"pbp_{season}.csv.gz")


def slim_path(season: int) -> str:
    return os.path.join(CACHE, f"pbp_slim_{season}.parquet")


def download(season: int, refresh: bool = False) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = raw_path(season)
    if os.path.exists(path) and not refresh:
        return path
    r = requests.get(URL.format(season), timeout=600)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def reduce_season(season: int, refresh: bool = False) -> pd.DataFrame:
    """One season, reduced to scoring plays with EPA. Cached as parquet."""
    slim = slim_path(season)
    if os.path.exists(slim) and not refresh:
        return pd.read_parquet(slim)

    df = pd.read_csv(download(season, refresh), compression="gzip",
                     usecols=lambda c: c in KEEP, low_memory=False)
    # Only plays a team ran on purpose: kneels, spikes, kicks and penalties
    # carry EPA that says nothing about how a team plays.
    df = df[df["play_type"].isin(["pass", "run"])]
    df = df[df["epa"].notna() & df["posteam"].notna() & df["defteam"].notna()]
    # Regular season only. Preseason is not the same team and the playoffs are
    # a biased sample of good ones.
    df = df[df["season_type"] == "REG"]
    os.makedirs(CACHE, exist_ok=True)
    df.to_parquet(slim, index=False)
    return df


def load(seasons: Iterable[int], refresh: bool = False) -> pd.DataFrame:
    frames = []
    for s in seasons:
        try:
            frames.append(reduce_season(s, refresh))
        except Exception as e:
            print(f"[pbp] {s} unavailable: {e}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", nargs=2, type=int, default=[2016, 2025],
                    metavar=("FIRST", "LAST"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    years = list(range(args.seasons[0], args.seasons[1] + 1))
    df = load(years, args.refresh)
    if df.empty:
        print("nothing loaded")
        return 1
    print(f"  {len(df):,} plays, {df.season.min()}-{df.season.max()}")
    print(f"  {df.game_id.nunique():,} games, {df.posteam.nunique()} teams")
    by = df.groupby("play_type").epa.agg(["count", "mean"])
    for pt, row in by.iterrows():
        print(f"    {pt:5s} {int(row['count']):>8,} plays  {row['mean']:+.4f} EPA/play")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
