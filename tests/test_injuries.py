import json

import pytest

from sleeper import api, injuries as I, players as P


def _row(**kw):
    base = {"injury_status": None, "status": "Active", "practice_participation": None, "team": "KC", "depth_chart_order": 1}
    return {**base, **kw}


def _table():
    return {
        "1": {"player_id": "1", "name": "Star Back", "position": "RB", "team": "KC", "search_rank": 20},
        "2": {"player_id": "2", "name": "Hurt Wideout", "position": "WR", "team": "SF", "search_rank": 150, "injury_status": "IR"},
        "3": {"player_id": "3", "name": "Deep Bench", "position": "TE", "team": "NYJ", "search_rank": 900, "injury_status": "IR"},
        "4": {"player_id": "4", "name": "Rival Guy", "position": "RB", "team": "LAR", "search_rank": 60, "injury_status": "PUP"},
        "5": {"player_id": "5", "name": "No Team", "position": "WR", "team": None, "search_rank": 100, "injury_status": "IR"},
    }


def test_sidelined_covers_designations_and_inactive_status():
    assert I.is_sidelined({"injury_status": "IR"}) and I.is_sidelined({"injury_status": "Out"})
    assert I.is_sidelined({"status": "Inactive"}) and not I.is_sidelined(_row())
    assert not I.is_sidelined({"injury_status": "Questionable", "status": "Active"})


def test_watchlist_is_my_roster_then_unowned_sidelined_by_rank():
    rosters = [{"roster_id": 3, "players": ["1"], "reserve": ["2"], "taxi": []},
               {"roster_id": 7, "players": ["4"], "reserve": [], "taxi": []}]
    got = I.watchlist(rosters, 3, _table(), limit=10)
    assert got[:2] == ["1", "2"]          # mine first, IR slot included
    assert "4" not in got                  # owned by a rival
    assert "3" not in got                  # rank past FREE_AGENT_RANK
    assert "5" not in got                  # no team
    assert I.watchlist(rosters, 3, _table(), limit=1) == ["1"]


def test_diff_reports_activation_and_ignores_first_sighting_and_noise():
    table = _table()
    before = {"1": _row(), "2": _row(injury_status="IR", status="Inactive")}
    after = {"1": _row(), "2": _row(injury_status=None, status="Active"), "9": _row(injury_status="IR")}
    rows = I.diff(before, after, table, mine={"2"})
    assert [r["change"] for r in rows] == ["activated"]
    assert rows[0]["name"] == "Hurt Wideout" and rows[0]["was"] == "IR" and rows[0]["now"] == "Active" and rows[0]["mine"]


def test_diff_flags_new_injuries_practice_and_sorts_mine_first():
    table = _table()
    before = {"1": _row(), "2": _row(injury_status="IR", status="Inactive"), "4": _row()}
    after = {"1": _row(injury_status="Out", status="Inactive"),
             "2": _row(injury_status="IR", status="Inactive", practice_participation="Limited Participation"),
             "4": _row(injury_status="Questionable")}
    rows = I.diff(before, after, table, mine={"1"})
    kinds = {r["name"]: r["change"] for r in rows}
    assert kinds == {"Star Back": "sidelined", "Hurt Wideout": "practice", "Rival Guy": "downgraded"}
    assert rows[0]["name"] == "Star Back"  # mine sorts first even though it is bad news
    assert I.diff(before, before, table) == []
    # a designation that eases without clearing is an upgrade, not an activation
    easing = I.diff({"4": _row(injury_status="Doubtful")}, {"4": _row(injury_status="Questionable")}, table)
    assert [r["change"] for r in easing] == ["upgraded"]


def test_poll_skips_unreadable_players_instead_of_reporting_them_as_changes(monkeypatch):
    calls = []

    def fake_get(host, path, params=None, fresh=False, headers=None):
        calls.append(path)
        if path.endswith("/2"):
            raise api.NotFound(path)
        return {"injury_status": "IR", "status": "Inactive", "team": "KC", "depth_chart_order": 2, "extra": "dropped"}

    monkeypatch.setattr(I.api, "get", fake_get)
    now, failed = I.poll(["1", "2"])
    assert failed == ["2"] and list(now) == ["1"]
    assert set(now["1"]) == set(I.FIELDS)
    assert calls == ["players/nfl/1", "players/nfl/2"]


def test_snapshot_round_trips_and_survives_a_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(I, "snapshot_path", lambda lid: tmp_path / f"injuries.{lid}.json")
    assert I.load_snapshot("L1") == {} and I.snapshot_age_min("L1") is None
    I.save_snapshot("L1", {"1": _row()})
    assert I.load_snapshot("L1")["1"]["team"] == "KC"
    assert I.snapshot_age_min("L1") < 1
    (tmp_path / "injuries.L1.json").write_text("{not json")
    assert I.load_snapshot("L1") == {}
