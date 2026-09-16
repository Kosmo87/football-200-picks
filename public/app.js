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

/** Picks by first kickoff, each parlay's legs in kickoff order too. */
function byKickoff(picks) {
  return picks
    .map((p) => ({ ...p, legs: [...p.legs].sort((a, b) => kickoffMs(a.kickoff) - kickoffMs(b.kickoff)) }))
    .sort((a, b) => kickoffMs(a.legs[0].kickoff) - kickoffMs(b.legs[0].kickoff));
}

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

/**
 * Games of evidence behind the rating.
 *
 * This replaced a 0-100 "signal" score that was mostly a restatement of numbers
 * already on the card: 35 of its points were the claimed edge, which is the
 * subtraction of the model and market columns sitting next to it, and 25 were a
 * function of the price, also shown. Only the 40 sample points said anything
 * new, so the card now shows that directly instead of blended into an index
 * whose movement could not be attributed to anything.
 */
function sampleBadge(sample) {
  const tone = sample >= 6 ? "high" : sample >= 3 ? "med" : "low";
  const word = sample >= 6 ? "deep" : sample >= 3 ? "fair" : "thin";
  return (
    `<span class="badge ${tone}" title="Games of evidence behind this rating, ` +
    `including capped credit for last season. Thin samples move a rating a long ` +
    `way on one result.">${sample.toFixed(1)} gm · ${word}</span>`
  );
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
/**
 * Does the model board still get to call itself a set of bets?
 *
 * Only where nothing better exists. Where a teaser shortlist is present the
 * model's own graded record — 10-24 at 5-10pp of claimed edge, 5-21 at 10+ —
 * is a worse bet than the teaser's measured 73.6% per leg, so the picks stay
 * on the page as an evaluation record and lose their stakes. Reference mode is
 * therefore decided by what else is on the board, not by league.
 */
// Always, now, in both leagues. It was briefly conditional on a teaser
// shortlist existing, which made college the exception by accident -- and
// college is the worse half: its longshots have gone 4-20. The Elo picks are
// an evaluation record everywhere, and if that ever changes it will be because
// a backtest said so, not because a section had nothing else to show.
const referenceMode = () => true;

/** A bucket of settled picks: record, realised units, what was claimed. */
function gradedBucket(rows) {
  const won = rows.filter((r) => r.status === "won").length;
  const units = rows.reduce((u, r) => {
    const d = r.open_odds > 0 ? r.open_odds / 100 : 100 / -r.open_odds;
    return u + (r.status === "won" ? d : -1);
  }, 0);
  const model = rows.reduce((a, r) => a + (r.model_prob || 0), 0) / (rows.length || 1);
  const market = rows.reduce((a, r) => a + (r.open_implied || 0), 0) / (rows.length || 1);
  return { won, n: rows.length, units, roi: rows.length ? units / rows.length : 0, model, market };
}

/**
 * Why the model board carries no stake, in this league's own numbers.
 *
 * Read off the ledger at render time rather than written into the page. The
 * first version quoted the September buckets as prose, which would have had
 * the page defending the decision with numbers months out of date — and a
 * stale number arguing for a decision is worse than no number, because it
 * cannot be checked against the table two sections below it.
 */
function renderReferenceNote() {
  const el_ = $("#tips-reference-note");
  const rows = (state.history?.picks || []).filter(
    (p) => p.league === state.league && (p.status === "won" || p.status === "lost"));
  if (rows.length < 8) {
    el_.innerHTML = `Kept for evaluation, not to bet. The model's picks are `
      + `graded in public here and carry no stake: every version of this engine `
      + `tested against closing lines has lost, and ${rows.length} settled `
      + `${state.league} pick${rows.length === 1 ? "" : "s"} is too few to `
      + `argue otherwise either way.`;
    return;
  }
  const mid = gradedBucket(rows.filter((r) => (r.edge_pp || 0) >= 5 && (r.edge_pp || 0) < 10));
  const wide = gradedBucket(rows.filter((r) => (r.edge_pp || 0) >= 10));
  const long = gradedBucket(rows.filter((r) => r.open_odds >= 150));
  const pct = (b) => `${b.roi >= 0 ? "+" : ""}${(b.roi * 100).toFixed(0)}%`;
  const part = (label, b) => b.n
    ? `${label} <strong>${b.won}-${b.n - b.won} (${pct(b)})</strong>` : null;
  const bits = [
    part("sides where it claimed 5-10 points of edge have gone", mid),
    part("sides where it claimed 10 or more have gone", wide),
    part("and every pick priced at +150 or longer has gone", long),
  ].filter(Boolean);
  el_.innerHTML = `Kept for evaluation, not to bet. The model's picks are graded `
    + `in public here, and this league's own record is why they carry no stake: `
    + `${bits.join(", ")}`
    + (long.n
        ? ` — the long ones while the model forecast ${fmtPct(long.model)} and the `
          + `market said ${fmtPct(long.market)}. <em>The price was closer.</em>`
        : ".")
    + ` A big gap between our number and the price is evidence the model is `
    + `wrong, not evidence of a bet.`;
}

/**
 * What to say in a league where no structural bet exists at all.
 *
 * The NFL has a teaser shortlist, so its own section carries the verdict. In
 * college there is nothing to list, and the honest version of that is a
 * heading that says so with the measurements under it -- not an empty space
 * above a board of longshots, which would read as the picks being the answer.
 */
function renderNoStructure() {
  const section = $("#nostructure");
  const lg = state.board.leagues[state.league] || {};
  const hasTeasers = ((lg.teasers || {}).legs || []).length >= 2;
  if (hasTeasers) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  $("#nostructure-note").textContent =
    `${state.league} · nothing here has beaten its own backtest`;
  const body = $("#nostructure-body");
  body.innerHTML = "";
  body.appendChild(el("div", "empty",
    `<strong>Keep your money in your pocket</strong>`
    + `The one structure that beat its test is an NFL bet, and it does not `
    + `transfer here: college margins land on 3 or 7 only <strong>17.8%</strong> `
    + `of the time against the NFL's 24.1%, so six teased points buy less. The `
    + `NFL's own qualifying windows, measured on 2,646 college games, win `
    + `<strong>70.9% ± 2.0</strong> per leg where -110 needs 72.4% — below the `
    + `bar, not above it.`
    + `<div class="empty-extra">Line shopping, the other candidate, is `
    + `<strong>3-17</strong> across its live ledger against the +5.8% it `
    + `claimed, on one logged closing price. Too early to call, and nothing to `
    + `bet on. The board below is kept as a record.</div>`));
}

function renderTips() {
  const ref = referenceMode();
  $("#tips-heading").firstChild.textContent =
    ref ? "Model board " : "Bets this week ";
  $("#tips-legend").hidden = ref;
  $("#tips-reference-note").hidden = !ref;
  if (ref) renderReferenceNote();
  const lg = state.board.leagues[state.league];
  const gamesById = new Map(lg.games.map((g) => [g.event_id, g]));
  const legs = flattenLegs(lg);

  const safeCfg = applyCertaintyTo(state.cfg);
  const safe = buildPicks(legs, safeCfg);

  // The second list is built from what the first did not take.
  //
  // Deduping whole bets was not enough. The safe list took Wake Forest at -148
  // as a single while the value list put Wake Forest in a parlay — two
  // different bets, so nothing was duplicated, and anyone placing both lists
  // would still be doubling down on one team. These are meant to be placeable
  // together, so the value list gets the same treatment selection already gives
  // itself: no reusing a team or a game.
  const usedTeams = new Set();
  const usedGames = new Set();
  for (const p of safe) {
    for (const l of p.legs) {
      usedTeams.add(l.team_id);
      usedTeams.add(l.oppId);
      usedGames.add(l.eventId);
    }
  }
  const free = legs.filter((l) => !usedTeams.has(l.team_id) && !usedGames.has(l.eventId));
  const value = buildPicks(free, state.cfg);

  renderPickList($("#tips-safe"), safe, safeCfg, legs, gamesById, ref);
  renderPickList($("#tips-value"), value, state.cfg, legs, gamesById, ref);

  const all = [...safe, ...value];
  const totalUnits = all.reduce((t, p) => t + p.stakeUnits, 0);
  $("#tips-note").textContent = ref
    ? "what the model would have bet · not recommended · graded below"
    : all.length
      ? `${all.length} bet${all.length > 1 ? "s" : ""} · ${+totalUnits.toFixed(1)}U total`
      : "nothing this week";
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
         + `${fmtOdds(cfg.minCombined)} on its own this week, so legs are stacked to `
         + `reach it. Stacking costs — a parlay's expectation is the product of its `
         + `legs', so two legs roughly double the house's cut on the same opinion.`;
  }
  if (parlays === 0) {
    // Say what actually happened: singles filled the list. Naming the payout
    // floor here was wrong on the safe list, where the floor is -400 and every
    // single clears it trivially — the reason was never the floor.
    let why = `All singles this week —${picks.length} cleared on their own, and `
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

function renderPickList(box, picks, cfg, legs, gamesById, reference = false) {
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
          + `${cfg.minSample}+ games of sample. Nothing here is worth a bet this week `
          + `— keep your money in your pocket.`;
    }
    if (staleCut && cfg.excludeStale) {
      why += `<div class="empty-extra">${staleCut} side${staleCut > 1 ? "s were" : " was"} `
           + `excluded outright: a quarterback was ruled out after the ratings were `
           + `built, so the model has no informed opinion on either side of `
           + `${staleCut > 2 ? "those games" : "that game"}.</div>`;
    }
    box.appendChild(el("div", "empty", `<strong>No bets this week</strong>${why}`));
    return;
  }

  // Selection ranks by likelihood; the page lists by when you have to place
  // them. Sorting here and not in buildPicks keeps the parity harness's order.
  picks = byKickoff(picks);
  let day = null;

  picks.forEach((pick, i) => {
    const pickDay = dayLabel(pick.legs[0].kickoff);
    if (pickDay !== day) {
      box.appendChild(el("div", "tip-day", pickDay));
      day = pickDay;
    }
    const card = el("div", reference ? "tip reference" : "tip");
    const head = el("div", "tip-head");
    // No trust badge: the gate now refuses anything past ten points from the
    // price, so every pick that reaches this page is in the same band and the
    // badge was the same word every time. The gap itself is still shown, as the
    // two probabilities and their difference.
    head.innerHTML = `
      <div class="tip-title"><span class="tip-rank">#${i + 1}</span>${
        // The stake comes off the label in reference mode rather than being
        // greyed out: a number in units is an instruction however it is styled.
        reference ? pick.label.replace(/^[\d.]+U · /, "") : pick.label}</div>
      <div class="tip-meta">
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
        <td class="num">${sampleBadge(l.sample)}</td>`;
      body.appendChild(tr);
    }
    table.appendChild(body);
    card.appendChild(table);

    const strip = flagStrip(pick.legs, gamesById);
    if (strip) card.appendChild(strip);

    const foot = el("div", "tip-foot");
    // The checkbox stays in reference mode — tagging one is how it gets graded,
    // and the record is the point of keeping the list — but it is offered with
    // no suggested stake attached.
    foot.appendChild(placementControls(
      betFromLegs(state.league, pick.legs, pick.combined, pick.winProb,
                  pick.marketProb, pick.combinedEdgePP),
      reference ? 0 : pick.stakeUnits
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
  const price = Number(state.teaserPrice) || -110;
  const p = chosen.reduce((a, l) => a * l.prob, 1);
  const dec = americanToDecimal(price);
  const ev = p * (dec - 1) - (1 - p);
  const need = teaserMaxPrice(chosen.map((l) => l.prob - (t.per_leg_stderr || 0)));

  wrap.appendChild(el("div", "tt-head",
    `<strong>${t.points}-point teaser, ${chosen.length} legs</strong> · `
    + chosen.map((l) => `${l.team_abbr} ${fmtLine(l.teased)}`).join(" + ")
    + ` · ticket wins ${fmtPct(p)}`));

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

/** teaser.max_price, for the pair on screen. Mirrors the Python. */
function teaserMaxPrice(probs) {
  const p = probs.reduce((a, b) => a * b, 1);
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
  const t = (state.board.leagues[state.league] || {}).teasers;
  const section = $("#teaser-section");
  const legs = (t && t.legs) || [];
  // Two legs or it is not a teaser. One qualifying number is not a bet.
  if (legs.length < 2) {
    section.hidden = true;
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
    ? `${legs.length} qualifying legs · pick two or three · ${t.points}-point teaser`
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
      // Up to three, oldest out first. Three legs is a real bet at six points
      // — the same bands, multiplied once more — and it needs a much longer
      // price, which the ticket works out from whatever is selected.
      if (!state.teaserPicks.includes(l.event_id)) picks.push(l.event_id);
      state.teaserPicks = picks.slice(-3);
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

function renderBoard() {
  const lg = state.board.leagues[state.league];
  const gamesById = new Map(lg.games.map((g) => [g.event_id, g]));
  let legs = flattenLegs(lg);
  if (state.onlyEV) legs = legs.filter((l) => l.edge_pp > 0);
  legs.sort((a, b) => b.edge_pp - a.edge_pp);

  const body = $("#board-table tbody");
  body.innerHTML = "";
  if (!legs.length) {
    body.innerHTML = `<tr><td colspan="11" class="dim" style="padding:20px;text-align:center">
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
      <td class="num">${sampleBadge(l.sample)}</td>
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
  renderNoStructure();
  renderTeasers();
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
