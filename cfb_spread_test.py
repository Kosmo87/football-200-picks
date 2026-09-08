"""
The NCAAF spread test at real scale: 1,800+ games against Pinnacle, not 101.

Walk-forward by game day. Efficiency ratings see only box scores that closed
before the day being predicted, then the model's expected margin is compared to
that game's closing spread and to what actually happened.

  python cfb_spread_test.py --seasons 2024 2025
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import mean
from typing import Dict, List

from boxscores import load_boxscores
from cfb_lines import load_game_lines
from efficiency import build_ratings


def ats_record(rows):
    """Wins, pushes and losses from backing whichever side the model prefers."""
    push = sum(1 for r in rows if r["actual"] == r["market"])
    won = sum(
        1 for r in rows
        if r["actual"] != r["market"]
        and (r["pred"] > r["market"]) == (r["actual"] > r["market"])
    )
    return won, push, len(rows) - won - push


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--prior", type=int, default=1)
    args = ap.parse_args()

    lines = {g.game_id: g for g in load_game_lines() if g.home_spread is not None}
    print(f"{len(lines):,} games carry a closing spread\n")

    rows: List[dict] = []
    for season in args.seasons:
        box = load_boxscores("NCAAF", season)
        prior = []
        for yr in range(season - args.prior, season):
            try:
                prior.extend(load_boxscores("NCAAF", yr))
            except Exception:
                pass

        by_event: Dict[str, list] = defaultdict(list)
        for r in box:
            by_event[r.event_id].append(r)

        games = []
        for eid, pair in by_event.items():
            if len(pair) != 2 or eid not in lines:
                continue
            home = next((p for p in pair if p.is_home), None)
            away = next((p for p in pair if not p.is_home), None)
            if home and away:
                games.append((home.date, eid, home, away))
        games.sort()
        print(f"{season}: {len(games):,} games with both a box score and a line")

        # Rebuild once per day rather than per game: same information cutoff,
        # a fraction of the work.
        by_day: Dict[str, list] = defaultdict(list)
        for date, eid, home, away in games:
            by_day[date[:10]].append((eid, home, away))

        for day in sorted(by_day):
            train = prior + [r for r in box if r.date and r.date[:10] < day]
            if len(train) < 200:
                continue
            rt = build_ratings(train)
            for eid, home, away in by_day[day]:
                gl = lines[eid]
                rows.append({
                    "season": season,
                    "pred": rt.expected_margin(home.team_id, away.team_id, home.neutral),
                    "market": -gl.home_spread,       # spread is negative when home is favoured
                    "actual": home.points - away.points,
                    "book": gl.book,
                })

    if not rows:
        print("no overlap")
        return 1

    print(f"\n=== {len(rows):,} games scored ===\n")
    print(f"  {'predicting the margin':<34} {'MAE':>7} {'RMSE':>7}")
    for name, f in [
        ("home team by 0 (naive)", lambda r: -r["actual"]),
        ("efficiency model", lambda r: r["pred"] - r["actual"]),
        ("closing spread", lambda r: r["market"] - r["actual"]),
    ]:
        m = mean(abs(f(r)) for r in rows)
        rm = mean(f(r) ** 2 for r in rows) ** 0.5
        print(f"  {name:<34} {m:>7.2f} {rm:>7.2f}")

    print(f"\n  Against the spread, flat 1u at -110:")
    print(f"  {'model disagrees by':>20} {'n':>6} {'won':>6} {'push':>6} {'ROI':>8}")
    for lo, hi in [(0, 2), (2, 4), (4, 7), (7, 10), (10, 100)]:
        sub = [r for r in rows if lo <= abs(r["pred"] - r["market"]) < hi]
        if len(sub) < 30:
            continue
        w, p, l = ats_record(sub)
        staked = w + l
        roi = (w * (100 / 110) - l) / staked * 100 if staked else 0
        print(f"  {lo:>8}-{hi:<4} pts {len(sub):>6} {w:>6} {p:>6} {roi:>+7.1f}%")

    w, p, l = ats_record(rows)
    staked = w + l
    print(f"\n  All {len(rows):,} games: {w}-{l}-{p} against the spread, "
          f"ROI {(w * (100/110) - l) / staked * 100:+.1f}%")

    pin = [r for r in rows if r["book"] == "PINNACLE"]
    if len(pin) > 100:
        print(f"\n  Pinnacle-only subset ({len(pin):,} games):")
        print(f"    closing spread   MAE {mean(abs(r['market']-r['actual']) for r in pin):.2f}")
        print(f"    efficiency model MAE {mean(abs(r['pred']-r['actual']) for r in pin):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
