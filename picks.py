"""+EV tip selection: Elo model vs implied odds, confidence-gated 1–5 leg picks."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

from elo import EloSystem
from espn import Game
from odds import (
    combine_odds,
    edge_pp,
    fair_american_from_prob,
    side_implied_prob,
)
from staking import (
    MIN_PLAYABLE_UNITS,
    disagreement_factor,
    parlay_win_prob,
    round1,
    stake_units,
    trust_label,
    units_label,
)

MIN_COMBINED_ODDS = 200
DEFAULT_MAX_SINGLE_LEG_ODDS = 600
DEFAULT_MIN_EDGE_PP = 5.0
DEFAULT_MIN_SAMPLE_NFL = 3
DEFAULT_MIN_SAMPLE_NCAAF = 4
DEFAULT_MAX_PARLAY_LEGS = 5

# Half a unit is the smallest stake that exists (see staking.to_half_units), so
# it is also the floor. A pick sizing under a quarter unit rounds to nothing and
# is not a bet -- that is the engine saying it does not believe it, which is an
# answer rather than a failure.
DEFAULT_MIN_STAKE_UNITS = MIN_PLAYABLE_UNITS

# "High certainty of happening" is a different gate from "high edge", and the
# two point opposite ways here: the backtest found claimed edge INVERSELY
# related to outcome, while certainty is just the model's win probability. Off
# by default (0.0) because a +200 target and a likely winner are close to
# mutually exclusive -- a bet paying +200 is about 33% by construction -- so
# switching it on is a deliberate choice to trade payout for hit rate.
DEFAULT_MIN_WIN_PROB = 0.0

# The same question asked of the TICKET rather than of each leg, which is the
# one that actually matters and the one a per-leg gate silently fails. Four legs
# each cleared at 55% is a 9% parlay; requiring "certainty" leg by leg and then
# multiplying them together produces exactly the bet the gate was meant to
# exclude. Applied after the parlay is assembled, to its combined win
# probability.
DEFAULT_MIN_PICK_WIN_PROB = 0.0

# How far the model may sit from the price before the bet is refused outright
# rather than merely sized down.
#
# The damper in staking.py shrinks a stake as the model strays from the market,
# because backtesting found claimed edge INVERSELY related to outcome. That
# shrinking used to be visible: a 12pp disagreement sized to 0.30U next to a
# 3pp one at 0.50U. Half-unit stakes cannot express it -- 0.30 and 0.50 both
# round to half a unit -- so the damper silently became a no-op for exactly the
# bets it existed to punish, and a bet we had measured ourselves to be wrong
# about got the same stake as one we believed.
#
# If the stake cannot carry the warning, the bet does not get made. 0.6 is the
# Medium band: the model within 10 points of the price. Anything further is
# refused.
DEFAULT_MIN_TRUST_FACTOR = 0.6


@dataclass
class ConfidenceConfig:
    """Tunable confidence / selection gates (UI sidebar)."""

    min_edge_pp: float = DEFAULT_MIN_EDGE_PP
    min_sample_games: int = DEFAULT_MIN_SAMPLE_NFL
    max_single_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS
    max_parlay_legs: int = DEFAULT_MAX_PARLAY_LEGS  # 1–5 (1 = singles only)
    min_combined_odds: int = MIN_COMBINED_ODDS
    high_confidence_mode: bool = False
    # Shortest price allowed on a single leg: do not lay more than 3.5 to 1.
    # Mirrors DEFAULTS.minLegOdds in the browser.
    min_leg_odds: int = -350
    # Noise gates. See the constants above for why these values.
    min_stake_units: float = DEFAULT_MIN_STAKE_UNITS
    min_win_prob: float = DEFAULT_MIN_WIN_PROB
    min_pick_win_prob: float = DEFAULT_MIN_PICK_WIN_PROB
    min_trust_factor: float = DEFAULT_MIN_TRUST_FACTOR
    exclude_stale: bool = True

    def apply_high_confidence_preset(self, league: str = "NFL") -> "ConfidenceConfig":
        """Raise gates and prefer stacking shorter prices (more legs allowed)."""
        self.high_confidence_mode = True
        self.min_edge_pp = max(self.min_edge_pp, 7.5)
        league_u = (league or "NFL").upper()
        floor = 5 if league_u == "NFL" else 6
        self.min_sample_games = max(self.min_sample_games, floor)
        self.max_single_leg_odds = min(self.max_single_leg_odds, 350)
        self.max_parlay_legs = max(self.max_parlay_legs, 5)
        return self

    def apply_high_certainty_preset(self) -> "ConfidenceConfig":
        """
        Likely to win, rather than well priced.

        Two floors, and the second is the one that does the work: the ticket
        must be better than a coin flip, not merely built from legs that were.
        Gating legs alone produced a four-leg parlay of 55%-plus sides that was
        21% to land -- technically every leg "certain", the bet itself a
        longshot.

        The payout floor drops to -400 to make that possible at all. American
        odds compare correctly as signed integers (-400 < -150 < +100 < +200,
        which is also increasing payout), so a negative floor reads as "any
        price at or longer than -400". Leaving it at +200 would be asking for a
        one-in-three shot to come in more than half the time, which has no
        answer -- the board would go empty and look broken rather than show
        favourites and look small.

        This is the trade stated in one place: the market prices certainty, so
        buying a likely winner means accepting a small payout. There is no
        setting that gives both.
        """
        self.min_win_prob = max(self.min_win_prob, 0.55)
        self.min_pick_win_prob = max(self.min_pick_win_prob, 0.50)
        self.min_combined_odds = min(self.min_combined_odds, -400)
        self.min_stake_units = max(self.min_stake_units, DEFAULT_MIN_STAKE_UNITS)
        return self


def default_min_sample(league: str) -> int:
    return DEFAULT_MIN_SAMPLE_NFL if (league or "").upper() == "NFL" else DEFAULT_MIN_SAMPLE_NCAAF


@dataclass
class Leg:
    game: Game
    side: str
    team_name: str
    team_abbr: str
    team_id: str
    odds_american: int
    model_win_prob: float = 0.0
    implied_prob: float = 0.0
    edge: float = 0.0  # probability points (e.g. 0.04 = 4pp)
    fair_odds: int = 0
    sample_games: float = 0.0  # min effective sample across both teams
    team_sample: float = 0.0
    opp_sample: float = 0.0
    team_games: int = 0  # raw completed games this season
    opp_games: int = 0
    prior_credit: float = 0.0  # sample credit from last season's rating
    confidence: float = 0.0  # 0–100
    confidence_label: str = "Low"
    # Set by build_board.mark_stale_legs: the ratings cannot speak to this game
    # because a quarterback was ruled out after they were computed. True on BOTH
    # sides of such a game, deliberately.
    stale: bool = False
    stale_reason: str = ""


@dataclass
class Pick:
    legs: List[Leg]
    combined_odds: int
    score: float  # ranking score
    label: str = ""
    combined_edge_pp: float = 0.0
    avg_confidence: float = 0.0
    confidence_label: str = "Low"
    win_prob: float = 0.0      # model probability every leg lands
    market_prob: float = 0.0   # the same, according to the price
    stake_units: float = 0.0   # ladder, capped by price and by disagreement
    trust: str = "None"


def _opp_id(leg: Leg) -> str:
    return leg.game.home.id if leg.side == "away" else leg.game.away.id


def legs_overlap(legs: List[Leg]) -> bool:
    teams: Set[str] = set()
    games: Set[str] = set()
    for leg in legs:
        if leg.team_id in teams or leg.game.event_id in games:
            return True
        teams.add(leg.team_id)
        teams.add(_opp_id(leg))
        games.add(leg.game.event_id)
    return False


def confidence_label_from_score(score: float) -> str:
    if score >= 70:
        return "High"
    if score >= 40:
        return "Med"
    return "Low"


def compute_confidence(
    edge: float,
    odds_american: int,
    sample_games: float,
) -> Tuple[float, str]:
    """
    Confidence 0–100 from sample depth + edge size + price shortness.

    Formula (documented in README):
      sample_score  = min(sample_games / 8, 1) * 40
      edge_score    = min(max(edge_pp, 0) / 15, 1) * 35
      short_score   = f(odds) * 25
        dogs  (+): max(0, 1 - (odds - 100) / 500)   # +100→1.0, +600→0
        favs  (−): max(0, 1 - (|odds| - 100) / 200) # −100→1.0, −300→0
      confidence    = sample_score + edge_score + short_score

    Labels: High ≥70, Med ≥40, else Low.
    """
    epp = edge * 100.0
    sample_score = min(max(float(sample_games), 0.0) / 8.0, 1.0) * 40.0
    edge_score = min(max(epp, 0.0) / 15.0, 1.0) * 35.0
    if odds_american >= 0:
        short_frac = max(0.0, 1.0 - (odds_american - 100) / 500.0)
    else:
        short_frac = max(0.0, 1.0 - (abs(odds_american) - 100) / 200.0)
    short_score = short_frac * 25.0
    # round1, not round(): Python rounds .x5 to even and JavaScript rounds it
    # up, and eleven legs on a typical board land exactly there. A 0.1
    # difference is invisible until it flips a sort, and then the two engines
    # select different bets from the same board -- which is what happened the
    # moment the pick cap was lifted and selection ran past the first three.
    score = round1(sample_score + edge_score + short_score)
    score = max(0.0, min(100.0, score))
    return score, confidence_label_from_score(score)


DEFAULT_MIN_LEG_ODDS = -350


def annotate_leg(
    game: Game,
    side: str,
    elo: EloSystem,
    max_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS,
    min_leg_odds: int = DEFAULT_MIN_LEG_ODDS,
) -> Optional[Leg]:
    team = game.home if side == "home" else game.away
    odds = game.odds.moneyline_home if side == "home" else game.odds.moneyline_away
    # Favorites are allowed down to min_leg_odds for multi-leg stacks; long dogs
    # are capped by max_leg_odds.
    if odds is None or not (min_leg_odds <= odds <= max_leg_odds):
        return None

    model_p = elo.win_prob_side(
        game.home.id, game.away.id, side, neutral=getattr(game, "neutral", False)
    )
    implied = side_implied_prob(
        odds,
        game.odds.moneyline_home,
        game.odds.moneyline_away,
        side,
        use_devig=True,
    )
    edge = model_p - implied
    opp = game.away if side == "home" else game.home
    # Effective sample credits prior-season games, so a week-2 team with a real
    # carried-over rating is not treated as a total unknown.
    team_sample = elo.effective_sample(team.id)
    opp_sample = elo.effective_sample(opp.id)
    team_games = elo.games_played.get(team.id, 0)
    opp_games = elo.games_played.get(opp.id, 0)
    sample = min(team_sample, opp_sample)
    credit = min(elo.prior_credit(team.id), elo.prior_credit(opp.id))
    conf, conf_label = compute_confidence(edge, odds, sample)
    return Leg(
        game=game,
        side=side,
        team_name=team.name,
        team_abbr=team.abbreviation,
        team_id=team.id,
        odds_american=odds,
        model_win_prob=model_p,
        implied_prob=implied,
        edge=edge,
        fair_odds=fair_american_from_prob(model_p),
        sample_games=sample,
        team_sample=team_sample,
        opp_sample=opp_sample,
        team_games=team_games,
        opp_games=opp_games,
        prior_credit=credit,
        confidence=conf,
        confidence_label=conf_label,
    )


def get_all_legs(
    games: List[Game],
    elo: EloSystem,
    max_leg_odds: int = DEFAULT_MAX_SINGLE_LEG_ODDS,
    min_leg_odds: int = DEFAULT_MIN_LEG_ODDS,
) -> List[Leg]:
    legs: List[Leg] = []
    for g in games:
        for side in ("away", "home"):
            leg = annotate_leg(
                g, side, elo, max_leg_odds=max_leg_odds, min_leg_odds=min_leg_odds
            )
            if leg is not None:
                legs.append(leg)
    return legs


def passes_confidence_gates(leg: Leg, cfg: ConfidenceConfig) -> bool:
    """
    Every gate, in the same order the browser applies them.

    The minimum-price gate is here rather than only inside get_all_legs, which
    is where it used to live. The browser applies it in passesGates, so the two
    engines were enforcing the same rule at different layers -- identical in
    production by luck, and NOT identical in the parity harness, which feeds
    Python a leg list directly and so skipped Python's copy of the rule
    entirely. The board carries prices down to -450 while selection allows -350,
    so the harness was handing Python legs the browser could never choose. It
    surfaced the moment the pick cap was lifted: Python took Pittsburgh at -375,
    the browser could not see it, and the two engines returned different bets
    from the same board.
    """
    if cfg.exclude_stale and leg.stale:
        return False
    if leg.odds_american < cfg.min_leg_odds:
        return False
    if leg.model_win_prob < cfg.min_win_prob:
        return False
    if leg.edge * 100.0 < cfg.min_edge_pp:
        return False
    if leg.sample_games < cfg.min_sample_games:
        return False
    if leg.odds_american > cfg.max_single_leg_odds:
        return False
    # Still require strictly +EV
    if leg.edge <= 0:
        return False
    return True


def get_plus_ev_legs(
    games: List[Game],
    elo: EloSystem,
    cfg: Optional[ConfidenceConfig] = None,
) -> List[Leg]:
    cfg = cfg or ConfidenceConfig()
    legs = get_all_legs(
        games, elo,
        max_leg_odds=cfg.max_single_leg_odds,
        min_leg_odds=cfg.min_leg_odds,
    )
    return [l for l in legs if passes_confidence_gates(l, cfg)]


def _avg_edge(legs: List[Leg]) -> float:
    if not legs:
        return 0.0
    return sum(l.edge for l in legs) / len(legs)


def _avg_confidence(legs: List[Leg]) -> float:
    if not legs:
        return 0.0
    return sum(l.confidence for l in legs) / len(legs)


def _rank_key(legs: List[Leg], combined: int) -> Tuple[float, float, float, int]:
    """
    Prefer higher avg confidence, higher avg edge, fewer legs, then
    milder combined odds (not juiciest lottery).
    """
    avg_c = _avg_confidence(legs)
    avg_e = _avg_edge(legs)
    n = len(legs)
    # Soft preference for fewer legs when confidence/edge comparable
    fewer_bonus = (6 - n) * 0.5
    # Mild preference against huge combined prices
    odds_penalty = max(0, combined - 500) / 5000.0
    score = avg_c + avg_e * 100.0 + fewer_bonus - odds_penalty
    return (score, avg_c, avg_e, -n)


def _make_pick(legs: List[Leg], combined: int) -> Pick:
    avg_e = _avg_edge(legs)
    avg_c = _avg_confidence(legs)
    score_tuple = _rank_key(legs, combined)
    n = len(legs)
    if n == 1:
        leg = legs[0]
        # The edge the leg carries, not a fresh derivation from its
        # probabilities. Those agree when this engine built the leg itself, and
        # do not when the leg was rebuilt from board.json -- where the
        # probabilities and the edge have been rounded independently. Trusting
        # the field we were handed keeps one number in play instead of two.
        epp = leg.edge * 100.0
        label = (
            f"Single: {leg.team_abbr} ML {leg.odds_american:+d} "
            f"(edge {epp:+.1f}pp, conf {leg.confidence:.0f}/{leg.confidence_label})"
        )
        combined_edge = epp
    else:
        parts = " + ".join(f"{l.team_abbr} ({l.odds_american:+d})" for l in legs)
        label = f"{n}-leg: {parts} → {combined:+d}"
        combined_edge = avg_e * 100.0
    # A parlay lands only if every leg does, so the stake follows the combined
    # probability rather than the average of the legs'.
    win_p = parlay_win_prob([l.model_win_prob for l in legs])
    market_p = parlay_win_prob([l.implied_prob for l in legs])
    units = stake_units(win_p, combined, market_p)
    if units > 0:
        label = f"{units_label(units)} · {label}"
    return Pick(
        legs=list(legs),
        combined_odds=combined,
        score=score_tuple[0],
        label=label,
        combined_edge_pp=combined_edge,
        avg_confidence=avg_c,
        confidence_label=confidence_label_from_score(avg_c),
        win_prob=win_p,
        market_prob=market_p,
        stake_units=units,
        trust=trust_label(win_p, market_p),
    )


def _candidate_combos(
    available: List[Leg],
    n_legs: int,
    min_combined: int,
    pool_cap: int,
) -> List[Tuple[float, List[Leg], int]]:
    """Enumerate non-overlapping n-leg combos that clear min_combined odds."""
    if n_legs < 1 or len(available) < n_legs:
        return []
    # Prefer higher confidence then edge when capping the search pool
    ranked = sorted(
        available,
        key=lambda l: (l.confidence, l.edge),
        reverse=True,
    )
    pool = ranked[:pool_cap]
    out: List[Tuple[float, List[Leg], int]] = []
    for combo in combinations(pool, n_legs):
        legs = list(combo)
        if legs_overlap(legs):
            continue
        combined = combine_odds([l.odds_american for l in legs])
        if combined >= min_combined:
            sc = _rank_key(legs, combined)[0]
            out.append((sc, legs, combined))
    out.sort(reverse=True, key=lambda x: x[0])
    return out


def _is_minimal_parlay(legs: List[Leg], min_combined: int) -> bool:
    """
    True when every leg is needed to clear the payout floor.

    A parlay exists to reach a price its legs cannot reach alone, so it should
    stop the moment it gets there. Combined odds only lengthen as legs are
    added, so if any leg can be dropped and the rest still clear the floor, the
    extra leg is buying nothing but another slice of the house's cut.

    This matters because Kelly gets MORE permissive as a parlay lengthens. A
    two-leg parlay that sized under the stake floor comes back as a four-leg one
    at a big enough price to pass -- not because the bet got better, but because
    the payout got bigger while the win probability got smaller. That produced
    DUKE +195 / OHIO -130 / MD +100 / KENN -340 at +1251 and a 16% chance, when
    DUKE and MD alone already cleared +200 at +490 and had been rejected. The
    model is known to be miscalibrated where it claims the most edge, so trusting
    it to four decimal places of compounded probability is exactly the wrong
    place to spend that trust.
    """
    if len(legs) < 2:
        return True
    for i in range(len(legs)):
        rest = legs[:i] + legs[i + 1:]
        if combine_odds([l.odds_american for l in rest]) >= min_combined:
            return False
    return True


def build_picks(
    games: List[Game],
    elo: EloSystem,
    n: Optional[int] = None,
    cfg: Optional[ConfidenceConfig] = None,
) -> List[Pick]:
    """
    Every independent bet that clears the gates. No cap by default.

    Prefer fewer legs when they already clear the payout floor; otherwise fill
    with 2-5 leg parlays from the legs that cannot get there alone. Every leg
    must pass gates (edge, sample, max odds, staleness) and every pick must size
    to at least the stake floor.

    n used to default to 3, which was a display decision leaking into selection:
    on a heavy slate the fourth-best bet was simply never computed, so a good
    week looked identical to a thin one. The quality bar is the gates and the
    stake floor -- if a bet clears those it should be placed, and how many of
    them there are is information rather than clutter. Pass n only to truncate
    deliberately.

    The list is still bounded, and tightly: no pick may reuse a team or a game,
    so the total cannot exceed the number of games on the board.
    """
    cfg = cfg or ConfidenceConfig()
    limit = float("inf") if n is None else n
    max_legs = max(1, min(5, int(cfg.max_parlay_legs)))
    min_combined = int(cfg.min_combined_odds)

    plus_ev = sorted(
        get_plus_ev_legs(games, elo, cfg),
        key=lambda l: (l.confidence, l.edge),
        reverse=True,
    )
    if not plus_ev:
        return []

    picks: List[Pick] = []
    used_teams: Set[str] = set()
    used_games: Set[str] = set()

    def can_use(leg: Leg) -> bool:
        if leg.team_id in used_teams or leg.game.event_id in used_games:
            return False
        if _opp_id(leg) in used_teams:
            return False
        return True

    def mark_used(leg: Leg) -> None:
        used_teams.add(leg.team_id)
        used_teams.add(_opp_id(leg))
        used_games.add(leg.game.event_id)

    # Pool size caps grow slightly for smaller n_legs
    pool_caps = {1: 80, 2: 50, 3: 40, 4: 28, 5: 22}

    for n_legs in range(1, max_legs + 1):
        if len(picks) >= limit:
            break
        available = [l for l in plus_ev if can_use(l)]
        if len(available) < n_legs:
            continue

        if n_legs == 1:
            # Singles that clear min combined (+200)
            singles = [
                l
                for l in available
                if l.odds_american >= min_combined and can_use(l)
            ]
            singles.sort(key=lambda l: (l.confidence, l.edge), reverse=True)
            for leg in singles:
                if len(picks) >= limit:
                    break
                if not can_use(leg):
                    continue
                pick = _make_pick([leg], leg.odds_american)
                # A pick we would barely stake is not a pick. Leave its teams
                # free so they can still appear in a combination.
                if pick.stake_units < cfg.min_stake_units:
                    continue
                if pick.win_prob < cfg.min_pick_win_prob:
                    continue
                if disagreement_factor(pick.win_prob, pick.market_prob) < cfg.min_trust_factor:
                    continue
                picks.append(pick)
                mark_used(leg)
            continue

        # Only legs that CANNOT reach the payout floor alone may be stacked.
        #
        # A parlay's expectation is the product of its legs' expectations, so it
        # is always worse than betting the same legs separately. The only thing
        # it buys is reach: two short prices combining to clear a floor neither
        # meets alone. A leg already paying +360 has nothing to reach for, and
        # putting it in a parlay just pays the house's cut twice for one
        # opinion.
        #
        # Without this the engine laundered its own rejects. A +360 single whose
        # stake came in under the floor was dropped, then reappeared inside a
        # three-leg parlay -- where the far longer combined price inflates the
        # Kelly cap enough to clear the same floor. The result was tickets like
        # DUKE +195 / SDSU +360 / GT +370 at +6278 and an 11% chance, built
        # entirely from bets that had just been judged too weak to place.
        stackable = [l for l in available if l.odds_american < min_combined]
        if len(stackable) < n_legs:
            continue
        candidates = _candidate_combos(
            stackable,
            n_legs,
            min_combined,
            pool_cap=pool_caps.get(n_legs, 25),
        )
        for _sc, legs_combo, comb in candidates:
            if len(picks) >= limit:
                break
            if any(not can_use(l) for l in legs_combo):
                continue
            if not _is_minimal_parlay(legs_combo, min_combined):
                continue
            pick = _make_pick(legs_combo, comb)
            if pick.stake_units < cfg.min_stake_units:
                continue
            if pick.win_prob < cfg.min_pick_win_prob:
                continue
            if disagreement_factor(pick.win_prob, pick.market_prob) < cfg.min_trust_factor:
                continue
            picks.append(pick)
            for l in legs_combo:
                mark_used(l)

    # Re-rank selected picks for display (confidence + edge)
    picks.sort(key=lambda p: (p.avg_confidence, p.combined_edge_pp), reverse=True)
    return picks if n is None else picks[:n]


def summarize_board(
    games: List[Game],
    elo: EloSystem,
    cfg: Optional[ConfidenceConfig] = None,
) -> Dict:
    """Diagnostics for smoke tests / UI notes."""
    cfg = cfg or ConfidenceConfig()
    all_legs = get_all_legs(games, elo, max_leg_odds=cfg.max_single_leg_odds)
    gated = [l for l in all_legs if passes_confidence_gates(l, cfg)]
    plus_raw = [l for l in all_legs if l.edge > 0]
    return {
        "games": len(games),
        "legs": len(all_legs),
        "plus_ev_legs": len(plus_raw),
        "gated_legs": len(gated),
        "top_edges": sorted(gated or plus_raw, key=lambda l: l.edge, reverse=True)[:5],
        "cfg": {
            "min_edge_pp": cfg.min_edge_pp,
            "min_sample_games": cfg.min_sample_games,
            "max_single_leg_odds": cfg.max_single_leg_odds,
            "max_parlay_legs": cfg.max_parlay_legs,
            "min_combined_odds": cfg.min_combined_odds,
            "high_confidence_mode": cfg.high_confidence_mode,
        },
    }
