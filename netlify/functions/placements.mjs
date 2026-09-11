/*
 * The write path the board never had.
 *
 * Everything else here is static: the board is generated hourly by GitHub
 * Actions, committed, and served as files. That is the right shape for
 * something read-only and the wrong shape the moment you want to record a
 * decision -- tagging a bet as placed has to survive a page reload, and it has
 * to be the same answer on the phone as on the laptop, which rules out
 * localStorage.
 *
 * So placements live in a site-wide blob store, one key per tagged bet, and
 * this endpoint is the only way in or out. The hourly build reads the same
 * store, settles what has finished against final scores, and writes the results
 * back -- which is why the graded fields are accepted from a caller holding the
 * key but never invented here.
 *
 *   GET  /api/placements   -> { placements: [...], configured: bool }
 *   POST /api/placements   -> upsert one bet (stake > 0) or untag it (stake <= 0)
 *
 * A BET IS A LIST OF LEGS, always, even when there is one of them. Most of what
 * this board recommends is a parlay, and a parlay is not its legs added up: it
 * pays once if every leg lands and nothing otherwise. Recording legs separately
 * would report five bets that mostly lost where there was one bet that lost,
 * and would price the win at the sum of the legs rather than their product.
 *
 * Auth is a single shared passphrase in PLACEMENT_KEY, sent as X-Placement-Key.
 * Deliberately modest: the board is public and has no accounts, the data behind
 * this endpoint is "which bets did Sean place", and the worst a leaked
 * passphrase buys is junk rows in a personal betting ledger. It guards no money
 * and should not pretend to.
 */

import { getStore } from "@netlify/blobs";

/** Football seasons are labelled by the year they start: Jan-Jul is last
 *  season. Same rule as espn.current_season_year, so the two agree on which
 *  prefix a September row belongs under. */
const seasonPrefix = () => {
  const now = new Date();
  const y = now.getUTCFullYear();
  return `${now.getUTCMonth() + 1 >= 8 ? y : y - 1}/`;
};

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

const LEG_FIELDS = [
  "event_id", "side", "team_id", "team_abbr", "team_name", "opp_abbr",
  "matchup", "kickoff", "odds", "model_prob", "implied_prob", "edge_pp",
];
const BET_FIELDS = [
  "league", "kind", "odds", "stake", "model_prob", "implied_prob", "edge_pp",
  "note",
];
/** Only the grader sets these, and it proves itself with the same key. */
const GRADED_FIELDS = ["status", "units", "graded_at", "result", "leg_results"];

function constantTimeEquals(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const clean = (v) => String(v ?? "").replace(/[^A-Za-z0-9_.-]/g, "");

/**
 * Deterministic id, so re-tagging the same bet edits the stake instead of
 * logging a second wager. Legs are sorted, because "A + B" and "B + A" are the
 * same ticket and must not become two rows.
 */
function betId(league, legs) {
  const parts = legs
    .map((l) => `${clean(l.event_id)}.${clean(l.side)}`)
    .filter((p) => p.length > 1)
    .sort();
  if (!parts.length || parts.length !== legs.length) return null;
  return `${clean(league)}-${parts.join("+")}`;
}

function normalise(body) {
  const legsIn = Array.isArray(body.legs) ? body.legs : [];
  if (!legsIn.length || legsIn.length > 8) return null;
  const legs = legsIn.map((l) => {
    const out = {};
    for (const f of LEG_FIELDS) if (l[f] !== undefined) out[f] = l[f];
    return out;
  });
  if (legs.some((l) => !l.event_id || !["home", "away"].includes(l.side))) return null;

  const rec = { legs };
  for (const f of BET_FIELDS) if (body[f] !== undefined) rec[f] = body[f];
  rec.kind = legs.length > 1 ? "parlay" : "single";
  // Earliest kickoff: a parlay is live from its first leg and is not gradeable
  // until its last, so both ends matter and the early one sorts the list.
  const kicks = legs.map((l) => l.kickoff).filter(Boolean).sort();
  rec.kickoff = kicks[0] || null;
  rec.last_kickoff = kicks[kicks.length - 1] || null;
  return rec;
}

async function readAll(store) {
  const { blobs } = await store.list({ prefix: seasonPrefix() });
  const rows = await Promise.all(
    blobs.map((b) => store.get(b.key, { type: "json" }).catch(() => null))
  );
  return rows
    .filter(Boolean)
    .sort((a, b) => String(a.kickoff || "").localeCompare(String(b.kickoff || "")));
}

export default async (req) => {
  const expected = process.env.PLACEMENT_KEY || "";
  // Strong consistency, not the default.
  //
  // Eventual reads serve a cached index, and a checkbox has to be checked the
  // instant after it was ticked -- with the default, a fresh placement POSTed
  // successfully and then vanished from the next GET, which reads as the tag
  // not having saved. Costs a little latency on a page that loads a few dozen
  // rows once. Worth it.
  const store = getStore({ name: "placements", consistency: "strong" });

  if (req.method === "GET") {
    // Readable without the key: it is the owner's own board, and the tags have
    // to render before there is anywhere to type a passphrase. Writing is what
    // needs proving.
    try {
      return json({ configured: Boolean(expected), placements: await readAll(store) });
    } catch (e) {
      return json({ configured: Boolean(expected), placements: [], error: String(e) });
    }
  }
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  if (!expected) {
    return json({
      error: "PLACEMENT_KEY is not set on this site, so bets cannot be recorded. "
           + "Set it under Site configuration > Environment variables, then redeploy.",
    }, 503);
  }
  if (!constantTimeEquals(req.headers.get("x-placement-key") || "", expected)) {
    return json({ error: "wrong passphrase" }, 401);
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "expected a JSON body" }, 400);
  }

  const incoming = normalise(body);
  if (!incoming) {
    return json({ error: "need 1-8 legs, each with an event_id and side home|away" }, 400);
  }
  const id = betId(incoming.league, incoming.legs);
  if (!id) return json({ error: "league and every leg's event_id are required" }, 400);

  const key = seasonPrefix() + id;
  const existing = (await store.get(key, { type: "json" }).catch(() => null)) || {};

  const stake = Number(body.stake);
  if (!Number.isFinite(stake) || stake <= 0) {
    // Unchecking the box. A settled bet is history and stays: removing it would
    // quietly improve the record, which is the one thing a ledger must not do.
    if (existing.status && existing.status !== "open") {
      return json(
        { error: "that bet has settled — it stays in the record", placement: existing },
        409
      );
    }
    await store.delete(key).catch(() => {});
    return json({ removed: id });
  }

  const rec = { ...existing, ...incoming, id, stake };
  for (const f of GRADED_FIELDS) if (body[f] !== undefined) rec[f] = body[f];
  rec.status = rec.status || "open";
  rec.placed_at = existing.placed_at || new Date().toISOString();
  rec.updated_at = new Date().toISOString();

  await store.setJSON(key, rec);
  return json({ placement: rec });
};

export const config = { path: "/api/placements" };
