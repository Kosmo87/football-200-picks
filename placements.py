"""
What you actually bet, settled against final scores.

The ledgers in this project measure strategies: shop_ledger records every price
gap the detector found, history_data records every pick the model surfaced.
Both answer "does this approach work". Neither answers "how am I doing", and
the two diverge the first time a flagged bet is skipped -- after a month the
ledger describes a strategy nobody followed.

So placement is recorded where it happens. The board has a checkbox; the
checkbox writes to a blob store through netlify/functions/placements.mjs; this
settles what is in there against ESPN finals and writes the result back, so the
dashboard, the report and the morning email all read the same numbers.

Grading matches on ESPN's event id rather than team names, because the board
carries the id and an exact key cannot mismatch a Miami for a Miami (OH).

  python placements.py --grade     # settle finished games, push results back
  python placements.py --report    # the running record
  python placements.py --publish   # write public/data/placements.json

Needs NETLIFY_SITE_ID and NETLIFY_AUTH_TOKEN to reach the store.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional

import keys  # noqa: F401  (loads ~/.football-picks.env)
from archive import ARCHIVE_DIR, utcnow
from espn import current_season_year, fetch_completed_games
from odds import american_to_decimal, american_to_implied_prob

ROOT = os.path.dirname(os.path.abspath(__file__))
BLOBS_CLI = os.path.join(ROOT, "netlify", "blobs_cli.mjs")
LEAGUES = ("NFL", "NCAAF")


def mirror_path(season: Optional[int] = None) -> str:
    """
    A committed copy of the store.

    The blob store is the live source -- the browser writes to it and reads it
    back. But a store is invisible in a diff and gone if the site is deleted, so
    every graded run also writes the same rows into the repo beside the other
    ledgers. That copy is what --report reads when the store is unreachable,
    which is most of the time when running locally.
    """
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    return os.path.join(ARCHIVE_DIR, f"placed_{season or current_season_year()}.ndjson")


# ── the store ─────────────────────────────────────────────────────────────

def store_available() -> bool:
    return bool(os.environ.get("NETLIFY_SITE_ID") and os.environ.get("NETLIFY_AUTH_TOKEN"))


def fetch_placements() -> List[dict]:
    """Pull every placement from the blob store via the Node bridge."""
    if not store_available():
        raise RuntimeError("NETLIFY_SITE_ID / NETLIFY_AUTH_TOKEN not set")
    out = subprocess.run(
        ["node", BLOBS_CLI, "list"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    if out.returncode != 0:
        raise RuntimeError(f"blobs list failed: {out.stderr.strip()[:300]}")
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def push_placements(rows: List[dict]) -> None:
    if not rows:
        return
    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    out = subprocess.run(
        ["node", BLOBS_CLI, "put"], cwd=ROOT, input=payload,
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(f"blobs put failed: {out.stderr.strip()[:300]}")


def load_mirror() -> List[dict]:
    path = mirror_path()
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def save_mirror(rows: List[dict]) -> None:
    with open(mirror_path(), "w") as f:
        for r in sorted(rows, key=lambda r: (r.get("kickoff") or "", r.get("id") or "")):
            f.write(json.dumps(r, sort_keys=True) + "\n")


def load() -> List[dict]:
    """The store when it is reachable, the committed mirror when it is not."""
    if store_available():
        try:
            return fetch_placements()
        except Exception as e:
            print(f"[placed] store unreachable ({e}) — using the committed mirror")
    return load_mirror()


# ── grading ───────────────────────────────────────────────────────────────

def _margin(g, side: str) -> int:
    """Final margin from this side's point of view."""
    return (g.home_score - g.away_score) if side == "home" else (g.away_score - g.home_score)


def _leg_outcome(leg: dict, finals: Dict[str, object],
                 kind: str = "single") -> Optional[str]:
    """'won' | 'lost' | 'push', or None while the game is unfinished."""
    g = finals.get(str(leg.get("event_id")))
    if g is None or leg.get("side") not in ("home", "away"):
        return None

    if kind == "teaser":
        # A teased leg is graded against the line it was teased TO, which is
        # stored on the placement: ESPN's completed-game record carries no
        # spread, so a line not written down at tag time cannot be recovered
        # afterwards. `teased` is signed from this side's point of view, so a
        # leg lands whenever its own margin plus its line is positive.
        teased = leg.get("teased")
        if teased is None:
            return None
        result = _margin(g, leg["side"]) + float(teased)
        if result == 0:
            return "push"
        return "won" if result > 0 else "lost"

    if g.home_score == g.away_score:
        return "push"
    home_won = g.home_score > g.away_score
    return "won" if (home_won if leg["side"] == "home" else not home_won) else "lost"


