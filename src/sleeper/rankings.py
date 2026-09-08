"""Draft rankings: Sleeper's own projections/ADP first, ESPN's board alongside, an optional FantasyPros overlay; cached on disk by Sleeper id.

Sleeper's projections endpoint is keyed by Sleeper id, so its rank (ADP order),
ADP and ppg need no join and win where present. ESPN supplies rank, ADP, bye
and its own ppg for every row it joins, and is the fallback. FantasyPros' free
key returns 10 rows per call, so it only decorates those rows (tier, pos_rank,
bye); a paid key that returns MIN_BOARD or more rows makes FantasyPros ECR the
board instead.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from . import api, players
from .api import CACHE_DIR

REPORT_ECR_CUTOFF = 250
POSITION_MAP = {"DST": "DEF"}
MIN_BOARD = 100
ESPN_LIMIT = 500
ESPN_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
ESPN_RANK_TYPES = {"PPR": "PPR", "HALF": "PPR", "STD": "STANDARD"}
SLEEPER_ADP = {"PPR": "adp_ppr", "HALF": "adp_half_ppr", "STD": "adp_std"}
SLEEPER_PTS = {"PPR": "pts_ppr", "HALF": "pts_half_ppr", "STD": "pts_std"}
SLEEPER_NO_ADP = 999.0
GAMES = 17


def scoring_for(league: dict) -> str:
    rec = (league.get("scoring_settings") or {}).get("rec")
    if rec == 1.0:
        return "PPR"
    if rec == 0.5:
        return "HALF"
    return "STD"


def cache_path(season: int | str, scoring: str) -> Path:
    return CACHE_DIR / f"rankings.{season}.{scoring}.json"


def raw_path(season: int | str, scoring: str) -> Path:
    """The two FantasyPros responses as received, kept for debugging and fixtures."""
    return CACHE_DIR / f"rankings.raw.{season}.{scoring}.json"


def espn_raw_path(season: int | str) -> Path:
    """The two ESPN responses as received, minus each player's stats block."""
    return CACHE_DIR / f"rankings.raw.espn.{season}.json"


def sleeper_raw_path(season: int | str) -> Path:
    """The Sleeper projections list as received."""
    return CACHE_DIR / f"rankings.raw.sleeper.{season}.json"


def espn_rank_type(scoring: str, superflex: bool = False) -> str:
    return "SUPERFLEX" if superflex else ESPN_RANK_TYPES.get(scoring, "PPR")


def sleeper_fields(scoring: str, superflex: bool = False) -> tuple[str, str]:
    """(adp field, points field) in Sleeper's stats block for this format."""
    return ("adp_2qb" if superflex else SLEEPER_ADP.get(scoring, "adp_ppr")), SLEEPER_PTS.get(scoring, "pts_ppr")


def fetch_sleeper(season: int | str, scoring: str, superflex: bool = False) -> list:
    """Sleeper's season projections with ADP for every fantasy position (one call, undocumented endpoint, DEF ids are team abbreviations)."""
    adp_field, _ = sleeper_fields(scoring, superflex)
    return api.get(
        "sleeper_root",
        f"projections/nfl/{season}",
        params={"season_type": "regular", "position[]": list(players.FANTASY), "order_by": adp_field},
    )


def sleeper_rows(doc: list, scoring: str, superflex: bool = False) -> dict[str, dict]:
    """{sleeper_id: {sl_pts, sl_ppg, sl_adp, sl_rank}}; sl_rank is the 1-based ADP order among rows with an ADP (999 means none)."""
    adp_field, pts_field = sleeper_fields(scoring, superflex)
    out: dict[str, dict] = {}
    for r in doc:
        p = r.get("player") or {}
        fps = [x for x in (p.get("fantasy_positions") or []) if x in players.FANTASY]
        if p.get("position") not in players.FANTASY and not fps:
            continue
        st = r.get("stats") or {}
        pts, adp = st.get(pts_field), st.get(adp_field)
        adp = round(float(adp), 1) if adp is not None and float(adp) < SLEEPER_NO_ADP else None
        if pts is None and adp is None:
            continue
        out[str(r["player_id"])] = {
            "sl_pts": pts,
            "sl_ppg": round(float(pts) / GAMES, 1) if pts is not None else None,
            "sl_adp": adp,
            "sl_rank": None,
        }
    ranked = sorted((pid for pid, k in out.items() if k["sl_adp"] is not None), key=lambda pid: out[pid]["sl_adp"])
    for n, pid in enumerate(ranked, start=1):
        out[pid]["sl_rank"] = n
    return out


