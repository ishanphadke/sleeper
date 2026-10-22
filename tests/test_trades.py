from sleeper import lineup as LU, trades as TR

SLOTS = ["QB", "RB", "RB", "WR", "WR", "FLEX", "BN", "BN"]
POS = {"q1": "QB", "q2": "QB", "q3": "QB", "r1": "RB", "r2": "RB", "r3": "RB", "r4": "RB", "r5": "RB", "r6": "RB",
       "w1": "WR", "w2": "WR", "w3": "WR", "w4": "WR", "w5": "WR", "w6": "WR"}
FILL = {"QB": [15.0], "RB": [8.0], "WR": [8.0]}          # best healthy free agent per position


def _weekly(avg: dict[str, float], weeks=(10, 11, 12)):
    return {w: dict(avg) for w in weeks}


def _fills(weeks=(10, 11, 12)):
    return {w: dict(FILL) for w in weeks}


def _side(name, players, limit=8):
    return {"name": name, "players": players, "reserve": set(), "limit": limit}


def _find(me, them, avg, **kw):
    return TR.find(me, [them], _weekly(avg), _fills(), FILL, POS, SLOTS, [], limit=10, **kw)


MIRROR = {"q1": 18, "q2": 18, "r1": 16, "r2": 15, "r3": 14, "r6": 13, "r4": 9, "r5": 8,
          "w1": 17, "w2": 6, "w3": 16, "w4": 15, "w5": 14, "w6": 13}
ME = ["q1", "r1", "r2", "r3", "r6", "w1", "w2"]          # WR2 w2 (6) is two points under waivers: a hole
THEM = ["q2", "r4", "r5", "w3", "w4", "w5", "w6"]        # their RBs are weak, w6 sits on their bench


def test_finds_a_fair_swap_that_fills_my_hole_and_helps_the_partner():
    # Best fair 1-for-1, worked by hand: my bench r6 (13 RB, 5 over waivers) for their w5 (14 WR, 6 over).
    # Mine: WR 6 -> 14, +8. Theirs: RB 8 -> 13 (+5), FLEX 14 -> 13 when w6 steps in (-1): +4.
    found = _find(_side("me", ME), _side("them", THEM), MIRROR)
    hit = next(t for t in found if t["give"] == ("r6",) and t["get"] == ("w5",))
    assert (hit["my_gain"], hit["their_gain"]) == (8.0, 4.0)
    assert (hit["hole"], hit["filled_by"], hit["value_give"], hit["value_get"]) == ("w2", "w5", 5.0, 6.0)
    assert all(t["my_gain"] >= TR.MIN_GAIN and t["their_gain"] >= 0 and t["hole"] == "w2" for t in found)


def test_a_lopsided_trade_is_rejected_even_when_both_lineups_improve():
    # r6 (5 over waivers) for w3 (8 over waivers) helps both lineups, but no manager gives 8 for 5.
    found = _find(_side("me", ME), _side("them", THEM), MIRROR)
    assert not any(t["give"] == ("r6",) and t["get"] == ("w3",) for t in found)
    ok, g, r = TR.fair(("r6",), ("w3",), MIRROR, POS, FILL)
    assert (ok, g, r) == (False, 5.0, 8.0)
    assert TR.fair(("r3",), ("w5",), MIRROR, POS, FILL)[0]


def test_a_trade_that_does_not_touch_a_hole_is_not_offered():
    # My only hole is QB (18 vs a 15 free agent). Their QB is equal, so swapping QBs on top of a
    # WR-for-RB deal is a lateral move dressed up as a fix and must not count.
    avg = {**MIRROR, "w2": 14}
    me = ["q1", "r1", "r2", "r3", "r6", "w1", "w2"]
    assert _find(_side("me", me), _side("them", THEM), avg) == []


def test_a_backup_qb_gets_no_credit_for_hindsight_start_sit():
    # Two QBs who trade the better week: picking the max each week credits the backup; a held lineup does not.
    weekly = {10: {"q1": 20, "q2": 14}, 11: {"q1": 16, "q2": 22}, 12: {"q1": 20, "q2": 14}}
    pos = {"q1": "QB", "q2": "QB"}
    avg = LU.ros_avg(weekly)
    held = LU.strength(["q1", "q2"], pos, weekly, ["QB"], [], None, avg, {})
    hindsight = LU.strength(["q1", "q2"], pos, weekly, ["QB"], [])
    assert held["by_week"] == {10: 20, 11: 16, 12: 20}       # q1 starts every week
    assert hindsight["by_week"] == {10: 20, 11: 22, 12: 20}
    assert LU.strength(["q1"], pos, weekly, ["QB"], [], None, LU.ros_avg({w: {"q1": v["q1"]} for w, v in weekly.items()}), {})["score"] == held["score"]