def settle(bet: dict, finals: Dict[str, object]) -> Optional[dict]:
    """
    Settle one ticket. Returns None if it is not gradeable yet.

    A parlay pays once, if every leg lands. Three rules do the work:

      * one loss settles the whole ticket immediately, even with legs still to
        play -- there is nothing left to wait for;
      * a push drops its leg and the ticket re-prices at the remaining legs,
        which is how books settle them, so the payout falls rather than voids;
      * anything else waits. A 4-leg parlay 3-0 up is not a win yet.

    The price used is the one recorded when the bet was tagged, because that is
    what the ticket says. Only a push forces a recompute, and then only from the
    surviving legs.

    A TEASER settles by neither rule. Its legs have no individual price -- the
    ticket is priced once, as a unit -- so there is nothing to re-price from,
    and a push inside a two-team teaser is graded as a loss at most books,
    which is also the assumption teaser.py's win rates are measured under. Both
    differences are handled below rather than by pretending a teaser is a
    parlay of moneylines.
    """
    legs = bet.get("legs") or []
    if not legs:
        return None
    kind = bet.get("kind") or ("parlay" if len(legs) > 1 else "single")
    outcomes = [_leg_outcome(l, finals, kind) for l in legs]
    stake = float(bet.get("stake") or 0)

    bet["leg_results"] = [
        {
            "event_id": l.get("event_id"), "team_abbr": l.get("team_abbr"),
            "outcome": o, "result": (
                f"{finals[str(l['event_id'])].away_abbr} "
                f"{finals[str(l['event_id'])].away_score}-"
                f"{finals[str(l['event_id'])].home_score} "
                f"{finals[str(l['event_id'])].home_abbr}"
            ) if str(l.get("event_id")) in finals else None,
        }
        for l, o in zip(legs, outcomes)
    ]

    if kind == "teaser":
        if "lost" in outcomes or "push" in outcomes:
            # Push is a loss here, not a void. Books grade it that way inside a
            # two-team teaser and the measured 73.6% per leg already charges
            # for it, so voiding would credit an edge the number does not have.
            bet["status"], bet["units"] = "lost", round(-stake, 4)
        elif any(o is None for o in outcomes):
            return None
        else:
            dec = american_to_decimal(int(bet.get("odds") or 0))
            bet["status"] = "won"
            bet["units"] = round((dec - 1) * stake, 4)
    elif "lost" in outcomes:
        bet["status"], bet["units"] = "lost", round(-stake, 4)
    elif any(o is None for o in outcomes):
        return None  # still running
    elif all(o == "push" for o in outcomes):
        bet["status"], bet["units"] = "push", 0.0
    else:
        survivors = [l for l, o in zip(legs, outcomes) if o == "won"]
        if len(survivors) == len(legs):
            dec = american_to_decimal(int(bet.get("odds") or 0))
        else:
            # Re-price from the surviving legs: a pushed leg is removed from the
            # ticket, not counted as a winner.
            dec = 1.0
            for l in survivors:
                dec *= american_to_decimal(int(l.get("odds") or 0))
        bet["status"] = "won"
        bet["units"] = round((dec - 1) * stake, 4)

    done = [r for r in bet["leg_results"] if r["result"]]
    bet["result"] = " · ".join(r["result"] for r in done) if done else None
    bet["graded_at"] = utcnow()
    return bet


def _side_implied(odds: int) -> float:
    """Raw implied probability of one American price. Vig included."""
    o = int(odds)
    return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)


