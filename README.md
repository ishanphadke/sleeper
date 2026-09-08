# sleeper

A read-only command line tool over the [Sleeper](https://docs.sleeper.com/) fantasy
football API, with ESPN's draft board joined alongside and an optional FantasyPros
overlay. It is built to be driven by Claude during a live draft: one command returns
everything needed to decide a pick, and another blocks until you are on the clock.

It reads. It cannot make picks, set lineups, claim waivers, or change anything in
your league — you do that in the Sleeper app.

## Install

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The only runtime
dependency is `httpx`.

```bash
uv sync
uv run sleeper whoami
```

## Configure

`~/.config/sleeper/config.toml` says who the tool acts as and which league it reads:

```toml
username = "your_sleeper_name"
user_id = "1400000000000000000"   # optional, saves a lookup per run
league_id = "1401288250064134144"
draft_id = "1401288251616047104"  # optional, defaults to the league's draft
season = 2026                      # optional, defaults to the current season

# Optional: force a join when a name does not match across sources.
[rankings.espn_overrides]          # ESPN player id -> Sleeper player id
"4262921" = "9502"

[rankings.overrides]               # FantasyPros player id -> Sleeper player id
"17240" = "8205"
```

`~/.config/sleeper/plan.toml` is an optional draft plan. `draft outlook` marks your
targets as available or gone, and warns when a name on the avoid list is on the board:

```toml
notes = """
RB-RB unless a top-3 WR falls to 6. QB from round 8. K/DEF in the last two rounds.
"""
targets = ["Drake London", "Kyren Williams", "Trey McBride"]
avoid = ["Christian McCaffrey"]
```

A `.env` in the project root supplies the optional FantasyPros key. It is gitignored,
and everything works without it — you simply lose the `tier` column:

```
FANTASYPROS_API_KEY=...
```

The free FantasyPros tier returns ten rows per call, so tiers are filled for only a
handful of players. A paid key of at least a hundred rows promotes FantasyPros to the
source of ranks and tiers.

## Commands

Output is an aligned text table. Add `--json` to compute over the result. Errors print
`error: ...` on stderr and exit 1.

| Command | What it returns |
|---|---|
| `whoami` | who the CLI acts as, NFL week, cache ages, requests sent per host in the last minute |
| `leagues [--season N]` | your leagues with format and scoring |
| `league [--league ID]` | format line, scoring, team table with records |
| `roster [--league ID] [--team X]` | one roster with names (default yours) |
| `matchups [--week N] [--detail]` | matchups with points; `--detail` adds starters |
| `transactions [--week N] [--type ...] [--limit N]` | moves with names, newest first |
| `players <query> [--pos QB] [--limit N]` | search the player dictionary; `--refresh` re-downloads it |
| `trending [--kind add\|drop] [--hours N]` | most added or dropped across Sleeper |
| `injuries [--limit N] [--no-save]` | injury and IR changes since the last check: your roster plus the best sidelined players nobody owns |
| `drafts [--season N]` | your drafts with status, format and slot |
| `draft status [--draft ID\|URL]` | on the clock, your next pick, clock estimate, last picks |
| `draft picks [--round N] [--team X] [--last N]` | picks made so far, names included |
| `draft available [--pos RB] [--limit N]` | best available with rank, ADP, projections, depth-chart slot |
| `draft outlook [--limit N]` | one call to decide a pick: needs, best available, tiers, byes, fallers, targets, who is likely gone by your next turn |
| `draft plan [--limit N]` | for each remaining pick, who is likely still there by ADP |
| `draft wait [--picks-away N] [--timeout S] [--since P]` | poll until you are on the clock |
| `draft watch [--picks-away N]` | self-refreshing terminal board |
| `plan` | the draft plan, with any names that did not resolve |
| `rankings refresh [--scoring PPR\|HALF\|STD]` | rebuild the rankings cache |
| `raw <path>` | GET any Sleeper `/v1` path |

`--draft` accepts an id or a pasted `sleeper.com/draft/nfl/<id>` URL.

## Draft day

Refresh first, then arm the alert:

```bash
uv run sleeper players --refresh
uv run sleeper rankings refresh          # must report source: sleeper+espn
uv run sleeper draft wait --timeout 590   # returns when you are on the clock
uv run sleeper draft outlook              # ~1 s, everything needed to pick
```

`draft wait` polls once a second and returns a full outlook the moment your turn opens,
so no second call is needed while the pick clock runs. It also returns a `since_pick`
cursor; pass it back as `--since` to re-arm without re-reporting the turn it just
reported.

The outlook flags a few things worth knowing about:

- **byes** groups your skill players by bye week and marks a week with `!` when it holds
  three of them or two at one position. Candidates on such a week show `bye 7!`.
- **fallers** names QBs, RBs and WRs still available well past their ADP, with the fall
  in picks. Value first, plan second.
- **check_news** lists players whose projections look wrong: a mid-round player under six
  points a game, or Sleeper and ESPN disagreeing by five or more. They are kept out of the
  recommendation list and marked `?` until you check the news.
- **dc** is the player's slot on Sleeper's depth chart. Preseason charts go stale; treat
  it as a prompt, not a fact.

## Rate limits

Every request passes through a token bucket shared across processes via a lock file in
the cache directory, so two runs at once cannot exceed the cap between them.

| Host | Requests per minute |
|---|---|
| Sleeper | 500 |
| ESPN | 5 |
| FantasyPros | 1 |

A 429 pauses that host for every process, honouring `Retry-After` up to five minutes.

## API notes

Worth recording, because the official docs do not mention any of it:

- The player dictionary at `/v1/players/nfl` is about 15 MB. Fetch it at most once a day;
  `sleeper players --refresh` stores a slimmed copy and revalidates with an ETag.
- Sleeper's own projections and ADP come from an undocumented endpoint,
  `GET https://api.sleeper.app/projections/nfl/{season}?season_type=regular&order_by=adp_ppr`.
  These are the numbers the Sleeper app itself shows. An ADP of 999 means no ADP.
- Keeper picks are pre-filled into `/draft/<id>/picks` with `is_keeper` set to null, so
  they are indistinguishable from live picks once the draft passes them. The tool records
  which pick numbers were pre-filled before the draft starts.
- Picks are served through a CDN with a 15 second cache; a cache-busting query parameter
  reaches the origin.
- There is **no injuries endpoint**. `/v1/players/nfl/injuries`, `/v1/injuries/nfl` and the
  seasonal variants all return 404, and there is no event feed either. Injury data lives in
  the player dictionary and in an undocumented per-player view,
  `GET /v1/players/nfl/<player_id>`, which returns about a kilobyte, is cached ten minutes,
  and carries `injury_status`, `injury_body_part`, `injury_notes`, `injury_start_date` and
  `practice_participation`. `sleeper injuries` polls that view for a watchlist and diffs the
  result against a stored snapshot, which is the only way to see someone come off IR.

## Cache and state

Everything lives in `~/.cache/sleeper`: the slimmed player dictionary and its ETag, the
rankings cache per season and scoring, remembered keeper picks per draft, rate limiter
state, and any large `raw` bodies. Deleting the directory costs nothing but a refresh.

## Tests

```bash
uv run pytest
```

155 tests, no network access. Fixtures are captured API responses in `tests/fixtures`.
