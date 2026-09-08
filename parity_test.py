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
            ))

    from picks import compute_confidence
    for l in all_legs:
        l.confidence, l.confidence_label = compute_confidence(
            l.edge, l.odds_american, l.sample_games
        )

    cfg = ConfidenceConfig(min_sample_games=lg["meta"]["default_min_sample"])

    import picks as picks_mod
    picks_mod.get_all_legs = lambda *a, **k: all_legs  # feed the identical board
    result = build_picks(games, elo, n=3, cfg=cfg)

    out[name] = [
        {
            "combined": p.combined_odds,
            "conf": f"{p.avg_confidence:.1f}",
            "edge": f"{p.combined_edge_pp:.1f}",
            "units": f"{p.stake_units:.1f}",
            "winp": f"{p.win_prob:.4f}",
            "legs": [f"{l.team_abbr}@{l.odds_american}" for l in p.legs],
        }
        for p in result
    ]

print(json.dumps(out, indent=2))
