import json
import pathlib

import pytest

from sleeper import draft as D
from sleeper import players

FX = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text())


SNAKE_3RR_16 = "1388280410676432896"
LINEAR_TRADED = "1354612949850800128"
KEEPERS = "1389736356023918593"
CPU_3RR_8 = "1385745991830900736"
NO_ORDER = "1395201566562082816"


@pytest.mark.parametrize("did", [SNAKE_3RR_16, LINEAR_TRADED, KEEPERS, CPU_3RR_8])
def test_slot_formula_matches_every_filled_pick(did):
    d = load(f"draft_{did}.json")
    picks = load(f"draft_{did}_picks.json")
    assert picks
    for p in picks:
        assert D._slot(d, p["pick_no"]) == (p["round"], p["draft_slot"]), p["pick_no"]


def test_reversal_round_flips_direction_from_that_round_on():
    assert [D.slot_for_pick(n, 4, "snake", 3)[1] for n in (1, 4, 5, 8, 9, 12, 13, 16)] == [1, 4, 4, 1, 4, 1, 1, 4]
    assert [D.slot_for_pick(n, 4, "snake", 0)[1] for n in (1, 4, 5, 8, 9, 12)] == [1, 4, 4, 1, 1, 4]
    assert [D.slot_for_pick(n, 4, "linear", 0)[1] for n in (1, 4, 5, 8)] == [1, 4, 1, 4]


def test_keeper_picks_leave_gaps_in_unfilled():
    d = load(f"draft_{KEEPERS}.json")
    picks = load(f"draft_{KEEPERS}_picks.json")
    keepers = {p["pick_no"] for p in picks}
    un = D.unfilled(d, picks)
    assert un[0] == 1
    assert not keepers & set(un)
    assert len(un) + len(keepers) == D.total_picks(d)


def test_complete_draft_has_no_unfilled_picks():
    d = load(f"draft_{SNAKE_3RR_16}.json")
    assert D.unfilled(d, load(f"draft_{SNAKE_3RR_16}_picks.json")) == []


def test_traded_pick_changes_the_picker():
    d = load(f"draft_{LINEAR_TRADED}.json")
    tmap = D.traded_map(d, load(f"draft_{LINEAR_TRADED}_traded_picks.json"))
    assert len(tmap) == 3
    teams = d["settings"]["teams"]
    for (rnd, original), owner in tmap.items():
        pick_no = (rnd - 1) * teams + D.slot_of_roster(d, original)
        assert D.picker(d, pick_no, tmap)[0] == owner
        assert D.picker(d, pick_no, {})[0] == original


def test_my_next_pick_skips_keeper_filled_slots():
    d = load(f"draft_{KEEPERS}.json")
    picks = load(f"draft_{KEEPERS}_picks.json")
    keepers = {p["pick_no"] for p in picks}
    un = D.unfilled(d, picks)
    for user_id in d["draft_order"]:
        rid = D.roster_of_user(d, user_id)
        mine, away = D.my_next(d, picks, {}, rid)
        assert mine not in keepers
        assert un[away] == mine
        assert D.picker(d, mine, {})[0] == rid


def test_cpu_slots_get_labels():
    d = load(f"draft_{CPU_3RR_8}.json")
    lab = D.labels(d, {uid: {"display_name": f"human{i}"} for i, uid in enumerate(d["draft_order"])})
    assert len(lab) == 8
    assert sum(v.endswith("(CPU)") for v in lab.values()) == 8 - len(d["draft_order"])
    assert sum(v.startswith("human") for v in lab.values()) == len(d["draft_order"])


def test_roster_of_user_falls_back_to_league_rosters():
    d = {"draft_order": None, "slot_to_roster_id": {"1": 1}}
    rosters = [{"roster_id": 3, "owner_id": "a", "co_owners": ["b"]}]
    assert D.roster_of_user(d, "b", rosters) == 3
    assert D.roster_of_user(d, "a", rosters) == 3
    assert D.roster_of_user(d, "zzz", rosters) is None


def test_no_draft_order_raises_a_clear_error_for_pickers_but_status_still_works():
    d = load(f"draft_{NO_ORDER}.json")
    d2 = dict(d, slot_to_roster_id=None)
    with pytest.raises(D.SleeperError):
        D.picker(d2, 1, {})
    st = D.status(d, [], [], "nobody")
    assert st["next_pick"] == 1 and st["note"] == "you are not in this draft"


def test_clock_states():
    base = {"settings": {"pick_timer": 120}, "status": "drafting", "last_picked": 1_000_000, "start_time": 0, "metadata": {}}
    assert D.clock(base, now=1_000 + 30) == "~90 s (est)"
    assert D.clock(base, now=1_000 + 200) == "overdue/stale"
    assert D.clock(dict(base, status="paused"), now=1_030) == "paused"
    assert D.clock(dict(base, metadata={"is_autopaused": "true"}), now=1_030) == "paused"
    assert D.clock(dict(base, settings={"pick_timer": 0}), now=1_030) == "no timer"
    assert D.clock(dict(base, status="pre_draft"), now=1_030) == "pre_draft"
    assert D.clock(dict(base, type="auction", metadata={"timer_end_at": "1060000"}), now=1_000) == "60 s"


