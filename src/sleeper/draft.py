"""Draft state: pick order, my picks, the draftable pool, and the live poll step.

Everything except the fetch_* functions and Poller is a pure function over the
draft object, its picks and traded picks, so it is testable from fixtures.
"""
from __future__ import annotations

import json
import time
from collections import Counter

from . import api, players
from .api import CACHE_DIR, RateLimited, SleeperError, TransientError

STARTER_SLOTS = {
    "slots_qb": "QB", "slots_rb": "RB", "slots_wr": "WR",
    "slots_te": "TE", "slots_k": "K", "slots_def": "DEF",
}
FLEX_SLOTS = {
    "slots_flex": ("RB", "WR", "TE"),
    "slots_super_flex": ("QB", "RB", "WR", "TE"),
    "slots_rec_flex": ("WR", "TE"),
    "slots_wrrb_flex": ("WR", "RB"),
}
STALE_GRACE = 6.0


def fetch(draft_id: str, fresh: bool = False) -> dict:
    return api.get("sleeper", f"draft/{draft_id}", fresh=fresh)


def fetch_picks(draft_id: str, fresh: bool = False) -> list:
    return api.get("sleeper", f"draft/{draft_id}/picks", fresh=fresh)


def fetch_traded(draft_id: str) -> list:
    return api.get("sleeper", f"draft/{draft_id}/traded_picks")


def is_auction(draft: dict) -> bool:
    return draft.get("type") == "auction"


def total_picks(draft: dict) -> int:
    s = draft["settings"]
    return s["teams"] * s["rounds"]


def slot_for_pick(pick_no: int, teams: int, draft_type: str, reversal_round: int | None = 0) -> tuple[int, int]:
    rnd = (pick_no - 1) // teams + 1
    idx = (pick_no - 1) % teams
    r = reversal_round or 0
    forward = True if draft_type == "linear" else ((rnd % 2 == 1) != (r > 0 and rnd >= r))
    return rnd, (idx + 1 if forward else teams - idx)


def _slot(draft: dict, pick_no: int) -> tuple[int, int]:
    s = draft["settings"]
    return slot_for_pick(pick_no, s["teams"], draft["type"], s.get("reversal_round"))


def unfilled(draft: dict, picks: list) -> list[int]:
    filled = {p["pick_no"] for p in picks}
    return [n for n in range(1, total_picks(draft) + 1) if n not in filled]


def traded_map(draft: dict, traded: list) -> dict[tuple[int, int], int]:
    season = str(draft.get("season"))
    return {(t["round"], t["roster_id"]): t["owner_id"] for t in traded if str(t.get("season")) == season}


def picker(draft: dict, pick_no: int, tmap: dict) -> tuple[int, int, int]:
    """(roster_id that makes an unfilled pick, round, slot)."""
    rnd, slot = _slot(draft, pick_no)
    s2r = draft.get("slot_to_roster_id")
    if not s2r:
        raise SleeperError("draft order is not set yet")
    original = int(s2r[str(slot)])
    return tmap.get((rnd, original), original), rnd, slot


def roster_of_user(draft: dict, user_id: str, rosters: list | None = None) -> int | None:
    s2r = draft.get("slot_to_roster_id") or {}
    slot = (draft.get("draft_order") or {}).get(user_id)
    if slot is not None and str(slot) in s2r:
        return int(s2r[str(slot)])
    for r in rosters or []:
        if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or []):
            return int(r["roster_id"])
    return None


def slot_of_roster(draft: dict, roster_id: int) -> int | None:
    for slot, rid in (draft.get("slot_to_roster_id") or {}).items():
        if int(rid) == roster_id:
            return int(slot)
    return None


def labels(draft: dict, users: dict | None = None) -> dict[int, str]:
    """roster_id -> display name; slots nobody claimed read 'slot N (CPU)'."""
    users = users or {}
    s2r = draft.get("slot_to_roster_id") or {}
    out = {int(rid): f"slot {slot} (CPU)" for slot, rid in s2r.items()}
    for user_id, slot in (draft.get("draft_order") or {}).items():
        rid = s2r.get(str(slot))
        if rid is not None:
            out[int(rid)] = users.get(user_id, {}).get("display_name") or f"user {user_id}"
    return out


