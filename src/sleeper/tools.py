"""The command set.

Every function in EXPORTS is a tool: typed keyword parameters, a JSON-serializable
dict result made of sections of flat rows, SleeperError on failure. The CLI and
the MCP server are wrappers over these.
"""
from __future__ import annotations

import os
import re
import time
import tomllib
from pathlib import Path

from . import api
from . import draft as D
from . import injuries as I
from . import league as L
from . import players as P
from . import rankings as R
from . import ratelimit
from .api import SleeperError

CONFIG_PATH = Path.home() / ".config" / "sleeper" / "config.toml"
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
MAX_LIMIT = 200
POLL_SECONDS = 1.0
STEP_SECONDS = 2.0
WAIT_MAX = 590
POSITION_ORDER = ("QB", "RB", "WR", "TE", "K", "DEF")
FLEX_POSITIONS = ("RB", "WR", "TE")
PPG_SORT_FROM_PICK = 90
RESERVED_PER_OPEN = 2
NEAR_TARGET_PICKS = 15
LATE_BENCH = 2
FALL_MIN = 6
FALL_DIVISOR = 8
FALLER_POSITIONS = ("QB", "RB", "WR")
LOW_PROJ_ECR = 120
LOW_PROJ_PPG = 6.0
STALE_PROJ_PPG = 10.0
STALE_PROJ_GAP = 25
STALE_DIVERGENCE = 5.0
TARGET_WINDOW = 40
MIN_BOARD = R.MIN_BOARD
SCORING_TYPES = {"ppr": "PPR", "half_ppr": "HALF", "std": "STD", "standard": "STD"}

_cfg: dict | None = None
_state: dict | None = None
_leagues: dict[str, dict] = {}


def load_config() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = tomllib.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        _load_env(ENV_PATH)
    return _cfg


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


PLAN_PATH = Path.home() / ".config" / "sleeper" / "plan.toml"
_plan_cache: dict | None = None


def _plan() -> dict:
    """Ishan's draft plan: notes, targets and avoid lists resolved to Sleeper ids."""
    global _plan_cache
    if _plan_cache is None:
        raw = tomllib.loads(PLAN_PATH.read_text()) if PLAN_PATH.exists() else {}
        out = {"notes": (raw.get("notes") or "").strip(), "targets": {}, "avoid": {}, "unknown": []}
        for key in ("targets", "avoid"):
            for name in raw.get(key) or []:
                hits = P.search(str(name), limit=2)
                exact = [h for h in hits if h["key"] == P.key(str(name))]
                pick = exact[0] if exact else (hits[0] if len(hits) == 1 else None)
                if pick:
                    out[key][pick["player_id"]] = pick["name"]
                else:
                    out["unknown"].append(str(name))
        _plan_cache = out
    return _plan_cache


def plan() -> dict:
    """The draft plan from ~/.config/sleeper/plan.toml: notes, targets, avoid, and any names that didn't resolve."""
    P.ensure(24 * 7, allow_refresh=True)
    pl = _plan()
    return {
        "path": str(PLAN_PATH),
        "notes": pl["notes"] or "(no plan.toml yet)",
        "targets": ", ".join(pl["targets"].values()) or "-",
        "avoid": ", ".join(pl["avoid"].values()) or "-",
        "unknown": ", ".join(pl["unknown"]) or "-",
    }


def state() -> dict:
    global _state
    if _state is None:
        _state = api.get("sleeper", "state/nfl")
    return _state


def _season(season: int | None) -> int:
    return int(season or load_config().get("season") or state()["season"])


def me() -> dict:
    cfg = load_config()
    if cfg.get("user_id"):
        return {"user_id": str(cfg["user_id"]), "username": cfg.get("username")}
    if not cfg.get("username"):
        raise SleeperError(f"set username in {CONFIG_PATH}")
    u = api.get("sleeper", f"user/{cfg['username']}")
    return {"user_id": u["user_id"], "username": u["username"]}


def _league_id(league_id: str | None) -> str:
    lid = league_id or load_config().get("league_id")
    if not lid:
        raise SleeperError("no league: pass --league or set league_id in config")
    return str(lid)


def _league_ctx(league_id: str) -> dict:
    if league_id not in _leagues:
        _leagues[league_id] = L.context(league_id)
    return _leagues[league_id]


def _limit(n: int, default: int) -> int:
    return max(1, min(int(n or default), MAX_LIMIT))


def _cap(rows: list, limit: int) -> list:
    return rows[: _limit(limit, len(rows) or 1)]


# ---------------------------------------------------------------- season tools

