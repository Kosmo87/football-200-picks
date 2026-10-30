"""
Tests for the stale-line detector.

Two mistakes it already made and must not repeat: calling a normal mirrored
spread a transposition, and reporting both sides of a fault as if both were
bets.
"""

import faults as F


def test_only_the_side_that_gains_is_a_bet():
    """
    DraftKings at Oklahoma -23.5 against a -21.5 market is two rows and one
    bet. New Mexico +23.5 is two points better than the market; Oklahoma
    -23.5 is two points WORSE.
    """
    good = {"kind": "STALE", "market": "spreads", "side": "New Mexico Lobos",
            "book_point": 23.5, "consensus_point": 21.5, "delta": 2.0,
            "price": -110, "book": "betmgm", "game": "A @ B", "kickoff": "x"}
    bad = dict(good, side="Oklahoma Sooners", book_point=-23.5,
               consensus_point=-21.5, delta=-2.0)
    assert F.bet_from(good) is not None
    assert F.bet_from(bad) is None, "laying MORE points than the market is not a bet"


def test_mirrored_line_is_stale_not_transposed():
    """Detroit -16.5 and New Orleans +16.5 is one coherent line."""
    assert F.classify({"market": "spreads", "book_point": -16.5,
                       "consensus_point": -21.5}) == "STALE"


def test_flipped_favourite_is_transposed():
    assert F.classify({"market": "spreads", "book_point": 6.0,
                       "consensus_point": -5.5}) == "TRANSPOSED"


def test_moneyline_transposition_uses_the_50pct_line():
    assert F.classify({"market": "h2h", "book_implied": 0.40,
                       "consensus": 0.64}) == "TRANSPOSED"
    assert F.classify({"market": "h2h", "book_implied": 0.58,
                       "consensus": 0.64}) == "STALE"


BASE = {"kind": "STALE", "market": "spreads", "side": "X", "price": -110,
        "book": "betmgm", "game": "A @ B", "kickoff": "x"}


def _bet(gain):
    return F.bet_from(dict(BASE, book_point=3.0 + gain, consensus_point=3.0,
                           delta=gain))


def test_small_gains_do_not_beat_the_juice():
    """
    Half a point is worth about 1.5 points of win probability and -110 costs
    2.4, so it is not a bet. The break-even gain is ~0.8 points, which is why
    the 2.0-point scan threshold has headroom rather than sitting on the edge.
    """
    assert _bet(0.50) is None
    assert _bet(0.75) is None
    assert _bet(1.00) is not None


def test_more_points_gained_means_a_bigger_stake():
    small, big = _bet(1.0), _bet(3.0)
    assert big["units"] > small["units"]
    assert big["chance"] > small["chance"]


def test_stake_is_capped():
    huge = F.bet_from({"kind": "STALE", "market": "spreads", "side": "X",
                       "book_point": 14.0, "consensus_point": 3.0, "delta": 11.0,
                       "price": -110, "book": "betmgm", "game": "A @ B",
                       "kickoff": "x"})
    assert huge["units"] <= 3.0, "no single stale line justifies the bankroll"


def test_transposed_alerts_and_leads():
    """
    These used to be silenced as "voidable", which had the arithmetic backwards:
    a void RETURNS THE STAKE, so the break-even honour rate is zero. They also
    vanish fastest, which makes the alert more useful, not less.
    """
    hits = [
        {"market": "spreads", "book": "betmgm", "side": "Arizona State",
         "book_point": 5.0, "consensus_point": -5.5, "price": -110, "delta": 10.5,
         "game": "Kansas @ Arizona State", "kickoff": "2026-09-19T16:00:00Z",
         "kind": "TRANSPOSED"},
        {"market": "spreads", "book": "betmgm", "side": "New Mexico",
         "book_point": 23.5, "consensus_point": 21.5, "price": -110, "delta": 2.0,
         "game": "New Mexico @ Oklahoma", "kickoff": "2026-09-19T23:30:00Z",
         "kind": "STALE"},
    ]
    msg = F.alert_text(hits)
    assert msg is not None
    assert "WRONG TEAM FAVOURED" in msg, msg          # it leads
    assert "Arizona State" in msg, msg
    assert "void returns the stake" in msg, msg       # and says what it is


