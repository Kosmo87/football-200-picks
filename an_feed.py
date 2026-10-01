"""
Every book's game lines from Action Network: free, keyless, no quota.

WHY. faults.py finds transposed and stale lines by comparing each book with
its peers, and it could only look every eight hours: the odds API costs a
credit per market per call and 500 a month does not go further. A transposed
line stands about an hour (BetMGM's Arizona State, 2026-09-13), so seven looks
in eight missed the window before the first one opened. This feed has no
meter, so the scan can run every couple of minutes.

WHAT IT RETURNS. The same shape line_shop.games_from_live() yields --
(game, kickoff, {book: {label: price}}) -- so the fault finders run on it
unchanged. Spread labels are "Team -5.5"; moneyline labels are the team.

COVERAGE, measured 2026-09-27 for the coming weekend: NFL 16 of 16 games at
11 books including BetMGM and FanDuel. College 59 games, but BetMGM on only 3
-- it may post later in the week; the ASU error was a BetMGM college line, so
check `python an_feed.py` midweek before trusting it there.

Book 15 is Action Network's consensus and 30 is the opening line. Neither is
a book you can bet, so neither is a peer.

    python an_feed.py            # coverage by book, both leagues
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Tuple

import requests

URL = "https://api.actionnetwork.com/web/v2/scoreboard/{league}"
BOOKS = {68: "draftkings", 69: "fanduel", 75: "betmgm", 123: "caesars",
         79: "bet365", 71: "betrivers", 972: "betrivers_ny", 2988: "fanatics",
         247: "unibet", 1903: "ballybet"}
PATHS = {"NFL": "nfl", "NCAAF": "ncaaf"}
# Books on the same odds engine post the same number, so counting each would
# let one trader vote three times on the consensus. BetRivers, Bally Bet and
# Unibet are all Kambi -- first run (2026-09-27) had them moving in lockstep on
# every fault it found. They count once, as BetRivers.
SAME_AS = {"betrivers_ny": "betrivers", "ballybet": "betrivers", "unibet": "betrivers"}


def _fetch(league: str, day: str) -> List[dict]:
    r = requests.get(URL.format(league=PATHS[league]),
                     params={"bookIds": ",".join(map(str, BOOKS)), "date": day},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.json().get("games") or []


def games(league: str, days: int = 7) -> List[dict]:
    """Upcoming games over the next `days`, de-duplicated by id."""
    now = datetime.now(timezone.utc)
    seen: Dict[int, dict] = {}
    for i in range(days):
        day = (now + timedelta(days=i)).strftime("%Y%m%d")
        for g in _fetch(league, day):
            if g.get("status") == "scheduled":
                seen[g["id"]] = g
    return list(seen.values())


def games_from_an(gs: List[dict], market: str, min_books: int = 3
                  ) -> Iterator[Tuple[str, str, Dict[str, Dict[str, int]]]]:
    """(game, kickoff, {book: {label: price}}) for 'spreads' or 'h2h'."""
    key = {"spreads": "spread", "h2h": "moneyline"}[market]
    for g in gs:
        names = {t["id"]: t["full_name"] for t in g.get("teams") or []}
        home, away = names.get(g["home_team_id"]), names.get(g["away_team_id"])
        if not home or not away:
            continue
        prices: Dict[str, Dict[str, int]] = {}
        for bid, m in (g.get("markets") or {}).items():
            book = BOOKS.get(int(bid))
            if not book or SAME_AS.get(book, book) in prices:
                continue
            # Group by market_id and keep only complete two-sided markets. Action
            # Network sometimes carries a lone extra row under its own
            # market_id (FanDuel, Cowboys @ Texans 2026-10-01: DAL +3 / HOU -3,
            # plus an orphan DAL -1.5). Read as a line, that orphan tripped a
            # false TRANSPOSED. A real line has both teams, and on a spread the
            # two points cancel.
            by_mkt: Dict[str, Dict[int, dict]] = {}
            for o in (m.get("event") or {}).get(key) or []:
                if o.get("is_live") or o.get("odds") is None:
                    continue
                if o.get("team_id") not in names:
                    continue
                by_mkt.setdefault(str(o.get("market_id")), {})[o["team_id"]] = o
            sides = {}
            for pair in by_mkt.values():
                if len(pair) != 2:
                    continue
                if key == "spread" and abs(sum(float(o["value"]) for o in pair.values())) > 1e-9:
                    continue
                for tid, o in pair.items():
                    team = names[tid]
                    label = f"{team} {float(o['value']):+g}" if key == "spread" else team
                    sides[label] = int(o["odds"])
            if len(sides) >= 2:
                prices[SAME_AS.get(book, book)] = sides
        if len(prices) >= min_books:
            kick = g["start_time"].replace(".000Z", "Z")
            yield f"{away} @ {home}", kick, prices


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    for league in PATHS:
        gs = games(league, a.days)
        c: Counter = Counter()
        for _, _, p in games_from_an(gs, "spreads", 1):
            c.update(p.keys())
        print(f"{league}: {len(gs)} upcoming games; spreads by book: "
              + ", ".join(f"{b} {n}" for b, n in c.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
