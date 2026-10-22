"""Trade finder: trades that lift a weak starting slot of mine without weakening the partner.

Three rules keep the suggestions to trades a manager would make and accept:
- lineups are set once from rest-of-season averages and held, so a backup gets no credit
  for hindsight start/sit calls; byes and injuries are covered from the bench or waivers;
- a trade must replace a starter who is within HOLE_MARGIN of waivers (a real hole);
- value over waivers received may exceed value given by at most FAIR_PCT / FAIR_ABS.

Every candidate is scored by best-lineup points per remaining week for both rosters, before
and after, with empty slots filled from waivers (so byes are never a reason to trade). A cheap
screen on rest-of-season averages cuts the search; every survivor gets the full week-by-week score."""
from __future__ import annotations

from itertools import combinations

from . import lineup as LU

TRADE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
MIN_GAIN = 0.5
PER_PARTNER = 3
HOLE_MARGIN = 3.5     # a starter within this many ppg of the best healthy free agent is a hole worth trading for
FAIR_PCT = 0.15       # I may receive at most 15% more value over waivers than I give...
FAIR_ABS = 1.0        # ...or 1 ppg more, whichever is larger; beyond that no manager accepts
HOLD_TOLERANCE = 0.5  # with --hold, points in that position's dedicated slots (not flex) may drop by at most this much
HOLE_UPGRADE = 1.0    # the newcomer must out-project the starter he replaces by this much: a lateral swap fixes nothing


ros_avg = LU.ros_avg


def after_trade(roster: list[str], out: tuple[str, ...], into: tuple[str, ...], limit: int, avg: dict[str, float],
                pos_of: dict[str, str], slots: list[str], fill: dict[str, list[float]], reserve: set[str]) -> tuple[list[str], str | None]:
    """The roster after the swap, cutting the least valuable non-starter if it is over the limit."""
    new = [p for p in roster if p not in out] + list(into)
    active = [p for p in new if p not in reserve]
    if len(active) <= limit:
        return new, None
    _, lineup = LU.best_lineup(new, pos_of, avg, slots, fill)
    starters = {pid for _, pid, _ in lineup}
    cut = min((p for p in active if p not in starters and p not in into), key=lambda p: avg.get(p, 0.0), default=None)
    return [p for p in new if p != cut], cut


def my_slots(roster: list[str], pos_of: dict[str, str], avg: dict[str, float], slots: list[str], fill: dict[str, list[float]]) -> list[dict]:
    """Each starting slot's average starter against the best free agent who could take it: the size of each hole."""
    _, lineup = LU.best_lineup(roster, pos_of, avg, slots, fill)
    rows = []
    for slot, pid, pts in lineup:
        repl = max((fill.get(pos, [0.0])[0] for pos in LU.SLOT_ELIGIBLE[slot] if fill.get(pos)), default=0.0)
        rows.append({"slot": slot, "pid": pid, "ppg": round(pts, 1), "waiver": round(repl, 1), "margin": round(pts - repl, 1)})
    return rows


def slot_repl(slot: str, fill: dict[str, list[float]]) -> float:
    return max((fill[pos][0] for pos in LU.SLOT_ELIGIBLE[slot] if fill.get(pos)), default=0.0)


def value(pid: str, avg: dict[str, float], pos_of: dict[str, str], fill: dict[str, list[float]]) -> float:
    """Points per game over the best healthy free agent at his position: what the market pays for."""
    return max(0.0, avg.get(pid, 0.0) - (fill.get(pos_of.get(pid), [0.0]) or [0.0])[0])


def holes(lineup: list[tuple[str, str, float]], fill: dict[str, list[float]]) -> dict[str, tuple[str, float]]:
    """{starter (or "FA:<slot>" for a waiver fill-in): (slot, margin)} for tradeable slots within HOLE_MARGIN of waivers.

    Kicker and defense are left out: they stream from waivers and are never traded for."""
    out = {}
    for slot, pid, pts in lineup:
        if not LU.SLOT_ELIGIBLE[slot] & TRADE_POSITIONS:
            continue
        margin = pts - slot_repl(slot, fill)
        if pid == "FA" or margin < HOLE_MARGIN:
            out[pid if pid != "FA" else f"FA:{slot}"] = (slot, round(margin, 1))
    return out