def test_needs_fills_flex_from_surplus():
    d = {"settings": {"slots_qb": 1, "slots_rb": 2, "slots_wr": 2, "slots_te": 1, "slots_flex": 2, "slots_k": 1, "slots_def": 1, "slots_bn": 5, "rounds": 15}}
    n = D.needs(d, ["RB", "RB", "WR", "RB"])
    assert n == {"picks_left": 11, "starters_open": {"QB": 1, "WR": 1, "TE": 1, "K": 1, "DEF": 1}, "flex_open": {"flex": 1}, "bench_open": 5}
    assert D.startable(d) == {"QB", "RB", "WR", "TE", "K", "DEF"}
    assert "K" not in D.startable({"settings": {"slots_qb": 1, "slots_flex": 1}})


def test_status_midway_through_a_3rr_draft():
    d = load(f"draft_{CPU_3RR_8}.json")
    picks = [p for p in load(f"draft_{CPU_3RR_8}_picks.json") if p["pick_no"] <= 20]
    user_id = next(iter(d["draft_order"]))
    st = D.status(dict(d, status="drafting"), picks, [], user_id, now=d["last_picked"] / 1000 + 10)
    assert st["next_pick"] == 21 and st["round"] == 3
    assert f"slot {D._slot(d, 21)[1]}" in st["on_the_clock"]
    assert st["my_next_pick"] > 20 and st["picks_away"] == st["my_next_pick"] - 21
    assert st["my_slot"] == d["draft_order"][user_id]


def test_pool_filters_drafted_rostered_positions_and_rookies(monkeypatch):
    rows = [
        {"player_id": "1", "position": "RB", "years_exp": 0},
        {"player_id": "2", "position": "RB", "years_exp": 3},
        {"player_id": "3", "position": "K", "years_exp": 3},
        {"player_id": "4", "position": "WR", "years_exp": 1},
        {"player_id": "5", "position": "WR", "years_exp": 0},
    ]
    monkeypatch.setattr(players, "pool", lambda: rows)
    d = {"settings": {"slots_rb": 1, "slots_wr": 1, "player_type": 0}}
    picks = [{"player_id": "4"}]
    assert [r["player_id"] for r in D.pool(d, picks, rostered={"2"})] == ["1", "5"]
    assert [r["player_id"] for r in D.pool(dict(settings=dict(d["settings"], player_type=1)), [], set())] == ["1", "5"]
    assert [r["player_id"] for r in D.pool(dict(settings=dict(d["settings"], player_type=2)), [], set())] == ["2", "4"]


def test_picks_since_excludes_keepers_and_respects_cursor():
    picks = [
        {"pick_no": 3, "round": 1, "roster_id": 1, "is_keeper": True, "metadata": {"first_name": "K", "last_name": "Eeper", "position": "RB", "team": "KC"}},
        {"pick_no": 1, "round": 1, "roster_id": 2, "metadata": {"first_name": "A", "last_name": "One", "position": "WR", "team": "SF"}},
        {"pick_no": 2, "round": 1, "roster_id": 3, "metadata": {"first_name": "B", "last_name": "Two", "position": "QB", "team": "BUF"}},
    ]
    rows = D.picks_since(picks, 2, {3: "me"})
    assert [r["name"] for r in rows] == ["B Two"] and rows[0]["by"] == "me"
    unflagged = [dict(p, is_keeper=None) for p in picks]
    assert [r["name"] for r in D.picks_since(unflagged, 1, {}, next_pick_no=3)] == ["A One", "B Two"]
    assert D.pick_row(unflagged[0], {}, next_pick_no=3).get("keeper") is True
    assert [p["pick_no"] for p in D.live_picks(unflagged, None)] == [1, 2, 3]


def test_poller_ends_on_my_turn_and_returns_picks_since(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "keeper_file", lambda did: tmp_path / "keepers.json")
    d = load(f"draft_{CPU_3RR_8}.json")
    all_picks = sorted(load(f"draft_{CPU_3RR_8}_picks.json"), key=lambda p: p["pick_no"])
    user_id = next(iter(d["draft_order"]))
    rid = D.roster_of_user(d, user_id)
    first_mine = next(p["pick_no"] for p in all_picks if p["roster_id"] == rid)
    calls = {"n": 0}

    def fake_get(host, path, params=None, fresh=False):
        if path.endswith("/picks"):
            calls["n"] += 1
            return all_picks[: first_mine - 3 + calls["n"] - 1]
        if path.endswith("/traded_picks"):
            return []
        return dict(d, status="drafting")

    monkeypatch.setattr(D.api, "get", fake_get)
    poller = D.Poller(d["draft_id"], user_id, picks_away=1, since_pick=1)
    results = [poller.step() for _ in range(5)]
    done = [r for r in results if r]
    assert done and done[0]["reason"] == "my_turn"
    assert done[0]["status"]["picks_away"] <= 1
    assert done[0]["picks_since"] and done[0]["picks_since"][0]["pick"] >= 1


