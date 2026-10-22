from sleeper import offense as OF


def _row(team, yd, td, gp):
    return {"team": team, "stats": {"pass_yd": yd, "pass_td": td, "gp": gp}}


def test_ranks_are_per_game_so_a_bye_does_not_count_against_a_team():
    rows = [_row("AAA", 1200, 6, 6), _row("BBB", 1100, 5, 5),     # BBB had its bye: fewer totals, better rate
            _row("CCC", 900, 3, 6), _row("BBB", 0, 0, 5), {"team": None, "stats": {"pass_yd": 999}}]
    t = OF.rank_teams(rows)
    assert t["BBB"]["ypg"] == 220.0 and t["AAA"]["ypg"] == 200.0
    assert (t["BBB"]["yd_rank"], t["AAA"]["yd_rank"], t["CCC"]["yd_rank"]) == (1, 2, 3)
    assert [x for x in sorted(t, key=lambda k: t[k]["rank"])] == ["BBB", "AAA", "CCC"]


def test_bad_takes_the_bottom_of_the_combined_ranking():
    t = {"A": {"rank": 1}, "B": {"rank": 2}, "C": {"rank": 3}, "D": {"rank": 4}}
    assert OF.bad(t, 2) == {"C", "D"} and OF.bad(t, 0) == frozenset()


def test_ordinals():
    assert [OF.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 31, 32)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "31st", "32nd"]


def test_passing_caches_for_half_a_day(monkeypatch, tmp_path):
    monkeypatch.setattr(OF, "cache_path", lambda season: tmp_path / "off.json")
    calls = []
    monkeypatch.setattr(OF.api, "get", lambda *a, **k: calls.append(1) or [_row("AAA", 200, 1, 1)])
    assert OF.passing(2026)["AAA"]["rank"] == 1 and OF.passing(2026)["AAA"]["rank"] == 1
    assert len(calls) == 1
    OF.passing(2026, max_age_h=0)
    assert len(calls) == 2


def test_recent_uses_each_teams_last_games_skips_byes_and_names_the_starter(monkeypatch):
    def qb(team, name, yd, td, att):
        return {"team": team, "pid": name, "pos": "QB", "name": name, "stats": {"pass_yd": yd, "pass_td": td, "pass_att": att, "gp": 1}}

    weeks = {
        6: [qb("CHI", "Williams", 305, 1, 35), qb("AAA", "Ace", 200, 2, 30)],
        5: [qb("CHI", "Keenum", 108, 0, 15), qb("CHI", "Bagent", 87, 0, 12)],      # AAA on bye
        4: [qb("CHI", "Bagent", 268, 0, 33), qb("AAA", "Ace", 220, 2, 31)],
        3: [qb("AAA", "Ace", 240, 3, 32)],
    }
    monkeypatch.setattr(OF, "week_rows", lambda season, wk: weeks.get(wk, []))
    out, looked = OF.recent(2026, 7, games=2)
    assert out["CHI"]["games"] == 2 and out["CHI"]["ypg"] == (305 + 108 + 87) / 2
    assert out["CHI"]["qbs"] == "wk5 Keenum / wk6 Williams"              # most attempts that week is the starter
    assert out["AAA"]["qbs"] == "wk4 Ace / wk6 Ace" and out["AAA"]["tdpg"] == 2.0   # reaches past its bye
    # CHI leads yards, AAA leads TDs: tied on rank sum, yards break it. Weeks with no stats are skipped.
    assert out["CHI"]["rank"] == 1 and out["AAA"]["rank"] == 2 and looked == [3, 4, 5, 6]
