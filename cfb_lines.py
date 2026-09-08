"""
College football closing lines, 2006-2025, free and keyless.

sportsdataverse mirrors CollegeFootballData's betting archive as a single
gzipped CSV in cfbfastR-data: 1.18 million rows covering spread, total and
moneyline from dozens of books, Pinnacle among them. No API key, no quota.

One wrinkle. Each spread row names the side by an abbreviation ('UTH' for Utah)
that matches nothing in ESPN's vocabulary, while the row also carries ESPN's
home_team_id and away_team_id. Rather than fuzzy-matching names, the mapping is
solved by intersection: whichever team id appears in *every* game where an
abbreviation shows up is that abbreviation's team. That is exact, needs no
lookup table, and cannot silently mismatch the way name matching does.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Set

import requests

from history_data import CACHE_DIR

URL = ("https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/"
       "betting/csv/cfb_line_odds.csv.gz")

# Sharpest first: a closing number from Pinnacle is the benchmark a model has to
# beat. Anything else is a consensus of softer books.
BOOK_PRIORITY = ["PINNACLE", "BOOKMAKER", "BetCRIS & BOOKMAKER", "MATCHBOOK",
                 "bet365", "5Dimes & sportbet"]


@dataclass
class GameLine:
    game_id: str
    season: int
    home_id: str
    away_id: str
    desc: str
    date_time: str
    home_spread: Optional[float]   # negative = home favoured
    total: Optional[float]
    home_ml: Optional[int]
    away_ml: Optional[int]
    book: str
    # The number first posted, before the market had seen any action. Where a
    # model has any chance of knowing something the price does not yet reflect.
    open_home_spread: Optional[float] = None
    open_total: Optional[float] = None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def download(refresh: bool = False) -> str:
    """Cache the gzip as delivered. Expanding it costs 136 MB on disk to save a
    few milliseconds of decompression, which is a bad trade in both directions."""
    path = os.path.join(CACHE_DIR, "cfb_line_odds.csv.gz")
    os.makedirs(CACHE_DIR, exist_ok=True)
    if refresh or not os.path.exists(path):
        r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=180)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def read_rows(refresh: bool = False) -> List[dict]:
    with gzip.open(download(refresh), "rt", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def solve_abbr_map(rows: List[dict]) -> Dict[str, str]:
    """
    Abbreviation -> ESPN team id, by intersection.

    An abbreviation belongs to exactly one team, and every row it appears in
    lists that team as either the home or away side. Intersecting the {home,
    away} pairs across all its rows leaves precisely that team.
    """
    candidates: Dict[str, Optional[Set[str]]] = {}
    for r in rows:
        abbr = (r.get("abbr") or "").strip()
        if not abbr or abbr in ("over", "under"):
            continue
        pair = {str(r.get("home_team_id") or ""), str(r.get("away_team_id") or "")}
        pair.discard("")
        if len(pair) != 2:
            continue
        cur = candidates.get(abbr)
        candidates[abbr] = pair if cur is None else (cur & pair)

    return {a: next(iter(s)) for a, s in candidates.items() if s and len(s) == 1}


def load_game_lines(refresh: bool = False) -> List[GameLine]:
    rows = read_rows(refresh)

    abbr_map = solve_abbr_map(rows)

    by_game: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        gid = str(r.get("game_id") or "")
        if gid:
            by_game[gid].append(r)

    out: List[GameLine] = []
    for gid, rs in by_game.items():
        head = rs[0]
        home_id = str(head.get("home_team_id") or "")
        away_id = str(head.get("away_team_id") or "")
        if not home_id or not away_id:
            continue

        books = {r.get("book") for r in rs}
        book = next((b for b in BOOK_PRIORITY if b in books), None)
        picked = [r for r in rs if r.get("book") == book] if book else rs
        book = book or "consensus"

        def collect(market, pred, col=None):
            key = col or ("lines" if market != "money_line" else "odds")
            vals = [
                _f(r.get(key))
                for r in picked
                if r.get("market_type") == market and pred(r)
            ]
            vals = [v for v in vals if v is not None]
            return median(vals) if vals else None

        home_spread = collect("spread", lambda r: abbr_map.get((r.get("abbr") or "").strip()) == home_id)
        total = collect("total", lambda r: (r.get("abbr") or "").strip() == "over")
        home_ml = collect("money_line", lambda r: abbr_map.get((r.get("abbr") or "").strip()) == home_id)
        away_ml = collect("money_line", lambda r: abbr_map.get((r.get("abbr") or "").strip()) == away_id)

        is_home = lambda r: abbr_map.get((r.get("abbr") or "").strip()) == home_id
        open_spread = collect("spread", is_home, "opening_lines")
        open_total = collect("total", lambda r: (r.get("abbr") or "").strip() == "over",
                             "opening_lines")

        season = _f(head.get("season"))
        out.append(GameLine(
            game_id=gid, season=int(season) if season else 0,
            home_id=home_id, away_id=away_id,
            desc=head.get("game_desc", ""), date_time=head.get("date_time", ""),
            home_spread=home_spread, total=total,
            home_ml=int(home_ml) if home_ml else None,
            away_ml=int(away_ml) if away_ml else None,
            book=book,
            open_home_spread=open_spread, open_total=open_total,
        ))
    return out
