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

RESEND_URL = "https://api.resend.com/emails"
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# A message is worth sending while the game is still ahead of us, and only then.
# Eighteen hours so a single morning send covers the whole day's card: from a
# 12:00 UTC send that reaches a 10:30pm ET kickoff, which a shorter window drops
# without saying anything.
SEND_WINDOW_HOURS = 18


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
    return sorted(best.values(), key=lambda r: (-r["edge"], r["kickoff"]))


def render_text(rows: List[dict]) -> str:
    """
    Short enough to arrive as one or two messages.

    A text that splits into four parts gets skimmed, and this one is meant to be
    acted on before kickoff. Mascots go, and only the top few make it.
    """
    if not rows:
        return "No mispriced numbers this morning. Nothing worth betting."
    lines = ["Better than market today:"]
    for r in rows[:4]:
        price = f"+{r['price']}" if r["price"] > 0 else str(r["price"])
        extra = f" (+{r['also_at']} more books)" if r.get("also_at") else ""
        # Team names are left whole. Dropping the last word to save characters
        # turns "Middle Tennessee Blue Raiders" into "Middle Tennessee Blue",
        # and a bet you cannot identify is worse than a second message.
        lines.append(f"{r['side']} {price} {r['book']}"
                     f" — {r['edge']*100:.0f}% over{extra}")
    if len(rows) > 4:
        lines.append(f"+{len(rows)-4} more on the site")
    lines.append("Prices move. Check before betting.")
    return "\n".join(lines)


def render_html(rows: List[dict]) -> str:
    if not rows:
        body = ("<p>No book is meaningfully out of line this morning. "
                "Nothing worth betting.</p>")
    else:
        cells = "".join(
            f"<tr>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb'>{r['game']}</td>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb'><b>{r['side']}</b></td>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb'>{r['book']}</td>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb;text-align:right;"
            f"font-family:ui-monospace,monospace'>"
            f"{'+' if r['price'] > 0 else ''}{r['price']}</td>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb;text-align:right;"
            f"color:#6b7280;font-family:ui-monospace,monospace'>"
            f"{'+' if r['consensus_price'] > 0 else ''}{r['consensus_price']}</td>"
            f"<td style='padding:8px 12px;border-top:1px solid #e5e7eb;text-align:right;"
            f"color:#15803d'>{r['edge']*100:.1f}%</td>"
            f"</tr>"
            for r in rows
        )
        body = (
            "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
            "<tr style='text-align:left;color:#6b7280;font-size:12px'>"
            "<th style='padding:6px 12px'>GAME</th><th style='padding:6px 12px'>BET</th>"
            "<th style='padding:6px 12px'>BOOK</th>"
            "<th style='padding:6px 12px;text-align:right'>PRICE</th>"
            "<th style='padding:6px 12px;text-align:right'>MARKET</th>"
            "<th style='padding:6px 12px;text-align:right'>BETTER BY</th></tr>"
            f"{cells}</table>"
        )
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
  max-width:640px;margin:0 auto;color:#111827">
  <h2 style="font-size:17px;margin:0 0 4px">Better-than-market prices</h2>
  <p style="color:#6b7280;font-size:13px;margin:0 0 16px">
    {datetime.now().strftime('%A %-d %B')} · sent the morning of so the numbers are current
  </p>
  {body}
  <p style="color:#6b7280;font-size:12px;margin-top:18px;line-height:1.5">
    These are books priced better than the rest of the market on the same bet —
    not predictions. Prices move; confirm before placing anything.
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
    ap.add_argument("--send-empty", action="store_true",
                    help="send even when nothing is mispriced (default: stay quiet)")
    ap.add_argument("--hours", type=float, default=SEND_WINDOW_HOURS)
    args = ap.parse_args()

    rows = todays_opportunities(args.hours)
    print(f"{len(rows)} open opportunit{'y' if len(rows) == 1 else 'ies'} "
          f"kicking off within {args.hours:g}h\n")

    text = render_text(rows)
    if args.dry_run or not (args.to or args.sms):
        print("--- text message ---")
        print(text)
        print(f"\n({len(text)} characters, "
              f"{-(-len(text) // 160)} SMS segment(s))")
        print("\n--- email subject ---")
        print(f"  {len(rows)} better-than-market price"
              f"{'' if len(rows) == 1 else 's'} this morning")
        print("\n(dry run: nothing sent)")
        return 0

    if not rows and not args.send_empty:
        # A daily "nothing today" is how a useful alert becomes one you skim
        # past. Silence on quiet days is what keeps the message worth opening.
        print("Nothing mispriced — staying quiet. (--send-empty to override.)")
        return 0

    subject = (f"{len(rows)} better-than-market price"
               f"{'' if len(rows) == 1 else 's'} this morning")
    if args.to:
        send_email(args.to, subject, render_html(rows))
    if args.sms:
        send_sms(args.sms, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
