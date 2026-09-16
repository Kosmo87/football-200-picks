"""
Send the morning's bets, by email or text.

Timing is the whole point. A pick sent the night before is priced on last
night's number; sent on the morning of the game it carries the line as it
actually stands, which is when it can still be acted on. Nothing is sent after
kickoff -- a bet you cannot place is worse than no message, because it reads
like one you could have.

Two channels behind one interface. Email is unrestricted and works today. Text
is written and tested but gated: US carriers treat sports betting as a
restricted category under A2P 10DLC, so a registered campaign is required before
messaging anyone but your own verified number. The schema in core/db/picks
carries the same sms|email split for the same reason.

  python delivery.py --dry-run                 # render, send nothing
  python delivery.py --to me@example.com       # email via Resend
  python delivery.py --sms +15555550123        # text via Twilio
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

import keys  # noqa: F401  (loads ~/.football-picks.env)

import books as BOOKS

from archive import read_ndjson
from shop_ledger import ledger_path
from staking import MIN_PLAYABLE_UNITS as MIN_STAKE, stake_units

RESEND_URL = "https://api.resend.com/emails"
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# A message is worth sending while the game is still ahead of us, and only then.
# Eighteen hours so a single morning send covers the whole day's card: from a
# 12:00 UTC send that reaches a 10:30pm ET kickoff, which a shorter window drops
# without saying anything.
SEND_WINDOW_HOURS = 18


# Relative to this file, not the working directory: the workflow runs it from
# the repo root but a person will run it from anywhere.
BOARD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "public", "data", "board.json")


def subject_for(rows: List[dict], teasers: Optional[List[dict]] = None) -> str:
    """One subject line, so the preview and the send cannot disagree."""
    teasers = teasers or []
    n = len(rows) + len(teasers)
    if not n:
        return "Keep your money in your pocket"
    units = sum(r["stake"] for r in rows) + sum(t["units"] for t in teasers)
    return f"{n} bet{'' if n == 1 else 's'} today — {units:g}U total"


def _board() -> dict:
    try:
        with open(BOARD_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _games_checked() -> int:
    return sum(len(lg.get("games", [])) for lg in _board().get("leagues", {}).values())


def _board_built() -> str:
    return (_board().get("generated_at") or "unknown").replace("T", " ")[:16] + " UTC"


def _dt(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def todays_opportunities(hours: float = SEND_WINDOW_HOURS,
                         book: Optional[List[str]] = None) -> List[dict]:
    """
    Open price gaps on games that have not started and kick off soon.

    Collapsed to one row per bet. Two books offering the same team at the same
    number is one thing to do, not two, and listing it twice makes a short
    message look like a longer card than it is. The best price wins; the others
    become a count, which is still worth knowing when the best book is one you
    do not hold an account with.
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    best: Dict[tuple, dict] = {}
    for r in read_ndjson(ledger_path()):
        if r.get("status") != "open":
            continue
        k = _dt(r.get("kickoff", ""))
        if not (k and now < k <= horizon):
            continue
        if book and not any(b.lower() in str(r.get("book", "")).lower()
                            for b in book):
            continue
        key = (r["event_id"], r["side"])
        prev = best.get(key)
        if prev is None:
            best[key] = dict(r, also_at=0)
        elif r["price"] > prev["price"]:
            best[key] = dict(r, also_at=prev.get("also_at", 0) + 1)
        else:
            prev["also_at"] = prev.get("also_at", 0) + 1
    # Size every one, then keep only what is worth placing.
    #
    # The message used to list price gaps with a percentage beside them and no
    # stake, which reads as "bet this" without saying how much — and the honest
    # answer for most of them is "less than you can place". Quarter-Kelly on a
    # 3-4% price edge at +350 is about a tenth of a unit; rounded to the half
    # unit everything is placed in, that is nothing.
    #
    # So the size is computed and the bet is dropped when it rounds to zero.
    # A list of things too small to bet is not a shorter list of good bets, it
    # is the same list with the sizing hidden.
    out = []
    for r in best.values():
        # The de-vigged consensus IS our estimate here: the line-shop claim is
        # about price, not about who wins, so there is no separate forecast to
        # disagree with and the damper has nothing to damp.
        fair = r.get("fair_prob") or 0
        r = dict(r, stake=stake_units(fair, r["price"], fair))
        if r["stake"] >= MIN_STAKE:
            out.append(r)
    return sorted(out, key=lambda r: (-r["stake"], -r["edge"], r["kickoff"]))


