"""
Vertical video for TikTok, built from the board rather than typed by hand.

    python reel.py                      # NFL, +300 target, frames + mp4
    python reel.py --league NCAAF --target 200
    python reel.py --frames-only        # no ffmpeg needed
    python reel.py --narrate            # macOS `say` voiceover

WHY IT IS GENERATED AND NOT EDITED. Every number on screen comes from
public/data/board.json and history.json, the same files the site reads, so a
clip cannot claim a record the ledger does not show or a ticket the board does
not have. That matters more here than anywhere else in this project: a losing
week posted honestly is the whole premise, and it is very easy to type a
better number into a video than into a ledger.

THE INTRO IS FIXED, DELIBERATELY. Scene one is a constant -- same words, same
timing, every single clip -- because a channel is recognised before it is
read. Everything after it changes with the board.

Pipeline: an HTML scene per beat, rendered by the Chrome already on this
machine at 1080x1920, stitched by ffmpeg. No API, no cloud, no upload -- the
file lands in video/out/ and posting stays a human decision.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "public", "data")
# ready/ is the upload queue and uploaded/ is the archive the user moves
# files into by hand. A clip already sitting in either is never regenerated:
# the point of two folders is that "what do I still owe TikTok" is answered by
# looking, not by remembering.
READY = os.path.join(ROOT, "video", "ready")
UPLOADED = os.path.join(ROOT, "video", "uploaded")
OUT = READY
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

W, H = 1080, 1920

# The brand line. Fixed on purpose -- see the module docstring. If this ever
# changes, every clip after it looks like a different channel, so it is a
# constant rather than an argument.
#
# "Finder", not "predictor", and the distinction is the project's whole
# finding: every prediction model here has been graded against closing lines
# and lost, while the teaser windows work precisely BECAUSE they do not
# predict anything -- they price a shape in the results the ladder ignores. A
# channel called a predictor would be promising the one thing the data says
# cannot be done.
INTRO_LINE = "Join me on my journey to building the best parlay finder"
INTRO_SUB = "Every ticket measured. Every result posted. Win or lose."
OUTRO_LINE = "The record updates whether I win or lose"
OUTRO_SUB = "Nobody posts their losses. That is the point."

# Where the clip sends people. A raw Netlify subdomain is unusable in a video
# -- nobody types "statuesque-brioche-a8fa92" from memory -- so this is a
# constant to change the day a domain exists, and the call to action leans on
# "no sign-up" rather than on the address, because that part is true today and
# the address is not memorable.
SITE_URL = "statuesque-brioche-a8fa92.netlify.app"
CTA_LINE = "Build your own"
CTA_SUB = ("Every qualifying leg, every leg count, and the price each one "
           "needs. No sign-up, no email, nothing to buy.")


def _read(name: str, default):
    try:
        with open(os.path.join(DATA, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;background:#0d1117;color:#e6edf3;
 font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
 -webkit-font-smoothing:antialiased;overflow:hidden}
.wrap{width:100%;height:100%;padding:130px 90px 330px;display:flex;flex-direction:column}
/* Bottom 330px stays empty: TikTok lays its caption, handle and music over
   that strip, and a number hidden behind the UI is worse than no number. */
.mark{font:800 44px/1 ui-monospace,Menlo,monospace;background:#4c8dff;color:#07101f;
 padding:16px 20px;border-radius:16px;align-self:flex-start;letter-spacing:-.02em}
.kicker{font:700 30px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.14em;
 text-transform:uppercase;color:#9aa7b5;margin-bottom:28px}
h1{font-size:96px;line-height:1.08;letter-spacing:-.03em;font-weight:800}
h2{font-size:70px;line-height:1.12;letter-spacing:-.02em;font-weight:750}
.sub{font-size:40px;line-height:1.35;color:#9aa7b5;margin-top:36px}
.spacer{flex:1}
.big{font:800 190px/1 ui-monospace,Menlo,monospace;letter-spacing:-.05em}
.pos{color:#3fb950}.neg{color:#f0616d}.dim{color:#6b7889}.accent{color:#4c8dff}
.row{display:flex;gap:26px;margin-top:40px}
.card{flex:1;background:#151b23;border:2px solid #262f3b;border-radius:22px;padding:32px 30px}
.card .lab{font:700 24px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
 text-transform:uppercase;color:#6b7889}
.card .val{font:800 62px/1.05 ui-monospace,Menlo,monospace;margin-top:16px;
 letter-spacing:-.04em;white-space:nowrap}
.card .note{font-size:26px;color:#6b7889;margin-top:12px}
.leg{display:flex;justify-content:space-between;align-items:baseline;
 background:#151b23;border:2px solid #262f3b;border-left:8px solid #4c8dff;
 border-radius:18px;padding:26px 30px;margin-top:20px}
.leg .team{font:800 52px/1 ui-monospace,Menlo,monospace;letter-spacing:-.02em}
.leg .from{font-size:28px;color:#6b7889;margin-top:10px}
.leg .game{font-size:28px;color:#9aa7b5;text-align:right}
.foot{font-size:32px;color:#6b7889;margin-top:44px;line-height:1.4}
.tag{margin-top:auto;font:700 28px/1.3 ui-monospace,Menlo,monospace;color:#6b7889;
 letter-spacing:.06em;border-top:2px solid #262f3b;padding-top:26px}
.banner{background:#12251a;border:2px solid #235c31;border-radius:22px;
 padding:34px 32px;margin-top:40px;font-size:38px;line-height:1.35}
.warn{background:#241f12;border-color:#5c4a23}
"""