def test_transposed_is_staked_smaller_than_kelly_wants():
    """
    Kelly prices the game, not the counterparty. A book reviewing an obvious
    error voids the ticket that made it worth reviewing.
    """
    t = {"market": "spreads", "book": "betmgm", "side": "Arizona State",
         "book_point": 5.0, "consensus_point": -5.5, "price": -110, "delta": 10.5,
         "game": "g", "kickoff": "k", "kind": "TRANSPOSED"}
    stale = dict(t, kind="STALE", book_point=23.5, consensus_point=21.5, delta=2.0)
    assert F.bet_from(t)["units"] <= F.TRANSPOSED_MAX_UNITS
    # The stale one is free to size on its merits.
    assert F.bet_from(stale)["units"] > 0


def test_other_books_never_alert():
    hits = [{"kind": "STALE", "book": "draftkings", "market": "spreads",
             "side": "X", "book_point": 23.5, "consensus_point": 21.5,
             "delta": 2.0, "price": -110, "game": "A @ B", "kickoff": "x"}]
    assert F.alert_text(hits) is None
    assert F.alert_text(hits, mine_only=False) is not None


def test_alert_fits_one_sms_segment():
    hits = [{"kind": "STALE", "book": "betmgm", "market": "spreads",
             "side": "New Mexico Lobos", "book_point": 23.5,
             "consensus_point": 21.5, "delta": 2.0, "price": -110,
             "game": "New Mexico Lobos @ Oklahoma Sooners",
             "kickoff": "2026-09-19T23:30:00Z"}]
    body = F.alert_text(hits)
    assert body.startswith("BET NOW at BETMGM")
    assert len(body) <= 320, f"{len(body)} chars is more than two segments"


# ---------------------------------------------------------------- kalshi

def _kalshi(**kw):
    row = {"kind": "KALSHI", "market": "kalshi", "book": "kalshi", "league": "NFL",
           "side": "Miami Dolphins", "kalshi_price": 0.40, "fee": 0.02,
           "size": 5000.0, "ticker": "KXNFLGAME-26NOV01MIABAL-MIA", "how": "YES",
           "consensus": 0.47, "edge": 0.05, "game": "Miami Dolphins @ Baltimore Ravens",
           "kickoff": "2026-11-01T17:00:00Z"}
    row.update(kw)
    return row


def test_kalshi_fee_is_seven_percent_of_variance_rounded_up():
    import kalshi_feed as K
    assert K.fee(0.50) == 0.02      # 1.75c -> 2c
    assert K.fee(0.10) == 0.01      # 0.63c -> 1c
    assert K.fee(0.99) == 0.01


def test_kalshi_alerts_even_with_mine_only():
    body = F.alert_text([_kalshi()])
    assert body and body.startswith("BET NOW at KALSHI"), body
    assert "buy YES MIA at 40c" in body
    assert len(body) <= 320, f"{len(body)} chars is more than two segments"


def test_kalshi_stake_is_whole_contracts_at_cost_with_fee():
    bet = F.kalshi_bet(_kalshi())
    # (0.47 - 0.42) / 0.58 = 8.6% Kelly -> quarter is 2.2% -> 2U
    assert bet["units"] == 2.0
    body = F.alert_text([_kalshi()])
    assert "4.76 contracts $2.00 to win $2.76" in body, body


def test_kalshi_filters_thin_tails_and_small_gaps():
    import kalshi_feed as K
    from datetime import datetime, timezone
    rows = [_kalshi(side="ok"),
            _kalshi(side="small", edge=0.02),
            _kalshi(side="longshot", kalshi_price=0.06),
            _kalshi(side="thin", size=10.0)]
    real = K.gaps
    K.gaps = lambda league, an: rows
    try:
        got = F.kalshi_hits("NFL", [], datetime(2026, 10, 30, tzinfo=timezone.utc), 10, "t")
    finally:
        K.gaps = real
    assert [h["side"] for h in got] == ["ok"]


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.ok = code, body, 200 <= code < 300
    def json(self):
        return self._b


def _trade_with(resp, spent=0.0, on=True):
    import kalshi_trade as T
    sent = []
    real = (T._call, T.spent_today, T.enabled)
    T._call = lambda m, p, **kw: (sent.append(kw.get("json")), resp)[1]
    T.spent_today = lambda: spent
    T.enabled = lambda: on
    return T, sent, real


def _restore(T, real):
    T._call, T.spent_today, T.enabled = real