def my_next(draft: dict, picks: list, tmap: dict, my_roster_id: int | None) -> tuple[int | None, int | None]:
    """(my next unfilled pick_no, picks others must make before it), or (None, None)."""
    if is_auction(draft) or my_roster_id is None or not draft.get("slot_to_roster_id"):
        return None, None
    for i, q in enumerate(unfilled(draft, picks)):
        if picker(draft, q, tmap)[0] == my_roster_id:
            return q, i
    return None, None


def clock(draft: dict, now: float | None = None) -> str:
    now = now or time.time()
    meta = draft.get("metadata") or {}
    timer = draft["settings"].get("pick_timer") or 0
    if draft.get("status") == "paused" or meta.get("is_autopaused") == "true":
        return "paused"
    if draft.get("status") != "drafting":
        return draft.get("status") or "unknown"
    if is_auction(draft):
        end = meta.get("timer_end_at")
        return f"{max(0, int(int(end) / 1000 - now))} s" if end else "no timer"
    if not timer:
        return "no timer"
    last = max(draft.get("last_picked") or 0, draft.get("start_time") or 0)
    if not last:
        return "not started"
    elapsed = now - last / 1000
    if elapsed > timer + STALE_GRACE:
        return "overdue/stale"
    return f"~{int(timer - elapsed)} s (est)"


def is_prefilled(p: dict, next_pick_no: int | None, keeper_nos: frozenset | set = frozenset()) -> bool:
    """Keeper picks sit at their future pick_no before the draft reaches it; Sleeper doesn't always flag them."""
    return bool(p.get("is_keeper")) or p["pick_no"] in keeper_nos or (next_pick_no is not None and p["pick_no"] >= next_pick_no)


def keeper_file(draft_id: str):
    return CACHE_DIR / f"keepers.{draft_id}.json"


def remember_keepers(draft_id: str, picks: list, next_pick_no: int | None) -> frozenset:
    """Keeper pick numbers for this draft, remembered across commands: once the draft passes a keeper's slot it looks like a live pick."""
    path = keeper_file(draft_id)
    try:
        known = {int(n) for n in json.loads(path.read_text())}
    except (OSError, ValueError, TypeError):
        known = set()
    found = {p["pick_no"] for p in picks if p.get("is_keeper") or (next_pick_no is not None and p["pick_no"] > next_pick_no)}
    if not found <= known:
        known |= found
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(known)))
    return frozenset(known)


def slot_labels(draft: dict, users: dict | None = None) -> dict[int, str]:
    """slot -> display name (mock picks carry a slot but no roster_id)."""
    lab = labels(draft, users)
    return {int(slot): lab.get(int(rid), f"slot {slot}") for slot, rid in (draft.get("slot_to_roster_id") or {}).items()}


def pick_row(p: dict, lab: dict[int, str], next_pick_no: int | None = None, keeper_nos: frozenset | set = frozenset(),
             slot_lab: dict[int, str] | None = None) -> dict:
    m = p.get("metadata") or {}
    rid = p.get("roster_id")
    if rid is not None:
        by = lab.get(rid, f"roster {rid}")
    else:
        by = (slot_lab or {}).get(p.get("draft_slot") or 0, f"slot {p.get('draft_slot')}" if p.get("draft_slot") else "-")
    row = {
        "pick": p["pick_no"],
        "rd": p["round"],
        "slot": p.get("draft_slot"),
        "name": f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip() or f"#{p.get('player_id')}",
        "pos": m.get("position"),
        "team": m.get("team"),
        "by": by,
    }
    if is_prefilled(p, next_pick_no, keeper_nos):
        row["keeper"] = True
    if m.get("amount"):
        row["amount"] = m["amount"]
    return row


def live_picks(picks: list, next_pick_no: int | None, keeper_nos: frozenset | set = frozenset()) -> list[dict]:
    """Picks actually made so far, in order, without pre-filled keepers."""
    return [p for p in sorted(picks, key=lambda p: p["pick_no"]) if not is_prefilled(p, next_pick_no, keeper_nos)]