def whoami() -> dict:
    """Who the CLI is acting as, the NFL week, and cache and rate-limit state."""
    m = me()
    st = state()
    cfg = load_config()
    age = P.age_hours()
    return {
        "user_id": m["user_id"],
        "username": m["username"],
        "season": st["season"],
        "week": st["week"],
        "config": str(CONFIG_PATH),
        "league_id": cfg.get("league_id"),
        "draft_id": cfg.get("draft_id"),
        "players_age_h": round(age, 1) if age is not None else None,
        "rankings": _rankings_ages() or None,
        "fantasypros_key": bool(os.environ.get("FANTASYPROS_API_KEY")),
        "requests": [ratelimit.limiter(h).status() for h in ("sleeper", "fantasypros")],
    }


def _rankings_ages() -> list[dict]:
    out = []
    for f in sorted(api.CACHE_DIR.glob("rankings.*.json")):
        parts = f.name.split(".")
        if len(parts) != 4 or parts[1] == "raw":
            continue
        _, season, scoring, _ = parts
        out.append({"season": season, "scoring": scoring, "age_h": round((time.time() - f.stat().st_mtime) / 3600, 1)})
    return out


def leagues(season: int | None = None) -> dict:
    """Your leagues for a season (default: the current one)."""
    yr = _season(season)
    rows = [L.league_summary(lg) for lg in api.get("sleeper", f"user/{me()['user_id']}/leagues/nfl/{yr}")]
    return {"season": yr, "leagues": rows}


def league(league_id: str | None = None) -> dict:
    """League format, scoring and the team table with records."""
    ctx = _league_ctx(_league_id(league_id))
    lg = ctx["league"]
    teams = sorted(ctx["teams"].values(), key=lambda t: (-_wins(t["record"]), -t["fpts"]))
    teams = [{k: v for k, v in t.items() if k != "owner_id"} for t in teams]
    return {
        "format": L.format_line(lg),
        "name": lg["name"],
        "status": lg["status"],
        "scoring": L.scoring_summary(lg["scoring_settings"]),
        "teams": teams,
    }


def _wins(record: str) -> int:
    return int(record.split("-")[0])


def roster(league_id: str | None = None, team: str | None = None) -> dict:
    """One team's roster with names (default: yours; team = roster id, username or team name)."""
    ctx = _league_ctx(_league_id(league_id))
    note = P.ensure(24, allow_refresh=True)
    out = L.roster_view(ctx, L.find_roster(ctx, team, me()["user_id"]))
    return _noted(out, note)


def matchups(league_id: str | None = None, week: int | None = None, detail: bool = False) -> dict:
    """Matchups for a week with team names and points; detail adds starters."""
    ctx = _league_ctx(_league_id(league_id))
    wk = int(week or state()["week"])
    note = P.ensure(24, allow_refresh=True)
    return _noted({"week": wk, **L.matchups_view(ctx, wk, detail)}, note)


def transactions(league_id: str | None = None, week: int | None = None, type: str | None = None, limit: int = 25) -> dict:
    """Waivers, free-agent moves and trades for a week, newest first."""
    ctx = _league_ctx(_league_id(league_id))
    wk = int(week or state()["week"])
    note = P.ensure(24, allow_refresh=True)
    return _noted({"week": wk, "transactions": L.transactions_view(ctx, wk, type, _limit(limit, 25))}, note)


def players(query: str, position: str | None = None, limit: int = 10) -> dict:
    """Search the player dictionary by name (or exact Sleeper id)."""
    note = P.ensure(24, allow_refresh=True)
    rows = [_player_row(r) for r in P.search(query, position, _limit(limit, 10))]
    return _noted({"players": rows}, note)


def trending(kind: str = "add", hours: int = 24, limit: int = 25) -> dict:
    """Most added or dropped players across Sleeper."""
    if kind not in ("add", "drop"):
        raise SleeperError("kind must be add or drop")
    note = P.ensure(24, allow_refresh=True)
    raw = api.get("sleeper", f"players/nfl/trending/{kind}", params={"lookback_hours": hours, "limit": _limit(limit, 25)})
    table = P.load()
    rows = []
    for t in raw:
        r = table.get(t["player_id"])
        rows.append({"count": t["count"], **(_player_row(r) if r else {"name": f"#{t['player_id']}", "pid": t["player_id"]})})
    return _noted({"kind": kind, "hours": hours, "players": rows}, note)


def _player_row(r: dict) -> dict:
    return {
        "name": r["name"], "pos": r["position"], "team": r["team"], "age": r["age"],
        "yrs": r["years_exp"], "inj": r["injury_status"], "status": r["status"], "pid": r["player_id"],
    }


