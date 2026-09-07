#!/usr/bin/env python3
"""
Football Tip Engine v0.6
Elo/model win probability vs market implied odds (+EV), targeting +200 American.
Exactly 3 independent picks per league when enough +EV candidates exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import streamlit as st

from elo import EloSystem, config_for_league
from espn import fetch_completed_games, get_upcoming_games
from odds import edge_pp
from picks import Pick, build_picks, summarize_board

VERSION = "v0.6 tip-engine"


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


def load_league(league: str) -> dict:
    games = get_upcoming_games(league)
    completed = fetch_completed_games(league)
    elo = EloSystem(config=config_for_league(league)).build(completed)
    picks = build_picks(games, elo, n=3)
    summary = summarize_board(games, elo)
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
    initial_sidebar_state="collapsed",
)

st.title("🏈 Football Tip Engine (+EV)")
st.caption(
    f"{VERSION} · Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
    "· Model win% vs market implied · Singles preferred"
)

with st.expander("How the tip engine works", expanded=False):
    st.markdown(
        """
**Methodology**
- Build simple **Elo ratings** from recent completed games (NFL: preseason + regular;
  NCAAF FBS: ~45-day lookback via ESPN scoreboards).
- For each upcoming moneyline side: **model_win_prob** from Elo difference + home-field;
  **implied_prob** from American odds (de-vigged when both sides are priced).
- **Edge** = model_win_prob − implied_prob. Tips require **edge > 0** (+EV).
- Target combined American odds **+200 or better**. Prefer **singles**, then **2-leg**,
  then **3-leg** parlays. Exactly **3 picks per league** when enough independent +EV
  candidates exist (zero overlapping teams/games by team ID).

**Not advice** — educational prototype only. Markets are efficient; a positive model
edge is not a guarantee of profit.
"""
    )

col_btn, col_hint = st.columns([1, 3])
with col_btn:
    refresh = st.button("🔄 Refresh Tips", type="primary", use_container_width=True)
with col_hint:
    st.caption(
        "Cached ~3 minutes. Elo rebuilds from ESPN completed games on each refresh."
    )

if refresh:
    st.cache_data.clear()


@st.cache_data(ttl=180, show_spinner=False)
def load_all():
    results = {}
    for league in ("NFL", "NCAAF"):
        try:
            results[league] = load_league(league)
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


with st.spinner("Fetching ESPN games, building Elo, scanning +EV edges…"):
    data = load_all()


def render_pick_card(pick: Pick, index: int):
    n_legs = len(pick.legs)
    kind = {1: "Single", 2: "2-leg parlay", 3: "3-leg parlay"}.get(n_legs, f"{n_legs}-leg")
    try:
        container = st.container(border=True)
    except TypeError:
        container = st.container()

    with container:
        st.markdown(f"**Tip #{index}** · {kind}")
        m1, m2, m3 = st.columns(3)
        m1.metric("Combined odds", f"{pick.combined_odds:+d}")
        m2.metric("Avg edge", f"{pick.combined_edge_pp:+.1f} pp")
        m3.metric("Legs", str(n_legs))
        st.caption(pick.label)

        for leg in pick.legs:
            g = leg.game
            st.markdown(
                f"**{leg.team_name}** (`{leg.team_abbr}`) ML **{leg.odds_american:+d}**"
            )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Model win%", format_pct(leg.model_win_prob))
            c2.metric("Implied win%", format_pct(leg.implied_prob))
            c3.metric("Edge", f"{edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp")
            c4.metric("Fair odds", f"{leg.fair_odds:+d}")

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

    if n_games == 0:
        st.warning(
            f"No upcoming {league} games with moneyline odds right now. "
            "Try again closer to game week, or hit Refresh after lines post."
        )
        continue

    st.write(
        f"Upcoming with moneylines: **{n_games}** · "
        f"Completed games in Elo: **{n_completed}** · "
        f"+EV sides: **{n_plus}**"
    )

    if n_completed < 10:
        st.warning(
            f"Elo is thin for {league} ({n_completed} completed games). "
            "Edges are noisier early in the season — treat tips cautiously."
        )

    picks: List[Pick] = section["picks"]
    if not picks:
        st.warning(
            "No qualifying +EV tips found (edge > 0 and combined odds ≥ +200) "
            "without overlapping teams/games. Board may be efficient, or Elo needs "
            "more completed results."
        )
        # Still show top near-misses from summary if any legs exist
        top = summary.get("top_edges") or []
        if not top:
            # show closest edges even if negative for transparency
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
                        f"edge {edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp"
                    )
        continue

    if len(picks) < 3:
        st.info(
            f"Only **{len(picks)}** independent +EV tip(s) available "
            f"(target is 3) without overlapping teams/games."
        )

    cols = st.columns(min(3, len(picks)))
    for i, pick in enumerate(picks):
        with cols[i]:
            render_pick_card(pick, i + 1)

    with st.expander(f"Details – {league}"):
        for i, pick in enumerate(picks, 1):
            st.markdown(
                f"**Tip #{i} → {pick.combined_odds:+d}** "
                f"(avg edge {pick.combined_edge_pp:+.1f} pp)"
            )
            for leg in pick.legs:
                st.write(
                    f"- {leg.team_name} ML {leg.odds_american:+d} · "
                    f"model {format_pct(leg.model_win_prob)} · "
                    f"implied {format_pct(leg.implied_prob)} · "
                    f"edge {edge_pp(leg.model_win_prob, leg.implied_prob):+.1f} pp · "
                    f"fair {leg.fair_odds:+d}  "
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
