"""
The top-N poll parlay, on paper until it earns a stake.

THE STRATEGY, in one line: every weekend, parlay the moneylines of the AP top
four against unranked opponents only.

WHERE IT CAME FROM. Backtested 2022-2025, staking $100 of new cash plus half
the bank each weekend:

    depth   weekends    W-L         legs        NET     ROI
      3        39       36-3    59/62  = 95%   + 114     38%
      4        46       42-4    88/93  = 95%   + 901    225%
      5        48       41-7   105/113 = 93%   + 812    116%
      6        49       36-13  123/137 = 90%   + 220     17%

Excluding RANKED opponents is what makes it work: nine of the ten top-5 losses
in 2025 came against a ranked team, and the old rule only excluded other top
FIVE teams, so it waved through #4 Clemson vs #9 LSU and #3 Oregon vs #7
Indiana as if they were free legs.

WHY IT IS ON PAPER. Three reasons, all of which the ledger should settle
rather than an argument:

  1. Depth 4 was chosen by sweeping ten depths over four seasons and taking
     the best. Some of that +225% is curve fitting, and the honest way to find
     out how much is to run it forward.
  2. 2024 lost money at EVERY depth (-200 at four, -293 at five, -677 at
     seven). One season like that erases two good ones.
  3. 46 weekends is a small sample for a bet that loses 100% of stake when it
     misses.

WHAT DOES NOT WORK, measured and closed. Teasing these same games loses in all
forty season-by-depth cells (-2,619 at depth four over the four seasons): a
six-point tease off a 25-point spread crosses no key number, so the legs cover
about 70% against a ladder that needs 70-76%. And there is no venue edge --
home 94.0% +/-3.4 against road 91.5% +/-3.6, with home the BIGGER favourite.

    python ranked.py                 # this week's ticket
    python ranked.py --record        # how the paper run is doing
    python ranked.py --log           # write this week's ticket to the ledger
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from typing import Dict, List, Optional

import rankings

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(ROOT, "public", "data", "board.json")
PAPER = os.path.join(ROOT, "data", "archive", "ranked_paper.ndjson")

# Four, from the sweep above. Named rather than inlined because the whole
# point of the forward test is to find out whether this number survives.
DEPTH = 4

# The backtest's staking, carried forward unchanged so the paper record is
# comparable to it: $100 of new money each weekend plus half the bank, the
# $100 coming home on a win and the rest banking.
BASE_STAKE = 100.0

# A moneyline of -1 is not a price. The college line file carries a handful
# and a profit function that trusts them pays 100-to-1 on a coin flip; that
# bug once turned a real -2.6% into a reported +40%.
def valid_price(odds: Optional[int]) -> bool:
    return odds is not None and (odds <= -100 or odds >= 100)


def decimal(american: int) -> float:
    return 1 + (american / 100.0 if american > 0 else 100.0 / -american)


def to_american(dec: float) -> int:
    return int(round((dec - 1) * 100)) if dec >= 2 else -int(round(100 / (dec - 1)))


def slate_games(board: Dict, league: str = "NCAAF") -> List[dict]:
    """This week's college games, in kickoff order."""
    import teaser as T
    cutoff = T.slate_end()
    out = []
    for g in ((board.get("leagues") or {}).get(league) or {}).get("games") or []:
        try:
            ko = datetime.datetime.fromisoformat(str(g["kickoff"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ko <= cutoff:
            out.append((ko, g))
    return [g for _, g in sorted(out, key=lambda x: x[0])]


def current_poll(games: List[dict], season: Optional[int] = None,
                 polls: Optional[List[dict]] = None) -> Optional[dict]:
    """
    The poll in effect for this slate: released before the first kickoff.

    Not "the current poll" from the live endpoint, even though that is usually
    the same thing. On a Sunday or Monday the live endpoint has already rolled
    to the poll that judged the weekend just played, and using it to pick next
    weekend's teams is fine -- but using it to pick THIS weekend's, mid-slate,
    is the lookahead that produced a fake 11-0 season.
    """
    if not games:
        return None
    first = min(datetime.datetime.fromisoformat(
        str(g["kickoff"]).replace("Z", "+00:00")) for g in games).date()
    season = season or (first.year if first.month >= 7 else first.year - 1)
    polls = polls if polls is not None else rankings.season_polls(season)
    return rankings.poll_in_effect(polls, first)


def build_ticket(board: Dict, depth: int = DEPTH,
                 polls: Optional[List[dict]] = None) -> Optional[dict]:
    """
    This weekend's legs: top-`depth` teams facing an unranked opponent.

    Returns None when there is no poll yet or no qualifying game, which is a
    real outcome and not an error -- four weekends of 2025 produced no ticket.
    """
    games = slate_games(board)
    poll = current_poll(games, polls=polls)
    if not poll:
        return None
    ranks = poll["ranks"]
    pool = {t for t, r in ranks.items() if r <= depth}

    legs, skipped = [], []
    for g in games:
        hid, aid = str(g["home"]["id"]), str(g["away"]["id"])
        ours = [t for t in (hid, aid) if t in pool]
        if len(ours) != 1:
            if len(ours) == 2:
                skipped.append({"game": g.get("short_name") or "",
                                "why": "both teams are top %d" % depth})
            continue
        tid = ours[0]
        opp = aid if tid == hid else hid
        if opp in ranks:
            skipped.append({"game": g.get("short_name") or "",
                            "why": f"opponent is ranked #{ranks[opp]}"})
            continue
        leg = next((l for l in (g.get("legs") or []) if str(l.get("team_id")) == tid), None)
        odds = int(leg["odds"]) if leg and valid_price(leg.get("odds")) else None
        if odds is None:
            skipped.append({"game": g.get("short_name") or "",
                            "why": "no moneyline posted"})
            continue
        legs.append({"team_id": tid, "team": leg.get("team_abbr") or tid,
                     "rank": ranks[tid], "game": g.get("short_name") or "",
                     "event_id": str(g["event_id"]), "kickoff": g["kickoff"],
                     "odds": odds, "side": leg.get("side"),
                     "implied": leg.get("implied_prob")})
    if not legs:
        return None

    dec = 1.0
    for l in legs:
        dec *= decimal(l["odds"])
    return {"poll": poll["name"], "poll_released": str(poll["released"]),
            "depth": depth, "legs": legs, "skipped": skipped,
            "decimal": dec, "price": to_american(dec),
            "slate": legs[0]["kickoff"][:10]}


def paper_rows() -> List[dict]:
    if not os.path.exists(PAPER):
        return []
    return [json.loads(l) for l in open(PAPER) if l.strip()]


def bank_state(rows: Optional[List[dict]] = None) -> dict:
    """
    Replay the staking rule over every settled paper ticket.

    Recomputed from the ledger every time rather than stored, so a corrected
    result flows through instead of leaving the bank permanently wrong.
    """
    rows = paper_rows() if rows is None else rows
    bank, pocket, w, l = 0.0, 0.0, 0, 0
    for r in sorted(rows, key=lambda x: x["slate"]):
        if r.get("result") not in ("won", "lost"):
            continue
        from_bank = bank * 0.5
        stake = BASE_STAKE + from_bank
        bank -= from_bank
        if r["result"] == "won":
            bank += stake * r["decimal"] - BASE_STAKE
            w += 1
        else:
            pocket += BASE_STAKE
            l += 1
    return {"bank": bank, "pocket": pocket, "net": bank - pocket,
            "won": w, "lost": l, "settled": w + l,
            "next_stake": BASE_STAKE + bank * 0.5}


def log_ticket(ticket: dict) -> str:
    """Append a pending paper ticket. One per slate; re-running is a no-op."""
    rows = paper_rows()
    if any(r["slate"] == ticket["slate"] and r["depth"] == ticket["depth"] for r in rows):
        return "already logged"
    state = bank_state(rows)
    row = dict(ticket, result="pending", stake=round(state["next_stake"], 2),
               logged_at=datetime.datetime.now(datetime.timezone.utc)
               .isoformat(timespec="seconds"))
    os.makedirs(os.path.dirname(PAPER), exist_ok=True)
    with open(PAPER, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return f"logged, staking {row['stake']:.2f} on paper"


def settle(rows: Optional[List[dict]] = None) -> int:
    """
    Grade every pending ticket whose games have all finished.

    A ticket is graded only when EVERY leg has a final. Grading the moment one
    leg loses would be arithmetically safe -- a parlay is dead at the first
    loss -- but it would also record a result before the slate is over, and a
    postponed game would then settle a bet that was never decided.
    """
    from espn import fetch_completed_games
    rows = paper_rows() if rows is None else rows
    pending = [r for r in rows if r.get("result") == "pending"]
    if not pending:
        return 0

    finals = {}
    try:
        for g in fetch_completed_games("NCAAF"):
            finals[str(g.event_id)] = g
    except Exception as e:
        print(f"  results unavailable: {e}")
        return 0

    graded = 0
    for r in pending:
        legs = [finals.get(str(l["event_id"])) for l in r["legs"]]
        if any(g is None for g in legs):
            continue
        won = True
        for leg, g in zip(r["legs"], legs):
            m = (g.home_score - g.away_score) if leg["side"] == "home" else \
                (g.away_score - g.home_score)
            leg["result"] = "won" if m > 0 else ("push" if m == 0 else "lost")
            if m <= 0:
                won = False
        r["result"] = "won" if won else "lost"
        graded += 1

    if graded:
        with open(PAPER, "w") as fh:
            for r in sorted(rows, key=lambda x: x["slate"]):
                fh.write(json.dumps(r) + "\n")
    return graded


def render_email(ticket: dict, state: dict) -> str:
    """The weekly note. Paper status is stated, not implied."""
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    legs = "".join(
        f"<tr><td style='padding:6px 12px 6px 0'>#{l['rank']} <b>{esc(l['team'])}</b></td>"
        f"<td style='padding:6px 12px 6px 0;color:#555'>{esc(l['game'])}</td>"
        f"<td style='padding:6px 0;text-align:right'>{l['odds']:+d}</td></tr>"
        for l in ticket["legs"])
    rec = (f"{state['won']}-{state['lost']}, net "
           f"{state['net']:+,.2f}" if state["settled"] else "no settled weekends yet")
    return f"""
<div style="font-family:system-ui,sans-serif;max-width:560px">
  <p style="background:#fffbe6;border:1px solid #f0d000;padding:8px 12px;
     border-radius:6px;margin:0 0 16px">
    <b>PAPER ONLY.</b> Nothing is placed. This is a forward test of the
    top-{ticket['depth']} rule, which was fitted on four past seasons.
  </p>
  <h2 style="margin:0 0 4px">Top {ticket['depth']} vs unranked &middot; {esc(ticket['slate'])}</h2>
  <p style="margin:0 0 16px;color:#555">{esc(ticket['poll'])},
     released {esc(ticket['poll_released'])}</p>
  <table style="border-collapse:collapse;width:100%">{legs}</table>
  <p style="margin:16px 0 4px;font-size:18px"><b>{len(ticket['legs'])} legs,
     pays {ticket['price']:+d}</b></p>
  <p style="margin:0;color:#555">paper stake {state['next_stake']:,.2f}
     &rarr; {state['next_stake']*ticket['decimal']:,.2f}</p>
  <p style="margin:16px 0 0;color:#555">Paper record: {rec}</p>
</div>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--depth", type=int, default=DEPTH)
    ap.add_argument("--log", action="store_true", help="write this week's ticket")
    ap.add_argument("--record", action="store_true", help="paper record so far")
    ap.add_argument("--settle", action="store_true", help="grade finished tickets")
    ap.add_argument("--email", help="send this week's ticket to an address")
    a = ap.parse_args()

    if a.settle:
        n = settle()
        print(f"  graded {n} paper ticket(s)")
        return 0

    if a.record:
        s = bank_state()
        print(f"\n  paper record: {s['won']}-{s['lost']} over {s['settled']} weekends")
        print(f"  banked {s['bank']:,.2f} · out of pocket {s['pocket']:,.0f} "
              f"· NET {s['net']:+,.2f}")
        print(f"  next stake would be {s['next_stake']:,.2f}\n")
        for r in sorted(paper_rows(), key=lambda x: x["slate"]):
            names = ", ".join(f"#{l['rank']} {l['team']}" for l in r["legs"])
            print(f"  {r['slate']}  {r['price']:+6d}  {r['result']:>7}  {names}")
        return 0

    with open(BOARD) as fh:
        board = json.load(fh)
    t = build_ticket(board, a.depth)
    if not t:
        print("\n  no ticket this week: no poll yet, or no top-"
              f"{a.depth} team facing an unranked opponent with a posted price.\n")
        return 0

    s = bank_state()
    print(f"\n  {t['poll']} (released {t['poll_released']}), top {t['depth']} "
          f"vs unranked · slate {t['slate']}")
    print(f"\n  {'leg':22}{'price':>8}{'market':>9}")
    for l in t["legs"]:
        imp = f"{l['implied']*100:.1f}%" if l.get("implied") else "-"
        print(f"  #{l['rank']} {l['team']:<18}{l['odds']:+8d}{imp:>9}   {l['game']}")
    print(f"\n  {len(t['legs'])} legs, pays {t['price']:+d}  "
          f"(paper stake {s['next_stake']:,.2f} -> "
          f"{s['next_stake']*t['decimal']:,.2f})")
    if t["skipped"]:
        print("\n  left out:")
        for sk in t["skipped"]:
            print(f"    {sk['game']:<28} {sk['why']}")
    if a.log:
        print(f"\n  {log_ticket(t)}")
    if a.email:
        import delivery
        subject = (f"[paper] Top {t['depth']} parlay {t['price']:+d} · {t['slate']}")
        delivery.send_email(a.email, subject, render_email(t, s), confirm=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
