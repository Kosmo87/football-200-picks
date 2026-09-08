"""
Fill in one file, run one command. That is the whole setup.

  python setup_keys.py --init     create ~/.football-picks.env to fill in
  python setup_keys.py --check    say what is present and what is missing
  python setup_keys.py            push everything to GitHub

Each value is checked for shape before it goes anywhere, because the failures
this project actually hit were not wrong keys -- they were an empty secret from
a prompt that received no input, a token from a different service, and the word
"your_resend_key" pasted verbatim. All three looked fine until something failed
hours later in a job nobody was watching.

Values are never printed. Only their length and first few characters appear, so
a mistake is diagnosable without the secret ending up in a terminal log.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from keys import KEY_FILE, read_key_file

REPO = "Kosmo87/football-200-picks"

# name, kind, required, what it is for, how to recognise a real one
SPEC: List[Tuple[str, str, bool, str, Optional[str]]] = [
    ("ODDS_API_KEY", "secret", True,
     "the-odds-api.com — finds mispriced numbers", r"^[0-9a-f]{32}$"),
    ("RESEND_API_KEY", "secret", True,
     "resend.com/api-keys — sends the morning email", r"^re_[A-Za-z0-9_\-]{10,}$"),
    ("DELIVER_TO", "variable", True,
     "where the morning email goes", r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ("RESEND_FROM", "variable", False,
     "sender address (leave blank to use Resend's test sender)",
     r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    ("TWILIO_ACCOUNT_SID", "secret", False,
     "twilio.com — only if you test texts", r"^AC[0-9a-f]{32}$"),
    ("TWILIO_AUTH_TOKEN", "secret", False, "twilio auth token", r"^[0-9a-f]{32}$"),
    ("TWILIO_FROM", "secret", False,
     "your Twilio number, e.g. +15555550123", r"^\+[1-9]\d{7,14}$"),
]

PLACEHOLDERS = re.compile(
    r"your[_-]?|paste|xxx|changeme|example|here$|placeholder|\.\.\.", re.I
)

TEMPLATE = """# Keys for the football picks project.
#
# Fill in the blanks after the "=" — no quotes, no spaces. Leave anything you
# are not using empty; nothing here is required to be present.
#
# Then run:   python setup_keys.py
#
# This file stays on your machine. It is never committed and never printed.

# Finds mispriced betting lines. Free key: https://the-odds-api.com
ODDS_API_KEY=

# Sends the morning email. Free key: https://resend.com/api-keys
# Starts with re_
RESEND_API_KEY=

# Where the morning email goes.
DELIVER_TO=

# Who it comes from. Leave blank to use Resend's test sender, which can only
# deliver to the address your Resend account is registered under.
RESEND_FROM=

# Only needed if you want to test text messages to your own phone.
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM=
"""


def mask(v: str) -> str:
    return f"{v[:4]}…{len(v)} chars" if len(v) > 8 else f"…{len(v)} chars"


def validate(name: str, value: str, pattern: Optional[str]) -> Optional[str]:
    if PLACEHOLDERS.search(value):
        return "looks like the example text rather than a real value"
    if pattern and not re.match(pattern, value):
        return f"does not match the expected shape ({pattern})"
    return None


def gh(args: List[str], body: Optional[str] = None) -> bool:
    try:
        subprocess.run(args, input=body, text=True, check=True,
                       capture_output=True)
        return True
    except FileNotFoundError:
        print("  gh (the GitHub CLI) is not installed.")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  failed: {(e.stderr or '').strip()[:160]}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", action="store_true", help="create the key file")
    ap.add_argument("--check", action="store_true", help="report only, push nothing")
    args = ap.parse_args()

    if args.init:
        if KEY_FILE.exists():
            print(f"{KEY_FILE} already exists — leaving it alone.")
        else:
            KEY_FILE.write_text(TEMPLATE)
            KEY_FILE.chmod(0o600)
            print(f"Created {KEY_FILE}\n\nOpen it, fill in the blanks, then run:\n"
                  f"  python setup_keys.py")
        return 0

    if not KEY_FILE.exists():
        print(f"No key file yet. Run:\n  python setup_keys.py --init")
        return 1

    found = read_key_file(KEY_FILE)
    print(f"Reading {KEY_FILE}\n")

    ok: List[Tuple[str, str, str]] = []
    problems: List[str] = []
    missing: List[str] = []

    for name, kind, required, purpose, pattern in SPEC:
        value = found.get(name, "")
        if not value:
            (missing if required else []).append(name)
            flag = "MISSING " if required else "skipped "
            print(f"  {flag} {name:<22} {purpose}")
            continue
        err = validate(name, value, pattern)
        if err:
            problems.append(f"{name}: {err}")
            print(f"  BAD     {name:<22} {mask(value)} — {err}")
        else:
            ok.append((name, kind, value))
            print(f"  ok      {name:<22} {mask(value)}")

    if problems:
        print("\nFix these before pushing:")
        for p in problems:
            print(f"  · {p}")
        return 1
    if missing:
        print(f"\nStill needed: {', '.join(missing)}")
        print(f"Add them to {KEY_FILE} and run this again.")
        if not args.check:
            return 1
    if args.check:
        return 0

    print(f"\nPushing to {REPO}:")
    for name, kind, value in ok:
        cmd = (["gh", "secret", "set", name, "--repo", REPO]
               if kind == "secret" else
               ["gh", "variable", "set", name, "--repo", REPO, "--body", value])
        sent = gh(cmd, body=value if kind == "secret" else None)
        print(f"  {'set' if sent else 'FAILED'}  {name} ({kind})")

    print("\nDone. The scheduled jobs will pick these up on the next run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
