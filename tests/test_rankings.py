import json
from pathlib import Path

import pytest

from sleeper import api, players
from sleeper import rankings as R
from sleeper.players import UNRANKED, _slim_row


def row(pid, name, pos, team=None, **extra):
    if pos == "DEF":
        city, nick = name.rsplit(" ", 1)
        p = {"first_name": city, "last_name": nick}
    else:
        p = {"full_name": name}
    p.update({"position": pos, "fantasy_positions": [pos], "team": team}, **extra)
    return _slim_row(pid, p)


TABLE = {r["player_id"]: r for r in [
    row("1", "Override Guy", "WR", "DAL", search_rank=1),
    row("2", "Radar Man", "RB", "SF", sportradar_id="uuid-2", yahoo_id=222, search_rank=2),
    row("3", "Yahoo Man", "QB", "BUF", yahoo_id=333, espn_id=3333, search_rank=3),
    row("4", "Espn Man", "TE", "KC", espn_id=444, search_rank=4),
    row("44", "Espn Flat", "TE", "LV", espn_id=4444, search_rank=5),
    row("KC", "Kansas City Chiefs", "DEF", "KC"),
    row("JAX", "Jacksonville Jaguars", "DEF", "JAX"),
    row("5", "Team Match", "WR", "JAX", search_rank=10),
    row("6", "Team Match", "WR", "MIA", search_rank=11),
    row("7", "Unique Name", "RB", "DEN", search_rank=12),
    row("8", "Frank Gore", "RB", None, search_rank=50),
    row("9", "Frank Gore", "RB", "BUF"),
    row("10", "Ronald Jones", "RB", None, search_rank=100),
    row("11", "Ronald Jones", "RB", None, search_rank=200),
    row("12", "Twin Ambiguous", "WR", None),
    row("13", "Twin Ambiguous", "WR", None),
    row("14", "Marvin Harrison Jr.", "WR", "ARI", search_rank=20),
    row("15", "Stale Dup", "WR", None, yahoo_id=555),
    row("16", "Stale Dup", "WR", "NYJ", yahoo_id=555, search_rank=30),
]}


def fp(fp_id, name, pos, team, ecr, **extra):
    r = {"player_id": fp_id, "player_name": name, "player_position_id": pos, "player_team_id": team, "rank_ecr": ecr}
    r.update(extra)
    return r


RANKED = [
    fp(100, "Override Guy", "WR", "DAL", 1, sportsdata_id="uuid-2"),
    fp(101, "Radar Man", "RB", "SF", 2, sportsdata_id="uuid-2", player_yahoo_id=333),
    fp(102, "Yahoo Man", "QB", "BUF", "3", player_yahoo_id=333, tier=1, pos_rank="QB1", player_bye_week="7"),
    fp(103, "Espn Man", "TE", "KC", 4),
    fp(104, "Espn Flat", "TE", "LV", 5),
    fp(105, "Kansas City Chiefs", "DST", "KC", 6),
    fp(106, "Jacksonville", "DST", "JAC", 7),
    fp(107, "Team Match", "WR3", "JAC", 8),
    fp(108, "Unique Name", "RB", "NYG", 9),
    fp(109, "Frank Gore", "RB", "FA", 10),
    fp(110, "Ronald Jones", "RB", "FA", 11),
    fp(111, "Twin Ambiguous", "WR", "FA", 12),
    fp(112, "Unique Name", "WR", "DEN", 13),
    fp(113, "Marvin Harrison", "WR", "ARI", 14),
    fp(114, "Stale Dup", "WR", "NYJ", 15, player_yahoo_id=555),
    fp(115, "Nobody Near", "RB", "SEA", 250),
    fp(116, "Nobody Far", "RB", "SEA", 251),
]
FP_PLAYERS = [
    {"player_id": 101, "rank_adp_ppr": 5, "rank_adp": 9},
    {"player_id": 102, "rank_adp_ppr": 20, "rank_adp": 25, "espn_id": 3333},
    {"player_id": 103, "external_ids": {"yahoo": None, "espn": 444}},
    {"player_id": 104, "espn_id": "4444"},
]
OVERRIDES = {"100": "1"}


@pytest.fixture
def joined():
    return R.join(RANKED, FP_PLAYERS, TABLE, OVERRIDES)


