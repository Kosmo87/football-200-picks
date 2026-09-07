# Football Tip Engine (+EV)

Compares a **prior-season-seeded Elo model** to **market implied odds** and surfaces
**confidence-gated +EV** moneyline sides for **NFL** and **NCAAF**, aiming for up to
3 independent picks per league at **+200 or better**.

Ships as a **static site on Netlify**: a scheduled GitHub Action runs the Python
pipeline against ESPN's free public scoreboard API and commits a JSON board; the
browser handles pick selection so the confidence sliders stay live with no server.
**No paid APIs, no backend.**

**Educational prototype only — not betting advice. No guarantees of profit. Bet responsibly.**

## Version

**v0.8** — Netlify static build, prior-season Elo carryover, results/CLV tracking

## Architecture

```
GitHub Action (hourly cron)
  espn.py  ──▶ elo.py ──▶ odds.py ──▶ picks.py       [Python: fetch + rate + annotate]
                        │
                        ├─▶ public/data/board.json    every game, both sides annotated
                        └─▶ public/data/history.json  tracked-pick ledger (W/L, ROI, CLV)
                                    │
                                    ▼  commit → Netlify auto-deploy
Netlify static site (publish = public/)
  index.html + app.js                                 [JS: gates + selection, client-side]
```

Python owns fetching, rating, and de-vigging. Only the **selection layer** is
duplicated in JS, so a slider change re-picks instantly without a round trip.
`parity_test.js` / `parity_test.py` score the same `board.json` through both engines
and diff the result — CI fails if they ever disagree.

| File | Role |
| --- | --- |
| `espn.py` | Scoreboard fetch, odds parsing, neutral-site + season-type detection |
| `elo.py` | Ratings: MOV scaling, preseason weighting, prior-season carryover |
| `odds.py` | American odds maths, de-vig, fair prices, edge |
| `picks.py` | Confidence scoring, gates, non-overlapping 1–5 leg selection |
| `build_board.py` | Headless build → `board.json` + ledger update |
| `public/app.js` | Front end + JS port of the selection layer |
| `app.py` | Optional local Streamlit view of the same pipeline |

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python build_board.py          # writes public/data/*.json (first run pulls last season)
cd public && python3 -m http.server 8787
# open http://localhost:8787
```

Useful flags and checks:

```bash
python build_board.py --leagues NFL      # one league only
python build_board.py --refresh-prior    # re-pull last season, ignoring the cache
python smoke_test.py                     # live end-to-end check, no UI
node parity_test.js > /tmp/js.json && python parity_test.py > /tmp/py.json && diff /tmp/js.json /tmp/py.json
streamlit run app.py                     # optional local Streamlit view
```

The prior season is cached in `cache/prior_<LEAGUE>_<YEAR>.json` — it never changes,
so it is fetched once (~20 requests) and committed.

## Deploying to Netlify

Netlify cannot run Streamlit (it needs a persistent Python WebSocket server), which
is why the board is prebuilt. Netlify only serves files.

1. **New site** → import `Kosmo87/football-200-picks` from GitHub.
2. Netlify reads `netlify.toml`: publish directory `public`, **no build command**.
3. Deploy. Every push to `main` — including the hourly data commit — redeploys.

Optional: if you turn off auto-deploy on push, add a build hook and store it as the
`NETLIFY_BUILD_HOOK` repository secret; the workflow pings it after each build.

## Scheduled refresh

`.github/workflows/build-board.yml` runs hourly (`17 * * * *`), on pushes that touch
the engine, and on manual dispatch. It builds the board, **verifies JS/Python
parity**, and commits `public/data/` and `cache/` only when something changed.

## Tip methodology

1. **Ratings** — Elo per league, **seeded from last season regressed to the mean**
   (`new = 1500 + carry × (prior − 1500)`; carry 0.75 NFL / 0.72 NCAAF), then updated
   game by game with a **margin-of-victory multiplier** and 538's autocorrelation
   damper, so blowouts by heavy favorites don't run a rating away. Preseason counts at
   40% K; **neutral sites drop home-field**; all-star games are excluded.
2. **Model win%** — logistic Elo expectation from the rating gap plus home-field.
3. **Implied win%** — from the priced side, **de-vigged** against the opposite
   moneyline so both probabilities sum to 1.
4. **Edge** — `model_win_prob − implied_prob`, in percentage points.
5. **Confidence (0–100)** — `sample(40) + edge(35) + price shortness(25)`:
   - `sample = min(effective_sample / 8, 1) × 40`
   - `edge = min(max(edge_pp, 0) / 15, 1) × 35`
   - `short`: dogs `max(0, 1 − (odds−100)/500)`, favs `max(0, 1 − (|odds|−100)/200)`, × 25
   - Labels: High ≥ 70, Med ≥ 40, else Low.
6. **Effective sample** — current-season games plus capped prior-season credit
   (0.35 games each, max 4). This is what fixed NCAAF returning **zero** tips in
   September: real teams have real ratings in week 2, while genuine unknowns
   (FCS opponents, new programs) still get gated out.
7. **Selection** — gated legs only, no overlapping teams or games; prefer singles and
   short parlays that already clear +200, then stack 3–5 shorter legs. Ranked by
   **average confidence and edge**, not by the juiciest combined price.

## Results tracking & CLV

Every leg clearing the **baseline** gates (5.0pp edge, sample ≥ 3 NFL / 4 NCAAF,
≤ +600) is logged once to `public/data/history.json` at the price first seen. The
gates are fixed on purpose, so the track record stays comparable no matter what a
visitor sets their sliders to.

Each later run refreshes the **latest price** on still-open picks until kickoff, then
grades them against final scores. The site reports record, units and ROI at a flat
1u, plus **CLV** — positive means the logged price was longer than the closing price,
which is the honest early read on whether the model finds real edges, well before
win–loss says anything.

## Data source & caveats

ESPN's public scoreboard endpoints, preferring the DraftKings line when present.
Nothing here is authenticated or paid, so: lines can be stale, some games carry no
moneyline, and `site.api.espn.com` is often blocked from cloud IPs (the fetcher falls
back to `site.web.api.espn.com`). Early-season ratings are the least reliable even
with carryover — the sample gate exists for exactly that reason.
