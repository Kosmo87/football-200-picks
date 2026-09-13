"""
close() and grade(), against stubbed sources.

Both had failed silently for a week. close() raised TypeError on its first line
every time it ran -- wrong argument, two missing -- and the raise was caught by
a broad except, reported as "live prices unavailable", then swallowed again by
continue-on-error in CI. The ledger accumulated twenty-three rows and zero
closing prices: the one measurement the file argues is the important one, never
collected, with nothing anywhere saying so.

A test would have caught it on the first run. That is the whole reason this
file exists, so it stubs the network rather than skipping when there is no key.
"""
from datetime import datetime, timedelta, timezone

import shop_ledger


class FakeFinal:
    def __init__(self, home, away, hs, as_, date):
        self.home_name, self.away_name = home, away
        self.home_abbr, self.away_abbr = home[:4].upper(), away[:4].upper()
        self.home_score, self.away_score = hs, as_
        self.date, self.event_id = date, f"{home}-{away}"


def soon(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


ok = fail = 0
def t(name, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  ok   {name}")
    else: fail += 1; print(f"  FAIL {name}: got {got!r}, want {want!r}")


def run_close(rows, prices):
    """close() against a stubbed market, without touching the network."""
    import io, json, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".ndjson")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    real_path, real_fetch, real_games = (
        shop_ledger.ledger_path, shop_ledger.fetch_live, shop_ledger.games_from_live)
    shop_ledger.ledger_path = lambda season=None: path
    shop_ledger.fetch_live = lambda *a, **k: ["payload"]
    shop_ledger.games_from_live = lambda payload, market: prices
    try:
        n = shop_ledger.close()
        out = [json.loads(l) for l in open(path) if l.strip()]
        return n, out
    finally:
        shop_ledger.ledger_path, shop_ledger.fetch_live, shop_ledger.games_from_live = (
            real_path, real_fetch, real_games)
        os.unlink(path)


print("close()")
kick = soon(30)
row = {"event_id": f"{shop_ledger._norm('Iowa State Cyclones @ Iowa Hawkeyes')}|{kick}",
       "league": "NCAAF", "market": "h2h", "game": "Iowa State Cyclones @ Iowa Hawkeyes",
       "kickoff": kick, "book": "betrivers", "side": "Iowa State Cyclones",
       "price": 500, "consensus_price": 470, "status": "open", "result": None, "units": None}
# Three books quoting the side at kickoff; the median is the close.
market = [("Iowa State Cyclones @ Iowa Hawkeyes", kick, {
    "a": {"Iowa State Cyclones": 400, "Iowa Hawkeyes": -520},
    "b": {"Iowa State Cyclones": 420, "Iowa Hawkeyes": -540},
    "c": {"Iowa State Cyclones": 440, "Iowa Hawkeyes": -560}})]

n, out = run_close([dict(row)], market)
t("it actually closes a bet, rather than raising", n, 1)
t("the close is the median across books", out[0].get("close_price"), 420)
t("it records how many books were behind it", out[0].get("close_books"), 3)
t("CLV is positive when the price taken was longer than the close",
  out[0].get("clv_pp", 0) > 0, True)
t("and is measured in probability points",
  round(out[0]["clv_pp"], 1), round((100/520 - 100/600) * 100, 1))

far = {**row, "kickoff": soon(600),
       "event_id": f"{shop_ledger._norm('Iowa State Cyclones @ Iowa Hawkeyes')}|{soon(600)}"}
n2, _ = run_close([far], market)
t("a bet days away is not closed yet", n2, 0)

settled = {**row, "status": "lost"}
n3, _ = run_close([settled], market)
t("a settled bet is never re-closed", n3, 0)

n4, out4 = run_close([dict(row)], [])
t("an empty market closes nothing rather than inventing a price", n4, 0)
t("and leaves the row untouched", out4[0].get("close_price"), None)

print("\ngrade() across sources that name teams differently")
from teamnames import same_team
t("App State", same_team("Appalachian State Mountaineers", "App State Mountaineers"), True)
t("Sam Houston", same_team("Sam Houston State Bearkats", "Sam Houston Bearkats"), True)

print(f"\n  {ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
