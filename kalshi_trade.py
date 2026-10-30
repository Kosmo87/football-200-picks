"""
Place the Kalshi bets the fault watch finds. Real money, so small and capped.

WHAT IT DOES. faults.py --fast finds a Kalshi team priced 3pp+ under the
books (KALSHI_MIN_EDGE) and sizes it with kalshi_bet(). This sends that bet as
an immediate-or-cancel limit order AT the price the scan saw: if the ask has
moved up since, nothing fills and nothing is left resting on the book.

  unit           $1 (UNIT_DOLLARS), set by the user 2026-10-30
  per bet        kalshi_bet's 0.5-3U, so $0.50-$3.00
  per day        DAILY_CAP_DOLLARS, counted from Kalshi's own fills since
                 midnight ET -- the exchange is the record, not a local file
                 that a cache miss could reset
  off switch     KALSHI_AUTOTRADE must be "1" (a repo variable in CI)

AUTH. Kalshi signs every request: Ed25519 over timestamp_ms + METHOD + path.
The key ID and the PEM come from KALSHI_KEY_ID / KALSHI_PRIVATE_KEY (CI
secrets) or ~/kalshi-api-key.txt / ~/kalshi-private-key.pem locally. The
repo's Actions logs are PUBLIC: nothing here prints a key or the balance.

ORDERS use the V2 endpoint (POST /portfolio/events/orders; the v1 one returns
410), which quotes the YES book only: "bid" buys YES. Only YES buys are
automated -- see place(). A 1c IOC bid was accepted 2026-10-30 (HTTP 201,
nothing filled, nothing resting).

SHARDS. The balance is split per exchange shard and an order can only spend
the shard its market lives on: NFL / NCAAF / NHL on 0, NBA / NCAAB on 3.

    python kalshi_trade.py --balance      # signed read-only check
    python kalshi_trade.py --today        # spent today against the cap
    python kalshi_trade.py --recap [--send]   # Monday's weekly summary
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"
UNIT_DOLLARS = 1.0
DAILY_CAP_DOLLARS = 10.0


def _load(env: str, path: str) -> str:
    v = os.environ.get(env, "").strip()
    if v:
        return v
    p = os.path.expanduser(path)
    return open(p).read().strip() if os.path.exists(p) else ""


def _key():
    from cryptography.hazmat.primitives import serialization
    raw = _load("KALSHI_PRIVATE_KEY", "~/kalshi-private-key.pem")
    if not raw:
        return None
    # A pasted PEM loses its line breaks (it did, 2026-10-30); rebuild them.
    head = re.search(r"-----BEGIN ([A-Z ]+)-----", raw)
    label = head.group(1) if head else "PRIVATE KEY"
    body = re.sub(r"-----[A-Z ]+-----|\s", "", raw)
    pem = (f"-----BEGIN {label}-----\n" + "\n".join(textwrap.wrap(body, 64))
           + f"\n-----END {label}-----\n")
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _headers(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding
    kid, key = _load("KALSHI_KEY_ID", "~/kalshi-api-key.txt"), _key()
    if not kid or key is None:
        raise RuntimeError("no Kalshi key (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY)")
    ts = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": kid, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def _call(method: str, path: str, **kw) -> requests.Response:
    full = API + path
    return requests.request(method, BASE + full, headers=_headers(method, full),
                            timeout=20, **kw)


def balance() -> float:
    r = _call("GET", "/portfolio/balance")
    r.raise_for_status()
    return r.json().get("balance", 0) / 100


def _et_offset(t: datetime) -> timedelta:
    """UTC-4 from 2am on the second Sunday of March to the first Sunday of November."""
    def sunday(month: int, nth: int) -> datetime:
        d = datetime(t.year, month, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7 + 7 * (nth - 1))
        return d
    start = sunday(3, 2) + timedelta(hours=7)     # 2am EST
    end = sunday(11, 1) + timedelta(hours=6)      # 2am EDT
    return timedelta(hours=-4) if start <= t < end else timedelta(hours=-5)


def _et(t: datetime) -> datetime:
    """`t` as naive Eastern wall-clock time."""
    return (t + _et_offset(t)).replace(tzinfo=None)


def _et_midnight_utc(day) -> datetime:
    """Midnight Eastern at the start of `day` (a date), as an aware UTC time."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - _et_offset(noon)


def _midnight_et(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return _et_midnight_utc(_et(now).date())


def _paged(path: str, key: str, max_pages: int = 20) -> list:
    """
    Every record of a portfolio list, newest first, all pages.

    No min_ts: Kalshi's docs do not say whether it is seconds or milliseconds,
    and guessing wrong silently drops every fill -- which would read as $0 spent
    and lift the daily cap. Callers filter on each record's own timestamp.
    """
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = _call("GET", path, params=params)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get(key) or [])
        cursor = d.get("cursor")
        if not cursor or not d.get(key):
            break
    return out


def _when(rec: dict, field: str) -> Optional[datetime]:
    v = rec.get(field)
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _fill_cost(f: dict) -> float:
    n = float(f.get("count_fp") or 0)
    side = f.get("outcome_side") or f.get("side")
    px = f.get("yes_price_dollars") if side == "yes" else f.get("no_price_dollars")
    return n * float(px or 0) + float(f.get("fee_cost") or 0)


def ours(ticker: str) -> bool:
    """
    A game-winner market this bot trades. The account also holds the user's
    own trades (15 short-interval markets the week of 2026-10-19); those must
    neither use up the bot's daily cap nor appear in its recap.
    """
    import kalshi_feed
    return any((ticker or "").startswith(s + "-") for s in kalshi_feed.SERIES.values())