def test_a_held_starter_on_bye_is_covered_by_the_better_of_bench_and_waivers():
    pos = {"w1": "WR", "w2": "WR"}
    weekly = {10: {"w1": 15, "w2": 7}, 11: {"w2": 7}}      # w1 on bye in week 11
    avg = LU.ros_avg(weekly)
    st = LU.strength(["w1", "w2"], pos, weekly, ["WR"], [], {10: {"WR": [9.0]}, 11: {"WR": [9.0]}}, avg, {"WR": [9.0]})
    assert st["by_week"] == {10: 15, 11: 9.0}               # waiver 9 beats bench w2 at 7
    st = LU.strength(["w1", "w2"], pos, weekly, ["WR"], [], {10: {"WR": [6.0]}, 11: {"WR": [6.0]}}, avg, {"WR": [6.0]})
    assert st["by_week"] == {10: 15, 11: 7}                 # bench w2 beats a 6-point waiver


def test_a_bye_is_not_a_reason_to_trade():
    avg = {"q1": 18, "q2": 18, "r1": 16, "r2": 15, "w1": 17, "w2": 14, "w3": 11, "w4": 10}
    weekly = _weekly(avg)
    del weekly[11]["w1"]
    me, them = _side("me", ["q1", "r1", "r2", "w1", "w2"]), _side("them", ["q2", "w3", "w4"])
    assert TR.find(me, [them], weekly, _fills(), FILL, POS, SLOTS, [], limit=5) == []


def test_after_trade_cuts_my_weakest_bench_player_when_the_roster_overflows():
    avg = {"q1": 18, "r1": 16, "r2": 15, "w1": 17, "w2": 12, "w3": 4, "w4": 11, "r5": 10}
    roster, cut = TR.after_trade(["q1", "r1", "r2", "w1", "w2", "w3"], ("r2",), ("w4", "r5"), 6, avg,
                                 POS, ["QB", "RB", "WR", "WR"], {}, set())
    assert cut == "w3" and "w3" not in roster and len(roster) == 6


def test_holes_skip_kicker_and_defense_and_count_waiver_fill_ins():
    lineup = [("QB", "q1", 16.0), ("WR", "w1", 17.0), ("WR", "FA", 8.0), ("K", "k1", 7.0)]
    got = TR.holes(lineup, {"QB": [15.0], "WR": [8.0], "K": [8.0]})
    assert got == {"q1": ("QB", 1.0), "FA:WR": ("WR", 0.0)}


def test_my_slots_measure_each_starter_against_the_best_free_agent():
    rows = TR.my_slots(["q1", "r1", "w1"], POS, {"q1": 18, "r1": 9, "w1": 17}, ["QB", "RB", "WR"], {"RB": [11.0], "WR": [8.0]})
    by = {r["slot"]: r for r in rows}
    assert by["RB"]["margin"] == -2.0 and by["WR"]["margin"] == 9.0 and by["QB"]["waiver"] == 0.0


def test_ros_avg_ignores_byes_and_players_missing_most_weeks():
    weekly = {10: {"a": 10, "b": 4}, 11: {"a": 14}, 12: {"a": 12}, 13: {"a": 12}}
    assert TR.ros_avg(weekly) == {"a": 12.0}


def test_need_and_hold_steer_the_search():
    # In MIRROR my hole is WR. The best fair fix is a bench RB for a WR (r6 for w5).
    me, them = _side("me", ME), _side("them", THEM)
    found = _find(me, them, MIRROR, need="WR")
    assert found and all(POS[t["filled_by"]] == "WR" for t in found)
    assert _find(me, them, MIRROR, need="QB") == []                       # nothing upgrades my QB
    # Trading a starting RB (r1 or r2) out of the RB slots breaks --hold RB; bench r6 and flex r3 do not.
    held = _find(me, them, MIRROR, hold=frozenset({"RB"}))
    assert held and not any({"r1", "r2"} & set(t["give"]) for t in held)


def test_by_slot_keeps_flex_separate_from_the_position():
    lineup = [("RB", "r1", 16.0), ("RB", "r2", 15.0), ("FLEX", "r3", 14.0), ("WR", "w1", 17.0)]
    assert TR.by_slot(lineup) == {"RB": 31.0, "FLEX": 14.0, "WR": 17.0}
    assert TR.by_position(lineup, POS) == {"RB": 45.0, "WR": 17.0}
