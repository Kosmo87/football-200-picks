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
/** A spread, signed, with no trailing zero: -6.5, +8.5, -1. */
const fmtLine = (n) => `${n > 0 ? "+" : ""}${+Number(n).toFixed(1)}`;

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

/**
 * Published stake: the ladder, capped by what the price justifies, snapped to a
 * half unit.
 *
 * The disagreement damper is deliberately NOT applied here — it is applied
 * earlier as a refusal (cfg.minTrustFactor). Doing both double-counted, and the
 * second application only flattened: every surviving bet is in the same trust
 * band, so the multiplier was the same 0.6 on all of them, and with half-unit
 * rounding that turned raw stakes of 0.30 through 0.70 into identical 0.5U
 * bets. Mirrors staking.stake_units.
 */
const stakeUnits = (p, odds, marketP) =>
  toHalfUnits(Math.round(Math.min(ladderUnits(p), kellyUnits(p, odds)) * 10) / 10);

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
  // Refuse, rather than merely shrink, a bet the model sits too far from.
  // The damper used to make a 12pp disagreement visibly smaller (0.3U against
  // 0.5U); half-unit stakes cannot express that, so it became a no-op for
  // exactly the bets it existed to punish. If the stake cannot carry the
  // warning, the bet is not made. Mirrors DEFAULT_MIN_TRUST_FACTOR.
  minTrustFactor: 0.6,
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

/**
 * True when every leg is needed to clear the payout floor. Mirrors
 * picks._is_minimal_parlay.
 *
 * Combined odds only lengthen as legs are added, so if a leg can be dropped and
 * the rest still clear the floor, that leg buys nothing but another slice of
 * the house's cut. Needed because Kelly gets MORE permissive as a parlay
 * lengthens: a two-leg ticket that sized under the stake floor returns as a
 * four-leg one at a price big enough to pass, which is the payout growing
 * rather than the bet improving.
 */
function isMinimalParlay(legs, minCombined) {
  if (legs.length < 2) return true;
  for (let i = 0; i < legs.length; i++) {
    const rest = legs.filter((_, j) => j !== i);
    if (combineOdds(rest.map((l) => l.odds)) >= minCombined) return false;
  }
  return true;
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

/**
 * Port of picks.build_picks: every independent gated bet, uncapped by default.
 *
 * n used to default to 3, which was a display decision leaking into selection:
 * on a heavy slate the fourth-best bet was never computed, so a good week
 * looked identical to a thin one. The quality bar is the gates and the stake
 * floor. Still bounded — no pick reuses a team or a game.
 */
function buildPicks(allLegs, cfg, n = Infinity) {
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
        if (disagreementFactor(pick.winProb, pick.marketProb) < cfg.minTrustFactor) continue;
        picks.push(pick);
        markUsed(leg);
      }
      continue;
    }

    // Only legs that cannot reach the payout floor alone may be stacked; see
    // the note in picks.build_picks. A parlay buys reach, never edge, so a leg
    // already paying more than the floor has nothing to reach for — and
    // stacking it pays the house's cut twice for one opinion. Without this the
    // engine recycled singles it had just rejected for being too weak into
    // longer parlays, where the bigger combined price inflates the Kelly cap
    // enough to let them back through.
    const stackable = available.filter((l) => l.odds < cfg.minCombined);
    if (stackable.length < nLegs) continue;

    for (const cand of candidateCombos(stackable, nLegs, cfg.minCombined, POOL_CAPS[nLegs] || 25)) {
      if (picks.length >= n) break;
      if (cand.legs.some((l) => !canUse(l))) continue;
      if (!isMinimalParlay(cand.legs, cfg.minCombined)) continue;
      const pick = makePick(cand.legs, cand.combined);
      if (pick.stakeUnits < cfg.minStake) continue;
      if (pick.winProb < cfg.minPickWinProb) continue;
      if (disagreementFactor(pick.winProb, pick.marketProb) < cfg.minTrustFactor) continue;
      picks.push(pick);
      cand.legs.forEach(markUsed);
    }
  }

  // Likeliest to land first — that is what the page is read for. Confidence and
  // edge stay as tie-breakers. Mirrors picks.build_picks.
  picks.sort((a, b) =>
    b.winProb - a.winProb
    || b.avgConfidence - a.avgConfidence
    || b.combinedEdgePP - a.combinedEdgePP);
  return Number.isFinite(n) ? picks.slice(0, n) : picks;
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
function betId(league, legs, kind, points) {
  const parts = legs
    .map((l) => `${cleanIdPart(l.event_id)}.${cleanIdPart(l.side)}`)
    .sort();
  const tag = kind === "teaser" ? `T${cleanIdPart(points ?? 6)}-` : "";
  return `${cleanIdPart(league)}-${tag}${parts.join("+")}`;
}