def test_override_beats_every_id_match(joined):
    rows, _ = joined
    assert rows["1"]["ecr"] == 1


def test_sportradar_beats_yahoo(joined):
    rows, _ = joined
    assert rows["2"]["ecr"] == 2


def test_yahoo_beats_espn_and_string_ecr_is_int(joined):
    rows, _ = joined
    assert rows["3"] == {"ecr": 3, "tier": 1, "pos_rank": "QB1", "adp": 20, "bye": 7}


def test_espn_id_from_players_response_in_either_shape(joined):
    rows, _ = joined
    assert rows["4"]["ecr"] == 4
    assert rows["44"]["ecr"] == 5


def test_dst_resolves_through_the_team_map(joined):
    rows, _ = joined
    assert rows["KC"]["ecr"] == 6
    assert rows["JAX"]["ecr"] == 7


def test_name_with_team_and_pos_rank_style_position(joined):
    rows, _ = joined
    assert rows["5"]["ecr"] == 8
    assert "6" not in rows


def test_name_and_position_alone_when_team_differs(joined):
    rows, _ = joined
    assert rows["7"]["ecr"] == 9


def test_name_fallback_prefers_a_team_then_search_rank(joined):
    rows, _ = joined
    assert rows["9"]["ecr"] == 10
    assert "8" not in rows
    assert rows["10"]["ecr"] == 11
    assert "11" not in rows


def test_suffix_is_ignored_in_name_matching(joined):
    rows, _ = joined
    assert rows["14"]["ecr"] == 14


def test_duplicate_sleeper_ids_resolve_to_the_preferred_row(joined):
    rows, _ = joined
    assert rows["16"]["ecr"] == 15
    assert "15" not in rows


def test_report_counts_and_unmatched_within_cutoff(joined):
    rows, report = joined
    assert report["count"] == len(RANKED)
    assert report["by_id"] == 8
    assert report["by_name"] == 5
    assert report["by_id"] + report["by_name"] == len(rows)
    assert [u["fp_id"] for u in report["unmatched"]] == ["111", "112", "115"]
    assert report["unmatched"][0] == {"fp_id": "111", "name": "Twin Ambiguous", "team": "FA", "pos": "WR", "ecr": 12}


def test_adp_field_follows_scoring():
    ranked = [fp(101, "Radar Man", "RB", "SF", 1, sportsdata_id="uuid-2")]
    assert R.join(ranked, FP_PLAYERS, TABLE, {}, scoring="PPR")[0]["2"]["adp"] == 5
    assert R.join(ranked, FP_PLAYERS, TABLE, {}, scoring="HALF")[0]["2"]["adp"] == 5
    assert R.join(ranked, FP_PLAYERS, TABLE, {}, scoring="STD")[0]["2"]["adp"] == 9
    assert R.join(ranked, [], TABLE, {})[0]["2"]["adp"] is None


@pytest.mark.parametrize("rec, expected", [(1.0, "PPR"), (1, "PPR"), (0.5, "HALF"), (0, "STD"), (None, "STD")])
def test_scoring_for(rec, expected):
    assert R.scoring_for({"scoring_settings": {"rec": rec}}) == expected


def test_scoring_for_without_settings_is_standard():
    assert R.scoring_for({}) == "STD"


FIXTURES = Path(__file__).parent / "fixtures"
ESPN_DOC = json.loads((FIXTURES / "espn_players_2026.json").read_text())
ESPN_TEAMS_DOC = json.loads((FIXTURES / "espn_pro_teams_2026.json").read_text())
ESPN_TEAMS = R.espn_teams(ESPN_TEAMS_DOC)
SLEEPER_DOC = json.loads((FIXTURES / "sleeper_projections_2026.json").read_text())
SLEEPER_PARAMS = {"season_type": "regular", "position[]": ["QB", "RB", "WR", "TE", "K", "DEF"], "order_by": "adp_ppr"}


def crow(**kw):
    """A full cache row: every rank_of key, None unless given."""
    return dict(dict.fromkeys(R.RANK_KEYS), **kw)


def sl(pid, adp=None, pts=None):
    """A Sleeper projections row with only the fields sleeper_rows reads."""
    return {"player_id": pid, "player": {"position": "RB", "team": "KC"}, "stats": {"adp_ppr": adp, "pts_ppr": pts}}


