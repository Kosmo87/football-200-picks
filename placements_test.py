"""
Settlement rules for tagged bets.

The arithmetic here decides what a ticket paid, so it gets pinned rather than
eyeballed. The push rule especially: a pushed leg drops out and the parlay
re-prices at the survivors, which is easy to write as "treat it as a win" and
then quietly overstates the return for the rest of the season.
"""

from placements import settle, summarise, bet_label


class Final:
    def __init__(self, home_abbr, away_abbr, home_score, away_score):
        self.home_abbr, self.away_abbr = home_abbr, away_abbr
        self.home_score, self.away_score = home_score, away_score


def finals(*games):
    return {str(i + 1): g for i, g in enumerate(games)}


def leg(event_id, side, abbr, odds):
    return {"event_id": event_id, "side": side, "team_abbr": abbr, "odds": odds,
            "matchup": f"{abbr} game", "kickoff": "2026-09-13T17:00Z"}


def bet(*legs, odds, stake=1.0):
    return {"league": "NFL", "legs": list(legs), "odds": odds, "stake": stake,
            "status": "open"}


def tleg(event_id, side, abbr, teased):
    """A teased leg: no price of its own, and the line it was moved TO."""
    return {"event_id": event_id, "side": side, "team_abbr": abbr,
            "teased": teased, "matchup": f"{abbr} game",
            "kickoff": "2026-09-20T17:00Z"}


def teaser(*legs, odds, stake=1.0):
    return {"league": "NFL", "kind": "teaser", "points": 6,
            "legs": list(legs), "odds": odds, "stake": stake, "status": "open"}


