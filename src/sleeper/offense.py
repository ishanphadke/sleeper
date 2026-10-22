"""Team passing offense, ranked per game from Sleeper's season stats or a recent window of weeks.

Sleeper has no team-stats or standings endpoint. `GET /stats/nfl/<season>?season_type=regular`
(undocumented) returns each player's season totals with his current team; summing pass_yd and
pass_td by team and dividing by games played (the most any player on the team has) gives
per-game passing. Per game matters: mid-season, some teams have had their bye and some have not.

`GET /stats/nfl/<season>/<week>` gives one week, which is how a recent window is built: a season
average blends a starter with the backups who replaced him (the 2026 Bears started Bagent and
Keenum in weeks 4-5 and Caleb Williams again in week 6)."""
from __future__ import annotations

import json
import time

from . import api
from .api import CACHE_DIR

CACHE_HOURS = 12.0
PASSING = ("QB", "WR", "TE")


def cache_path(season: int | str):
    return CACHE_DIR / f"offense.{season}.json"


def rank_teams(rows: list) -> dict[str, dict]:
    """{team: {ypg, tdpg, games, yd_rank, td_rank, rank}}; rank orders by the sum of the two ranks, yards breaking ties."""
    team: dict[str, dict] = {}
    for r in rows:
        t, s = r.get("team"), r.get("stats") or {}
        if not t:
            continue
        d = team.setdefault(t, {"yd": 0.0, "td": 0.0, "games": 0})
        d["yd"] += s.get("pass_yd") or 0
        d["td"] += s.get("pass_td") or 0
        d["games"] = max(d["games"], int(s.get("gp") or 0))
    out = {t: {"ypg": round(d["yd"] / max(1, d["games"]), 1), "tdpg": round(d["td"] / max(1, d["games"]), 2), "games": d["games"]}
           for t, d in team.items() if d["games"]}
    for key, field in (("yd_rank", "ypg"), ("td_rank", "tdpg")):
        for i, t in enumerate(sorted(out, key=lambda t: -out[t][field]), 1):
            out[t][key] = i
    for i, t in enumerate(sorted(out, key=lambda t: (out[t]["yd_rank"] + out[t]["td_rank"], out[t]["yd_rank"])), 1):
        out[t]["rank"] = i
    return out


def passing(season: int | str, max_age_h: float = CACHE_HOURS) -> dict[str, dict]:
    path = cache_path(season)
    try:
        doc = json.loads(path.read_text()) if path.exists() else None
    except (ValueError, OSError):
        doc = None
    if doc and time.time() - doc.get("at", 0) <= max_age_h * 3600:
        return doc["teams"]
    rows = api.get("sleeper_root", f"stats/nfl/{season}", params={"season_type": "regular", "position[]": ["QB", "RB", "WR", "TE"]})
    teams = rank_teams(rows)
    path.write_text(json.dumps({"at": time.time(), "teams": teams}))
    return teams


def bad(teams: dict[str, dict], bottom: int) -> frozenset[str]:
    """The `bottom` worst passing offenses."""
    n = len(teams)
    return frozenset(t for t, d in teams.items() if d["rank"] > n - bottom)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def week_rows(season: int | str, week: int) -> list:
    """One week's player stats, cached; a finished week does not change."""
    path = CACHE_DIR / f"stats.{season}.{week}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    rows = api.get("sleeper_root", f"stats/nfl/{season}/{week}", params={"season_type": "regular", "position[]": ["QB", "RB", "WR", "TE"]})
    slim = [{"team": r.get("team"), "pid": r.get("player_id"), "pos": (r.get("player") or {}).get("position"),
             "name": f"{(r.get('player') or {}).get('last_name') or r.get('player_id')}",
             "stats": {k: (r.get("stats") or {}).get(k) for k in ("pass_yd", "pass_td", "pass_att", "gp")}} for r in rows]
    if slim:                                              # an unplayed week is empty: do not cache it
        path.write_text(json.dumps(slim))
    return slim


def recent(season: int | str, current_week: int, games: int = 3, max_back: int = 6) -> tuple[dict[str, dict], list[int]]:
    """Passing ranked over each team's last `games` played weeks before `current_week`, plus its starting QB each week.

    Returns ({team: {ypg, tdpg, games, yd_rank, td_rank, rank, qbs}}, weeks looked at)."""
    per_team: dict[str, list[tuple[int, float, float, str]]] = {}
    weeks = []
    for wk in range(current_week - 1, max(0, current_week - 1 - max_back), -1):
        rows = week_rows(season, wk)
        if not rows:
            continue
        weeks.append(wk)
        agg: dict[str, dict] = {}
        for r in rows:
            t, st = r["team"], r["stats"]
            if not t or not st.get("gp"):
                continue
            d = agg.setdefault(t, {"yd": 0.0, "td": 0.0, "qb": None, "att": 0})
            d["yd"] += st.get("pass_yd") or 0
            d["td"] += st.get("pass_td") or 0
            if r["pos"] == "QB" and (st.get("pass_att") or 0) > d["att"]:
                d["qb"], d["att"] = r["name"], st.get("pass_att") or 0
        for t, d in agg.items():
            if len(per_team.setdefault(t, [])) < games:
                per_team[t].append((wk, d["yd"], d["td"], d["qb"] or "?"))
    out = {t: {"ypg": round(sum(x[1] for x in g) / len(g), 1), "tdpg": round(sum(x[2] for x in g) / len(g), 2), "games": len(g),
               "qbs": " / ".join(f"wk{x[0]} {x[3]}" for x in sorted(g))} for t, g in per_team.items() if g}
    for key, field in (("yd_rank", "ypg"), ("td_rank", "tdpg")):
        for i, t in enumerate(sorted(out, key=lambda t: -out[t][field]), 1):
            out[t][key] = i
    for i, t in enumerate(sorted(out, key=lambda t: (out[t]["yd_rank"] + out[t]["td_rank"], out[t]["yd_rank"])), 1):
        out[t]["rank"] = i
    return out, sorted(weeks)
