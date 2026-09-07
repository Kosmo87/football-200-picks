#!/usr/bin/env python3
"""
Football +200 Picks Dashboard v0.5
- Prefers single +200+ moneyline underdogs
- Falls back to independent 2-leg or 3-leg parlays when needed
- 3 total picks per league, zero overlapping teams/games
"""

import streamlit as st
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from dataclasses import dataclass
import time
from itertools import combinations

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MIN_ODDS = 200
MAX_SINGLE_ODDS = 900
PREFERRED_PROVIDER = "Draft Kings"  # ESPN provider name (space, not "DraftKings")
# Prefer site.web.api; site.api is often Akamai-blocked (403) from cloud IPs
ESPN_HOSTS = (
    "https://site.web.api.espn.com",
    "https://site.api.espn.com",
)
ESPN_NFL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_NCAAF_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TeamInfo:
    id: str
    name: str
    abbreviation: str
    home_away: str

@dataclass
class OddsInfo:
    provider: str
    moneyline_home: Optional[int] = None
    moneyline_away: Optional[int] = None
    spread: Optional[float] = None
    spread_details: Optional[str] = None
    total: Optional[float] = None

@dataclass
class Game:
    event_id: str
    name: str
    short_name: str
    date: str
    status: str
    home: TeamInfo
    away: TeamInfo
    odds: OddsInfo

@dataclass
class Leg:
    game: Game
    side: str
    team_name: str
    team_abbr: str
    team_id: str
    odds_american: int

@dataclass
class Pick:
    legs: List[Leg]
    combined_odds: int
    score: float
    label: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
        return 1 + odds / 100
    return 1 + 100 / abs(odds)

def decimal_to_american(dec: float) -> int:
    if dec >= 2.0:
        return int(round((dec - 1) * 100))
    return int(round(-100 / (dec - 1)))

def combine_odds(odds_list: List[int]) -> int:
    dec = 1.0
    for o in odds_list:
        dec *= american_to_decimal(o)
    return decimal_to_american(dec)

def is_upcoming(status_type: dict) -> bool:
    """Prefer ESPN state == 'pre'; fall back to description heuristics."""
    state = (status_type.get("state") or "").lower()
    if state:
        return state == "pre"
    s = (status_type.get("description") or "").lower()
    return not any(
        x in s
        for x in (
            "final",
            "in progress",
            "halftime",
            "end of",
            "delayed",
            "postponed",
            "canceled",
            "cancelled",
        )
    )

def _side_moneyline(ml_side: Optional[dict]) -> Optional[int]:
    """Read close odds, then open, then flat odds from a moneyline side object."""
    if not ml_side or not isinstance(ml_side, dict):
        return None
    for key in ("close", "open"):
        nested = ml_side.get(key)
        if isinstance(nested, dict):
            parsed = parse_american_odds(nested.get("odds"))
            if parsed is not None:
                return parsed
    return parse_american_odds(ml_side.get("odds"))

def _provider_matches(name: str, preferred: str) -> bool:
    a = "".join((name or "").lower().split())
    b = "".join((preferred or "").lower().split())
    return bool(a) and a == b

def _espn_url_candidates(url: str) -> list:
    """Try preferred ESPN hosts when a URL uses a known ESPN API host."""
    candidates = [url]
    for host in ESPN_HOSTS:
        marker = "://"
        if marker not in url:
            continue
        rest = url.split(marker, 1)[1]
        path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
        alt = host + path
        if alt not in candidates:
            candidates.append(alt)
    return candidates


def fetch_scoreboard(url: str, retries: int = 3) -> dict:
    last_error = None
    candidates = _espn_url_candidates(url)
    for attempt in range(retries):
        for candidate in candidates:
            try:
                r = requests.get(candidate, headers=HEADERS, timeout=15)
                if r.status_code == 200:
                    return r.json()
                last_error = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    last_error = "HTTP 429 (rate limited)"
                if r.status_code in (401, 403):
                    continue  # try next host immediately
            except requests.Timeout:
                last_error = "request timed out"
            except requests.RequestException as e:
                last_error = str(e)
            except Exception as e:
                last_error = str(e)
        time.sleep(1.2 + attempt)
    raise RuntimeError(f"ESPN fetch failed after {retries} tries: {last_error}")