def scene_html(body: str) -> str:
    return f"<!doctype html><meta charset='utf-8'><style>{CSS}</style><div class='wrap'>{body}</div>"


def intro_scene() -> str:
    return scene_html(
        f"<div class='mark'>+200</div>"
        f"<div class='spacer'></div>"
        f"<h1>{html.escape(INTRO_LINE)}</h1>"
        f"<div class='sub'>{html.escape(INTRO_SUB)}</div>"
        f"<div class='spacer'></div>"
    )


def record_scene(history: Dict) -> str:
    s = history.get("summary") or {}
    won, lost = int(s.get("won") or 0), int(s.get("lost") or 0)
    roi = float(s.get("roi_pct") or 0.0)
    units = float(s.get("units") or 0.0)
    clv = float(s.get("avg_clv_pp") or 0.0)
    settled = won + lost
    tone = "pos" if roi > 0 else "neg"
    return scene_html(
        "<div class='kicker'>Where the record stands</div>"
        f"<div class='big {tone}'>{roi:+.0f}%</div>"
        f"<div class='sub'>return over {settled} settled picks</div>"
        "<div class='row'>"
        f"<div class='card'><div class='lab'>Record</div>"
        f"<div class='val'>{won}-{lost}</div><div class='note'>flat 1u a pick</div></div>"
        f"<div class='card'><div class='lab'>Units</div>"
        f"<div class='val {tone}'>{units:+.1f}</div><div class='note'>since day one</div></div>"
        f"<div class='card'><div class='lab'>CLV</div>"
        f"<div class='val'>{clv:+.1f}</div><div class='note'>points vs close</div></div>"
        "</div>"
        f"<div class='spacer'></div>"
        f"<div class='foot'>The model's own picks lost money. That is why the "
        f"board stopped recommending them &mdash; and why what comes next is "
        f"not a prediction.</div>"
    )


def record_line(history: Dict) -> str:
    """
    The standing record, short enough to sit under a ticket.

    On every data scene on purpose: a clip that shows only the bet is a tout,
    and the one thing this channel has that a tout does not is the losing half
    of the ledger printed next to the pick.
    """
    s = history.get("summary") or {}
    won, lost = int(s.get("won") or 0), int(s.get("lost") or 0)
    roi = float(s.get("roi_pct") or 0.0)
    if not (won + lost):
        return "NO SETTLED RECORD YET"
    return f"RECORD SO FAR  {won}-{lost}  ({roi:+.0f}% RETURN)"


def all_routes(board: Dict, league: str) -> List[Dict]:
    """
    Every teaser ticket the board can actually fill, richest edge first.

    One clip per ticket rather than one clip listing them: eight routes on a
    card is a spreadsheet, and the leg counts are genuinely different bets --
    the 6-leg hits one week in six for a third more edge, the 4-leg nearly a
    third of the time. Those deserve separate posts, not separate lines.
    """
    lg = (board.get("leagues") or {}).get(league) or {}
    teasers = lg.get("teasers") or {}
    out: List[Dict] = []
    for pts, block in (teasers.get("ladder") or {}).items():
        pool = ((teasers.get("ten") if pts == "10" else teasers) or {}).get("legs") or []
        for n_str, price in (block.get("prices") or {}).items():
            n = int(n_str)
            p = (block.get("joint") or {}).get(n_str)
            if p is None or len(pool) < n:
                continue
            dec = 1 + (price / 100.0 if price > 0 else 100.0 / -price)
            out.append({"points": int(pts), "legs": n, "price": int(price),
                        "prob": float(p), "ev": p * (dec - 1) - (1 - p),
                        "needs": 1 / dec, "fill": pool[:n]})
    return sorted(out, key=lambda r: -r["ev"])