def _noted(out: dict, note: str | None) -> dict:
    if note:
        out["note"] = note
    return out


# ----------------------------------------------------------------- draft tools

def drafts(season: int | None = None) -> dict:
    """Your drafts for a season with status, format and your slot."""
    yr = _season(season)
    uid = me()["user_id"]
    rows = []
    fetched = 0
    for d in api.get("sleeper", f"user/{uid}/drafts/nfl/{yr}"):
        if d.get("status") != "complete" and fetched < 10:
            d = D.fetch(d["draft_id"])
            fetched += 1
        s = d["settings"]
        rid = D.roster_of_user(d, uid)
        rows.append({
            "draft_id": d["draft_id"],
            "league": (d.get("metadata") or {}).get("name") or "(no league)",
            "status": d.get("status"),
            "type": d.get("type"),
            "teams": s.get("teams"),
            "rounds": s.get("rounds"),
            "timer": s.get("pick_timer"),
            "player_type": s.get("player_type"),
            "my_slot": D.slot_of_roster(d, rid) if rid is not None else None,
            "start": _iso(d.get("start_time")),
        })
    return {"season": yr, "drafts": rows}


def _iso(ms: int | None) -> str | None:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else None


def _draft_id(draft_id: str | None) -> str:
    if draft_id:
        m = re.search(r"(\d{15,})", str(draft_id))
        if not m:
            raise SleeperError(f"not a draft id or URL: {draft_id}")
        return m.group(1)
    cfg = load_config()
    if cfg.get("draft_id"):
        return str(cfg["draft_id"])
    if cfg.get("league_id"):
        did = api.get("sleeper", f"league/{cfg['league_id']}").get("draft_id")
        if did:
            return str(did)
    open_ = [d for d in drafts()["drafts"] if d["status"] != "complete"]
    if len(open_) == 1:
        return open_[0]["draft_id"]
    raise SleeperError("no draft: pass --draft <id or URL> " + (f"(candidates: {', '.join(d['draft_id'] for d in open_)})" if open_ else ""))


def _draft_env(did: str, fresh: bool = True) -> dict:
    """Fetch a draft plus its league context (users, rosters, league) when it has one."""
    d = D.fetch(did, fresh=fresh)
    lg = users = None
    rosters: list = []
    lid = d.get("league_id")
    mock_lid = None if lid else (d.get("metadata") or {}).get("league_id")
    if lid or mock_lid:
        ctx = _league_ctx(lid or mock_lid)
        lg, users = ctx["league"], ctx["users"]
        # A league mock borrows the league's names and scoring, but its slots are not the league's rosters.
        rosters = ctx["rosters"] if lid else []
    return {"draft": d, "league": lg, "users": users, "rosters": rosters}


def _format(env: dict) -> str:
    d = env["draft"]
    if env["league"]:
        return L.format_line(env["league"], d)
    s = d["settings"]
    slots = " ".join(f"{k.removeprefix('slots_').upper()}×{v}" for k, v in s.items() if k.startswith("slots_") and v)
    scoring = SCORING_TYPES.get((d.get("metadata") or {}).get("scoring_type", ""), "?")
    return f"{s['teams']}-team {d['type']} | {scoring} | {slots} | {s.get('pick_timer') or 0}s clock"


def _picks_ctx(did: str, env: dict) -> tuple[list, int | None, frozenset]:
    picks = D.fetch_picks(did, fresh=True)
    un = D.unfilled(env["draft"], picks)
    nxt = un[0] if un else None
    return picks, nxt, D.remember_keepers(did, picks, nxt)


def draft_status(draft_id: str | None = None) -> dict:
    """Where the draft stands: on the clock, your next pick, clock estimate, last picks."""
    did = _draft_id(draft_id)
    env = _draft_env(did)
    d = env["draft"]
    picks, nxt, keepers = _picks_ctx(did, env)
    traded = D.fetch_traded(did)
    st = D.status(d, picks, traded, me()["user_id"], env["users"], env["rosters"], keeper_nos=keepers)
    lab = D.labels(d, env["users"])
    slot_lab = D.slot_labels(d, env["users"])
    last = [D.pick_row(p, lab, nxt, keepers, slot_lab) for p in D.live_picks(picks, nxt, keepers)][-5:]
    return {"format": _format(env), "draft_id": did, **st, "last_picks": last}