def check(name, got, want):
    ok = abs(got - want) < 1e-6 if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main():
    results = []

    # A single that wins pays the price on the stake.
    b = bet(leg("1", "home", "CIN", -198), odds=-198, stake=2.0)
    settle(b, finals(Final("CIN", "TB", 24, 17)))
    results.append(check("single won status", b["status"], "won"))
    results.append(check("single won units", b["units"], round((1 + 100 / 198 - 1) * 2, 4)))

    # A single that loses costs exactly the stake, whatever the price was.
    b = bet(leg("1", "away", "TB", 164), odds=164, stake=2.0)
    settle(b, finals(Final("CIN", "TB", 24, 17)))
    results.append(check("single lost units", b["units"], -2.0))

    # A parlay pays the recorded combined price, not the sum of its legs.
    b = bet(leg("1", "home", "CIN", -198), leg("2", "home", "KC", -150),
            odds=118, stake=1.0)
    settle(b, finals(Final("CIN", "TB", 24, 17), Final("KC", "LV", 30, 10)))
    results.append(check("parlay won status", b["status"], "won"))
    results.append(check("parlay won units", b["units"], 1.18))

    # One loss settles the ticket even with a leg still to play: nothing the
    # unplayed game can do brings it back.
    b = bet(leg("1", "home", "CIN", -198), leg("2", "home", "KC", -150),
            odds=118, stake=1.0)
    settle(b, finals(Final("CIN", "TB", 17, 24)))
    results.append(check("one loss settles early", b["status"], "lost"))
    results.append(check("early loss units", b["units"], -1.0))

    # A parlay ahead on every finished leg is NOT a win while one is unplayed.
    b = bet(leg("1", "home", "CIN", -198), leg("2", "home", "KC", -150),
            odds=118, stake=1.0)
    results.append(check("incomplete stays open",
                         settle(b, finals(Final("CIN", "TB", 24, 17))), None))

    # A pushed leg drops out and the ticket re-prices at the survivor. Treating
    # it as a winner would pay 1.18u here instead of 0.51u.
    b = bet(leg("1", "home", "CIN", -198), leg("2", "home", "KC", -150),
            odds=118, stake=1.0)
    settle(b, finals(Final("CIN", "TB", 21, 21), Final("KC", "LV", 30, 10)))
    results.append(check("push drops its leg", b["status"], "won"))
    results.append(check("push re-prices to the survivor",
                         b["units"], round(100 / 150, 4)))

    # Every leg pushed is a void ticket, not a win.
    b = bet(leg("1", "home", "CIN", -198), odds=-198, stake=1.0)
    settle(b, finals(Final("CIN", "TB", 21, 21)))
    results.append(check("all pushed is a push", b["status"], "push"))
    results.append(check("pushed units", b["units"], 0.0))

    # Pushes leave the denominator alone: money returned was never at risk.
    s = summarise([
        {"status": "won", "stake": 1.0, "units": 1.5, "edge_pp": 6},
        {"status": "lost", "stake": 1.0, "units": -1.0, "edge_pp": 6},
        {"status": "push", "stake": 5.0, "units": 0.0, "edge_pp": 6},
    ])
    results.append(check("push excluded from staked", s["staked"], 2.0))
    results.append(check("roi ignores pushes", s["roi_pct"], 25.0))

    results.append(check("single label", bet_label(bet(leg("1", "home", "CIN", -198), odds=-198)),
                         "CIN ML  (CIN game)"))
    results.append(check("parlay label",
                         bet_label(bet(leg("1", "home", "CIN", -198),
                                       leg("2", "home", "KC", -150), odds=118)),
                         "2-leg: CIN + KC"))

    # ── teasers ────────────────────────────────────────────────────────────
    #
    # A teased leg is graded against the line it was moved to, and the line is
    # only on the record because it was stored at tag time -- a finished game
    # carries no spread.

    # ATL +8.5 loses by 3: covered. PHI -1 wins by 7: covered. Pays the ticket
    # price once, not the product of anything.
    b = teaser(tleg("1", "away", "ATL", 8.5), tleg("2", "home", "PHI", -1.0),
               odds=-110, stake=1.0)
    settle(b, finals(Final("CAR", "ATL", 24, 21), Final("PHI", "TEN", 28, 21)))
    results.append(check("teaser won status", b["status"], "won"))
    results.append(check("teaser won units", b["units"], round(100 / 110, 4)))

    # The teased number is what decides it: ATL +8.5 covers a 6-point loss
    # although the moneyline lost outright. Grading this as a moneyline would
    # call the same ticket a loser.
    b = teaser(tleg("1", "away", "ATL", 8.5), odds=-110)
    settle(b, finals(Final("CAR", "ATL", 27, 21)))
    results.append(check("teased dog covers a loss", b["status"], "won"))

    # One leg short is a lost ticket, whatever the other did.
    b = teaser(tleg("1", "away", "ATL", 8.5), tleg("2", "home", "PHI", -1.0),
               odds=-110, stake=2.0)
    settle(b, finals(Final("CAR", "ATL", 24, 21), Final("PHI", "TEN", 21, 28)))
    results.append(check("teaser one leg short", b["status"], "lost"))
    results.append(check("teaser loss costs the stake", b["units"], -2.0))

    # A PUSH IS A LOSS, not a void and not a re-price. Most books grade it that
    # way inside a two-teamer, and the 73.6% per-leg rate is measured under the
    # same assumption -- voiding here would credit an edge the number does not
    # have. PHI -1 winning by exactly 1 is the case.
    b = teaser(tleg("1", "away", "ATL", 8.5), tleg("2", "home", "PHI", -1.0),
               odds=-110, stake=1.0)
    settle(b, finals(Final("CAR", "ATL", 24, 21), Final("PHI", "TEN", 22, 21)))
    results.append(check("teaser push is a loss", b["status"], "lost"))
    results.append(check("teaser push units", b["units"], -1.0))

    # A teased leg has no price of its own, so nothing may try to price one:
    # this is the ZeroDivisionError the moneyline re-pricing branch would hit.
    b = teaser(tleg("1", "away", "ATL", 8.5), tleg("2", "home", "PHI", -1.0),
               odds=-110, stake=1.0)
    graded = settle(b, finals(Final("CAR", "ATL", 24, 21)))
    results.append(check("teaser waits for the second game", graded, None))

    results.append(check("teaser label",
                         bet_label(teaser(tleg("1", "away", "ATL", 8.5),
                                          tleg("2", "home", "PHI", -1.0),
                                          odds=-110)),
                         "6-pt teaser: ATL +8.5 + PHI -1"))

    print(f"\n  {sum(results)} passed, {len(results) - sum(results)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
