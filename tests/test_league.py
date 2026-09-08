import json
import pathlib
from datetime import datetime

import pytest

from sleeper import league as L
from sleeper import players
from sleeper.players import _slim_row

FX = pathlib.Path(__file__).parent / "fixtures"
LEAGUE = "289646328504385536"
DYNASTY_LEAGUE = "1354612949834035200"
SNAKE_3RR_16 = "1388280410676432896"
LINEAR_ROOKIES = "1354612949850800128"
ME = "189140835533586432"
CO_OWNER = "476735150112768"


def load(name):
    return json.loads((FX / name).read_text())


def fixture_for(path):
    return "league_" + path.removeprefix("league/").replace("/", "_") + ".json"


def row(pid, name, pos, team, injury=None):
    return _slim_row(pid, {"full_name": name, "position": pos, "fantasy_positions": [pos], "team": team, "injury_status": injury, "status": "Active"})


def defense(abbr, city, nick):
    return _slim_row(abbr, {"first_name": city, "last_name": nick, "position": "DEF", "fantasy_positions": ["DEF"], "team": abbr})


TABLE = {r["player_id"]: r for r in [
    row("4881", "Starter QB", "QB", "BAL"),
    row("4035", "Starter RB1", "RB", "NO"),
    row("788", "Starter RB2", "RB", "MIA"),
    row("2133", "Starter WR1", "WR", "GB"),
    row("2449", "Starter WR2", "WR", "KC", "Questionable"),
    row("2118", "Starter TE", "TE", "TEN"),
    row("223", "Flex One", "WR", "NO"),
    row("421", "Bench QB", "QB", "ATL"),
    row("2319", "Bench WR", "WR", "WAS"),
    row("2078", "IR Back", "RB", "SEA", "IR"),
    row("3204", "Waiver Add", "WR", "PHI"),
    row("5010", "Rookie Add", "RB", "DEN"),
    row("4111", "Dropped Vet", "WR", "CHI"),
    row("3306", "Trade Piece", "RB", "LAC"),
    defense("CLE", "Cleveland", "Browns"),
    defense("PHI", "Philadelphia", "Eagles"),
]}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(host, path, params=None, fresh=False):
        seen.append(path)
        return load(fixture_for(path))

    monkeypatch.setattr(L.api, "get", fake_get)
    monkeypatch.setattr(players, "load", lambda: TABLE)
    return seen


@pytest.fixture
def ctx(calls):
    return L.context(LEAGUE)


def test_context_builds_the_team_table(ctx, calls):
    assert calls == [f"league/{LEAGUE}", f"league/{LEAGUE}/users", f"league/{LEAGUE}/rosters"]
    assert ctx["league"]["name"] == "Sleeper Friends League"
    assert len(ctx["users"]) == 14 and len(ctx["rosters"]) == 12
    assert sorted(ctx["teams"]) == list(range(1, 13))
    assert ctx["teams"][1] == {
        "roster_id": 1, "owner_id": ME, "owner": "progamer", "team_name": "Kamara is the new DJ",
        "record": "7-6", "fpts": 1776.06, "waiver_position": 4,
    }
    assert ctx["teams"][2]["fpts"] == 1736.02
    assert ctx["teams"][3]["record"] == "10-3"
    assert ctx["teams"][5]["team_name"] == "tobrepeels"


def test_team_row_handles_ties_and_missing_owner():
    t = L.team_row({"roster_id": 9, "owner_id": None, "settings": {"wins": 1, "losses": 1, "ties": 1, "fpts": 10, "fpts_decimal": 5}}, {})
    assert t["record"] == "1-1-1" and t["fpts"] == 10.05
    assert t["owner"] == "(no owner)" and t["team_name"] == "(no owner)"


def test_scoring_label_and_summary():
    assert L.scoring_label({"rec": 1.0}) == "PPR"
    assert L.scoring_label({"rec": 0.5}) == "HALF"
    assert L.scoring_label({"rec": 0.0}) == "STD"
    assert L.scoring_label({}) == "STD"
    assert L.scoring_summary(load(f"league_{LEAGUE}.json")["scoring_settings"]) == "PPR 1.0, 6pt passTD"
    s = {"rec": 0.5, "pass_td": 4.0, "bonus_rec_te": 0.5, "bonus_rush_yd_100": 3.0, "bonus_pass_yd_300": 0.0}
    assert L.scoring_summary(s) == "HALF 0.5, 4pt passTD, TE+0.5, rush_yd_100+3"


