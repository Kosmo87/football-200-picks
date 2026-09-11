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

def _leg_outcome(leg: dict, finals: Dict[str, object]) -> Optional[str]:
    """'won' | 'lost' | 'push', or None while the game is unfinished."""
    g = finals.get(str(leg.get("event_id")))
    if g is None or leg.get("side") not in ("home", "away"):
        return None
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
    """
    legs = bet.get("legs") or []
    if not legs:
        return None
    outcomes = [_leg_outcome(l, finals) for l in legs]
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

    if "lost" in outcomes:
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
