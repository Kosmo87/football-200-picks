#!/usr/bin/env python3
"""
Football Tip Engine v0.7
Elo/model win probability vs market implied odds (+EV), confidence bands,
and 1–5 leg parlays targeting +200 American with shorter high-confidence legs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import streamlit as st

from elo import EloSystem, config_for_league
from espn import fetch_completed_games, get_upcoming_games
from odds import edge_pp
from picks import (
    ConfidenceConfig,
    Pick,
    build_picks,
    summarize_board,
)

VERSION = "v0.7 tip-engine"


def format_game_time(iso_date: str) -> str:
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%a %b %d · %H:%M UTC")
    except Exception:
        return iso_date


def format_pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def conf_badge(label: str, score: float) -> str:
    emoji = {"High": "🟢", "Med": "🟡", "Low": "🟠"}.get(label, "⚪")
    return f"{emoji} **{label}** ({score:.0f})"


def load_league(league: str, cfg: ConfidenceConfig) -> dict:
    games = get_upcoming_games(league)
    completed = fetch_completed_games(league)
    elo = EloSystem(config=config_for_league(league)).build(completed)
    picks = build_picks(games, elo, n=3, cfg=cfg)
    summary = summarize_board(games, elo, cfg=cfg)
    return {
        "games": games,
        "completed": completed,
        "elo": elo,
        "picks": picks,
        "summary": summary,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Football Tip Engine",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🏈 Football Tip Engine (+EV)")
st.caption(
    f"{VERSION} · Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
    "· Confidence-gated · Singles → 5-leg stacks for +200"
)

# --- Sidebar confidence controls ---
with st.sidebar:
    st.header("Confidence filters")
    high_mode = st.checkbox(
        "High confidence mode",
        value=False,
        help="Raises min edge / min sample, caps long dogs at +350, allows up to 5 legs.",
    )
    st.caption(
        "Defaults: min edge **5.0** pp · sample **3** NFL / **4** NCAAF · "
        "max leg **+600** · max legs **5** · min combined **+200**."
    )
    min_edge = st.slider("Min edge (pp)", 0.0, 15.0, 5.0, 0.5)
    sample_note = st.radio(
        "Min Elo sample games",
        options=["League defaults (NFL 3 / NCAAF 4)", "Shared value"],
        index=0,
    )
    if sample_note.startswith("Shared"):
        min_sample_shared = st.slider("Min sample (both leagues)", 0, 12, 3, 1)
        min_sample_nfl = min_sample_shared
        min_sample_ncaaf = min_sample_shared
    else:
        min_sample_nfl = st.slider("Min sample NFL", 0, 12, 3, 1)
        min_sample_ncaaf = st.slider("Min sample NCAAF", 0, 12, 4, 1)
        st.caption("Sample = min completed games rated for both teams in the matchup.")

    max_leg_odds = st.slider(
        "Max single-leg American odds",
        150,
        900,
        600,
        25,
        help="Filters lottery tickets; shorter prices stack into +200 parlays.",
    )
    max_legs = st.slider("Max parlay legs (1 = singles only)", 1, 5, 5, 1)
    min_combined = st.number_input(
        "Min combined American odds",
        min_value=100,
        max_value=1000,
        value=200,
        step=25,
    )

    if high_mode:
        st.info(
            "High confidence preset active: min edge ≥ 7.5 pp, higher sample floors, "
            "max leg ≤ +350, up to 5 legs."
        )


with st.expander("How the tip engine works", expanded=False):
    st.markdown(
        """
**Methodology**
- Build simple **Elo ratings** from recent completed games (NFL: preseason + regular;
  NCAAF FBS: ~45-day lookback via ESPN scoreboards).
- For each upcoming moneyline side: **model_win_prob** from Elo difference + home-field;
  **implied_prob** from American odds (de-vigged when both sides are priced).
- **Edge** = model_win_prob − implied_prob. Tips require **edge > 0** and confidence gates.
- **Confidence (0–100)** blends sample depth, edge size, and how short the price is
  (legs nearer even money score higher). Badges: High ≥70 · Med ≥40 · Low &lt;40.
- Target combined American odds **≥ +200**. Prefer **fewer legs** when they clear +200
  with high confidence; otherwise stack **3 / 4 / 5** shorter +EV legs. Exactly **3**
  independent tips per league when possible (zero overlapping teams/games by team ID).

**Why multi-leg +200?** Stacking shorter +EV prices under confidence filters often beats
a single long dog for sample quality and price stability.

