"""
Season-by-season replay of the top-N poll parlay from a starting bankroll.

Same rule as ranked.py: each weekend, parlay the moneylines of the AP top
`depth` against UNRANKED opponents, using the poll released strictly before
each game (poll_in_effect). Regular season only -- bowls and the playoff are
not in the odds file's regular season, and the paper test doesn't bet them.

Data, all cached under cache/ after the first run:
  - dated AP polls from sports.core.api.espn.com. 2016 onward only: earlier
    payloads carry no release date, and guessing it is how the fake 11-0
    season happened, so they are refused rather than inferred.
  - moneylines from cfb_line_odds.csv.gz, median across books per team.
  - finals from the ESPN scoreboard, week by week.

A leg with no moneyline in the odds file is left off the ticket and counted
as "unpriced". Over 2016-2025 none of those legs lost, so the W-L is exact
and the payout is slightly understated. 2020 has no regular-season
moneylines at all and is skipped.

    python ranked_backtest.py --bank 1700            # all staking rules
    python ranked_backtest.py --bank 1700 --weeks    # every ticket
    python ranked_backtest.py --tease 10 --depth 3   # same legs, teased
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
from collections import defaultdict
from typing import Dict, List

import pandas as pd

import espn
import rankings

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
ODDS = os.path.join(CACHE, "cfb_line_odds.csv.gz")
FIRST_DATED_SEASON = 2016
SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/"
              "college-football/scoreboard?dates={year}&seasontype=2&week={week}"
              "&groups=80&limit=400")


def _dec(american: float) -> float:
    return 1 + (american / 100 if american > 0 else 100 / -american)


def _american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def polls(year: int) -> List[dict]:
    path = os.path.join(CACHE, f"ap_polls_{year}.json")
    if os.path.exists(path):
        raw = json.load(open(path))
    else:
        raw = []
        for stype, week in rankings.POLL_WEEKS:
            try:
                p = rankings.fetch_poll(year, stype, week)
            except KeyError:          # undated payload -- refuse, don't guess
                p = None
            if p:
                raw.append(dict(p, released=p["released"].isoformat()))
        json.dump(raw, open(path, "w"))
    out = [dict(p, released=datetime.date.fromisoformat(p["released"])) for p in raw]
    return sorted(out, key=lambda p: p["released"])


def finals(year: int) -> Dict[str, dict]:
    """event_id -> {home, away, home_pts, away_pts, kickoff}."""
    path = os.path.join(CACHE, f"cfb_finals_{year}.json")
    if os.path.exists(path):
        return json.load(open(path))
    out = {}
    for week in range(1, 17):
        d = espn.fetch_scoreboard(SCOREBOARD.format(year=year, week=week))
        for ev in d.get("events") or []:
            comp = ev["competitions"][0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            side = {c["homeAway"]: c for c in comp["competitors"]}
            out[str(ev["id"])] = {
                "kickoff": ev["date"],
                "home": str(side["home"]["team"]["id"]),
                "away": str(side["away"]["team"]["id"]),
                "home_pts": int(side["home"].get("score") or 0),
                "away_pts": int(side["away"].get("score") or 0)}
    json.dump(out, open(path, "w"))
    return out


def moneylines() -> Dict[str, Dict[str, float]]:
    """game_id -> {team_id: median decimal price across books}."""
    d = pd.read_csv(ODDS, usecols=["game_id", "season", "market_type", "abbr",
                                   "odds", "home_team_id", "away_team_id"])
    d = d[(d.market_type == "money_line") & d.odds.notna() & d.game_id.notna()
          & d.home_team_id.notna() & d.away_team_id.notna() & d.abbr.notna()]
    # The file names the side by abbreviation only. Each abbreviation's team id
    # is the one id common to every game it appears in.
    ids = {}
    for abbr, g in d.groupby("abbr"):
        both = pd.concat([g.home_team_id, g.away_team_id]).value_counts()
        ids[abbr] = str(int(both.index[0]))
    out: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for r in d.itertuples():
        tid = ids[r.abbr]
        if tid in (str(int(r.home_team_id)), str(int(r.away_team_id))) and r.odds != 0:
            out[str(int(r.game_id))][tid].append(_dec(r.odds))
    return {g: {t: statistics.median(v) for t, v in s.items()} for g, s in out.items()}


def spreads() -> Dict[str, Dict[str, float]]:
    """game_id -> {team_id: median CLOSING spread across books}, that team's side."""
    d = pd.read_csv(ODDS, usecols=["game_id", "market_type", "abbr", "lines",
                                   "home_team_id", "away_team_id"])
    d = d[(d.market_type == "spread") & d.lines.notna() & d.game_id.notna()
          & d.home_team_id.notna() & d.away_team_id.notna() & d.abbr.notna()]
    ids = {}
    for abbr, g in d.groupby("abbr"):
        ids[abbr] = str(int(pd.concat([g.home_team_id, g.away_team_id])
                            .value_counts().index[0]))
    out: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for r in d.itertuples():
        tid = ids[r.abbr]
        if tid in (str(int(r.home_team_id)), str(int(r.away_team_id))):
            out[str(int(r.game_id))][tid].append(float(r.lines))
    return {g: {t: statistics.median(v) for t, v in s.items()} for g, s in out.items()}