def espn(espn_id, name, pos_id, team_id, rank, adp=None, **ranks):
    by_type = {"PPR": {"rank": rank}, "STANDARD": {"rank": rank}, "SUPERFLEX": {"rank": rank}}
    by_type.update({k: {"rank": v} for k, v in ranks.items()})
    return {"player": {
        "id": espn_id, "fullName": name, "defaultPositionId": pos_id, "proTeamId": team_id,
        "injuryStatus": "ACTIVE", "ownership": {"averageDraftPosition": adp}, "draftRanksByRankType": by_type,
        "stats": [{"big": True}],
    }}


ESPN_SYNTH = {"players": [
    espn(3333, "Yahoo Man", 1, 2, 1, adp=4.26),
    espn(444, "Espn Man", 4, 12, 2, adp=0),
    espn(-16012, "Chiefs D/ST", 16, 12, 3, adp=150.049),
    espn(9001, "Unique Name", 2, 7, 4),
    espn(9002, "Frank Gore", 2, 2, 5),
    espn(9003, "Twin Ambiguous", 3, 0, 6),
    espn(9004, "Override Guy", 3, 6, 7),
    espn(9005, "Espn Flat", 4, 13, 8),
    espn(9006, "Jaguars D/ST", 16, 30, 9),
    espn(9007, "Nobody Home", 2, 26, 251),
    {"player": {"id": 9008, "fullName": "No Rank", "defaultPositionId": 2, "proTeamId": 1, "draftRanksByRankType": {}}},
    {"player": {"id": 9009, "fullName": "Long Snapper", "defaultPositionId": 9, "proTeamId": 1, "draftRanksByRankType": {"PPR": {"rank": 10}}}},
]}


def test_espn_teams_map_to_sleeper_abbreviations_with_bye():
    assert ESPN_TEAMS[8] == {"team": "DET", "bye": 6}
    assert ESPN_TEAMS[28] == {"team": "WAS", "bye": 7}
    assert ESPN_TEAMS[30] == {"team": "JAX", "bye": 7}
    assert 0 not in ESPN_TEAMS
    assert len(ESPN_TEAMS) == 32


def test_espn_rows_from_the_captured_fixture():
    rows = R.espn_rows(ESPN_DOC, ESPN_TEAMS, "PPR")
    assert len(rows) == 63
    assert rows[0] == {"espn_id": "4429795", "name": "Jahmyr Gibbs", "pos": "RB", "team": "DET", "rank": 1, "adp": 1.3, "bye": 6, "injury": "ACTIVE", "ppg": None}
    assert [r["rank"] for r in rows] == sorted(r["rank"] for r in rows)
    assert {r["pos"] for r in rows} == {"QB", "RB", "WR", "TE", "DEF"}
    dst = [r for r in rows if r["pos"] == "DEF"]
    assert dst[0] == {"espn_id": "-16034", "name": "Texans D/ST", "pos": "DEF", "team": "HOU", "rank": 176, "adp": 93.0, "bye": 8, "injury": None, "ppg": None}
    assert (FIXTURES / "espn_players_2026.json").stat().st_size < 300_000


def test_espn_rows_follow_the_rank_type():
    ppr = R.espn_rows(ESPN_DOC, ESPN_TEAMS, "PPR")
    sf = R.espn_rows(ESPN_DOC, ESPN_TEAMS, "SUPERFLEX")
    gibbs = next(r for r in sf if r["name"] == "Jahmyr Gibbs")
    assert ppr[0]["name"] == "Jahmyr Gibbs" and gibbs["rank"] == 7
    assert sf[0]["pos"] == "QB" and sf[0]["rank"] == 1
    assert [r["rank"] for r in sf] == sorted(r["rank"] for r in sf)
    assert R.espn_rows(ESPN_DOC, ESPN_TEAMS, "NOPE") == []


@pytest.mark.parametrize("scoring, superflex, expected", [
    ("PPR", False, "PPR"), ("HALF", False, "PPR"), ("STD", False, "STANDARD"),
    ("PPR", True, "SUPERFLEX"), ("STD", True, "SUPERFLEX"),
])
def test_espn_rank_type(scoring, superflex, expected):
    assert R.espn_rank_type(scoring, superflex) == expected


