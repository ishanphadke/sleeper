from sleeper import lineup as LU

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF", "BN", "BN"]
POS = {"q1": "QB", "q2": "QB", "r1": "RB", "r2": "RB", "r3": "RB", "w1": "WR", "w2": "WR", "w3": "WR",
       "t1": "TE", "t2": "TE", "k1": "K", "HOU": "DEF"}


def test_best_lineup_fills_fixed_slots_then_flex_with_the_best_leftovers():
    pts = {"q1": 20, "q2": 25, "r1": 15, "r2": 14, "r3": 13, "w1": 18, "w2": 9, "w3": 12, "t1": 8, "t2": 11, "k1": 7, "HOU": 6}
    total, lineup = LU.best_lineup(list(POS), POS, pts, SLOTS)
    got = {}
    for slot, pid, _ in lineup:
        got.setdefault(slot, []).append(pid)
    assert got["QB"] == ["q2"] and got["TE"] == ["t2"] and got["K"] == ["k1"] and got["DEF"] == ["HOU"]
    assert sorted(got["RB"]) == ["r1", "r2"] and sorted(got["WR"]) == ["w1", "w3"]
    assert sorted(got["FLEX"]) == ["r3", "w2"]                # 13 and 9 beat TE t1 at 8
    assert "q1" not in {p for _, p, _ in lineup}        # a second QB never takes a FLEX
    assert total == 25 + 15 + 14 + 18 + 12 + 11 + 13 + 9 + 7 + 6


def test_bye_and_unprojected_players_leave_their_slot_empty_rather_than_scoring():
    pts = {"r1": 15, "w1": 10}                          # everyone else is on bye or has no projection
    total, lineup = LU.best_lineup(list(POS), POS, pts, SLOTS)
    assert total == 25 and [s for s, _, _ in lineup] == ["RB", "WR"]


def test_superflex_takes_the_second_qb():
    pts = {"q1": 20, "q2": 25, "r1": 15, "w1": 10}
    _, lineup = LU.best_lineup(list(POS), POS, pts, ["QB", "SUPER_FLEX", "RB", "WR"])
    assert {p for _, p, _ in lineup} == {"q1", "q2", "r1", "w1"}


def test_season_weeks_run_through_the_fantasy_final():
    league = {"settings": {"playoff_week_start": 14, "playoff_teams": 8}}
    weeks, playoff = LU.season_weeks(league, 7)
    assert weeks == list(range(7, 17)) and playoff == [14, 15, 16]
    assert LU.season_weeks({"settings": {"playoff_week_start": 15, "playoff_teams": 6}}, 16) == ([16, 17], [16, 17])
    assert LU.season_weeks({"settings": {"playoff_week_start": 15, "playoff_teams": 4}}, 17) == ([], [])


def test_strength_weights_playoff_weeks():
    pos = {"r1": "RB", "w1": "WR"}
    weekly = {12: {"r1": 10, "w1": 10}, 13: {"r1": 10}, 14: {"r1": 20, "w1": 20}}
    st = LU.strength(["r1", "w1"], pos, weekly, ["RB", "WR"], [14])
    assert st["by_week"] == {12: 20, 13: 10, 14: 40}
    assert st["regular_ppg"] == 15.0 and st["playoff_ppg"] == 40.0 and st["ros_ppg"] == 23.3
    assert st["score"] == round((20 + 10 + 40 * LU.PLAYOFF_WEIGHT) / (2 + LU.PLAYOFF_WEIGHT), 2)


def test_a_bye_is_filled_from_waivers_so_it_costs_only_the_gap_to_the_free_agent():
    pos = {"r1": "RB", "w1": "WR"}
    weekly = {12: {"r1": 10, "w1": 10}, 13: {"r1": 10}}             # w1 on bye in week 13
    fills = {12: {"WR": [6.0]}, 13: {"WR": [7.0, 5.0]}}
    st = LU.strength(["r1", "w1"], pos, weekly, ["RB", "WR"], [], fills)
    assert st["by_week"] == {12: 20, 13: 17.0}
    total, lineup = LU.best_lineup(["r1"], pos, {"r1": 10}, ["RB", "WR", "WR"], {"WR": [7.0, 5.0]})
    assert total == 22.0 and [p for _, p, _ in lineup] == ["r1", "FA", "FA"]   # second empty slot takes the next free agent


def test_replacement_ignores_rostered_players_and_keeps_the_best_first():
    pts = {"a": 9.0, "b": 12.0, "c": 7.0, "d": 0.0, "e": 11.0}
    pos = {"a": "WR", "b": "WR", "c": "WR", "d": "WR", "e": "RB"}
    assert LU.replacement(pts, {"b"}, pos) == {"WR": [9.0, 7.0], "RB": [11.0]}


def test_fetch_week_drops_byes_and_unprojected_rows(monkeypatch):
    rows = [
        {"player_id": "1", "opponent": "LAC", "stats": {"pts_ppr": 21.96, "pts_std": 15.0}},
        {"player_id": "2", "opponent": None, "stats": {"pts_ppr": None}},       # bye
        {"player_id": "3", "opponent": "DAL", "stats": {}},                     # IR: row, no points
        {"player_id": "HOU", "opponent": "KC", "stats": {"pts_ppr": 6.1}},
    ]
    seen = {}

    def fake_get(host, path, params=None, fresh=False, headers=None):
        seen.update(host=host, path=path, params=params)
        return rows

    monkeypatch.setattr(LU.api, "get", fake_get)
    assert LU.fetch_week(2026, 8, "PPR") == {"1": 21.96, "HOU": 6.1}
    assert seen["host"] == "sleeper_root" and seen["path"] == "projections/nfl/2026/8"
    assert LU.fetch_week(2026, 8, "STD") == {"1": 15.0}


def test_load_weeks_caches_and_refetches_only_stale_weeks(monkeypatch, tmp_path):
    monkeypatch.setattr(LU, "cache_path", lambda season, scoring: tmp_path / "weekly.json")
    calls = []
    monkeypatch.setattr(LU, "fetch_week", lambda season, w, scoring: calls.append(w) or {"p": float(w)})
    assert LU.load_weeks(2026, [8, 9], "PPR") == {8: {"p": 8.0}, 9: {"p": 9.0}}
    assert LU.load_weeks(2026, [8, 9, 10], "PPR")[10] == {"p": 10.0}
    assert calls == [8, 9, 10]
    LU.load_weeks(2026, [8], "PPR", max_age_h=0)
    assert calls == [8, 9, 10, 8]
