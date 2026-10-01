// Starts the fault watch every five minutes, from Netlify rather than GitHub.
//
// fault-watch.yml asks GitHub for "*/5 * * * *" and got 4-5 runs a day
// (measured 2026-09-28..10-01): GitHub treats scheduled workflows as
// best-effort and drops most of them. Stale lines stand about an hour, so a
// scan every five hours missed most of them. Netlify's scheduler keeps time,
// and workflow_dispatch runs immediately.
//
// GITHUB_DISPATCH_TOKEN is a fine-grained token scoped to this one repo with
// Actions: read and write, nothing else. It is a Netlify env var, never here.

const REPO = "Kosmo87/football-200-picks";
const WORKFLOW = "fault-watch.yml";

export default async () => {
  const token = (process.env.GITHUB_DISPATCH_TOKEN || "").trim();
  if (!token) {
    console.log("GITHUB_DISPATCH_TOKEN not set; fault watch not triggered");
    return new Response("no token", { status: 500 });
  }
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "football-200-picks-fault-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  // 204 is success. Anything else is logged with GitHub's reason, which is
  // where an expired or under-scoped token shows up.
  const detail = r.status === 204 ? "" : ` ${(await r.text()).slice(0, 200)}`;
  console.log(`dispatch ${WORKFLOW}: HTTP ${r.status}${detail}`);
  return new Response(null, { status: r.status === 204 ? 200 : 502 });
};

export const config = { schedule: "*/5 * * * *" };
