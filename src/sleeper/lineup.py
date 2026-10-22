"""Rest-of-season lineup strength from Sleeper's weekly projections.

`GET /projections/nfl/<season>/<week>` (undocumented, the season endpoint ignores a
`week=` parameter) returns one row per player with that week's projected points. A team on
bye has a row with no opponent and no points; an IR player has no points either."""
from __future__ import annotations

import json
import math
import time

from . import api, players
from . import rankings as R
from .api import CACHE_DIR

SLOT_ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"}, "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
PLAYOFF_WEIGHT = 1.5
CACHE_HOURS = 12.0
LAST_NFL_WEEK = 18


def cache_path(season: int | str, scoring: str):
    return CACHE_DIR / f"weekly.{season}.{scoring}.json"


def season_weeks(league: dict, current_week: int) -> tuple[list[int], list[int]]:
    """(weeks still to play through the fantasy final, the playoff weeks among them)."""
    s = league.get("settings") or {}
    start = int(s.get("playoff_week_start") or 15)
    rounds = max(1, math.ceil(math.log2(max(2, int(s.get("playoff_teams") or 4)))))
    last = min(LAST_NFL_WEEK, start + rounds - 1)
    weeks = list(range(max(1, current_week), last + 1))
    return weeks, [w for w in weeks if w >= start]


def fetch_week(season: int | str, week: int, scoring: str) -> dict[str, float]:
    """{player_id: projected points} for one week; byes and unprojected players are left out."""
    _, pts_field = R.sleeper_fields(scoring)
    rows = api.get("sleeper_root", f"projections/nfl/{season}/{week}",
                   params={"season_type": "regular", "position[]": list(players.FANTASY)})
    out = {}
    for r in rows:
        pts = (r.get("stats") or {}).get(pts_field)
        if pts is not None and r.get("opponent"):
            out[str(r["player_id"])] = float(pts)
    return out


def load_weeks(season: int | str, weeks: list[int], scoring: str, max_age_h: float = CACHE_HOURS) -> dict[int, dict[str, float]]:
    """Weekly projections for `weeks`, refetching any week older than `max_age_h` (one call per week)."""
    path = cache_path(season, scoring)
    try:
        doc = json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError):
        doc = {}
    now, changed = time.time(), False
    for w in weeks:
        entry = doc.get(str(w))
        if not entry or now - entry.get("at", 0) > max_age_h * 3600:
            doc[str(w)] = {"at": now, "pts": fetch_week(season, w, scoring)}
            changed = True
    if changed:
        path.write_text(json.dumps(doc))
    return {w: doc[str(w)]["pts"] for w in weeks}


def best_lineup(pids: list[str], pos_of: dict[str, str], pts: dict[str, float], slots: list[str]) -> tuple[float, list[tuple[str, str]]]:
    """Highest-scoring legal lineup: fixed slots first, then flex slots from narrowest to widest."""
    pool = sorted((p for p in pids if pts.get(p, 0) > 0), key=lambda p: -pts[p])
    used: set[str] = set()
    chosen: list[tuple[str, str]] = []
    starters = [s for s in slots if s in SLOT_ELIGIBLE]
    for slot in sorted(starters, key=lambda s: len(SLOT_ELIGIBLE[s])):
        pick = next((p for p in pool if p not in used and pos_of.get(p) in SLOT_ELIGIBLE[slot]), None)
        if pick:
            used.add(pick)
            chosen.append((slot, pick))
    return round(sum(pts[p] for _, p in chosen), 2), chosen


def strength(pids: list[str], pos_of: dict[str, str], weekly: dict[int, dict[str, float]], slots: list[str], playoff_weeks: list[int]) -> dict:
    """Weekly best-lineup totals and their summaries for one roster."""
    by_week = {w: best_lineup(pids, pos_of, pts, slots)[0] for w, pts in weekly.items()}
    reg = [v for w, v in by_week.items() if w not in playoff_weeks]
    po = [v for w, v in by_week.items() if w in playoff_weeks]
    weights = {w: PLAYOFF_WEIGHT if w in playoff_weeks else 1.0 for w in by_week}
    total_w = sum(weights.values()) or 1.0
    low = min(by_week, key=by_week.get) if by_week else None
    return {
        "by_week": by_week,
        "ros_ppg": round(sum(by_week.values()) / max(1, len(by_week)), 1),
        "regular_ppg": round(sum(reg) / len(reg), 1) if reg else None,
        "playoff_ppg": round(sum(po) / len(po), 1) if po else None,
        "score": round(sum(v * weights[w] for w, v in by_week.items()) / total_w, 1),
        "low_week": low,
        "low_pts": by_week.get(low) if low is not None else None,
    }
