"""Injury and IR watching. Sleeper has no injuries endpoint; this diffs snapshots of
`/v1/players/nfl/<id>`, an undocumented per-player view (~1 KB, CDN-cached ~10 min)."""
from __future__ import annotations

import json
import time

from . import api, players as P
from .api import CACHE_DIR

FIELDS = ("injury_status", "status", "practice_participation", "team", "depth_chart_order")
SIDELINED = frozenset({"IR", "PUP", "NA", "Out", "Sus", "COV", "DNR"})
PLAYABLE_STATUS = frozenset({"Active"})
WATCH_LIMIT = 80
FREE_AGENT_RANK = 400


def snapshot_path(league_id: str):
    return CACHE_DIR / f"injuries.{league_id}.json"


def is_sidelined(row: dict) -> bool:
    return (row.get("injury_status") or "") in SIDELINED or (row.get("status") or "Active") not in PLAYABLE_STATUS


def watchlist(rosters: list, my_roster_id, table: dict[str, dict], limit: int = WATCH_LIMIT) -> list[str]:
    """My whole roster (IR included), then the best sidelined players nobody owns."""
    mine, owned = [], set()
    for r in rosters:
        held = [pid for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])]
        owned.update(held)
        if r.get("roster_id") == my_roster_id:
            mine = held
    free = sorted((pid for pid, p in table.items()
                   if pid not in owned and p.get("team") and is_sidelined(p) and p.get("search_rank", 10**6) <= FREE_AGENT_RANK),
                  key=lambda pid: table[pid].get("search_rank", 10**6))
    return list(dict.fromkeys(mine + free))[:limit]


def fetch(pid: str, fresh: bool = False) -> dict:
    row = api.get("sleeper", f"players/nfl/{pid}", fresh=fresh)
    return {k: row.get(k) for k in FIELDS}


def poll(pids: list[str], fresh: bool = False) -> tuple[dict[str, dict], list[str]]:
    """Current state per player; ids that failed are returned separately, not treated as changes."""
    now, failed = {}, []
    for pid in pids:
        try:
            now[pid] = fetch(pid, fresh=fresh)
        except api.SleeperError:
            failed.append(pid)
    return now, failed


def load_snapshot(league_id: str) -> dict:
    path = snapshot_path(league_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("players", {})
    except (ValueError, OSError):
        return {}


def snapshot_age_min(league_id: str) -> float | None:
    path = snapshot_path(league_id)
    return (time.time() - path.stat().st_mtime) / 60 if path.exists() else None


def save_snapshot(league_id: str, state: dict[str, dict]) -> None:
    snapshot_path(league_id).write_text(json.dumps({"at": time.time(), "players": state}))


def _severity(row: dict) -> int:
    if is_sidelined(row):
        return 3
    return {"Doubtful": 2, "Questionable": 1}.get(row.get("injury_status") or "", 0)


def _kind(before: dict, after: dict) -> str | None:
    was, now = _severity(before), _severity(after)
    if was == 3 and now < 3:
        return "activated"
    if now == 3 and was < 3:
        return "sidelined"
    if now < was:
        return "upgraded"
    if now > was:
        return "downgraded"
    if (after.get("practice_participation") or "") != (before.get("practice_participation") or "") and after.get("practice_participation"):
        return "practice"
    if before.get("team") != after.get("team"):
        return "team"
    return None


def diff(before: dict[str, dict], after: dict[str, dict], table: dict[str, dict], mine: set[str] = frozenset()) -> list[dict]:
    """One row per meaningful change. Players absent from the previous snapshot are not changes."""
    out = []
    for pid, cur in after.items():
        prev = before.get(pid)
        if prev is None:
            continue
        kind = _kind(prev, cur)
        if kind is None:
            continue
        p = table.get(pid, {})
        out.append({
            "change": kind,
            "name": p.get("name", f"#{pid}"),
            "pos": p.get("position"),
            "team": cur.get("team") or p.get("team"),
            "was": prev.get("injury_status") or prev.get("status") or "-",
            "now": cur.get("injury_status") or cur.get("status") or "-",
            "practice": cur.get("practice_participation") or "-",
            "dc": cur.get("depth_chart_order"),
            "mine": pid in mine,
            "pid": pid,
        })
    order = {"activated": 0, "practice": 1, "upgraded": 2, "sidelined": 3, "downgraded": 4, "team": 5}
    out.sort(key=lambda r: (not r["mine"], order.get(r["change"], 9), r["name"]))
    return out