def picks_since(picks: list, since_pick: int, lab: dict[int, str], limit: int = 15, next_pick_no: int | None = None,
                keeper_nos: frozenset | set = frozenset(), slot_lab: dict[int, str] | None = None) -> list[dict]:
    """Live picks at or after `since_pick`; when more than `limit`, the first row says how many were left out."""
    rows = [pick_row(p, lab, next_pick_no, keeper_nos, slot_lab) for p in live_picks(picks, next_pick_no, keeper_nos) if p["pick_no"] >= since_pick]
    if len(rows) > limit:
        dropped = len(rows) - limit
        rows = rows[-limit:]
        rows.insert(0, {"pick": None, "rd": None, "slot": None, "name": f"... {dropped} earlier picks not shown (draft picks --last {limit + dropped})",
                        "pos": None, "team": None, "by": None})
    return rows


def is_mine(p: dict, my_roster: int | None, my_slot: int | None) -> bool:
    """Mock-draft picks carry a slot but no roster_id."""
    if p.get("roster_id") is not None:
        return p["roster_id"] == my_roster
    return my_slot is not None and p.get("draft_slot") == my_slot


def startable(draft: dict) -> set[str]:
    s = draft["settings"]
    out = {pos for k, pos in STARTER_SLOTS.items() if s.get(k)}
    for k, poss in FLEX_SLOTS.items():
        if s.get(k):
            out |= set(poss)
    return out or set(players.FANTASY)


def needs(draft: dict, my_positions: list[str]) -> dict:
    s = draft["settings"]
    counts = Counter(my_positions)
    starters_open: dict[str, int] = {}
    surplus: Counter = Counter()
    for k, pos in STARTER_SLOTS.items():
        n = s.get(k) or 0
        if counts[pos] < n:
            starters_open[pos] = n - counts[pos]
        elif counts[pos] > n:
            surplus[pos] = counts[pos] - n
    flex_open: dict[str, int] = {}
    for k, poss in FLEX_SLOTS.items():
        name = k.removeprefix("slots_")
        for _ in range(s.get(k) or 0):
            pos = next((p for p in poss if surplus[p] > 0), None)
            if pos:
                surplus[pos] -= 1
            else:
                flex_open[name] = flex_open.get(name, 0) + 1
    picks_left = s["rounds"] - len(my_positions)
    return {
        "picks_left": picks_left,
        "starters_open": starters_open,
        "flex_open": flex_open,
        "bench_open": max(0, picks_left - sum(starters_open.values()) - sum(flex_open.values())),
    }


def pool(draft: dict, picks: list, rostered: frozenset | set = frozenset()) -> list[dict]:
    """Slim rows still available in this draft, unranked."""
    drafted = {p["player_id"] for p in picks}
    ptype = draft["settings"].get("player_type") or 0
    positions = startable(draft)
    out = []
    for r in players.pool():
        if r["player_id"] in drafted or r["player_id"] in rostered or r["position"] not in positions:
            continue
        rookie = r.get("years_exp") == 0
        if (ptype == 1 and not rookie) or (ptype == 2 and rookie):
            continue
        out.append(r)
    return out


def status(draft: dict, picks: list, traded: list, my_user_id: str | None, users: dict | None = None, rosters: list | None = None,
           now: float | None = None, keeper_nos: frozenset | set = frozenset()) -> dict:
    s = draft["settings"]
    tmap = traded_map(draft, traded)
    lab = labels(draft, users)
    un = unfilled(draft, picks)
    nxt = un[0] if un else None
    live = live_picks(picks, nxt, keeper_nos)
    out = {
        "status": "complete" if nxt is None else draft.get("status"),
        "type": draft.get("type"),
        "teams": s["teams"],
        "rounds": s["rounds"],
        "picks_made": len(live),
        "keepers": len(picks) - len(live),
        "next_pick": nxt,
        "clock": clock(draft, now),
    }
    if not draft.get("slot_to_roster_id"):
        out["note"] = "draft order not set yet"
    elif nxt and not is_auction(draft):
        rid, rnd, slot = picker(draft, nxt, tmap)
        out["round"] = rnd
        out["on_the_clock"] = f"pick {nxt} (rd {rnd}, slot {slot}): {lab.get(rid)}"
    if my_user_id:
        my_roster = roster_of_user(draft, my_user_id, rosters)
        if my_roster is None:
            out.setdefault("note", "you are not in this draft")
        else:
            out["my_roster_id"] = my_roster
            out["my_slot"] = slot_of_roster(draft, my_roster)
            mn, away = my_next(draft, picks, tmap, my_roster)
            out["my_next_pick"] = mn
            out["picks_away"] = away
            if mn:
                out["my_next_round"] = _slot(draft, mn)[0]
            if is_auction(draft):
                spent = sum(int((p.get("metadata") or {}).get("amount") or 0) for p in picks if is_mine(p, my_roster, out["my_slot"]))
                out["budget_left"] = (s.get("budget") or 0) - spent
    if is_auction(draft):
        out["note"] = "auction: no pick order"
    return out