def test_roster_line_collapses_bench_and_abbreviates():
    assert L.roster_line(load(f"league_{LEAGUE}.json")["roster_positions"]) == "QB RB RB WR WR TE FLEX FLEX DEF BN×6"
    assert L.roster_line(["QB", "SUPER_FLEX", "REC_FLEX", "BN", "BN"]) == "QB SF RF BN×2"
    assert L.roster_line(["QB", "RB"]) == "QB RB"
    assert L.roster_line(["BN", "QB", "BN"]) == "BN QB BN×1"


def test_format_line_with_and_without_a_draft():
    league = load(f"league_{LEAGUE}.json")
    base = "12-team | PPR 1.0, 6pt passTD | QB RB RB WR WR TE FLEX FLEX DEF BN×6"
    assert L.format_line(league) == base
    assert L.format_line(league, load(f"draft_{SNAKE_3RR_16}.json")) == base.replace("12-team", "12-team snake 3RR") + " | 10s clock"
    assert L.format_line(dict(league, settings=dict(league["settings"], type=1))) == base + " | KEEPER"
    dynasty = L.format_line(load(f"league_{DYNASTY_LEAGUE}.json"), load(f"draft_{LINEAR_ROOKIES}.json"))
    assert dynasty == "12-team linear | STD 0.0, 6pt passTD | QB RB RB WR WR TE FLEX FLEX K DEF BN×8 | no clock | DYNASTY | ROOKIES ONLY"


def test_league_summary():
    league = load(f"league_{LEAGUE}.json")
    assert L.league_summary(league) == {
        "league_id": LEAGUE, "name": "Sleeper Friends League", "teams": 12, "type": "redraft", "scoring": "PPR",
        "superflex": False, "te_premium": False, "status": "complete", "draft_id": "289646328508579840",
    }
    odd = dict(league, settings=dict(league["settings"], type=3), roster_positions=["QB", "SUPER_FLEX"],
               scoring_settings=dict(league["scoring_settings"], rec=0.5, bonus_rec_te=1.0))
    s = L.league_summary(odd)
    assert (s["type"], s["scoring"], s["superflex"], s["te_premium"]) == ("type_3", "HALF", True, True)


def test_find_roster_by_none_int_and_substring(ctx):
    assert L.find_roster(ctx, None, ME) == 1
    assert L.find_roster(ctx, None, CO_OWNER) == 2
    with pytest.raises(L.SleeperError, match="you are not in league Sleeper Friends League"):
        L.find_roster(ctx, None, "nobody")
    assert L.find_roster(ctx, 3, ME) == 3
    assert L.find_roster(ctx, "3", ME) == 3
    with pytest.raises(L.SleeperError, match="no roster 99"):
        L.find_roster(ctx, 99, ME)
    assert L.find_roster(ctx, "dolphins", ME) == 2
    assert L.find_roster(ctx, "PROGAMER", ME) == 1


def test_find_roster_ambiguous_or_missing_lists_candidates(ctx):
    with pytest.raises(L.SleeperError, match="several teams") as e:
        L.find_roster(ctx, "o", ME)
    assert "Giant Dolphins (2KSports)" in str(e.value) and "Pickle Rick Mahomes (deshine)" in str(e.value)
    with pytest.raises(L.SleeperError, match="no team") as e:
        L.find_roster(ctx, "zzz", ME)
    assert str(e.value).count("(") == 12


def test_roster_view_slot_order_and_fallbacks(ctx):
    v = L.roster_view(ctx, 1)
    assert (v["team"], v["owner"], v["record"]) == ("Kamara is the new DJ", "progamer", "7-6")
    assert [s["slot"] for s in v["starters"]] == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "DEF"]
    assert [s["pid"] for s in v["starters"]] == ctx["rosters"][0]["starters"]
    assert v["starters"][0]["name"] == "Starter QB"
    assert v["starters"][4]["injury"] == "Questionable"
    assert v["starters"][7] == {"slot": "FLEX", "pid": "1352", "name": "#1352", "pos": None, "team": None, "injury": None}
    assert v["starters"][8] == {"slot": "DEF", "pid": "CLE", "name": "Cleveland Browns", "pos": "DEF", "team": "CLE", "injury": None}
    assert len(v["bench"]) == 6 and not {b["pid"] for b in v["bench"]} & set(ctx["rosters"][0]["starters"])
    assert [b["name"] for b in v["bench"][:2]] == ["Bench QB", "Bench WR"]
    assert v["reserve"] == [] and v["taxi"] == []