**Not advice** — educational prototype only. Markets are efficient; a positive model
edge is not a guarantee of profit.
"""
    )

col_btn, col_hint = st.columns([1, 3])
with col_btn:
    refresh = st.button("🔄 Refresh Tips", type="primary", use_container_width=True)
with col_hint:
    st.caption(
        "Cached ~3 minutes. Elo rebuilds from ESPN completed games on each refresh. "
        "Sidebar filters apply on load/refresh."
    )

if refresh:
    st.cache_data.clear()


@st.cache_data(ttl=180, show_spinner=False)
def load_all(
    min_edge_pp: float,
    min_sample_nfl: int,
    min_sample_ncaaf: int,
    max_leg_odds: int,
    max_legs: int,
    min_combined: int,
    high_mode: bool,
):
    results = {}
    for league in ("NFL", "NCAAF"):
        cfg = ConfidenceConfig(
            min_edge_pp=min_edge_pp,
            min_sample_games=(
                min_sample_nfl if league == "NFL" else min_sample_ncaaf
            ),
            max_single_leg_odds=max_leg_odds,
            max_parlay_legs=max_legs,
            min_combined_odds=min_combined,
            high_confidence_mode=high_mode,
        )
        if high_mode:
            cfg.apply_high_confidence_preset(league)
            cfg.min_edge_pp = max(min_edge_pp, cfg.min_edge_pp)
            base = min_sample_nfl if league == "NFL" else min_sample_ncaaf
            cfg.min_sample_games = max(base, cfg.min_sample_games)
            cfg.max_single_leg_odds = min(max_leg_odds, cfg.max_single_leg_odds)
        try:
            results[league] = load_league(league, cfg)
        except Exception as e:
            results[league] = {
                "games": [],
                "completed": [],
                "elo": None,
                "picks": [],
                "summary": {},
                "error": str(e),
            }
    return results


with st.spinner("Fetching ESPN games, building Elo, scanning confidence-gated +EV…"):
    data = load_all(
        float(min_edge),
        int(min_sample_nfl),
        int(min_sample_ncaaf),
        int(max_leg_odds),
        int(max_legs),
        int(min_combined),
        bool(high_mode),
    )


def render_pick_card(pick: Pick, index: int):
    n_legs = len(pick.legs)
    kind = {
        1: "Single",
        2: "2-leg parlay",
        3: "3-leg parlay",
        4: "4-leg parlay",
        5: "5-leg parlay",
    }.get(n_legs, f"{n_legs}-leg")
    try:
        container = st.container(border=True)
    except TypeError:
        container = st.container()

    with container:
        st.markdown(
            f"**Tip #{index}** · {kind} · {conf_badge(pick.confidence_label, pick.avg_confidence)}"
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Combined odds", f"{pick.combined_odds:+d}")
        m2.metric("Avg edge", f"{pick.combined_edge_pp:+.1f} pp")
        m3.metric("Avg conf", f"{pick.avg_confidence:.0f}")
        m4.metric("Legs", str(n_legs))
        st.caption(pick.label)
        if n_legs >= 3:
            st.caption(
                "Multi-leg +200 stacks shorter +EV prices under confidence filters "
                "(sample / edge / shortness) instead of one long dog."
            )

        for leg in pick.legs:
            g = leg.game
            st.markdown(
                f"**{leg.team_name}** (`{leg.team_abbr}`) ML **{leg.odds_american:+d}** · "
                f"{conf_badge(leg.confidence_label, leg.confidence)}"
            )
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Model win%", format_pct(leg.model_win_prob))
            c2.metric("Implied win%", format_pct(leg.implied_prob))
            c3.metric("Edge", f"{edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp")
            c4.metric("Fair odds", f"{leg.fair_odds:+d}")
            c5.metric("Sample games", str(leg.sample_games))

            meta = f"{g.away.abbreviation} @ {g.home.abbreviation}"
            when = format_game_time(g.date)
            if when:
                meta += f" · {when}"
            st.caption(meta)
            if g.odds.spread_details or g.odds.total is not None:
                bits = []
                if g.odds.spread_details:
                    bits.append(g.odds.spread_details)
                if g.odds.total is not None:
                    bits.append(f"O/U {g.odds.total}")
                bits.append(g.odds.provider)
                st.caption(" · ".join(bits))


for league in ("NFL", "NCAAF"):
    section = data[league]
    st.header(league)

    if section["error"]:
        st.error(f"Could not load {league}: {section['error']}")
        st.info(
            "ESPN sometimes rate-limits or times out. Wait 30–60 seconds, then click "
            "**Refresh Tips**. Check your network if this persists."
        )
        continue

    n_games = len(section["games"])
    n_completed = len(section["completed"])
    summary = section.get("summary") or {}
    n_plus = summary.get("plus_ev_legs", 0)
    n_gated = summary.get("gated_legs", 0)
    cfg_info = summary.get("cfg") or {}

    if n_games == 0:
        st.warning(
            f"No upcoming {league} games with moneyline odds right now. "
            "Try again closer to game week, or hit Refresh after lines post."
        )
        continue

    st.write(
        f"Upcoming with moneylines: **{n_games}** · "
        f"Completed games in Elo: **{n_completed}** · "
        f"+EV sides: **{n_plus}** · "
        f"Pass confidence gates: **{n_gated}**"
    )
    if cfg_info:
        st.caption(
            f"Gates: min edge {cfg_info.get('min_edge_pp')} pp · "
            f"min sample {cfg_info.get('min_sample_games')} · "
            f"max leg {cfg_info.get('max_single_leg_odds'):+d} · "
            f"max legs {cfg_info.get('max_parlay_legs')} · "
            f"min combined {cfg_info.get('min_combined_odds'):+d}"
            + (
                " · high-confidence mode"
                if cfg_info.get("high_confidence_mode")
                else ""
            )
        )

    if n_completed < 10:
        st.warning(
            f"Elo is thin for {league} ({n_completed} completed games). "
            "Edges are noisier early in the season — treat tips cautiously."
        )

    picks: List[Pick] = section["picks"]
    if not picks:
        st.warning(
            "No qualifying tips under current confidence filters "
            "(edge / sample / max odds) with combined odds ≥ target and "
            "no overlapping teams/games. Loosen sidebar gates or wait for more results."
        )
        top = summary.get("top_edges") or []
        if not top:
            from picks import get_all_legs

            if section["elo"] is not None:
                all_legs = get_all_legs(section["games"], section["elo"])
                top = sorted(all_legs, key=lambda l: l.edge, reverse=True)[:5]
        if top:
            with st.expander("Closest edges on the board (not selected)"):
                for leg in top:
                    st.write(
                        f"- {leg.team_abbr} ML {leg.odds_american:+d}: "
                        f"model {format_pct(leg.model_win_prob)} vs "
                        f"implied {format_pct(leg.implied_prob)} → "
                        f"edge {edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp · "
                        f"conf {leg.confidence:.0f}/{leg.confidence_label} · "
                        f"sample {leg.sample_games}"
                    )
        continue

    if len(picks) < 3:
        st.info(
            f"Only **{len(picks)}** independent tip(s) available "
            f"(target is 3) under confidence gates without overlapping teams/games."
        )

    cols = st.columns(min(3, len(picks)))
    for i, pick in enumerate(picks):
        with cols[i]:
            render_pick_card(pick, i + 1)

    with st.expander(f"Details – {league}"):
        for i, pick in enumerate(picks, 1):
            st.markdown(
                f"**Tip #{i} → {pick.combined_odds:+d}** "
                f"(avg edge {pick.combined_edge_pp:+.1f} pp · "
                f"avg conf {pick.avg_confidence:.0f}/{pick.confidence_label} · "
                f"{len(pick.legs)} leg(s))"
            )
            for leg in pick.legs:
                st.write(
                    f"- {leg.team_name} ML {leg.odds_american:+d} · "
                    f"model {format_pct(leg.model_win_prob)} · "
                    f"implied {format_pct(leg.implied_prob)} · "
                    f"edge {edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp · "
                    f"fair {leg.fair_odds:+d} · "
                    f"conf {leg.confidence:.0f}/{leg.confidence_label} · "
                    f"sample {leg.sample_games}  "
                    f"({leg.game.away.abbreviation} @ {leg.game.home.abbreviation})"
                )
            st.divider()

    elo = section["elo"]
    if elo is not None:
        with st.expander(f"Elo snapshot – top 10 {league}"):
            rows = []
            for tid, rating, gp in elo.top(10):
                name = elo.names.get(tid) or elo.abbrs.get(tid) or tid
                rows.append(
                    {
                        "Team": name,
                        "Elo": round(rating, 1),
                        "Games rated": gp,
                    }
                )
            st.table(rows)

st.divider()
st.caption(
    "Educational prototype only — not betting advice and not a guarantee of profit. "
    "Always verify lines at your sportsbook. Bet responsibly. "
    "If you or someone you know has a gambling problem, call 1-800-GAMBLER."
)
