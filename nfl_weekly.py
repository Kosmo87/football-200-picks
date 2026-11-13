"""
The weekly NFL pick list: every game's winner by nfl_model, emailed Thursday
morning, then graded and emailed again Tuesday.

WHY a frozen list. A pick that can be rewritten after kickoff has no record.
Thursday's list is written to data/archive/nfl_weekly.ndjson the first time it
is made and never touched again; Tuesday grades exactly those rows against
nflverse's final scores. The market's favorite rides beside each pick as the
scorecard (nfl_model never reads it), so the record can be read against the
number it has to beat.

THE REASON is the model's own arithmetic, not a story: each input's push on
the pick (weight x standardized value), the biggest one or two in words, and
the largest one pulling the other way when it is big enough to matter.

    python nfl_weekly.py --picks              # this week's list, printed
    python nfl_weekly.py --results            # last picked week, graded
    python nfl_weekly.py --auto --send        # what CI runs: Thu picks, Tue results
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "data", "archive", "nfl_weekly.ndjson")
SENT = os.path.join(ROOT, "data", "archive", "nfl_weekly_sent.json")
# A factor pulling against the pick is named when it is at least this big, in
# log-odds (0.15 is about 4 points of win chance near a coin flip).
AGAINST_MIN = 0.15
# Tuesday's last scheduled run sends whatever is final rather than wait a week.
RESULTS_LAST_HOUR_UTC = 18


def _et_today(now: datetime) -> date:
    import kalshi_trade
    return kalshi_trade._et(now).date()


SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _qb_last(name) -> str:
    """Surname for the push: "Michael Penix Jr." -> "Penix"."""
    words = [w for w in str(name).split() if w.lower() not in SUFFIXES] if isinstance(name, str) else []
    return words[-1] if words else "?"


def reasons(r, b, mu, sd) -> str:
    """One line: why the model likes r.pick, from its own weights."""
    from nfl_model import PICK_INPUTS
    s = 1.0 if r.pick == r.home else -1.0
    opp = r.away if s > 0 else r.home
    pick_qb, opp_qb = (r.home_qb, r.away_qb) if s > 0 else (r.away_qb, r.home_qb)
    push = {"home": b[0] * r.homefld * s}
    for j, c in enumerate(PICK_INPUTS, start=1):
        v = getattr(r, c)
        push[c] = 0.0 if v != v else b[j] * (v - mu[c]) / sd[c] * s

    def words(c: str, favours: bool) -> str:
        if c == "home":
            return "home field" if favours else "being on the road"
        if c == "margin":
            return f"{abs(r.margin):.1f} pts/game {'better' if favours else 'worse'} recent margin"
        if c == "qb":
            d = r.qb * s
            a, z = (pick_qb, opp_qb) if favours else (opp_qb, pick_qb)
            return f"{_qb_last(a)} over {_qb_last(z)} at QB ({abs(d):.2f} EPA/dropback)"
        side = "offensive" if c == "inj_off" else "defensive"
        # inj_* is home snaps lost minus away snaps lost: positive hurts home.
        who = opp if favours else r.pick
        return f"{who} missing {abs(getattr(r, c)):.1f} starters' worth of {side} snaps"

    pro = sorted((c for c in push if push[c] > 0.02), key=lambda c: -push[c])[:2]
    con = min(push, key=push.get)
    out = "; ".join(words(c, True) for c in pro) or "a coin flip on every input"
    if push[con] <= -AGAINST_MIN:
        out += f" (despite {words(con, False)})"
    return out


def build(F, season: int, week: int, now: datetime) -> List[dict]:
    import nfl_model as M
    U = M.picks(F, week, season)
    b, mu, sd = M.fit(F)
    rows = []
    for r in U.itertuples():
        mkt = r.market_pct
        rows.append({"season": season, "week": week, "game_id": r.game_id,
                     "gameday": r.gameday, "away": r.away, "home": r.home,
                     "pick": r.pick, "win_pct": round(float(r.win_pct), 3),
                     "market_pct": None if mkt != mkt else round(float(mkt), 3),
                     "reason": reasons(r, b, mu, sd), "made_at": now.isoformat()})
    return rows


def upcoming_week(F, today: date) -> Optional[tuple]:
    """(season, week) of the unplayed games in the next seven days."""
    d = F[F.result.isna()].copy()
    d = d[(d.gameday >= today.isoformat()) &
          (d.gameday <= (today + timedelta(days=6)).isoformat())]
    if d.empty:
        return None
    first = d.sort_values("gameday").iloc[0]
    return int(first.season), int(first.week)


def load_log(path: Optional[str] = None) -> List[dict]:
    path = path or LOG
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def _sent(path: Optional[str] = None) -> Dict[str, str]:
    path = path or SENT
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def _mark(key: str, now: datetime, path: Optional[str] = None) -> None:
    path = path or SENT
    s = _sent(path)
    s[key] = now.isoformat()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(s, open(path, "w"), indent=1, sort_keys=True)


def picks_text(rows: List[dict]) -> str:
    w = rows[0]["week"]
    lines = [f"NFL PICKS Week {w}"]
    for r in sorted(rows, key=lambda r: (r["gameday"], r["game_id"])):
        mkt = r["market_pct"]
        if mkt is None:
            vs = "mkt n/a"
        elif mkt < 0.5:
            vs = f"mkt {mkt*100:.0f}%, underdog"
        else:
            vs = f"mkt {mkt*100:.0f}%"
        lines.append(f"{r['away']} @ {r['home']}: {r['pick']} {r['win_pct']*100:.0f}% ({vs}) "
                     f"- {r['reason']}")
    lines.append("Metrics only (margin, QB, injuries, home). Injury reports as of Thursday.")
    return "\n".join(lines)


def grade(rows: List[dict], games) -> List[dict]:
    """Each pick with its final score, or result None while unplayed. `games` is nflverse games.csv."""
    by = {r.game_id: r for r in games.itertuples()}
    out = []
    for p in rows:
        g = by.get(p["game_id"])
        res = None if g is None or g.result != g.result else float(g.result)
        x = dict(p, result=res)
        if res is not None:
            x["home_score"], x["away_score"] = int(g.home_score), int(g.away_score)
            winner = None if res == 0 else (p["home"] if res > 0 else p["away"])
            x["won"] = None if winner is None else winner == p["pick"]
            mkt = p["market_pct"]
            if mkt is not None and mkt != 0.5 and winner is not None:
                fav = p["pick"] if mkt > 0.5 else (p["away"] if p["pick"] == p["home"] else p["home"])
                x["fav_won"] = fav == winner
        out.append(x)
    return out


def _wl(g: List[dict], key: str = "won") -> str:
    w = sum(1 for x in g if x.get(key) is True)
    l = sum(1 for x in g if x.get(key) is False)
    t = sum(1 for x in g if x.get("result") == 0)
    return f"{w}-{l}" + (f"-{t}" if t and key == "won" else "")


def results_text(week_rows: List[dict], season_rows: List[dict]) -> str:
    w = week_rows[0]["week"]
    done = [x for x in week_rows if x["result"] is not None]
    lines = [f"NFL RESULTS Week {w}: {_wl(done)}"]
    for x in sorted(week_rows, key=lambda r: (r["gameday"], r["game_id"])):
        if x["result"] is None:
            lines.append(f"... {x['pick']} ({x['away']} @ {x['home']}) not final yet")
            continue
        mark = "TIE" if x["won"] is None else ("W" if x["won"] else "L")
        lines.append(f"{mark}  {x['pick']} {x['win_pct']*100:.0f}%   "
                     f"{x['away']} {x['away_score']} @ {x['home']} {x['home_score']}")
    fav = [x for x in done if "fav_won" in x]
    if fav:
        lines.append(f"Market favorites: {_wl(fav, 'fav_won')}")
    dis = [x for x in done if x.get("market_pct") is not None and x["market_pct"] < 0.5]
    if dis:
        lines.append(f"Picked the underdog: {_wl(dis)}")
    sdone = [x for x in season_rows if x["result"] is not None]
    sfav = [x for x in sdone if "fav_won" in x]
    lines.append(f"Season: {_wl(sdone)} (market favorites {_wl(sfav, 'fav_won')})")
    return "\n".join(lines)


def do_picks(F, now: datetime, send: bool, force: bool = False) -> int:
    wk = upcoming_week(F, _et_today(now))
    if not wk:
        print("no NFL games in the next week")
        return 0
    key = f"picks|{wk[0]}|{wk[1]}"
    log = load_log()
    rows = [r for r in log if (r["season"], r["week"]) == wk]
    if not rows:
        rows = build(F, wk[0], wk[1], now)
        if send:      # frozen the first time it is sent, never rewritten
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
    text = picks_text(rows)
    print(text)
    if send and (force or key not in _sent()):
        if _push(text):
            _mark(key, now)
    return 0


def do_results(F, now: datetime, send: bool, force: bool = False) -> int:
    log = load_log()
    sent = _sent()
    weeks = sorted({(r["season"], r["week"]) for r in log})
    todo = [w for w in weeks if force or f"results|{w[0]}|{w[1]}" not in sent]
    today = _et_today(now).isoformat()
    todo = [w for w in todo if all(r["gameday"] < today for r in log if (r["season"], r["week"]) == w)]
    if not todo:
        print("no picked week waiting on results")
        return 0
    wk = todo[-1]
    import nfl_model as M
    graded = grade(log, M.load_games(refresh=False))
    week_rows = [x for x in graded if (x["season"], x["week"]) == wk]
    season_rows = [x for x in graded if x["season"] == wk[0]]
    text = results_text(week_rows, season_rows)
    print(text)
    pending = any(x["result"] is None for x in week_rows)
    if pending and now.hour < RESULTS_LAST_HOUR_UTC and not force:
        print("waiting for the last scores")
        return 0
    if send and _push(text):
        _mark(f"results|{wk[0]}|{wk[1]}", now)
    return 0


def email_html(text: str) -> str:
    """The push text as a readable email: title as a heading, one game per line."""
    from html import escape
    title, *lines = text.split("\n")
    body = []
    for ln in lines:
        head, sep, why = ln.partition(" - ")
        if sep:
            body.append(f"<p style='margin:0 0 10px'><b>{escape(head)}</b><br>"
                        f"<span style='color:#555'>{escape(why)}</span></p>")
        else:
            body.append(f"<p style='margin:0 0 6px'>{escape(ln)}</p>")
    return (f"<div style='font-family:-apple-system,Helvetica,Arial,sans-serif;"
            f"font-size:15px;max-width:640px'><h2 style='margin:0 0 14px'>{escape(title)}</h2>"
            + "".join(body) + "</div>")


def _push(text: str) -> bool:
    """Email to DELIVER_TO (his choice, 2026-11-13: email, not ntfy)."""
    import delivery
    to = os.environ.get("DELIVER_TO", "").strip()
    if not to:
        print("  DELIVER_TO not set - not sent")
        return False
    return delivery.send_email(to, text.split("\n", 1)[0], email_html(text))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--picks", action="store_true")
    ap.add_argument("--results", action="store_true")
    ap.add_argument("--auto", action="store_true", help="Thursday: picks; Tuesday: results")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--force", action="store_true", help="send again even if already sent")
    a = ap.parse_args()
    import nfl_model as M
    now = datetime.now(timezone.utc)
    day = _et_today(now).weekday()
    do_p = a.picks or (a.auto and day == 3)
    do_r = a.results or (a.auto and day == 1)
    if not (do_p or do_r):
        print("nothing to do today")
        return 0
    F = M.load_all()
    if do_p:
        do_picks(F, now, a.send, a.force)
    if do_r:
        do_results(F, now, a.send, a.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
