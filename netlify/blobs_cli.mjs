/*
 * Read and write the placements store from outside Netlify.
 *
 * Inside a function the blob store configures itself from the runtime. From a
 * GitHub Actions runner it does not, so this passes siteID and a personal
 * access token explicitly -- the documented way to reach a site-wide store from
 * anywhere Node runs.
 *
 * It exists because the grading logic is Python (it reads ESPN box scores
 * through the same parser the rest of the project uses) and the blobs client is
 * JavaScript. Rather than reimplement either, this is the seam between them:
 * NDJSON on stdout, NDJSON on stdin.
 *
 *   node netlify/blobs_cli.mjs list          > placements.ndjson
 *   node netlify/blobs_cli.mjs put < graded.ndjson
 *
 * Needs NETLIFY_AUTH_TOKEN and NETLIFY_SITE_ID in the environment.
 */

import { getStore } from "@netlify/blobs";

const seasonPrefix = () => {
  const now = new Date();
  const y = now.getUTCFullYear();
  return `${now.getUTCMonth() + 1 >= 8 ? y : y - 1}/`;
};

function store() {
  const siteID = process.env.NETLIFY_SITE_ID;
  const token = process.env.NETLIFY_AUTH_TOKEN;
  if (!siteID || !token) {
    console.error("NETLIFY_SITE_ID and NETLIFY_AUTH_TOKEN are both required");
    process.exit(2);
  }
  return getStore({ name: "placements", siteID, token, consistency: "strong" });
}

async function list() {
  const s = store();
  const { blobs } = await s.list({ prefix: seasonPrefix() });
  for (const b of blobs) {
    const rec = await s.get(b.key, { type: "json" }).catch(() => null);
    if (rec) process.stdout.write(JSON.stringify(rec) + "\n");
  }
}

async function put() {
  const s = store();
  const text = await new Response(process.stdin).text();
  let n = 0;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    if (!rec.id) continue;
    await s.setJSON(seasonPrefix() + rec.id, rec);
    n++;
  }
  console.error(`[blobs] wrote ${n}`);
}

const cmd = process.argv[2];
if (cmd === "list") await list();
else if (cmd === "put") await put();
else {
  console.error("usage: blobs_cli.mjs list|put");
  process.exit(2);
}
