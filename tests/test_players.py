import pytest

from sleeper.players import UNRANKED, _slim_row, key, team


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Kenneth Walker III", "kennethwalker"),
        ("Patrick Mahomes II", "patrickmahomes"),
        ("Marvin Harrison Jr.", "marvinharrison"),
        ("Amon-Ra St. Brown", "amonrastbrown"),
        ("Ja'Marr Chase", "jamarrchase"),
        ("D.J. Moore", "djmoore"),
        ("DJ Moore", "djmoore"),
    ],
)
def test_key_normalizes_hard_names(raw, expected):
    assert key(raw) == expected


def test_team_aliases():
    assert team("JAC") == "JAX"
    assert team("KC") == "KC"
    assert team(None) is None


def test_fullback_is_kept_via_fantasy_positions():
    row = _slim_row("2496", {"full_name": "Malcolm Johnson", "position": "FB", "fantasy_positions": ["RB"], "active": True})
    assert row["position"] == "RB"
    assert row["fantasy_positions"] == ["RB"]


def test_defense_gets_a_composed_name_and_aliases():
    row = _slim_row("KC", {"first_name": "Kansas City", "last_name": "Chiefs", "position": "DEF", "fantasy_positions": ["DEF"], "team": "KC"})
    assert row["name"] == "Kansas City Chiefs"
    assert row["aliases"] == ["kc", "kansascity", "chiefs"]


def test_non_fantasy_positions_are_dropped():
    assert _slim_row("1", {"full_name": "Some Lineman", "position": "OL", "fantasy_positions": ["OL"]}) is None


def test_missing_search_rank_becomes_unranked_and_ids_are_strings():
    row = _slim_row("4046", {"full_name": "Patrick Mahomes", "position": "QB", "fantasy_positions": ["QB"], "yahoo_id": 30123, "espn_id": None})
    assert row["search_rank"] == UNRANKED
    assert row["yahoo_id"] == "30123"
    assert row["espn_id"] is None
