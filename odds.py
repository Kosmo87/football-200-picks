"""American odds helpers: implied probability, de-vig, fair odds, edge."""

from __future__ import annotations

from typing import List, Optional, Tuple


def parse_american_odds(odds_str) -> Optional[int]:
    if odds_str is None or odds_str == "":
        return None
    s = str(odds_str).strip().replace("\u2212", "-").replace("\u2013", "-")
    if s.startswith("+"):
        s = s[1:]
    try:
        return int(float(s)) if "." in s else int(s)
    except ValueError:
        return None


def american_to_decimal(odds: int) -> float:
    if odds > 0:
        return 1 + odds / 100.0
    return 1 + 100.0 / abs(odds)


def decimal_to_american(dec: float) -> int:
    if dec <= 1.0:
        return -10000
    if dec >= 2.0:
        return int(round((dec - 1) * 100))
    return int(round(-100 / (dec - 1)))


def combine_odds(odds_list: List[int]) -> int:
    dec = 1.0
    for o in odds_list:
        dec *= american_to_decimal(o)
    return decimal_to_american(dec)


def american_to_implied_prob(odds: int) -> float:
    """Raw (vigged) implied win probability from American odds."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def fair_american_from_prob(p: float) -> int:
    """Convert a win probability to fair American odds."""
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    dec = 1.0 / p
    return decimal_to_american(dec)


def de_vig_probs(
    odds_a: Optional[int], odds_b: Optional[int]
) -> Tuple[Optional[float], Optional[float]]:
    """
    If both sides have moneylines, return de-vigged fair probs (sum to 1).
    Otherwise return (None, None).
    """
    if odds_a is None or odds_b is None:
        return None, None
    raw_a = american_to_implied_prob(odds_a)
    raw_b = american_to_implied_prob(odds_b)
    total = raw_a + raw_b
    if total <= 0:
        return None, None
    return raw_a / total, raw_b / total


def side_implied_prob(
    side_odds: int,
    home_ml: Optional[int],
    away_ml: Optional[int],
    side: str,
    use_devig: bool = True,
) -> float:
    """
    Implied probability for the priced side.
    Prefers de-vig when both moneylines are present; else raw implied.
    """
    if use_devig:
        home_p, away_p = de_vig_probs(home_ml, away_ml)
        if home_p is not None and away_p is not None:
            return home_p if side == "home" else away_p
    return american_to_implied_prob(side_odds)


def edge_pp(model_win_prob: float, implied_prob: float) -> float:
    """Edge in percentage points (e.g. 0.05 -> 5.0 pp)."""
    return (model_win_prob - implied_prob) * 100.0