/**
 * A teaser ticket, in the shape the store keeps.
 *
 * Separate from betFromLegs because the two are not the same bet written
 * differently. A teased leg has no price of its own -- the ticket is priced
 * once, by the book, at whatever it offers for six points -- so there is no
 * per-leg `odds` here, and `teased` is carried because the grader has nothing
 * else to settle against once the game is over.
 */
function teaserBet(league, legs, price, points) {
  return {
    league,
    kind: "teaser",
    points,
    odds: price,
    legs: legs.map((l) => ({
      event_id: l.event_id, side: l.side, team_abbr: l.team_abbr,
      team_name: l.team_name, matchup: l.matchup, kickoff: l.kickoff,
      spread: l.spread, teased: l.teased, band: l.band,
      prob: l.prob, push_risk: l.push_risk,
    })),
  };
}

/**
 * Closing-line value on a tagged bet, in the unit the bet actually moves in.
 *
 * Moneylines move in probability points; a teased leg moves in points of
 * spread, and printing one as the other would be a units error dressed up as a
 * number. Blank until a build has seen a live price for the side, because "no
 * reading yet" and "no value" are different answers.
 */
const clvLabel = (r) => {
  if (r.kind === "teaser") {
    return r.clv_pts == null ? "—"
      : `${r.clv_pts >= 0 ? "+" : ""}${Number(r.clv_pts).toFixed(1)}pt`;
  }
  return r.clv_pp == null ? "—" : fmtPP(Number(r.clv_pp));
};
const clvTone = (r) => {
  const v = r.kind === "teaser" ? r.clv_pts : r.clv_pp;
  return v == null ? "dim" : v > 0 ? "pos" : v < 0 ? "neg" : "dim";
};

/**
 * What a stored bet is called. Mirrors placements.bet_label.
 *
 * A teaser prints the line it was teased TO, not the number it came from: -7
 * is the qualifying condition, -1 is the bet, and a row that shows -7 next to
 * a win at -1 misreports what was placed.
 */