def tease_tickets(year: int, depth: int, points: float,
                  sp: Dict[str, Dict[str, float]]) -> List[dict]:
    """
    Same selection, graded against the closing spread moved `points` toward
    the team. A push is a loss, as most books grade a teaser leg.
    """
    ps = polls(year)
    by_poll: Dict[str, dict] = {}
    for eid, g in finals(year).items():
        ko = datetime.datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00")).date()
        poll = rankings.poll_in_effect(ps, ko)
        if not poll:
            continue
        ranks = poll["ranks"]
        for side, opp in (("home", "away"), ("away", "home")):
            tid, oid = g[side], g[opp]
            r = ranks.get(tid)
            if not r or r > depth or oid in ranks:
                continue
            t = by_poll.setdefault(poll["released"].isoformat(),
                                   {"poll": poll["released"].isoformat(), "legs": [],
                                    "unpriced": []})
            line = sp.get(eid, {}).get(tid)
            margin = g[f"{side}_pts"] - g[f"{opp}_pts"]
            if line is None:
                t["unpriced"].append({"rank": r, "won": margin > 0})
                continue
            t["legs"].append({"rank": r, "line": line, "margin": margin,
                              "covered": margin + line + points > 0})
    out = []
    for key in sorted(by_poll):
        t = by_poll[key]
        t["won"] = all(l["covered"] for l in t["legs"])
        out.append(t)
    return out


def tickets(year: int, depth: int, ml: Dict[str, Dict[str, float]]) -> List[dict]:
    ps = polls(year)
    by_poll: Dict[str, dict] = {}
    for eid, g in finals(year).items():
        ko = datetime.datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00")).date()
        poll = rankings.poll_in_effect(ps, ko)
        if not poll:
            continue
        ranks = poll["ranks"]
        for side, opp in (("home", "away"), ("away", "home")):
            tid, oid = g[side], g[opp]
            r = ranks.get(tid)
            if not r or r > depth or oid in ranks:
                continue
            t = by_poll.setdefault(poll["released"].isoformat(),
                                   {"poll": poll["released"].isoformat(), "legs": [],
                                    "unpriced": []})
            won = g[f"{side}_pts"] > g[f"{opp}_pts"]
            price = ml.get(eid, {}).get(tid)
            leg = {"rank": r, "team": tid, "event": eid, "won": won, "dec": price}
            (t["legs"] if price else t["unpriced"]).append(leg)
    out = []
    for key in sorted(by_poll):
        t = by_poll[key]
        if not t["legs"]:
            continue
        dec = 1.0
        for leg in t["legs"]:
            dec *= leg["dec"]
        t["dec"] = dec
        t["won"] = all(l["won"] for l in t["legs"])
        out.append(t)
    return out


# Staking rules, each (bank, ticket) -> stake. Stakes never exceed the bank.
RULES = {
    "all-in every week": lambda bank: bank,
    "flat $100": lambda bank: 100.0,
    "10% of bank": lambda bank: bank * 0.10,
}


def run(ts: List[dict], start: float, rule) -> dict:
    bank, low = start, start
    for t in ts:
        stake = min(rule(bank), bank)
        bank += stake * (t["dec"] - 1) if t["won"] else -stake
        low = min(low, bank)
    return {"end": bank, "low": low}


# Teaser ladders, profit per $1 staked, by leg count. -134 at two legs is
# FanDuel's, checked on the slip (books.py); the rest are the common ladder
# from target.py and are assumptions. A 1-leg weekend is not a teaser: skipped.
LADDERS = {6: {2: -134, 3: 160, 4: 265}, 10: {3: -120, 4: 160}}


def _profit(american: int) -> float:
    return american / 100 if american > 0 else 100 / -american


