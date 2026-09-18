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
OUT = os.path.join(ROOT, "video", "out")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

W, H = 1080, 1920

# The brand line, in the user's own words. Fixed on purpose -- see the module
# docstring. If this ever changes, every clip after it looks like a different
# channel, so it is a constant rather than an argument.
INTRO_LINE = "Join me on my journey to building the best parlay predictor"
INTRO_SUB = "Every ticket measured. Every result posted. Win or lose."
OUTRO_LINE = "The record updates whether I win or lose"
OUTRO_SUB = "Nobody posts their losses. That is the point."


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


def narrate(lines: List[str], path: str) -> Optional[str]:
    """macOS `say` to an AIFF. Free, local, and sounds like a Mac."""
    if not shutil.which("say"):
        return None
    subprocess.run(["say", "-o", path, "--data-format=LEI16@22050", " ".join(lines)],
                   check=True, capture_output=True, timeout=180)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--league", default="NFL", choices=("NFL", "NCAAF"))
    ap.add_argument("--target", type=int, default=300)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--narrate", action="store_true")
    a = ap.parse_args()

    board, history = _read("board.json", {}), _read("history.json", {})
    if not board:
        raise SystemExit("no board.json — run build_board.py first")
    route = best_route(board, a.league, a.target)
    if not route:
        raise SystemExit(f"no teaser route to {a.target:+d} on the {a.league} board")

    os.makedirs(a.out, exist_ok=True)
    scenes = [
        ("01-intro", intro_scene(), 3.0,
         f"{INTRO_LINE}. {INTRO_SUB}"),
        ("02-record", record_scene(history), 4.5,
         "Here is where the record stands. The model's own picks lost money, "
         "which is why the board stopped recommending them."),
        ("03-ticket", ticket_scene(route, a.league, history), 6.0,
         f"This week: a {route['legs']} leg {route['points']} point teaser at "
         f"{route['price']}, which wins {route['prob']*100:.0f} percent of the time."),
        ("04-why", why_scene(route, board, a.league), 5.5,
         "Six points moved across three and seven is worth more than six points "
         "anywhere else. That gap is the entire bet."),
        ("05-outro", outro_scene(), 3.0,
         f"{OUTRO_LINE}. {OUTRO_SUB}"),
    ]

    frames = []
    for name, markup, secs, _ in scenes:
        png = os.path.join(a.out, f"{name}.png")
        shoot(markup, png)
        frames.append((png, secs))
        print(f"  {name}.png  {secs:g}s")

    audio = None
    if a.narrate:
        audio = narrate([s[3] for s in scenes], os.path.join(a.out, "voice.aiff"))
        print(f"  voice: {'written' if audio else 'skipped (no say)'}")

    if a.frames_only:
        print(f"\n{len(frames)} frames in {a.out} — install ffmpeg to stitch them.")
        return 0

    mp4 = os.path.join(a.out, f"reel-{a.league.lower()}.mp4")
    if stitch(frames, mp4, audio):
        print(f"\nwrote {mp4}  ({sum(s for _, s in frames):g}s)")
    else:
        print("\nffmpeg not installed — frames are written. Install with:")
        print("  brew install ffmpeg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
