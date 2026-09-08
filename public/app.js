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

/** Published stake: the ladder, capped by the price, shrunk by disagreement. */
const stakeUnits = (p, odds, marketP) =>
  Math.round(
    Math.min(ladderUnits(p), kellyUnits(p, odds)) * disagreementFactor(p, marketP) * 10
  ) / 10;

/** A parlay lands only if every leg does. Independence is why legs never share a game. */
const parlayWinProb = (probs) => probs.reduce((a, b) => a * b, 1);

const unitsLabel = (u) => (u <= 0 ? "no bet" : `${+u.toFixed(1)}U`);

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
        // A pick we would stake nothing on is not a pick. Leave its teams free
        // so they can still appear in a combination.
        if (pick.stakeUnits <= 0) continue;
        picks.push(pick);
        markUsed(leg);
      }
      continue;
    }

    for (const cand of candidateCombos(available, nLegs, cfg.minCombined, POOL_CAPS[nLegs] || 25)) {
      if (picks.length >= n) break;
      if (cand.legs.some((l) => !canUse(l))) continue;
      const pick = makePick(cand.legs, cand.combined);
      if (pick.stakeUnits <= 0) continue;
      picks.push(pick);
      cand.legs.forEach(markUsed);
    }
  }

  picks.sort((a, b) => b.avgConfidence - a.avgConfidence || b.combinedEdgePP - a.combinedEdgePP);
  return picks.slice(0, n);
}

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------

const state = {
  board: null,
  history: null,
  league: null,
  cfg: { ...DEFAULTS },
  onlyEV: true,
  preset: false,
  // Sliders the user has actually moved. Untouched ones follow the per-league
  // baseline when the tab changes (NCAAF wants a deeper sample than the NFL).
  touched: new Set(),
};

const SLIDER_SPECS = [
  { key: "minEdgePP", label: "Min edge", min: 0, max: 25, step: 0.5,
    fmt: (v) => `${v.toFixed(1)}pp`, hint: "model win% over implied" },
  { key: "minSample", label: "Min sample", min: 0, max: 12, step: 0.5,
    fmt: (v) => `${v} gm`, hint: "incl. prior-season credit" },
  { key: "maxLegOdds", label: "Max leg price", min: 150, max: 1000, step: 25,
    fmt: (v) => fmtOdds(v), hint: "longest single leg allowed" },
  { key: "maxParlayLegs", label: "Max legs", min: 1, max: 5, step: 1,
    fmt: (v) => `${v}`, hint: "1 = singles only" },
  { key: "minCombined", label: "Min combined", min: 100, max: 600, step: 25,
    fmt: (v) => fmtOdds(v), hint: "target payout floor" },
];

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
      applyLeagueDefaults();
      render();
    };
    nav.appendChild(b);
  }
}

function renderSliders() {
  const box = $("#sliders");
  if (box.childElementCount) {
    // Already built — just sync values (preset/reset may have changed them)
    for (const spec of SLIDER_SPECS) {
      const input = box.querySelector(`input[data-key="${spec.key}"]`);
      input.value = state.cfg[spec.key];
      box.querySelector(`[data-val="${spec.key}"]`).textContent = spec.fmt(state.cfg[spec.key]);
    }
    return;
  }
  for (const spec of SLIDER_SPECS) {
    const wrap = el("div", "slider");
    wrap.innerHTML = `
      <label>${spec.label}<span class="val" data-val="${spec.key}">${spec.fmt(state.cfg[spec.key])}</span></label>
      <input type="range" data-key="${spec.key}" min="${spec.min}" max="${spec.max}"
             step="${spec.step}" value="${state.cfg[spec.key]}">
      <div class="hint">${spec.hint}</div>`;
    wrap.querySelector("input").addEventListener("input", (e) => {
      state.cfg[spec.key] = parseFloat(e.target.value);
      state.touched.add(spec.key);
      wrap.querySelector(".val").textContent = spec.fmt(state.cfg[spec.key]);
      renderTips();
      renderBoard();
      renderGateSummary();
    });
    box.appendChild(wrap);
  }
}

function renderGateSummary() {
  const lg = state.board.leagues[state.league];
  const legs = flattenLegs(lg);
  const gated = legs.filter((l) => passesGates(l, state.cfg));
  const plusEV = legs.filter((l) => l.edge_pp > 0);
  const m = lg.meta;
  $("#gate-summary").textContent =
    `${m.upcoming_games} games · ${legs.length} priced sides · ${plusEV.length} +EV · ` +
    `${gated.length} pass gates · ${m.rated_teams} teams rated from ` +
    `${m.completed_games} finals` +
    (m.carryover ? ` + ${m.prior_games_used} prior-season games` : " (no carryover)");
}