def slug(league: str, route: Dict, built: str) -> str:
    """A filename that says what the ticket is, so a folder of them is legible."""
    return (f"{league.lower()}-{route['points']}pt-{route['legs']}leg"
            f"-{route['price']:+d}-{built[:10]}")


def best_route(board: Dict, league: str, target: int) -> Optional[Dict]:
    """
    The route the site would lead with: measured teaser first, by chance.

    Deliberately narrow. The video shows the one ticket, because a clip that
    lists six alternatives is a spreadsheet nobody watches to the end.
    """
    lg = (board.get("leagues") or {}).get(league) or {}
    teasers = lg.get("teasers") or {}
    ladder = teasers.get("ladder") or {}
    want = 1 + (target / 100.0 if target > 0 else 100.0 / -target)
    best = None
    for pts, block in ladder.items():
        prices = block.get("prices") or {}
        joint = block.get("joint") or {}
        pool = (teasers.get("ten") if pts == "10" else teasers).get("legs") or []
        for n_str, price in prices.items():
            n = int(n_str)
            p = joint.get(n_str)
            dec = 1 + (price / 100.0 if price > 0 else 100.0 / -price)
            if p is None or dec < want * 0.88 or len(pool) < n:
                continue
            ev = p * (dec - 1) - (1 - p)
            cand = {"points": int(pts), "legs": n, "price": int(price),
                    "prob": float(p), "ev": ev, "fill": pool[:n],
                    "needs": 1 / dec}
            if best is None or cand["prob"] > best["prob"]:
                best = cand
    return best


def ticket_scene(route: Dict, league: str, history: Dict) -> str:
    legs = "".join(
        f"<div class='leg'><div><div class='team'>{html.escape(l['team_abbr'])} "
        f"{l['teased']:+g}</div><div class='from'>from {l['spread']:+g}</div></div>"
        f"<div class='game'>{html.escape(l.get('matchup') or '')}</div></div>"
        for l in route["fill"]
    )
    price = f"{route['price']:+d}"
    return scene_html(
        f"<div class='kicker'>This week &middot; {html.escape(league)}</div>"
        f"<h2>{route['legs']}-leg, {route['points']}-point teaser at {price}</h2>"
        f"<div class='sub'>Wins <span class='accent'>{route['prob']*100:.1f}%</span> "
        f"of the time. The price needs {route['needs']*100:.1f}%.</div>"
        f"{legs}"
        f"<div class='tag'>{html.escape(record_line(history))}</div>"
    )


def why_scene(route: Dict, board: Dict, league: str) -> str:
    t = ((board.get("leagues") or {}).get(league) or {}).get("teasers") or {}
    rate = float(t.get("per_leg_rate") or 0) * 100
    return scene_html(
        "<div class='kicker'>Why this one</div>"
        f"<h2>Six points moved across 3 and 7 is worth more than six points "
        f"anywhere else.</h2>"
        f"<div class='sub'>24% of NFL games end on a margin of exactly 3 or 7. "
        f"Legs starting in those windows have won <span class='accent'>"
        f"{rate:.1f}%</span> of the time across 1,804 of them &mdash; measured "
        f"from finished games, not guessed from a model.</div>"
        f"<div class='banner'>The ladder only asks for "
        f"{route['needs']**(1/route['legs'])*100:.1f}% a leg at {route['legs']} "
        f"legs. That gap is the entire bet.</div>"
        f"<div class='tag'>NOT ADVICE &middot; MEASURED, NOT PREDICTED</div>"
    )


def outro_scene() -> str:
    return scene_html(
        "<div class='mark'>+200</div>"
        "<div class='spacer'></div>"
        f"<h1>{html.escape(OUTRO_LINE)}</h1>"
        f"<div class='sub'>{html.escape(OUTRO_SUB)}</div>"
        "<div class='banner warn'>Not advice. A model under evaluation, posted "
        "in public so it can be judged.</div>"
        "<div class='spacer'></div>"
    )


