"""The Sleeper player dictionary: slim on-disk cache, names, search, draftable pool."""
from __future__ import annotations

import json
import re
import time

from . import api
from .api import CACHE_DIR, SleeperError

FANTASY = ("QB", "RB", "WR", "TE", "K", "DEF")
SLIM_PATH = CACHE_DIR / "players.slim.json"
ETAG_PATH = CACHE_DIR / "players.etag"
UNRANKED = 9_999_999
ID_FIELDS = ("sportradar_id", "yahoo_id", "espn_id")
# Abbreviations other sites use that Sleeper doesn't.
TEAM_ALIASES = {
    "JAC": "JAX", "WSH": "WAS", "LA": "LAR", "KCC": "KC", "GBP": "GB",
    "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB", "LVR": "LV",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def key(name: str) -> str:
    """Normalize a name for matching: drop a generational suffix, keep [a-z0-9]."""
    parts = name.lower().split()
    if len(parts) > 1 and parts[-1].strip(".") in SUFFIXES:
        parts = parts[:-1]
    return re.sub(r"[^a-z0-9]", "", "".join(parts))


def team(abbr: str | None) -> str | None:
    return TEAM_ALIASES.get(abbr, abbr) if abbr else abbr


def _slim_row(pid: str, p: dict) -> dict | None:
    fps = [x for x in (p.get("fantasy_positions") or []) if x in FANTASY]
    pos = p.get("position") if p.get("position") in FANTASY else (fps[0] if fps else None)
    if pos is None:
        return None
    name = p.get("full_name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
    row = {
        "player_id": pid,
        "name": name,
        "key": key(name),
        "position": pos,
        "fantasy_positions": fps or [pos],
        "team": p.get("team"),
        "status": p.get("status"),
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
        "injury_status": p.get("injury_status"),
        "depth_chart_order": p.get("depth_chart_order"),
        "search_rank": p.get("search_rank") or UNRANKED,
    }
    for f in ID_FIELDS:
        v = p.get(f)
        row[f] = str(v) if v is not None else None
    if pos == "DEF":
        row["aliases"] = [pid.lower(), key(p.get("first_name") or ""), key(p.get("last_name") or "")]
    return row


def refresh() -> dict:
    """Download the dictionary (ETag-aware) and rewrite the slim file."""
    headers = {}
    if ETAG_PATH.exists() and SLIM_PATH.exists():
        headers["If-None-Match"] = ETAG_PATH.read_text().strip()
    r = api.get_response("sleeper", "players/nfl", headers=headers)
    if r.status_code == 304:
        SLIM_PATH.touch()
        return {"refreshed": False, "rows": len(load())}
    rows = [row for pid, p in r.json().items() if (row := _slim_row(pid, p))]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SLIM_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    tmp.replace(SLIM_PATH)
    if r.headers.get("ETag"):
        ETAG_PATH.write_text(r.headers["ETag"])
    global _table
    _table = None
    return {"refreshed": True, "rows": len(rows)}


_table: dict[str, dict] | None = None


def load() -> dict[str, dict]:
    global _table
    if _table is None:
        if not SLIM_PATH.exists():
            raise SleeperError("player dictionary missing: run `sleeper players --refresh`")
        _table = {r["player_id"]: r for r in json.loads(SLIM_PATH.read_text())}
    return _table


def age_hours() -> float | None:
    return (time.time() - SLIM_PATH.stat().st_mtime) / 3600 if SLIM_PATH.exists() else None


def ensure(max_age_hours: float, allow_refresh: bool) -> str | None:
    """Make the slim file usable. Returns a note when something is worth telling the caller."""
    age = age_hours()
    if age is None:
        if not allow_refresh:
            raise SleeperError("player dictionary missing: run `sleeper players --refresh`")
        refresh()
        return "player dictionary downloaded"
    if age > max_age_hours:
        if allow_refresh:
            refresh()
            return "player dictionary refreshed"
        return f"player dictionary is {age / 24:.1f} days old (run `sleeper players --refresh`)"
    return None


def name(pid: str) -> str:
    row = load().get(pid)
    return row["name"] if row else f"#{pid}"


def names(pids) -> dict[str, str]:
    return {pid: name(pid) for pid in pids}


def search(query: str, position: str | None = None, limit: int = 10) -> list[dict]:
    table = load()
    if query in table:
        return [table[query]]
    q = key(query)
    pos = position.upper() if position else None
    rows = [
        r for r in table.values()
        if (pos is None or r["position"] == pos)
        and (q in r["key"] or any(q in a for a in r.get("aliases", ())))
    ]
    rows.sort(key=lambda r: (r["team"] is None, r["search_rank"]))
    return rows[:limit]


def pool() -> list[dict]:
    """Draftable universe: on a team and not marked Inactive."""
    return [r for r in load().values() if r["team"] and r["status"] != "Inactive"]
