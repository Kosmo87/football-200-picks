"""
Say out loud that the site stopped updating, and why.

A failed deploy is the one failure in this pipeline that hides. Everything
else is loud: a broken scanner shows up in the log, a bad build fails a test,
a red job emails you. But when publishing fails the build has already
succeeded -- the board was rebuilt, the ledger graded, the fresh data committed
to the repo -- so the only signal is GitHub's generic "all jobs have failed",
and the site goes on serving a board that gets older every hour while looking
completely normal.

That happened on 2026-09-17: a Netlify token stopped being accepted, six
consecutive runs failed at the last step, and the live board sat 25 hours
stale. The generic email arrived daily and said nothing about a token.

So this sends the one sentence that would have fixed it in a minute, using the
Resend account the picks email already uses.

    python deploy_alert.py --reason token
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REASONS = {
    "token": (
        "Netlify rejected the deploy token",
        "The board built fine and the fresh data is committed — only "
        "publishing failed, so the live site is frozen at its last good "
        "deploy and is getting older every hour.",
        "Create a new personal access token at app.netlify.com (User "
        "settings &rarr; Applications &rarr; Personal access tokens), save it "
        "to a file, then run:<br><code>gh secret set NETLIFY_AUTH_TOKEN "
        "--repo Kosmo87/football-200-picks &lt; /path/to/file</code>",
    ),
    "other": (
        "Netlify deploy failed",
        "The board built and committed, but publishing failed for a reason "
        "other than the token. The live site is frozen at its last good "
        "deploy.",
        "Open the failing run's log and read the deploy step — the CLI output "
        "is printed above the error.",
    ),
}


def board_age_hours() -> float | None:
    """How stale the site will look until this is fixed."""
    from datetime import datetime, timezone
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "public", "data", "board.json")
    try:
        with open(path) as fh:
            gen = json.load(fh).get("generated_at")
        built = datetime.fromisoformat(gen)
        return (datetime.now(timezone.utc) - built).total_seconds() / 3600.0
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--reason", choices=sorted(REASONS), default="other")
    a = ap.parse_args()

    to = os.environ.get("DELIVER_TO", "").strip()
    if not to:
        print("[alert] DELIVER_TO is not set — nothing to email.")
        return 0

    subject, what, fix = REASONS[a.reason]
    age = board_age_hours()
    run = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "Kosmo87/football-200-picks")
    link = f"https://github.com/{repo}/actions/runs/{run}" if run else ""

    html = (
        f"<p><strong>{subject}.</strong></p><p>{what}</p>"
        + (f"<p>The board just built is <strong>fresh</strong>; the copy the "
           f"site is serving is whatever the last successful deploy left, and "
           f"this run's board would have been {age:.0f} hour(s) old by now.</p>"
           if age is not None else "")
        + f"<p><strong>Fix:</strong> {fix}</p>"
        + (f'<p><a href="{link}">The failing run</a></p>' if link else "")
    )

    # Imported here, not at module scope: delivery pulls in the whole board
    # stack, and an alert that cannot be sent because its own import failed
    # would be the same class of bug this file exists to catch.
    try:
        import delivery
        ok = delivery.send_email(to, f"[picks] {subject}", html, confirm=False)
    except Exception as e:
        print(f"[alert] could not send: {e}")
        return 0
    print(f"[alert] {'sent' if ok else 'not accepted'}: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