def cta_scene(league: str, board: Dict) -> str:
    """
    The one scene that asks for something.

    It asks for a visit, not a subscription, because there is nothing to
    subscribe to -- and saying "no sign-up" is both the truth and the better
    hook. Naming what the site actually does beats naming the site, given the
    address is a Netlify subdomain nobody will retype.
    """
    t = ((board.get("leagues") or {}).get(league) or {}).get("teasers") or {}
    six = len(t.get("legs") or [])
    ten = len((t.get("ten") or {}).get("legs") or [])
    return scene_html(
        "<div class='mark'>+200</div>"
        f"<div class='spacer'></div>"
        f"<h1>{html.escape(CTA_LINE)}</h1>"
        f"<div class='sub'>{html.escape(CTA_SUB)}</div>"
        f"<div class='banner'>{html.escape(SITE_URL)}</div>"
        f"<div class='sub'>{six} qualifying legs at 6 points and {ten} at 10 on "
        f"the {html.escape(league)} board today &mdash; name a payout and it "
        f"builds the ticket.</div>"
        f"<div class='spacer'></div>"
    )


def shoot(html_text: str, png: str) -> None:
    if not os.path.exists(CHROME):
        raise SystemExit(f"Chrome not found at {CHROME}")
    tmp = png.replace(".png", ".html")
    with open(tmp, "w") as fh:
        fh.write(html_text)
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--window-size={W},{H}",
         f"--screenshot={png}", "--virtual-time-budget=2000", f"file://{tmp}"],
        check=True, capture_output=True, timeout=120,
    )
    os.remove(tmp)


VOICE = "Samantha"          # the only intelligible en_US voice on this machine