class Poller:
    """One live-draft poll per step(); the sleep loop belongs to the caller."""

    def __init__(self, draft_id: str, my_user_id: str | None, picks_away: int = 1, since_pick: int = 0,
                 users: dict | None = None, rosters: list | None = None, refresh_every: int = 5):
        self.draft_id = draft_id
        self.my_user_id = my_user_id
        self.picks_away = picks_away
        self.since_pick = since_pick
        self.users = users
        self.rosters = rosters
        self.refresh_every = refresh_every
        self.draft: dict | None = None
        self.picks: list = []
        self.traded: list = []
        self.keeper_nos: set[int] = set()
        self.calls = 0
        self.rate_limited = 0
        self.picks_seen_at_start: int | None = None

    def step(self) -> dict | None:
        """Fetch once. Returns a result when the wait should end, else None."""
        try:
            picks = fetch_picks(self.draft_id, fresh=True)
            if self.draft is None or self.calls % self.refresh_every == 0 or len(picks) != len(self.picks):
                self.draft = fetch(self.draft_id, fresh=True)
            if self.calls % self.refresh_every == 0:
                self.traded = fetch_traded(self.draft_id)
            self.picks = picks
            self.rate_limited = 0
        except RateLimited:
            self.rate_limited += 1
            return self.result("rate_limited") if self.rate_limited >= 2 else None
        except TransientError:
            return None
        finally:
            self.calls += 1
        un = unfilled(self.draft, self.picks)
        self.keeper_nos |= remember_keepers(self.draft_id, self.picks, un[0] if un else None)
        st = self.status()
        if st["next_pick"] is None or st["status"] == "complete":
            return self.result("complete", st)
        if not self.draft.get("slot_to_roster_id"):
            return None
        if is_auction(self.draft):
            if self.picks_seen_at_start is None:
                self.picks_seen_at_start = len(self.picks)
            elif len(self.picks) > self.picks_seen_at_start:
                return self.result("new_pick", st)
        elif "my_roster_id" in st and st["my_next_pick"] is None:
            return self.result("no_picks_left", st)
        elif st.get("picks_away") is not None and st["picks_away"] <= self.picks_away and st["my_next_pick"] != self.since_pick:
            # A my_turn result hands back my_next_pick as since_pick, so the same turn is not reported twice.
            return self.result("my_turn", st)
        return None

    def status(self) -> dict:
        if self.draft is None:
            return {"status": "unknown", "next_pick": None, "clock": "no data"}
        return status(self.draft, self.picks, self.traded, self.my_user_id, self.users, self.rosters, keeper_nos=self.keeper_nos)

    def result(self, reason: str, st: dict | None = None) -> dict:
        lab = labels(self.draft, self.users) if self.draft else {}
        slot_lab = slot_labels(self.draft, self.users) if self.draft else {}
        st = st or self.status()
        return {
            "reason": reason,
            "status": st,
            "picks_since": picks_since(self.picks, self.since_pick, lab, limit=40, next_pick_no=st.get("next_pick"), keeper_nos=self.keeper_nos, slot_lab=slot_lab),
            "draft": self.draft,
            "picks": self.picks,
            "traded": self.traded,
        }