def fetch_espn(season: int | str, rank_type: str, limit: int = ESPN_LIMIT) -> dict:
    """ESPN's draft board: the top `limit` players by `rank_type` (PPR | STANDARD | SUPERFLEX)."""
    flt = {"players": {
        "limit": limit,
        "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": rank_type},
        "filterRanksForScoringPeriodIds": {"value": [1]},
        "filterRanksForRankTypes": {"value": [rank_type]},
    }}
    return api.get(
        "espn",
        f"seasons/{season}/segments/0/leaguedefaults/3",
        params={"view": "kona_player_info"},
        headers={"X-Fantasy-Filter": json.dumps(flt)},
    )


def fetch_espn_teams(season: int | str) -> dict:
    """Pro teams with abbreviations and bye weeks (settings.proTeams[])."""
    return api.get("espn", f"seasons/{season}", params={"view": "proTeamSchedules_wl"})


def fetch_docs(season: int | str, scoring: str) -> tuple[dict, dict]:
    """Both FantasyPros responses; the second call waits out the 1/min cap, so this takes about a minute."""
    consensus = api.get(
        "fantasypros",
        f"nfl/{season}/consensus-rankings",
        params={"position": "ALL", "type": "DRAFT", "scoring": scoring, "week": 0},
    )
    fp_players = api.get("fantasypros", "nfl/players", params={"ecr": "included", "external_ids": "yahoo:espn"})
    return consensus, fp_players


def fetch(season: int | str, scoring: str) -> tuple[list, list]:
    consensus, fp_players = fetch_docs(season, scoring)
    return consensus["players"], fp_players["players"]


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _position(r: dict) -> str | None:
    v = r.get("player_position_id") or r.get("player_positions") or ""
    if isinstance(v, list):
        v = v[0] if v else ""
    v = re.sub(r"\d+$", "", str(v).split(",")[0].strip().upper())
    return POSITION_MAP.get(v, v) or None


def _external_id(fp: dict, site: str) -> str | None:
    ext = fp.get("external_ids")
    v = fp.get(f"{site}_id")
    if v is None and isinstance(ext, dict):
        v = ext.get(site, ext.get(f"{site}_id"))
    return str(v) if v not in (None, "") else None


def _pref(row: dict) -> tuple[bool, int]:
    return (row["team"] is None, row["search_rank"])


def _best(cands: list[dict]) -> dict | None:
    """The single preferred row, or None when none or several tie."""
    if not cands:
        return None
    cands = sorted(cands, key=_pref)
    if len(cands) > 1 and _pref(cands[0]) == _pref(cands[1]):
        return None
    return cands[0]


class _Index:
    def __init__(self, table: dict[str, dict]):
        self.by_id: dict[str, dict[str, str]] = {f: {} for f in players.ID_FIELDS}
        self.by_key: dict[str, list[dict]] = defaultdict(list)
        self.def_ids: set[str] = set()
        # Sleeper carries stale duplicate rows that share an external id; the
        # preferred row (on a team, then best search_rank) must win the index.
        for row in sorted(table.values(), key=_pref):
            for f in players.ID_FIELDS:
                if row.get(f):
                    self.by_id[f].setdefault(row[f], row["player_id"])
            self.by_key[row["key"]].append(row)
            if row["position"] == "DEF":
                self.def_ids.add(row["player_id"])

    def by_name(self, name: str, pos: str | None, team: str | None) -> str | None:
        cands = [c for c in self.by_key.get(players.key(name), ()) if c["position"] == pos]
        hit = _best([c for c in cands if c["team"] == team]) if team else None
        hit = hit or _best(cands)
        return hit["player_id"] if hit else None


