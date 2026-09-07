# Football +200 Independent Picks

Local Streamlit dashboard that builds **exactly 3 independent +200 (or better) American-odds picks** for **NFL** and **NCAAF**.

It prefers moneyline **singles**, then falls back to independent **2-leg** and **3-leg** parlays when needed. No overlapping teams or games across the three picks (a selected side and its opponent are both locked out).

Odds come from ESPN public scoreboard APIs, preferring the **Draft Kings** provider when present.

**Educational prototype only — not betting advice. Bet responsibly.**

## Requirements

- Python **3.11+** (3.12/3.13 fine)
- Network access to `site.api.espn.com`

## Setup (local)

```bash
cd football_picks
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

## How picks work

1. Load upcoming games with moneylines (NFL current scoreboard; NCAAF next ~9 days, FBS `groups=80`).
2. Rank candidate moneyline legs (favor dogs near +200, slight home bias, closer spreads).
3. Take up to 3 **singles** at **+200 or better** with zero team/game overlap.
4. If fewer than 3 remain, fill with best **2-leg** parlays that still clear +200 combined and stay independent.
5. Last resort: **3-leg** parlays under the same rules.

Refresh clears the ~3-minute cache and re-fetches ESPN.

## Deploy on Streamlit Community Cloud

1. Push this folder to a **public GitHub** repo (include `app.py` and `requirements.txt`).
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → select the repo/branch.
4. Set **Main file path** to `app.py`.
5. Deploy. No secrets required (public ESPN endpoints).

Optional: add a `.streamlit/config.toml` later for theme/server tweaks; do **not** commit `.streamlit/secrets.toml`.

## Limitations

- ESPN rate-limits or briefly omits odds; use **Refresh** after 30–60s if a league errors.
- Some networks block `site.api.espn.com` (HTTP 403). The app prefers `site.web.api.espn.com` and falls back across hosts.
- Lines are snapshots for education — always verify at your sportsbook before wagering.
- NCAAF coverage is FBS-focused (`groups=80`) over a short date window.
- Not a tip service, bankroll tool, or guarantee of profit.
- Parlay combined odds are decimal-multiplied approximations of American odds.

## Project layout

```
football_picks/
  app.py              # Streamlit app (v0.5)
  requirements.txt
  README.md
  .gitignore
```