def render_text(rows: List[dict], teasers: Optional[List[dict]] = None,
                conf: Optional[List[dict]] = None,
                ladders: Optional[Dict[str, dict]] = None) -> str:
    """
    The bets, and what to put on them. Nothing else.

    No sections, no percentages to interpret, no "here is what we looked at".
    Every line is one thing to do and how much: if it is in the message it has
    already cleared the bar, and if nothing has, the message says so in one
    sentence rather than offering a consolation list.
    """
    teasers = teasers or []
    conf = conf or []
    if not rows and not teasers and not conf:
        return ("Keep your money in your pocket — nothing today is worth a bet. "
                f"Checked {_games_checked()} game(s); board built {_board_built()}.")
    total = sum(r["stake"] for r in rows) + sum(t["units"] for t in teasers)
    n = len(rows) + len(teasers)
    if n:
        lines = [f"Today — {total:g}U across {n} bet{'' if n == 1 else 's'}:"]
    else:
        lines = ["Nothing cleared the bar to place today. "
                 "The board, by chance of winning:"]
    for r in rows:
        price = f"+{r['price']}" if r["price"] > 0 else str(r["price"])
        extra = f" (also at {r['also_at']} other book{'' if r['also_at'] == 1 else 's'})" if r.get("also_at") else ""
        lines.append(f"{r['stake']:g}U  {r['side']} {price} at {r['book']}{extra}")
    lines.extend(render_teaser_lines(teasers))
    ladders = ladders or {}
    if conf:
        for book, book_rows in sorted(by_book(conf).items(),
                                      key=lambda kv: -len(kv[1])):
            lines.append("")
            lines.append(f"--- {book.upper()} — most likely to win")
            lines.extend("  " + l for l in render_confidence_lines(book_rows))
            gaps = [abs(c["gap"]) for c in book_rows[:8]]
            lines.append(f"  (each costs {min(gaps)*100:.1f}-{max(gaps)*100:.1f} "
                         "points against its own odds)")
            lad = ladders.get(book)
            if lad:
                lines.append(f"  {len(lad['legs'])}-leg parlay at {book}: "
                             f"{lad['chance']*100:.1f}% combined, pays "
                             f"{lad['american']:+d}")
                for l in lad["legs"]:
                    lines.append(f"      {l['chance']*100:.0f}%  {l['side']}")
                lines.append(f"    risk 100 to win {lad['profit_per_100']:.2f} — "
                             f"price needs {lad['needs']*100:.1f}%")
            else:
                lines.append(f"  (fewer than 2 legs at {book} — no parlay)")
    if teasers:
        lines.append("The teaser price is the bet. Above the stated number it is "
                     "not worth placing — walk away rather than take -130.")
    lines.append("Prices move. Check before betting.")
    return "\n".join(lines)


