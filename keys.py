"""
One file holds every key. Nothing else needs to know where they came from.

Scripts here read credentials from the environment, which is right for CI and
awkward for a person: it means remembering an export before every command, and
an export that scrolls off is indistinguishable from one never typed. So the
same values live in a single file outside the repo, and importing this module
loads them.

  ~/.football-picks.env

Never committed, never printed, never sent anywhere. `python setup_keys.py`
reads it, checks each value is the shape it claims to be, and pushes them to
GitHub so the scheduled jobs have them too.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

KEY_FILE = Path.home() / ".football-picks.env"


def read_key_file(path: Path = KEY_FILE) -> Dict[str, str]:
    """Parse KEY=value lines. Blank values mean 'not filled in yet'."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


def load(path: Path = KEY_FILE) -> Dict[str, str]:
    """
    Put the file's values into the environment, without overwriting anything
    already set. CI supplies its own; this only fills the gaps.
    """
    found = read_key_file(path)
    for k, v in found.items():
        os.environ.setdefault(k, v)
    return found


# Importing is enough. Every entry point does this so no command ever needs an
# export in front of it.
load()
