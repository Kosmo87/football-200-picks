"""
Tests for the video generator: the words and the numbers, not the pixels.

What must never break is the honesty of a clip. A video is the one output
nobody can check against the ledger while watching it, so the record has to
travel with the ticket, the intro has to be identical every time, and a route
the board cannot fill must never reach a scene.
"""

import reel as R


def check(name, got, want):
    ok = got == want
    # Scenes are whole HTML documents; printing one fills the terminal and
    # buries the other results.
    shown = repr(got)
    if len(shown) > 90:
        shown = shown[:87] + "..."
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {shown}" + ("" if ok else f" (want {want!r})"))
    return ok


def board(legs=4, price=265, joint=0.3032, pts="6", pool=None):
    """A board whose ladder wants `legs` and whose shortlist holds `pool`."""
    made = [
        {"team_abbr": f"T{i}", "spread": 2.5, "teased": 8.5, "matchup": f"A{i} @ T{i}",
         "prob": 0.7409, "event_id": str(i)}
        for i in range(pool if pool is not None else legs)
    ]
    return {"leagues": {"NFL": {"teasers": {
        "legs": made, "per_leg_rate": 0.7362, "per_leg_stderr": 0.0104,
        "ten": {"legs": made, "per_leg_rate": 0.7918},
        "ladder": {pts: {"prices": {str(legs): price}, "joint": {str(legs): joint}}},
    }}}}


def history(won=15, lost=30, roi=-23.3):
    return {"summary": {"won": won, "lost": lost, "roi_pct": roi, "units": -10.49,
                        "avg_clv_pp": -0.36}}


def main():
    r = []

    # The intro is a constant. A channel is recognised before it is read, so
    # this string changing silently would be a bug, not a tweak.
    a, b = R.intro_scene(), R.intro_scene()
    r.append(check("intro is deterministic", a, b))
    r.append(check("intro carries the brand line", R.INTRO_LINE in a, True))
    # "Finder", never "predictor": prediction is the thing this project
    # measured and lost at, so the brand must not promise it.
    r.append(check("brand says finder", "finder" in R.INTRO_LINE.lower(), True))
    r.append(check("brand avoids predictor", "predictor" in R.INTRO_LINE.lower(), False))

    # The record travels with the ticket. Without this a clip is a tout.
    t = R.ticket_scene(R.best_route(board(), "NFL", 265), "NFL", history())
    r.append(check("ticket shows the standing record", "RECORD SO FAR  15-30" in t, True))
    r.append(check("ticket shows the losing return", "(-23% RETURN)" in t, True))
    r.append(check("ticket names its legs", t.count("class='leg'"), 4))

    # An empty ledger says so rather than printing 0-0 at 0%.
    r.append(check("no record yet is said out loud",
                   R.record_line({}), "NO SETTLED RECORD YET"))

    # A route the board cannot fill must not reach a scene: the video would be
    # naming legs that do not exist.
    # The ladder wants four legs; the board only has two of them.
    r.append(check("unfillable route refused",
                   R.best_route(board(legs=4, pool=2), "NFL", 265), None))

    # Nor may a route pay materially less than asked. 12% under is the site's
    # tolerance; half the payout is not.
    r.append(check("route far under the ask refused",
                   R.best_route(board(legs=4, price=120), "NFL", 300), None))

    # The chance comes from the measured joint rate, never from multiplying.
    route = R.best_route(board(), "NFL", 265)
    r.append(check("chance is the measured joint rate", route["prob"], 0.3032))

    # A winning record should not be printed in the losing colour.
    good = R.record_scene(history(won=30, lost=15, roi=12.5))
    r.append(check("a winning record reads positive", "big pos" in good, True))
    r.append(check("a losing record reads negative", "big neg" in R.record_scene(history()), True))

    # Escaping: team names come from a feed, and a feed is not trusted markup.
    bad = board()
    bad["leagues"]["NFL"]["teasers"]["legs"][0]["matchup"] = "<script>x</script>"
    scene = R.ticket_scene(R.best_route(bad, "NFL", 265), "NFL", history())
    r.append(check("feed text is escaped", "<script>" in scene, False))

    # Batch mode: every fillable ticket, and nothing that loses money.
    many = R.all_routes(board(legs=4, pool=6), "NFL")
    r.append(check("all_routes finds the fillable ticket", len(many), 1))
    r.append(check("a ticket needing more legs than the board has is dropped",
                   R.all_routes(board(legs=6, pool=3), "NFL"), []))

    # Filenames carry the ticket, so a folder of clips is legible and the
    # skip-if-already-done check has something stable to match on.
    name = R.slug("NFL", many[0], "2026-09-18T14:01:38+00:00")
    r.append(check("slug names the ticket", name, "nfl-6pt-4leg-+265-2026-09-18"))

    # The call to action points at the site and says what is free about it.
    cta = R.cta_scene("NFL", board())
    r.append(check("cta carries the url", R.SITE_URL in cta, True))
    r.append(check("cta says it will not stay free", "not the plan forever" in cta, True))
    # No dated promise, ever: an invented deadline is the one claim that would
    # cost this channel the honesty it trades on.
    for word in ("Friday", "tonight", "24 hours", "midnight", "last chance"):
        r.append(check(f"cta invents no deadline ({word})", word in cta, False))

    print(f"\n  {sum(r)} passed, {len(r) - sum(r)} failed")
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