def test_kalshi_order_is_ioc_bid_at_the_seen_price_sized_in_dollar_units():
    T, sent, real = _trade_with(_Resp(201, {"fill_count": "4.76",
                                            "average_fill_price": "0.4000"}))
    try:
        out = T.place(F.kalshi_bet(_kalshi()))
    finally:
        _restore(T, real)
    o = sent[0]
    assert (o["side"], o["price"], o["time_in_force"]) == ("bid", "0.4000", "immediate_or_cancel")
    assert o["count"] == "4.76", o["count"]     # 2U x $1 / 42c with fee
    assert out == "PLACED 4.76 @ 40c", out


def test_order_waits_on_funding_and_reports_why_not():
    T, sent, real = _trade_with(_Resp(201, {"fill_count": "1"}))
    rf = T.fund
    T.fund = lambda shard, need: "account short ($1.00 total)"
    try:
        out = T.place(F.kalshi_bet(_kalshi(exchange_index=3)))
    finally:
        T.fund = rf
        _restore(T, real)
    assert out == "not placed: account short ($1.00 total)" and sent == []


def test_kalshi_respects_cap_switch_and_no_route():
    T, sent, real = _trade_with(_Resp(201, {}), spent=9.0)
    try:
        assert "cap" in T.place(F.kalshi_bet(_kalshi()))
        T.spent_today = lambda: 0.0
        assert "NO route" in T.place(F.kalshi_bet(_kalshi(how="NO")))
        T.enabled = lambda: False
        assert "off" in T.place(F.kalshi_bet(_kalshi()))
    finally:
        _restore(T, real)
    assert sent == [], "nothing may be sent when declining"


def test_kalshi_unfilled_and_shard_errors_read_plainly():
    T, _, real = _trade_with(_Resp(201, {"fill_count": "0.00"}))
    try:
        assert T.place(F.kalshi_bet(_kalshi())) == "not filled: price moved"
        T._call = lambda m, p, **kw: _Resp(404, {"error": {"code": "insufficient_shard_balance"}})
        assert "shard" in T.place(F.kalshi_bet(_kalshi()))
    finally:
        _restore(T, real)


def test_placed_bet_gets_its_own_push_and_leaves_the_alert():
    sent = []
    real = F._deliver
    F._deliver = lambda body: sent.append(body) or True
    import kalshi_trade as T
    rp = T.place
    T.place = lambda bet, cap=10.0: "PLACED 4.76 @ 40c"
    try:
        hits = [_kalshi()]
        F.place_kalshi(hits)
    finally:
        F._deliver, T.place = real, rp
    assert sent and sent[0].startswith("BET PLACED at KALSHI\nPLACED 4.76 @ 40c"), sent
    assert F.alert_text(hits) is None, "already pushed as placed"


def test_recap_once_per_monday_after_nine():
    import os, tempfile, kalshi_trade as T
    from datetime import datetime, timezone
    sent, real = [], (F._deliver, T.recap)
    F._deliver = lambda b: sent.append(b) or True
    T.recap = lambda now=None: "KALSHI WEEK x"
    path = os.path.join(tempfile.mkdtemp(), "a.json")
    try:
        assert not F.maybe_recap(path, datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))  # 8am ET
        assert F.maybe_recap(path, datetime(2026, 11, 2, 14, 5, tzinfo=timezone.utc))
        assert not F.maybe_recap(path, datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc))
        assert not F.maybe_recap(path, datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc))  # Tue
        assert F.new_only([], path) == [] and F.maybe_recap(
            path, datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)) is False, "survives pruning"
    finally:
        F._deliver, T.recap = real
    assert sent == ["KALSHI WEEK x"]


def test_eastern_time_edges():
    import kalshi_trade as T
    from datetime import datetime, timezone
    u = lambda *a: datetime(*a, tzinfo=timezone.utc)
    assert T._midnight_et(u(2026, 10, 30, 15)) == u(2026, 10, 30, 4)    # EDT
    assert T._midnight_et(u(2026, 11, 3, 15)) == u(2026, 11, 3, 5)      # EST
    assert T._midnight_et(u(2026, 10, 31, 2)) == u(2026, 10, 30, 4), "10pm ET is still the 30th"
    assert T._et(u(2026, 11, 2, 14)).hour == 9                         # Monday 9am EST


