"""
Do the win percentages mean what they say?

THE QUESTION THIS ANSWERS. The board labels a bet "93% to win". That number
is only worth anything if bets labelled 93% actually win about 93 times in a
hundred. Nothing else on the board matters if the labels are wrong -- a pick
that says 93 and wins 70 is worse than useless, because it is confidently
wrong rather than obviously uncertain.

Graded on WINNING, not on price. Whether a bet was worth its odds is a
different question and this file does not ask it.

    logged     every board pick, with the probability claimed at the time
    graded     against the final score once the game is done
    reported   claimed vs actual, by band

Measured on day one, the ranking of signals by how often they picked the
outright winner:

    market favourite   6/8   75%
    our EPA model      5/8   62%
    home team          4/8   50%

The market is the best winner-picker available, which is why the board ranks
by its de-vigged probability rather than by anything we built. This file
checks that the ranking is honest.

  python calibration.py --log        # record today's board
  python calibration.py --grade      # settle what has finished
  python calibration.py --report     # claimed vs actual, by band
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import requests

import keys  # noqa: F401

ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(ROOT, "data", "archive", "calibration.ndjson")

SCOREBOARD = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NCAAF": ("https://site.api.espn.com/apis/site/v2/sports/football/"
              "college-football/scoreboard"),
}

# Wide bands. Narrow ones look rigorous and take a whole season to fill.
BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]


def _rows() -> List[dict]:
    if not os.path.exists(LEDGER):
        return []
    return [json.loads(l) for l in open(LEDGER) if l.strip()]


def log(min_chance: float = 0.55, leagues=("NFL", "NCAAF"),
        books=("betmgm", "fanduel")) -> int:
    """Record what the board claims right now, before anything is known."""
    import confidence as C
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = {(r["side"], r["game"]) for r in _rows()}
    new = 0
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as fh:
        for lg in leagues:
            for r in C.scan(lg, min_chance, days=8,
                            only_books=[b.lower() for b in books]):
                key = (r["side"], r["game"])
                if key in seen:
                    continue          # one claim per bet; later prices are not new claims
                seen.add(key)
                fh.write(json.dumps({
                    "logged_at": stamp, "league": lg, "game": r["game"],
                    "side": r["side"], "chance": round(r["chance"], 4),
                    "price": r["price"], "book": r["book"],
                    "market": r["market"], "kickoff": r["kickoff"].isoformat(),
                    "status": "open",
                }) + "\n")
                new += 1
    print(f"logged {new} claim(s); {len(seen)} tracked in total")
    return 0


def _finals(league: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    params = {"limit": 200}
    if league == "NCAAF":
        params["groups"] = 80
    try:
        r = requests.get(SCOREBOARD[league], params=params, timeout=30)
        events = r.json().get("events") or []
    except (requests.RequestException, ValueError):
        return out
    for e in events:
        for c in e.get("competitions") or []:
            st = (c.get("status") or {}).get("type") or {}
            if st.get("state") != "post":
                continue
            comp = c.get("competitors") or []
            if len(comp) != 2:
                continue
            names, scores = {}, {}
            for x in comp:
                t = x.get("team") or {}
                for label in (t.get("displayName"), t.get("shortDisplayName"),
                              t.get("abbreviation"), t.get("location")):
                    if label:
                        names[label] = t.get("displayName")
                scores[t.get("displayName")] = int(x.get("score") or 0)
            out[e.get("shortName", "")] = {"names": names, "scores": scores}
    return out


def grade() -> int:
    """
    Settle claims against the final score of THE GAME THEY WERE MADE ON.

    The first version matched on team name alone and graded 46 of 51 claims
    instantly -- every one a Sept 19-20 game matched to some earlier final
    involving the same team. Notre Dame "won 52-0" in a game that had not been
    played. It produced a full calibration table, by band, with a plausible
    overall number, and all of it was fiction.

    So two conditions now, both required: the kickoff has passed, AND the
    matched ESPN event contains BOTH teams from the logged game.
    """
    rows = _rows()
    if not rows:
        print("Nothing logged yet.")
        return 0
    now = datetime.now(timezone.utc)
    finals = {lg: _finals(lg) for lg in ("NFL", "NCAAF")}
    changed = skipped = 0
    for r in rows:
        if r["status"] != "open":
            continue
        try:
            kick = datetime.fromisoformat(r["kickoff"])
        except (ValueError, KeyError):
            continue
        if kick > now:
            skipped += 1
            continue                      # not played; nothing to grade
        # the logged game is "Away @ Home" -- both must appear in the final
        away, _, home = r["game"].partition(" @ ")
        for _, f in finals.get(r["league"], {}).items():
            names = f["names"]
            full_side = names.get(r["side"])
            if not full_side:
                continue
            if not (names.get(away.strip()) and names.get(home.strip())):
                continue                  # same team, different fixture
            scores = f["scores"]
            if full_side not in scores or len(scores) != 2:
                continue
            mine = scores[full_side]
            theirs = [v for k, v in scores.items() if k != full_side][0]
            if r["market"] != "h2h":
                r["status"] = "n/a"       # the chance is a chance to WIN
            else:
                r["status"] = ("won" if mine > theirs
                               else "lost" if mine < theirs else "push")
                r["final"] = f"{mine}-{theirs}"
            changed += 1
            break
    with open(LEDGER, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"graded {changed}; {skipped} still to be played")
    return 0


def report() -> int:
    rows = [r for r in _rows() if r["status"] in ("won", "lost")]
    if not rows:
        print("Nothing settled yet. Log the board, wait for games, then grade.")
        return 0
    print(f"\n{len(rows)} settled claim(s)\n")
    print(f"{'band':<12}{'n':>5}{'claimed':>10}{'actual':>10}{'gap':>9}")
    for lo, hi in BANDS:
        sub = [r for r in rows if lo <= r["chance"] < hi]
        if not sub:
            continue
        claimed = sum(r["chance"] for r in sub) / len(sub)
        actual = sum(1 for r in sub if r["status"] == "won") / len(sub)
        flag = "  thin" if len(sub) < 10 else ""
        print(f"{lo*100:.0f}-{hi*100:.0f}%{'':<6}{len(sub):>5}{claimed*100:>9.1f}%"
              f"{actual*100:>9.1f}%{(actual-claimed)*100:>+8.1f}{flag}")
    claimed = sum(r["chance"] for r in rows) / len(rows)
    actual = sum(1 for r in rows if r["status"] == "won") / len(rows)
    print(f"\noverall: claimed {claimed*100:.1f}%, actual {actual*100:.1f}% "
          f"({(actual-claimed)*100:+.1f}pp)")
    if len(rows) < 40:
        print("Under 40 settled bets nothing here is a verdict, only a direction.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.log:
        return log()
    if a.grade:
        return grade()
    if a.report:
        return report()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
