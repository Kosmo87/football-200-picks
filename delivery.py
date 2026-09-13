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


def subject_for(rows: List[dict]) -> str:
    """One subject line, so the preview and the send cannot disagree."""
    if not rows:
        return "Keep your money in your pocket"
    units = sum(r["stake"] for r in rows)
    return (f"{len(rows)} bet{'' if len(rows) == 1 else 's'} today"
            f" — {units:g}U total")


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


def todays_opportunities(hours: float = SEND_WINDOW_HOURS) -> List[dict]:
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


def render_text(rows: List[dict]) -> str:
    """
    The bets, and what to put on them. Nothing else.

    No sections, no percentages to interpret, no "here is what we looked at".
    Every line is one thing to do and how much: if it is in the message it has
    already cleared the bar, and if nothing has, the message says so in one
    sentence rather than offering a consolation list.
    """
    if not rows:
        return ("Keep your money in your pocket — nothing today is worth a bet. "
                f"Checked {_games_checked()} game(s); board built {_board_built()}.")
    total = sum(r["stake"] for r in rows)
    lines = [f"Today — {total:g}U across {len(rows)} bet{'' if len(rows) == 1 else 's'}:"]
    for r in rows:
        price = f"+{r['price']}" if r["price"] > 0 else str(r["price"])
        extra = f" (also at {r['also_at']} other book{'' if r['also_at'] == 1 else 's'})" if r.get("also_at") else ""
        lines.append(f"{r['stake']:g}U  {r['side']} {price} at {r['book']}{extra}")
    lines.append("Prices move. Check before betting.")
    return "\n".join(lines)


def render_html(rows: List[dict]) -> str:
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
    if not rows:
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
        total = sum(r["stake"] for r in rows)
        body = (
            "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
            "<tr style='text-align:left;color:#6b7280;font-size:11.5px'>"
            "<th style='padding:6px 12px;text-align:right'>STAKE</th>"
            "<th style='padding:6px 12px'>BET</th>"
            "<th style='padding:6px 12px;text-align:right'>PRICE</th>"
            "<th style='padding:6px 12px'>BOOK</th></tr>"
            f"{cells}</table>"
            f"<p style='margin:14px 0 0;font-size:13.5px;color:#374151'>"
            f"<b>{total:g}U</b> across {len(rows)} bet{'' if len(rows) == 1 else 's'}.</p>"
        )
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


def send_email(to: str, subject: str, html: str) -> bool:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("  RESEND_API_KEY not set — nothing sent.")
        return False
    # Catch the placeholder before spending a request on it. Pasting the example
    # command verbatim stores the literal words, and without this the failure
    # arrives later as an opaque 401 from someone else's API.
    if not key.startswith("re_"):
        print(f"  RESEND_API_KEY does not look like a Resend key "
              f"(they begin 're_', this one begins '{key[:6]}…'). "
              f"Nothing sent — re-set the secret with the real value.")
        return False
    # `.get(name, default)` returns "" when the variable EXISTS and is empty,
    # which is exactly what an unset GitHub Actions `vars.X` produces — so the
    # default never applied and the send went out with a blank From. Resend
    # rejects that, and `continue-on-error` on the workflow step meant the build
    # stayed green while no mail left. Treat empty as unset.
    sender = os.environ.get("RESEND_FROM", "").strip() or "onboarding@resend.dev"
    if sender == "onboarding@resend.dev":
        print("  RESEND_FROM is not set — using Resend's test sender, which only "
              "delivers to the address the Resend account is registered under. "
              "Set it to an address on a domain verified in Resend.")
    print(f"  sending as {sender}")
    r = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps({"from": sender, "to": [to], "subject": subject, "html": html}),
        timeout=30,
    )
    ok = r.status_code < 300
    print(f"  email -> {to}: HTTP {r.status_code} {'' if ok else r.text[:160]}")
    return ok


def send_sms(to: str, body: str) -> bool:
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    frm = os.environ.get("TWILIO_FROM", "").strip()
    if sid and not sid.startswith("AC"):
        print(f"  TWILIO_ACCOUNT_SID does not look like one (they begin 'AC'). "
              f"Nothing sent.")
        return False
    if not (sid and token and frm):
        print("  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM not set — "
              "nothing sent.")
        return False
    r = requests.post(
        TWILIO_URL.format(sid=sid),
        auth=(sid, token),
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
    args = ap.parse_args()

    rows = todays_opportunities(args.hours)
    print(f"{len(rows)} open opportunit{'y' if len(rows) == 1 else 'ies'} "
          f"kicking off within {args.hours:g}h\n")

    text = render_text(rows)
    subject = subject_for(rows)
    if args.dry_run or not (args.to or args.sms):
        print("--- text message ---")
        print(text)
        print(f"\n({len(text)} characters, "
              f"{-(-len(text) // 160)} SMS segment(s))")
        print("\n--- email subject ---")
        print(" ", subject)
        print("\n(dry run: nothing sent)")
        return 0

    if not rows and args.quiet_when_empty:
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
        send_email(args.to, subject, render_html(rows))
    if args.sms:
        send_sms(args.sms, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