function betLabel(league, legs, kind, points) {
  if (!legs || !legs.length) return "(no legs)";
  if (kind === "teaser") {
    return `${+(points || 6)}-pt teaser: `
      + legs.map((l) => `${l.team_abbr} ${fmtLine(l.teased)}`).join(" + ");
  }
  if (legs.length === 1) return `${legs[0].team_abbr} ML`;
  return `${legs.length}-leg: ${legs.map((l) => l.team_abbr).join(" + ")}`;
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

  const id = betId(bet.league, bet.legs, bet.kind, bet.points);
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
  const id = betId(bet.league, bet.legs, bet.kind, bet.points);
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
    + `${betLabel(bet.league, bet.legs, bet.kind, bet.points)} ${fmtOdds(bet.odds)}`;
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
    const label = betLabel(r.league, legs, r.kind, r.points);
    const u = r.units == null ? null : Number(r.units);
    const tr = el("tr");
    tr.innerHTML = `
      <td class="team-cell">${label}
        <div class="lg-game">${legs.map((l) => l.matchup).join(" · ")}</div></td>
      <td class="num">${fmtOdds(r.odds)}</td>
      <td class="num">${Number(r.stake || 0).toFixed(2)}u</td>
      <td class="num ${u > 0 ? "pos" : u < 0 ? "neg" : "dim"}">${
        u == null ? "—" : `${u >= 0 ? "+" : ""}${u.toFixed(2)}u`}</td>
      <td class="num ${clvTone(r)}">${clvLabel(r)}</td>
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
  // Teaser ticket being assembled: event ids, in the order they were picked,
  // and the price the book is actually offering. A teaser is built by hand at
  // the book, so the page mirrors that rather than pre-forming tickets.
  teaserPicks: [],
  teaserPrice: -110,
  teaserLegCount: 0,
  // Which tease the watchlist is showing. The two sizes qualify DIFFERENT
  // numbers, so switching clears the selection rather than carrying legs from
  // one window into a ticket priced for the other.
  teaserPoints: 6,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};

const kickoffMs = (iso) => {
  const t = new Date(iso || "").getTime();
  return Number.isNaN(t) ? Infinity : t;
};


function dayLabel(iso) {
  const t = kickoffMs(iso);
  if (t === Infinity) return "Date TBD";
  return new Date(t).toLocaleDateString(undefined, {
    weekday: "long", month: "short", day: "numeric",
  });
}

function kickoffLabel(iso) {
  if (!iso) return "TBD";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
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





/**
 * The two-leg ticket, priced at what the book is actually offering.
 *
 * The price is typed in rather than assumed, because it is the only number
 * that decides whether this bet exists and the page cannot see it — the
 * shortlist comes from a spread feed, not from a teaser menu. It is also what
 * gets stored, so the ledger grades the ticket that was really placed.
 *
 * The verdict under the price is the whole point of typing it: these two legs
 * at -110 and the same two at -130 are a +3% bet and a -4% bet.
 */
function teaserTicket(chosen, t) {
  const wrap = el("div", "teaser-ticket");
  // Default to this leg count's ladder price rather than a fixed -110: a
  // 5-leg ticket priced at -110 would read as a catastrophic bet, and the
  // number people will actually be quoted is the ladder's.
  const ladderPrice = (((t.ladder || {})[String(t.points)] || {}).prices
                        || {})[String(chosen.length)];
  if (state.teaserLegCount !== chosen.length) {
    state.teaserLegCount = chosen.length;
    if (ladderPrice != null) state.teaserPrice = Number(ladderPrice);
  }
  const price = Number(state.teaserPrice) || ladderPrice || -110;
  // The measured rate for this leg count where one exists, and only the
  // product of the legs as a fallback. They are close at two legs (53.70%
  // measured against 53.13% multiplied) but the measured one is the number the
  // solver prices with, and two parts of the same page must not disagree about
  // what a ticket wins.
  const measured = (((t.ladder || {})[String(t.points)] || {}).joint
                     || {})[String(chosen.length)];
  const p = measured != null
    ? Number(measured)
    : chosen.reduce((a, l) => a * l.prob, 1);
  const dec = americanToDecimal(price);
  const ev = p * (dec - 1) - (1 - p);
  // The conservative threshold has to come from the SAME number the verdict
  // uses. Deriving it by re-multiplying the legs while EV came from the
  // measured joint rate made the card contradict itself: it called a +310
  // ticket a bet and printed a break-even of +329. So the joint rate's own
  // uncertainty is propagated instead -- one standard error on a leg, carried
  // through n legs, which is d(p^n) = n * p^(n-1) * se.
  const legSe = t.per_leg_stderr || 0;
  const legRate = t.per_leg_rate || Math.pow(p, 1 / chosen.length);
  const jointSe = chosen.length * Math.pow(legRate, chosen.length - 1) * legSe;
  const need = priceForProb(Math.max(0.0001, p - jointSe));

  wrap.appendChild(el("div", "tt-head",
    `<strong>${t.points}-point teaser, ${chosen.length} legs</strong> · `
    + chosen.map((l) => `${l.team_abbr} ${fmtLine(l.teased)}`).join(" + ")
    + ` · ticket wins ${fmtPct(p)}`
    + (measured != null ? "" : " <span class=\"dim\">(legs multiplied — no "
        + "measured rate for this leg count)</span>")));

  const row = el("div", "tt-row");
  const priceLabel = el("label", "tt-price");
  priceLabel.innerHTML = "<span>Price at your book</span>";
  const input = el("input");
  input.type = "number";
  input.step = "5";
  input.value = String(price);
  input.setAttribute("aria-label", "Teaser price in American odds");
  input.addEventListener("change", () => {
    state.teaserPrice = Math.round(Number(input.value) || -110);
    renderTeasers();
  });
  priceLabel.appendChild(input);
  row.appendChild(priceLabel);

  const ok = ev > 0 && dec > 1;
  const these = chosen.length === 2 ? "These two" : `These ${chosen.length}`;
  row.appendChild(el("div", `tt-verdict ${ok ? "pos" : "neg"}`,
    ok
      ? `A bet: ${(ev * 100).toFixed(1)}% expected return at ${fmtOdds(price)}. `
        + `Break-even is ${fmtOdds(need)} allowing for the error on the band rates.`
      : `Not a bet at ${fmtOdds(price)}: ${(ev * 100).toFixed(1)}% expected `
        + `return. ${these} need ${fmtOdds(need)} or better.`));
  wrap.appendChild(row);

  // Tagged even when the price says no. The ledger's job is to record what was
  // placed, not to agree with it, and a bet the page argued against is the
  // most useful row in it.
  const bet = teaserBet(state.league, chosen, price, t.points);
  const foot = el("div", "tt-foot");
  foot.appendChild(placementControls(bet, 1));
  foot.appendChild(el("span", "dim",
    `1u returns ${(dec - 1).toFixed(2)}u · a pushed leg is graded a loss, `
    + `which is how most books settle one inside a ${chosen.length}-team teaser`));
  wrap.appendChild(foot);
  return wrap;
}

/** The worst price at which a chance is still break-even. Mirrors max_price. */
function priceForProb(p) {
  if (p <= 0 || p >= 1) return 0;
  const profit = (1 - p) / p;
  return profit < 1 ? -Math.floor(100 / profit) : Math.ceil(profit * 100);
}

/**
 * The teaser shortlist, which leads the page wherever it exists.
 *
 * It is a list of NUMBERS, deliberately. Every leg in a band won at the same
 * rate whether it was a favourite or a dog, home or away (73.2-74.3%, all
 * inside one standard error), so ranking them by team would be inventing a
 * signal the measurement says is not there. They are printed in probability
 * order only because a whole-number teased line can push.
 *
 * The price does the deciding. Two legs at -110 is +3.4%; the same two at -130
 * is -4.2%. So the list is useless without the threshold under it, and the
 * threshold is given for both the best pair and the worst so any two legs on
 * it can be priced without recomputing anything.
 */
function renderTeasers() {
  const all = (state.board.leagues[state.league] || {}).teasers;
  // The block for the chosen tease, with the ladder carried across: the ladder
  // is stored once on the 6-point block because it prices both.
  const t = all && (state.teaserPoints === 10
    ? (all.ten ? { ...all.ten, ladder: all.ladder, at_my_books: all.at_my_books,
                   playable: all.playable, verified_prices: all.verified_prices } : null)
    : all);
  const toggle = $("#teaser-points");
  if (toggle) {
    for (const b of toggle.querySelectorAll("button")) {
      const pts = Number(b.dataset.pts);
      b.setAttribute("aria-pressed", String(pts === state.teaserPoints));
      b.onclick = () => {
        if (state.teaserPoints === pts) return;
        state.teaserPoints = pts;
        state.teaserPicks = [];
        state.teaserLegCount = 0;
        renderTeasers();
      };
    }
  }
  const section = $("#teaser-section");
  const legs = (t && t.legs) || [];
  // Two legs or it is not a teaser. One qualifying number is not a bet.
  if (legs.length < 2) {
    // Hide the list but keep the toggle reachable: the other tease size may
    // have legs even when this one does not.
    section.hidden = !(all && ((all.legs || []).length >= 2
                               || (((all.ten || {}).legs) || []).length >= 2));
    $("#teaser-legs").innerHTML = "";
    if (!section.hidden) {
      $("#teaser-note").textContent =
        `no qualifying legs at ${state.teaserPoints} points — try the other size`;
    }
    return;
  }
  section.hidden = false;

  const box = $("#teaser-legs");
  box.innerHTML = "";

  // Whether this is a bet or a watchlist is decided by the accounts, not the
  // games. A section headed "bets" above a product neither book sells is the
  // same failure as staking the model's longshots: presenting something as
  // placeable because the arithmetic liked it.
  const mine = t.at_my_books || [];
  const playable = t.playable !== false;
  $("#teaser-heading").firstChild.textContent =
    playable ? "Bets this week " : "Teaser watchlist ";
  $("#teaser-note").textContent = playable
    ? `${legs.length} qualifying legs · pick two to six · ${t.points}-point teaser`
    : `${legs.length} qualifying legs · nothing placeable at your books`;

  if (mine.length && !playable) {
    box.appendChild(el("div", "empty",
      `<strong>Not placeable at your books</strong>`
      + mine.map((m) => `${m.reason}.`).join(" ")
      + `<div class="empty-extra">The legs below still qualify — this is a `
      + `price-and-product problem, not a change in the measurement. A book `
      + `that sells 2-team 6-pointers at ${fmtOdds(t.max_price_best_se)} or `
      + `better turns them back into bets; until then the honest answer for `
      + `the NFL this week is no bet.</div>`));
  }

  const table = el("table", "data teaser-table");
  table.innerHTML = `
    <thead><tr>
      <th class="bet-col">Use</th>
      <th>Game</th><th>Kickoff</th><th>Leg</th>
      <th class="num">Teased to</th><th class="num">Leg win%</th><th>Range</th>
    </tr></thead>`;
  const body = el("tbody");
  let day = null;
  for (const l of legs) {
    const d = dayLabel(l.kickoff);
    if (d !== day) {
      const head = el("tr", "teaser-day");
      head.innerHTML = `<td colspan="7">${d}</td>`;
      body.appendChild(head);
      day = d;
    }
    const picked = state.teaserPicks.includes(l.event_id);
    const tr = el("tr", picked ? "picked" : "");
    tr.innerHTML = `
      <td class="bet-cell"><input type="checkbox" ${picked ? "checked" : ""}
        aria-label="Use ${l.team_abbr} ${fmtLine(l.teased)} in a teaser"></td>
      <td>${l.matchup}</td>
      <td class="dim">${kickoffLabel(l.kickoff)}</td>
      <td class="team-cell">${l.team_abbr} ${fmtLine(l.spread)}</td>
      <td class="num"><strong>${fmtLine(l.teased)}</strong>${
        l.push_risk ? ' <span class="flag-dot low" title="Teased onto a whole'
          + ' number: 2.5% of these push, and most books grade a push inside a'
          + ' two-team teaser as a loss.">!</span>' : ""}</td>
      <td class="num">${fmtPct(l.prob)}</td>
      <td class="dim">${l.band}</td>`;
    tr.querySelector("input").addEventListener("change", () => {
      const picks = state.teaserPicks.filter((id) => id !== l.event_id);
      // Up to six, oldest out first. The cap used to be three, which quietly
      // made the best tickets untaggable: the solver recommends 4, 5 and 6-leg
      // teasers because the ladder asks for less per leg as legs are added, and
      // a bet that cannot be tagged cannot be graded.
      if (!state.teaserPicks.includes(l.event_id)) picks.push(l.event_id);
      state.teaserPicks = picks.slice(-6);
      renderTeasers();
    });
    body.appendChild(tr);
  }
  table.appendChild(body);
  const scroll = el("div", "table-scroll");
  scroll.appendChild(table);
  box.appendChild(scroll);

  const chosen = state.teaserPicks
    .map((id) => legs.find((l) => l.event_id === id))
    .filter(Boolean);
  if (chosen.length >= 2) box.appendChild(teaserTicket(chosen, t));

  // The number to carry to the book, which is the actual output of all this.
  const best = t.max_price_best, worst = t.max_price_worst, se = t.max_price_best_se;
  box.appendChild(el("p", "teaser-price",
    `<strong>Play only at ${fmtOdds(se)} or better.</strong> The two best legs `
    + `here break even at ${fmtOdds(best)} on the measured rate, but that rate `
    + `is an estimate off 1,804 games — one standard error down and the same `
    + `pair needs ${fmtOdds(se)}, so that is the number to hold out for. The two `
    + `weakest legs need ${fmtOdds(worst)}. Anything longer is a losing bet `
    + `however good the teams look.`
    + (t.max_price_3_se
        ? `<br><strong>Three legs: ${fmtOdds(t.max_price_3_se)} or better</strong> `
          + `(${fmtOdds(t.max_price_3)} on the measured rate). A book can price a `
          + `longer teaser relatively better, so a refused 2-leg price says `
          + `nothing about this one — it is worth asking for at the slip.`
        : "")));
  const age = t.captured_at
    ? `, as of ${kickoffLabel(t.captured_at)}` : "";
  box.appendChild(el("p", "list-note",
    (t.provider_is_mine
      ? `Legs are read off ${t.provider}'s own number${age} — a book you hold. `
      : `Legs are read off ${t.provider || "one book"}'s number, which is not a `
        + `book you hold: no snapshot from BetMGM or FanDuel was recent enough, `
        + `so this is ESPN's quote. `)
    + `Either way it is a shortlist rather than a quote — a band is decided by `
    + `a half point and books disagree by that often. Confirm the number and `
    + `the teaser price at your own book before placing anything. Pushes: a leg `
    + `teased onto a whole number is marked, and is charged for that risk in `
    + `its win% already.`));
}