def render_html(rows: List[dict], teasers: Optional[List[dict]] = None,
                conf: Optional[List[dict]] = None,
                ladders: Optional[Dict[str, dict]] = None) -> str:
    """
    The same message as the text, laid out.

    STAKE FIRST, and no columns that need interpreting. The old version led
    with the game and ended with a percentage, which asked the reader to work
    out both whether to bet and how much — and the percentage it showed was a
    price gap, which is not a reason to think a team will win. Somebody read it
    as one and asked why we liked the Browns.

    So every row is now an instruction: this many units, on this, at this book.
    If it is in the table it has already cleared the bar.
    """
    teasers = teasers or []
    conf = conf or []
    if not rows and not teasers and not conf:
        body = (
            "<p style='font-size:15px;margin:0 0 10px'><b>Nothing today is worth a bet.</b></p>"
            "<p style='color:#4b5563;font-size:14px;margin:0 0 14px'>Every price was checked "
            "and none of them justified a stake. Keep your money in your pocket.</p>"
            f"<p style='color:#64748b;font-size:12.5px'>Checked {_games_checked()} game(s); "
            f"board built {_board_built()}. This is sent on quiet days too, so that no message "
            f"means something is broken rather than nothing was on.</p>")
    else:
        cell = "padding:9px 12px;border-top:1px solid #e5e7eb"
        cells = "".join(
            f"<tr>"
            f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace;"
            f"font-size:16px;font-weight:700;white-space:nowrap'>{r['stake']:g}U</td>"
            f"<td style='{cell}'><b>{r['side']}</b>"
            f"<div style='color:#6b7280;font-size:12px'>{r['game']}</div></td>"
            f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace;"
            f"white-space:nowrap'>{'+' if r['price'] > 0 else ''}{r['price']}</td>"
            f"<td style='{cell};color:#4b5563'>{r['book']}"
            + (f"<div style='color:#9ca3af;font-size:11.5px'>also at {r['also_at']} other"
               f"{'' if r['also_at'] == 1 else 's'}</div>" if r.get('also_at') else "")
            + "</td></tr>"
            for r in rows
        )
        for t in teasers:
            legs = " + ".join(f"{l.side} {l.teased:+g}" for l in t["legs"])
            cells += (
                f"<tr>"
                f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace;"
                f"font-size:16px;font-weight:700;white-space:nowrap'>{t['units']:g}U</td>"
                f"<td style='{cell}'><b>6-pt teaser</b>"
                f"<div style='color:#6b7280;font-size:12px'>{legs}</div></td>"
                f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace;"
                f"white-space:nowrap;color:#b45309'><b>{t['max_price']:+d}</b>"
                f"<div style='color:#9ca3af;font-size:11px'>or better</div></td>"
                f"<td style='{cell};color:#4b5563'>{t['book']}"
                + (f"<div style='color:#9ca3af;font-size:11.5px'>also at {t['also_at']} other"
                   f"{'' if t['also_at'] == 1 else 's'}</div>" if t.get('also_at') else "")
                + "</td></tr>")
        total = sum(r["stake"] for r in rows) + sum(t["units"] for t in teasers)
        body = (
            "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
            "<tr style='text-align:left;color:#6b7280;font-size:11.5px'>"
            "<th style='padding:6px 12px;text-align:right'>STAKE</th>"
            "<th style='padding:6px 12px'>BET</th>"
            "<th style='padding:6px 12px;text-align:right'>PRICE</th>"
            "<th style='padding:6px 12px'>BOOK</th></tr>"
            f"{cells}</table>"
            f"<p style='margin:14px 0 0;font-size:13.5px;color:#374151'>"
            f"<b>{total:g}U</b> across {len(rows) + len(teasers)} "
            f"bet{'' if len(rows) + len(teasers) == 1 else 's'}.</p>"
            + ("<p style='margin:10px 0 0;font-size:13px;color:#b45309'>"
               "On the teaser the <b>price is the bet</b>. The number shown is the worst "
               "price at which it is still worth placing — at -130 the edge is gone, so "
               "walk away rather than take it.</p>" if teasers else "")
        )
    ladders = ladders or {}
    if conf:
        cell = "padding:8px 12px;border-top:1px solid #e5e7eb"
        for book, book_rows in sorted(by_book(conf).items(),
                                      key=lambda kv: -len(kv[1])):
            crows = "".join(
                f"<tr>"
                f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace;"
                f"font-size:17px;font-weight:700;white-space:nowrap;color:#065f46'>"
                f"{c['chance']*100:.0f}%</td>"
                f"<td style='{cell}'><b>{c['side']}</b>"
                f"<div style='color:#6b7280;font-size:12px'>{c['game']}</div></td>"
                f"<td style='{cell};text-align:right;font-family:ui-monospace,monospace'>"
                f"{'+' if c['price'] > 0 else ''}{c['price']}"
                f"<div style='color:#9ca3af;font-size:11px'>needs "
                f"{c['needs']*100:.0f}%</div></td></tr>"
                for c in book_rows[:8])
            body += (
                f"<h3 style='font-size:15px;margin:24px 0 2px;padding:6px 10px;"
                f"background:#111827;color:#fff;border-radius:4px'>{book.upper()}</h3>"
                "<p style='color:#6b7280;font-size:12.5px;margin:6px 0 8px'>"
                "Everything below is placeable at this book. A price is a required "
                "win rate, so both numbers are shown.</p>"
                "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
                "<tr style='text-align:left;color:#6b7280;font-size:11.5px'>"
                "<th style='padding:6px 12px;text-align:right'>CHANCE</th>"
                "<th style='padding:6px 12px'>BET</th>"
                "<th style='padding:6px 12px;text-align:right'>PRICE</th></tr>"
                f"{crows}</table>")
            lad = ladders.get(book)
            if lad:
                legs = "".join(f"<li style='margin:2px 0'>{l['chance']*100:.0f}% — "
                               f"{l['side']}</li>" for l in lad["legs"])
                body += (
                    f"<p style='font-size:13.5px;margin:12px 0 4px'><b>"
                    f"{len(lad['legs'])}-leg parlay at {book}</b> — most legs "
                    f"staying above 60%:</p>"
                    f"<ul style='margin:0 0 6px;padding-left:20px;font-size:13px;"
                    f"color:#374151'>{legs}</ul>"
                    f"<p style='font-size:13.5px;margin:0'>"
                    f"<b>{lad['chance']*100:.1f}%</b> combined, pays "
                    f"<b>{lad['american']:+d}</b> — risk 100 to win "
                    f"{lad['profit_per_100']:.2f}, price needs "
                    f"<b>{lad['needs']*100:.1f}%</b>.</p>")
            else:
                body += ("<p style='font-size:13px;color:#6b7280;margin:10px 0 0'>"
                         f"Fewer than two qualifying legs at {book} — no parlay.</p>")

    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
  max-width:640px;margin:0 auto;color:#111827">
  <h2 style="font-size:17px;margin:0 0 4px">Today's bets</h2>
  <p style="color:#6b7280;font-size:13px;margin:0 0 16px">
    {datetime.now().strftime('%A %-d %B')} · sent the morning of, so the prices are current
  </p>
  {body}
  <p style="color:#6b7280;font-size:12px;margin-top:18px;line-height:1.5">
    Each of these is a book priced better than the rest of the market on the same
    bet — the stake is what that price edge justifies, not a forecast that the team
    wins. Prices move; confirm before placing anything.
    Educational only, not betting advice.
  </p>
