"""League joins: team table, rosters, matchups and transactions with ids resolved to names."""
from __future__ import annotations

from datetime import datetime

from . import api, players
from .api import SleeperError

LEAGUE_TYPES = {0: "redraft", 1: "keeper", 2: "dynasty"}
SLOT_LABELS = {"SUPER_FLEX": "SF", "REC_FLEX": "RF"}
POS_ORDER = {p: i for i, p in enumerate(players.FANTASY)}


def context(league_id: str) -> dict:
    """League, users and rosters in one fetch each; `teams` maps roster_id to a resolved team row."""
    league = api.get("sleeper", f"league/{league_id}")
    users = {u["user_id"]: u for u in api.get("sleeper", f"league/{league_id}/users")}
    rosters = api.get("sleeper", f"league/{league_id}/rosters")
    teams = {r["roster_id"]: team_row(r, users) for r in rosters}
    return {"league": league, "users": users, "rosters": rosters, "teams": teams}


def team_row(roster: dict, users: dict) -> dict:
    s = roster.get("settings") or {}
    user = users.get(roster.get("owner_id")) or {}
    owner = user.get("display_name") or "(no owner)"
    record = f"{s.get('wins') or 0}-{s.get('losses') or 0}"
    if s.get("ties"):
        record += f"-{s['ties']}"
    return {
        "roster_id": roster["roster_id"],
        "owner_id": roster.get("owner_id"),
        "owner": owner,
        "team_name": ((user.get("metadata") or {}).get("team_name") or owner).strip(),
        "record": record,
        "fpts": round((s.get("fpts") or 0) + (s.get("fpts_decimal") or 0) / 100, 2),
        "waiver_position": s.get("waiver_position"),
    }


def scoring_label(scoring_settings: dict) -> str:
    rec = scoring_settings.get("rec") or 0
    return "PPR" if rec == 1 else "HALF" if rec == 0.5 else "STD"


def scoring_summary(scoring_settings: dict) -> str:
    rec = scoring_settings.get("rec") or 0
    rec_txt = f"{rec:.1f}" if float(rec).is_integer() else f"{rec:g}"
    parts = [f"{scoring_label(scoring_settings)} {rec_txt}", f"{scoring_settings.get('pass_td') or 0:g}pt passTD"]
    te = scoring_settings.get("bonus_rec_te") or 0
    if te:
        parts.append(f"TE{te:+g}")
    for k in sorted(scoring_settings):
        v = scoring_settings[k] or 0
        if k.startswith("bonus_") and k != "bonus_rec_te" and v:
            parts.append(f"{k.removeprefix('bonus_')}{v:+g}")
    return ", ".join(parts)


def roster_line(roster_positions: list[str]) -> str:
    slots = list(roster_positions)
    bench = 0
    while slots and slots[-1] == "BN":
        slots.pop()
        bench += 1
    parts = [SLOT_LABELS.get(p, p) for p in slots]
    if bench:
        parts.append(f"BN×{bench}")
    return " ".join(parts)


def format_line(league: dict, draft: dict | None = None) -> str:
    ds = (draft or {}).get("settings") or {}
    head = f"{league.get('total_rosters')}-team"
    if draft:
        head += f" {draft.get('type')}"
        if ds.get("reversal_round"):
            head += f" {ds['reversal_round']}RR"
    parts = [head, scoring_summary(league.get("scoring_settings") or {}), roster_line(league.get("roster_positions") or [])]
    if draft:
        parts.append(f"{ds['pick_timer']}s clock" if ds.get("pick_timer") else "no clock")
    t = (league.get("settings") or {}).get("type")
    if t in (1, 2):
        parts.append(LEAGUE_TYPES[t].upper())
    if ds.get("player_type") == 1:
        parts.append("ROOKIES ONLY")
    elif ds.get("player_type") == 2:
        parts.append("VETS ONLY")
    return " | ".join(parts)


def league_summary(league: dict) -> dict:
    t = (league.get("settings") or {}).get("type")
    scoring = league.get("scoring_settings") or {}
    positions = league.get("roster_positions") or []
    return {
        "league_id": league.get("league_id"),
        "name": league.get("name"),
        "teams": league.get("total_rosters"),
        "type": LEAGUE_TYPES.get(t, f"type_{t}"),
        "scoring": scoring_label(scoring),
        "superflex": "SUPER_FLEX" in positions,
        "te_premium": bool(scoring.get("bonus_rec_te")),
        "status": league.get("status"),
        "draft_id": league.get("draft_id"),
    }


def find_roster(ctx: dict, team: str | int | None, my_user_id: str) -> int:
    teams = ctx["teams"]
    name = ctx["league"].get("name")
    if team is None:
        for r in ctx["rosters"]:
            if r.get("owner_id") == my_user_id or my_user_id in (r.get("co_owners") or []):
                return r["roster_id"]
        raise SleeperError(f"you are not in league {name}")
    if isinstance(team, int) or team.isdigit():
        rid = int(team)
        if rid not in teams:
            raise SleeperError(f"no roster {rid} in {name} (rosters 1-{len(teams)})")
        return rid
    q = team.lower()
    hits = [t for t in teams.values() if q in t["owner"].lower() or q in t["team_name"].lower()]
    if len(hits) == 1:
        return hits[0]["roster_id"]
    candidates = ", ".join(f"{t['team_name']} ({t['owner']})" for t in (hits or teams.values()))
    raise SleeperError(f"{team!r} matches {'several teams' if hits else 'no team'} in {name}: {candidates}")


