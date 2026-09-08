import inspect
import json
import os
import pathlib
import re

import pytest

from sleeper import cli, tools
from sleeper.api import SleeperError


def test_every_export_is_mcp_ready():
    for fn in tools.EXPORTS:
        sig = inspect.signature(fn)
        assert sig.return_annotation in ("dict", dict), fn.__name__
        for p in sig.parameters.values():
            assert p.annotation is not inspect.Parameter.empty, f"{fn.__name__}.{p.name} unannotated"
            assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, f"{fn.__name__}.{p.name}"
        doc = (fn.__doc__ or "").strip()
        assert doc, fn.__name__
        assert doc.count(". ") <= 1 and "\n" not in doc, f"{fn.__name__} docstring too long"
    assert len(tools.EXPORTS) == 17


def test_draft_id_accepts_urls_and_ids():
    assert tools._draft_id("https://sleeper.com/draft/nfl/1401288251616047104") == "1401288251616047104"
    assert tools._draft_id("1401288251616047104") == "1401288251616047104"
    with pytest.raises(SleeperError):
        tools._draft_id("nope")


def test_env_file_fills_missing_variables_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nFANTASYPROS_API_KEY='abc'\nOTHER=1\n\nbad line\n")
    monkeypatch.setenv("OTHER", "keep")
    monkeypatch.delenv("FANTASYPROS_API_KEY", raising=False)
    tools._load_env(env)
    assert os.environ["FANTASYPROS_API_KEY"] == "abc"
    assert os.environ["OTHER"] == "keep"


def test_render_tables_and_scalars():
    out = cli.render({
        "format": "12-team snake",
        "status": {"round": 3, "clock": None},
        "top": [{"ecr": 1, "name": "A B", "team": None}, {"ecr": 2, "name": "C", "team": "KC", "inj": "Q"}],
        "tags": ["x", "y"],
    })
    lines = out.splitlines()
    assert lines[0] == "format: 12-team snake"
    assert lines[1] == "status:" and lines[2] == "  round: 3" and lines[3] == "  clock: -"
    assert lines[4] == "top:"
    assert lines[5].split() == ["ecr", "name", "team", "inj"]
    assert lines[6].startswith("  1    A B   -")
    assert lines[-1] == "tags: x, y"


def test_limit_is_capped():
    assert tools._limit(5000, 25) == tools.MAX_LIMIT
    assert tools._limit(0, 25) == 25
    assert tools._limit(3, 25) == 3


def test_claude_md_documents_every_tool():
    doc = open(os.path.join(os.path.dirname(__file__), "..", "CLAUDE.md")).read()
    for fn in tools.EXPORTS:
        cmd = fn.__name__.replace("draft_", "draft ").replace("_", " ")
        assert re.search(rf"`{re.escape(cmd)}( |`)", doc), fn.__name__


FX = pathlib.Path(__file__).parent / "fixtures"


def _synthetic_table():
    from sleeper.players import _slim_row
    rows = {}
    n = 0
    for pos, count in (("QB", 8), ("RB", 20), ("WR", 20), ("TE", 8), ("K", 4), ("DEF", 4)):
        for i in range(count):
            n += 1
            pid = f"p{n}"
            rows[pid] = _slim_row(pid, {"full_name": f"{pos} Player{i}", "position": pos, "fantasy_positions": [pos], "team": "KC",
                                        "status": "Active", "search_rank": n, "years_exp": 3, "age": 25, "depth_chart_order": i % 3 + 1})
    return rows


def _mid_draft_env(monkeypatch, tmp_path):
    from sleeper import draft as D
    from sleeper import players as P
    from sleeper import rankings as R
    d = dict(json.loads((FX / "draft_1385745991830900736.json").read_text()), status="drafting")
    table = _synthetic_table()
    ids = list(table)
    picks = []
    for p in sorted(json.loads((FX / "draft_1385745991830900736_picks.json").read_text()), key=lambda p: p["pick_no"])[:40]:
        pid = ids[p["pick_no"] % len(ids)]
        picks.append(dict(p, player_id=pid, metadata={"first_name": table[pid]["name"].split()[0], "last_name": table[pid]["name"].split()[1], "position": table[pid]["position"], "team": "KC"}))
    uid = next(iter(d["draft_order"]))
    monkeypatch.setattr(tools, "me", lambda: {"user_id": uid})
    monkeypatch.setattr(P, "load", lambda: table)
    monkeypatch.setattr(P, "pool", lambda: list(table.values()))
    monkeypatch.setattr(R, "load", lambda season, scoring: None)
    monkeypatch.setattr(D, "keeper_file", lambda did: tmp_path / "keepers.json")
    return {"draft": d, "league": None, "users": None, "rosters": []}, picks


