"""Name matching across two sources. The negative cases are the point."""
from teamnames import same_team

ok = fail = 0
def t(name, got, want):
    global ok, fail
    if got == want: ok += 1; print(f"  ok   {name}")
    else: fail += 1; print(f"  FAIL {name}: got {got}, want {want}")

print("the real mismatches that lost bets")
t("App State abbreviates Appalachian State",
  same_team("Appalachian State Mountaineers", "App State Mountaineers"), True)
t("Sam Houston dropped its State",
  same_team("Sam Houston State Bearkats", "Sam Houston Bearkats"), True)
t("identical names still match",
  same_team("East Carolina Pirates", "East Carolina Pirates"), True)

print("\nthe matches that would be dangerous")
t("Ohio is not Ohio State", same_team("Ohio Bobcats", "Ohio State Buckeyes"), False)
t("the two Miamis stay apart", same_team("Miami Hurricanes", "Miami RedHawks"), False)
t("Texas is not Texas Tech",
  same_team("Texas Longhorns", "Texas Tech Red Raiders"), False)
t("Washington is not Washington State",
  same_team("Washington Huskies", "Washington State Cougars"), False)
t("Carolina panthers are not Carolina gamecocks",
  same_team("Carolina Panthers", "South Carolina Gamecocks"), False)
# A single-token name has only the place to go on, so it matches any team in
# that place. Accepted deliberately — the alternative is refusing to grade a
# source that abbreviates to "Miami" at all — and safe in practice because
# grading also requires the kickoff to be within six hours.
t("one token matches on the place alone", same_team("Miami", "Miami Hurricanes"), True)
t("and does so even across schools, which the kickoff check then filters",
  same_team("Ohio", "Ohio State Buckeyes"), True)

print("\nnoise words")
t("'University of' is ignored",
  same_team("University of Alabama Crimson Tide", "Alabama Crimson Tide"), True)
t("empty is never a match", same_team("", "Alabama Crimson Tide"), False)

print(f"\n  {ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