/**
 * Say so when the board has stopped being refreshed.
 *
 * The only failure in this pipeline that looks like success. On 2026-09-17 a
 * dead deploy token left the site serving a 25-hour-old board: the page
 * rendered, the prices looked plausible, and three of the games had already
 * been played. Nothing on the page said a word, because nothing on the page
 * was wrong -- it was just old.
 *
 * Thresholds follow the MEASURED rebuild rate, not the cron line. The schedule
 * asks for hourly; GitHub actually delivers a scheduled run every 2.5 to 6.3
 * hours on this repo, because cron there is best-effort and drops runs under
 * load. Warning at three hours would therefore fire on a perfectly healthy
 * board most of the day, and a banner that cries wolf is worse than none --
 * it teaches you to ignore the one that matters. Eight hours means the
 * schedule has genuinely stopped; a day means the deploy is broken.
 */
function renderStaleness() {
  const box = $("#staleness");
  const built = new Date(state.board?.generated_at || "");
  if (Number.isNaN(built.getTime())) {
    box.hidden = true;
    return;
  }
  const hours = (Date.now() - built.getTime()) / 3.6e6;
  if (hours < 8) {
    box.hidden = true;
    return;
  }
  const severe = hours >= 24;
  box.className = severe ? "stale severe" : "stale";
  box.hidden = false;
  box.innerHTML =
    `<strong>This board is ${hours < 24
      ? `${Math.round(hours)} hours old`
      : `${Math.floor(hours / 24)} day(s) old`}.</strong>`
    + `It rebuilds every few hours, so ${severe
      ? "something has been failing for a while — prices have moved, and games on it may already have been played"
      : "the schedule has stopped and prices have moved since"}. `
    + `<span class="dim">Built ${built.toLocaleString(undefined, {
        weekday: "short", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit" })}. `
    + `Placing anything off a stale board is betting into a price that no `
    + `longer exists.</span>`;
}

