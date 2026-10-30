"""
Kalshi game-winner contracts, priced against the sportsbook consensus.

WHY. Kalshi is an exchange, not a book: its price is whatever the last trader
left on the ask, and in a thin market that can sit well away from where every
sportsbook has the game. A contract that pays $1 if a team wins, offered below
the books' de-vigged chance of that team winning, is the same kind of stale
number faults.py hunts at FanDuel and BetMGM -- with no palpable-error clause,
because nobody at an exchange decides to void a trade they merely regret.

WHAT IT READS. The public market endpoints (no key, no quota) for the
KXNFLGAME / KXNCAAFGAME / KXNBAGAME / KXNHLGAME / KXNCAAMBGAME series (NHL
includes overtime and shootouts, as the books' moneylines do): one binary market per team per game, "Team
wins". Buying YES on one team and buying NO on the other are the same bet, so
each team's price is the cheaper of the two.

COSTS. Kalshi charges a taker fee of 0.07 x P x (1 - P) per contract, rounded
up to the cent per order -- 1.75c at 50c, less toward the tails. It comes off
the edge before anything is called a bet. A tie settles at 50c, which is
close enough to a push that it is ignored here.

    python kalshi_feed.py            # every matched game, gap vs consensus
"""

from __future__ import annotations

import argparse
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
SERIES = {"NFL": "KXNFLGAME", "NCAAF": "KXNCAAFGAME", "NBA": "KXNBAGAME",
          "NHL": "KXNHLGAME", "NCAAB": "KXNCAAMBGAME",
          # Soccer is three markets per game -- each side and "Tie" -- settled
          # on 90 minutes plus stoppage, the same as the books' 3-way line.
          "SOCCER": ("KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME",
                     "KXLIGUE1GAME", "KXMLSGAME", "KXUCLGAME", "KXUELGAME"),
          # Tennis pays the winner once a ball is played, so a mid-match
          # retirement pays the opponent. Books differ on retirements; that
          # is a rules gap, not an edge, and a rare one.
          "ATP": "KXATPMATCH", "WTA": "KXWTAMATCH"}
TIE = "Tie"


def series(league: str) -> tuple:
    v = SERIES[league]
    return v if isinstance(v, tuple) else (v,)
FEE_RATE = 0.07
# Kalshi's occurrence_datetime is meant as the expected END of the game, about
# three hours after kickoff (BUF @ MIN, 2026-11-09: kick 01:15Z, occurrence
# 04:15Z) -- but for college it is often a placeholder set before the kickoff
# was: Wisconsin @ Iowa kicks Saturday 16:00Z and Kalshi had Friday 03:00Z.
# So the window is a day each way. Two named teams do not meet twice in that,
# and it still keeps a rematch weeks apart from matching.
KICK_WINDOW = (timedelta(hours=-24), timedelta(hours=24))


def fee(price: float) -> float:
    """Taker fee per contract, in dollars, for one contract bought at `price`."""
    return math.ceil(FEE_RATE * price * (1 - price) * 100 - 1e-9) / 100


