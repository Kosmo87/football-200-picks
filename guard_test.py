"""
The __main__ guard must be the last thing in every entry point.

This broke three separate files in one session. Appending helpers to a module
puts them BELOW `raise SystemExit(main())`, so main() runs before they are
defined and dies on NameError -- but only on the code path that calls them, so
a dry run can pass while the real send fails. Cheaper to assert than to
rediscover.
"""

import os
import re

ENTRY_POINTS = ("delivery.py", "confidence.py", "teaser.py", "parlays.py",
                "futures.py", "rankings.py", "shop_ledger.py")
ROOT = os.path.dirname(os.path.abspath(__file__))


def test_guard_is_last():
    for name in ENTRY_POINTS:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        src = open(path).read().rstrip()
        assert 'if __name__ == "__main__":' in src, f"{name} has no guard"
        tail = src[src.index('if __name__ == "__main__":'):]
        # nothing but the guard body may follow it
        leftovers = [l for l in tail.splitlines()[1:]
                     if l and not l.startswith((" ", "\t"))]
        assert not leftovers, (
            f"{name}: {len(leftovers)} top-level statement(s) after the guard "
            f"-- first is {leftovers[0][:40]!r}")


def test_every_entry_point_defines_main():
    for name in ENTRY_POINTS:
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            assert re.search(r"^def main\(", open(path).read(), re.M), name


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ok   {n}")
            except AssertionError as e:
                f += 1; print(f"  FAIL {n}: {e}")
    print(f"\n{f} failure(s)")
    raise SystemExit(1 if f else 0)