def parse_game(event: dict) -> Optional[Game]:
    try:
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        comp = competitions[0]
        status_type = (comp.get("status") or {}).get("type") or {}
        status = status_type.get("description", "Unknown")
        if not is_upcoming(status_type):
            return None

        competitors = {
            c["homeAway"]: c for c in comp.get("competitors", []) if "homeAway" in c
        }
        if "home" not in competitors or "away" not in competitors:
            return None

        home_c, away_c = competitors["home"], competitors["away"]
        home = TeamInfo(
            id=str(home_c.get("id") or home_c.get("team", {}).get("id") or ""),
            name=home_c.get("team", {}).get("displayName", "Home"),
            abbreviation=home_c.get("team", {}).get("abbreviation", "HOME"),
            home_away="home",
        )
        away = TeamInfo(
            id=str(away_c.get("id") or away_c.get("team", {}).get("id") or ""),
            name=away_c.get("team", {}).get("displayName", "Away"),
            abbreviation=away_c.get("team", {}).get("abbreviation", "AWAY"),
            home_away="away",
        )
        if not home.id or not away.id:
            return None

        odds_list = comp.get("odds") or []
        odds_data = None
        for o in odds_list:
            pname = (o.get("provider") or {}).get("name", "")
            if _provider_matches(pname, PREFERRED_PROVIDER):
                odds_data = o
                break
        if not odds_data and odds_list:
            odds_data = odds_list[0]
        if not odds_data:
            return None

        ml = odds_data.get("moneyline") or {}
        home_ml = _side_moneyline(ml.get("home"))
        away_ml = _side_moneyline(ml.get("away"))

        # Legacy / alternate ESPN shapes
        if home_ml is None:
            home_ml = parse_american_odds(
                (odds_data.get("homeTeamOdds") or {}).get("moneyLine")
            )
        if away_ml is None:
            away_ml = parse_american_odds(
                (odds_data.get("awayTeamOdds") or {}).get("moneyLine")
            )

        return Game(
            event_id=str(event.get("id")),
            name=event.get("name", ""),
            short_name=event.get("shortName", ""),
            date=event.get("date", ""),
            status=status,
            home=home,
            away=away,
            odds=OddsInfo(
                provider=(odds_data.get("provider") or {}).get("name", "Unknown"),
                moneyline_home=home_ml,
                moneyline_away=away_ml,
                spread=odds_data.get("spread"),
                spread_details=odds_data.get("details"),
                total=odds_data.get("overUnder"),
            ),
        )
    except Exception:
        return None

def get_games(league: str) -> List[Game]:
    events = []
    if league.upper() == "NFL":
        data = fetch_scoreboard(ESPN_NFL)
        events = data.get("events") or []
    else:
        today = datetime.now(timezone.utc).date()
        fetch_errors = []
        for i in range(0, 9):
            d = (today + timedelta(days=i)).strftime("%Y%m%d")
            url = f"{ESPN_NCAAF_BASE}?dates={d}&groups=80&limit=120"
            try:
                data = fetch_scoreboard(url)
                events.extend(data.get("events") or [])
            except Exception as e:
                fetch_errors.append(str(e))
                continue
        if not events and fetch_errors:
            raise RuntimeError(f"NCAAF scoreboard unavailable ({fetch_errors[0]})")
        seen = set()
        unique = []
        for ev in events:
            eid = ev.get("id")
            if eid and eid not in seen:
                seen.add(eid)
                unique.append(ev)
        events = unique

    games = []
    for ev in events:
        g = parse_game(ev)
        if g and g.odds.moneyline_home is not None and g.odds.moneyline_away is not None:
            games.append(g)
    return games

def get_all_legs(games: List[Game]) -> List[Leg]:
    legs = []
    for g in games:
        for side, team, odds in [
            ("away", g.away, g.odds.moneyline_away),
            ("home", g.home, g.odds.moneyline_home),
        ]:
            # Allow slight favorites for parlay legs; singles still filtered to +200+
            if odds is not None and -200 <= odds <= MAX_SINGLE_ODDS:
                legs.append(
                    Leg(
                        game=g,
                        side=side,
                        team_name=team.name,
                        team_abbr=team.abbreviation,
                        team_id=team.id,
                        odds_american=odds,
                    )
                )
    return legs