def fixes_hole(before: list[tuple[str, str, float]], after: list[tuple[str, str, float]], into: tuple[str, ...],
               hole: dict[str, tuple[str, float]]) -> tuple[str, str] | None:
    """(hole starter who loses his job, the received player who takes that slot) when the trade fills a hole, else None."""
    slot_after = {pid: slot for slot, pid, _ in after}
    new_starters = [p for p in into if p in slot_after]
    if not new_starters:
        return None
    vacated = [p for p in hole if (p.startswith("FA:") and sum(1 for _, x, _ in after if x == "FA") < sum(1 for _, x, _ in before if x == "FA"))
               or (not p.startswith("FA:") and p not in slot_after)]
    pts_before = {(pid if pid != "FA" else f"FA:{slot}"): v for slot, pid, v in before}
    pts_after = {pid: v for _, pid, v in after}
    for h in vacated:                                  # pair each vacated hole with a newcomer who is a real upgrade, same slot first
        takers = sorted((p for p in new_starters if pts_after[p] - pts_before[h] >= HOLE_UPGRADE),
                        key=lambda p: (slot_after[p] != hole[h][0], -pts_after[p]))
        if takers:
            return h, takers[0]
    return None


def fair(give: tuple[str, ...], get: tuple[str, ...], avg, pos_of, fill) -> tuple[bool, float, float]:
    g = sum(value(p, avg, pos_of, fill) for p in give)
    r = sum(value(p, avg, pos_of, fill) for p in get)
    return r <= g + max(FAIR_ABS, FAIR_PCT * g), round(g, 1), round(r, 1)


def by_slot(lineup: list[tuple[str, str, float]]) -> dict[str, float]:
    """Points in each dedicated slot type (RB, WR, ...); flex slots are their own types and do not count toward a position."""
    out: dict[str, float] = {}
    for slot, _, pts in lineup:
        out[slot] = out.get(slot, 0.0) + pts
    return out


def by_position(lineup: list[tuple[str, str, float]], pos_of: dict[str, str]) -> dict[str, float]:
    """Starting points per position, counting flex starters at their own position; waiver fill-ins are left out."""
    out: dict[str, float] = {}
    for _, pid, pts in lineup:
        if pid != "FA":
            out[pos_of.get(pid, "?")] = out.get(pos_of.get(pid, "?"), 0.0) + pts
    return out


