"""
What each book will actually let you place, and at what price.

WHY THIS FILE EXISTS. The odds feed says what a book is PRICING. It says
nothing about what that book will let you BUILD. Those came apart on
2026-09-13: the teaser scanner found four qualifying BetMGM spreads, the email
went out recommending a 6-point teaser at BetMGM, and BetMGM does not offer
teasers at all -- the bet slip has Singles and Parlays and nothing else. The
recommendation was unplaceable from the moment it was generated, and nothing in
the pipeline could have known, because no feed carries it.

The other half the feed cannot see is TEASER JUICE. No public odds API quotes
it, and it decides the entire bet: the Wong window clears -110, sits inside the
noise at -120 and is dead at -130. FanDuel's 2-team 6-pointer was quoted at
-134 the same day, which is -4.14% -- worse than betting the spread straight.
A scanner that reports legs without that number is reporting half a bet.

So both facts are recorded by hand, dated, with how they were established.
VERIFY, do not assume: I assumed BetMGM had teasers because their spreads
qualified, and was wrong within the hour.

Update a price by checking the book's own slip and editing the entry here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class Book:
    key: str
    name: str
    teasers: Optional[bool]          # None = never checked
    teaser_6pt: Dict[int, int] = field(default_factory=dict)   # legs -> american
    verified: str = ""
    note: str = ""

    def teaser_price(self, legs: int) -> Optional[int]:
        return self.teaser_6pt.get(legs)


BOOKS: Dict[str, Book] = {
    "betmgm": Book(
        key="betmgm", name="BetMGM", teasers=False, verified="2026-09-13",
        note="Bet slip offers Singles and Parlays only -- no teaser bet type. "
             "Confirmed on the live slip, not inferred. Held for Vegas host "
             "benefits and MGM Rewards tier credits, which is a real reason to "
             "accept worse pricing; it is not a reason to let the scanner "
             "recommend a product the book does not sell."),
    "fanduel": Book(
        key="fanduel", name="FanDuel", teasers=True,
        teaser_6pt={2: -134}, verified="2026-09-13",
        note="2-team 6-point quoted -134. Break-even on the Wong window is "
             "-121, so that is -4.14% and refused. 3-team price NOT yet "
             "checked -- it needs +147 or better, and books often price longer "
             "teasers relatively better, so it is worth a look."),
    "draftkings": Book(key="draftkings", name="DraftKings", teasers=None,
                       note="Believed to offer teasers; never verified here."),
    "betrivers": Book(key="betrivers", name="BetRivers", teasers=None),
    "lowvig": Book(key="lowvig", name="LowVig.ag", teasers=None,
                   note="Sharpest pricing measured (median -3.27% vs -4.5% "
                        "field, best price 47.5% of the time). Offshore: no "
                        "regulatory recourse, legality varies by state."),
}

# Books the user actually holds. The email is written for these.
MINE = ("betmgm", "fanduel")


def get(book_name: str) -> Optional[Book]:
    """Match a feed's book label ('BetMGM', 'betmgm') to an entry."""
    s = (book_name or "").lower().replace(".", "").replace(" ", "")
    for k, b in BOOKS.items():
        if k in s or b.name.lower().replace(".", "").replace(" ", "") in s:
            return b
    return None


def offers_teasers(book_name: str) -> bool:
    """False only when checked and known absent. Unknown books stay eligible."""
    b = get(book_name)
    return True if b is None or b.teasers is None else b.teasers


def teaser_playable(book_name: str, legs: int, max_price: int):
    """
    (playable, reason). A known price worse than the threshold is refused here
    rather than left for the reader to catch.
    """
    b = get(book_name)
    if b is None:
        return True, "book not in the table — price unverified"
    if b.teasers is False:
        return False, f"{b.name} does not offer teasers ({b.verified})"
    quoted = b.teaser_price(legs)
    if quoted is None:
        return True, f"{b.name} {legs}-leg price unverified — need {max_price:+d} or better"
    if quoted < max_price:      # more negative = worse
        return False, (f"{b.name} quotes {quoted:+d} on {legs} legs, "
                       f"needs {max_price:+d} — refused")
    return True, f"{b.name} quotes {quoted:+d}, clears {max_price:+d}"


def mine() -> list:
    return [BOOKS[k] for k in MINE if k in BOOKS]