def _match(r: dict, fp: dict, idx: _Index, overrides: dict[str, str]) -> tuple[str | None, str | None]:
    """(sleeper id, 'id' | 'name') for one ranked row, first hit wins."""
    fp_id = str(r.get("player_id"))
    if fp_id in overrides:
        return overrides[fp_id], "id"
    sportradar = r.get("sportsdata_id")
    if sportradar and str(sportradar) in idx.by_id["sportradar_id"]:
        return idx.by_id["sportradar_id"][str(sportradar)], "id"
    yahoo = r.get("player_yahoo_id") or _external_id(fp, "yahoo")
    if yahoo and str(yahoo) in idx.by_id["yahoo_id"]:
        return idx.by_id["yahoo_id"][str(yahoo)], "id"
    espn = _external_id(fp, "espn")
    if espn and espn in idx.by_id["espn_id"]:
        return idx.by_id["espn_id"][espn], "id"
    pos = _position(r)
    team = players.team(r.get("player_team_id"))
    if pos == "DEF" and team in idx.def_ids:
        return team, "id"
    pid = idx.by_name(r.get("player_name") or "", pos, team)
    return (pid, "name") if pid else (None, None)


def espn_teams(doc: dict) -> dict[int, dict]:
    """proTeamId -> {team (Sleeper abbreviation), bye}; id 0 is ESPN's free-agent pseudo-team."""
    out: dict[int, dict] = {}
    for t in (doc.get("settings") or {}).get("proTeams") or []:
        if t.get("id"):
            out[int(t["id"])] = {"team": players.team(t.get("abbrev")), "bye": _int(t.get("byeWeek")) or None}
    return out


def strip_stats(doc: dict) -> dict:
    """Lift the season projection (points, per-game) out of each player's stats block, then drop the block (about 30 KB per player)."""
    for entry in doc.get("players") or []:
        p = entry.get("player") or {}
        for st in p.pop("stats", None) or []:
            # statSourceId 1 = projected, scoringPeriodId 0 = full season
            if st.get("statSourceId") == 1 and st.get("scoringPeriodId") == 0 and st.get("statSplitTypeId", 0) == 0:
                p["proj_pts"] = st.get("appliedTotal")
                p["proj_ppg"] = st.get("appliedAverage")
                break
    return doc


def espn_rows(doc: dict, teams: dict[int, dict], rank_type: str) -> list[dict]:
    """Flatten ESPN player entries to {espn_id, name, pos, team, rank, adp, bye, injury}, sorted by rank."""
    rows = []
    for entry in doc.get("players") or []:
        p = entry.get("player") or {}
        pos = ESPN_POSITIONS.get(p.get("defaultPositionId"))
        rank = _int(((p.get("draftRanksByRankType") or {}).get(rank_type) or {}).get("rank"))
        if pos is None or rank is None or p.get("id") is None:
            continue
        t = teams.get(p.get("proTeamId")) or {}
        adp = (p.get("ownership") or {}).get("averageDraftPosition")
        rows.append({
            "espn_id": str(p["id"]),
            "name": p.get("fullName") or f"{p.get('firstName') or ''} {p.get('lastName') or ''}".strip(),
            "pos": pos,
            "team": t.get("team"),
            "rank": rank,
            "adp": round(float(adp), 1) if adp else None,
            "bye": t.get("bye"),
            "injury": p.get("injuryStatus"),
            "ppg": round(float(p["proj_ppg"]), 1) if p.get("proj_ppg") else None,
        })
    rows.sort(key=lambda r: r["rank"])
    return rows


def _match_espn(r: dict, idx: _Index, overrides: dict[str, str]) -> tuple[str | None, str | None]:
    if r["espn_id"] in overrides:
        return overrides[r["espn_id"]], "id"
    pid = idx.by_id["espn_id"].get(r["espn_id"])
    if pid:
        return pid, "id"
    if r["pos"] == "DEF":
        return (r["team"], "id") if r["team"] in idx.def_ids else (None, None)
    pid = idx.by_name(r["name"], r["pos"], r["team"])
    return (pid, "name") if pid else (None, None)


