"""
Does the model know anything the market has not priced yet?

Beating the closing line is the hardest bar in betting, and two tests have said
we cannot. This asks a different and more forgiving question. Lines open before
the market has seen action and move as money arrives. If our disagreement with
the OPENING number predicts the direction the line subsequently moves, then the
model holds information the market later discovers -- which is real edge, and is
bettable at the open even if the close is unbeatable.

If it does not, there is nothing here, because a model that cannot anticipate
the market's own correction certainly cannot beat its final answer.

  python clv_test.py --seasons 2023 2024 2025
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import mean
from typing import Dict, List

from boxscores import load_boxscores
from cfb_lines import load_game_lines
from efficiency import build_ratings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025])
    args = ap.parse_args()

    lines = {
        g.game_id: g for g in load_game_lines()
        if g.home_spread is not None and g.open_home_spread is not None
    }
    print(f"{len(lines):,} games with both an opening and a closing spread\n")

    rows: List[dict] = []
    for season in args.seasons:
        box = load_boxscores("NCAAF", season)
        prior = []
        for yr in range(season - 1, season):
            try:
                prior.extend(load_boxscores("NCAAF", yr))
            except Exception:
                pass

        by_event: Dict[str, list] = defaultdict(list)
        for r in box:
            by_event[r.event_id].append(r)

        by_day: Dict[str, list] = defaultdict(list)
        for eid, pair in by_event.items():
            if len(pair) != 2 or eid not in lines:
                continue
            home = next((p for p in pair if p.is_home), None)
            away = next((p for p in pair if not p.is_home), None)
            if home and away:
                by_day[home.date[:10]].append((eid, home, away))

        for day in sorted(by_day):
            train = prior + [r for r in box if r.date and r.date[:10] < day]
            if len(train) < 200:
                continue
            rt = build_ratings(train)
            for eid, home, away in by_day[day]:
                gl = lines[eid]
                rows.append({
                    "pred": rt.expected_margin(home.team_id, away.team_id, home.neutral),
                    "open": -gl.open_home_spread,   # implied home margin at open
                    "close": -gl.home_spread,       # implied home margin at close
                    "actual": home.points - away.points,
                })
        print(f"{season}: {sum(1 for d in by_day.values() for _ in d):,} games matched")

    if not rows:
        print("no overlap")
        return 1

    moved = [r for r in rows if r["close"] != r["open"]]
    print(f"\n=== {len(rows):,} games, {len(moved):,} where the line moved ===\n")

    print(f"  {'accuracy vs the OPENING number':<34} {'MAE':>7}")
    for name, f in [("opening line", lambda r: r["open"] - r["actual"]),
                    ("efficiency model", lambda r: r["pred"] - r["actual"]),
                    ("closing line", lambda r: r["close"] - r["actual"])]:
        print(f"  {name:<34} {mean(abs(f(r)) for r in rows):>7.2f}")

    # The question that matters: when we disagree with the open, does the market
    # subsequently move our way?
    print(f"\n  Does the line move toward the model? (50% = no information)")
    print(f"  {'model vs open':>16} {'n':>6} {'moved to us':>13} {'avg move':>10}")
    for lo, hi in [(0.5, 2), (2, 4), (4, 7), (7, 100)]:
        sub = [r for r in moved if lo <= abs(r["pred"] - r["open"]) < hi]
        if len(sub) < 30:
            continue
        toward = sum(
            1 for r in sub
            if (r["close"] - r["open"]) * (r["pred"] - r["open"]) > 0
        )
        avg = mean((r["close"] - r["open"]) * (1 if r["pred"] > r["open"] else -1)
                   for r in sub)
        print(f"  {lo:>7}-{hi:<5} pts {len(sub):>6} {toward/len(sub)*100:>12.1f}% "
              f"{avg:>+9.2f}")

    toward_all = sum(
        1 for r in moved if (r["close"] - r["open"]) * (r["pred"] - r["open"]) > 0
    )
    print(f"\n  Overall: line moved toward the model in {toward_all:,}/{len(moved):,} "
          f"({toward_all/len(moved)*100:.1f}%)")

    # And the money question: bet the opener where we disagree.
    print(f"\n  Betting the OPENING number, flat 1u at -110:")
    print(f"  {'model vs open':>16} {'n':>6} {'won':>6} {'ROI':>8}")
    for lo, hi in [(0.5, 2), (2, 4), (4, 7), (7, 100)]:
        sub = [r for r in rows if lo <= abs(r["pred"] - r["open"]) < hi]
        if len(sub) < 30:
            continue
        push = sum(1 for r in sub if r["actual"] == r["open"])
        won = sum(1 for r in sub if r["actual"] != r["open"]
                  and (r["pred"] > r["open"]) == (r["actual"] > r["open"]))
        lost = len(sub) - won - push
        roi = (won * (100 / 110) - lost) / (won + lost) * 100 if (won + lost) else 0
        print(f"  {lo:>7}-{hi:<5} pts {len(sub):>6} {won:>6} {roi:>+7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