def _get(params: dict, tries: int = 3) -> dict:
    for attempt in range(tries):
        try:
            r = requests.get(URL, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def markets(league: str) -> List[dict]:
    """Every open game-winner market in the league, all pages."""
    out = []
    for st in series(league):
        cursor = None
        while True:
            params = {"series_ticker": st, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = _get(params)
            out.extend(d.get("markets") or [])
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                break
    return out


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def events(league: str) -> List[dict]:
    """
    One row per game: {event, ends, teams: {name: {price, size, ticker, how}}}.

    `price` is the cheapest way to own that team winning -- YES on its own
    market or NO on its opponent's -- and `size` is how many contracts are
    sitting at that price. A price with nothing behind it is not a price.
    """
    by_event: Dict[str, List[dict]] = {}
    for m in markets(league):
        by_event.setdefault(m["event_ticker"], []).append(m)
    out = []
    for ev, ms in by_event.items():
        three_way = len(ms) == 3 and any(m.get("yes_sub_title") == TIE for m in ms)
        if len(ms) != 2 and not three_way:
            continue
        a, b = ms[0], ms[1]
        teams = {}
        # NO on the opponent only means "this team wins" when there are two
        # outcomes. With a draw it means "win OR draw", a different bet.
        pairs = ((a, b), (b, a)) if not three_way else ((m, None) for m in ms)
        for mine, other in pairs:
            name = mine.get("yes_sub_title") or ""
            yes, yes_n = _f(mine.get("yes_ask_dollars")), _f(mine.get("yes_ask_size_fp"))
            # NO on the opponent's market. Its size at the ask is the YES bid
            # size on that market, since a NO ask is a YES bid seen backwards.
            no, no_n = ((_f(other.get("no_ask_dollars")), _f(other.get("yes_bid_size_fp")))
                        if other else (None, None))
            opts = [(p, n, mine["ticker"], "YES") for p, n in ((yes, yes_n),)
                    if p and 0 < p < 1]
            opts += [(p, n, other["ticker"], "NO") for p, n in ((no, no_n),)
                     if p and 0 < p < 1]
            if not opts:
                continue
            p, n, ticker, how = min(opts, key=lambda o: o[0])
            teams[name] = {"price": p, "size": n or 0.0, "ticker": ticker, "how": how,
                           # the balance shard an order here must be paid from
                           "exchange_index": mine.get("exchange_index")}
        if len(teams) == len(ms):
            out.append({"event": ev, "ends": a.get("occurrence_datetime") or "",
                        "teams": teams})
    return out


# Kalshi short names no token rule can reach (measured 2026-10-30).
ALIASES = {"M´gladbach": "Moenchengladbach", "Saint Louis": "St. Louis City",
           "New York RB": "New York Red Bulls", "Los Angeles G": "LA Galaxy",
           "Hamburg": "Hamburger SV"}


def _same(kname: str, feed_name: str) -> bool:
    """
    teamnames.same_team, after undoing Kalshi's two abbreviations.

    "Ohio St." -- "St" is under same_team's three-letter prefix floor, so it
    becomes "State". "Los Angeles R" / "New York J" -- the city is shared, so
    the letter has to match the start of the feed's mascot (Rams, Jets).
    """
    from teamnames import same_team, tokens
    name = ALIASES.get(kname.strip(), kname.strip())
    # German umlauts the way English feeds spell them: Köln -> Koeln.
    for a, b in (("ö", "oe"), ("ü", "ue"), ("ä", "ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("Ä", "Ae")):
        name = name.replace(a, b)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    feed_name = unicodedata.normalize("NFKD", feed_name).encode("ascii", "ignore").decode()
    name = re.sub(r"\bSt\.?$", "State", name)
    m = re.match(r"^(.*\S)\s+([A-Z])$", name)
    if m:
        city, letter = m.groups()
        words = feed_name.split()
        return (len(words) > 1 and words[-1].startswith(letter)
                and same_team(city, " ".join(words[:-1])))
    # "Ohio" must not take "Ohio State Buckeyes": same_team lets a lone city
    # stand for any school in it. A State the feed has and Kalshi does not
    # means a different school -- a missed match costs an alert, a wrong one
    # sends a bet on the wrong team.
    if "state" in tokens(feed_name) and "state" not in tokens(name):
        return False
    return same_team(name, feed_name)


def match(ev: dict, game: str, kick: str) -> Optional[Dict[str, str]]:
    """
    {kalshi team name: feed team name} when this Kalshi event is `game`.

    Both teams must match AND the kickoff must sit inside KICK_WINDOW before
    Kalshi's expected end. `game` is "Away @ Home" from an_feed.
    """
    try:
        k = datetime.fromisoformat(kick.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ev["ends"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError):
        return None
    if not (KICK_WINDOW[0] <= end - k <= KICK_WINDOW[1]):
        return None
    sides = [s.strip() for s in game.split(" @ ")]
    if len(sides) != 2:
        return None
    pairs = {}
    for kname in ev["teams"]:
        if kname == TIE:
            pairs[kname] = "Draw"           # an_feed.DRAW
            continue
        hits = [s for s in sides if _same(kname, s)]
        if len(hits) != 1:
            return None
        pairs[kname] = hits[0]
    if len(set(pairs.values())) != len(pairs):
        return None
    return pairs


def gaps(league: str, an_games: List[dict]) -> List[dict]:
    """
    Every matched (game, team): Kalshi's cost against the books' fair chance.

    `edge` is fair - (price + fee), in probability, which is also the expected
    profit per $1 contract. Positive means Kalshi sells that team for less than
    the sportsbooks think it is worth.
    """
    import an_feed
    import line_shop as LS
    kev = events(league)
    out = []
    for game, kick, prices in an_feed.games_from_an(an_games, "h2h", 4):
        fair = LS.fair_probs(prices)
        for ev in kev:
            pairs = match(ev, game, kick)
            if not pairs:
                continue
            for kname, feed_name in pairs.items():
                p = fair.get(feed_name)
                if p is None:
                    continue
                t = ev["teams"][kname]
                cost = t["price"] + fee(t["price"])
                out.append({"market": "kalshi", "book": "kalshi", "league": league,
                            "game": game, "kickoff": kick, "side": feed_name,
                            "kalshi_price": t["price"], "fee": fee(t["price"]),
                            "size": t["size"], "ticker": t["ticker"], "how": t["how"],
                            "exchange_index": t.get("exchange_index"),
                            "consensus": round(p, 4), "peers": len(prices),
                            "edge": round(p - cost, 4)})
            break
    return out


def main() -> int:
    import an_feed
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    for league in SERIES:
        an = an_feed.games(league, a.days if league in ("NFL", "NCAAF") else
                           3 if league == "SOCCER" else 2)
        rows = gaps(league, an)
        kev = events(league)
        print(f"\n{league}: {len(kev)} Kalshi games, {len(rows)//2} matched to the books")
        for r in sorted(rows, key=lambda r: -r["edge"])[:12]:
            print(f"  {r['edge']*100:+5.1f}pp  {r['side'][:26]:26} "
                  f"kalshi {r['kalshi_price']*100:3.0f}c+{r['fee']*100:.0f}c "
                  f"(x{r['size']:.0f})  books {r['consensus']*100:4.1f}%  {r['kickoff'][:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