def candidates(mine: list[str], theirs: list[str], pos_of: dict[str, str], avg: dict[str, float]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """1-for-1, 2-for-1, 1-for-2 and 2-for-2 swaps of QB/RB/WR/TE with a projection."""
    give = [p for p in mine if pos_of.get(p) in TRADE_POSITIONS and avg.get(p, 0) > 0]
    get = [p for p in theirs if pos_of.get(p) in TRADE_POSITIONS and avg.get(p, 0) > 0]
    out = []
    for g in (1, 2):
        for r in (1, 2):
            for gs in combinations(give, g):
                for rs in combinations(get, r):
                    out.append((gs, rs))
    return out


def _padded(t: dict, kept: list[dict]) -> bool:
    """True when a kept trade with this partner is this one minus some players, and gains at least as much for both sides."""
    return any(k["partner"] == t["partner"] and set(k["give"]) <= set(t["give"]) and set(k["get"]) <= set(t["get"])
               and k["my_gain"] >= t["my_gain"] and k["their_gain"] >= t["their_gain"] for k in kept)


def find(me: dict, rivals: list[dict], weekly: dict[int, dict[str, float]], fills: dict[int, dict[str, list[float]]],
         fill: dict[str, list[float]], pos_of: dict[str, str], slots: list[str], playoff: list[int], limit: int,
         tolerance: float = 0.0, need: str | None = None, hold: frozenset[str] = frozenset()) -> list[dict]:
    """Trades ranked by my gain in weighted points per week, keeping only those the partner does not lose by more than `tolerance`.

    `me` and each rival are {"name", "players", "reserve", "limit"}. `fills` holds each week's best
    healthy free agents (a bye is streamed); `fill` the best healthy free agents by season average.
    `need` (a position) requires the trade to raise my starting points there and fill a hole with a
    player of that position; `hold` (positions) lets the points in their dedicated slots, flex excluded, drop by at most HOLD_TOLERANCE."""
    avg = ros_avg(weekly)

    def season(roster):
        return LU.best_lineup(roster, pos_of, avg, slots, fill)

    def full(roster):
        return LU.strength(roster, pos_of, weekly, slots, playoff, fills, avg, fill)["score"]

    base_total, base_lineup = season(me["players"])
    my_holes = holes(base_lineup, fill)
    base_pos, base_slot = by_position(base_lineup, pos_of), by_slot(base_lineup)
    screened = []
    for rv in rivals:
        base_rv = season(rv["players"])[0]
        for gs, rs in candidates(me["players"], rv["players"], pos_of, avg):
            ok, gv, rv_val = fair(gs, rs, avg, pos_of, fill)
            if not ok:
                continue
            mine_after, my_cut = after_trade(me["players"], gs, rs, me["limit"], avg, pos_of, slots, fill, me["reserve"])
            total_after, lineup_after = season(mine_after)
            if total_after - base_total < MIN_GAIN:
                continue
            fixed = fixes_hole(base_lineup, lineup_after, rs, my_holes)
            if fixed is None:
                continue
            after_pos = by_position(lineup_after, pos_of)
            if need and (pos_of.get(fixed[1]) != need or after_pos.get(need, 0.0) <= base_pos.get(need, 0.0)):
                continue
            after_slot = by_slot(lineup_after)          # hold means the dedicated slots; the flex is fair game
            if any(after_slot.get(h, 0.0) < base_slot.get(h, 0.0) - HOLD_TOLERANCE for h in hold):
                continue
            theirs_after, their_cut = after_trade(rv["players"], rs, gs, rv["limit"], avg, pos_of, slots, fill, rv["reserve"])
            if season(theirs_after)[0] - base_rv < -tolerance - 1.0:   # the screen is approximate; leave a margin for the full score
                continue
            screened.append((total_after - base_total, rv, gs, rs, mine_after, theirs_after, my_cut, their_cut, fixed, gv, rv_val))
    screened.sort(key=lambda x: -x[0])

    full_me = full(me["players"])
    full_base = {rv["name"]: full(rv["players"]) for rv in rivals}
    out = []
    for gain, rv, gs, rs, mine_after, theirs_after, my_cut, their_cut, fixed, gv, rv_val in screened:   # every survivor: a global cut hid whole partners
        mg = full(mine_after) - full_me
        tg = full(theirs_after) - full_base[rv["name"]]
        if mg < MIN_GAIN or tg < -tolerance:
            continue
        starts = {pid for _, pid, _ in LU.best_lineup(mine_after, pos_of, avg, slots, fill)[1]}
        out.append({"partner": rv["name"], "give": gs, "get": rs, "my_gain": round(mg, 1), "their_gain": round(tg, 1),
                    "i_cut": my_cut, "they_cut": their_cut, "core": frozenset(p for p in rs if p in starts),
                    "hole": fixed[0], "hole_margin": my_holes[fixed[0]][1], "filled_by": fixed[1], "value_give": gv, "value_get": rv_val})
    # Leanest version of each package first, so throw-ins that change nothing collapse into it.
    out.sort(key=lambda t: (-t["my_gain"], -t["their_gain"], len(t["give"]) + len(t["get"])))
    seen, per_partner, best = set(), {}, []
    for t in out:
        key = (t["partner"], t["core"])                # the players who would start for me; bench fillers do not make a new trade
        if key in seen or per_partner.get(t["partner"], 0) >= PER_PARTNER or _padded(t, best):
            continue
        seen.add(key)
        per_partner[t["partner"]] = per_partner.get(t["partner"], 0) + 1
        best.append(t)
    return best[:limit]