def test_espn_rows_skip_unranked_and_non_fantasy_positions_and_round_adp():
    rows = {r["espn_id"]: r for r in R.espn_rows(ESPN_SYNTH, ESPN_TEAMS, "PPR")}
    assert "9008" not in rows and "9009" not in rows
    assert rows["3333"]["adp"] == 4.3
    assert rows["444"]["adp"] is None
    assert rows["-16012"]["adp"] == 150.0
    assert rows["9001"]["adp"] is None
    assert rows["9003"]["team"] is None and rows["9003"]["bye"] is None
    assert rows["-16012"] == {"espn_id": "-16012", "name": "Chiefs D/ST", "pos": "DEF", "team": "KC", "rank": 3, "adp": 150.0, "bye": 5, "injury": "ACTIVE", "ppg": None}


@pytest.fixture
def espn_joined():
    return R.join_espn(R.espn_rows(ESPN_SYNTH, ESPN_TEAMS, "PPR"), TABLE, {"9004": "1"})


def test_espn_join_by_id_then_dst_then_name(espn_joined):
    rows, report = espn_joined
    assert rows["3"]["rank"] == 1 and rows["4"]["rank"] == 2 and rows["44"]["rank"] == 8
    assert rows["KC"]["rank"] == 3 and rows["JAX"]["rank"] == 9
    assert rows["7"]["rank"] == 4
    assert rows["9"]["rank"] == 5 and "8" not in rows
    assert rows["1"]["rank"] == 7
    assert rows["44"]["rank"] == 8
    assert report["count"] == 10 and report["by_id"] == 5 and report["by_name"] == 3
    assert report["by_id"] + report["by_name"] == len(rows) == 8
    assert report["unmatched"] == [{"espn_id": "9003", "name": "Twin Ambiguous", "team": None, "pos": "WR", "rank": 6}]


def test_espn_join_dst_without_a_def_row_is_unmatched():
    rows, report = R.join_espn(R.espn_rows({"players": [espn(-16034, "Texans D/ST", 16, 34, 1)]}, ESPN_TEAMS, "PPR"), TABLE)
    assert rows == {} and report["unmatched"][0]["team"] == "HOU"


def test_espn_join_reports_duplicates_as_unmatched():
    doc = {"players": [espn(3333, "Yahoo Man", 1, 2, 1), espn(9010, "Yahoo Man", 1, 2, 2)]}
    rows, report = R.join_espn(R.espn_rows(doc, ESPN_TEAMS, "PPR"), TABLE)
    assert list(rows) == ["3"] and [u["espn_id"] for u in report["unmatched"]] == ["9010"]


def fp_rows(n, start=1):
    return {str(1000 + i): {"ecr": start + i, "tier": 1, "pos_rank": f"WR{i + 1}", "adp": start + i, "bye": 9} for i in range(n)}


def test_merge_espn_alone_derives_pos_rank(espn_joined):
    rows, source = R.merge(espn_joined[0], None)
    assert source == "espn"
    assert rows["3"] == crow(rank=1, ecr=1, pos_rank="QB1", adp=4.3, bye=7, espn_adp=4.3)
    assert rows["4"]["pos_rank"] == "TE1" and rows["44"]["pos_rank"] == "TE2"
    assert rows["KC"]["pos_rank"] == "DEF1" and rows["JAX"]["pos_rank"] == "DEF2"
    assert rows["7"]["pos_rank"] == "RB1" and rows["9"]["pos_rank"] == "RB2"
    assert set(rows) == set(espn_joined[0])


def test_merge_overlays_a_small_fantasypros_set_without_taking_over(espn_joined):
    fp = {"3": {"ecr": 40, "tier": 3, "pos_rank": "QB4", "adp": 33, "bye": None}, "999": {"ecr": 2, "tier": 1, "pos_rank": "RB1", "adp": 2, "bye": 5}}
    rows, source = R.merge(espn_joined[0], fp)
    assert source == "espn"
    assert rows["3"] == crow(rank=1, ecr=1, tier=3, pos_rank="QB4", adp=4.3, bye=7, espn_adp=4.3)
    assert "999" not in rows
    assert rows["4"]["tier"] is None