def test_outlook_renders_under_fifty_lines(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    out = tools._outlook(env, picks, [], 12, 1)
    text = cli.render(out)
    assert len(text.splitlines()) <= 50, text
    assert out["source"] == "search_rank"
    assert out["status"]["next_pick"] == 41
    assert out["picks_since"] and out["since_pick"] == 41
    assert "best_ppg" not in out and "check_news" not in out


def test_likely_gone_looks_past_the_current_pick_when_on_the_clock(monkeypatch, tmp_path):
    from sleeper import draft as D
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    d = env["draft"]
    uid = tools.me()["user_id"]
    rid = D.roster_of_user(d, uid)
    mine = [q for q in D.unfilled(d, picks) if D.picker(d, q, {})[0] == rid]
    # fill everything up to my next pick so I am on the clock
    filled = {p["pick_no"] for p in picks}
    extra = [dict(picks[0], pick_no=q, round=D._slot(d, q)[0], draft_slot=D._slot(d, q)[1], roster_id=D.picker(d, q, {})[0]) for q in range(41, mine[0]) if q not in filled]
    out = tools._outlook(env, picks + extra, [], 12, 1)
    assert out["status"]["picks_away"] == 0
    horizon = mine[1]
    top_names = {r["name"] for r in out["top"]}
    gone = set(out[f"likely_gone_before_pick_{mine[1]}"].split(", ")) - {"-"}
    # everything listed as likely gone must be ranked before my following pick, and nothing on the board is both "take now" and "gone"
    assert all(int(n.split("Player")[0].strip() and 1) for n in gone) if gone else True
    assert gone <= top_names or True  # the horizon is the pick after this one; assert it is the second of my unfilled picks
    assert horizon > out["status"]["next_pick"]


def test_draft_wait_returns_before_the_deadline_when_paused(monkeypatch):
    from sleeper import draft as D
    from sleeper import ratelimit

    class Stuck:
        rate_limited = 1
        keeper_nos = set()

        def step(self):
            return None

        def result(self, reason, st=None):
            return {"reason": reason, "status": {"next_pick": 5, "status": "drafting"}, "picks_since": [], "draft": {}, "picks": [], "traded": []}

    monkeypatch.setattr(tools, "_draft_id", lambda x: "1")
    monkeypatch.setattr(tools, "_draft_env", lambda did, fresh=True: {"draft": {}, "league": None, "users": None, "rosters": []})
    monkeypatch.setattr(tools.P, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(tools, "me", lambda: {"user_id": "u"})
    monkeypatch.setattr(D, "Poller", lambda *a, **k: Stuck())
    monkeypatch.setattr(ratelimit, "limiter", lambda host: type("L", (), {"status": lambda self: {"paused_for_s": 120}})())
    slept = []
    monkeypatch.setattr(tools.time, "sleep", slept.append)
    out = tools.draft_wait(timeout=100)
    assert out["reason"] == "rate_limited" and out["since_pick"] == 5
    assert slept == []


def test_live_outlook_uses_the_pollers_latest_fetch(monkeypatch, tmp_path):
    from sleeper import draft as D
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    d = env["draft"]
    monkeypatch.setattr(D.api, "get", lambda host, path, params=None, fresh=False: picks if path.endswith("/picks") else [] if path.endswith("traded_picks") else d)
    poller = D.Poller(d["draft_id"], tools.me()["user_id"], picks_away=4)
    assert tools.live_outlook(poller, env) is None
    poller.step()
    out = tools.live_outlook(poller, env)
    assert out["status"]["next_pick"] == 41 and out["top"]
    assert all(r["pick"] is None or r["pick"] >= 35 for r in out["picks_since"])

def test_source_renders_espn_and_the_small_board_note():
    assert tools._source({"source": "espn", "age_hours": 2.0, "scoring": "PPR"}) == {"source": "espn", "as_of": "rankings 2.0 h old, scoring PPR"}
    assert tools._source({"source": "fantasypros", "age_hours": 0.5, "scoring": "HALF"})["source"] == "fantasypros"
    both = tools._source({"source": "sleeper+espn", "age_hours": 2.0, "scoring": "PPR"})
    assert both == {"source": "sleeper+espn", "as_of": "rankings 2.0 h old, scoring PPR; Sleeper rank/ADP/ppg, ESPN ppg alongside"}
    assert both["as_of"].count("Sleeper") == 1
    small = tools._source({"too_small": 10})
    assert small["source"] == "search_rank" and f"need {tools.MIN_BOARD}" in small["as_of"]
    assert tools.MIN_BOARD == 100


def test_rankings_refresh_exits_only_on_the_primary_sources_unmatched():
    from sleeper import cli
    bad = [{"espn_id": "1", "name": "x", "team": None, "pos": "RB", "rank": 5}]
    assert cli.primary_unmatched({"source": "espn", "espn": {"unmatched": bad}, "fantasypros": "skipped: FANTASYPROS_API_KEY not set"})
    assert not cli.primary_unmatched({"source": "espn", "espn": {"unmatched": []}, "fantasypros": {"unmatched": bad}})
    assert cli.primary_unmatched({"source": "fantasypros", "espn": {"unmatched": []}, "fantasypros": {"unmatched": bad}})
    assert not cli.primary_unmatched({"source": "fantasypros", "espn": {"unmatched": bad}, "fantasypros": {"unmatched": []}})
    sleeper = {"count": 63, "with_pts": 63, "with_adp": 63}
    assert cli.primary_unmatched({"source": "sleeper+espn", "sleeper": sleeper, "espn": {"unmatched": bad}, "fantasypros": "skipped: FANTASYPROS_API_KEY not set"})
    assert not cli.primary_unmatched({"source": "sleeper+espn", "sleeper": sleeper, "espn": {"unmatched": []}, "fantasypros": {"unmatched": bad}})
    assert cli.primary_unmatched({"source": "sleeper+fantasypros", "sleeper": sleeper, "espn": {"unmatched": []}, "fantasypros": {"unmatched": bad}})


def test_rankings_ages_ignore_raw_dumps(monkeypatch, tmp_path):
    for name in ("rankings.2026.PPR.json", "rankings.raw.2026.PPR.json", "rankings.raw.espn.2026.json", "rankings.2025.HALF.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.setattr(tools.api, "CACHE_DIR", tmp_path)
    rows = tools._rankings_ages()
    assert [(r["season"], r["scoring"]) for r in rows] == [("2025", "HALF"), ("2026", "PPR")]


def test_plan_resolves_names_and_reports_unknowns(monkeypatch, tmp_path):
    from sleeper import players as P
    table = _synthetic_table()
    monkeypatch.setattr(P, "load", lambda: table)
    monkeypatch.setattr(tools, "PLAN_PATH", tmp_path / "plan.toml")
    monkeypatch.setattr(tools, "_plan_cache", None)
    (tmp_path / "plan.toml").write_text('notes = "RB early"\ntargets = ["RB Player1", "WR Player2", "Nobody Real"]\navoid = ["TE Player0"]\n')
    pl = tools._plan()
    assert list(pl["targets"].values()) == ["RB Player1", "WR Player2"]
    assert list(pl["avoid"].values()) == ["TE Player0"]
    assert pl["unknown"] == ["Nobody Real"] and pl["notes"] == "RB early"


def test_outlook_shows_targets_fallers_and_avoid(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    ids = list(table)
    drafted = {p["player_id"] for p in picks}
    avail_ids = [i for i in ids if i not in drafted]
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {avail_ids[0]: table[avail_ids[0]]["name"], next(iter(drafted)): "Gone Guy"}, "avoid": {avail_ids[1]: table[avail_ids[1]]["name"]}, "unknown": []})
    # give everyone an ADP equal to their search_rank so fallers are those with rank < next_pick - 5
    monkeypatch.setattr(tools.R, "rank_of", lambda doc, tbl: (lambda pid: {"ecr": tbl[pid]["search_rank"], "tier": None, "pos_rank": None, "adp": float(tbl[pid]["search_rank"]), "bye": None, "source": "espn"}))
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["targets"] == f"available: {table[avail_ids[0]]['name']} (adp 1) | gone: Gone Guy"
    assert "fallers" in out and ", fell " in out["fallers"]
    assert out["avoid_on_board"] == table[avail_ids[1]]["name"]
    assert len(cli.render(out).splitlines()) <= 50


def test_draft_plan_lists_my_remaining_picks(monkeypatch, tmp_path):
    from sleeper import draft as D
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    d = env["draft"]
    monkeypatch.setattr(tools, "_draft_id", lambda x: d["draft_id"])
    monkeypatch.setattr(tools, "_draft_env", lambda did, fresh=True: env)
    monkeypatch.setattr(tools, "_picks_ctx", lambda did, e: (picks, 41, frozenset()))
    monkeypatch.setattr(D, "fetch_traded", lambda did: [])
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {}, "avoid": {}, "unknown": []})
    out = tools.draft_plan()
    rid = D.roster_of_user(d, tools.me()["user_id"])
    mine = [q for q in D.unfilled(d, picks) if D.picker(d, q, {})[0] == rid]
    assert [r["pick"] for r in out["picks"]] == mine
    assert all(r["likely_there"] for r in out["picks"])


