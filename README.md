# Football Tip Engine (+EV)

Local Streamlit **tip engine** that compares a simple **Elo model win probability** to
**market implied odds** and surfaces **confidence-gated +EV** moneyline sides for
**NFL** and **NCAAF**.

It aims for **exactly 3 independent picks per league** when enough candidates exist,
targeting combined American odds of **+200 or better**. Prefers **fewer legs** when
they already clear +200 with high confidence; otherwise stacks **3 / 4 / 5-leg**
parlays from **shorter** +EV prices. No overlapping teams or games (team IDs).

Odds and results come from free ESPN public scoreboard APIs (preferring the DraftKings
provider when present). **No paid APIs.**

**Educational prototype only — not betting advice. No guarantees of profit. Bet responsibly.**

## Version

**v0.7 tip-engine** — confidence bands + up to 5-leg +200 parlays

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
   **edge > 0** plus confidence gates below
6. **Selection** — Filter legs by confidence gates, then take up to 3 **independent**
   tips: prefer singles / short parlays that clear **+200**, else fill with **3–5 leg**
   stacks of shorter +EV legs. Rank by **avg confidence** and **avg edge** (not juiciest
   combined odds)

### Confidence gates (sidebar defaults)

| Control | Default |
| --- | --- |
| Min edge (pp) | **5.0** |
| Min Elo sample games | **3** NFL / **4** NCAAF (or shared) |
| Max single-leg American odds | **+600** |
| Max parlay legs | **5** (1 = singles only) |
| Min combined odds | **+200** |
| High confidence mode | Off — raises min edge ≥ **7.5**, sample floors (NFL **5** / NCAAF **6**), caps legs at **+350**, keeps up to 5 legs |

**Sample** = minimum of completed games used in Elo for **both** teams in the matchup.

### Confidence score (0–100)

Shown on every tip / leg as a badge (**High** ≥ 70 · **Med** ≥ 40 · **Low** &lt; 40):

```
sample_score = min(sample_games / 8, 1) × 40
edge_score   = min(max(edge_pp, 0) / 15, 1) × 35
short_score  = shortness(odds) × 25
  dogs (+):  max(0, 1 − (odds − 100) / 500)   # +100 → 1.0, +600 → 0
  favs (−):  max(0, 1 − (|odds| − 100) / 200) # −100 → 1.0, −300 → 0
confidence   = sample_score + edge_score + short_score   # clamped 0–100
```

Shorter prices + deeper samples + larger edges score higher. Favorites as short as **−350** may be stacked into multi-leg +200 tips; long dogs are still capped by the max single-leg control. Multi-leg **+200** tips
exist so the engine can stack those shorter +EV legs under filters instead of one
noisy long dog.

UI shows per leg: confidence badge, model win%, implied win%, edge (pp), fair American
odds, and sample games.

## Limitations (read these)

- **Simple Elo is not a sharp model.** No injuries, weather, rest, line movement, or
  market consensus beyond a single ESPN provider snapshot.
- **Early season / thin history:** NFL may lean on **preseason** results until enough
  regular-season games finish — edges will be noisy; raise min sample / use High
  confidence mode.
- **NCAAF** lookback is short and FBS-only; FCS / early cupcakes can distort ratings.
- ESPN may **rate-limit**, omit odds, or return **403** on some hosts/IPs — the app
  retries across hosts; use **Refresh** after a short wait.
- De-vig and parlay combined odds are **approximations**. Always verify prices at your
  sportsbook.
- **+EV vs this model ≠ guaranteed profit.** Markets are efficient; variance is large;
  this is an educational demo, not a tip service or bankroll tool.
- Confidence is a **heuristic**, not a calibrated probability of winning or of true edge.

## Deploy on Streamlit Community Cloud

1. Push this folder to a **public GitHub** repo.
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → select the repo/branch.
4. Set **Main file path** to `app.py`.
5. Deploy. No secrets required.

## Project layout

```
football_picks/
  app.py           # Streamlit UI (v0.7 tip-engine)
  espn.py          # ESPN fetch + parse (upcoming / completed)
  elo.py           # Elo ratings + win probability
  odds.py          # American odds, implied, de-vig, fair odds
  picks.py         # Confidence gates + 1–5 leg pick builder
  smoke_test.py    # CLI smoke test (filters + multi-leg paths)
  requirements.txt
  README.md
  .gitignore
```