def track_clv(board: dict) -> int:
    """
    Refresh the latest pre-kickoff price on every bet the user tagged.

    WHY THIS EXISTS AT ALL. Win rate cannot measure a bettor inside a lifetime:
    the heaviest NFL favourites come along four times a season, and telling
    +2% from -2% at that price would take 193 of them. Closing-line value can.
    If a side is taken at +200 and closes at +150, the market moved toward it
    after the bet was made, and that is information the price did not contain
    when the bet was placed -- readable in weeks instead of decades, and the
    only measurement in this project that runs fast enough to judge a person.

    The model's own picks have been graded this way all along (see
    build_board.log_tracked_legs). This gives a hand-picked bet the same
    treatment, so "am I any good at this" becomes a question with an answer.

    Two units, because a teaser is not priced like a moneyline: moneyline legs
    move in probability points, a teased leg moves in points of spread.
    """
    rows = load()
    prices, spreads = {}, {}
    for league, lg in (board.get("leagues") or {}).items():
        for g in lg.get("games") or []:
            for leg in g.get("legs") or []:
                prices[(league, str(g.get("event_id")), leg.get("side"))] = (
                    leg.get("odds"), leg.get("implied_prob"))
            if g.get("spread") is not None:
                spreads[(league, str(g.get("event_id")))] = float(g["spread"])

    touched: List[dict] = []
    for bet in rows:
        if (bet.get("status") or "open") != "open":
            continue
        league, kind = bet.get("league"), (bet.get("kind") or "")
        changed = False
        for leg in bet.get("legs") or []:
            eid, side = str(leg.get("event_id")), leg.get("side")
            if kind == "teaser":
                cur = spreads.get((league, eid))
                if cur is None or leg.get("spread") is None:
                    continue
                # The board stores the home spread; this side's number mirrors it.
                cur_side = cur if side == "home" else -cur
                leg["close_spread"] = cur_side
                # Positive means the number taken was the better one: a side
                # taken at +2.5 that is now +3.5 got a point less than it could.
                leg["clv_pts"] = round(float(leg["spread"]) - cur_side, 2)
                changed = True
                continue
            quote = prices.get((league, eid, side))
            if not quote or quote[0] is None:
                continue
            odds, imp = quote
            leg.setdefault("open_odds", leg.get("odds"))
            leg.setdefault("open_implied", _side_implied(leg.get("open_odds") or odds))
            leg["close_odds"], leg["close_implied"] = odds, imp
            leg["clv_pp"] = round((imp - leg["open_implied"]) * 100.0, 2)
            changed = True

        if not changed:
            continue
        legs = bet.get("legs") or []
        if kind == "teaser":
            pts = [l.get("clv_pts") for l in legs if l.get("clv_pts") is not None]
            bet["clv_pts"] = round(sum(pts), 2) if pts else None
        else:
            # A ticket's CLV, not the sum of its legs': for a parlay the two
            # differ, because what moved is the chance of the whole thing.
            opens = [l.get("open_implied") for l in legs if l.get("open_implied") is not None]
            closes = [l.get("close_implied") for l in legs if l.get("close_implied") is not None]
            if opens and len(opens) == len(closes):
                po = pc = 1.0
                for o in opens:
                    po *= o
                for c in closes:
                    pc *= c
                bet["clv_pp"] = round((pc - po) * 100.0, 2)
        bet["priced_at"] = utcnow()
        touched.append(bet)

    if touched and store_available():
        try:
            push_placements(touched)
        except Exception as e:
            print(f"[placed] could not write closing prices back: {e}")
    if touched:
        save_mirror(rows)
    print(f"[placed] refreshed the live price on {len(touched)} open bet(s)")
    return len(touched)


def grade() -> int:
    rows = load()
    openers = [r for r in rows if (r.get("status") or "open") == "open"]
    if not openers:
        print("[placed] nothing open to grade")
        save_mirror(rows)
        return 0

    wanted = {r.get("league") for r in openers if r.get("league")} or set(LEAGUES)
    finals: Dict[str, object] = {}
    for league in wanted:
        try:
            for g in fetch_completed_games(league):
                finals[str(g.event_id)] = g
        except Exception as e:
            print(f"[placed] {league} results unavailable: {e}")

    graded = [b for b in openers if settle(b, finals) is not None]

    if graded and store_available():
        try:
            push_placements(graded)
        except Exception as e:
            print(f"[placed] could not write results back to the store: {e}")
    save_mirror(rows)
    print(f"[placed] graded {len(graded)}")
    return len(graded)


# ── reporting ─────────────────────────────────────────────────────────────

def bet_label(bet: dict) -> str:
    """'CIN ML' for a single, 'CIN + KC + PHI' for a parlay."""
    legs = bet.get("legs") or []
    if not legs:
        return "(no legs)"
    if (bet.get("kind") or "") == "teaser":
        pts = bet.get("points") or 6
        # The teased line, not the original number: that is what was bet, and
        # a teaser row that prints -7 next to a win at -1 reads as a lie.
        return (f"{pts:g}-pt teaser: "
                + " + ".join(f"{l.get('team_abbr') or '?'} "
                             f"{float(l.get('teased') or 0):+g}" for l in legs))
    if len(legs) == 1:
        return f"{legs[0].get('team_abbr') or '?'} ML  ({legs[0].get('matchup') or ''})"
    return f"{len(legs)}-leg: " + " + ".join(l.get("team_abbr") or "?" for l in legs)


