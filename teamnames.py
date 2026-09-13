"""
Match a team named by one source against the same team named by another.

The ledger records what the odds API called a team; grading looks it up in
ESPN's results. They do not agree, and exact matching therefore loses bets:

    Appalachian State Mountaineers   (odds API)
    App State Mountaineers           (ESPN)

    Sam Houston State Bearkats       (odds API)
    Sam Houston Bearkats             (ESPN)

Both of those sat ungraded for a day with their games long finished, which
quietly understates the record — and understates it selectively, since whether
a bet grades has nothing to do with whether it won.

THE RULE. Every token of the shorter name must match some token of the longer
one, where tokens match exactly or by prefix of at least three characters. That
handles "App" for "Appalachian" and a dropped "State", while refusing the
matches that would actually be dangerous:

    Ohio Bobcats        vs Ohio State Buckeyes    -> no ("bobcats" matches nothing)
    Miami Hurricanes    vs Miami RedHawks         -> no
    Texas Longhorns     vs Texas Tech Red Raiders -> no

The mascot does the work. Two different schools in the same place almost never
share one, which is why dropping "State" as a stopword — the obvious first
idea — is unsafe and this does not do it.
"""

from __future__ import annotations

import re
from typing import List

# Words that carry no identity. Deliberately short: "state" is NOT here,
# because Ohio and Ohio State are different schools.
NOISE = {"the", "of", "university", "univ"}

MIN_PREFIX = 3


def tokens(name: str) -> List[str]:
    return [t for t in re.findall(r"[a-z]+", (name or "").lower()) if t not in NOISE]


def token_match(a: str, b: str) -> bool:
    """Equal, or one an abbreviation of the other by at least three letters."""
    if a == b:
        return True
    if len(a) >= MIN_PREFIX and b.startswith(a):
        return True
    if len(b) >= MIN_PREFIX and a.startswith(b):
        return True
    return False


def same_team(a: str, b: str) -> bool:
    """
    True when two names plausibly denote one team.

    Asymmetric on purpose: every token of the SHORTER name must find a partner
    in the longer one. Requiring it both ways would reject "Sam Houston
    Bearkats" against "Sam Houston State Bearkats"; requiring it neither way
    would accept "Ohio" against "Ohio State".
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # A single-token name is too little to go on: "Miami" must not match
    # "Miami Hurricanes" when the other Miami is also in the data.
    if len(short) < 2:
        return short[0] in long_
    return all(any(token_match(s, l) for l in long_) for s in short)
