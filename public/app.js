/*
 * Football +200 Picks — static front end.
 *
 * The Python build (build_board.py) ships every upcoming game with both sides
 * already annotated (model prob, de-vigged implied prob, edge, sample). The
 * selection layer below is a direct port of picks.py so the confidence gates
 * stay interactive with no server: change a slider, tips rebuild in the browser.
 */

// --------------------------------------------------------------------------
// odds.py port
// --------------------------------------------------------------------------

const americanToDecimal = (o) => (o > 0 ? 1 + o / 100 : 1 + 100 / Math.abs(o));

function decimalToAmerican(dec) {
  if (dec <= 1) return -10000;
  if (dec >= 2) return Math.round((dec - 1) * 100);
  return Math.round(-100 / (dec - 1));
}

function combineOdds(oddsList) {
  return decimalToAmerican(oddsList.reduce((d, o) => d * americanToDecimal(o), 1));
}

const fmtOdds = (o) => (o > 0 ? `+${o}` : `${o}`);
const fmtPct = (p) => `${(p * 100).toFixed(1)}%`;
const fmtPP = (pp) => `${pp >= 0 ? "+" : ""}${pp.toFixed(1)}pp`;

// --------------------------------------------------------------------------
// staking.py port — units
//
// The ladder sizes a bet by how likely it is to land, which is the capper
// convention and beat flat staking in both seasons we can measure. Capped by
// Kelly because win probability alone does not say whether a bet is good: a
// 75% favourite at -300 is priced fairly and deserves nothing, while the raw
// ladder would stake 2U on it.
// --------------------------------------------------------------------------

const LADDER = [[0.60, 0.5], [0.70, 1.0], [0.80, 2.0], [0.90, 3.0], [0.95, 4.0], [1.01, 5.0]];
const MAX_UNITS = 5.0;
const KELLY_FRACTION = 0.25;

// Published stakes come in half units and nothing finer. Mirrors
// staking.UNIT_STEP / to_half_units — a decision about what a stake IS, not a
// display choice: "half a unit" is an instruction, 0.3U is an optimisation
// result, and only one of them gets acted on. Rounding half away from zero to
// match Python's floor(x/step + 0.5).
const UNIT_STEP = 0.5;
const toHalfUnits = (raw) =>
  raw < UNIT_STEP / 2 ? 0 : Math.floor(raw / UNIT_STEP + 0.5) * UNIT_STEP;

function ladderUnits(p) {
  for (const [ceiling, units] of LADDER) if (p < ceiling) return units;
  return MAX_UNITS;
}

function kellyUnits(p, odds, fraction = KELLY_FRACTION) {
  const b = americanToDecimal(odds) - 1;
  if (b <= 0) return 0;
  const f = (b * p - (1 - p)) / b;
  if (f <= 0) return 0;
  return Math.min(Math.round(f * fraction * 20 * 10) / 10, MAX_UNITS);
}

// How far the model may disagree with the price before the disagreement counts
// as its own error. Backtesting sorted picks by how far they strayed from the
// market and the relationship ran backwards: at 10+ points of claimed edge it
// won 25-38% against the 57-67% it forecast. So a wide gap shrinks the stake
// rather than growing it.
const DISAGREEMENT_BANDS = [[5, 1.0], [10, 0.6], [15, 0.3]];

function disagreementFactor(p, marketP) {
  if (marketP == null) return 1;
  const gap = Math.abs(p - marketP) * 100;
  for (const [limit, factor] of DISAGREEMENT_BANDS) if (gap <= limit) return factor;
  return 0;
}

function trustLabel(p, marketP) {
  const f = disagreementFactor(p, marketP);
  return f >= 1 ? "High" : f >= 0.6 ? "Medium" : f > 0 ? "Low" : "None";
}

/** Published stake: the ladder, capped by the price, shrunk by disagreement,
 *  then snapped to a half unit. */
const stakeUnits = (p, odds, marketP) =>
  toHalfUnits(
    Math.round(
      Math.min(ladderUnits(p), kellyUnits(p, odds)) * disagreementFactor(p, marketP) * 10
    ) / 10
  );

/** A parlay lands only if every leg does. Independence is why legs never share a game. */
const parlayWinProb = (probs) => probs.reduce((a, b) => a * b, 1);

const unitsLabel = (u) => (u <= 0 ? "no bet" : `${+u.toFixed(1)}U`);
/** Mirrors staking.stake_words: the stake as an instruction, not a number. */
const stakeWords = (u) =>
  u <= 0 ? "no bet" : u === 0.5 ? "half a unit" : u === 1 ? "one unit" : `${+u.toFixed(1)} units`;

// --------------------------------------------------------------------------
// picks.py port — confidence, gates, selection
// --------------------------------------------------------------------------

const DEFAULTS = {
  minEdgePP: 5.0,
  minSample: 3,
  maxLegOdds: 600,
  maxParlayLegs: 5,
  minCombined: 200,
  minLegOdds: -350,
  // Half a unit is the smallest stake that exists now, so it is also the floor.
  // Anything that sized under a quarter unit rounds to nothing and is not a bet
  // — which is the engine saying it does not believe it, and the right answer
  // rather than a failure. Mirrors MIN_PLAYABLE_UNITS.
  minStake: 0.5,
  // "Likely to win" rather than "well priced" — a different gate, and one that
  // points the other way, since claimed edge measured inversely related to
  // outcome. Off by default: a bet paying +200 is a one-in-three shot by
  // construction, so certainty and payout cannot both be maximised.
  minWinProb: 0,
  // The same question asked of the TICKET, which is the one that matters. Four
  // legs each cleared at 55% is a 9% parlay: gating legs and then multiplying
  // them produces exactly the bet the gate meant to exclude.
  minPickWinProb: 0,
  // Drop both sides of a game the ratings cannot speak to.
  excludeStale: true,
};

// Baseline min-sample differs by league (college needs a touch more).
const LEAGUE_MIN_SAMPLE = { NFL: 3, NCAAF: 4 };

