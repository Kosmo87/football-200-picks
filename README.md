# Football Tip Engine (+EV)

Local Streamlit **tip engine** that compares a simple **Elo model win probability** to
**market implied odds** and surfaces **+EV** moneyline sides for **NFL** and **NCAAF**.

It still aims for **exactly 3 independent picks per league** when enough +EV candidates
exist, targeting combined American odds of **+200 or better**. Prefers **singles**, then
independent **2-leg** / **3-leg** parlays. No overlapping teams or games (team IDs).

Odds and results come from free ESPN public scoreboard APIs (preferring the DraftKings
provider when present). **No paid APIs.**

**Educational prototype only — not betting advice. No guarantees of profit. Bet responsibly.**

## Version

**v0.6 tip-engine**

## Requirements

- Python **3.11+** (3.12/3.13 fine)
- Network access to ESPN (`site.web.api.espn.com`; falls back to `site.api.espn.com`)

## Setup (local)

```bash
cd football_picks
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Smoke test (no UI):

```bash
python smoke_test.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

## Tip methodology

1. **Completed games** — Fetch recent finals from ESPN:
   - NFL: preseason weeks (bootstrap early season) + regular-season weeks with finals
   - NCAAF FBS (`groups=80`): dated scoreboards over a ~45-day lookback
2. **Elo ratings** — Separate systems for NFL and NCAAF. Defaults roughly:
   - Start 1500; NFL K≈20, HFA≈55 Elo pts; NCAAF K≈24, HFA≈65
   - Games applied in chronological order
3. **Model win%** — Logistic Elo expectation from rating difference + home-field
4. **Implied win%** — From the priced side’s American odds; when both moneylines exist,
   probabilities are **de-vigged** (normalized to sum to 1)
5. **Edge** — `model_win_prob − implied_prob` (shown in percentage points). Tips require
   **edge > 0**
6. **Selection** — Rank +EV legs by edge. Take up to 3 **singles** at **+200+**, else fill
   with independent **2-leg** then **3-leg** parlays that still clear **+200** combined
   and keep zero team/game overlap

UI shows per leg: model win%, implied win%, edge (pp), and fair American odds.

## Limitations (read these)

- **Simple Elo is not a sharp model.** No injuries, weather, rest, line movement, or
  market consensus beyond a single ESPN provider snapshot.
- **Early season / thin history:** NFL may lean on **preseason** results until enough
  regular-season games finish — edges will be noisy.
- **NCAAF** lookback is short and FBS-only; FCS / early cupcakes can distort ratings.
- ESPN may **rate-limit**, omit odds, or return **403** on some hosts/IPs — the app
  retries across hosts; use **Refresh** after a short wait.
- De-vig and parlay combined odds are **approximations**. Always verify prices at your
  sportsbook.
- **+EV vs this model ≠ guaranteed profit.** Markets are efficient; variance is large;
  this is an educational demo, not a tip service or bankroll tool.

## Deploy on Streamlit Community Cloud

1. Push this folder to a **public GitHub** repo.
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → select the repo/branch.
4. Set **Main file path** to `app.py`.
5. Deploy. No secrets required.

## Project layout

```
football_picks/
  app.py           # Streamlit UI (v0.6 tip-engine)
  espn.py          # ESPN fetch + parse (upcoming / completed)
  elo.py           # Elo ratings + win probability
  odds.py          # American odds, implied, de-vig, fair odds
  picks.py         # +EV ranking and independent pick builder
  smoke_test.py    # CLI smoke test
  requirements.txt
  README.md
  .gitignore
```
