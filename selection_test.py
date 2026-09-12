"""
The two rules that decide when a parlay is allowed to exist.

Both were written after uncapping the pick list exposed what the engine does
when it is allowed to keep going, and both are easy to regress silently because
the failure looks like a longer list rather than an error.
"""

from types import SimpleNamespace

from picks import ConfidenceConfig, Leg, _is_minimal_parlay, build_picks
import picks as picks_mod
from odds import combine_odds


def leg(abbr, odds, prob, market, event, sample=8.0):
    game = SimpleNamespace(event_id=event, name=f"{abbr} game", short_name=abbr,
                           date="2026-09-13T17:00Z", status="pre",
                           home=SimpleNamespace(id=f"{abbr}h", name=abbr, abbreviation=abbr),
                           away=SimpleNamespace(id=f"{abbr}a", name="OPP", abbreviation="OPP"),
                           neutral=False)
    l = Leg(game=game, side="home", team_name=abbr, team_abbr=abbr, team_id=f"{abbr}h",
            odds_american=odds, model_win_prob=prob, implied_prob=market,
            edge=(prob - market), fair_odds=0, sample_games=sample,
            team_sample=sample, opp_sample=sample)
    l.confidence = 60.0
    return l


def run(legs, cfg=None):
    cfg = cfg or ConfidenceConfig(min_sample_games=0)
    picks_mod.get_all_legs = lambda *a, **k: legs
    return build_picks([], None, cfg=cfg)


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main():
    r = []

    # --- minimality -------------------------------------------------------
    # Two legs neither of which reaches +200 alone: the pair is minimal.
    a, b = leg("A", 120, 0.50, 0.44, "1"), leg("B", 130, 0.50, 0.43, "2")
    r.append(check("two short legs are minimal", _is_minimal_parlay([a, b], 200), True))

    # Add a third when the first two already clear +200: the third buys nothing.
    c = leg("C", 110, 0.52, 0.46, "3")
    r.append(check("pair already clears +200", combine_odds([a.odds_american, b.odds_american]) >= 200, True))
    r.append(check("third leg makes it non-minimal", _is_minimal_parlay([a, b, c], 200), False))

    # A single is always minimal — nothing can be dropped.
    r.append(check("single is minimal", _is_minimal_parlay([a], 200), True))

    # --- a leg that can stand alone is never stacked ----------------------
    # D pays +360 by itself, so it has nothing to reach for. Its single is
    # rejected on stake (the model gives it no edge worth backing), and it must
    # NOT come back inside a parlay.
    d = leg("D", 360, 0.23, 0.22, "4")
    e = leg("E", 140, 0.44, 0.41, "5")
    f = leg("F", 150, 0.44, 0.40, "6")
    picks = run([d, e, f])
    used = {l.team_abbr for p in picks for l in p.legs}
    r.append(check("single-eligible leg is not recycled into a parlay",
                   "D" in used and any(len(p.legs) > 1 and any(l.team_abbr == "D" for l in p.legs)
                                       for p in picks),
                   False))

    # --- uncapped ---------------------------------------------------------
    # Six independent singles that each clear +200 and each size to a stake:
    # all six should come back, not the first three.
    #
    # The prices matter. An earlier draft used +260 at a 14pp claimed edge and
    # got zero picks -- correctly, because the disagreement damper cuts a stake
    # that far from the market to nothing. The test data has to be a bet the
    # engine would actually take, or it tests the damper instead of the cap.
    # 0.38 - 0.32, not 0.35 - 0.30: the latter is 4.999...pp in binary floating
    # point and falls under the 5pp gate, so the test silently measured nothing.
    many = [leg(f"S{i}", 250, 0.38, 0.32, str(100 + i)) for i in range(6)]
    picks = run(many)
    r.append(check("uncapped returns every qualifying single", len(picks), 6))

    # And an explicit n still truncates, for callers that want a short list.
    picks_mod.get_all_legs = lambda *a, **k: many
    r.append(check("explicit n still truncates",
                   len(build_picks([], None, n=2, cfg=ConfidenceConfig(min_sample_games=0))), 2))

    # --- a bet we do not believe is refused, not shrunk --------------------
    # The damper used to express distrust in the stake: a 12pp disagreement
    # sized to 0.3U beside a 3pp one at 0.5U. Half-unit stakes round both to
    # half a unit, so the warning has to be a refusal instead.
    from staking import stake_units, trust_label
    low = leg("L", -130, 0.661, 0.543, "9")        # 11.8pp apart: Low trust
    r.append(check("the two stakes really are indistinguishable now",
                   stake_units(0.661, -130, 0.543) == stake_units(0.60, -130, 0.57),
                   True))
    r.append(check("that leg is Low trust", trust_label(0.661, 0.543), "Low"))
    r.append(check("and is refused", len(run([low])), 0))

    # Medium still plays.
    med = leg("M", 250, 0.38, 0.32, "10")
    r.append(check("Medium trust still plays", len(run([med])), 1))

    # --- no pick reuses a team or a game ----------------------------------
    picks = run(many)
    ids = [l.team_id for p in picks for l in p.legs]
    r.append(check("no team appears twice", len(ids), len(set(ids))))
    events = [l.game.event_id for p in picks for l in p.legs]
    r.append(check("no game appears twice", len(events), len(set(events))))

    print(f"\n  {sum(r)} passed, {len(r) - sum(r)} failed")
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