def audio_seconds(path: str) -> float:
    """How long a rendered clip actually is, asked of ffprobe not guessed."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def narrate_scenes(lines: List[str], out_dir: str, rate: int = 180):
    """
    One audio file per scene, so a scene can be held for exactly as long as
    its own line takes to read.

    Narrating the whole script in one pass and hoping the visuals line up is
    how the first cut ended up three seconds longer than its pictures, with
    the voice still talking over a finished clip. Timing the scene to the
    sentence is the fix, and it means editing a line changes the pacing by
    itself.
    """
    if not shutil.which("say") or not shutil.which("ffprobe"):
        return []
    made = []
    for i, line in enumerate(lines, start=1):
        path = os.path.join(out_dir, f"voice-{i:02d}.aiff")
        subprocess.run(["say", "-v", VOICE, "-r", str(rate), "-o", path, line],
                       check=True, capture_output=True, timeout=180)
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return []
        made.append((path, audio_seconds(path)))
    return made


def join_audio(parts: List[tuple], out: str) -> Optional[str]:
    """Concatenate the per-scene narration into one track."""
    if not parts or not shutil.which("ffmpeg"):
        return None
    listing = out + ".txt"
    with open(listing, "w") as fh:
        for path, _ in parts:
            fh.write(f"file '{path}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing,
                    "-c:a", "pcm_s16le", out],
                   check=True, capture_output=True, timeout=300)
    os.remove(listing)
    return out


def narrate(lines: List[str], path: str, rate: int = 180) -> Optional[str]:
    """
    macOS `say` to an AIFF. Free, local, and sounds like a Mac.

    Named voice rather than the system default, which varies by machine and
    would change the channel's sound without anyone touching this file. 180
    words a minute is a shade quicker than the default 175 and lands the
    script inside the scene timings.
    """
    if not shutil.which("say"):
        return None
    # No --data-format: on this machine `say` answers "Opening output file
    # failed: fmt?" and writes a zero-byte file while still exiting 0, so the
    # failure only shows up as silence in the finished clip. The default AIFF
    # is fine -- ffmpeg re-encodes it to AAC anyway.
    subprocess.run(["say", "-v", VOICE, "-r", str(rate), "-o", path,
                    " ".join(lines)],
                   check=True, capture_output=True, timeout=180)
    # Exit 0 is not proof: check the file actually has audio in it.
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        print("  narration produced no audio — carrying on without it")
        return None
    return path


def stitch(frames: List[tuple], mp4: str, audio: Optional[str]) -> bool:
    """
    Frames to an MP4, each held for its own beat.

    A concat demuxer rather than a filter graph: the scenes are stills, so
    there is nothing to interpolate, and the simple path is the one that keeps
    working when ffmpeg changes under it.
    """
    if not shutil.which("ffmpeg"):
        return False
    listing = mp4.replace(".mp4", ".txt")
    with open(listing, "w") as fh:
        for png, secs in frames:
            fh.write(f"file '{png}'\nduration {secs}\n")
        fh.write(f"file '{frames[-1][0]}'\n")     # last frame needs repeating
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing]
    if audio:
        cmd += ["-i", audio, "-shortest"]
    cmd += ["-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
            "-crf", "20"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [mp4]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    os.remove(listing)
    return True


def render(route: Dict, league: str, board: Dict, history: Dict, out_dir: str,
           narrate_it: bool, frames_only: bool) -> Optional[str]:
    """One clip for one ticket. Frames land beside it in a working folder."""
    built = board.get("generated_at") or ""
    name = slug(league, route, built)
    work = os.path.join(out_dir, "frames", name)
    os.makedirs(work, exist_ok=True)

    scenes = [
        ("01-intro", intro_scene(), 3.0, f"{INTRO_LINE}. {INTRO_SUB}"),
        ("02-record", record_scene(history), 4.5,
         "Here is where the record stands. The model's own picks lost money, "
         "which is why the board stopped recommending them."),
        ("03-ticket", ticket_scene(route, league, history), 6.0,
         f"This week: a {route['legs']} leg {route['points']} point teaser at "
         f"{route['price']}, which wins {route['prob']*100:.0f} percent of the "
         f"time. The price only needs {route['needs']*100:.0f}."),
        ("04-why", why_scene(route, board, league), 5.5,
         "Six points moved across three and seven is worth more than six points "
         "anywhere else. That gap is the entire bet."),
        ("05-cta", cta_scene(league, board), 4.5,
         f"{CTA_LINE}. {CTA_SUB}"),
        ("06-outro", outro_scene(), 3.0, f"{OUTRO_LINE}. {OUTRO_SUB}"),
    ]

    frames = []
    for scene_name, markup, secs, _ in scenes:
        png = os.path.join(work, f"{scene_name}.png")
        shoot(markup, png)
        frames.append((png, secs))

    audio = None
    if narrate_it:
        parts = narrate_scenes([sc[3] for sc in scenes], work)
        if parts:
            frames = [(png, max(base, secs + 0.6))
                      for (png, base), (_, secs) in zip(frames, parts)]
            audio = join_audio(parts, os.path.join(work, "voice.wav"))

    if frames_only:
        print(f"  {name}: {len(frames)} frames (no mp4)")
        return None
    mp4 = os.path.join(out_dir, f"{name}.mp4")
    if not stitch(frames, mp4, audio):
        print("  ffmpeg missing — frames only")
        return None
    total = sum(sec for _, sec in frames)
    print(f"  {name}.mp4  {total:.0f}s  ev {route['ev']*100:+.1f}%")
    return mp4


def already_done(name: str) -> Optional[str]:
    """Has this exact ticket already been rendered, or posted?"""
    for folder, label in ((READY, "ready"), (UPLOADED, "uploaded")):
        if os.path.exists(os.path.join(folder, f"{name}.mp4")):
            return label
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=("NFL", "NCAAF"))
    ap.add_argument("--target", type=int, default=300,
                    help="single-clip mode: the payout to build for")
    ap.add_argument("--all", action="store_true",
                    help="one clip per fillable ticket, richest edge first")
    ap.add_argument("--out", default=READY)
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--narrate", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-render a ticket already in ready/ or uploaded/")
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="skip tickets whose edge is below this (default: %(default)s)")
    a = ap.parse_args()

    board, history = _read("board.json", {}), _read("history.json", {})
    if not board:
        raise SystemExit("no board.json — run build_board.py first")
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(UPLOADED, exist_ok=True)

    if a.all:
        routes = all_routes(board, a.league)
        # A losing ticket does not get a video. The 2-leg 6-pointer is the one
        # count the ladder prices above its measured rate, and posting it would
        # be promoting the single bet this project refuses to make.
        routes = [r for r in routes if r["ev"] > a.min_ev]
    else:
        one = best_route(board, a.league, a.target)
        if not one:
            raise SystemExit(f"no teaser route to {a.target:+d} on the {a.league} board")
        routes = [one]

    if not routes:
        print("Nothing on this board clears the edge floor — no clips to make.")
        return 0

    built = board.get("generated_at") or ""
    made, skipped = 0, 0
    print(f"{len(routes)} ticket(s) from the {built[:16]} board\n")
    for route in routes:
        name = slug(a.league, route, built)
        where = None if a.force else already_done(name)
        if where:
            print(f"  {name}: already {where} — skipping")
            skipped += 1
            continue
        if render(route, a.league, board, history, a.out, a.narrate, a.frames_only):
            made += 1

    print(f"\n{made} clip(s) in {a.out}")
    if skipped:
        print(f"{skipped} skipped (already rendered or posted)")
    print(f"Move each one to {UPLOADED} once it is up, and it will not come back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