/**
 * The payout-target solver, in the browser. Mirrors target.py.
 *
 * Probabilities come from `implied_prob`, which the build already de-vigged,
 * so this is the market's own opinion rather than the model's. That is the
 * whole design: the model has been graded against closing lines and loses, and
 * a solver that priced its routes off the model would hand back the same
 * inflated numbers that made the old board recommend longshots.
 *
 * The routes are deliberately compared on the SAME target, because the useful
 * answer is not "here is a bet" but "here is what wanting +300 costs you".
 */
function solveRoutes(target, maxLegs = 6, tolerance = 0.12) {
  const lg = state.board.leagues[state.league] || {};
  const want = americanToDecimal(target);
  // A route paying a little under the ask is still worth seeing when it costs
  // fewer legs: at a +300 ask the 4-leg 6-pointer pays +265 and wins 30.3%
  // against the 6-leg's 28.7%, and hiding it because it missed the number by
  // 35 cents on the dollar is answering the letter of the question instead of
  // the point of it. Flagged, never silently substituted.
  const floor = want * (1 - tolerance);
  const legs = [];
  for (const g of lg.games || []) {
    for (const l of g.legs || []) {
      legs.push({
        eventId: g.event_id, abbr: l.team_abbr, odds: l.odds,
        prob: l.implied_prob, matchup: g.short_name || g.name,
        dec: americanToDecimal(l.odds),
      });
    }
  }
  const routes = [];
  // Teasers only. Singles and moneyline parlays are rearrangements of the same
  // market prices and land about 4% short however they are stacked; showing
  // them beside the one structure that measures positive buried it. They live
  // on in target.py, which is where a price gets checked.
  const ladder = (lg.teasers || {}).ladder;
  if (ladder) {
    for (const [pts, block] of Object.entries(ladder)) {
      for (const [n, price] of Object.entries(block.prices || {})) {
        const joint = (block.joint || {})[n];
        const dec = americanToDecimal(Number(price));
        if (!joint || dec < floor || Number(n) > maxLegs) continue;
        const verified = (lg.teasers.verified_prices || []).includes(`${pts}pt:${n}`);
        // The legs for THIS tease size. The 10-point windows are different
        // numbers from the 6-point ones, so a route has to name its own.
        const pool = pts === "10"
          ? (((lg.teasers || {}).ten || {}).legs || [])
          : ((lg.teasers || {}).legs || []);
        const fill = pool.slice(0, Number(n));
        const fillable = pool.length >= Number(n);
        routes.push({
          what: `${pts}-pt teaser, ${n} legs (${fmtOdds(Number(price))})`,
          dec, prob: joint, teaser: true, legs: Number(n), fillable,
          fill, price: Number(price), points: Number(pts),
          // Each leg's own chance, kept beside the ticket's. Showing only the
          // ticket makes 15.6% look like a bad bet built from good legs;
          // showing only the leg would be a lie, because five of six pays
          // nothing. Both, always.
          perLeg: pts === "10"
            ? ((lg.teasers || {}).ten || {}).per_leg_rate
            : (lg.teasers || {}).per_leg_rate,
          why: (fillable
                 ? `${pool.length} qualifying legs at ${pts} points, so this is fillable today. `
                 : `Only ${pool.length} qualifying legs at ${pts} points — needs ${n}. `)
             // Name the book, or say plainly that none was recorded. Ladders
             // differ by shop, so a price with no source attached invites the
             // reader to assume their own book pays it.
             + `Legs off ${(lg.teasers || {}).provider || "one book"}; `
             + `price: ${((lg.teasers || {}).price_sources || {})[`${pts}:${n}`]
                          || "not checked"}. `
             + (joint * (dec - 1) - (1 - joint) > 0
                 ? `The chance is measured from completed games rather than read `
                   + `off the price, and here it comes in above what the ladder `
                   + `demands — that is the whole bet.`
                 : `The chance is measured from completed games, and here it `
                   + `falls short of what this ladder price demands, so the `
                   + `structure reaches the payout without beating it.`),
        });
      }
    }
  }
  // Ranked by chance of winning, then by FEWER LEGS when two routes are within
  // a point of each other. Legs are a cost the percentages do not show: every
  // one is another line to get down at the same book, another chance of a
  // push, and another game that can be spoiled by a late scratch.
  for (const r of routes) r.short = r.dec < want;
  return routes.sort((a, b) =>
    (Math.abs(b.prob - a.prob) > 0.01 ? b.prob - a.prob : (a.legs || 1) - (b.legs || 1)));
}