def _team_name(ctx: dict, roster_id: int) -> str:
    return (ctx["teams"].get(roster_id) or {}).get("team_name") or f"roster {roster_id}"


def _row(pid: str | None, table: dict) -> dict:
    if not pid or pid == "0":
        return {"pid": None, "name": "(empty)", "pos": None, "team": None, "injury": None}
    p = table.get(pid) or {}
    return {"pid": pid, "name": players.name(pid), "pos": p.get("position"), "team": p.get("team"), "injury": p.get("injury_status")}


def _label(pid: str, table: dict) -> str:
    p = table.get(pid)
    return f"{p['name']} ({p['position']})" if p else players.name(pid)


def roster_view(ctx: dict, roster_id: int) -> dict:
    roster = next((r for r in ctx["rosters"] if r["roster_id"] == roster_id), None)
    if roster is None:
        raise SleeperError(f"no roster {roster_id} in {ctx['league'].get('name')}")
    team = ctx["teams"][roster_id]
    table = players.load()
    slots = [SLOT_LABELS.get(p, p) for p in ctx["league"].get("roster_positions") or [] if p != "BN"]
    starters = roster.get("starters") or []
    reserve = roster.get("reserve") or []
    taxi = roster.get("taxi") or []
    placed = set(starters) | set(reserve) | set(taxi)
    bench = [_row(p, table) for p in roster.get("players") or [] if p not in placed]
    bench.sort(key=lambda r: (POS_ORDER.get(r["pos"], len(POS_ORDER)), r["name"]))
    return {
        "team": team["team_name"],
        "owner": team["owner"],
        "record": team["record"],
        "starters": [
            {"slot": slots[i] if i < len(slots) else "?", **_row(starters[i] if i < len(starters) else "0", table)}
            for i in range(max(len(slots), len(starters)))
        ],
        "bench": bench,
        "reserve": [_row(p, table) for p in reserve],
        "taxi": [_row(p, table) for p in taxi],
    }


def matchups_view(ctx: dict, week: int, detail: bool = False) -> dict:
    rows = api.get("sleeper", f"league/{ctx['league']['league_id']}/matchups/{week}")
    table = players.load() if detail else {}
    groups: dict = {}
    for m in rows:
        groups.setdefault(m.get("matchup_id"), []).append(m)

    def name(m: dict) -> str:
        return _team_name(ctx, m["roster_id"])

    out: list[dict] = []
    starters: list[dict] = []
    # Sleeper has no home/away; the first roster the API lists for a matchup is "home".
    for mid, ms in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        if mid is None or len(ms) < 2:
            out.extend({"matchup": mid, "home": name(m), "home_pts": m.get("points"), "away": None, "away_pts": None, "bye": True} for m in ms)
        else:
            a, b = ms[0], ms[1]
            out.append({"matchup": mid, "home": name(a), "home_pts": a.get("points"), "away": name(b), "away_pts": b.get("points")})
        if detail:
            for m in ms:
                pts = m.get("starters_points") or []
                pp = m.get("players_points") or {}
                for i, pid in enumerate(m.get("starters") or []):
                    r = _row(pid, table)
                    starters.append({"matchup": mid, "team": name(m), "name": r["name"], "pos": r["pos"], "pts": pts[i] if i < len(pts) else pp.get(pid)})
    result = {"matchups": out}
    if detail:
        result["starters"] = starters
    return result


def transactions_view(ctx: dict, week: int, type: str | None = None, limit: int = 25) -> list[dict]:
    txs = api.get("sleeper", f"league/{ctx['league']['league_id']}/transactions/{week}")
    table = players.load()
    txs = sorted((t for t in txs if type is None or t.get("type") == type), key=lambda t: t.get("created") or 0, reverse=True)

    def moves(m: dict | None, joiner: str) -> str | None:
        return ", ".join(f"{_label(pid, table)} {joiner} {_team_name(ctx, rid)}" for pid, rid in m.items()) if m else None

    out = []
    for t in txs[:limit]:
        rids = t.get("roster_ids") or []
        by = [_team_name(ctx, r) for r in (rids if t.get("type") == "trade" else rids[:1])]
        created = t.get("created")
        out.append({
            "type": t.get("type"),
            "status": t.get("status"),
            "by": " / ".join(by) or None,
            "adds": moves(t.get("adds"), "->"),
            "drops": moves(t.get("drops"), "from"),
            "faab": (t.get("settings") or {}).get("waiver_bid") or None,
            "picks": len(t.get("draft_picks") or []) or None,
            "when": datetime.fromtimestamp(created / 1000).date().isoformat() if created else None,
        })
    return out