function renderTips() {
  const lg = state.board.leagues[state.league];
  const legs = flattenLegs(lg);
  const picks = buildPicks(legs, state.cfg, 3);
  const box = $("#tips");
  box.innerHTML = "";

  const gated = legs.filter((l) => passesGates(l, state.cfg)).length;
  $("#tips-note").textContent = picks.length
    ? `${picks.length} independent · from ${gated} qualifying sides`
    : "";

  if (!picks.length) {
    const why = gated
      ? `${gated} sides pass the gates, but none combine to ${fmtOdds(state.cfg.minCombined)} without overlapping teams. Raise max legs or lower the combined floor.`
      : `No sides clear ${state.cfg.minEdgePP.toFixed(1)}pp edge at ${state.cfg.minSample} games of sample. Loosen the gates to see marginal plays.`;
    box.appendChild(el("div", "empty", `<strong>No qualifying tips</strong>${why}`));
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

    if (pick.legs.length > 1) {
      const foot = el("div", "tip-head");
      foot.innerHTML = `<span class="dim">${pick.legs.length} legs · no shared teams or games</span>
        <span class="dim">1u returns ${pick.payout.toFixed(2)}u</span>`;
      card.appendChild(foot);
    }
    box.appendChild(card);
  });
}

function renderBoard() {
  const lg = state.board.leagues[state.league];
  let legs = flattenLegs(lg);
  if (state.onlyEV) legs = legs.filter((l) => l.edge_pp > 0);
  legs.sort((a, b) => b.edge_pp - a.edge_pp);

  const body = $("#board-table tbody");
  body.innerHTML = "";
  if (!legs.length) {
    body.innerHTML = `<tr><td colspan="10" class="dim" style="padding:20px;text-align:center">
      No priced sides to show.</td></tr>`;
    return;
  }
  for (const l of legs) {
    const passes = passesGates(l, state.cfg);
    const tr = el("tr");
    tr.innerHTML = `
      <td class="${passes ? "team-cell" : "dim"}">${l.matchup}${l.neutral ? ' <span class="dim">N</span>' : ""}</td>
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
  renderSliders();
  renderGateSummary();
  renderTips();
  renderBoard();
  renderRatings();
  renderHistory();
}

// --------------------------------------------------------------------------
// Presets + boot
// --------------------------------------------------------------------------

/** Re-apply league baselines to any gate the user has not set themselves. */
function applyLeagueDefaults() {
  if (state.preset) {
    applyPresetValues();
    return;
  }
  if (!state.touched.has("minSample")) {
    state.cfg.minSample = LEAGUE_MIN_SAMPLE[state.league] ?? DEFAULTS.minSample;
  }
}

function applyPresetValues() {
  // Mirrors ConfidenceConfig.apply_high_confidence_preset
  state.cfg.minEdgePP = Math.max(state.cfg.minEdgePP, 7.5);
  state.cfg.minSample = Math.max(state.cfg.minSample, state.league === "NFL" ? 5 : 6);
  state.cfg.maxLegOdds = Math.min(state.cfg.maxLegOdds, 350);
  state.cfg.maxParlayLegs = 5;
}

function applyPreset(on) {
  state.preset = on;
  if (on) {
    applyPresetValues();
  } else {
    resetCfg();
  }
  render();
}

function resetCfg() {
  state.cfg = {
    ...DEFAULTS,
    minSample: LEAGUE_MIN_SAMPLE[state.league] ?? DEFAULTS.minSample,
  };
  state.touched.clear();
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
  const leagues = Object.keys(board.leagues);
  if (!leagues.length) {
    $("#tips").appendChild(el("div", "empty",
      "<strong>No leagues in this build</strong>The last run produced no data."));
    return;
  }
  state.league = leagues[0];
  resetCfg();

  const gen = new Date(board.generated_at);
  $("#updated").textContent = `updated ${gen.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
  $("#season").textContent = `${board.season} season`;

  $("#preset-high").addEventListener("change", (e) => applyPreset(e.target.checked));
  $("#reset").addEventListener("click", () => {
    $("#preset-high").checked = false;
    state.preset = false;
    resetCfg();
    render();
  });
  $("#only-ev").addEventListener("change", (e) => {
    state.onlyEV = e.target.checked;
    renderBoard();
  });

  renderRecord();
  render();
}

boot();