def draft_picks(draft_id: str | None = None, round: int | None = None, team: str | None = None, last: int | None = None) -> dict:
    """Picks made so far, names included; filter by round, team name substring, or the last N."""
    did = _draft_id(draft_id)
    env = _draft_env(did)
    lab = D.labels(env["draft"], env["users"])
    slot_lab = D.slot_labels(env["draft"], env["users"])
    picks, nxt, keepers = _picks_ctx(did, env)
    live = len(D.live_picks(picks, nxt, keepers))
    rows = [D.pick_row(p, lab, nxt, keepers, slot_lab) for p in sorted(picks, key=lambda p: p["pick_no"])]
    if round:
        rows = [r for r in rows if r["rd"] == int(round)]
    if team:
        rows = [r for r in rows if team.lower() in r["by"].lower()]
    if last:
        rows = rows[-int(last):]
    return {"picks_made": live, "keepers": len(picks) - live, "shown": len(rows), "picks": rows[-MAX_LIMIT:]}


def _rankings(env: dict) -> tuple[dict | None, object]:
    d = env["draft"]
    scoring = R.scoring_for(env["league"]) if env["league"] else SCORING_TYPES.get((d.get("metadata") or {}).get("scoring_type", ""), "STD")
    doc = R.load(d.get("season") or state()["season"], scoring)
    if doc and len(doc.get("rows") or {}) < MIN_BOARD:
        doc = {"too_small": len(doc["rows"])}
    return doc, R.rank_of(doc if doc and "rows" in doc else None, P.load())


def _stale_ids(doc: dict | None) -> frozenset:
    """Players whose ESPN projection says top-tier while the consensus rank says bench (ESPN had Kamara RB49 at 15.7 ppg while
    Sleeper projected 3.3): a gap of STALE_PROJ_GAP places between the ppg order and the positional rank marks the projection
    stale. Only rows without a Sleeper projection are flagged; with one, the diverge rule in _avail_rows covers it."""
    rows = (doc or {}).get("rows") or {}
    by_pos: dict[str, list] = {}
    for pid, k in rows.items():
        pr = k.get("pos_rank") or ""
        m = re.match(r"([A-Z]+)(\d+)$", pr)
        ppg = k["espn_ppg"] if k.get("espn_ppg") is not None else k.get("ppg")
        if m and ppg is not None and m.group(1) in ("QB", "RB", "WR", "TE"):
            by_pos.setdefault(m.group(1), []).append((pid, ppg, int(m.group(2))))
    out = set()
    for pos, items in by_pos.items():
        items.sort(key=lambda t: -t[1])
        for i, (pid, ppg, pos_rank) in enumerate(items, start=1):
            if ppg >= STALE_PROJ_PPG and pos_rank - i >= STALE_PROJ_GAP and rows[pid].get("sl_ppg") is None:
                out.add(pid)
    return frozenset(out)


def _avail_rows(env: dict, picks: list, rank, stale_ids: frozenset = frozenset()) -> list[dict]:
    rostered = {pid for r in env["rosters"] for pid in (r.get("players") or [])}
    rows = []
    for r in D.pool(env["draft"], picks, rostered):
        k = rank(r["player_id"])
        ecr, ppg, e_ppg, sl_ppg = k["ecr"], k.get("ppg"), k.get("espn_ppg"), k.get("sl_ppg")
        rk = k.get("rank") if k.get("rank") is not None else ecr
        dc = r.get("depth_chart_order")
        low = rk is not None and rk <= LOW_PROJ_ECR and ppg is not None and ppg < LOW_PROJ_PPG
        diverge = sl_ppg is not None and e_ppg is not None and abs(sl_ppg - e_ppg) >= STALE_DIVERGENCE
        stale = r["player_id"] in stale_ids
        rows.append({
            "rk": rk, "ecr": ecr, "tier": k["tier"], "pos_rank": k["pos_rank"], "name": r["name"], "pos": r["position"],
            "team": r["team"], "bye": k["bye"], "adp": k["adp"], "ppg": ppg, "e_ppg": e_ppg, "dc": dc,
            "flag": "diverge" if diverge else ("low-proj" if low else ("stale-proj" if stale else None)),
            "age": r["age"], "yrs": r["years_exp"], "inj": r["injury_status"], "sl_rank": k.get("sl_rank"), "pid": r["player_id"],
        })
    rows.sort(key=lambda r: (r["rk"] is None, r["rk"] or 0, r["ecr"] is None, r["ecr"] or 0))
    return rows