def score_leg(leg: Leg) -> float:
    # Prefer underdogs closer to +200, slight home bias, closer spreads
    distance = abs(leg.odds_american - 200) if leg.odds_american > 0 else 300
    score = 800 - distance * 1.2
    if leg.side == "home":
        score += 25
    if leg.game.odds.spread is not None:
        abs_spread = abs(leg.game.odds.spread)
        if abs_spread <= 7:
            score += 40
        elif abs_spread >= 14:
            score -= 30
    return score

def _opp_id(leg: Leg) -> str:
    return leg.game.home.id if leg.side == "away" else leg.game.away.id

def legs_overlap(legs: List[Leg]) -> bool:
    teams = set()
    games = set()
    for leg in legs:
        if leg.team_id in teams or leg.game.event_id in games:
            return True
        teams.add(leg.team_id)
        teams.add(_opp_id(leg))
        games.add(leg.game.event_id)
    return False

def build_picks(games: List[Game], n: int = 3) -> List[Pick]:
    legs = get_all_legs(games)
    if not legs:
        return []

    ranked_legs = sorted(legs, key=score_leg, reverse=True)

    picks: List[Pick] = []
    used_teams = set()
    used_games = set()

    def can_use(leg: Leg) -> bool:
        if leg.team_id in used_teams or leg.game.event_id in used_games:
            return False
        if _opp_id(leg) in used_teams:
            return False
        return True

    def mark_used(leg: Leg):
        used_teams.add(leg.team_id)
        used_teams.add(_opp_id(leg))
        used_games.add(leg.game.event_id)

    # 1. Pure singles that already clear +200
    for leg in ranked_legs:
        if len(picks) >= n:
            break
        if leg.odds_american >= MIN_ODDS and can_use(leg):
            picks.append(
                Pick(
                    legs=[leg],
                    combined_odds=leg.odds_american,
                    score=score_leg(leg) + 100,
                    label=f"Single: {leg.team_abbr} ML {leg.odds_american:+d}",
                )
            )
            mark_used(leg)

    # 2. Independent 2-leg parlays
    if len(picks) < n:
        available = [l for l in ranked_legs if can_use(l)]
        best_parlays = []
        for a, b in combinations(available, 2):
            if legs_overlap([a, b]):
                continue
            combined = combine_odds([a.odds_american, b.odds_american])
            if combined >= MIN_ODDS:
                sc = (score_leg(a) + score_leg(b)) / 2
                best_parlays.append((sc, [a, b], combined))
        best_parlays.sort(reverse=True)

        for sc, legs_pair, comb in best_parlays:
            if len(picks) >= n:
                break
            if any(not can_use(l) for l in legs_pair):
                continue
            label = " + ".join(
                f"{l.team_abbr} ({l.odds_american:+d})" for l in legs_pair
            )
            picks.append(
                Pick(
                    legs=legs_pair,
                    combined_odds=comb,
                    score=sc,
                    label=f"2-leg: {label} \u2192 {comb:+d}",
                )
            )
            for l in legs_pair:
                mark_used(l)

    # 3. Last resort: 3-leg parlays
    if len(picks) < n:
        available = [l for l in ranked_legs if can_use(l)]
        best_parlays = []
        # Cap combinatorial explosion on large NCAAF boards
        pool = available[:40]
        for combo in combinations(pool, 3):
            if legs_overlap(list(combo)):
                continue
            odds = [l.odds_american for l in combo]
            combined = combine_odds(odds)
            if combined >= MIN_ODDS:
                sc = sum(score_leg(l) for l in combo) / 3
                best_parlays.append((sc, list(combo), combined))
        best_parlays.sort(reverse=True)

        for sc, legs3, comb in best_parlays:
            if len(picks) >= n:
                break
            if any(not can_use(l) for l in legs3):
                continue
            label = " + ".join(f"{l.team_abbr} ({l.odds_american:+d})" for l in legs3)
            picks.append(
                Pick(
                    legs=legs3,
                    combined_odds=comb,
                    score=sc,
                    label=f"3-leg: {label} \u2192 {comb:+d}",
                )
            )
            for l in legs3:
                mark_used(l)

    return picks[:n]

def format_game_time(iso_date: str) -> str:
    if not iso_date:
        return ""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%a %b %d \u00b7 %H:%M UTC")
    except Exception:
        return iso_date

# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Football +200 Picks",
    page_icon="\U0001f3c8",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("\U0001f3c8 Football +200 Independent Picks")
st.caption(
    f"v0.5 \u00b7 Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
    "\u00b7 Singles preferred, parlays used when needed"
)