def tease_report(a) -> int:
    sp = spreads()
    ladder = LADDERS.get(int(a.tease), {})
    print(f"AP top {a.depth} vs unranked, teased {a.tease:g} off the closing "
          f"spread (median across books), push = loss")
    print(f"ladder: " + ", ".join(f"{n} legs {p:+d}" for n, p in sorted(ladder.items()))
          + "   (only 6pt/2-leg -134 is verified)\n")
    print(f"{'season':>6} {'legs covered':>14} {'tickets':>8} {'W-L':>7} "
          f"{'flat $100':>10}  from ${a.bank:,.0f}, 10% of bank")
    by_n = defaultdict(lambda: [0, 0])
    tot_net, tot_legs, tot_cov = 0.0, 0, 0
    for year in range(a.first, a.last + 1):
        ts = tease_tickets(year, a.depth, a.tease, sp)
        legs = [l for t in ts for l in t["legs"]]
        cov = sum(l["covered"] for l in legs)
        tot_legs += len(legs); tot_cov += cov
        playable = [t for t in ts if len(t["legs"]) in ladder]
        w = sum(t["won"] for t in playable)
        net, bank = 0.0, a.bank
        for t in playable:
            n = len(t["legs"])
            by_n[n][0] += 1; by_n[n][1] += t["won"]
            pr = _profit(ladder[n])
            net += 100 * pr if t["won"] else -100
            stake = bank * 0.10
            bank += stake * pr if t["won"] else -stake
        tot_net += net
        pct = f"{cov}/{len(legs)} {cov / len(legs) * 100:3.0f}%" if legs else "-"
        print(f"{year:>6} {pct:>14} {len(playable):>8} {w:>3}-{len(playable) - w:<3} "
              f"{net:>+10,.0f}  {bank:>10,.0f}")
        if a.weeks:
            for t in ts:
                shown = ", ".join(f"#{l['rank']} {l['line']:+g}→{l['margin']:+d}"
                                  + ("" if l["covered"] else " X") for l in t["legs"])
                print(f"         {t['poll']}  {shown}")
    print(f"\nlegs covered {tot_cov}/{tot_legs} = {tot_cov / max(tot_legs, 1) * 100:.1f}%"
          f"   flat $100 net over all seasons {tot_net:+,.0f}\n")
    print(f"{'legs':>5} {'tickets':>8} {'hit':>7} {'price':>7} {'break-even':>11}")
    for n in sorted(by_n):
        c, won = by_n[n]
        be = "never" if won == 0 else (
            f"{round((c / won - 1) * 100):+d}" if c / won >= 2 else f"{round(-100 / (c / won - 1)):+d}")
        print(f"{n:>5} {c:>8} {won / c * 100:>6.1f}% {ladder[n]:>+7d} {be:>11}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--bank", type=float, default=1700)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--from", dest="first", type=int, default=FIRST_DATED_SEASON)
    ap.add_argument("--to", dest="last", type=int, default=2025)
    ap.add_argument("--weeks", action="store_true")
    ap.add_argument("--tease", type=float, default=0,
                    help="points to tease off the closing spread (6 or 10)")
    a = ap.parse_args()
    if a.tease:
        return tease_report(a)

    ml = moneylines()
    print(f"AP top {a.depth} vs unranked, start ${a.bank:,.0f} each season "
          f"(median moneyline across books)\n")
    hdr = f"{'season':>6} {'tix':>4} {'W-L':>6} {'legs':>8} {'unpriced':>8}  " + \
          "  ".join(f"{k:>18}" for k in RULES)
    print(hdr)
    totals = defaultdict(float)
    for year in range(a.first, a.last + 1):
        ts = tickets(year, a.depth, ml)
        if not ts:
            # 2020: the odds file has spreads and totals but no regular-season
            # moneylines. Left out rather than priced off the spread.
            print(f"{year:>6}  no moneylines in the odds file -- not replayed")
            continue
        w = sum(t["won"] for t in ts)
        legs = sum(len(t["legs"]) for t in ts)
        legw = sum(l["won"] for t in ts for l in t["legs"])
        unp = sum(len(t["unpriced"]) for t in ts)
        cells = []
        for name, rule in RULES.items():
            r = run(ts, a.bank, rule)
            totals[name] += r["end"] - a.bank
            cells.append(f"{r['end']:>10,.0f} (lo {r['low']:>5,.0f})")
        print(f"{year:>6} {len(ts):>4} {w:>3}-{len(ts) - w:<2} {legw:>3}/{legs:<4} "
              f"{unp:>8}  " + "  ".join(f"{c:>18}" for c in cells))
        if a.weeks:
            for t in ts:
                lost = [f"#{l['rank']}" for l in t["legs"] if not l["won"]]
                print(f"         {t['poll']}  {len(t['legs'])} legs  "
                      f"{_american(t['dec']):>6}  {'WON' if t['won'] else 'LOST ' + ','.join(lost)}")
    print(f"\n{'net over all seasons':>36}  " +
          "  ".join(f"{totals[k]:>+18,.0f}" for k in RULES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
