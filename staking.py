"""
Stake sizing in units.

The ladder is the capper vocabulary: how much you put on a bet should follow how
likely you think it is to land. That is the right instinct and it beat flat
staking in both seasons we can measure.

It has one failure mode, which shows up plainly in the data. Win probability
alone does not say whether a bet is good — the price does. A +200 shot breaks
even at 33%; a -300 favourite breaks even at 75%. So the raw ladder will stake
2U on a 75% favourite at -300, which is a bet with no edge at all, and in the
2026 sample that bucket was three bets at an average of -219 that lost 6 units.

The fix is not to abandon the ladder but to cap it: never stake more than the
price justifies. Kelly answers exactly that question, and returns zero when a
price offers nothing, so the cap removes the pathological bets without touching
the ones the ladder gets right.
"""

from __future__ import annotations

from typing import List, Optional

from odds import american_to_decimal

# Units by model win probability — the published ladder.
LADDER = [
    (0.60, 0.5),   # a coin flip
    (0.70, 1.0),
    (0.80, 2.0),
    (0.90, 3.0),
    (0.95, 4.0),
    (1.01, 5.0),   # as close to certain as the model gets
]

MAX_UNITS = 5.0
KELLY_FRACTION = 0.25  # quarter Kelly: full Kelly is far too violent in practice


def ladder_units(win_prob: float) -> float:
    """Units from win probability alone. The capper ladder, unmodified."""
    for ceiling, units in LADDER:
        if win_prob < ceiling:
            return units
    return MAX_UNITS


def kelly_units(
    win_prob: float, odds_american: int, fraction: float = KELLY_FRACTION
) -> float:
    """
    What the price justifies, in the same unit vocabulary.

    f* = (bp - q) / b. Returns 0 when the price offers no edge, which is what
    makes it usable as a ceiling rather than just another opinion.
    """
    b = american_to_decimal(odds_american) - 1.0
    if b <= 0:
        return 0.0
    f = (b * win_prob - (1.0 - win_prob)) / b
    if f <= 0:
        return 0.0
    return min(round(f * fraction * 20, 1), MAX_UNITS)


def stake_units(win_prob: float, odds_american: int) -> float:
    """
    The published stake: the ladder, never exceeding what the price justifies.

    Zero means the model likes the side but the price does not pay enough to
    back it — a real answer, and one the raw ladder cannot give.
    """
    return min(ladder_units(win_prob), kelly_units(win_prob, odds_american))


def parlay_win_prob(leg_probs: List[float]) -> float:
    """
    Combined probability for a multi-leg pick.

    Multiplying assumes the legs are independent, which is why selection refuses
    to reuse a team or a game. Correlated legs would make this an overestimate.
    """
    p = 1.0
    for lp in leg_probs:
        p *= lp
    return p


def units_label(units: float) -> str:
    if units <= 0:
        return "no bet"
    return f"{units:g}U"