function confidenceLabel(score) {
  if (score >= 70) return "High";
  if (score >= 40) return "Med";
  return "Low";
}

function computeConfidence(edgePP, odds, sample) {
  const sampleScore = Math.min(Math.max(sample, 0) / 8, 1) * 40;
  const edgeScore = Math.min(Math.max(edgePP, 0) / 15, 1) * 35;
  const shortFrac =
    odds >= 0
      ? Math.max(0, 1 - (odds - 100) / 500)
      : Math.max(0, 1 - (Math.abs(odds) - 100) / 200);
  const score = Math.max(0, Math.min(100, sampleScore + edgeScore + shortFrac * 25));
  return Math.round(score * 10) / 10;
}

/** Flatten board games into legs, attaching game context and confidence. */
function flattenLegs(league) {
  const out = [];
  for (const game of league.games) {
    for (const leg of game.legs) {
      const opp = leg.side === "home" ? game.away : game.home;
      out.push({
        ...leg,
        eventId: game.event_id,
        oppId: opp.id,
        oppAbbr: opp.abbr,
        matchup: game.short_name || game.name,
        kickoff: game.kickoff,
        neutral: game.neutral,
        confidence: computeConfidence(leg.edge_pp, leg.odds, leg.sample),
      });
    }
  }
  return out;
}

function passesGates(leg, cfg) {
  // Order mirrors passes_confidence_gates in picks.py. The staleness check is
  // first because it is a statement about the input, not about the price.
  if (cfg.excludeStale && leg.stale) return false;
  if (leg.model_prob < cfg.minWinProb) return false;
  if (leg.edge_pp < cfg.minEdgePP) return false;
  if (leg.sample < cfg.minSample) return false;
  if (leg.odds > cfg.maxLegOdds) return false;
  if (leg.odds < cfg.minLegOdds) return false;
  return leg.edge_pp > 0;
}

function legsOverlap(legs) {
  const teams = new Set();
  const games = new Set();
  for (const l of legs) {
    if (teams.has(l.team_id) || games.has(l.eventId)) return true;
    teams.add(l.team_id);
    teams.add(l.oppId);
    games.add(l.eventId);
  }
  return false;
}

const avgBy = (legs, key) =>
  legs.length ? legs.reduce((s, l) => s + l[key], 0) / legs.length : 0;

function rankScore(legs, combined) {
  const avgC = avgBy(legs, "confidence");
  const avgE = avgBy(legs, "edge_pp");
  const fewerBonus = (6 - legs.length) * 0.5;
  const oddsPenalty = Math.max(0, combined - 500) / 5000;
  return avgC + avgE + fewerBonus - oddsPenalty;
}

/** Enumerate non-overlapping n-leg combos clearing minCombined. */
function candidateCombos(available, nLegs, minCombined, poolCap) {
  if (nLegs < 1 || available.length < nLegs) return [];
  const pool = [...available]
    .sort((a, b) => b.confidence - a.confidence || b.edge_pp - a.edge_pp)
    .slice(0, poolCap);

  const out = [];
  const combo = [];
  (function walk(start) {
    if (combo.length === nLegs) {
      if (legsOverlap(combo)) return;
      const combined = combineOdds(combo.map((l) => l.odds));
      if (combined >= minCombined) {
        out.push({ score: rankScore(combo, combined), legs: [...combo], combined });
      }
      return;
    }
    for (let i = start; i < pool.length; i++) {
      combo.push(pool[i]);
      walk(i + 1);
      combo.pop();
    }
  })(0);

  out.sort((a, b) => b.score - a.score);
  return out;
}

function makePick(legs, combined) {
  const avgC = avgBy(legs, "confidence");
  const avgE = avgBy(legs, "edge_pp");
  const n = legs.length;
  const winProb = parlayWinProb(legs.map((l) => l.model_prob));
  const marketProb = parlayWinProb(legs.map((l) => l.implied_prob));
  const units = stakeUnits(winProb, combined, marketProb);
  let label =
    n === 1
      ? `Single: ${legs[0].team_abbr} ML ${fmtOdds(legs[0].odds)}`
      : `${n}-leg: ${legs.map((l) => `${l.team_abbr} (${fmtOdds(l.odds)})`).join(" + ")}`;
  if (units > 0) label = `${unitsLabel(units)} · ${label}`;
  return {
    winProb,
    marketProb,
    stakeUnits: units,
    trust: trustLabel(winProb, marketProb),
    legs,
    combined,
    label,
    avgConfidence: avgC,
    confidenceLabel: confidenceLabel(avgC),
    combinedEdgePP: n === 1 ? legs[0].edge_pp : avgE,
    payout: americanToDecimal(combined) - 1,
  };
}

const POOL_CAPS = { 1: 80, 2: 50, 3: 40, 4: 28, 5: 22 };

