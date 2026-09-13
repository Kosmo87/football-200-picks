import sys
from datetime import datetime, timedelta, timezone
import parlays as P

def leg(book, game, edge, keep, days=0, hours=0):
    return {"book":book,"game":game,"side":game+" side","price":-110,
            "edge":edge,"keep":keep,"market":"spreads",
            "kickoff":datetime.now(timezone.utc)+timedelta(days=days,hours=hours)}

def test_one_leg_per_game():
    legs=[leg("dk","A @ B",.03,1.03),leg("dk","A @ B",.02,1.02),leg("dk","C @ D",.01,1.01)]
    out=P.build(legs)["dk"]["legs"]
    assert len(out)==2 and len({l["game"] for l in out})==2

def test_books_never_mix():
    legs=[leg("dk","A @ B",.03,1.03),leg("fd","C @ D",.02,1.02)]
    assert P.build(legs)=={}, "two books is two singles, not a parlay"

def test_same_slate_rule():
    legs=[leg("dk","A @ B",.03,1.03),leg("dk","C @ D",.02,1.02,days=6)]
    assert P.build(legs)=={}, "6 days apart must be refused"
    ok=[leg("dk","A @ B",.03,1.03),leg("dk","C @ D",.02,1.02,days=2)]
    assert "dk" in P.build(ok)

def test_edge_multiplies_upward():
    legs=[leg("dk","A @ B",.02,1.02),leg("dk","C @ D",.016,1.016),leg("dk","E @ F",.011,1.011)]
    p=P.build(legs)["dk"]
    assert p["ev"] > .02+.016+.011-.02, "combined edge should exceed any single leg"
    assert abs(p["keep"]-1.02*1.016*1.011) < 1e-9

def test_single_leg_is_not_a_parlay():
    assert P.build([leg("dk","A @ B",.03,1.03)])=={}

if __name__=="__main__":
    f=0
    for n,fn in sorted(globals().items()):
        if n.startswith("test_"):
            try: fn(); print(f"  ok   {n}")
            except AssertionError as e: f+=1; print(f"  FAIL {n}: {e}")
    print(f"\n{f} failure(s)"); raise SystemExit(1 if f else 0)
