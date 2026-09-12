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

import math

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

# Published stakes come in half units and nothing finer.
#
# This is a decision about what a stake IS, not a display choice. A board that
# says 0.3U is describing an optimisation result; a board that says "half a
# unit" is telling you what to put on the bet, and the second is the only one
# anybody acts on. It also stops false precision: the difference between 0.2U
# and 0.3U is far inside the error on a win probability estimated from Elo, so
# printing it claims an accuracy the model does not have.
#
# Rounding to nearest can raise a stake -- 0.3 becomes 0.5, two thirds more than
# quarter Kelly asked for. That is deliberate and safe in this direction: the
# ladder is already capped at a quarter of Kelly, so even the largest upward
# round lands nearer three-eighths Kelly, well short of the full-Kelly sizing
# that is genuinely dangerous. Anything under a quarter unit rounds to nothing
# and is not a bet, which is the more common outcome and the one that matters.
UNIT_STEP = 0.5
MIN_PLAYABLE_UNITS = 0.5

# How far the model may disagree with the market before the disagreement is
# treated as the model's error rather than the market's.
#
# This is not caution, it is the measured result. Backtesting sorted every pick
# by how far it strayed from the price and the relationship ran backwards: where
# the model claimed 10+ points of edge it won 25-38% of the time against the
# 57-67% it forecast. Sizing by win probability alone therefore stakes the most
# on the picks that lose most, which is the opposite of what a stake should do.
#
# Elo cannot see talent, only results, so two teams with similar records get
# similar ratings even when one is a Big Ten program and the other is not. A
# fifty-point disagreement is that blindness talking, not an edge.
DISAGREEMENT_BANDS = [
    (5.0, 1.00),    # within 5 points of the market: trust it
    (10.0, 0.60),
    (15.0, 0.30),
]
DISAGREEMENT_CUTOFF = 0.0   # beyond the last band, no bet


def round1(x: float) -> float:
    """
    Round to one decimal, half away from zero. For non-negative x only.

    Python's built-in round() is half-to-even, so round(0.15, 1) is 0.1 while
    JavaScript's Math.round gives 0.2. Both engines score the same board, and a
    number that differs between them is a real disagreement about what to bet --
    so the rounding rule is stated here rather than inherited from whichever
    language is running.

    Public because it is not a staking detail: anything the browser also
    computes has to round this way. compute_confidence did not, and eleven legs
    per board landed exactly on a .x5 boundary and scored 0.1 apart in the two
    engines.
    """
    return math.floor(x * 10 + 0.5) / 10


# Kept for readers of the old name.
_round1 = round1


def to_half_units(raw: float) -> float:
    """
    Snap a raw stake to the nearest half unit; below a quarter unit, no bet.

    Rounds half away from zero, matching _round1 and the browser's Math.round,
    because the two engines must agree on what to bet.
    """
    if raw < UNIT_STEP / 2:
        return 0.0
    return math.floor(raw / UNIT_STEP + 0.5) * UNIT_STEP


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
    return min(_round1(f * fraction * 20), MAX_UNITS)


def disagreement_factor(win_prob: float, market_prob: Optional[float]) -> float:
    """
    Shrink the stake as the model strays further from the price.

    Returns 1.0 when the two broadly agree and 0.0 once the gap is wide enough
    that the model has historically been the wrong one.
    """
    if market_prob is None:
        return 1.0
    gap = abs(win_prob - market_prob) * 100.0
    for limit, factor in DISAGREEMENT_BANDS:
        if gap <= limit:
            return factor
    return DISAGREEMENT_CUTOFF


def stake_units(
    win_prob: float,
    odds_american: int,
    market_prob: Optional[float] = None,
) -> float:
    """
    The published stake: the ladder, capped by what the price justifies, then
    shrunk by how far the model has strayed from the market.

    Zero is a real answer: the price does not pay enough to back the opinion.

    THE DISAGREEMENT DAMPER IS NOT APPLIED HERE ANY MORE. It is applied earlier,
    as a refusal -- picks.min_trust_factor drops any bet more than ten points
    from the price instead of sizing it down.

    Applying it in both places double-counted, and the second application did
    nothing but flatten. Every bet that survives the gate is in the same trust
    band, so the multiplier is the same 0.6 on all of them: it cannot change one
    stake relative to another, it can only shrink them all by forty percent.
    Combined with half-unit rounding that was fatal to the whole idea of sizing.
    On a real board it turned raw stakes of 0.30 through 0.70 into eleven
    identical 0.5U bets -- Kennesaw, which the ladder wanted at 3U and Kelly
    allowed at 1.2U, got the same stake as a bet worth 0.3U.

    Refusing is strictly more conservative than shrinking, so the finding the
    damper encodes is still honoured; it is just honoured once, where it can
    actually be expressed.
    """
    return to_half_units(
        _round1(min(ladder_units(win_prob), kelly_units(win_prob, odds_american)))
    )


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


def trust_label(win_prob: float, market_prob: Optional[float]) -> str:
    """
    Plain-language version of the disagreement factor.

    Answers "how much should you believe our number here", which is the question
    the old composite signal score looked like it was answering but was not.
    """
    f = disagreement_factor(win_prob, market_prob)
    if f >= 1.0:
        return "High"
    if f >= 0.6:
        return "Medium"
    if f > 0:
        return "Low"
    return "None"


def units_label(units: float) -> str:
    if units <= 0:
        return "no bet"
    return f"{units:g}U"


def stake_words(units: float) -> str:
    """The stake as an instruction rather than a number."""
    if units <= 0:
        return "no bet"
    if units == 0.5:
        return "half a unit"
    if units == 1:
        return "one unit"
    return f"{units:g} units"