/** The price a probability deserves, before anyone takes a cut. */
function fairOdds(p) {
  if (p <= 0 || p >= 1) return 0;
  const dec = 1 / p;
  return dec >= 2 ? Math.round((dec - 1) * 100) : -Math.round(100 / (dec - 1));
}

function renderSolver() {
  const box = $("#solver-routes");
  box.innerHTML = "";
  const target = Math.round(Number($("#solver-target").value) || 300);
  const maxLegs = Number($("#solver-maxlegs").value) || 12;
  // A floor on the win chance, because "pay me +750" and "I want to win more
  // often than not" are both real asks and the honest answer to holding both
  // at once is usually "nothing does that" — which this now says out loud
  // instead of quietly showing a 15% ticket.
  const minWin = Math.min(0.95, Math.max(0, (Number($("#solver-minwin").value) || 0) / 100));
  const want = americanToDecimal(target);
  const fair = 1 / want;
  const all = solveRoutes(target, maxLegs);
  const routes = all.filter((r) => r.prob >= minWin);
  $("#solver-note").textContent = `${state.league} · best chance at the payout you name`;
  $("#solver-summary").innerHTML =
    `${fmtOdds(target)} pays ${want.toFixed(2)}x, so <strong>${fmtPct(fair)}</strong> `
    + `is break-even — anything above that is the book paying you to take it`;

  if (!routes.length) {
    const best = all.length ? all[0] : null;
    box.appendChild(el("div", "empty",
      minWin > 0 && best
        ? `<strong>Nothing pays ${fmtOdds(target)} and wins ${fmtPct(minWin)} of `
          + `the time</strong>The best this board can do at that payout is `
          + `${fmtPct(best.prob)} — ${best.what.toLowerCase()}. Payout and `
          + `certainty are one knob: asking for more of both is asking the `
          + `market for a gift.`
        : `<strong>Nothing on this board reaches ${fmtOdds(target)}</strong>`
          + `Ask for less and the routes come back.`));
    return;
  }

  // Meeting the ask comes first. A +265 ticket is worth seeing when +300 was
  // asked for -- it is one leg cheaper and likelier -- but it is not an
  // answer to the question, so it sits under its own heading instead of at
  // the top pretending to be one.
  const meets = routes.filter((r) => !r.short);
  const under = routes.filter((r) => r.short);
  const best = (meets[0] || routes[0]);
  let headed = false;
  [...meets, ...under].forEach((r) => {
    if (r.short && !headed) {
      headed = true;
      box.appendChild(el("p", "group-head",
        `Just under ${fmtOdds(target)} — fewer legs, better odds of landing`));
    }
    const ev = r.prob * (r.dec - 1) - (1 - r.prob);
    const cls = ev > 0 ? "route beats-fair" : r === best ? "route best" : "route";
    const node = el("div", cls);
    const pays = r.dec >= 2 ? Math.round((r.dec - 1) * 100) : -Math.round(100 / (r.dec - 1));
    node.innerHTML = `
      <div class="route-what">${r === best ? "<strong>Best chance:</strong> " : ""}${r.what}${
        r.teaser && !r.fillable ? ' <span class="dim">(not fillable today)</span>' : ""}</div>
      <div class="route-nums">${(r.legs || 1) > 1 && r.perLeg
          ? `each leg ${fmtPct(r.perLeg)} · <strong>all ${r.legs} land ${fmtPct(r.prob)}</strong>`
          : `wins <strong>${fmtPct(r.prob)}</strong>`} · pays ${fmtOdds(pays)}${
        r.short ? ' <span class="route-short">(under your ask)</span>' : ""}
        · <span class="${ev > 0 ? "pos" : "neg"}">${ev >= 0 ? "+" : ""}${(ev * 100).toFixed(1)}%</span></div>
      <div class="route-why">${r.why}</div>
      ${r.fill && r.fill.length ? `<div class="route-legs">${
        r.fill.map((l) => `<span class="rl"><strong>${l.team_abbr} ${fmtLine(l.teased)}</strong>`
          + `<span class="dim"> from ${fmtLine(l.spread)} · ${l.matchup}</span></span>`).join("")
      }</div>` : ""}`;
    // Tag it here rather than rebuilding it below: this is the card someone
    // is looking at when they decide, and a ticket that cannot be marked from
    // where it is read does not get marked at all.
    if (r.teaser && r.fill && r.fill.length) {
      const foot = el("div", "route-foot");
      foot.appendChild(placementControls(
        teaserBet(state.league, r.fill, Number(r.price), Number(r.points)), 1));
      foot.appendChild(el("span", "dim",
        `1u returns ${(r.dec - 1).toFixed(2)}u · a pushed leg is graded a loss`));
      node.appendChild(foot);
    }
    box.appendChild(node);
  });

  // The ceiling, stated once. Without it the list reads as though a better
  // percentage might be hiding somewhere on the board, and it is not: every
  // route to a fixed payout is pinned near its break-even by the price, and
  // only a measured structure lifts it.
  const beats = routes.filter((r) => r.prob * (r.dec - 1) - (1 - r.prob) > 0);
  box.appendChild(el("p", "list-note",
    `The most likely way to be paid ${fmtOdds(target)} on this board wins `
    + `<strong>${fmtPct(best.prob)}</strong>. `
    + (beats.length
        ? (beats.length === 1
            ? `One route beats the price — the ${beats[0].what.split(" (")[0]} — `
              + `because its chance is measured against a fixed ladder rather `
              + `than read off the price.`
            : `${beats.length} routes beat the price, all of them teasers: a `
              + `measured chance against a fixed ladder is the only way that `
              + `happens.`)
        : `None of them beat the price: every route is a rearrangement of the `
          + `same market numbers, so they all sit about 4% short. A teaser leg `
          + `count that clears ${fmtOdds(target)} would be the exception, and `
          + `none does on this board.`)));
}

function render() {
  // The page is the teaser tickets, the legs behind them, and what you
  // placed. Everything else -- the Elo model's board, the full price table,
  // the power ratings, its settled picks -- described a strategy the ledger
  // retired, and kept a reader scrolling past four sections to reach the one
  // bet worth making.
  renderStaleness();
  renderTabs();
  renderSolver();
  renderTeasers();
  renderYourBets();
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

  for (const id of ["#solver-target", "#solver-maxlegs", "#solver-minwin"]) {
    $(id).addEventListener("change", renderSolver);
    $(id).addEventListener("input", renderSolver);
  }

  render();
}

boot();