def test_roster_view_reserve_and_empty_slot(ctx):
    v = L.roster_view(ctx, 2)
    assert v["team"] == "Giant Dolphins" and v["record"] == "6-7"
    assert v["reserve"] == [{"pid": "2078", "name": "IR Back", "pos": "RB", "team": "SEA", "injury": "IR"}]
    assert "2078" not in {b["pid"] for b in v["bench"]}
    ctx["rosters"][0]["starters"][2] = "0"
    empty = L.roster_view(ctx, 1)["starters"][2]
    assert empty == {"slot": "RB", "pid": None, "name": "(empty)", "pos": None, "team": None, "injury": None}
    with pytest.raises(L.SleeperError, match="no roster 42"):
        L.roster_view(ctx, 42)


def test_matchups_pairing_and_detail(ctx, calls):
    rows = L.matchups_view(ctx, 1)["matchups"]
    assert calls[-1] == f"league/{LEAGUE}/matchups/1"
    assert len(rows) == 6 and [r["matchup"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[1] == {"matchup": 2, "home": "Kamara is the new DJ", "home_pts": 148.04, "away": "🔥Gordon x Gordon 🔥", "away_pts": 146.52}
    starters = [s for s in L.matchups_view(ctx, 1, detail=True)["starters"] if s["matchup"] == 2 and s["team"] == "Kamara is the new DJ"]
    assert len(starters) == 9
    assert starters[1] == {"matchup": 2, "team": "Kamara is the new DJ", "name": "Starter RB1", "pos": "RB", "pts": 43.1}
    assert starters[8]["name"] == "Philadelphia Eagles" and starters[8]["pts"] == 11.0
    assert starters[2]["name"] == "#3242"


def test_matchups_bye_rows(ctx, monkeypatch):
    fixture = load(f"league_{LEAGUE}_matchups_1.json")
    monkeypatch.setattr(L.api, "get", lambda host, path, params=None, fresh=False: [
        dict(m, matchup_id=None) if m["roster_id"] in (9, 12) else m for m in fixture
    ])
    rows = L.matchups_view(ctx, 1)["matchups"]
    assert [r.get("matchup") for r in rows] == [1, 2, 3, 4, 6, None, None]
    assert rows[-2] == {"matchup": None, "home": "🍿🏖🍻", "home_pts": 147.16, "away": None, "away_pts": None, "bye": True}


def test_transactions_newest_first_with_names(ctx, monkeypatch):
    fixture = load(f"league_{LEAGUE}_transactions_1.json")
    monkeypatch.setattr(L.api, "get", lambda host, path, params=None, fresh=False: list(reversed(fixture)))
    rows = L.transactions_view(ctx, 1)
    assert len(rows) == 25
    assert rows[0] == {
        "type": "waiver", "status": "failed", "by": "Giant Dolphins",
        "adds": "Waiver Add (WR) -> Giant Dolphins", "drops": None, "faab": None, "picks": None,
        "when": datetime.fromtimestamp(1536735185478 / 1000).date().isoformat(),
    }
    assert rows[3] == {
        "type": "waiver", "status": "complete", "by": "Not saved by the Bell",
        "adds": "Rookie Add (RB) -> Not saved by the Bell", "drops": "Dropped Vet (WR) from Not saved by the Bell",
        "faab": 11, "picks": None, "when": datetime.fromtimestamp(1536731858504 / 1000).date().isoformat(),
    }


def test_transactions_filter_limit_and_trades(ctx):
    trades = L.transactions_view(ctx, 1, type="trade")
    assert len(trades) == 4 and {t["type"] for t in trades} == {"trade"}
    assert trades[-1]["by"] == "Kamara is the new DJ / Giant Dolphins"
    assert trades[-1]["adds"] == "Starter WR2 (WR) -> Kamara is the new DJ, Trade Piece (RB) -> Giant Dolphins"
    assert trades[-1]["drops"] == "Starter WR2 (WR) from Giant Dolphins, Trade Piece (RB) from Kamara is the new DJ"
    assert len(L.transactions_view(ctx, 1, type="free_agent", limit=200)) == 69
    assert len(L.transactions_view(ctx, 1, limit=3)) == 3


def test_transactions_count_draft_picks(ctx, monkeypatch):
    fixture = load(f"league_{LEAGUE}_transactions_1.json")
    trade = dict(fixture[0], type="trade", roster_ids=[1, 2], adds=None, drops=None, draft_picks=[{}, {}], settings=None)
    monkeypatch.setattr(L.api, "get", lambda host, path, params=None, fresh=False: [trade])
    assert L.transactions_view(ctx, 1) == [{
        "type": "trade", "status": "failed", "by": "Kamara is the new DJ / Giant Dolphins",
        "adds": None, "drops": None, "faab": None, "picks": 2,
        "when": datetime.fromtimestamp(1536735185478 / 1000).date().isoformat(),
    }]
