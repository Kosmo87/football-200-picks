#!/usr/bin/env python3
"""Compare the two engines' output with a tolerance, not a rounding.

The browser re-implements pick selection, and a real drift there means the site
shows different tips than the Python engine that logs the ledger. That is worth
failing CI over. A difference in the fifteenth significant figure is not.

The previous check rounded both sides to one decimal and diffed the strings.
That reads like a tolerance and behaves like a cliff: board.json stores edge_pp
to two decimals, so roughly one value in thirty sits exactly on a .x5 boundary,
and the engines land on opposite sides of it -- JS rounds the stored double half
away from zero, Python arrives via a /100 then *100 round-trip that puts it a
few bits lower. Nothing is wrong with either engine; the comparison was asking a
question neither could answer stably.

Discrete decisions -- which picks, in what order, which legs, the trust label,
the combined odds -- are still compared exactly. Those are the things that must
never differ. Only the continuous quantities get the tolerance.
"""
import json
import sys

# Far tighter than anything that could change a displayed number or a decision,
# and far looser than double-precision noise (~1e-16 relative).
TOL = 1e-9
NUMERIC = {"conf", "edge", "units", "winp", "mktp"}


def close(a, b):
    if a == b:
        return True
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False
    return abs(a - b) <= max(TOL, TOL * max(abs(a), abs(b)))


def compare(js, py, problems):
    if set(js) != set(py):
        problems.append(f"different leagues: {sorted(js)} vs {sorted(py)}")
        return
    for league in sorted(js):
        a, b = js[league], py[league]
        if len(a) != len(b):
            problems.append(f"{league}: {len(a)} picks vs {len(b)}")
            continue
        for i, (x, y) in enumerate(zip(a, b)):
            for key in sorted(set(x) | set(y)):
                u, v = x.get(key), y.get(key)
                if key in NUMERIC:
                    if not close(u, v):
                        problems.append(
                            f"{league} pick {i} {key}: js={u!r} py={v!r} "
                            f"(diff {abs(u - v):.3g}, tolerance {TOL:g})")
                elif u != v:
                    problems.append(f"{league} pick {i} {key}: js={u!r} py={v!r}")


def main() -> int:
    with open(sys.argv[1]) as f:
        js = json.load(f)
    with open(sys.argv[2]) as f:
        py = json.load(f)

    problems = []
    compare(js, py, problems)
    if problems:
        print("ENGINE PARITY FAILED")
        for p in problems:
            print(f"  {p}")
        return 1

    n = sum(len(v) for v in js.values())
    print(f"parity OK — {n} picks across {len(js)} leagues agree within {TOL:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