def test_merge_lets_a_full_fantasypros_set_take_over(espn_joined):
    fp = dict(fp_rows(R.MIN_BOARD - 1), **{"3": {"ecr": 40, "tier": 3, "pos_rank": "QB4", "adp": 33, "bye": 8}})
    rows, source = R.merge(espn_joined[0], fp)
    assert source == "fantasypros"
    assert rows["3"] == crow(rank=40, ecr=40, tier=3, pos_rank="QB4", adp=4.3, bye=8, espn_adp=4.3)
    assert rows["1000"] == crow(rank=1, ecr=1, tier=1, pos_rank="WR1", adp=1, bye=9)
    assert rows["4"] == crow(rank=2, ecr=2, pos_rank="TE1", bye=5)
    assert R.merge(espn_joined[0], fp_rows(R.MIN_BOARD - 1))[1] == "espn"
    assert R.merge(espn_joined[0], fp, {"3": {"sl_rank": 2, "sl_adp": 2.2, "sl_ppg": 20.1}})[1] == "sleeper+fantasypros"


def test_sleeper_rows_from_the_captured_fixture():
    rows = R.sleeper_rows(SLEEPER_DOC, "PPR")
    assert len(rows) == 63 and (FIXTURES / "sleeper_projections_2026.json").stat().st_size < 60_000
    assert rows["9221"] == {"sl_pts": 331.4, "sl_ppg": 19.5, "sl_adp": 1.7, "sl_rank": 1}
    assert rows["4035"] == {"sl_pts": 63.0, "sl_ppg": 3.7, "sl_adp": 167.6, "sl_rank": 63}
    assert rows["LAR"] == {"sl_pts": 106.0, "sl_ppg": 6.2, "sl_adp": 86.9, "sl_rank": 61}
    ranked = sorted(rows.values(), key=lambda k: k["sl_rank"])
    assert [k["sl_rank"] for k in ranked] == list(range(1, 64))
    assert [k["sl_adp"] for k in ranked] == sorted(k["sl_adp"] for k in ranked)
    half = R.sleeper_rows(SLEEPER_DOC, "HALF")
    assert half["9221"]["sl_adp"] == 1.9 and half["9221"]["sl_ppg"] == 17.6
    assert R.sleeper_rows(SLEEPER_DOC, "STD", superflex=True)["9221"]["sl_adp"] == 1.4


def test_sleeper_rows_skip_the_999_adp_sentinel_and_non_fantasy_rows():
    doc = [
        sl("a", adp=999.0, pts=12.7),
        sl("b", adp=5.0),
        sl("c", adp=3.0, pts=170.0),
        dict(sl("d", adp=1.0, pts=100.0), player={"position": "P", "fantasy_positions": ["P"]}),
        dict(sl("e", adp=2.0, pts=100.0), player={"position": "FB", "fantasy_positions": ["RB"]}),
        {"player_id": "f", "player": {"position": "WR"}, "stats": {}},
        {"player_id": "g", "player": {"position": "WR"}, "stats": {"adp_ppr": 999.0}},
    ]
    rows = R.sleeper_rows(doc, "PPR")
    assert set(rows) == {"a", "b", "c", "e"}
    assert rows["a"] == {"sl_pts": 12.7, "sl_ppg": 0.7, "sl_adp": None, "sl_rank": None}
    assert rows["b"] == {"sl_pts": None, "sl_ppg": None, "sl_adp": 5.0, "sl_rank": 3}
    assert rows["c"]["sl_rank"] == 2 and rows["e"]["sl_rank"] == 1


def test_merge_biases_toward_sleeper_and_keeps_espn_under_its_own_names(espn_joined):
    sleeper = {
        "3": {"sl_rank": 9, "sl_adp": 9.9, "sl_ppg": 18.2},
        "4": {"sl_rank": None, "sl_adp": None, "sl_ppg": 6.0},
        "KC": {"sl_rank": 60, "sl_adp": 88.0, "sl_ppg": None},
        "9221": {"sl_rank": 1, "sl_adp": 1.7, "sl_ppg": 19.5},
    }
    espn = dict(espn_joined[0])
    espn["3"] = dict(espn["3"], ppg=22.0)
    rows, source = R.merge(espn, {"3": {"ecr": 40, "tier": 3, "pos_rank": "QB4", "adp": 33, "bye": None}}, sleeper)
    assert source == "sleeper+espn"
    assert rows["3"] == crow(rank=9, ecr=1, tier=3, pos_rank="QB4", adp=9.9, bye=7, ppg=18.2, espn_adp=4.3, espn_ppg=22.0, sl_rank=9, sl_adp=9.9, sl_ppg=18.2)
    assert rows["4"] == crow(rank=2, ecr=2, pos_rank="TE1", bye=5, ppg=6.0, sl_ppg=6.0)
    assert rows["KC"] == crow(rank=60, ecr=3, pos_rank="DEF1", adp=88.0, bye=5, espn_adp=150.0, sl_rank=60, sl_adp=88.0)
    assert rows["9221"] == crow(rank=1, adp=1.7, ppg=19.5, sl_rank=1, sl_adp=1.7, sl_ppg=19.5)
    assert rows["7"] == crow(rank=4, ecr=4, pos_rank="RB1", bye=10)
    assert len(rows) == len(espn) + 1


