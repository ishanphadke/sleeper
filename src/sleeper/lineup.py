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


def replacement(pts: dict[str, float], rostered: set[str], pos_of: dict[str, str], depth: int = 4) -> dict[str, list[float]]:
    """Best free-agent projections per position for one week, highest first: what fills an empty slot."""
    out: dict[str, list[float]] = {}
    for pid, v in pts.items():
        pos = pos_of.get(pid)
        if pid not in rostered and pos and v > 0:
            out.setdefault(pos, []).append(v)
    return {pos: sorted(vs, reverse=True)[:depth] for pos, vs in out.items()}


def best_lineup(pids: list[str], pos_of: dict[str, str], pts: dict[str, float], slots: list[str],
                fill: dict[str, list[float]] | None = None) -> tuple[float, list[tuple[str, str, float]]]:
    """Highest-scoring legal lineup: fixed slots first, then flex slots narrowest to widest.

    A slot no rostered player can fill (bye, injury, empty position) takes the best unused
    free agent from `fill`, recorded as player id "FA"."""
    pool = sorted((p for p in pids if pts.get(p, 0) > 0), key=lambda p: -pts[p])
    used: set[str] = set()
    fa_used = {pos: 0 for pos in (fill or {})}
    chosen: list[tuple[str, str, float]] = []
    for slot in sorted((s for s in slots if s in SLOT_ELIGIBLE), key=lambda s: len(SLOT_ELIGIBLE[s])):
        pick = next((p for p in pool if p not in used and pos_of.get(p) in SLOT_ELIGIBLE[slot]), None)
        if pick:
            used.add(pick)
            chosen.append((slot, pick, pts[pick]))
            continue
        best = max(((fill[pos][fa_used[pos]], pos) for pos in SLOT_ELIGIBLE[slot]
                    if fill and pos in fill and fa_used[pos] < len(fill[pos])), default=None)
        if best:
            fa_used[best[1]] += 1
            chosen.append((slot, "FA", best[0]))
    return round(sum(v for _, _, v in chosen), 2), chosen


def ros_avg(weekly: dict[int, dict[str, float]]) -> dict[str, float]:
    """Projected points per game played over the remaining weeks (byes left out); players projected in under half of them are dropped."""
    tot: dict[str, list[float]] = {}
    for pts in weekly.values():
        for pid, v in pts.items():
            tot.setdefault(pid, []).append(v)
    n = max(1, len(weekly))
    return {pid: sum(v) / len(v) for pid, v in tot.items() if len(v) >= n / 2}


def _fixed_week(plan: list[tuple[str, str, float]], bench: list[str], pos_of: dict[str, str], pts: dict[str, float],
                fill: dict[str, list[float]] | None) -> float:
    """One week of a lineup set in advance: each starter scores his projection; a starter on bye or hurt is
    replaced by the best bench player or free agent who can fill the slot that week (byes are known ahead)."""
    used: set[str] = set()
    fa_used = {pos: 0 for pos in (fill or {})}
    total = 0.0
    for slot, pid, _ in plan:
        v = pts.get(pid) if pid != "FA" else None
        if v:
            total += v
            continue
        sub = max(((pts[b], b) for b in bench if b not in used and pos_of.get(b) in SLOT_ELIGIBLE[slot] and pts.get(b, 0) > 0),
                  default=None)
        fa = max(((fill[pos][fa_used[pos]], pos) for pos in SLOT_ELIGIBLE[slot]
                  if fill and pos in fill and fa_used[pos] < len(fill[pos])), default=None)
        if sub and (not fa or sub[0] >= fa[0]):
            used.add(sub[1])
            total += sub[0]
        elif fa:
            fa_used[fa[1]] += 1
            total += fa[0]
    return round(total, 2)


def strength(pids: list[str], pos_of: dict[str, str], weekly: dict[int, dict[str, float]], slots: list[str],
             playoff_weeks: list[int], fills: dict[int, dict[str, list[float]]] | None = None,
             avg: dict[str, float] | None = None, season_fill: dict[str, list[float]] | None = None) -> dict:
    """Weekly lineup totals for one roster and their summaries.

    With `avg`, the lineup is set once from rest-of-season averages and held, which is how a manager
    actually plays it; without, each week takes its own best lineup, which credits a backup with
    hindsight start/sit calls nobody can make. Empty slots are filled from waivers either way."""
    if avg is None:
        by_week = {w: best_lineup(pids, pos_of, pts, slots, (fills or {}).get(w))[0] for w, pts in weekly.items()}
    else:
        _, plan = best_lineup(pids, pos_of, avg, slots, season_fill)
        starters = {pid for _, pid, _ in plan}
        bench = [p for p in pids if p not in starters]
        by_week = {w: _fixed_week(plan, bench, pos_of, pts, (fills or {}).get(w)) for w, pts in weekly.items()}
    reg = [v for w, v in by_week.items() if w not in playoff_weeks]
    po = [v for w, v in by_week.items() if w in playoff_weeks]
    weights = {w: PLAYOFF_WEIGHT if w in playoff_weeks else 1.0 for w in by_week}
    total_w = sum(weights.values()) or 1.0
    return {
        "by_week": by_week,
        "ros_ppg": round(sum(by_week.values()) / max(1, len(by_week)), 1),
        "regular_ppg": round(sum(reg) / len(reg), 1) if reg else None,
        "playoff_ppg": round(sum(po) / len(po), 1) if po else None,
        "score": round(sum(v * weights[w] for w, v in by_week.items()) / total_w, 2),
    }
