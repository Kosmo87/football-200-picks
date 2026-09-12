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

## Keys

Everything that needs a credential reads one file:

```bash
python setup_keys.py --init      # creates ~/.football-picks.env
# open it, fill in the blanks
python setup_keys.py             # checks each value, pushes them to GitHub
```

Fill in the blanks and nothing else. No exports, no editing commands, no
deciding which part of an example to replace — that last one has caused three
separate silent failures here, each surfacing hours later in a job nobody was
watching.

Every value is checked for shape before it goes anywhere: Resend keys begin
`re_`, Twilio SIDs begin `AC`, the Odds API key is 32 hex characters, and
anything resembling example text is rejected outright. Values are never printed,
only their length and first few characters, so a mistake is diagnosable without
the secret landing in a terminal log.

The file lives outside the repo at `~/.football-picks.env`, mode 600, and cannot
be committed. Scripts import `keys`, which loads it without overwriting anything
already in the environment — so CI keeps using its own secrets.

`python setup_keys.py --check` reports what is present and what is missing
without pushing or sending anything.

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

**Live:** https://statuesque-brioche-a8fa92.netlify.app

GitHub remains the source of truth. The hourly Action builds the board and then
**uploads the finished `public/` folder** with the Netlify CLI — Netlify never
clones the repo, so deploys cost no build minutes (a git-connected site would burn
~215–250 of the free tier's 300 at hourly cadence).

One-time setup, already done except the token:

```bash
# 1. Create a personal access token at
#    https://app.netlify.com/user/applications#personal-access-tokens
# 2. Store it as a repo secret:
gh secret set NETLIFY_AUTH_TOKEN --repo Kosmo87/football-200-picks
```

Without that secret the deploy step is skipped and the rest of the build still runs.
Deploying by hand from a checkout:

```bash
netlify deploy --prod --dir=public
```

## Scheduled refresh

`.github/workflows/build-board.yml` runs hourly (`17 * * * *`), on pushes that touch
the engine, and on manual dispatch. It builds the board, **verifies JS/Python
parity**, and commits `public/data/` and `cache/` only when something changed.

## Tip methodology

1. **Ratings** — Elo per league, chained across the **five prior seasons**, each one
   seeded from the last regressed toward the team's own baseline, then updated game by
   game with a **margin-of-victory multiplier** and 538's autocorrelation damper.
   FCS opponents start at **1000, not 1500** — treating them as an average FBS team was
   the single largest source of phantom edge on home underdogs. Preseason counts at 40%
   K; **neutral sites drop home-field**; all-star games are excluded.

   Parameters are **fitted, not chosen**: `tune_elo.py` runs coordinate descent on
   walk-forward log-loss over 2021–2025 (4,495 NCAAF / 1,425 NFL games). It picked
   K 36 / HFA 55 / carry 0.70 / FCS 1000 for college, and K 24 / **HFA 35** / carry
   0.55 for the NFL — that low NFL home-field number is a real result, matching the
   league's measured decline in home advantage. Re-run it after changing the window.
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

## Does it actually work? No — and here is the evidence

Backtested on NCAAF Sep 4–6 2026 with ratings built only from earlier games and
ESPN's recorded pregame DraftKings lines:

| | tips | all qualifying legs | ROI | model said | actual |
| --- | --- | --- | --- | --- | --- |
| before calibration work | 0–3 | 2–14 | −60.3% | 49.5% | 12.5% |
| after | 0–3 | 2–10 | −47.1% | 52.9% | 16.7% |

Walk-forward calibration over the season, model against the market's de-vigged
price on the same 73 games:

| Brier (lower is better) | before | after | market |
| --- | --- | --- | --- |
| NCAAF | 0.1820 | **0.1474** | **0.0999** |

The fitted model is a much better *predictor* — 19% better Brier, and the 80–90%
and 90–100% buckets are now calibrated to within 3pp. It is still not better than
the market, and the decisive test is this one:

```
model edge      n    model said    actual
 -100..0pp     43        72.3%     95.3%    <- where it saw no value
  +5..10pp      7        83.7%     57.1%
 +10..20pp      4        67.6%     25.0%    <- where it screamed value
```

**Claimed edge is inversely related to outcome.** Wherever the model disagrees
with the closing line, the market is right and the model is wrong. The +200 target
makes this worse rather than better, because clearing +200 forces the engine onto
underdogs — exactly the region where the model's residual error lives.

Treat the tips as a model under evaluation, not as advice. `calibrate.py` is the
scoreboard that matters: until model Brier beats the market's, no selection logic
layered on top can profit.

## Pricing a parlay you are considering

`parlay_price.py` answers "which leg should I change", and the answer is almost
never the one that feels weak.

A parlay's expectation reduces to a single product:

    EV + 1  =  product over legs of ( fair_prob / priced_prob )

That ratio is a leg's **keep rate** — the share of a unit that survives the
book's margin on that leg. Three things follow, and all three are
counter-intuitive:

**Expectation does not care how big a favourite a leg is.** A −3000 leg and a
−110 leg contribute identically if their keep rates match.

**The leg to drop is usually a heavy favourite.** Books take a wider cut on
lopsided lines because almost nobody shops them. On a real six-leg ticket
(Oregon, Texas A&M, Oklahoma, Penn State, Alabama, BYU at +216) the worst leg
was Oregon −2500 at 95.71% keep and the *best* was Oklahoma −200 at 96.43% —
6th best of 168 priced sides on the board. Swapping the leg that looked weakest
was the least useful change available: +0.09pp.

**Leg count sets the floor, so no swap rescues a long ticket.** Taking the best
keep rate available at each count:

| legs | combined | EV |
| --- | --- | --- |
| 1 | −165 | −3.5% |
| 2 | +152 | −6.8% |
| 3 | +581 | −10.2% |
| 6 | +6755 | −19.8% |

Meanwhile 22 of those 168 sides were **+EV as singles** at the best book of nine
— the top one at +13.3%. That is the edge a parlay structurally cannot have,
because a ticket sits at one book and takes that book's price on every leg.

## Cutting the noise

Two filters run before a tip is displayed, both mirrored in `picks.py` and
`public/app.js` and both covered by the parity test.

**A stake floor of 0.3U.** The stake distribution was measured before the floor
was chosen: across a full board every stake fell between 0.1U and 0.8U — nothing
reached 1U, because the stake is the ladder capped by Kelly and shrunk by
disagreement, and at +200-and-longer prices Kelly is small. A tip the engine
would put 0.2U on is the engine saying it barely believes it; showing that as
"#1 Tip" reads as conviction it does not have. The floor cut 9 of 10 staked NFL
sides.

**Stale games, both sides.** When a quarterback is ruled out after the ratings
were built, `build_board.mark_stale_legs` marks *both* legs. The obvious move is
to drop the injured team and keep the opponent, and it is wrong: the opponent's
number comes from the same rating and is wrong the same way. On Atlanta at
Pittsburgh the model read 45.1% on Atlanta against a market 30.4% ("back
Atlanta") and 54.9% on Pittsburgh against 69.6% ("Pittsburgh is overpriced").
One error, stated twice.

When nothing survives, the board says which gate emptied it. "No tips" and "no
tips worth backing" are different answers and only one means something broke.

### What the card shows, and what it used to show

A pick card carried five numbers where three were functions of the other two:
model win%, market win%, **edge** (the subtraction of those two), **Trust** (a
relabelling of that same subtraction), and a **0–100 signal score** of which 35
points were the edge again and 25 were a function of the price already displayed
alongside. Only the score's 40 sample points said anything new.

The Trust badge was also constant. The gate refuses anything more than ten
points from the price, so every pick that reaches the page is in the same band
and the badge read "Medium" every time.

Both are gone. The card now shows model win%, market win%, their difference, the
price, and **games of evidence** — the one input that was being blended into an
index whose movement could not be attributed to anything.

### Matchup model from play-by-play: tested, does not beat the close

Built and backtested rather than argued about. `pbp_data.py` reduces ten seasons
of nflverse play-by-play (329,347 plays, 2,639 games) to per-play EPA;
`matchup.py` solves `epa ~ offence(team) + defence(opponent) + home` by ridge
regression, separately for pass and run, so every offence is judged against the
defences it actually faced; `matchup_test.py` fits the margin model on 2017–2022
and evaluates on a held-out 2023–2025 against the **closing** spread.

**The carry-over hypothesis is confirmed.** Rosters and philosophies mostly
survive the off-season, and weighting prior-season plays in improves the
forecast monotonically — and makes week-1 games predictable at all:

| prior-season weight | model MAE | games predictable |
| --- | --- | --- |
| 0.0 | 11.102 | 807 |
| 0.25 | 10.921 | 855 |
| 0.5 | 10.883 | 855 |
| 1.0 | **10.875** | 855 |

**It still does not beat the market.** Closing-spread MAE is 9.786 against the
model's 10.875, and against the spread it runs 50.5%, worse as the disagreement
grows — the same inversion the Elo model shows. Nine configurations (three
feature sets × three ridge values) topped out at 50.5%; the bar at −110 is
52.4%.

The decisive test is whether the model knows *anything* the price does not.
Regressing the actual margin on both:

| term | coef | std err | t |
| --- | --- | --- | --- |
| closing spread | 0.969 | 0.052 | **18.72** |
| model prediction | 0.146 | 0.180 | **0.81** |

A market coefficient of ~1.0 with a ~0 intercept says the closing line is an
unbiased estimate of the margin. A model coefficient indistinguishable from zero
says it adds nothing on top. Out of sample the blend is very slightly *worse*
than the market alone (9.790 vs 9.786).

So the play-by-play detail is real, the matchup asymmetries are real, and the
market has already priced them. **None of this is wired into the board**, which
is the point of testing first.

### What the model knows about the teams: nothing### What the model knows about the teams: nothing

Elo is built from final scores and margins. It has no notion of scheme,
personnel, or which unit is weak — one number per team, updated by results.

The data to do better is free and unused. nflverse publishes NFL play-by-play at
~19 MB a season, 372 columns, including `shotgun`, `no_huddle`, `run_location`,
`pass_location`, `air_yards`, down and distance, and EPA split by pass and rush.
On tonight's top NFL pick (Tennessee −122 vs the Jets) it says the Jets have the
league's worst pass defence at +0.253 EPA allowed per play and Tennessee the
second worst at +0.167 — two defences failing in the same specific way, which
two similar Elo ratings cannot express.

Two honest caveats before anyone builds on it. In week 2 the current season's
file holds 234 plays, so any read is last season's; and `efficiency.py` already
does opponent-adjusted ratings from box scores, and `cfb_spread_test.py` tested
it over 1,800 games against Pinnacle without beating the closing line. Richer
inputs are not the same as an edge, and this project's standard is a backtest
against the close, not a plausible story.

### Why every bet is "Medium" trust, and never "High"

The trust label is the model's distance from the price. The edge gate is the
same distance. `min_edge_pp = 5.0` demands a gap of **at least** 5 points;
"High" trust means a gap of **at most** 5 points. For a single they are one
number read in opposite directions — verified on a live board, 62 of 62 +EV
sides — so a single can never be both *worth betting* and *close enough to the
market to believe*.

That is not a bug in the gates; it is the finding above restated. The engine
only ever bets where the model disagrees with the price, and disagreement is
precisely where the model has been measured wrong. Nothing in the selection
layer can resolve that, which is why the README says what it says: until model
Brier beats the market's, no selection logic layered on top can profit.

What selection *can* do is refuse the worst of it, which it now does.

### Distrust has to be a refusal, not a smaller stake

`staking.disagreement_factor` shrinks a stake as the model strays from the
market — the project's most important measured finding. Half-unit stakes broke
it. A 12pp disagreement sized to 0.30U and a 3pp one to 0.50U; rounded to half
units, **both become 0.5U**. The damper silently became a no-op for exactly the
bets it existed to punish, and a bet measured to be probably wrong got the same
stake as one we believed.

So `min_trust_factor` (default 0.6, the Medium band) refuses anything further
than 10 points from the price rather than merely sizing it down. If the stake
cannot carry the warning, the bet does not get made.

### No knobs

The board used to expose every gate as a slider. That made it a
build-your-own-bet tool: whatever it recommended was whatever you had just asked
it to recommend, and the track record described a strategy no visitor was
running. The gates came out of backtests — they are findings, not preferences —
so they are applied rather than offered, and the page shows one decided list per
posture.

Stakes come in **half units and nothing finer** (`staking.to_half_units`). This
is a decision about what a stake is, not a display choice: "half a unit" is an
instruction, "0.3U" is an optimisation result, and only one of them gets acted
on. It also stops false precision — the gap between 0.2U and 0.3U is well inside
the error on a win probability estimated from Elo. Rounding to nearest can raise
a stake, which is safe in this direction because the ladder is already capped at
quarter-Kelly, so even the largest upward round lands near three-eighths Kelly.
Under a quarter unit rounds to nothing and is not a bet.

### The certainty / payout trade

The page shows two decided lists, **Safest** and **Best priced** — the same
engine and gates read in the two directions a bet can be good, with no bet
appearing in both. Safest asks for a likely winner rather than a good price,
and applies its floor to the *ticket*, not to each leg — gating legs alone
produced a four-leg parlay of 55%-plus sides that was 21% to land, technically
"certain" leg by leg and a longshot as a bet.

It also drops the payout floor to −400, because a bet paying +200 is a
one-in-three shot by construction and asking for both is asking for nothing. The
difference is the whole point:

| list | bets | pays | model win% |
| --- | --- | --- | --- |
| Best priced (+200 target) | ISU +425, ASU +500 | big | 26–27% |
| Safest | ECU −265, WAKE −148 | small | 70–76% |

The market prices certainty. There is no setting that gives both.

## Which bets you actually placed

The board recommends; `placements.py` records. They are different questions and
the answers diverge the first time a flagged bet is skipped — after a month a
ledger of everything the engine surfaced describes a strategy nobody followed.

Every tip and every board row has a checkbox. Ticking it writes the bet
server-side through `netlify/functions/placements.mjs` (a Netlify blob store, so
a bet tagged on the phone is tagged on the laptop) with the price and stake at
that moment. The hourly build settles what has finished against final scores and
writes the result back, and **Your bets** on the page shows that record alone.

A bet is a list of legs, always, even when there is one, because most of what
this board recommends is a parlay and a parlay is not its legs added up. One
losing leg settles the ticket immediately; a pushed leg drops out and the rest
re-prices. `placements_test.py` pins all of it.

`PLACEMENT_KEY` on the Netlify site is the passphrase; without it the checkboxes
disable themselves and say so.

## What the ratings do not know

`context.py` attaches injuries and weather to every game. **Displayed, never
applied** — ESPN's injury feed is a snapshot of today, so there is no archive to
fit "what is a missing quarterback worth in Elo points" against, and the finding
above is what happens when a number gets invented instead of measured.

The case that justifies it: the board's top tip was once Atlanta ML +215 with a
claimed +14.7pp edge, on a morning when both Atlanta quarterbacks had been ruled
out. The market's 30.4% knew. A rating built from final scores cannot.

Two measurements shaped it. Injured Reserve is counted but never flagged — it
was 121 of 209 NFL absences, so including it fired on all 32 teams, and a player
out for weeks is one the ratings already absorbed. And injury *coverage* is
reported, because the same endpoint serves 28 of 28 NFL teams and 1 of 105
college teams: an unflagged college game means unknown, not healthy.

## Parlays

`parlay_math.py` has the general form: a parlay returns R^n where R is one leg's
expected return, so it amplifies an edge, amplifies a loss, and cannot turn the
second into the first.

`parlay_top25.py` runs it on the live board. On one representative week, all 147
independent 3-leg parlays of top-25 college teams were negative at *the market's
own de-vigged numbers* — best case −11.5%, which is just the per-leg margin
compounded. Mixing markets on one team does not help: "team wins" and "team goes
over" are positively correlated, which is exactly why no book sells the pair at
the product of its parts.

The decisive number is the forfeit. Line shopping is the one signal here that
has measured positive, and it needs the best price across nine books. A parlay
has to sit at one book: the three largest logged gaps pay 184.8x as singles at
their own best books and 147.3x as one parlay — **20% of the payout surrendered
to a bet shape that cannot use the only edge we have.**

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