def _source(doc: dict | None) -> dict:
    if doc is None:
        return {"source": "search_rank", "as_of": "no rankings cache: run `sleeper rankings refresh`"}
    if "too_small" in doc:
        return {"source": "search_rank", "as_of": f"rankings cache has only {doc['too_small']} players (need {MIN_BOARD}; run `sleeper rankings refresh`); using Sleeper popularity order"}
    source = doc.get("source", "espn")
    as_of = f"rankings {doc['age_hours']:.1f} h old, scoring {doc['scoring']}"
    if source.startswith("sleeper+"):
        as_of += "; Sleeper rank/ADP/ppg, ESPN ppg alongside"
    return {"source": source, "as_of": as_of}


def draft_available(draft_id: str | None = None, position: str | None = None, limit: int = 20) -> dict:
    """Best available players by rank (Sleeper ADP order, ESPN board alongside, FantasyPros overlay; fallback: Sleeper search_rank)."""
    did = _draft_id(draft_id)
    env = _draft_env(did)
    note = P.ensure(24 * 7, allow_refresh=False)
    picks = D.fetch_picks(did, fresh=True)
    doc, rank = _rankings(env)
    rows = _avail_rows(env, picks, rank, _stale_ids(doc if doc and "rows" in doc else None))
    if position:
        rows = [r for r in rows if r["pos"] == position.upper()]
    return _noted({**_source(doc), "players": _cap(rows, limit)}, note)


def _avail_str(r: dict, heavy: frozenset | set = frozenset()) -> str:
    """`Name (rk 12, 14.2 ppg / e 13.1 ?, WR2)`: e is ESPN's ppg, ? flags a suspect projection, WR2 a depth-chart slot below the starter."""
    bits = [f"rk {r['rk'] if r.get('rk') is not None else '-'}"]
    if r.get("ppg") is not None:
        proj = f"{r['ppg']} ppg" + (f" / e {r['e_ppg']}" if r.get("e_ppg") is not None and r["e_ppg"] != r["ppg"] else "")
        bits.append(proj + (" ?" if r.get("flag") else ""))
    elif r.get("flag"):
        bits.append("?")
    if r["pos"] in FLEX_POSITIONS and r.get("dc") and r["dc"] > 1:
        bits.append(f"{r['pos']}{r['dc']}")
    if r.get("bye") in heavy:
        bits.append(f"bye {r['bye']}!")
    if r.get("fall"):
        bits.append(f"fell {r['fall']}")
    return f"{r['name']} ({', '.join(bits)})"



def _news_str(r: dict) -> str:
    if r["flag"] == "diverge":
        return f"{r['name']} (sl {r['ppg']} / espn {r['e_ppg']})"
    return f"{r['name']} ({r['ppg']} ppg)"


def _capped(items: list[str], cap: int) -> str:
    more = len(items) - cap
    return ", ".join(items[:cap]) + (f" +{more} more" if more > 0 else "")


def _surname(name: str) -> str:
    parts = name.split()
    if len(parts) > 1 and parts[-1].strip(".").lower() in P.SUFFIXES:
        parts = parts[:-1]
    return parts[-1] if parts else name


