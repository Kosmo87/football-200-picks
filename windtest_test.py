"""
The wind paper test's bookkeeping, pinned with the network stubbed.

What can go wrong quietly here: a snapshot overwriting `first` so the early
bet is lost, a game that kicked off still collecting a `close` from a stale
total, and a push counted as a loss.
"""

from datetime import datetime, timedelta, timezone

import windtest as W

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def game(eid, hours, total, kickoff=None):
    return {"event_id": eid, "game": f"G{eid}", "venue": "V", "city": "C", "state": "S",
            "kickoff": kickoff or (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ"),
            "total": total, "book": "DraftKings"}


def wx(wind):
    return {"wind_mph": wind, "gust_mph": wind + 8, "temp_f": 50, "precip_pct": 10}


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main():
    res = []
    ledger = []

    # Outside 24h: nothing. Inside: logged with first == close.
    n = W.record_snapshots([game("1", 30, 44.5)], {"1": wx(18)}, ledger, NOW)
    res.append(check("beyond 24h ignored", (n, len(ledger)), (0, 0)))
    W.record_snapshots([game("1", 20, 44.5)], {"1": wx(18)}, ledger, NOW)
    res.append(check("first snapshot taken", ledger[0]["first"]["total"], 44.5))

    # A later run moves close, never first.
    later = NOW + timedelta(hours=19)
    W.record_snapshots([game("1", 1, 42.5, ledger[0]["kickoff"])], {"1": wx(22)}, ledger, later)
    res.append(check("first kept", (ledger[0]["first"]["total"], ledger[0]["first"]["wind_mph"]),
                     (44.5, 18)))
    res.append(check("close moved", (ledger[0]["close"]["total"], ledger[0]["close"]["wind_mph"]),
                     (42.5, 22)))

    # After kickoff nothing more is written.
    W.record_snapshots([game("1", 0, 40.0, ledger[0]["kickoff"])], {"1": wx(30)},
                       ledger, NOW + timedelta(hours=21))
    res.append(check("no snapshot after kickoff", ledger[0]["close"]["total"], 42.5))

    # Missing total or forecast: skipped, not half-written.
    W.record_snapshots([game("2", 5, None), game("3", 5, 41.0)], {"2": wx(20), "3": None},
                       ledger, NOW)
    res.append(check("incomplete snapshots skipped", len(ledger), 1))

    # Grading: 41 lands under 42.5 (close) and under 44.5 (first).
    W.grade_row(ledger[0], 24, 17, at="x")
    res.append(check("close under won", ledger[0]["close"]["under"], "won"))
    res.append(check("first under won", ledger[0]["first"]["under"], "won"))

    # A push is a push, and the tally keeps it out of W-L.
    r = {"event_id": "4", "kickoff": "k", "game": "G4", "status": "pending",
         "first": {"total": 41.0, "wind_mph": 16}, "close": {"total": 41.0, "wind_mph": 16}}
    W.grade_row(r, 21, 20, at="x")
    res.append(check("push graded as push", r["close"]["under"], "push"))
    calm = {"event_id": "5", "kickoff": "k", "game": "G5", "status": "pending",
            "first": {"total": 40.0, "wind_mph": 5}, "close": {"total": 40.0, "wind_mph": 5}}
    W.grade_row(calm, 30, 20, at="x")
    t = W.tally(ledger + [r, calm])
    res.append(check("rule tally excludes calm and pushes",
                     (t["won"], t["lost"], t["push"], t["units"]), (1, 0, 1, 0.91)))
    t = W.tally(ledger + [r, calm], "close", 0, 10)
    res.append(check("calm band", (t["won"], t["lost"]), (0, 1)))

    print(f"\n  {sum(res)} passed, {len(res) - sum(res)} failed")
    return 0 if all(res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