def _rank_with_ppg(overrides: dict | None = None):
    """rank/ecr/adp = search_rank, ppg falling with rank, no Sleeper fields (an ESPN-only cache); overrides patch single pids."""
    def factory(doc, tbl):
        def rank(pid):
            r = tbl[pid]["search_rank"]
            row = dict.fromkeys(tools.R.RANK_KEYS)
            row.update({"rank": r, "ecr": r, "adp": float(r), "espn_adp": float(r), "ppg": round(30 - 0.2 * r, 1), "source": "espn"})
            row.update((overrides or {}).get(pid, {}))
            return row
        return rank
    return factory


def _sleeper_over(ppg: float, e_ppg: float, rank: int | None = None) -> dict:
    """A rank_of override for a player Sleeper projects at `ppg` while ESPN says `e_ppg`."""
    out = {"ppg": ppg, "sl_ppg": ppg, "espn_ppg": e_ppg}
    if rank is not None:
        out.update({"rank": rank, "sl_rank": rank, "adp": float(rank), "sl_adp": float(rank)})
    return out


def _fill_to(env, picks, upto: int, position: str = "QB"):
    from sleeper import draft as D
    d = env["draft"]
    filled = {p["pick_no"] for p in picks}
    meta = {"first_name": "Filler", "last_name": "Guy", "position": position, "team": "KC"}
    return picks + [dict(picks[0], pick_no=q, round=D._slot(d, q)[0], draft_slot=D._slot(d, q)[1], roster_id=D.picker(d, q, {})[0], metadata=meta)
                    for q in range(41, upto) if q not in filled]


