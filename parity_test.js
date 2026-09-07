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
new Function("exports", engine + "\nObject.assign(exports, {flattenLegs, buildPicks, DEFAULTS, LEAGUE_MIN_SAMPLE, computeConfidence, combineOdds});")(sandbox);

const board = JSON.parse(fs.readFileSync("public/data/board.json", "utf8"));
const out = {};
for (const [name, lg] of Object.entries(board.leagues)) {
  const cfg = { ...sandbox.DEFAULTS, minSample: sandbox.LEAGUE_MIN_SAMPLE[name] ?? 3 };
  const picks = sandbox.buildPicks(sandbox.flattenLegs(lg), cfg, 3);
  out[name] = picks.map((p) => ({
    combined: p.combined,
    // Fixed-width strings: JS prints 5 where Python prints 5.0, and averaging
    // order can differ in the last float bit. One decimal is the real tolerance.
    conf: p.avgConfidence.toFixed(1),
    edge: p.combinedEdgePP.toFixed(1),
    legs: p.legs.map((l) => `${l.team_abbr}@${l.odds}`),
  }));
}
console.log(JSON.stringify(out, null, 2));