def join_espn(rows: list[dict], table: dict[str, dict], overrides: dict[str, str] | None = None) -> tuple[dict[str, dict], dict]:
    """Map ESPN rows to Sleeper ids. Returns ({sleeper_id: espn row}, report)."""
    idx = _Index(table)
    overrides = overrides or {}
    out: dict[str, dict] = {}
    report: dict = {"count": len(rows), "by_id": 0, "by_name": 0, "unmatched": []}
    for r in rows:
        pid, how = _match_espn(r, idx, overrides)
        if pid is None or pid in out:
            if r["rank"] <= REPORT_ECR_CUTOFF:
                report["unmatched"].append({k: r[k] for k in ("espn_id", "name", "team", "pos", "rank")})
            continue
        report[f"by_{how}"] += 1
        out[pid] = r
    report["unmatched"].sort(key=lambda u: u["rank"])
    return out, report


def merge(espn: dict[str, dict], fp: dict[str, dict] | None, sleeper: dict[str, dict] | None = None) -> tuple[dict[str, dict], str]:
    """Cache rows: ESPN rows carry ecr/espn_adp/espn_ppg, FantasyPros overlays tier/pos_rank/bye on the rows it
    covers and takes over ECR (source fantasypros) once it covers MIN_BOARD rows, then Sleeper's rank/ADP/ppg
    win the biased `rank`/`adp`/`ppg` fields wherever Sleeper has the player (source sleeper+...)."""
    fp = fp or {}
    sleeper = sleeper or {}
    seen: dict[str, int] = defaultdict(int)
    rows: dict[str, dict] = {}
    for pid, r in sorted(espn.items(), key=lambda kv: kv[1]["rank"]):
        seen[r["pos"]] += 1
        rows[pid] = {"ecr": r["rank"], "tier": None, "pos_rank": f"{r['pos']}{seen[r['pos']]}", "adp": r["adp"], "bye": r["bye"],
                     "ppg": r.get("ppg"), "espn_adp": r["adp"], "espn_ppg": r.get("ppg")}
    primary = len(fp) >= MIN_BOARD
    for pid, f in fp.items():
        if pid not in rows:
            if primary:
                rows[pid] = dict(f, ppg=None, espn_adp=None, espn_ppg=None)
            continue
        for k in ("tier", "pos_rank", "bye"):
            if f.get(k) is not None:
                rows[pid][k] = f[k]
        if primary:
            rows[pid]["ecr"] = f["ecr"]
    empty = {"ecr": None, "tier": None, "pos_rank": None, "adp": None, "bye": None, "ppg": None, "espn_adp": None, "espn_ppg": None}
    for pid in sleeper:
        rows.setdefault(pid, dict(empty))
    for pid, row in rows.items():
        sl = sleeper.get(pid) or {}
        row["sl_rank"], row["sl_adp"], row["sl_ppg"] = sl.get("sl_rank"), sl.get("sl_adp"), sl.get("sl_ppg")
        row["rank"] = row["sl_rank"] if row["sl_rank"] is not None else row["ecr"]
        if row["sl_adp"] is not None:
            row["adp"] = row["sl_adp"]
        if row["sl_ppg"] is not None:
            row["ppg"] = row["sl_ppg"]
    base = "fantasypros" if primary else "espn"
    return rows, (f"sleeper+{base}" if sleeper else base)


def join(
    ranked: list[dict],
    fp_players: list[dict],
    table: dict[str, dict],
    overrides: dict[str, str],
    scoring: str = "PPR",
) -> tuple[dict[str, dict], dict]:
    """Map consensus-rankings rows to Sleeper ids. Returns ({sleeper_id: row}, report)."""
    idx = _Index(table)
    fp_index = {str(p.get("player_id")): p for p in fp_players}
    adp_field = "rank_adp" if scoring == "STD" else "rank_adp_ppr"
    rows: dict[str, dict] = {}
    report: dict = {"count": len(ranked), "by_id": 0, "by_name": 0, "unmatched": []}
    for r in ranked:
        ecr = _int(r.get("rank_ecr"))
        if ecr is None:
            continue
        fp = fp_index.get(str(r.get("player_id")), {})
        pid, how = _match(r, fp, idx, overrides)
        if pid is None or pid in rows:
            if ecr <= REPORT_ECR_CUTOFF:
                report["unmatched"].append({
                    "fp_id": str(r.get("player_id")),
                    "name": r.get("player_name"),
                    "team": r.get("player_team_id"),
                    "pos": r.get("player_position_id"),
                    "ecr": ecr,
                })
            continue
        report[f"by_{how}"] += 1
        rows[pid] = {
            "ecr": ecr,
            "tier": _int(r.get("tier")),
            "pos_rank": r.get("pos_rank") or None,
            "adp": _int(fp.get(adp_field)),
            "bye": _int(r.get("player_bye_week")),
        }
    report["unmatched"].sort(key=lambda u: u["ecr"])
    return rows, report


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(path)