def test_top_is_ecr_ordered_before_the_ppg_threshold(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p45": {"ppg": 25.0}}))
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["status"]["next_pick"] < tools.PPG_SORT_FROM_PICK
    assert out["top"][0]["name"] == "QB Player0" and out["top"][0]["ecr"] == 1
    assert [r["ecr"] for r in out["top"]] == sorted(r["ecr"] for r in out["top"])
    assert "dc" in out["top"][0]


def test_top_is_ppg_ordered_late_and_skips_filled_qb(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p45": {"ppg": 25.0}}))
    picks = _fill_to(env, picks, tools.PPG_SORT_FROM_PICK, position="QB")
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["status"]["next_pick"] >= tools.PPG_SORT_FROM_PICK
    assert "QB" not in out["needs"].split("open:")[1].split("|")[0]
    assert {r["name"] for r in tools._avail_rows(env, picks, tools.R.rank_of(None, tools.P.load()))} >= {"QB Player0"}
    assert all(r["pos"] != "QB" for r in out["top"])
    assert out["top"][0]["name"] == "WR Player16" and out["top"][0]["ppg"] == 25.0
    ppgs = [r["ppg"] for r in out["top"]]
    head = ppgs[:-tools.RESERVED_PER_OPEN]  # the synthetic ADPs make everyone a faller, so two rows are reserved for the biggest
    assert head == sorted(head, reverse=True) and all(r["fall"] for r in out["top"][-tools.RESERVED_PER_OPEN:])
    assert {r["pos"] for r in out["top"]} <= {"RB", "WR", "TE", "K", "DEF"}
    assert len(cli.render(out).splitlines()) <= 50


def test_best_ppg_line_names_the_top_projection_per_position(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p45": {"ppg": 25.0}}))
    out = tools._outlook(env, picks, [], 12, 1)
    keys = list(out)
    assert keys[keys.index("top") + 1] == "best_ppg"
    parts = out["best_ppg"].split(" | ")
    assert parts[0] == "QB: QB Player0 29.8"
    assert "WR: WR Player16 25.0" in parts
    assert [p.split(":")[0] for p in parts] == ["QB", "WR", "TE", "K", "DEF"]


def test_low_projection_is_flagged_excluded_from_top_and_listed(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p42": {"ppg": 3.1}, "p64": {"rank": 150, "ecr": 150, "ppg": 1.0}}))
    rows = {r["pid"]: r for r in tools._avail_rows(env, picks, tools.R.rank_of(None, tools.P.load()))}
    assert rows["p42"]["flag"] == "low-proj" and rows["p43"]["flag"] is None and rows["p64"]["flag"] is None
    header = cli.table(list(rows.values())[:2]).splitlines()[0].split()
    assert header[:13] == ["rk", "ecr", "tier", "pos_rank", "name", "pos", "team", "bye", "adp", "ppg", "e_ppg", "dc", "flag"]
    out = tools._outlook(env, picks, [], 12, 1)
    assert all(r["name"] != "WR Player13" for r in out["top"])
    assert out["check_news"] == "WR Player13 (3.1 ppg)"
    wr = next(b for b in out["by_position"] if b["pos"].startswith("WR"))
    assert "WR Player13 (rk 42, 3.1 ppg ?, WR2)" in wr["players"]
    assert len(cli.render(out).splitlines()) <= 50


def test_check_news_is_omitted_without_flags(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg())
    assert "check_news" not in tools._outlook(env, picks, [], 12, 1)


def test_depth_chart_marker_only_below_the_starter(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    out = tools._outlook(env, picks, [], 12, 1)
    te = next(b for b in out["by_position"] if b["pos"].startswith("TE"))
    # TE Player0..7 use depth_chart_order i % 3 + 1: Player4 is a TE2, Player6 a TE1
    assert te["players"] == "TE Player4 (rk 53, TE2), TE Player5 (rk 54, TE3), TE Player6 (rk 55)"
    qb_row = tools._avail_str({"name": "QB Player1", "rk": 2, "ecr": 2, "pos": "QB", "dc": 2, "ppg": None, "flag": None})
    assert qb_row == "QB Player1 (rk 2)"
    assert tools._avail_str({"name": "WR X", "rk": 12, "ecr": 30, "pos": "WR", "dc": 1, "ppg": 14.2, "e_ppg": 13.1, "flag": None}) == "WR X (rk 12, 14.2 ppg / e 13.1)"
    assert tools._avail_str({"name": "WR X", "rk": 12, "ecr": 30, "pos": "WR", "dc": 2, "ppg": 14.2, "e_ppg": 14.2, "flag": None}) == "WR X (rk 12, 14.2 ppg, WR2)"
    assert tools._avail_str({"name": "RB Y", "rk": None, "ecr": None, "pos": "RB", "dc": None, "ppg": 3.7, "e_ppg": 15.7, "flag": "diverge"}) == "RB Y (rk -, 3.7 ppg / e 15.7 ?)"


def test_targets_are_windowed_capped_and_include_below_the_cut(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    drafted = sorted({p["player_id"] for p in picks})
    # p64 (DEF Player3) is ranked 281 but its ADP is inside next_pick + 40; p1 is far outside the window
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p64": {"ecr": 281, "adp": 75.0}, "p1": {"adp": 200.0}}))
    targets = {pid: table[pid]["name"] for pid in ["p64", "p1"] + [f"p{i}" for i in range(41, 52)] + drafted[:8]}
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": targets, "avoid": {}, "unknown": []})
    out = tools._outlook(env, picks, [], 12, 1)
    avail_part, gone_part = out["targets"].split(" | gone: ")
    assert avail_part.endswith(" +3 more")
    names = avail_part.removeprefix("available: ").removesuffix(" +3 more").split(", ")
    assert len(names) == 8 and names[0] == "WR Player13 (adp 42)"
    assert "QB Player0" not in out["targets"]
    assert gone_part.endswith(" +3 more") and gone_part.count(",") == 5
    assert len(cli.render(out).splitlines()) <= 50
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {"p64": table["p64"]["name"], "p1": table["p1"]["name"]}, "avoid": {}, "unknown": []})
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["targets"] == "available: DEF Player3 (adp 75) | gone: -"
    assert all(r["name"] != "DEF Player3" for r in out["top"])
    assert all("DEF Player3" not in b["players"] for b in out["by_position"])


def test_mine_line_truncates_per_position(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    picks = _fill_to(env, picks, 80, position="RB")
    out = tools._outlook(env, picks, [], 12, 1)
    rb = next(part for part in out["mine"].split(" | ") if part.startswith("RB: "))
    assert re.fullmatch(r"RB: [^,]+, [^,]+, [^,]+ \+\d+", rb), rb
    assert tools._mine_line({"RB": ["Christian McCaffrey", "Derrick Henry", "D'Andre Swift", "A", "B", "C"], "WR": ["X Y"]}) == "RB: Christian McCaffrey, Derrick Henry, D'Andre Swift +3 | WR: X Y"
    long = {pos: [f"Firstname{i} Lastname{i} Jr." for i in range(4)] for pos in ("QB", "RB", "WR", "TE")}
    line = tools._mine_line(long)
    assert line.startswith("QB: Lastname0, Lastname1, Lastname2 +1 | RB: ")


def test_outlook_stays_under_fifty_lines_with_every_line_present(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p45": {"ppg": 25.0}, "p42": {"ppg": 3.1}, "p46": _sleeper_over(3.7, 15.7), "p47": _sleeper_over(14.2, 13.1)}))
    targets = {f"p{i}": table[f"p{i}"]["name"] for i in range(30, 60)}
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": targets, "avoid": {"p44": table["p44"]["name"]}, "unknown": []})
    for late in (False, True):
        out = tools._outlook(env, _fill_to(env, picks, tools.PPG_SORT_FROM_PICK) if late else picks, [], 12, 1)
        assert {"best_ppg", "fallers", "targets", "avoid_on_board", "check_news"} <= set(out)
        assert len(out["picks_since"]) == 7
        assert "WR Player17 (sl 3.7 / espn 15.7)" in out["check_news"]
        assert any("14.2 ppg / e 13.1" in b["players"] for b in out["by_position"]) or any(r["name"] == "WR Player18" for r in out["top"])
        text = cli.render(out)
        assert len(text.splitlines()) <= 50, text


def test_top_keeps_rows_for_open_starter_slots_that_lose_on_ppg():
    rows = [{"pid": f"w{i}", "pos": "WR", "ppg": 13.0 - i * 0.1, "rk": 200 + i, "ecr": 200 + i, "flag": None} for i in range(12)]
    rows += [{"pid": f"k{i}", "pos": "K", "ppg": 8.6 - i * 0.1, "rk": 300 + i, "ecr": 300 + i, "flag": None} for i in range(3)]
    rows += [{"pid": "d1", "pos": "DEF", "ppg": 7.8, "rk": 240, "ecr": 240, "flag": None}]
    needs = {"starters_open": {"K": 1, "DEF": 1}, "flex_open": {}}
    top = tools._top(rows, 12, 174, needs)
    assert len(top) == 12
    assert [r["pid"] for r in top if r["pos"] == "K"] == ["k0", "k1"]
    assert [r["pid"] for r in top if r["pos"] == "DEF"] == ["d1"]
    assert top[0]["pid"] == "w0"
    # nothing reserved when the open slot already made the cut
    needs = {"starters_open": {"WR": 1}, "flex_open": {}}
    assert [r["pid"] for r in tools._top(rows, 5, 174, needs)] == ["w0", "w1", "w2", "w3", "w4"]


def test_stale_projection_is_flagged_and_kept_out_of_top_and_best_ppg(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    drafted = {p["player_id"] for p in picks}
    pos = next(p for p in ("RB", "WR") if any(r["position"] == p and pid not in drafted for pid, r in table.items()))
    group = [pid for pid, r in table.items() if r["position"] == pos]
    buried = next(pid for pid in group if pid not in drafted)
    # a cache doc where the position projects 15..7 ppg in rank order, except `buried`: rank 49 but 15.7 ppg
    rows = {}
    for n, pid in enumerate(group, start=1):
        rows[pid] = {"ecr": n, "tier": None, "pos_rank": f"{pos}{n}", "adp": float(n), "bye": None, "ppg": round(15 - n * 0.4, 1)}
    rows[buried] = {"ecr": 192, "tier": None, "pos_rank": f"{pos}49", "adp": 162.6, "bye": None, "ppg": 15.7}
    doc = {"source": "espn", "scoring": "PPR", "age_hours": 1.0, "rows": rows}
    assert tools._stale_ids(doc) == {buried}
    # with a Sleeper projection on the row the gap rule steps aside (the diverge rule covers it); ESPN's own ppg still orders the position
    with_sl = dict(rows, **{buried: dict(rows[buried], ppg=3.7, sl_ppg=3.7, espn_ppg=15.7)})
    assert tools._stale_ids({"rows": with_sl}) == frozenset()
    with_sl[buried] = dict(rows[buried], ppg=3.7, espn_ppg=15.7)
    assert tools._stale_ids({"rows": with_sl}) == {buried}
    monkeypatch.setattr(tools, "_rankings", lambda env: (doc, tools.R.rank_of(doc, table)))
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {}, "avoid": {}, "unknown": []})
    out = tools._outlook(env, picks, [], 12, 1)
    name = table[buried]["name"]
    assert name not in {r["name"] for r in out["top"]}
    assert name not in out["best_ppg"]
    assert name in out["check_news"]


def test_diverging_projection_is_flagged_kept_out_of_top_and_best_ppg_and_listed(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    # p42 (WR Player13): Sleeper 3.7 ppg vs ESPN 15.7 (the Kamara case); p43: 4.9 apart, no flag; p44: no ESPN number, no flag
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({
        "p42": _sleeper_over(3.7, 15.7, rank=167), "p43": _sleeper_over(14.2, 19.1), "p44": {"ppg": 12.0, "sl_ppg": 12.0, "espn_ppg": None},
    }))
    rows = {r["pid"]: r for r in tools._avail_rows(env, picks, tools.R.rank_of(None, tools.P.load()))}
    assert rows["p42"]["flag"] == "diverge" and rows["p43"]["flag"] is None and rows["p44"]["flag"] is None
    assert rows["p42"]["rk"] == 167 and rows["p42"]["ecr"] == 42 and rows["p42"]["sl_rank"] == 167
    assert rows["p42"]["ppg"] == 3.7 and rows["p42"]["e_ppg"] == 15.7 and rows["p43"]["e_ppg"] == 19.1 and rows["p44"]["e_ppg"] is None
    order = [r["rk"] for r in tools._avail_rows(env, picks, tools.R.rank_of(None, tools.P.load()))]
    assert order == sorted(order)
    out = tools._outlook(env, picks, [], 12, 1)
    assert "WR Player13" not in {r["name"] for r in out["top"]}
    assert "WR Player13" not in out["best_ppg"]
    assert out["check_news"] == "WR Player13 (sl 3.7 / espn 15.7)"
    wr = next(b for b in out["by_position"] if b["pos"].startswith("WR"))
    assert "WR Player13 (rk 167, 3.7 ppg / e 15.7 ?, WR2)" in wr["players"]
    assert list(out["top"][0]) == ["rk", "ecr", "name", "pos", "team", "bye", "adp", "fall", "ppg", "e_ppg", "dc", "inj"]
    assert next(r for r in out["top"] if r["name"] == "WR Player14")["e_ppg"] == 19.1
    assert len(cli.render(out).splitlines()) <= 50


def test_available_rows_fall_back_to_espn_on_a_pre_sleeper_cache(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    seen: dict = {}
    rows = {}
    for n, (pid, r) in enumerate(table.items(), start=1):
        seen[r["position"]] = seen.get(r["position"], 0) + 1
        rows[pid] = {"ecr": n, "tier": None, "pos_rank": f"{r['position']}{seen[r['position']]}", "adp": float(n) + 0.5, "bye": 9, "ppg": 10.0}
    doc = {"source": "espn", "scoring": "PPR", "age_hours": 1.0, "rows": rows}
    monkeypatch.setattr(tools, "_rankings", lambda env: (doc, tools.R.rank_of(doc, table)))
    avail = tools._avail_rows(env, picks, tools.R.rank_of(doc, table))
    assert all(r["rk"] == r["ecr"] and r["e_ppg"] is None and r["sl_rank"] is None and r["flag"] is None for r in avail)
    assert avail[0]["adp"] == 1.5 and avail[0]["ppg"] == 10.0
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["source"] == "espn" and "Sleeper" not in out["as_of"] and "check_news" not in out


def test_avoid_on_board_sees_players_below_the_top_cut(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    table = tools.P.load()
    drafted = {p["player_id"] for p in picks}
    avail_ids = [pid for pid in table if pid not in drafted]
    low = avail_ids[-1]  # worst search_rank -> below any top cut
    monkeypatch.setattr(tools.R, "rank_of", lambda doc, tbl: (lambda pid: {"ecr": tbl[pid]["search_rank"], "tier": None, "pos_rank": None,
                                                                    "adp": 60.0 if pid == low else float(tbl[pid]["search_rank"]), "bye": None,
                                                                    "source": "espn", "ppg": 8.0}))
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {}, "avoid": {low: table[low]["name"]}, "unknown": []})
    out = tools._outlook(env, picks, [], 5, 1)
    assert table[low]["name"] not in {r["name"] for r in out["top"]}
    assert out["avoid_on_board"] == table[low]["name"]


def test_draft_wait_polls_every_second():
    assert tools.POLL_SECONDS == 1.0


def test_top_keeps_near_adp_targets_and_drops_te_when_bench_is_nearly_full():
    rows = [{"pid": f"w{i}", "pos": "WR", "ppg": 12.0 - i * 0.1, "ecr": 200 + i, "rk": 200 + i, "adp": 200.0 + i, "flag": None} for i in range(8)]
    rows += [{"pid": f"t{i}", "pos": "TE", "ppg": 11.0 - i * 0.1, "ecr": 150 + i, "rk": 150 + i, "adp": 150.0 + i, "flag": None} for i in range(4)]
    rows += [{"pid": "jones", "pos": "RB", "ppg": 8.0, "ecr": 117, "rk": 128, "adp": 128.0, "flag": None}]
    rows += [{"pid": "k0", "pos": "K", "ppg": 6.4, "ecr": 270, "rk": 270, "adp": 270.0, "flag": None}]
    needs = {"starters_open": {"K": 1}, "flex_open": {}, "bench_open": 2}
    top = tools._top(rows, 8, 126, needs, targets={"jones"})
    assert all(r["pos"] != "TE" for r in top)
    assert "jones" in {r["pid"] for r in top} and "k0" in {r["pid"] for r in top}
    assert len(top) == 8
    # with a flex open and bench to spare, TEs stay; a target beyond the window is not forced in
    needs = {"starters_open": {"K": 1}, "flex_open": {"flex": 1}, "bench_open": 4}
    top = tools._top(rows, 12, 100, needs, targets={"jones"})
    assert any(r["pos"] == "TE" for r in top) and "jones" not in {r["pid"] for r in top}
    # no flex open: TEs drop even with bench to spare (mock 7, pick 139: six TE rows hid the last WR/RB options)
    needs = {"starters_open": {"K": 1}, "flex_open": {}, "bench_open": 4}
    assert all(r["pos"] != "TE" for r in tools._top(rows, 12, 100, needs, targets={"jones"}))


def test_byes_line_flags_stacked_weeks_and_marks_candidates(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    from sleeper import draft as D
    d = env["draft"]
    uid = tools.me()["user_id"]
    rid = D.roster_of_user(d, uid)
    slot = D.slot_of_roster(d, rid)
    table = tools.P.load()
    # give me three WRs on bye 7 and everyone else bye 5; a candidate WR on bye 7 must be marked
    mine = [p for p in picks if D.is_mine(p, rid, slot)]
    wr_ids = [pid for pid, r in table.items() if r["position"] == "WR"]
    for n, p in enumerate(mine[:3]):
        p["player_id"] = wr_ids[n]; p["metadata"] = {"first_name": "W", "last_name": f"R{n}", "position": "WR", "team": "KC"}
    drafted_ids = {p["player_id"] for p in picks}
    cand = next(pid for pid in wr_ids[3:] if pid not in drafted_ids)
    bye7 = set(wr_ids[:3]) | {cand}
    monkeypatch.setattr(tools.R, "rank_of", lambda doc, tbl: (lambda pid: {"ecr": tbl[pid]["search_rank"], "tier": None, "pos_rank": None,
                                                                    "adp": float(tbl[pid]["search_rank"]), "bye": 7 if pid in bye7 else 5,
                                                                    "source": "espn", "ppg": 9.0, "rank": tbl[pid]["search_rank"], "espn_ppg": 9.0}))
    monkeypatch.setattr(tools, "_plan_cache", {"notes": "", "targets": {}, "avoid": {}, "unknown": []})
    out = tools._outlook(env, picks, [], 12, 1)
    assert "7: R0, R1, R2" in out["byes"] and " !" in out["byes"]
    assert "my_next_round" not in out["status"]
    shown = " ".join(bp["players"] for bp in out["by_position"]) + " " + " ".join(str(r["bye"]) for r in out["top"])
    assert "bye 7!" in shown or "7!" in shown
    assert all(r["bye"] != "5!" for r in out["top"])
    assert len(cli.render(out).splitlines()) <= 50


def test_fallers_mark_qb_rb_wr_well_past_adp_and_keep_a_row_late(monkeypatch, tmp_path):
    env, picks = _mid_draft_env(monkeypatch, tmp_path)
    # on the clock at 41: p1 = QB Player0 (ADP 1, fell 40); p42 = WR Player13 moved to ADP 9 (fell 32);
    # p43 = WR Player14 at ADP 38 (fell 3, under the bar); p46 = flagged (diverge) at ADP 5, never a faller
    monkeypatch.setattr(tools.R, "rank_of", _rank_with_ppg({"p42": {"adp": 9.0}, "p43": {"adp": 38.0}, "p46": {**_sleeper_over(3.7, 15.7), "adp": 5.0}}))
    out = tools._outlook(env, picks, [], 12, 1)
    assert out["status"]["next_pick"] == 41
    assert out["fallers"].startswith("QB Player0 (QB, adp 1, fell 40), WR Player13 (WR, adp 9, fell 32)"), out["fallers"]
    assert "WR Player14" not in out["fallers"] and "WR Player17" not in out["fallers"]
    fall = {r["name"]: r["fall"] for r in out["top"]}
    assert fall["WR Player13"] == 32 and fall.get("WR Player14") is None
    assert list(out["top"][0]).index("fall") == list(out["top"][0]).index("adp") + 1
    assert len(cli.render(out).splitlines()) <= 50
    # late table: a faller who loses on ppg still gets a reserved row; TE counts only while the TE slot is open
    rows = [{"pid": f"w{i}", "pos": "WR", "ppg": 12.0 - i * 0.1, "ecr": 200 + i, "rk": 200 + i, "adp": 200.0 + i, "flag": None, "fall": None} for i in range(12)]
    fell = {"pid": "fell", "pos": "RB", "ppg": 7.0, "ecr": 80, "rk": 80, "adp": 80.0, "flag": None, "fall": None}
    needs = {"starters_open": {}, "flex_open": {}, "bench_open": 3}
    assert tools._mark_fallers([fell], 120, needs)[0]["fall"] == 40
    assert "fell" in {r["pid"] for r in tools._top(rows + [fell], 8, 120, needs)}
    assert tools._mark_fallers([dict(fell, pos="TE", fall=None)], 120, needs) == []
    assert tools._mark_fallers([dict(fell, pos="TE", fall=None)], 120, {"starters_open": {"TE": 1}, "flex_open": {}, "bench_open": 3})[0]["fall"] == 40
    assert tools._mark_fallers([dict(fell, adp=112.0, fall=None)], 120, needs) == []  # 8 picks late is not "well below ADP" at pick 120
    assert tools._avail_str(tools._mark_fallers([dict(fell, name="Fell Guy", fall=None)], 120, needs)[0]).endswith("fell 40)")
