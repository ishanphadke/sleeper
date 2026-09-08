"""argparse front end over tools.py. Default output is an aligned text table; --json prints the dict."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

from . import api
from . import draft as D
from . import players as P
from . import rankings as R
from . import tools
from .api import CACHE_DIR, SleeperError

RAW_SPILL_BYTES = 8 * 1024


def render(d, indent: int = 0) -> str:
    pad = " " * indent
    lines = []
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                lines.append(f"{pad}{k}:")
                lines.append(table(v, indent + 2))
            elif isinstance(v, list):
                lines.append(f"{pad}{k}: {', '.join(str(x) for x in v) or '-'}")
            elif isinstance(v, dict):
                lines.append(f"{pad}{k}:")
                lines.append(render(v, indent + 2))
            else:
                lines.append(f"{pad}{k}: {'-' if v is None else v}")
    elif isinstance(d, list):
        lines.append(table(d, indent) if d and all(isinstance(x, dict) for x in d) else pad + ", ".join(map(str, d)))
    else:
        lines.append(f"{pad}{d}")
    return "\n".join(lines)


def table(rows: list[dict], indent: int = 0) -> str:
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    cells = [[_cell(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    pad = " " * indent
    out = [pad + "  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()]
    out += [pad + "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(out)


def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def notify(title: str, text: str) -> None:
    script = f'display notification "{text}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


# ----------------------------------------------------------------- CLI-only commands

def raw(path: str) -> dict | list:
    data = api.get("sleeper", path)
    body = json.dumps(data)
    if len(body) <= RAW_SPILL_BYTES:
        return data
    out = CACHE_DIR / "raw"
    out.mkdir(parents=True, exist_ok=True)
    f = out / (hashlib.sha1(path.encode()).hexdigest()[:12] + ".json")
    f.write_text(body)
    shape = f"list[{len(data)}]" if isinstance(data, list) else f"dict keys: {', '.join(list(data)[:20])}"
    sample = data[0] if isinstance(data, list) and data else {k: data[k] for k in list(data)[:5]} if isinstance(data, dict) else data
    return {"path": str(f), "bytes": len(body), "shape": shape, "sample": json.dumps(sample)[:600]}


def rankings_refresh(scoring: str | None) -> dict:
    cfg = tools.load_config()
    season = tools._season(None)
    league = api.get("sleeper", f"league/{tools._league_id(None)}") if not scoring or cfg.get("league_id") else None
    scoring = scoring or R.scoring_for(league)
    superflex = "SUPER_FLEX" in (league.get("roster_positions") or []) if league else False
    P.ensure(24, allow_refresh=True)
    rk = cfg.get("rankings") or {}
    overrides = {str(k): str(v) for k, v in (rk.get("overrides") or {}).items()}
    espn_overrides = {str(k): str(v) for k, v in (rk.get("espn_overrides") or {}).items()}
    print(f"fetching Sleeper {season} projections (one call), then ESPN {R.espn_rank_type(scoring, superflex)} board (two calls)", file=sys.stderr)
    if os.environ.get("FANTASYPROS_API_KEY"):
        print(f"then FantasyPros {scoring} rankings (two calls, about a minute under the 1/min cap)", file=sys.stderr)
    return R.refresh(season, scoring, overrides, superflex=superflex, espn_overrides=espn_overrides)


def primary_unmatched(result: dict) -> bool:
    """True when the source that supplies the board has unmatched rows in its top 250 (Sleeper rows are keyed by Sleeper id and need no join)."""
    primary = result.get((result.get("source") or "").removeprefix("sleeper+"))
    return bool(isinstance(primary, dict) and primary.get("unmatched"))


# ----------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result as JSON")
    lg = argparse.ArgumentParser(add_help=False)
    lg.add_argument("--league", help="league id (default: config)")
    dr = argparse.ArgumentParser(add_help=False)
    dr.add_argument("--draft", help="draft id or sleeper.com draft URL (default: config / league)")

    p = argparse.ArgumentParser(prog="sleeper", description="Sleeper fantasy football CLI for Claude")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", parents=[common]).set_defaults(run=lambda a: tools.whoami())
    s = sub.add_parser("leagues", parents=[common]); s.add_argument("--season", type=int)
    s.set_defaults(run=lambda a: tools.leagues(a.season))
    s = sub.add_parser("league", parents=[common, lg]); s.set_defaults(run=lambda a: tools.league(a.league))
    s = sub.add_parser("roster", parents=[common, lg]); s.add_argument("--team")
    s.set_defaults(run=lambda a: tools.roster(a.league, a.team))
    s = sub.add_parser("matchups", parents=[common, lg]); s.add_argument("--week", type=int); s.add_argument("--detail", action="store_true")
    s.set_defaults(run=lambda a: tools.matchups(a.league, a.week, a.detail))
    s = sub.add_parser("transactions", parents=[common, lg]); s.add_argument("--week", type=int); s.add_argument("--type", choices=["waiver", "free_agent", "trade"]); s.add_argument("--limit", type=int, default=25)
    s.set_defaults(run=lambda a: tools.transactions(a.league, a.week, a.type, a.limit))
    s = sub.add_parser("players", parents=[common]); s.add_argument("query", nargs="?"); s.add_argument("--pos"); s.add_argument("--limit", type=int, default=10); s.add_argument("--refresh", action="store_true", help="re-download the player dictionary")
    s.set_defaults(run=_players)
    s = sub.add_parser("trending", parents=[common]); s.add_argument("--kind", choices=["add", "drop"], default="add"); s.add_argument("--hours", type=int, default=24); s.add_argument("--limit", type=int, default=25)
    s.set_defaults(run=lambda a: tools.trending(a.kind, a.hours, a.limit))
    s = sub.add_parser("injuries", parents=[common, lg], help="injury/IR changes since the last check")
    s.add_argument("--limit", type=int, default=tools.I.WATCH_LIMIT); s.add_argument("--no-save", action="store_true", help="report without moving the snapshot forward")
    s.set_defaults(run=lambda a: tools.injuries(a.league, a.limit, not a.no_save))
    s = sub.add_parser("drafts", parents=[common]); s.add_argument("--season", type=int)
    s.set_defaults(run=lambda a: tools.drafts(a.season))

    d = sub.add_parser("draft", help="live-draft tools").add_subparsers(dest="sub", required=True)
    s = d.add_parser("status", parents=[common, dr]); s.set_defaults(run=lambda a: tools.draft_status(a.draft))
    s = d.add_parser("picks", parents=[common, dr]); s.add_argument("--round", type=int); s.add_argument("--team"); s.add_argument("--last", type=int)
    s.set_defaults(run=lambda a: tools.draft_picks(a.draft, a.round, a.team, a.last))
    s = d.add_parser("available", parents=[common, dr]); s.add_argument("--pos"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(run=lambda a: tools.draft_available(a.draft, a.pos, a.limit))
    s = d.add_parser("outlook", parents=[common, dr]); s.add_argument("--limit", type=int, default=12)
    s.set_defaults(run=lambda a: tools.draft_outlook(a.draft, a.limit))
    s = d.add_parser("plan", parents=[common, dr]); s.add_argument("--limit", type=int, default=6)
    s.set_defaults(run=lambda a: tools.draft_plan(a.draft, a.limit))
    s = d.add_parser("wait", parents=[common, dr])
    s.add_argument("--picks-away", type=int, default=0, help="return when my pick is this close (0 = only when I am on the clock)")
    s.add_argument("--timeout", type=int, default=100, help="seconds, max 590")
    s.add_argument("--since", type=int, default=0, help="pick number the previous call returned as since_pick")
    s.add_argument("--notify", action="store_true", help="macOS notification when the wait returns")
    s.set_defaults(run=_wait)
    s = d.add_parser("watch", parents=[dr], help="self-refreshing board: on the clock, needs, best available; Ctrl-C to stop")
    s.add_argument("--picks-away", type=int, default=4, help="notify when my pick is this close")
    s.add_argument("--limit", type=int, default=12); s.add_argument("--interval", type=float, default=3.0)
    s.add_argument("--notify", action="store_true"); s.add_argument("--once", action="store_true", help="render once and exit")
    s.set_defaults(run=_watch, json=False)

    r = sub.add_parser("rankings", help="draft rankings cache (Sleeper projections/ADP, ESPN board, FantasyPros overlay)").add_subparsers(dest="sub", required=True)
    s = r.add_parser("refresh", parents=[common]); s.add_argument("--scoring", choices=["PPR", "HALF", "STD"])
    s.set_defaults(run=lambda a: rankings_refresh(a.scoring), exit_on_unmatched=True)

    sub.add_parser("plan", parents=[common], help="show the draft plan from ~/.config/sleeper/plan.toml").set_defaults(run=lambda a: tools.plan())
    s = sub.add_parser("raw", parents=[common], help="GET any /v1 path (large bodies are written to a file)"); s.add_argument("path")
    s.set_defaults(run=lambda a: raw(a.path), always_json=True)
    return p


def _players(a):
    if a.refresh:
        return P.refresh()
    if not a.query:
        raise SleeperError("give a query or --refresh")
    return tools.players(a.query, a.pos, a.limit)


def _wait(a):
    out = tools.draft_wait(a.draft, a.picks_away, a.timeout, a.since)
    if a.notify:
        st = out.get("status") or {}
        notify("Sleeper draft", f"{out['reason']}: pick {st.get('next_pick')}, clock {st.get('clock')}")
    return out


def _watch(a):
    did = tools._draft_id(a.draft)
    env = tools._draft_env(did, fresh=False)
    P.ensure(24 * 7, allow_refresh=False)
    poller = D.Poller(did, tools.me()["user_id"], a.picks_away, 0, env["users"], env["rosters"])
    notified_for = None
    while True:
        res = poller.step()
        out = tools.live_outlook(poller, env, a.limit)
        if a.once:
            return out or {"note": "no data yet"}
        if out:
            sys.stdout.write("\x1b[2J\x1b[H" + render(out) + "\n")
            sys.stdout.flush()
            st = out["status"]
            close = st.get("picks_away") is not None and st["picks_away"] <= a.picks_away
            if close and st.get("my_next_pick") != notified_for:
                notified_for = st.get("my_next_pick")
                sys.stdout.write("\a")
                if a.notify:
                    notify("Sleeper draft", f"pick {st['my_next_pick']} is {st['picks_away']} away")
        if res and res["reason"] in ("complete", "no_picks_left"):
            return None
        time.sleep(a.interval)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.run(args)
    except SleeperError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if result is None:
        return 0
    print(json.dumps(result, indent=1, ensure_ascii=False) if args.json or getattr(args, "always_json", False) else render(result))
    if getattr(args, "exit_on_unmatched", False) and primary_unmatched(result):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