</div>"""


def send_email(to: str, subject: str, html: str, confirm: bool = True) -> bool:
    """
    Send, then ASK RESEND WHAT HAPPENED TO IT.

    A 200 from Resend means accepted, not delivered, and for a whole day that
    was the only thing this function could report. Every message was landing in
    iCloud's junk folder while the log said HTTP 200 and looked fine. That is
    the same failure as the CI job that ran green for a week with close()
    throwing on its first line: a success code standing in for an outcome
    nobody checked.

    So the message id is kept and the status polled. `delivered` means Apple
    (or whoever) accepted it; `bounced` and `complained` are named out loud
    rather than left to be inferred from silence. It still cannot see a junk
    folder -- nothing can -- but it can now tell the difference between "never
    left" and "arrived somewhere you are not looking", which are the two
    explanations that matter and were previously indistinguishable.
    """
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("  RESEND_API_KEY not set — nothing sent.")
        return False
    if not key.startswith("re_"):
        print(f"  RESEND_API_KEY does not look like a Resend key "
              f"(they begin 're_'; got {len(key)} chars). Nothing sent.")
        return False

    # RESEND_FROM used to have a default applied AFTER this read, so the
    # default never applied and the send went out with a blank From. Resend
    # accepted it and delivered nothing.
    sender = os.environ.get("RESEND_FROM", "").strip() or "onboarding@resend.dev"
    if sender == "onboarding@resend.dev":
        print("  RESEND_FROM is not set — using Resend's test sender, which only "
              "delivers to the address the Resend account is registered under. "
              "Set it to an address on a domain verified in Resend.")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(
        RESEND_URL,
        headers=headers,
        json={"from": sender, "to": [to], "subject": subject, "html": html},
        timeout=30,
    )
    ok = r.status_code < 300
    print(f"  email -> {to}: HTTP {r.status_code} {'' if ok else r.text[:200]}")
    if not ok:
        return False

    msg_id = ""
    try:
        msg_id = (r.json() or {}).get("id", "")
    except ValueError:
        pass
    if not (confirm and msg_id):
        return True

    # Resend records the event a moment after the send; one short wait is
    # enough for `delivered` and costs nothing on a job that runs twice a week.
    import time
    for wait in (4, 6):
        time.sleep(wait)
        try:
            q = requests.get(f"{RESEND_URL}/{msg_id}", headers=headers, timeout=20)
            if q.status_code != 200:
                break
            event = (q.json() or {}).get("last_event", "")
        except requests.RequestException:
            break
        if event in ("delivered", "bounced", "complained", "delivery_delayed"):
            print(f"  resend says: {event}")
            if event == "bounced":
                print("  BOUNCED — the address rejected it. Nothing arrived.")
                return False
            if event == "complained":
                print("  marked as spam by the recipient — further sends will "
                      "be filtered harder.")
            if event == "delivered":
                print("  (delivered to the mail host. If it is not in the inbox "
                      "it is in junk — add the sender to Contacts.)")
            return True
        if event:
            print(f"  resend says: {event} (still in flight)")
    return True


def send_sms(to: str, body: str) -> bool:
    """
    Twilio, authenticating with an API key when one is present.

    Two credential shapes work against the same endpoint, and the difference
    matters. The Account SID + Auth Token pair is the whole account: leaking it
    means rotating everything. An API key (SK..., plus a secret shown exactly
    once at creation) is revocable on its own, which is what Twilio recommends
    and what this prefers.

    Either way the URL carries the ACCOUNT SID -- only the basic-auth pair
    changes. Getting that backwards returns a 404 rather than a 401, because
    Twilio looks up the account from the path before it authenticates, which
    makes it read like a missing endpoint instead of a credential problem.
    """
    account = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    key_sid = os.environ.get("TWILIO_API_KEY_SID", "").strip()
    key_secret = os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    frm = os.environ.get("TWILIO_FROM", "").strip()

    if account and not account.startswith("AC"):
        print("  TWILIO_ACCOUNT_SID does not look like one (they begin 'AC'). "
              "Nothing sent.")
        return False
    if key_sid and not key_sid.startswith("SK"):
        print("  TWILIO_API_KEY_SID does not look like one (they begin 'SK'). "
              "Nothing sent.")
        return False

    if key_sid and key_secret:
        user, password, how = key_sid, key_secret, "API key"
    elif token:
        user, password, how = account, token, "auth token"
    else:
        print("  No Twilio credential: set TWILIO_API_KEY_SID + "
              "TWILIO_API_KEY_SECRET (preferred) or TWILIO_AUTH_TOKEN.")
        return False
    if not (account and frm):
        print("  TWILIO_ACCOUNT_SID / TWILIO_FROM not set — nothing sent.")
        return False
    print(f"  authenticating with {how}")

    r = requests.post(
        TWILIO_URL.format(sid=account),
        auth=(user, password),
        data={"From": frm, "To": to, "Body": body},
        timeout=30,
    )
    ok = r.status_code < 300
    print(f"  sms -> {to}: HTTP {r.status_code} {'' if ok else r.text[:200]}")
    if not ok and "21610" in r.text:
        print("  (recipient has replied STOP; carrier will not deliver)")
    if not ok and ("A2P" in r.text or "campaign" in r.text.lower()):
        print("  (blocked pending A2P 10DLC campaign registration — expected for "
              "betting content until a campaign is approved)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", help="email address")
    ap.add_argument("--sms", help="phone number in E.164, e.g. +15555550123")
    ap.add_argument("--dry-run", action="store_true", help="render only, send nothing")
    ap.add_argument("--quiet-when-empty", action="store_true",
                    help="skip the send when nothing is mispriced (default: send anyway)")
    ap.add_argument("--hours", type=float, default=SEND_WINDOW_HOURS)
    ap.add_argument("--books", default=",".join(BOOKS.MINE),
                    help="comma-separated books you hold; only bets placeable "
                         "at one of them are sent. 'all' to drop the filter.")
    ap.add_argument("--no-teasers", action="store_true",
                    help="skip the teaser scan (it costs an odds-API credit)")
    args = ap.parse_args()

    picked = None if args.books.strip().lower() == "all" else [
        b.strip() for b in args.books.split(",") if b.strip()]
    rows = todays_opportunities(args.hours, picked)
    teasers = [] if args.no_teasers else todays_teasers(args.hours, picked)
    conf = todays_confidence(args.hours, picked)
    ladders = ladders_by_book(conf)
    print(f"{len(rows)} open opportunit{'y' if len(rows) == 1 else 'ies'} "
          f"and {len(teasers)} teaser(s) kicking off within {args.hours:g}h\n")

    text = render_text(rows, teasers, conf, ladders)
    subject = subject_for(rows, teasers)
    if not rows and not teasers and conf:
        subject = f"Nothing to place — {len(conf)} bets by win %"
    if args.dry_run or not (args.to or args.sms):
        print("--- text message ---")
        print(text)
        print(f"\n({len(text)} characters, "
              f"{-(-len(text) // 160)} SMS segment(s))")
        print("\n--- email subject ---")
        print(" ", subject)
        print("\n(dry run: nothing sent)")
        return 0

    if not rows and not teasers and args.quiet_when_empty:
        print("Nothing mispriced — staying quiet, because --quiet-when-empty was passed.")
        return 0

    # A quiet day sends anyway, on purpose. The argument for silence is that a
    # daily "nothing today" becomes something you skim past; the argument
    # against is stronger, because silence is ALSO what a broken pipeline looks
    # like. This job has been failing invisibly for weeks at a time — a blank
    # From address, a parity check flipping on a rounding boundary — and in
    # every case nothing arriving was the only symptom, indistinguishable from a
    # quiet Tuesday. An email that says "keep your money in your pocket" costs a
    # glance and turns silence back into a signal.

    if args.to:
        send_email(args.to, subject, render_html(rows, teasers, conf, ladders))
    if args.sms:
        send_sms(args.sms, text)
    return 0




# ------------------------------------------------------------------- teasers

# Quarter-Kelly on the teaser expressed as a percentage of bankroll, because
# staking.stake_units() returns 0.0U for every teaser price -- including -110,
# where the edge is +4.40% and the largest this project has measured. That is
# the unit scale, not the bet: kelly_units multiplies by 20, so 1U is 5% of
# bankroll and a correct 1.2%-of-bankroll stake rounds to nothing. Rather than
# silently drop the best edge on the board, the teaser is sized here against a
# conventional 1U = 1% of bankroll and the scale is stated in the message.
TEASER_UNIT_PCT = 0.01
TEASER_MIN_UNITS = 0.5


def _teaser_units(win_prob: float, price: int) -> float:
    from staking import to_half_units
    b = price / 100.0 if price > 0 else 100.0 / abs(price)
    f = (b * win_prob - (1.0 - win_prob)) / b
    if f <= 0:
        return 0.0
    return to_half_units((f / 4.0) / TEASER_UNIT_PCT)


def todays_teasers(hours: float = SEND_WINDOW_HOURS,
                   book: Optional[List[str]] = None) -> List[dict]:
    """
    Qualifying 6-point teasers, one per book, on games kicking off soon.

    The teaser price is not in the odds feed -- no public API carries it -- so
    the message cannot say "bet this at X". It says the threshold instead:
    the worst price at which the bet still exists. That is the number that
    decides it, since the window clears -110 and is dead at -130.
    """
    import teaser as T
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    try:
        # Only the books with an account: a teaser is built inside one book,
        # so a leg at a book the reader cannot use is not a leg.
        payload = T.fetch_spreads("NFL", books=(book or BOOKS.MINE))
    except SystemExit:
        return []
    legs = T.qualifying_legs(payload, within_days=max(1, int(hours / 24) + 1))
    out = []
    wanted = [b.lower() for b in (book or [])]
    for bk, book_legs in T.by_book(legs).items():
        if wanted and not any(w in bk.lower() for w in wanted):
            continue
        if not BOOKS.offers_teasers(bk):
            continue          # the book does not sell this product at all
        # Push risk is charged in prob already, but a whole-number teased line
        # is a worse bet at the same price; prefer clean legs when there are
        # enough of them to fill a ticket.
        clean = [l for l in book_legs if not l.push_risk]
        pool = clean if len(clean) >= 2 else book_legs
        pool = [l for l in pool
                if (k := _dt(l.kickoff)) and now < k <= horizon]
        if len(pool) < 2:
            continue
        pick = pool[:2]
        probs = [l.prob for l in pick]
        p = probs[0] * probs[1]
        mp = T.max_price(probs)
        playable, reason = BOOKS.teaser_playable(bk, len(pick), mp)
        if not playable:
            continue          # a known price worse than break-even is not a bet
        out.append({
            "book": bk,
            "legs": pick,
            "win_prob": p,
            "max_price": mp,
            "units": _teaser_units(p, -110),
            "ev_110": T.teaser_ev(probs, -110),
            "price_note": reason,
        })
    # One per BOOK when the reader holds several, because those are different
    # places to place a bet, not the same idea listed twice. Within a book the
    # best ticket wins. When no book was named, fall back to a single overall
    # recommendation so a general send does not list nine versions of one idea.
    out.sort(key=lambda r: (-r["units"], -r["win_prob"], r["max_price"]))
    if not out:
        return []
    if wanted:
        seen, keep = set(), []
        for r in out:
            if r["book"] not in seen:
                seen.add(r["book"]); keep.append(r)
        return keep
    return [dict(out[0], also_at=len(out) - 1)]


def render_teaser_lines(teasers: List[dict]) -> List[str]:
    """One line per teaser, same shape as every other line in the message."""
    lines = []
    for t in teasers:
        legs = " + ".join(f"{l.side} {l.teased:+g}" for l in t["legs"])
        extra = (f" (also at {t['also_at']} other book"
                 f"{'' if t['also_at'] == 1 else 's'})") if t.get("also_at") else ""
        lines.append(f"{t['units']:g}U  6-pt teaser at {t['book']}: {legs}"
                     f"  — only at {t['max_price']:+d} or better{extra}")
    return lines




# ---------------------------------------------------------------- confidence

# How likely a bet is to WIN, which is not the same question as whether it is
# worth making, and is the question actually being asked. A price is a required
# win rate -- -167 needs 62.5%, -500 needs 83.3% -- so both numbers travel
# together on every line. CHANCE is the de-vigged consensus; NEEDS is what the
# price demands.
CONFIDENCE_FLOOR = 0.62


def todays_confidence(hours: float = SEND_WINDOW_HOURS,
                      book: Optional[List[str]] = None,
                      floor: float = CONFIDENCE_FLOOR,
                      leagues=("NFL", "NCAAF")) -> List[dict]:
    import confidence as C
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    out: List[dict] = []
    for lg in leagues:
        try:
            rows = C.scan(lg, floor, days=max(1, int(hours / 24) + 1),
                          only_books=[b.lower() for b in (book or [])] or None)
        except SystemExit:
            continue
        for r in rows:
            if now < r["kickoff"] <= horizon:
                out.append(dict(r, league=lg))
    return sorted(out, key=lambda r: -r["chance"])


def render_confidence_lines(rows: List[dict], limit: int = 8) -> List[str]:
    """One line per bet: what it is, how often it wins, what the price asks."""
    lines = []
    for r in rows[:limit]:
        price = f"+{r['price']}" if r["price"] > 0 else str(r["price"])
        lines.append(f"{r['chance']*100:.0f}% to win  {r['side']} {price} "
                     f"at {r['book']}  (price needs {r['needs']*100:.0f}%)")
    return lines


def by_book(rows: List[dict]) -> Dict[str, List[dict]]:
    """Split the board into one list per book, best chance first."""
    out: Dict[str, List[dict]] = {}
    for r in rows:
        out.setdefault(r["book"], []).append(r)
    for b in out:
        out[b] = sorted(out[b], key=lambda r: -r["chance"])
    return out


def ladders_by_book(rows: List[dict], floor: float = 0.60,
                    max_legs: int = 8) -> Dict[str, dict]:
    """
    One ladder per book, because a parlay is built inside one book.

    The first version of this pooled every book and produced a seven-leg
    ticket with six FanDuel legs and one BetMGM leg, which cannot be placed
    anywhere. teaser.py and parlays.py both enforce this; the confidence
    ladder did not, and shipped an unplaceable recommendation.
    """
    out: Dict[str, dict] = {}
    for book, book_rows in by_book(rows).items():
        lad = parlay_ladder(book_rows, floor, max_legs)
        if lad:
            out[book] = lad
    return out


def parlay_ladder(rows: List[dict], floor: float = 0.60,
                  max_legs: int = 8) -> Optional[dict]:
    """
    The most legs that stay above a combined win-rate floor, WITHIN ONE BOOK.

    Callers should reach this through ladders_by_book(); passing a mixed-book
    list builds a ticket nobody can place.

    Greedy by chance is optimal here: the highest-chance leg costs the least
    probability, so taking them in order reaches the most legs. Every leg's
    own cost is carried in `gap`, which is why the ladder is reported with it
    -- seven legs at 66% sounds better than one at 62% and is eight times
    more expensive.
    """
    if len(rows) < 2:
        return None
    books = {r["book"] for r in rows}
    if len(books) > 1:
        raise ValueError(f"parlay_ladder got {len(books)} books: {sorted(books)}. "
                         "Use ladders_by_book().")
    ch, dec_total, keep = 1.0, 1.0, 1.0
    legs: List[dict] = []
    seen_games = set()
    for r in rows:
        if r["game"] in seen_games:
            continue                       # one leg per game
        nxt = ch * r["chance"]
        if nxt < floor or len(legs) >= max_legs:
            break
        d = 1 + (r["price"] / 100 if r["price"] > 0 else 100 / abs(r["price"]))
        ch, dec_total = nxt, dec_total * d
        keep *= r["chance"] / (1 / d)
        legs.append(r)
        seen_games.add(r["game"])
    if len(legs) < 2:
        return None
    american = int(round((dec_total - 1) * 100)) if dec_total >= 2 \
        else -int(round(100 / (dec_total - 1)))
    return {"legs": legs, "chance": ch, "american": american,
            "needs": 1 / dec_total, "keep": keep,
            "profit_per_100": (dec_total - 1) * 100}


if __name__ == "__main__":
    raise SystemExit(main())
