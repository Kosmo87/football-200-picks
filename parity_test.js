/*
 * Parity harness: runs the browser selection engine (public/app.js) headlessly
 * against public/data/board.json and prints the picks it produces, so the same
 * board can be scored by parity_test.py and diffed.
 */
const fs = require("fs");

// Load only the engine half of app.js (everything above the State section).
const src = fs.readFileSync("public/app.js", "utf8");
const engine = src.split("// --------------------------------------------------------------------------\n// State")[0];
const sandbox = {};
new Function("exports", engine + "\nObject.assign(exports, {flattenLegs, buildPicks, DEFAULTS, LEAGUE_MIN_SAMPLE, computeConfidence, combineOdds, stakeUnits, applyCertaintyTo});")(sandbox);

const board = JSON.parse(fs.readFileSync("public/data/board.json", "utf8"));
const out = {};

// Both configurations, not just the defaults. The certainty preset reaches gates
// the default path never evaluates -- the pick-level win-probability floor and a
// negative combined-odds floor -- so testing only the defaults would leave the
// newest engine logic unchecked in exactly the place a port is most likely to
// drift.
const CONFIGS = {
  default: (cfg) => cfg,
  certainty: (cfg) => sandbox.applyCertaintyTo(cfg),
};

for (const [name, lg] of Object.entries(board.leagues)) {
 for (const [preset, apply] of Object.entries(CONFIGS)) {
  const cfg = apply({ ...sandbox.DEFAULTS, minSample: sandbox.LEAGUE_MIN_SAMPLE[name] ?? 3 });
  const picks = sandbox.buildPicks(sandbox.flattenLegs(lg), cfg);
  out[`${name}/${preset}`] = picks.map((p) => ({
    combined: p.combined,
    // Raw floats, compared by parity_check.py with a real tolerance.
    //
    // These used to be rounded strings, on the theory that "one decimal is the
    // real tolerance". Rounding is not a tolerance -- it is a cliff. board.json
    // stores edge_pp to two decimals, so about one value in thirty sits exactly
    // on a .x5 boundary, and the two engines fall off opposite sides of it:
    // toFixed rounds the stored double half away from zero, while Python
    // reaches the same number through a /100 then *100 round-trip that lands it
    // a few bits below. Same arithmetic, different last bit, whole-step
    // disagreement. Comparing the numbers themselves has no cliff to fall off.
    conf: p.avgConfidence,
    edge: p.combinedEdgePP,
    units: p.stakeUnits,
    winp: p.winProb,
    mktp: p.marketProb,
    trust: p.trust,
    legs: p.legs.map((l) => `${l.team_abbr}@${l.odds}`),
  }));
 }
}
console.log(JSON.stringify(out, null, 2));