/** Port of picks.build_picks: up to n independent, gated tips. */
function buildPicks(allLegs, cfg, n = 3) {
  const maxLegs = Math.max(1, Math.min(5, cfg.maxParlayLegs));
  const gated = allLegs
    .filter((l) => passesGates(l, cfg))
    .sort((a, b) => b.confidence - a.confidence || b.edge_pp - a.edge_pp);
  if (!gated.length) return [];

  const picks = [];
  const usedTeams = new Set();
  const usedGames = new Set();
  const canUse = (l) =>
    !usedTeams.has(l.team_id) && !usedTeams.has(l.oppId) && !usedGames.has(l.eventId);
  const markUsed = (l) => {
    usedTeams.add(l.team_id);
    usedTeams.add(l.oppId);
    usedGames.add(l.eventId);
  };

  for (let nLegs = 1; nLegs <= maxLegs && picks.length < n; nLegs++) {
    const available = gated.filter(canUse);
    if (available.length < nLegs) continue;

    if (nLegs === 1) {
      // Singles long enough to clear +200 on their own
      for (const leg of available.filter((l) => l.odds >= cfg.minCombined)) {
        if (picks.length >= n) break;
        if (!canUse(leg)) continue;
        const pick = makePick([leg], leg.odds);
        // A pick we would barely stake is not a pick. Leave its teams free so
        // they can still appear in a combination.
        if (pick.stakeUnits < cfg.minStake) continue;
        if (pick.winProb < cfg.minPickWinProb) continue;
        picks.push(pick);
        markUsed(leg);
      }
      continue;
    }

    for (const cand of candidateCombos(available, nLegs, cfg.minCombined, POOL_CAPS[nLegs] || 25)) {
      if (picks.length >= n) break;
      if (cand.legs.some((l) => !canUse(l))) continue;
      const pick = makePick(cand.legs, cand.combined);
      if (pick.stakeUnits < cfg.minStake) continue;
      if (pick.winProb < cfg.minPickWinProb) continue;
      picks.push(pick);
      cand.legs.forEach(markUsed);
    }
  }

  picks.sort((a, b) => b.avgConfidence - a.avgConfidence || b.combinedEdgePP - a.combinedEdgePP);
  return picks.slice(0, n);
}

/**
 * Mirrors ConfidenceConfig.apply_high_certainty_preset.
 *
 * Pure, and defined in the engine half on purpose: the parity harness loads
 * everything above the State section, and a preset the harness cannot reach is
 * a preset nothing checks. Both engines must agree here too.
 */
function applyCertaintyTo(cfg) {
  return {
    ...cfg,
    minWinProb: Math.max(cfg.minWinProb, 0.55),
    minPickWinProb: Math.max(cfg.minPickWinProb, 0.5),
    minCombined: Math.min(cfg.minCombined, -400),
    minStake: Math.max(cfg.minStake, DEFAULTS.minStake),
  };
}

// --------------------------------------------------------------------------
// Placements — which of these did you actually bet
//
// The board recommends; this records. They are different questions and the
// answers diverge the first time a flagged bet is skipped, so nothing here
// infers a placement from a recommendation: a bet is placed when the box is
// ticked and not otherwise.
//
// State lives server-side (netlify/functions/placements.mjs) rather than in
// localStorage, so a bet tagged on the phone is tagged on the laptop, and so
// the hourly build can settle it against the final score. The passphrase is
// the one thing kept locally — it is a write credential, not data.
// --------------------------------------------------------------------------

const PLACEMENTS_API = "/api/placements";
const KEY_STORAGE = "fb200.placementKey";