with st.expander("How picks work", expanded=False):
    st.markdown(
        """
**Rules**
- Target: every pick pays **+200 or better** (American odds)
- Prefer pure moneyline **singles**
- If not enough singles \u2192 build independent **2-leg** then **3-leg** parlays
- Exactly **3 picks per league** (NFL + NCAAF) with **zero overlapping teams or games**
  (a picked side and its opponent both lock out of other picks)
- Odds from ESPN scoreboard APIs, preferring **Draft Kings** when available
"""
    )

col_btn, col_hint = st.columns([1, 3])
with col_btn:
    refresh = st.button("\U0001f504 Refresh Picks", type="primary", use_container_width=True)
with col_hint:
    st.caption("Cached ~3 minutes. Refresh after a rate-limit wait if a league fails to load.")

if refresh:
    st.cache_data.clear()

@st.cache_data(ttl=180, show_spinner=False)
def load_all():
    results = {}
    for league in ("NFL", "NCAAF"):
        try:
            games = get_games(league)
            picks = build_picks(games, n=3)
            results[league] = {"games": len(games), "picks": picks, "error": None}
        except Exception as e:
            results[league] = {"games": 0, "picks": [], "error": str(e)}
    return results

with st.spinner("Fetching live odds from ESPN\u2026"):
    data = load_all()

def render_pick_card(pick: Pick, index: int):
    n_legs = len(pick.legs)
    kind = {1: "Single", 2: "2-leg parlay", 3: "3-leg parlay"}.get(n_legs, f"{n_legs}-leg")
    try:
        container = st.container(border=True)
    except TypeError:
        container = st.container()

    with container:
        st.markdown(f"**Pick #{index}** \u00b7 {kind}")
        m1, m2 = st.columns(2)
        m1.metric("Combined odds", f"{pick.combined_odds:+d}")
        m2.metric("Legs", str(n_legs))
        st.caption(pick.label)

        for leg in pick.legs:
            g = leg.game
            st.markdown(
                f"**{leg.team_name}** (`{leg.team_abbr}`) ML **{leg.odds_american:+d}**"
            )
            meta = f"{g.away.abbreviation} @ {g.home.abbreviation}"
            when = format_game_time(g.date)
            if when:
                meta += f" \u00b7 {when}"
            st.caption(meta)
            if g.odds.spread_details or g.odds.total is not None:
                bits = []
                if g.odds.spread_details:
                    bits.append(g.odds.spread_details)
                if g.odds.total is not None:
                    bits.append(f"O/U {g.odds.total}")
                bits.append(g.odds.provider)
                st.caption(" \u00b7 ".join(bits))

for league in ("NFL", "NCAAF"):
    section = data[league]
    st.header(league)

    if section["error"]:
        st.error(f"Could not load {league}: {section['error']}")
        st.info(
            "ESPN sometimes rate-limits or times out. Wait 30\u201360 seconds, then click "
            "**Refresh Picks**. Check your network if this persists."
        )
        continue

    if section["games"] == 0:
        st.warning(
            f"No upcoming {league} games with moneyline odds right now. "
            "Try again closer to game week, or hit Refresh after lines post."
        )
        continue

    st.write(f"Games loaded with moneylines: **{section['games']}**")

    picks = section["picks"]
    if not picks:
        st.warning(
            "No qualifying +200+ independent picks/parlays found from the current board. "
            "Lines may be too short on singles and not enough independent legs to combine."
        )
        continue

    if len(picks) < 3:
        st.info(
            f"Only **{len(picks)}** independent +200+ pick(s) available "
            f"(target is 3) without overlapping teams/games."
        )

    cols = st.columns(min(3, len(picks)))
    for i, pick in enumerate(picks):
        with cols[i]:
            render_pick_card(pick, i + 1)

    with st.expander(f"Details \u2013 {league}"):
        for i, pick in enumerate(picks, 1):
            st.markdown(f"**Pick #{i} \u2192 {pick.combined_odds:+d}**")
            for leg in pick.legs:
                st.write(
                    f"- {leg.team_name} ML {leg.odds_american:+d}  "
                    f"({leg.game.away.abbreviation} @ {leg.game.home.abbreviation})"
                )
            st.divider()

st.divider()
st.caption(
    "Educational prototype only \u2014 not betting advice. Always verify lines at your sportsbook. "
    "Bet responsibly. If you or someone you know has a gambling problem, call 1-800-GAMBLER."
)