def _fake_get(calls):
    def fake_get(host, path, params=None, fresh=False, headers=None):
        calls.append((host, path, params, headers))
        if host == "sleeper_root":
            return json.loads(json.dumps(SLEEPER_DOC))
        if host == "espn" and path.endswith("leaguedefaults/3"):
            return json.loads(json.dumps(ESPN_SYNTH))
        if host == "espn":
            return ESPN_TEAMS_DOC
        if path.endswith("consensus-rankings"):
            return {"players": RANKED, "last_updated_ts": 1_700_000_000}
        return {"players": FP_PLAYERS}
    return fake_get


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "get_response", lambda *a, **k: pytest.fail("real request attempted"))
    monkeypatch.setattr(R, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(players, "load", lambda: TABLE)
    calls = []
    monkeypatch.setattr(api, "get", _fake_get(calls))
    return calls


def test_refresh_without_a_key_uses_espn_only(tmp_path, offline, monkeypatch):
    monkeypatch.delenv("FANTASYPROS_API_KEY", raising=False)
    report = R.refresh(2026, "HALF", OVERRIDES, superflex=True, espn_overrides={"9004": "1"})
    assert [(c[0], c[1], c[2]) for c in offline] == [
        ("sleeper_root", "projections/nfl/2026", dict(SLEEPER_PARAMS, order_by="adp_2qb")),
        ("espn", "seasons/2026/segments/0/leaguedefaults/3", {"view": "kona_player_info"}),
        ("espn", "seasons/2026", {"view": "proTeamSchedules_wl"}),
    ]
    assert offline[0][3] is None
    flt = json.loads(offline[1][3]["X-Fantasy-Filter"])["players"]
    assert flt["limit"] == R.ESPN_LIMIT and flt["sortDraftRanks"]["value"] == "SUPERFLEX" and flt["filterRanksForRankTypes"]["value"] == ["SUPERFLEX"]
    assert report["source"] == "sleeper+espn"
    assert report["sleeper"] == {"count": 63, "with_pts": 63, "with_adp": 63}
    assert report["fantasypros"] == "skipped: FANTASYPROS_API_KEY not set"
    assert report["espn"]["count"] == 10 and report["espn"]["by_id"] == 5 and report["espn"]["by_name"] == 3
    assert [u["espn_id"] for u in report["espn"]["unmatched"]] == ["9003"]
    assert report["path"] == str(tmp_path / "rankings.2026.HALF.json")
    doc = json.loads((tmp_path / "rankings.2026.HALF.json").read_text())
    assert doc["source"] == "sleeper+espn" and doc["scoring"] == "HALF" and doc["season"] == "2026" and doc["rank_type"] == "SUPERFLEX"
    assert doc["last_updated_ts"] is None and isinstance(doc["fetched_at"], int)
    assert doc["sleeper_rows"] == 63 and doc["espn_rows"] == 8 and doc["fantasypros_rows"] == 0
    assert len(doc["rows"]) == 8 + 63
    assert doc["rows"]["3"] == crow(rank=1, ecr=1, pos_rank="QB1", adp=4.3, bye=7, espn_adp=4.3)
    assert doc["rows"]["1"]["ecr"] == 7
    # Sleeper-only rows are keyed by Sleeper id without a join: 2QB ADP, half-PPR points
    assert doc["rows"]["9221"] == crow(rank=1, adp=1.4, ppg=17.6, sl_rank=1, sl_adp=1.4, sl_ppg=17.6)
    assert json.loads((tmp_path / "rankings.raw.sleeper.2026.json").read_text()) == SLEEPER_DOC
    raw = json.loads((tmp_path / "rankings.raw.espn.2026.json").read_text())
    assert raw["rank_type"] == "SUPERFLEX" and len(raw["players"]["players"]) == 12
    assert all("stats" not in e["player"] for e in raw["players"]["players"])
    assert raw["teams"] == ESPN_TEAMS_DOC
    assert not (tmp_path / "rankings.raw.2026.HALF.json").exists()
    assert not (tmp_path / "rankings.2026.HALF.tmp").exists()
    loaded = R.load(2026, "HALF")
    assert loaded["rows"] == doc["rows"]
    assert 0 <= loaded["age_hours"] < 1


def test_refresh_with_a_key_overlays_fantasypros(tmp_path, offline, monkeypatch):
    monkeypatch.setenv("FANTASYPROS_API_KEY", "test-key")
    report = R.refresh(2026, "PPR", OVERRIDES)
    assert [(c[0], c[1], c[2]) for c in offline] == [
        ("sleeper_root", "projections/nfl/2026", SLEEPER_PARAMS),
        ("espn", "seasons/2026/segments/0/leaguedefaults/3", {"view": "kona_player_info"}),
        ("espn", "seasons/2026", {"view": "proTeamSchedules_wl"}),
        ("fantasypros", "nfl/2026/consensus-rankings", {"position": "ALL", "type": "DRAFT", "scoring": "PPR", "week": 0}),
        ("fantasypros", "nfl/players", {"ecr": "included", "external_ids": "yahoo:espn"}),
    ]
    assert report["source"] == "sleeper+espn"
    assert report["fantasypros"]["by_id"] == 8 and report["fantasypros"]["by_name"] == 5
    assert report["espn"]["count"] == 10 and report["espn"]["by_id"] == 4 and report["espn"]["by_name"] == 4
    doc = json.loads((tmp_path / "rankings.2026.PPR.json").read_text())
    assert doc["source"] == "sleeper+espn" and doc["last_updated_ts"] == 1_700_000_000
    assert doc["espn_rows"] == 8 and doc["fantasypros_rows"] == 13
    assert doc["rows"]["3"] == crow(rank=1, ecr=1, tier=1, pos_rank="QB1", adp=4.3, bye=7, espn_adp=4.3)
    assert doc["rows"]["1"]["ecr"] == 7
    assert doc["rows"]["4035"] == crow(rank=63, adp=167.6, ppg=3.7, sl_rank=63, sl_adp=167.6, sl_ppg=3.7)
    assert "2" not in doc["rows"]
    assert (tmp_path / "rankings.raw.2026.PPR.json").exists()


def test_refresh_lets_a_paid_key_supply_the_board(tmp_path, offline, monkeypatch):
    monkeypatch.setenv("FANTASYPROS_API_KEY", "test-key")
    monkeypatch.setattr(R, "join", lambda *a, **k: (fp_rows(R.MIN_BOARD), {"count": R.MIN_BOARD, "by_id": R.MIN_BOARD, "by_name": 0, "unmatched": []}))
    report = R.refresh(2026, "PPR", OVERRIDES)
    assert report["source"] == "sleeper+fantasypros"
    doc = json.loads((tmp_path / "rankings.2026.PPR.json").read_text())
    assert doc["source"] == "sleeper+fantasypros" and doc["fantasypros_rows"] == R.MIN_BOARD
    assert doc["rows"]["1000"]["ecr"] == 1 and doc["rows"]["3"]["ecr"] == 1


def test_refresh_without_sleeper_rows_stays_espn(tmp_path, offline, monkeypatch):
    monkeypatch.delenv("FANTASYPROS_API_KEY", raising=False)
    monkeypatch.setattr(R, "fetch_sleeper", lambda season, scoring, superflex=False: [])
    report = R.refresh(2026, "PPR", OVERRIDES)
    assert report["source"] == "espn" and report["sleeper"] == {"count": 0, "with_pts": 0, "with_adp": 0}
    doc = json.loads((tmp_path / "rankings.2026.PPR.json").read_text())
    assert doc["source"] == "espn" and doc["sleeper_rows"] == 0 and len(doc["rows"]) == 8
    assert doc["rows"]["3"]["sl_rank"] is None and doc["rows"]["3"]["rank"] == 1


def test_load_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CACHE_DIR", tmp_path)
    assert R.load(2026, "PPR") is None


def test_rank_of_falls_back_to_search_rank():
    rank = R.rank_of(None, TABLE)
    assert rank("8") == crow(rank=50, ecr=50, source="search_rank")
    assert rank("nope")["ecr"] == UNRANKED and rank("nope")["rank"] == UNRANKED


def test_rank_of_reads_the_cache():
    # a pre-Sleeper cache row: rank falls back to ecr, adp/ppg stay ESPN's, espn_*/sl_* are None
    rank = R.rank_of({"source": "fantasypros", "rows": {"3": {"ecr": 3, "tier": 1, "pos_rank": "QB1", "adp": 20, "bye": 7, "ppg": 15.1}}}, TABLE)
    assert rank("3") == crow(rank=3, ecr=3, tier=1, pos_rank="QB1", adp=20, bye=7, ppg=15.1, source="fantasypros")
    assert rank("8") == crow(source="fantasypros")
    assert R.rank_of({"source": "espn", "rows": {}}, TABLE)("3")["source"] == "espn"
    full = crow(rank=9, ecr=1, pos_rank="QB1", adp=9.9, bye=7, ppg=18.2, espn_adp=4.3, espn_ppg=22.0, sl_rank=9, sl_adp=9.9, sl_ppg=18.2)
    rank = R.rank_of({"source": "sleeper+espn", "rows": {"3": full}}, TABLE)
    assert rank("3") == dict(full, source="sleeper+espn")
    assert set(rank("3")) == set(R.RANK_KEYS) | {"source"}


def test_strip_stats_lifts_the_season_projection_and_espn_rows_carry_ppg():
    from sleeper import rankings as R
    doc = {"players": [{"player": {
        "id": 1, "fullName": "Jahmyr Gibbs", "defaultPositionId": 2, "proTeamId": 8,
        "draftRanksByRankType": {"PPR": {"rank": 1}}, "ownership": {"averageDraftPosition": 1.32},
        "stats": [
            {"id": "002026", "statSourceId": 0, "scoringPeriodId": 0, "statSplitTypeId": 0, "appliedTotal": 0.0, "appliedAverage": 0.0},
            {"id": "102026", "statSourceId": 1, "scoringPeriodId": 0, "statSplitTypeId": 0, "appliedTotal": 365.7, "appliedAverage": 21.51},
            {"id": "1120261", "statSourceId": 1, "scoringPeriodId": 1, "statSplitTypeId": 1, "appliedTotal": 20.0, "appliedAverage": 20.0},
        ],
    }}]}
    R.strip_stats(doc)
    p = doc["players"][0]["player"]
    assert "stats" not in p and p["proj_ppg"] == 21.51 and p["proj_pts"] == 365.7
    rows = R.espn_rows(doc, {8: {"team": "DET", "bye": 6}}, "PPR")
    assert rows[0]["ppg"] == 21.5
    merged, source = R.merge({"9221": rows[0]}, None)
    assert merged["9221"]["ppg"] == 21.5 and merged["9221"]["espn_ppg"] == 21.5 and source == "espn"


def test_fetch_sleeper_hits_the_root_host_with_repeated_position_params(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "get_response", lambda *a, **k: pytest.fail("real request attempted"))
    monkeypatch.setattr(api, "get", lambda host, path, params=None, fresh=False, headers=None: calls.append((host, path, params, fresh, headers)) or [])
    assert R.fetch_sleeper(2026, "PPR") == []
    assert R.fetch_sleeper("2026", "STD", superflex=True) == []
    assert calls == [
        ("sleeper_root", "projections/nfl/2026", SLEEPER_PARAMS, False, None),
        ("sleeper_root", "projections/nfl/2026", dict(SLEEPER_PARAMS, order_by="adp_2qb"), False, None),
    ]
    assert api.HOSTS["sleeper_root"] == "https://api.sleeper.app" and api.LIMITER_OF["sleeper_root"] == "sleeper"


@pytest.mark.parametrize("scoring, superflex, expected", [
    ("PPR", False, ("adp_ppr", "pts_ppr")), ("HALF", False, ("adp_half_ppr", "pts_half_ppr")), ("STD", False, ("adp_std", "pts_std")),
    ("PPR", True, ("adp_2qb", "pts_ppr")), ("HALF", True, ("adp_2qb", "pts_half_ppr")),
])
def test_sleeper_fields(scoring, superflex, expected):
    assert R.sleeper_fields(scoring, superflex) == expected
