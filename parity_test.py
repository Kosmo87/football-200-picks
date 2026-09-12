"""Score the same board with the Python engine, in the JS harness's format."""
import json
from types import SimpleNamespace

from elo import EloSystem, config_for_league
from picks import ConfidenceConfig, Leg, build_picks

board = json.load(open("public/data/board.json"))
out = {}

for name, lg in board["leagues"].items():
    # Rebuild Game/Elo stubs from the serialized board so both engines see
    # byte-identical inputs (no refetch, no drift).
    elo = EloSystem(config=config_for_league(name))
    games, legs_by_game = [], {}
    for g in lg["games"]:
        home = SimpleNamespace(id=g["home"]["id"], name=g["home"]["name"],
                               abbreviation=g["home"]["abbr"])
        away = SimpleNamespace(id=g["away"]["id"], name=g["away"]["name"],
                               abbreviation=g["away"]["abbr"])
        game = SimpleNamespace(event_id=g["event_id"], name=g["name"],
                               short_name=g["short_name"], date=g["kickoff"],
                               status=g["status"], home=home, away=away,
                               neutral=g["neutral"])
        games.append(game)
        legs_by_game[g["event_id"]] = (game, g["legs"])

    all_legs = []
    for game, raw_legs in legs_by_game.values():
        for rl in raw_legs:
            all_legs.append(Leg(
                game=game, side=rl["side"], team_name=rl["team_name"],
                team_abbr=rl["team_abbr"], team_id=rl["team_id"],
                odds_american=rl["odds"], model_win_prob=rl["model_prob"],
                implied_prob=rl["implied_prob"], edge=rl["edge_pp"] / 100.0,
                fair_odds=rl["fair_odds"], sample_games=rl["sample"],
                team_sample=rl["team_sample"], opp_sample=rl["opp_sample"],
                # Carried through, not re-derived. The gate that reads it is the
                # one most likely to diverge between the two engines, because
                # it is the only one whose input is not a price.
                stale=bool(rl.get("stale")),
                stale_reason=rl.get("stale_reason", ""),
            ))

    from picks import compute_confidence
    for l in all_legs:
        l.confidence, l.confidence_label = compute_confidence(
            l.edge, l.odds_american, l.sample_games
        )

    import picks as picks_mod
    picks_mod.get_all_legs = lambda *a, **k: all_legs  # feed the identical board

    # Both configurations. The certainty preset reaches gates the default path
    # never evaluates -- the pick-level win-probability floor and a negative
    # combined-odds floor -- so testing only the defaults would leave the newest
    # engine logic unchecked exactly where a port is most likely to drift.
    def _base():
        return ConfidenceConfig(min_sample_games=lg["meta"]["default_min_sample"])

    for preset, cfg in (
        ("default", _base()),
        ("certainty", _base().apply_high_certainty_preset()),
    ):
     result = build_picks(games, elo, cfg=cfg)

     out[f"{name}/{preset}"] = [
        {
            "combined": p.combined_odds,
            # Raw floats; see the note in parity_test.js. Rounding here is what
            # made this comparison flaky rather than strict.
            "conf": p.avg_confidence,
            "edge": p.combined_edge_pp,
            "units": p.stake_units,
            "winp": p.win_prob,
            "mktp": p.market_prob,
            "trust": p.trust,
            "legs": [f"{l.team_abbr}@{l.odds_american}" for l in p.legs],
        }
        for p in result
     ]

print(json.dumps(out, indent=2))