def test_keepers_stay_keepers_after_the_draft_passes_them(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "keeper_file", lambda did: tmp_path / "keepers.json")
    d = load(f"draft_{KEEPERS}.json")
    keepers = [dict(p, is_keeper=None) for p in load(f"draft_{KEEPERS}_picks.json")]
    keeper_nos = {p["pick_no"] for p in keepers}
    remembered = D.remember_keepers(d["draft_id"], keepers, 1)
    assert remembered == frozenset(keeper_nos)
    live = [{"pick_no": n, "round": (n - 1) // 12 + 1, "draft_slot": 1, "roster_id": 1, "player_id": str(n), "is_keeper": None,
             "metadata": {"first_name": "Live", "last_name": str(n), "position": "RB", "team": "KC"}}
            for n in range(1, 31) if n not in keeper_nos]
    picks = keepers + live
    nxt = D.unfilled(d, picks)[0]
    assert nxt == 31
    later = D.remember_keepers(d["draft_id"], picks, nxt)
    assert later == remembered
    since = D.picks_since(picks, 1, {}, limit=40, next_pick_no=nxt, keeper_nos=later)
    assert [r["pick"] for r in since] == [p["pick_no"] for p in live]
    assert D.pick_row(keepers[0], {}, nxt, later).get("keeper") is True
    assert D.status(d, picks, [], None, keeper_nos=later)["picks_made"] == len(live)
    assert D.status(d, picks, [], None, keeper_nos=later)["keepers"] == len(keepers)


def test_picks_since_marks_truncation():
    picks = [{"pick_no": n, "round": 1, "draft_slot": n, "roster_id": 1, "metadata": {"first_name": "P", "last_name": str(n), "position": "RB", "team": "KC"}} for n in range(1, 11)]
    rows = D.picks_since(picks, 1, {}, limit=4, next_pick_no=11)
    assert len(rows) == 5 and rows[0]["name"].startswith("... 6 earlier picks not shown (draft picks --last 10)")
    assert [r["pick"] for r in rows[1:]] == [7, 8, 9, 10]


def test_poller_keeps_polling_while_the_order_is_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "keeper_file", lambda did: tmp_path / "keepers.json")
    d = dict(load(f"draft_{NO_ORDER}.json"), status="pre_draft", slot_to_roster_id=None)
    monkeypatch.setattr(D.api, "get", lambda host, path, params=None, fresh=False: [] if path.endswith("picks") else d)
    poller = D.Poller(d["draft_id"], "nobody", picks_away=1)
    assert poller.step() is None
    assert poller.status()["note"] == "draft order not set yet"


def test_mock_picks_without_roster_id_are_labelled_by_slot():
    d = {"slot_to_roster_id": {"1": 1, "2": 2}, "draft_order": {"u1": 2}}
    slot_lab = D.slot_labels(d, {"u1": {"display_name": "ishan"}})
    assert slot_lab == {1: "slot 1 (CPU)", 2: "ishan"}
    p = {"pick_no": 1, "round": 1, "draft_slot": 2, "roster_id": None, "metadata": {"first_name": "A", "last_name": "B", "position": "RB", "team": "KC"}}
    assert D.pick_row(p, {}, slot_lab=slot_lab)["by"] == "ishan"
    assert D.pick_row(dict(p, draft_slot=1), {}, slot_lab=slot_lab)["by"] == "slot 1 (CPU)"


def test_poller_does_not_re_report_the_pick_it_already_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "keeper_file", lambda did: tmp_path / "keepers.json")
    d = dict(load(f"draft_{CPU_3RR_8}.json"), status="drafting")
    all_picks = sorted(load(f"draft_{CPU_3RR_8}_picks.json"), key=lambda p: p["pick_no"])
    user_id = next(iter(d["draft_order"]))
    rid = D.roster_of_user(d, user_id)
    first_mine = next(p["pick_no"] for p in all_picks if p["roster_id"] == rid)
    on_clock = all_picks[: first_mine - 1]
    monkeypatch.setattr(D.api, "get", lambda host, path, params=None, fresh=False: on_clock if path.endswith("/picks") else [] if path.endswith("traded_picks") else d)
    assert D.Poller(d["draft_id"], user_id, picks_away=1, since_pick=first_mine).step() is None
    assert D.Poller(d["draft_id"], user_id, picks_away=1, since_pick=0).step()["reason"] == "my_turn"
    # one pick earlier: the CPU ahead of me is on the clock; a cursor at that CPU pick must not suppress my turn
    on_clock[:] = all_picks[: first_mine - 2]
    assert D.Poller(d["draft_id"], user_id, picks_away=1, since_pick=first_mine - 1).step()["reason"] == "my_turn"
    assert D.Poller(d["draft_id"], user_id, picks_away=1, since_pick=first_mine).step() is None


def test_is_mine_falls_back_to_slot_for_mock_picks():
    assert D.is_mine({"roster_id": 3, "draft_slot": 9}, 3, 7)
    assert not D.is_mine({"roster_id": 4, "draft_slot": 7}, 3, 7)
    assert D.is_mine({"roster_id": None, "draft_slot": 7}, 3, 7)
    assert not D.is_mine({"roster_id": None, "draft_slot": 7}, 3, None)