def _mine_line(mine: dict[str, list[str]]) -> str:
    def line(fmt) -> str:
        parts = []
        for pos, names in sorted(mine.items(), key=lambda kv: POSITION_ORDER.index(kv[0]) if kv[0] in POSITION_ORDER else 9):
            shown = ", ".join(fmt(n) for n in names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
            parts.append(f"{pos}: {shown}")
        return " | ".join(parts)
    full = line(lambda n: n)
    return (full if len(full) <= 120 else line(_surname)) or "-"


def _mark_fallers(avail: list[dict], nxt: int, needs: dict) -> list[dict]:
    """Set `fall` (picks past ADP) on unflagged QB/RB/WR rows, and TE while the TE slot is open, whose ADP is well before this pick; return them, biggest fall first."""
    positions = set(FALLER_POSITIONS) | ({"TE"} if "TE" in needs["starters_open"] else set())
    threshold = max(FALL_MIN, nxt // FALL_DIVISOR)
    out = []
    for r in avail:
        fall = round(nxt - r["adp"]) if r["adp"] and r["pos"] in positions and not r["flag"] else 0
        r["fall"] = fall if fall >= threshold else None
        if r["fall"]:
            out.append(r)
    return sorted(out, key=lambda r: -r["fall"])


def _top(avail: list[dict], limit: int, nxt: int, needs: dict, targets: frozenset | set = frozenset()) -> list[dict]:
    """Best available for the top table: rank order early, projected ppg once PPG_SORT_FROM_PICK is reached."""
    rows = [r for r in avail if not r["flag"]]
    if nxt < PPG_SORT_FROM_PICK:
        return _cap(rows, limit)
    keep = set(FLEX_POSITIONS) | set(needs["starters_open"])
    if "super_flex" in needs["flex_open"]:
        keep.add("QB")
    if "TE" not in needs["starters_open"] and (not needs["flex_open"] or needs.get("bench_open", 0) <= LATE_BENCH):
        keep.discard("TE")  # a second TE is noise once no flex can take him or the bench is nearly full
    rows = [r for r in rows if r["pos"] in keep]
    rows.sort(key=lambda r: (r["ppg"] is None, -(r["ppg"] or 0), r["rk"] is None, r["rk"] or 0))
    top = _cap(rows, limit)
    # Kickers and defenses never win on raw ppg; keep a couple of rows for every open starter slot,
    # the biggest ADP fallers, and plan targets whose ADP is about to pass, whatever their projection.
    present = {r["pos"] for r in top}
    reserved = [r for pos in needs["starters_open"] if pos not in present for r in [x for x in rows if x["pos"] == pos][:RESERVED_PER_OPEN]]
    seen = {r["pid"] for r in top} | {r["pid"] for r in reserved}
    reserved += sorted((r for r in rows if r.get("fall") and r["pid"] not in seen), key=lambda r: -r["fall"])[:RESERVED_PER_OPEN]
    seen |= {r["pid"] for r in reserved}
    reserved += [r for r in rows if r["pid"] in targets and r["pid"] not in seen and r["adp"] is not None and r["adp"] <= nxt + NEAR_TARGET_PICKS][:RESERVED_PER_OPEN]
    if reserved:
        top = top[: max(0, len(top) - len(reserved))] + reserved
    return top


def _outlook(env: dict, picks: list, traded: list, limit: int, since_pick: int, keeper_nos: frozenset | set = frozenset()) -> dict:
    d = env["draft"]
    uid = me()["user_id"]
    st = D.status(d, picks, traded, uid, env["users"], env["rosters"], keeper_nos=keeper_nos)
    lab = D.labels(d, env["users"])
    doc, rank = _rankings(env)
    avail = _avail_rows(env, picks, rank, _stale_ids(doc if doc and "rows" in doc else None))
    my_roster = st.get("my_roster_id")
    mine_picks = [p for p in picks if D.is_mine(p, my_roster, st.get("my_slot"))] if my_roster is not None else []
    byes: dict[int, list[tuple[str, str]]] = {}
    for p in mine_picks:
        m = p.get("metadata") or {}
        b = rank(p.get("player_id"))["bye"]
        if b is not None and m.get("position") in ("QB", "RB", "WR", "TE"):
            byes.setdefault(int(b), []).append((m.get("last_name") or "?", m.get("position")))
    heavy = frozenset(b for b, ps in byes.items() if len(ps) >= 3 or any(sum(1 for _, pos in ps if pos == q) >= 2 for q in ("RB", "WR")))
    byes_line = " | ".join(
        f"{b}: {', '.join(n for n, _ in ps)}" + (" !" if b in heavy else "")
        for b, ps in sorted(byes.items()) if len(ps) >= 2) or "-"
    mine: dict[str, list[str]] = {}
    for p in sorted(mine_picks, key=lambda p: p["pick_no"]):
        m = p.get("metadata") or {}
        mine.setdefault(m.get("position") or "?", []).append(f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip())
    n = D.needs(d, [pos for pos in mine for _ in mine[pos] if pos != "?"])
    needs_line = (f"picks_left {n['picks_left']} | open: "
                  + (" ".join(f"{k}×{v}" if v > 1 else k for k, v in n["starters_open"].items()) or "none")
                  + " | flex: " + (" ".join(f"{k}×{v}" for k, v in n["flex_open"].items()) or "none")
                  + f" | bench: {n['bench_open']}")
    nxt = st.get("next_pick") or 0
    fallers = _mark_fallers(avail, nxt, n)[:5]
    top = _top(avail, limit, nxt, n, frozenset(_plan()["targets"]))
    top_ids = {r["pid"] for r in top}
    best_ppg = []
    by_pos = []
    for pos in POSITION_ORDER:
        rows = [r for r in avail if r["pos"] == pos]
        if not rows:
            continue
        with_ppg = [r for r in rows if r["ppg"] is not None and not r["flag"]]
        if with_ppg:
            b = max(with_ppg, key=lambda r: r["ppg"])
            best_ppg.append(f"{pos}: {b['name']} {b['ppg']}")
        tiers = [r["tier"] for r in rows if r["tier"] is not None]
        head = f"{pos} (tier {tiers[0]}: {tiers.count(tiers[0])} left)" if tiers else pos
        rest = [r for r in rows if r["pid"] not in top_ids][:3]
        by_pos.append({"pos": head, "players": ", ".join(_avail_str(r, heavy) for r in rest) or "(all in top list)"})
    horizon = st.get("my_next_pick")
    if horizon and st.get("picks_away") == 0:
        tmap = D.traded_map(d, traded)
        mine_un = [q for q in D.unfilled(d, picks) if D.picker(d, q, tmap)[0] == my_roster]
        horizon = mine_un[1] if len(mine_un) > 1 else None
    gone = [r["name"] for r in avail if (r["adp"] or r["ecr"] or 10**9) < horizon][:10] if horizon else []
    gone_key = f"likely_gone_before_pick_{horizon}" if horizon else "likely_gone"
    flagged = [r for r in avail if r["flag"]][:5]
    pl = _plan()
    drafted_ids = {p.get("player_id") for p in picks}
    t_avail = sorted((r for r in avail if r["pid"] in pl["targets"] and (r["adp"] or r["ecr"]) and (r["adp"] or r["ecr"]) <= nxt + TARGET_WINDOW),
                     key=lambda r: r["adp"] or r["ecr"])
    t_gone = [n for pid, n in pl["targets"].items() if pid in drafted_ids]
    near = {r["pid"] for r in avail if (r["adp"] or r["ecr"] or 10**9) <= nxt + TARGET_WINDOW}
    avoid_on_board = [n for pid, n in pl["avoid"].items() if pid in top_ids or pid in near]
    extras = {}
    if fallers:
        extras["fallers"] = ", ".join(f"{r['name']} ({r['pos']}, adp {r['adp']:.0f}, fell {r['fall']})" for r in fallers)
    if pl["targets"]:
        shown = [f"{r['name']} (adp {r['adp']:.0f})" if r["adp"] else f"{r['name']} (ecr {r['ecr']})" for r in t_avail]
        extras["targets"] = f"available: {_capped(shown, 8) or '-'} | gone: {_capped(t_gone, 6) or '-'}"
    if avoid_on_board:
        extras["avoid_on_board"] = ", ".join(avoid_on_board)
    if flagged:
        extras["check_news"] = ", ".join(_news_str(r) for r in flagged)
    return {
        "format": _format(env),
        "status": {k: st.get(k) for k in ("status", "next_pick", "on_the_clock", "my_next_pick", "picks_away", "clock", "note") if st.get(k) is not None},
        **_source(doc),
        "needs": needs_line,
        "mine": _mine_line(mine),
        "byes": byes_line,
        "top": [{**{k: r[k] for k in ("rk", "ecr", "name", "pos", "team", "bye", "adp")}, "fall": r.get("fall"), **{k: r[k] for k in ("ppg", "e_ppg", "dc", "inj")}, "bye": f"{r['bye']}!" if r["bye"] in heavy else r["bye"]} for r in top],
        **({"best_ppg": " | ".join(best_ppg)} if best_ppg else {}),
        "by_position": by_pos,
        gone_key: ", ".join(gone) or "-",
        **extras,
        "picks_since": D.picks_since(picks, since_pick, lab, limit=6, next_pick_no=st.get("next_pick"), keeper_nos=keeper_nos, slot_lab=D.slot_labels(d, env["users"])),
        "since_pick": st.get("next_pick"),
    }


def draft_outlook(draft_id: str | None = None, limit: int = 12) -> dict:
    """Everything needed to decide a pick in one call: needs, best available, tiers, likely gone before your next turn, picks since (with the since_pick cursor)."""
    did = _draft_id(draft_id)
    env = _draft_env(did)
    note = P.ensure(24 * 7, allow_refresh=False)
    picks, _, keepers = _picks_ctx(did, env)
    traded = D.fetch_traded(did)
    return _noted(_outlook(env, picks, traded, limit, 0, keepers), note)


def draft_plan(draft_id: str | None = None, limit: int = 6) -> dict:
    """For each of your remaining picks: who is likely still there by ADP, the best of them, and your targets among them."""
    did = _draft_id(draft_id)
    env = _draft_env(did)
    note = P.ensure(24 * 7, allow_refresh=False)
    d = env["draft"]
    picks, _, keepers = _picks_ctx(did, env)
    traded = D.fetch_traded(did)
    st = D.status(d, picks, traded, me()["user_id"], env["users"], env["rosters"], keeper_nos=keepers)
    if st.get("my_roster_id") is None:
        raise SleeperError(st.get("note") or "you are not in this draft")
    doc, rank = _rankings(env)
    avail = _avail_rows(env, picks, rank)
    pl = _plan()
    tmap = D.traded_map(d, traded)
    rows = []
    for q in D.unfilled(d, picks):
        if D.picker(d, q, tmap)[0] != st["my_roster_id"]:
            continue
        likely = [r for r in avail if (r["adp"] or r["ecr"]) and (r["adp"] or r["ecr"]) >= q - 3]
        best = likely[: _limit(limit, 6)]
        rows.append({
            "pick": q,
            "rd": D._slot(d, q)[0],
            "likely_there": ", ".join(f"{r['name']} ({r['pos']}{' ' + str(r['ppg']) if r.get('ppg') else ''})" for r in best) or "-",
            "targets": ", ".join(r["name"] for r in likely if r["pid"] in pl["targets"])[:80] or "-",
        })
    return _noted({"format": _format(env), **_source(doc), "my_slot": st.get("my_slot"), "picks": rows}, note)


def draft_wait(draft_id: str | None = None, picks_away: int = 1, timeout: int = 100, since_pick: int = 0) -> dict:
    """Poll the live draft until your pick is within picks_away, the draft ends, or timeout seconds pass (a timeout is not an error). Returns the picks made since since_pick and the next since_pick cursor."""
    did = _draft_id(draft_id)
    env = _draft_env(did, fresh=False)
    note = P.ensure(24 * 7, allow_refresh=False)
    timeout = max(5, min(int(timeout), WAIT_MAX))
    poller = D.Poller(did, me()["user_id"], picks_away, since_pick, env["users"], env["rosters"])
    deadline = time.monotonic() + timeout
    while True:
        res = poller.step()
        if res:
            break
        remaining = deadline - time.monotonic()
        paused = ratelimit.limiter("sleeper").status()["paused_for_s"]
        if paused >= remaining or remaining <= POLL_SECONDS + STEP_SECONDS:
            res = poller.result("rate_limited" if poller.rate_limited else "timeout")
            break
        time.sleep(POLL_SECONDS)
    if res["reason"] == "my_turn":
        env["draft"] = res["draft"]
        out = _outlook(env, res["picks"], res["traded"], 12, since_pick, poller.keeper_nos)
        out["since_pick"] = res["status"].get("my_next_pick") or out["since_pick"]
        return _noted({"reason": "my_turn", **out}, note)
    st = res["status"]
    return _noted({
        "reason": res["reason"],
        "status": {k: st.get(k) for k in ("status", "round", "next_pick", "on_the_clock", "my_next_pick", "picks_away", "clock", "note") if st.get(k) is not None},
        "picks_since": res["picks_since"],
        "since_pick": st.get("next_pick"),
    }, note)


def live_outlook(poller: "D.Poller", env: dict, limit: int = 12) -> dict | None:
    """Outlook built from a poller's latest fetch, for the self-refreshing `draft watch` view; None before the first fetch."""
    if poller.draft is None:
        return None
    env = dict(env, draft=poller.draft)
    un = D.unfilled(poller.draft, poller.picks)
    nxt = un[0] if un else D.total_picks(poller.draft) + 1
    return _outlook(env, poller.picks, poller.traded, limit, max(1, nxt - 6), poller.keeper_nos)


def injuries(league_id: str | None = None, limit: int = I.WATCH_LIMIT, save: bool = True) -> dict:
    """Injury and IR changes since the last check: my roster plus the best unowned sidelined players."""
    lid = _league_id(league_id)
    note = P.ensure(24 * 7, allow_refresh=False)
    ctx = _league_ctx(lid)
    table = P.load()
    uid = me()["user_id"]
    mine_id = next((r["roster_id"] for r in ctx["rosters"] if r.get("owner_id") == uid or uid in (r.get("co_owners") or [])), None)
    pids = I.watchlist(ctx["rosters"], mine_id, table, limit)
    my_pids = {pid for r in ctx["rosters"] if r.get("roster_id") == mine_id
               for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])}
    before = I.load_snapshot(lid)
    now, failed = I.poll(pids)
    changes = I.diff(before, now, table, my_pids)
    if save:
        I.save_snapshot(lid, {**before, **now})
    out = {
        "league": ctx["league"].get("name", lid),
        "watching": f"{len(now)} players ({len(my_pids & set(now))} mine)" + (f", {len(failed)} unreadable" if failed else ""),
        "since": "first run: baseline saved, nothing to compare" if not before else f"{(I.snapshot_age_min(lid) or 0):.0f} min since the last check",
        "changes": changes or "-",
    }
    return _noted(out, note)


EXPORTS = [
    whoami, leagues, league, roster, matchups, transactions, players, trending,
    drafts, draft_status, draft_picks, draft_available, draft_outlook, draft_plan, draft_wait, plan, injuries,
]
