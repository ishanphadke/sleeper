from sleeper import lineup as LU, trades as TR

SLOTS = ["QB", "RB", "RB", "WR", "WR", "FLEX", "BN", "BN"]
POS = {"q1": "QB", "q2": "QB", "r1": "RB", "r2": "RB", "r3": "RB", "r4": "RB", "r5": "RB", "r6": "RB",
       "w1": "WR", "w2": "WR", "w3": "WR", "w4": "WR", "w5": "WR", "w6": "WR"}


def _weekly(avg: dict[str, float], weeks=(10, 11, 12)):
    return {w: dict(avg) for w in weeks}


def _side(name, players, limit=8):
    return {"name": name, "players": players, "reserve": set(), "limit": limit}


def test_finds_the_swap_that_fills_my_hole_and_helps_the_partner():
    # My WR2 (w2, 6) is a hole and r6 sits on my bench; their RBs are weak and w6 sits on theirs.
    # r3 for w5: my WR slot +8, my FLEX -1 (r6 steps in); their RB slot +6, their FLEX -1 (w6 steps in).
    avg = {"q1": 18, "q2": 18, "r1": 16, "r2": 15, "r3": 14, "r6": 13, "r4": 9, "r5": 8,
           "w1": 17, "w2": 6, "w3": 16, "w4": 15, "w5": 14, "w6": 13}
    me = _side("me", ["q1", "r1", "r2", "r3", "r6", "w1", "w2"])
    them = _side("them", ["q2", "r4", "r5", "w3", "w4", "w5", "w6"])
    weekly, fills = _weekly(avg), {w: {} for w in (10, 11, 12)}
    found = TR.find(me, [them], weekly, fills, {}, POS, SLOTS, [], limit=5)
    # Best 1-for-1, worked by hand: my bench r6 (13) for their w3 (16). Mine: WR 6 -> 16, +10.
    # Theirs: RB 8 -> 13 (+5), WRs 16+15 and FLEX 14 become 15+14 and FLEX 13 (-3), net +2.
    assert any(t["give"] == ("r6",) and t["get"] == ("w3",) and t["my_gain"] == 10.0 and t["their_gain"] == 2.0 for t in found)
    # Swapping two equal QBs on top of that adds nothing for either side, so it is not listed.
    assert not any(set(t["give"]) == {"q1", "r6"} and set(t["get"]) == {"q2", "w3"} for t in found)
    assert all(t["my_gain"] >= TR.MIN_GAIN and t["their_gain"] >= 0 for t in found)
    assert found == sorted(found, key=lambda t: -t["my_gain"])


def test_nothing_is_offered_when_the_partner_would_lose():
    avg = {"q1": 18, "q2": 18, "r1": 16, "r2": 15, "w1": 17, "w2": 6, "w3": 16, "w4": 15}
    me = _side("me", ["q1", "r1", "r2", "w1", "w2"])
    them = _side("them", ["q2", "w3", "w4"])                     # they have no RB to spare and need none of mine to win
    weekly, fills = _weekly(avg), {w: {} for w in (10, 11, 12)}
    found = TR.find(me, [them], weekly, fills, {}, POS, SLOTS, [], limit=5)
    assert all(t["their_gain"] >= 0 for t in found)


def test_a_bye_is_not_a_reason_to_trade():
    # w1 misses week 11 on bye; a free agent covers it, so no trade should be built around that week.
    avg = {"q1": 18, "q2": 18, "r1": 16, "r2": 15, "w1": 17, "w2": 12, "w3": 11, "w4": 10}
    weekly = _weekly(avg)
    del weekly[11]["w1"]
    fills = {10: {"WR": [9.0]}, 11: {"WR": [9.0]}, 12: {"WR": [9.0]}}
    me = _side("me", ["q1", "r1", "r2", "w1", "w2"])
    them = _side("them", ["q2", "w3", "w4"])
    found = TR.find(me, [them], weekly, fills, {"WR": [9.0]}, POS, SLOTS, [], limit=5)
    assert found == []


def test_after_trade_cuts_my_weakest_bench_player_when_the_roster_overflows():
    avg = {"q1": 18, "r1": 16, "r2": 15, "w1": 17, "w2": 12, "w3": 4, "w4": 11}
    roster, cut = TR.after_trade(["q1", "r1", "r2", "w1", "w2", "w3"], ("r2",), ("w4", "r5"), 6, {**avg, "r5": 10},
                                 POS, ["QB", "RB", "WR", "WR"], {}, set())
    assert cut == "w3" and "w3" not in roster and len(roster) == 6


def test_my_slots_measure_each_starter_against_the_best_free_agent():
    rows = TR.my_slots(["q1", "r1", "w1"], POS, {"q1": 18, "r1": 9, "w1": 17}, ["QB", "RB", "WR"], {"RB": [11.0], "WR": [8.0]})
    by = {r["slot"]: r for r in rows}
    assert by["RB"]["margin"] == -2.0 and by["WR"]["margin"] == 9.0 and by["QB"]["waiver"] == 0.0


def test_ros_avg_ignores_byes_and_players_missing_most_weeks():
    weekly = {10: {"a": 10, "b": 4}, 11: {"a": 14}, 12: {"a": 12}, 13: {"a": 12}}
    assert TR.ros_avg(weekly) == {"a": 12.0}