const loadKey = () => {
  try { return localStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
};
const saveKey = (k) => {
  try { k ? localStorage.setItem(KEY_STORAGE, k) : localStorage.removeItem(KEY_STORAGE); }
  catch { /* private browsing; the key just will not persist */ }
};

/** Must match betId() in the function exactly, or the page and the store
 *  disagree about whether a bet is already tagged. */
const cleanIdPart = (v) => String(v ?? "").replace(/[^A-Za-z0-9_.-]/g, "");
function betId(league, legs) {
  const parts = legs
    .map((l) => `${cleanIdPart(l.event_id)}.${cleanIdPart(l.side)}`)
    .sort();
  return `${cleanIdPart(league)}-${parts.join("+")}`;
}

/** A tip or a board row, in the shape the store keeps. */
function betFromLegs(league, legs, combinedOdds, winProb, marketProb, edgePP) {
  return {
    league,
    odds: combinedOdds,
    model_prob: round5(winProb),
    implied_prob: round5(marketProb),
    edge_pp: Math.round(edgePP * 100) / 100,
    legs: legs.map((l) => ({
      event_id: l.eventId, side: l.side, team_id: l.team_id,
      team_abbr: l.team_abbr, team_name: l.team_name, opp_abbr: l.oppAbbr,
      matchup: l.matchup, kickoff: l.kickoff, odds: l.odds,
      model_prob: round5(l.model_prob), implied_prob: round5(l.implied_prob),
      edge_pp: l.edge_pp,
    })),
  };
}
const round5 = (n) => (n == null ? null : Math.round(n * 1e5) / 1e5);

async function fetchPlacements() {
  try {
    const r = await fetch(`${PLACEMENTS_API}?t=${Date.now()}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    state.placementsConfigured = j.configured !== false;
    state.placements = new Map((j.placements || []).map((p) => [p.id, p]));
    state.placementsError = j.error || null;
  } catch (e) {
    // A static-only deploy has no function. Say so once rather than failing
    // every checkbox click silently.
    state.placementsConfigured = false;
    state.placementsError = e.message;
    state.placements = new Map();
  }
}

/** Upsert or untag. Returns an error string, or null on success. */
async function savePlacement(bet, stake) {
  const key = state.placementKey || loadKey();
  if (!key) return "need-key";
  let r;
  try {
    r = await fetch(PLACEMENTS_API, {
      method: "POST",
      headers: { "content-type": "application/json", "x-placement-key": key },
      body: JSON.stringify({ ...bet, stake }),
    });
  } catch (e) {
    return e.message;
  }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) {
    saveKey("");
    state.placementKey = "";
    return "wrong passphrase — enter it again";
  }
  if (!r.ok) return j.error || `HTTP ${r.status}`;

  const id = betId(bet.league, bet.legs);
  if (j.removed) state.placements.delete(id);
  else if (j.placement) state.placements.set(j.placement.id, j.placement);
  return null;
}

function promptForKey() {
  const k = window.prompt(
    "Passphrase for recording bets\n\n"
    + "This is the PLACEMENT_KEY set on the Netlify site. It is stored in this "
    + "browser so you only type it once."
  );
  if (k) {
    state.placementKey = k.trim();
    saveKey(state.placementKey);
  }
  return Boolean(k);
}

/**
 * The checkbox, plus the stake it was placed at.
 *
 * Stake defaults to what the engine recommends, because the common case is
 * taking the advice, and it stays editable because the common case is not the
 * only one. Changing it on an already-tagged bet rewrites the stake rather than
 * logging a second bet — the id is derived from the legs, not the amount.
 */
function placementControls(bet, suggestedStake) {
  const id = betId(bet.league, bet.legs);
  const existing = state.placements.get(id);
  const wrap = el("div", "place");

  const box = el("input");
  box.type = "checkbox";
  box.checked = Boolean(existing);
  box.id = `pl-${id}`;

  const label = el("label", "place-label");
  label.htmlFor = box.id;
  label.textContent = existing ? "Placed" : "Tag as placed";
  // The board hides the label to save a column, so the box carries the whole
  // description itself for anyone reading it without one.
  box.title = `${existing ? "Placed" : "Tag as placed"}: `
    + (bet.legs.length === 1
        ? `${bet.legs[0].team_abbr} ML ${fmtOdds(bet.odds)}`
        : `${bet.legs.length}-leg ${fmtOdds(bet.odds)}`);
  box.setAttribute("aria-label", box.title);

  const stake = el("input", "place-stake");
  stake.type = "number";
  stake.min = "0";
  stake.step = "0.25";
  stake.value = existing ? Number(existing.stake).toFixed(2)
                         : Number(suggestedStake || 1).toFixed(2);
  stake.title = "Units staked";
  stake.hidden = !existing;

  const note = el("span", "place-note");
  const settled = existing && existing.status && existing.status !== "open";
  if (settled) {
    box.disabled = true;
    stake.disabled = true;
    const u = Number(existing.units || 0);
    note.className = `place-note ${u > 0 ? "pos" : u < 0 ? "neg" : "dim"}`;
    note.textContent = `${existing.status.toUpperCase()} ${u >= 0 ? "+" : ""}${u.toFixed(2)}u`;
  }

  async function commit(checked, units) {
    box.disabled = true;
    note.className = "place-note dim";
    note.textContent = "saving…";
    if (checked && !state.placementKey && !loadKey()) {
      if (!promptForKey()) {
        box.checked = false;
        box.disabled = false;
        note.textContent = "";
        return;
      }
    }
    const err = await savePlacement(bet, checked ? units : 0);
    box.disabled = false;
    if (err) {
      box.checked = Boolean(state.placements.get(id));
      note.className = "place-note neg";
      note.textContent = err === "need-key" ? "passphrase required" : err;
      return;
    }
    note.textContent = "";
    render();
    renderYourBets();
  }

  box.addEventListener("change", () => commit(box.checked, parseFloat(stake.value) || 1));
  stake.addEventListener("change", () => {
    if (box.checked) commit(true, parseFloat(stake.value) || 1);
  });

  wrap.append(box, label);
  wrap.appendChild(stake);
  wrap.appendChild(note);
  return wrap;
}

/** The panel that answers "how am I doing", which is not what the board says. */
function renderYourBets() {
  const section = $("#yourbets");
  const body = $("#yourbets-body");
  const stats = $("#yourbets-stats");
  body.innerHTML = "";
  stats.innerHTML = "";

  const rows = [...state.placements.values()];
  if (!rows.length) {
    section.hidden = state.placementsConfigured !== false;
    if (!state.placementsConfigured) {
      section.hidden = false;
      body.innerHTML = `<p class="empty-inline">Recording bets is not switched on for
        this site${state.placementsError ? ` (${state.placementsError})` : ""}. Set a
        <code>PLACEMENT_KEY</code> environment variable in Netlify and redeploy.</p>`;
    }
    return;
  }
  section.hidden = false;

  const settled = rows.filter((r) => r.status && r.status !== "open");
  const won = settled.filter((r) => r.status === "won");
  const lost = settled.filter((r) => r.status === "lost");
  const risked = [...won, ...lost].reduce((s, r) => s + Number(r.stake || 0), 0);
  const units = settled.reduce((s, r) => s + Number(r.units || 0), 0);
  const open = rows.filter((r) => !r.status || r.status === "open");
  const atRisk = open.reduce((s, r) => s + Number(r.stake || 0), 0);

  const cards = [
    { label: "Tagged", value: `${rows.length}`, sub: `${open.length} still running`, tone: "" },
    { label: "Record", value: settled.length ? `${won.length}–${lost.length}` : "—",
      sub: settled.length ? `${settled.length} settled` : "nothing settled yet", tone: "" },
    { label: "Units", value: settled.length ? `${units >= 0 ? "+" : ""}${units.toFixed(2)}u` : "—",
      sub: risked ? `${risked.toFixed(2)}u risked` : "—",
      tone: units > 0 ? "pos" : units < 0 ? "neg" : "" },
    { label: "Return", value: risked ? `${units / risked >= 0 ? "+" : ""}${(units / risked * 100).toFixed(1)}%` : "—",
      sub: "on settled bets", tone: units > 0 ? "pos" : units < 0 ? "neg" : "" },
    { label: "At risk", value: `${atRisk.toFixed(2)}u`, sub: "open tickets", tone: "" },
  ];
  for (const c of cards) {
    const card = el("div", "stat");
    card.appendChild(el("div", "stat-label", c.label));
    card.appendChild(el("div", `stat-value ${c.tone}`, c.value));
    card.appendChild(el("div", "stat-sub", c.sub));
    stats.appendChild(card);
  }

  rows.sort((a, b) => String(b.kickoff || "").localeCompare(String(a.kickoff || "")));
  for (const r of rows) {
    const legs = r.legs || [];
    const label = legs.length === 1
      ? `${legs[0].team_abbr} ML`
      : `${legs.length}-leg: ${legs.map((l) => l.team_abbr).join(" + ")}`;
    const u = r.units == null ? null : Number(r.units);
    const tr = el("tr");
    tr.innerHTML = `
      <td class="team-cell">${label}
        <div class="lg-game">${legs.map((l) => l.matchup).join(" · ")}</div></td>
      <td class="num">${fmtOdds(r.odds)}</td>
      <td class="num">${Number(r.stake || 0).toFixed(2)}u</td>
      <td class="num ${u > 0 ? "pos" : u < 0 ? "neg" : "dim"}">${
        u == null ? "—" : `${u >= 0 ? "+" : ""}${u.toFixed(2)}u`}</td>
      <td class="${r.status === "won" ? "pos" : r.status === "lost" ? "neg" : "dim"}">
        ${(r.status || "open").toUpperCase()}
        <div class="lg-game">${(r.leg_results || [])
          .map((lr) => `${{ won: "✓", lost: "✗", push: "=" }[lr.outcome] || "·"} ${lr.team_abbr}`)
          .join(" ")}</div></td>`;
    body.appendChild(tr);
  }
}

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------

const state = {
  board: null,
  history: null,
  league: null,
  // One configuration, fixed. The board used to expose every gate as a slider,
  // which made it a build-your-own-bet tool: whatever it recommended was
  // whatever you had just asked it to recommend, and the track record below
  // described a strategy no visitor was actually running. The gates are a
  // finding, not a preference — they came out of backtests — so they are
  // applied rather than offered.
  cfg: { ...DEFAULTS },
  onlyEV: true,
  // Bets tagged as placed, by id, from /api/placements.
  placements: new Map(),
  placementKey: loadKey(),
  placementsConfigured: true,
  placementsError: null,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};

function kickoffLabel(iso) {
  if (!iso) return "TBD";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

function confBadge(score) {
  const label = confidenceLabel(score);
  return (
    `<span class="badge ${label.toLowerCase()}" title="Signal score, not a win ` +
    `probability. Combines sample depth (40), claimed edge (35) and how short ` +
    `the price is (25).">${label} ${Math.round(score)}/100</span>`
  );
}

/** How much to believe our own number, from how far it sits from the price. */
function trustBadge(trust) {
  const cls = { High: "high", Medium: "med", Low: "low", None: "low" }[trust];
  const tip = {
    High: "Our number is within 5 points of the market — worth acting on.",
    Medium: "We are 5-10 points from the market. Reduced stake.",
    Low: "We are 10-15 points from the market. Heavily reduced stake.",
    None: "We are more than 15 points from the market, which historically means "
        + "we are the ones who are wrong. No stake.",
  }[trust];
  return `<span class="badge ${cls}" title="${tip}">Trust: ${trust}</span>`;
}

// --------------------------------------------------------------------------
// Context — what the ratings do not know
//
// Injuries and weather are attached to each game by context.py and are shown
// here, never applied. There is no historical injury or weather archive to fit
// a coefficient against, so a number would be invented; what IS already
// established, by backtest, is that a large claimed edge usually means the
// model is the one that is wrong. A quarterback ruled out is a mechanism for
// exactly that, so it earns a warning next to the edge rather than a silent
// adjustment to it.
// --------------------------------------------------------------------------

/** Flags for a game, highest severity first. */
function gameFlags(game) {
  const flags = (game.context && game.context.flags) || [];
  return [...flags].sort((a, b) => (a.severity === "high" ? -1 : 1) - (b.severity === "high" ? -1 : 1));
}

/** Flags that bear on one side specifically, plus the game-wide ones. */
function legFlags(leg, game) {
  return gameFlags(game).filter((f) => !f.side || f.side === leg.side);
}

function weatherLine(game) {
  const c = game.context || {};
  const w = c.weather;
  if (!w) {
    return c.venue && c.venue.indoor ? `Indoors · ${c.venue.venue}` : null;
  }
  const bits = [`${Math.round(w.temp_f)}°F`];
  bits.push(`wind ${Math.round(w.wind_mph)}${
    w.gust_mph > w.wind_mph + 3 ? ` (gusts ${Math.round(w.gust_mph)})` : ""} mph`);
  if (w.precip_pct >= 20) bits.push(`${Math.round(w.precip_pct)}% rain`);
  if (w.snow_in > 0) bits.push("snow");
  return bits.join(" · ") + ((c.venue && c.venue.venue) ? ` · ${c.venue.venue}` : "");
}

/** The strip under a tip: every warning that applies to its legs. */
function flagStrip(legs, gamesById) {
  const seen = new Set();
  const out = [];
  for (const leg of legs) {
    const game = gamesById.get(leg.eventId);
    if (!game) continue;
    for (const f of legFlags(leg, game)) {
      const k = `${f.kind}|${f.text}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(f);
    }
  }
  if (!out.length) return null;
  const box = el("div", "flags");
  for (const f of out) {
    box.appendChild(el("div", `flag ${f.severity}`,
      `<span class="flag-mark">${f.severity === "high" ? "!" : "i"}</span>${f.text}`));
  }
  return box;
}

/** A compact marker for the full board, where there is no room for sentences. */
function flagBadge(game) {
  const flags = gameFlags(game);
  if (!flags.length) return "";
  const high = flags.filter((f) => f.severity === "high");
  const tip = flags.map((f) => f.text).join(" \n");
  return ` <span class="flag-dot ${high.length ? "high" : "low"}" title="${
    tip.replace(/"/g, "&quot;")}">${high.length ? "!" : "i"}</span>`;
}

/** Said once, at the top of the board, rather than implied by empty badges. */
function injuryCoverageNote() {
  const lg = state.board.leagues[state.league];
  const meta = (lg.games.find((g) => g.context) || {}).context;
  const inj = meta && meta.injuries;
  if (!inj) return "";
  if (inj.source === "espn") {
    return `injury data for ${inj.teams_covered} of ${inj.teams_playing} teams playing`;
  }
  if (inj.source === "sparse") {
    return `no usable injury data — ESPN lists ${inj.teams_covered} of `
         + `${inj.teams_playing} teams playing, so an unflagged game here means `
         + `unknown, not healthy`;
  }
  if (inj.source === "unavailable") return "injury feed unavailable this build";
  return "no injuries reported";
}

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

function renderRecord() {
  const s = state.history && state.history.summary;
  if (!s || (!s.won && !s.lost && !s.open)) return;
  const box = $("#record-stats");
  box.innerHTML = "";

  const settled = s.won + s.lost;
  const stats = [
    { label: "Record", value: `${s.won}–${s.lost}`,
      sub: s.pushed ? `${s.pushed} push` : `${settled} settled`, tone: "" },
    { label: "Units", value: `${s.units >= 0 ? "+" : ""}${s.units.toFixed(2)}u`,
      sub: "flat 1u per pick", tone: s.units > 0 ? "pos" : s.units < 0 ? "neg" : "" },
    { label: "ROI", value: `${s.roi_pct >= 0 ? "+" : ""}${s.roi_pct.toFixed(1)}%`,
      sub: `${s.win_pct.toFixed(0)}% win rate`, tone: s.roi_pct > 0 ? "pos" : s.roi_pct < 0 ? "neg" : "" },
    { label: "Avg CLV", value: fmtPP(s.avg_clv_pp),
      sub: `beat close ${s.beat_close_pct.toFixed(0)}%`, tone: s.avg_clv_pp > 0 ? "pos" : s.avg_clv_pp < 0 ? "neg" : "" },
    { label: "Open", value: `${s.open}`, sub: "awaiting result", tone: "" },
  ];

  for (const st of stats) {
    const card = el("div", "stat");
    card.appendChild(el("div", "stat-label", st.label));
    card.appendChild(el("div", `stat-value ${st.tone}`, st.value));
    card.appendChild(el("div", "stat-sub", st.sub));
    box.appendChild(card);
  }
  $("#record").hidden = false;
}

function renderTabs() {
  const nav = $("#league-tabs");
  nav.innerHTML = "";
  for (const [name, lg] of Object.entries(state.board.leagues)) {
    const b = el("button", "tab",
      `${name} <span class="cnt">${lg.meta.upcoming_games}</span>`);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(name === state.league));
    b.onclick = () => {
      state.league = name;
      applyLeagueConfig();
      render();
    };
    nav.appendChild(b);
  }
}

/** What was examined to arrive at the list above. Not a set of controls. */
function renderGateSummary() {
  const lg = state.board.leagues[state.league];
  const legs = flattenLegs(lg);
  const gated = legs.filter((l) => passesGates(l, state.cfg));
  const plusEV = legs.filter((l) => l.edge_pp > 0);
  const stale = legs.filter((l) => l.stale).length;
  const m = lg.meta;
  $("#gate-summary").textContent =
    `Looked at ${m.upcoming_games} games · ${legs.length} priced sides · ` +
    `${plusEV.length} +EV · ${gated.length} cleared every gate` +
    (stale ? ` · ${stale} dropped for a late quarterback ruling` : "") +
    ` · ${m.rated_teams} teams rated from ${m.completed_games} finals` +
    (m.carryover ? ` + ${m.prior_games_used} prior-season games` : " (no carryover)") +
    (injuryCoverageNote() ? ` · ${injuryCoverageNote()}` : "");
}

/**
 * Two decided lists, not one list and a pile of sliders.
 *
 * "Safest" and "Best priced" are the same engine and the same gates read in the
 * two directions a bet can be good: likely to land, or generously priced. They
 * are genuinely different bets — the market prices certainty, so a likely
 * winner pays little and a big payout is a longshot, and no single ordering can
 * be both. Showing both as finished lists keeps the choice ("which kind of bet
 * am I making today") without handing back the choice that ruined the old
 * board ("which gates should the model use"), where whatever it recommended was
 * whatever you had just asked it to recommend.
 */
function renderTips() {
  const lg = state.board.leagues[state.league];
  const gamesById = new Map(lg.games.map((g) => [g.event_id, g]));
  const legs = flattenLegs(lg);

  const safeCfg = applyCertaintyTo(state.cfg);
  const safe = buildPicks(legs, safeCfg, 3);
  // Anything already in the safe list is not repeated below: the same bet under
  // two headings reads as two bets.
  const claimed = new Set(safe.map((p) => betId(state.league, legsOf(p))));
  const value = buildPicks(legs, state.cfg, 3)
    .filter((p) => !claimed.has(betId(state.league, legsOf(p))));

  renderPickList($("#tips-safe"), safe, safeCfg, legs, gamesById);
  renderPickList($("#tips-value"), value, state.cfg, legs, gamesById);

  const all = [...safe, ...value];
  const totalUnits = all.reduce((t, p) => t + p.stakeUnits, 0);
  $("#tips-note").textContent = all.length
    ? `${all.length} bet${all.length > 1 ? "s" : ""} · ${+totalUnits.toFixed(1)}U total`
    : "nothing today";
}

/** The leg shape betId() expects, from a built pick. */
const legsOf = (pick) => pick.legs.map((l) => ({ event_id: l.eventId, side: l.side }));

/**
 * Why the list looks the way it does on leg count.
 *
 * Selection walks singles first and stops once it has enough picks, so parlays
 * only appear when singles cannot reach the payout floor on their own. That
 * ordering is deliberate and it is the one thing about this board people ask
 * about, because a night of all singles looks like a missing feature.
 *
 * It is not. A parlay's expectation is the product of its legs' expectations,
 * so stacking two legs roughly doubles the house's cut for the same opinion. A
 * parlay is a way to REACH a payout, never a source of edge — so when a single
 * already pays +295 there is nothing to reach for, and stacking would only cost
 * more.
 */
function legCountNote(picks, cfg) {
  if (!picks.length) return null;
  const parlays = picks.filter((p) => p.legs.length > 1).length;
  if (parlays === picks.length) {
    return `Every one of these is a parlay: no single side pays `
         + `${fmtOdds(cfg.minCombined)} on its own tonight, so legs are stacked to `
         + `reach it. Stacking costs — a parlay's expectation is the product of its `
         + `legs', so two legs roughly double the house's cut on the same opinion.`;
  }
  if (parlays === 0) {
    // Say what actually happened: singles filled the list. Naming the payout
    // floor here was wrong on the safe list, where the floor is -400 and every
    // single clears it trivially — the reason was never the floor.
    let why = `All singles tonight — ${picks.length} cleared on their own, and `
            + `singles are taken first. Parlays only appear when singles cannot fill `
            + `the list, because a parlay is a way to reach a payout, not a source of `
            + `edge: its expectation is the product of its legs', so stacking two `
            + `roughly doubles the house's cut on the same opinion.`;
    if (cfg.minPickWinProb > 0) {
      why += ` Here a parlay also has to stay above `
           + `${(cfg.minPickWinProb * 100).toFixed(0)}% as a whole ticket, which two `
           + `likely winners only just manage — 76% and 70% multiply to 53%.`;
    }
    return why;
  }
  return `${parlays} of these stack legs to reach ${fmtOdds(cfg.minCombined)}; the `
       + `rest get there on their own. Singles are preferred — a parlay multiplies `
       + `the house's cut along with the payout.`;
}

function renderPickList(box, picks, cfg, legs, gamesById) {
  box.innerHTML = "";

  const gated = legs.filter((l) => passesGates(l, cfg)).length;
  if (!picks.length) {
    // Which gate emptied the board, counted rather than guessed. "No tips" and
    // "no tips worth backing" are different answers and only one of them means
    // something is wrong.
    const staleCut = legs.filter((l) => l.stale).length;
    const certaintyCut = cfg.minWinProb > 0
      ? legs.filter((l) => !l.stale && l.model_prob < cfg.minWinProb).length : 0;
    const best = legs
      .filter((l) => passesGates(l, cfg))
      .map((l) => stakeUnits(l.model_prob, l.odds, l.implied_prob))
      .reduce((a, b) => Math.max(a, b), 0);

    let why;
    if (gated && best <= 0) {
      why = `${gated} sides cleared the gates, but every one of them sized under `
          + `half a unit — which is the engine saying it does not believe them `
          + `enough to back them. That is an answer, not a failure.`;
    } else if (gated) {
      why = `${gated} sides cleared the gates, but none combine to `
          + `${fmtOdds(cfg.minCombined)} without overlapping teams.`;
    } else if (cfg.minWinProb > 0) {
      why = `Nothing clears a ${(cfg.minWinProb * 100).toFixed(0)}% win chance with an `
          + `edge on the price — ${certaintyCut} sides were cut by the win-chance floor `
          + `alone. The market prices certainty accurately, so the likely winners `
          + `usually pay too little to be worth backing.`;
    } else {
      why = `No side clears ${cfg.minEdgePP.toFixed(1)}pp of edge on `
          + `${cfg.minSample}+ games of sample. Nothing here is worth a bet today `
          + `— keep your money in your pocket.`;
    }
    if (staleCut && cfg.excludeStale) {
      why += `<div class="empty-extra">${staleCut} side${staleCut > 1 ? "s were" : " was"} `
           + `excluded outright: a quarterback was ruled out after the ratings were `
           + `built, so the model has no informed opinion on either side of `
           + `${staleCut > 2 ? "those games" : "that game"}.</div>`;
    }
    box.appendChild(el("div", "empty", `<strong>No bets today</strong>${why}`));
    return;
  }

  picks.forEach((pick, i) => {
    const card = el("div", "tip");
    const head = el("div", "tip-head");
    head.innerHTML = `
      <div class="tip-title"><span class="tip-rank">#${i + 1}</span>${pick.label}</div>
      <div class="tip-meta">
        ${trustBadge(pick.trust)}
        <span class="dim">we say ${fmtPct(pick.winProb)} · market says ${fmtPct(pick.marketProb)}</span>
        <span class="tip-odds ${pick.combined > 0 ? "pos" : ""}">${fmtOdds(pick.combined)}</span>
      </div>`;
    card.appendChild(head);

    const table = el("table", "tip-legs");
    const body = el("tbody");
    for (const l of pick.legs) {
      const tr = el("tr");
      tr.innerHTML = `
        <td>
          <div class="lg-team">${l.team_name} ML</div>
          <div class="lg-game">${l.matchup}${l.neutral ? " · neutral" : ""} · ${kickoffLabel(l.kickoff)}</div>
          ${(() => {
            const wl = weatherLine(gamesById.get(l.eventId) || {});
            return wl ? `<div class="lg-game dim">${wl}</div>` : "";
          })()}
        </td>
        <td class="num">${fmtOdds(l.odds)}</td>
        <td class="num">${fmtPct(l.model_prob)}<div class="lg-game">model win%</div></td>
        <td class="num">${fmtPct(l.implied_prob)}<div class="lg-game">market win%</div></td>
        <td class="num pos">${fmtPP(l.edge_pp)}<div class="lg-game">fair ${fmtOdds(l.fair_odds)}</div></td>
        <td class="num">${confBadge(l.confidence)}</td>`;
      body.appendChild(tr);
    }
    table.appendChild(body);
    card.appendChild(table);

    const strip = flagStrip(pick.legs, gamesById);
    if (strip) card.appendChild(strip);

    const foot = el("div", "tip-foot");
    foot.appendChild(placementControls(
      betFromLegs(state.league, pick.legs, pick.combined, pick.winProb,
                  pick.marketProb, pick.combinedEdgePP),
      pick.stakeUnits
    ));
    foot.appendChild(el("span", "dim",
      (pick.legs.length > 1
        ? `${pick.legs.length} legs · no shared teams or games · `
        : "")
      + `1u returns ${pick.payout.toFixed(2)}u`));
    card.appendChild(foot);
    box.appendChild(card);
  });

  const note = legCountNote(picks, cfg);
  if (note) box.appendChild(el("p", "list-note", note));
}

function renderBoard() {
  const lg = state.board.leagues[state.league];
  const gamesById = new Map(lg.games.map((g) => [g.event_id, g]));
  let legs = flattenLegs(lg);
  if (state.onlyEV) legs = legs.filter((l) => l.edge_pp > 0);
  legs.sort((a, b) => b.edge_pp - a.edge_pp);

  const body = $("#board-table tbody");
  body.innerHTML = "";
  if (!legs.length) {
    body.innerHTML = `<tr><td colspan="12" class="dim" style="padding:20px;text-align:center">
      No priced sides to show.</td></tr>`;
    return;
  }
  for (const l of legs) {
    const passes = passesGates(l, state.cfg);
    const suggested = stakeUnits(l.model_prob, l.odds, l.implied_prob);
    const tr = el("tr");
    tr.innerHTML = `
      <td class="bet-cell"></td>
      <td class="${passes ? "team-cell" : "dim"}">${l.matchup}${l.neutral ? ' <span class="dim">N</span>' : ""}${flagBadge(gamesById.get(l.eventId) || {})}</td>
      <td class="dim">${kickoffLabel(l.kickoff)}</td>
      <td class="${passes ? "team-cell" : "dim"}">${l.team_abbr} <span class="dim">vs ${l.oppAbbr}</span></td>
      <td class="num">${fmtOdds(l.odds)}</td>
      <td class="num">${fmtPct(l.model_prob)}</td>
      <td class="num dim">${fmtPct(l.implied_prob)}</td>
      <td class="num ${l.edge_pp > 0 ? "pos" : "neg"}">${fmtPP(l.edge_pp)}</td>
      <td class="num dim">${fmtOdds(l.fair_odds)}</td>
      <td class="num dim">${l.sample.toFixed(1)}</td>
      <td class="num">${confBadge(l.confidence)}</td>
      <td class="num">${(() => {
        const u = stakeUnits(l.model_prob, l.odds, l.implied_prob);
        return u > 0
          ? `<strong>${unitsLabel(u)}</strong>`
          : `<span class="dim">no bet</span>`;
      })()}</td>`;
    // A single-leg ticket on this side, at whatever price is showing now.
    tr.querySelector(".bet-cell").appendChild(placementControls(
      betFromLegs(state.league, [l], l.odds, l.model_prob, l.implied_prob, l.edge_pp),
      suggested > 0 ? suggested : 1
    ));
    if (!passes) tr.style.opacity = "0.55";
    body.appendChild(tr);
  }
}

function renderRatings() {
  const lg = state.board.leagues[state.league];
  const body = $("#ratings-table tbody");
  body.innerHTML = "";
  lg.ratings.slice(0, 25).forEach((r, i) => {
    const tr = el("tr");
    tr.innerHTML = `<td class="dim">${i + 1}</td>
      <td class="team-cell">${r.name}</td>
      <td class="num">${r.elo.toFixed(0)}</td>
      <td class="num dim">${r.games}</td>`;
    body.appendChild(tr);
  });
}

function renderHistory() {
  const body = $("#history-table tbody");
  body.innerHTML = "";
  const settled = (state.history?.picks || [])
    .filter((p) => p.status !== "open" && p.league === state.league)
    .sort((a, b) => (b.graded_at || "").localeCompare(a.graded_at || ""))
    .slice(0, 25);

  if (!settled.length) {
    body.innerHTML = `<tr><td colspan="4" class="dim" style="padding:18px;text-align:center">
      No settled ${state.league} picks yet — results land after kickoff.</td></tr>`;
    return;
  }
  for (const p of settled) {
    const won = p.status === "won";
    const score = p.result ? `${p.result.away_score}–${p.result.home_score}` : "";
    const tr = el("tr");
    tr.innerHTML = `
      <td class="team-cell">${p.team_abbr} <span class="dim">vs ${p.opp_abbr}</span></td>
      <td class="num">${fmtOdds(p.open_odds)}</td>
      <td class="num ${p.clv_pp > 0 ? "pos" : p.clv_pp < 0 ? "neg" : "dim"}">${fmtPP(p.clv_pp || 0)}</td>
      <td class="${won ? "pos" : p.status === "lost" ? "neg" : "dim"}">
        ${p.status.toUpperCase()} <span class="dim">${score}</span></td>`;
    body.appendChild(tr);
  }
}

function render() {
  renderTabs();
  renderYourBets();
  renderGateSummary();
  renderTips();
  renderBoard();
  renderRatings();
  renderHistory();
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

/** The only thing that varies by league: college needs a deeper sample. */
function applyLeagueConfig() {
  state.cfg = {
    ...DEFAULTS,
    minSample: LEAGUE_MIN_SAMPLE[state.league] ?? DEFAULTS.minSample,
  };
}

async function boot() {
  const bust = `?t=${Math.floor(Date.now() / 60000)}`;
  let board, history;
  try {
    [board, history] = await Promise.all([
      fetch(`data/board.json${bust}`).then((r) => r.json()),
      fetch(`data/history.json${bust}`).then((r) => r.json()).catch(() => ({ picks: [], summary: {} })),
    ]);
  } catch (e) {
    $("#updated").textContent = "board unavailable";
    $("#tips").appendChild(el("div", "empty",
      `<strong>Could not load the board</strong>${e.message}`));
    return;
  }

  state.board = board;
  state.history = history;
  // Not in the Promise.all above: a missing function must not take the board
  // down with it, and the page is useful without the checkboxes.
  await fetchPlacements();
  const leagues = Object.keys(board.leagues);
  if (!leagues.length) {
    $("#tips").appendChild(el("div", "empty",
      "<strong>No leagues in this build</strong>The last run produced no data."));
    return;
  }
  state.league = leagues[0];
  applyLeagueConfig();

  const gen = new Date(board.generated_at);
  $("#updated").textContent = `updated ${gen.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
  $("#season").textContent = `${board.season} season`;

  $("#only-ev").addEventListener("change", (e) => {
    state.onlyEV = e.target.checked;
    renderBoard();
  });

  renderRecord();
  render();
}

boot();