def test_fund_moves_only_the_shortfall_from_the_richest_shard():
    import kalshi_trade as T
    moves, bals = [], [{0: 0.50, 2: 7.88, 3: 5.00}, {0: 2.11, 2: 6.27, 3: 5.00}]
    real = (T.shard_balances, T.transfer, T.time.sleep)
    T.shard_balances = lambda: bals.pop(0) if len(bals) > 1 else bals[0]
    T.transfer = lambda a, b, d: moves.append((a, b, d)) or True
    T.time.sleep = lambda s: None
    try:
        assert T.fund(0, 2.11) is None
    finally:
        T.shard_balances, T.transfer, T.time.sleep = real
    assert moves == [(2, 0, 1.61)], moves


def test_fund_does_nothing_when_shard_already_holds_it():
    import kalshi_trade as T
    real = (T.shard_balances, T.transfer)
    T.shard_balances = lambda: {0: 9.0, 3: 0.0}
    T.transfer = lambda *a: (_ for _ in ()).throw(AssertionError("no transfer"))
    try:
        assert T.fund(0, 2.11) is None
        assert "short" in T.fund(3, 20.0)
    finally:
        T.shard_balances, T.transfer = real


def test_soccer_three_way_is_yes_only_and_maps_the_tie():
    import kalshi_feed as K
    mk = lambda team, ask, t: {"event_ticker": "KXEPLGAME-26NOV08BRIBRE",
                               "ticker": f"KXEPLGAME-26NOV08BRIBRE-{t}",
                               "yes_sub_title": team, "yes_ask_dollars": ask,
                               "yes_ask_size_fp": "500", "no_ask_dollars": "0.10",
                               "yes_bid_size_fp": "500", "exchange_index": 0,
                               "occurrence_datetime": "2026-11-08T17:00:00Z"}
    real = K.markets
    K.markets = lambda lg: [mk("Brighton", "0.45", "BRI"), mk("Tie", "0.27", "TIE"),
                            mk("Brentford", "0.30", "BRE")]
    try:
        ev = K.events("SOCCER")[0]
    finally:
        K.markets = real
    # NO at 10c on an opponent is "win OR draw" here, so it must not be used.
    assert {t["how"] for t in ev["teams"].values()} == {"YES"}
    assert K.match(ev, "Brentford @ Brighton & Hove Albion", "2026-11-08T15:00:00Z") == {
        "Brighton": "Brighton & Hove Albion", "Tie": "Draw", "Brentford": "Brentford"}


def test_draw_alert_reads_as_a_draw():
    body = F.alert_text([_kalshi(side="Draw", ticker="KXEPLGAME-26NOV08BRIBRE-TIE")])
    assert "Draw: buy YES TIE at 40c" in body, body


def test_per_sport_bar_is_higher_where_books_disagree():
    import kalshi_feed as K
    from datetime import datetime, timezone
    real = K.gaps
    K.gaps = lambda league, an: [_kalshi(league=league, edge=0.035)]
    try:
        now = datetime(2026, 10, 30, tzinfo=timezone.utc)
        assert len(F.kalshi_hits("NFL", [], now, 10, "t")) == 1
        assert F.kalshi_hits("ATP", [], now, 10, "t") == []
    finally:
        K.gaps = real


def test_gap_log_keeps_first_and_best_and_grades_once():
    import os, json, tempfile, kalshi_gaps as G
    d = tempfile.mkdtemp()
    live, arch = os.path.join(d, "g.json"), os.path.join(d, "a.ndjson")
    G.observe([_kalshi(edge=0.01, kalshi_price=0.44, kickoff="2026-01-01T00:00:00Z")], "t1", live)
    # observe() drops rows 3 days past kickoff, so use a recent kickoff
    from datetime import datetime, timedelta, timezone
    kick = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    G.observe([_kalshi(edge=0.01, kalshi_price=0.44, kickoff=kick)], "t1", live)
    G.observe([_kalshi(edge=0.05, kalshi_price=0.40, kickoff=kick)], "t2", live)
    G.observe([_kalshi(edge=0.02, kalshi_price=0.43, kickoff=kick)], "t3", live)
    e = list(json.load(open(live)).values())
    assert len(e) == 1 and e[0]["first"]["at"] == "t1" and e[0]["best"]["at"] == "t2"
    assert G.grade(live, arch, result=lambda t: "yes") == 1
    assert G.grade(live, arch, result=lambda t: "yes") == 0, "graded once"
    row = json.loads(open(arch).read())
    assert row["won"] and abs(row["pnl"] - 0.58) < 1e-9     # 1 - (40c + 2c)
    assert "return/$" in G.report(arch)