def summarise(rows: List[dict]) -> dict:
    settled = [r for r in rows if (r.get("status") or "open") != "open"]
    won = [r for r in settled if r["status"] == "won"]
    lost = [r for r in settled if r["status"] == "lost"]
    push = [r for r in settled if r["status"] == "push"]
    # Staked excludes pushes: money returned was never at risk, and counting it
    # in the denominator flatters the return.
    risked = sum(float(r.get("stake") or 0) for r in won + lost)
    units = sum(float(r.get("units") or 0) for r in settled)
    openers = [r for r in rows if (r.get("status") or "open") == "open"]
    return {
        "placed": len(rows),
        "open": len(openers),
        "open_units": round(sum(float(r.get("stake") or 0) for r in openers), 2),
        "won": len(won), "lost": len(lost), "pushed": len(push),
        "units": round(units, 2),
        "staked": round(risked, 2),
        "roi_pct": round(units / risked * 100, 2) if risked else 0.0,
        "win_pct": round(len(won) / (len(won) + len(lost)) * 100, 1)
                   if (won or lost) else 0.0,
        # The edge the board claimed on the bets actually taken, which is the
        # honest comparison for the return above.
        "claimed_edge_pp": round(
            sum(float(r.get("edge_pp") or 0) for r in settled) / len(settled), 2
        ) if settled else 0.0,
    }


def report() -> None:
    rows = load()
    if not rows:
        print("\n  No bets tagged yet. Check one on the board and it lands here.")
        return
    s = summarise(rows)
    print(f"\n  Your bets")
    print(f"  {'tagged':<22}{s['placed']}")
    print(f"  {'open':<22}{s['open']}  ({s['open_units']:.2f}u at risk)")
    if not (s["won"] or s["lost"]):
        print(f"  {'settled':<22}none yet — results land after kickoff")
        return
    print(f"  {'record':<22}{s['won']}-{s['lost']}"
          + (f"-{s['pushed']}" if s["pushed"] else "")
          + f"  ({s['win_pct']:.0f}%)")
    print(f"  {'units':<22}{s['units']:+.2f} on {s['staked']:.2f}u risked")
    print(f"  {'return':<22}{s['roi_pct']:+.2f}%")
    print(f"  {'edge it claimed':<22}{s['claimed_edge_pp']:+.2f}pp")
    if s["won"] + s["lost"] < 30:
        print(f"  {'':<22}(thin — under 30 settled bets this is noise, not a verdict)")

    print(f"\n  {'bet':<40}{'odds':>7}{'stake':>7}{'units':>8}  status")
    for r in sorted(rows, key=lambda r: (r.get("kickoff") or "")):
        print(f"  {bet_label(r):<40}{int(r.get('odds') or 0):+7d}"
              f"{float(r.get('stake') or 0):>6.2f}u"
              f"{(f'{r["units"]:+.2f}' if r.get("units") is not None else '—'):>8}"
              f"  {(r.get('status') or 'open').upper()}")
        if r.get("leg_results"):
            for lr in r["leg_results"]:
                mark = {"won": "\u2713", "lost": "\u2717", "push": "="}.get(lr["outcome"], "\u00b7")
                print(f"      {mark} {lr.get('team_abbr') or '?':<6}"
                      f"{lr.get('result') or 'not final'}")


def publish(path: str = None) -> dict:
    """
    Write the graded placements next to the board.

    The dashboard reads placements live from the function, so this file is not
    what draws the checkboxes. It exists so the page can show results without a
    second round trip, and so the numbers survive the function being down.
    """
    rows = load()
    out = {
        "generated_at": utcnow(),
        "season": current_season_year(),
        "summary": summarise(rows),
        "placements": rows,
    }
    path = path or os.path.join(ROOT, "public", "data", "placements.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print(f"[placed] wrote {path} ({len(rows)} placements)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grade", action="store_true", help="settle finished games")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--publish", action="store_true",
                    help="write public/data/placements.json")
    args = ap.parse_args()
    if args.grade:
        grade()
    if args.publish:
        publish()
    if args.report or not (args.grade or args.publish):
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
