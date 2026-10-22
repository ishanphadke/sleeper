"""Trade finder: trades that lift a weak starting slot of mine without weakening the partner.

Every candidate is scored by best-lineup points per remaining week for both rosters, before
and after, with empty slots filled from waivers (so byes are never a reason to trade). A cheap
screen on rest-of-season averages cuts the search; every survivor gets the full week-by-week score."""
from __future__ import annotations

from itertools import combinations

from . import lineup as LU

TRADE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
MIN_GAIN = 0.5
PER_PARTNER = 3


def ros_avg(weekly: dict[int, dict[str, float]]) -> dict[str, float]:
    """Projected points per game played over the remaining weeks (byes left out)."""
    tot: dict[str, list[float]] = {}
    for pts in weekly.values():
        for pid, v in pts.items():
            tot.setdefault(pid, []).append(v)
    n = max(1, len(weekly))
    return {pid: sum(v) / len(v) for pid, v in tot.items() if len(v) >= n / 2}


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
         tolerance: float = 0.0) -> list[dict]:
    """Trades ranked by my gain in weighted points per week, keeping only those the partner does not lose by more than `tolerance`.

    `me` and each rival are {"name", "players", "reserve", "limit"}. `fills` holds each week's best
    healthy free agents (a bye is streamed); `fill` the best healthy free agents by season average."""
    avg = ros_avg(weekly)

    def season(roster):
        return LU.best_lineup(roster, pos_of, avg, slots, fill)[0]

    def full(roster):
        return LU.strength(roster, pos_of, weekly, slots, playoff, fills)["score"]

    base_me = season(me["players"])
    screened = []
    for rv in rivals:
        base_rv = season(rv["players"])
        for gs, rs in candidates(me["players"], rv["players"], pos_of, avg):
            mine_after, my_cut = after_trade(me["players"], gs, rs, me["limit"], avg, pos_of, slots, fill, me["reserve"])
            gain = season(mine_after) - base_me
            if gain < MIN_GAIN:
                continue
            theirs_after, their_cut = after_trade(rv["players"], rs, gs, rv["limit"], avg, pos_of, slots, fill, rv["reserve"])
            their_gain = season(theirs_after) - base_rv
            if their_gain < -tolerance - 1.0:          # the screen is approximate; leave a margin for the full score
                continue
            screened.append((gain, rv, gs, rs, mine_after, theirs_after, my_cut, their_cut))
    screened.sort(key=lambda x: -x[0])

    full_me = full(me["players"])
    full_base = {rv["name"]: full(rv["players"]) for rv in rivals}
    out = []
    for gain, rv, gs, rs, mine_after, theirs_after, my_cut, their_cut in screened:   # every survivor: a global cut hid whole partners
        mg = full(mine_after) - full_me
        tg = full(theirs_after) - full_base[rv["name"]]
        if mg < MIN_GAIN or tg < -tolerance:
            continue
        starts = {pid for _, pid, _ in LU.best_lineup(mine_after, pos_of, avg, slots, fill)[1]}
        out.append({"partner": rv["name"], "give": gs, "get": rs, "my_gain": round(mg, 1), "their_gain": round(tg, 1),
                    "i_cut": my_cut, "they_cut": their_cut, "core": frozenset(p for p in rs if p in starts)})
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