def test_kalshi_names_match_feed_names():
    import kalshi_feed as K
    assert K._same("Los Angeles R", "Los Angeles Rams")
    assert not K._same("Los Angeles R", "Los Angeles Chargers")
    assert K._same("Ohio St.", "Ohio State Buckeyes")
    assert not K._same("Ohio", "Ohio State Buckeyes"), "Ohio is the Bobcats"
    assert not K._same("Miami (OH)", "Miami (FL) Hurricanes")
    assert K._same("FC Köln", "FC Koeln") and K._same("Hamburg", "Hamburger SV")
    assert not K._same("Paris FC", "Paris Saint-Germain")


def test_kalshi_event_needs_both_teams_and_a_close_date():
    import kalshi_feed as K
    ev = {"ends": "2026-10-31T03:00:00Z",
          "teams": {"Wisconsin": {}, "Iowa": {}}}
    g = "Wisconsin Badgers @ Iowa Hawkeyes"
    # Kalshi's placeholder end time is 13h BEFORE this kickoff; still the game.
    assert K.match(ev, g, "2026-10-31T16:00:00Z") == {
        "Wisconsin": "Wisconsin Badgers", "Iowa": "Iowa Hawkeyes"}
    assert K.match(ev, g, "2026-11-07T16:00:00Z") is None, "a week later is another game"
    assert K.match(ev, "Wisconsin Badgers @ Rutgers Scarlet Knights",
                   "2026-10-31T16:00:00Z") is None


# ---------------------------------------------------------------- ml mirroring

def test_ml_logs_both_sides_not_just_the_dear_one():
    """
    A book with a big favourite trips the threshold on the favourite and not on
    the dog, because the same vig is a smaller share of a small number. Logging
    only the side that tripped records the fault and discards the bet.
    """
    prices = {
        "betrivers": {"Miami": -3335, "Wake Forest": 1400},
        "bookA":     {"Miami": -1000, "Wake Forest": 650},
        "bookB":     {"Miami": -1050, "Wake Forest": 660},
        "bookC":     {"Miami": -980,  "Wake Forest": 640},
    }
    rows = F.ml_faults(prices)
    br = [r for r in rows if r["book"] == "betrivers"]
    sides = {r["side"] for r in br}
    assert sides == {"Miami", "Wake Forest"}, f"only logged {sides}"
    # And exactly one of them is the half worth acting on.
    actionable = [r for r in br if r["delta"] > 0]
    assert len(actionable) == 1, f"{len(actionable)} actionable sides"


def test_ml_silent_when_nothing_trips():
    prices = {
        "betrivers": {"Miami": -1000, "Wake Forest": 650},
        "bookA":     {"Miami": -1000, "Wake Forest": 650},
        "bookB":     {"Miami": -1010, "Wake Forest": 655},
        "bookC":     {"Miami": -990,  "Wake Forest": 645},
    }
    assert F.ml_faults(prices) == []


def test_pickem_sign_flip_is_stale_not_transposed():
    """
    Atlanta +1.5 against a -1.0 consensus crosses zero, but it is a 2.5-point
    move on a near-pick'em, not an inverted line. Calling it TRANSPOSED
    suppressed the alert on a bettable NFL number, because transposed hits
    never alert.
    """
    f = {"market": "spreads", "book_point": 1.5, "consensus_point": -1.0}
    assert F.classify(f) == "STALE"


def test_big_sign_flip_is_still_transposed():
    """Kansas +5.0 against a -5.5 consensus: 10.5 points, too big to be a move."""
    f = {"market": "spreads", "book_point": 5.0, "consensus_point": -5.5}
    assert F.classify(f) == "TRANSPOSED"


def test_moneyline_coinflip_straddle_is_stale():
    f = {"market": "h2h", "book_implied": 0.52, "consensus": 0.46}
    assert F.classify(f) == "STALE"


def test_moneyline_real_inversion_is_transposed():
    """BetMGM had Kansas at 64.9% where the market said 35.7%."""
    f = {"market": "h2h", "book_implied": 0.6491, "consensus": 0.357}
    assert F.classify(f) == "TRANSPOSED"


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