def refresh(
    season: int | str,
    scoring: str,
    overrides: dict[str, str] | None = None,
    superflex: bool = False,
    espn_overrides: dict[str, str] | None = None,
) -> dict:
    """Fetch Sleeper's projections (one call), ESPN (two calls), FantasyPros when a key is set (two more, about a
    minute), join the last two to Sleeper ids and write the cache. Returns {source, sleeper, espn, fantasypros, path}."""
    # Load the dictionary before spending any request budget, so a missing file fails fast.
    table = players.load()
    sleeper_doc = fetch_sleeper(season, scoring, superflex)
    _save(sleeper_raw_path(season), sleeper_doc)
    sl = sleeper_rows(sleeper_doc, scoring, superflex)
    sleeper_report = {
        "count": len(sl),
        "with_pts": sum(1 for k in sl.values() if k["sl_ppg"] is not None),
        "with_adp": sum(1 for k in sl.values() if k["sl_adp"] is not None),
    }
    rank_type = espn_rank_type(scoring, superflex)
    espn_doc = strip_stats(fetch_espn(season, rank_type))
    teams_doc = fetch_espn_teams(season)
    _save(espn_raw_path(season), {"rank_type": rank_type, "players": espn_doc, "teams": teams_doc})
    espn_joined, espn_report = join_espn(espn_rows(espn_doc, espn_teams(teams_doc), rank_type), table, espn_overrides)
    fp_joined = None
    fp_report: dict | str = "skipped: FANTASYPROS_API_KEY not set"
    last_updated = None
    if os.environ.get("FANTASYPROS_API_KEY"):
        consensus, fp_doc = fetch_docs(season, scoring)
        _save(raw_path(season, scoring), {"consensus": consensus, "players": fp_doc})
        fp_joined, fp_report = join(consensus["players"], fp_doc["players"], table, overrides or {}, scoring=scoring)
        last_updated = consensus.get("last_updated_ts")
    rows, source = merge(espn_joined, fp_joined, sl)
    doc = {
        "source": source,
        "scoring": scoring,
        "season": str(season),
        "rank_type": rank_type,
        "fetched_at": int(time.time()),
        "last_updated_ts": last_updated,
        "sleeper_rows": len(sl),
        "espn_rows": len(espn_joined),
        "fantasypros_rows": len(fp_joined or {}),
        "rows": rows,
    }
    path = cache_path(season, scoring)
    _save(path, doc)
    return {"source": source, "sleeper": sleeper_report, "espn": espn_report, "fantasypros": fp_report, "path": str(path)}


def load(season: int | str, scoring: str) -> dict | None:
    path = cache_path(season, scoring)
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    doc["age_hours"] = (time.time() - path.stat().st_mtime) / 3600
    return doc


RANK_KEYS = ("rank", "ecr", "tier", "pos_rank", "adp", "bye", "ppg", "espn_adp", "espn_ppg", "sl_rank", "sl_adp", "sl_ppg")


def rank_of(cache_doc: dict | None, table: dict[str, dict]) -> Callable[[str], dict]:
    """A pid -> {rank, ecr, tier, pos_rank, adp, bye, ppg, espn_adp, espn_ppg, sl_rank, sl_adp, sl_ppg, source} lookup;
    rank/adp/ppg are Sleeper's where present, else ESPN's (rank falls back to ecr); every key is None for unranked ids."""
    empty = dict.fromkeys(RANK_KEYS)
    if cache_doc is None:
        def by_search_rank(pid: str) -> dict:
            row = table.get(pid)
            sr = row["search_rank"] if row else players.UNRANKED
            return dict(empty, rank=sr, ecr=sr, source="search_rank")
        return by_search_rank
    rows = cache_doc.get("rows") or {}
    source = cache_doc.get("source") or "espn"

    def by_cache(pid: str) -> dict:
        out = dict(empty, **rows.get(pid, {}), source=source)
        if out["rank"] is None:
            out["rank"] = out["ecr"]
        return out
    return by_cache