def fills_between(a: datetime, b: datetime) -> list:
    return [f for f in _paged("/portfolio/fills", "fills")
            if ours(f.get("ticker") or f.get("market_ticker"))
            and (t := _when(f, "created_time")) and a <= t < b]


def spent_today() -> float:
    """Dollars bought since midnight ET, fees included, from Kalshi's fills."""
    start = _midnight_et()
    return round(sum(_fill_cost(f) for f in
                     fills_between(start, start + timedelta(days=1))), 2)


def enabled() -> bool:
    return os.environ.get("KALSHI_AUTOTRADE", "").strip() == "1"


def place(bet: dict, cap: float = DAILY_CAP_DOLLARS) -> str:
    """
    Send one bet from faults.kalshi_bet. Returns a one-line result for the push.

    Every way this can decline returns a reason rather than raising: the alert
    still has to go out, and "not placed: cap" is the line you most need to see.
    """
    if not enabled():
        return "not placed: autotrade off"
    risk = bet["units"] * UNIT_DOLLARS
    try:
        left = cap - spent_today()
    except Exception as e:
        return f"not placed: fills unreadable ({type(e).__name__})"
    if risk > left:
        return f"not placed: ${cap:.0f}/day cap (${max(left, 0):.2f} left)"
    count = round(risk / bet["cost"], 2)
    if count < 0.01:
        return "not placed: stake under one hundredth of a contract"
    if bet["how"] != "YES":
        # Owning a team as NO on its opponent is an ASK on that market, and
        # that order shape is unverified (the live test was not run). Until it
        # is, only YES buys go through; the push says to place it by hand.
        return "not placed: NO route, place by hand"
    side, price = "bid", bet["kalshi_price"]
    body = {"ticker": bet["ticker"], "side": side, "count": f"{count:.2f}",
            "price": f"{price:.4f}", "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4())}
    try:
        r = _call("POST", "/portfolio/events/orders", json=body)
    except requests.RequestException as e:
        return f"not placed: {type(e).__name__}"
    if not r.ok:
        try:
            err = r.json().get("error") or r.json()
        except ValueError:
            err = {}
        if err.get("code") == "insufficient_shard_balance":
            # Kalshi keeps a balance per shard. Football and hockey trade on
            # shard 0, basketball on shard 3 (2026-10-30); money on one cannot
            # buy on another.
            return "not placed: no funds on this sport's Kalshi shard"
        return f"not placed: HTTP {r.status_code} {err.get('code', '')}"[:80]
    d = r.json()
    filled = float(d.get("fill_count") or 0)
    if filled <= 0:
        return "not filled: price moved"
    avg = d.get("average_fill_price")
    return (f"PLACED {filled:g} @ {float(avg)*100:.0f}c" if avg
            else f"PLACED {filled:g}") + (f" of {count:g}" if filled < count else "")


def recap(now: Optional[datetime] = None) -> str:
    """
    Last Monday-to-Monday (ET): what was bought, what settled, what is open.

    Settled P&L comes from Kalshi's settlement records -- payout (`revenue`,
    cents) less the cost basis and fees -- not from re-grading scores, so it is
    what actually landed in the account.
    """
    now = now or datetime.now(timezone.utc)
    today = _et(now).date()
    monday = today - timedelta(days=today.weekday())
    end = _et_midnight_utc(monday)                # this Monday 00:00 ET
    start = _et_midnight_utc(monday - timedelta(days=7))
    fills = fills_between(start, end)
    bought = sum(_fill_cost(f) for f in fills)
    tickers = {f.get("ticker") or f.get("market_ticker") for f in fills}

    sets = [x for x in _paged("/portfolio/settlements", "settlements")
            if ours(x.get("ticker"))
            and (t := _when(x, "settled_time")) and start <= t < end]
    lines, net, w, l = [], 0.0, 0, 0
    for x in sets:
        cost = (float(x.get("yes_total_cost_dollars") or 0)
                + float(x.get("no_total_cost_dollars") or 0)
                + float(x.get("fee_cost") or 0))
        pnl = (x.get("revenue") or 0) / 100 - cost
        net += pnl
        if pnl > 0:
            w += 1
        elif pnl < 0:
            l += 1
        team = x["ticker"].rsplit("-", 1)[-1]
        held = "YES" if float(x.get("yes_count_fp") or 0) else "NO"
        lines.append(f"{'W' if pnl > 0 else 'L' if pnl < 0 else 'P'} {held} {team} "
                     f"{pnl:+.2f}")
    settled_t = {x["ticker"] for x in sets}
    open_n = len([t for t in tickers if t not in settled_t])

    last = monday - timedelta(days=1)
    span = f"{monday - timedelta(days=7):%b %d}-{last:%b %d}"
    body = [f"KALSHI WEEK {span}",
            f"placed {len(tickers)} bet(s), ${bought:.2f} incl. fees",
            f"settled {len(sets)}: {w}-{l}, net ${net:+.2f}"]
    if open_n:
        body.append(f"still open {open_n}")
    body += lines[:12]
    if len(lines) > 12:
        body.append(f"(+{len(lines) - 12} more)")
    return "\n".join(body)


def main() -> int:
    import keys  # noqa: F401  (local env file)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--balance", action="store_true")
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--recap", action="store_true",
                    help="last week's bets and settlements; --send pushes it")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.recap:
        text = recap()
        print(text)
        if a.send:
            import faults
            faults._deliver(text)
        return 0
    if a.balance:
        print(f"balance ${balance():.2f}")
    if a.today or not a.balance:
        print(f"spent today ${spent_today():.2f} of ${DAILY_CAP_DOLLARS:.0f}; "
              f"autotrade {'ON' if enabled() else 'off'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
