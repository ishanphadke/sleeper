# sleeper — Sleeper API tooling for Claude

Design document. Status: **v5, implemented 2026-09-05** — v2 was reviewed
by a 3-designer / 4-critic panel; v3 added FantasyPros as the rankings
source; v4 added hard per-host request caps; v5 made ESPN's public fantasy
API the draft board after the FantasyPros free key turned out to return 10
rows per call. §12 records where the code differs from the text above.
Facts marked *verified* were checked against the live API or real public
drafts that day. **Draft: Tuesday 2026-09-08.**

## 1. Goal

A Python CLI that lets Claude (Claude Code via Bash today, Claude Desktop via
MCP later) answer questions about my Sleeper leagues and help me during a live
draft. The CLI does deterministic data work — fetch, join, filter, rank; Claude
does the judgment.

Rules:

- **Read-only.** The public API has no write endpoints. I pick in the Sleeper
  app; the tool tells me what to think about.
- **Context-safe.** No tool emits a raw API payload. Every list is capped,
  player rows are trimmed, ids are resolved to names.
- **MCP-ready without a rewrite.** Every command is a pure function in
  `tools.py`; CLI now and MCP later are thin wrappers.
- **Minimal.** One runtime dependency (`httpx`). Three upstream hosts: Sleeper
  (leagues, drafts, players, and its own projections/ADP endpoint, §2.3),
  ESPN (draft board: rank, ADP, bye, projections) and FantasyPros (optional
  overlay: tiers). No IDP or auction strategy; the only undocumented
  endpoints are ESPN's read API and Sleeper's read-only projections list.
- **Capped.** Every outbound request passes through a per-host limiter
  shared across processes: **Sleeper 500/min, ESPN 5/min, FantasyPros
  1/min.** A 429 from any host pauses that host for 60 s in every process.
  There is no bypass, including `raw`.

Timing: the 2026 season starts **2026-09-09**; draft tools are built first.

## 2. API facts the design depends on

Base `https://api.sleeper.app/v1`, no auth, GET only, "stay under 1000
calls/min", non-commercial. Served through Cloudflare.

| Endpoint | Notes |
|---|---|
| `/state/nfl` | `season`, `week`, `season_start_date` |
| `/user/<username or id>` | `user_id`, `username`, `display_name`. **Unknown user returns HTTP 200 with body `null`** (*verified*) — treat a null body as not-found everywhere. |
| `/user/<id>/leagues/nfl/<season>` | league objects |
| `/user/<id>/drafts/nfl/<season>` | draft objects **with `draft_order` and `slot_to_roster_id` null on every entry** and `status`/`last_picked` sometimes stale (*verified*, 0/43 entries carried draft_order). Use only to enumerate ids; fetch `/draft/<id>` for detail. Mock drafts were **not** observed in this list — see §10. |
| `/league/<id>` | `name`, `status`, `season`, `total_rosters`, `roster_positions` (e.g. `[QB,RB,RB,WR,WR,TE,FLEX,FLEX,DEF,BN×6]`; also `SUPER_FLEX`, `REC_FLEX`, `IDP_*`), `scoring_settings` (flat: `rec`, `pass_td`, `bonus_rec_te`, …), `settings.type` (0 redraft, 1 keeper, 2 dynasty, 3 seen in the wild), `draft_id`, `previous_league_id` |
| `/league/<id>/users` | `user_id`, `display_name`, `metadata.team_name` |
| `/league/<id>/rosters` | `roster_id`, `owner_id`, `co_owners[]`, `players[]`, `starters[]`, `reserve[]`, `taxi[]`, `settings.{wins,losses,ties,fpts,fpts_decimal,fpts_against,waiver_position,waiver_budget_used}` |
| `/league/<id>/matchups/<week>` | per roster: `matchup_id`, `points`, `starters[]`, `starters_points[]`, `players_points{}` |
| `/league/<id>/transactions/<week>` | `type` waiver/free_agent/trade, `status`, `adds{pid:roster_id}`, `drops{}`, `draft_picks[]`, `roster_ids[]`, `settings.waiver_bid`, `created` |
| `/league/<id>/drafts` | draft objects, **with** `draft_order` (*verified*) |
| `/draft/<id>` | `status` ∈ `pre_draft` · `drafting` · `paused` · `complete` (*verified*); `type` ∈ `snake` · `linear` · `auction`; `draft_order{user_id: slot}` (null until the order is set; only humans appear — CPU slots are absent); `slot_to_roster_id{slot: roster_id}`; `settings.{teams, rounds, pick_timer (0/null/30/60/90 seen), reversal_round (0 or the 3RR round), player_type (0 all, 1 rookies only, 2 vets only), cpu_autopick, budget (auction), slots_*}`; `last_picked` ms; `start_time` ms; `metadata.{scoring_type, is_autopaused, timer_end_at (auction)}`; `league_id` |
| `/draft/<id>/picks` | `pick_no`, `round`, `draft_slot`, `roster_id` (**the current owner** — already accounts for trades), `picked_by` (user_id, `""` for CPU/autopick), `player_id`, `is_keeper` (**null on real keeper picks** — my league's 11 keepers all carry `null`, so keepers are detected as filled picks with `pick_no >= next_pick_no` instead), `metadata.{first_name,last_name,position,team,injury_status,status,amount (auction)}`. **Names come with the pick.** Keeper picks are pre-populated at their future `pick_no` before the draft starts (*verified*: draft `1389736356023918593`, 14 keepers at picks 10, 12, 25, 44, …). |
| `/draft/<id>/traded_picks` | `{season, round, roster_id (original), owner_id (current), previous_owner_id}` |
| `/players/nfl` | **~14.6 MB** (docs say 5 MB — stale), 12,226 entries. Useful: `full_name` (**null for DEF** — compose `first_name last_name`), `position`, `fantasy_positions[]` (**124 players, mostly `FB`, are fantasy-eligible only via this field**), `team` (null ⇒ free agent/retired, even when `active` is true), `status` (`Active`, `Inactive`, `Injured Reserve`, …), `active`, `age`, `years_exp`, `injury_status`, `depth_chart_order`, `search_rank` (`9999999` or null = unranked), `search_full_name` (null for DEF). DEF entries keyed by team abbr (`"KC"`). **No bye week, no ADP.** Supports `If-None-Match` → 304 (*verified*). |
| `/players/nfl/trending/add\|drop?lookback_hours=&limit=` | `[{player_id, count}]` |

CDN behaviour (*verified*): picks `s-maxage=15` (86400 once complete), draft
object `s-maxage=30`, both `stale-while-revalidate=300`; a cache-buster query
param (`?_=<ms>`) yields `cf-cache-status: MISS`, i.e. hits origin. Typical
latency 44–188 ms.

Types to remember: `slot_to_roster_id` keys are strings, `draft_order` values
ints, `picked_by`/`owner_id` are user-id strings, `roster_id` ints,
timestamps ms.

Not available from Sleeper's public `/v1` API: rankings, ADP, bye weeks,
projections. Sleeper's undocumented projections endpoint (§2.3) supplies
projections and ADP keyed by Sleeper id; ESPN (§2.2) supplies the board and
bye weeks alongside, with FantasyPros (§2.1) as an overlay. Per-pick
timestamps and push don't exist anywhere; polling only.

### 2.1 FantasyPros public API v2 (rankings, ADP, bye weeks)

Base `https://api.fantasypros.com/public/v2/json`, header `x-api-key`, free
non-commercial key, GET only. OpenAPI 3.1 spec at
`https://api.fantasypros.com/public/v2/docs/fantasypros_v2_public.yml` — the
facts below are read from the spec, not yet exercised with the key (§10).
**No rate limit is documented**, so the design treats calls as scarce: fetch
once, cache, never call from the draft loop.

| Endpoint | Notes |
|---|---|
| `/nfl/{season}/consensus-rankings?position=ALL&type=DRAFT&scoring=PPR&week=0` | `players[]`: `player_id` (FP id), `player_name`, `player_team_id`, `player_position_id`, `player_positions`, `rank_ecr` (int, occasionally string), `tier` (int), `pos_rank` (`RB1`), `player_bye_week` (string), `sportsdata_id` (Sportradar uuid — Sleeper has `sportradar_id`), `player_yahoo_id` (Sleeper has `yahoo_id`), `player_owned_avg`, `player_ecr_delta`. Meta: `count`, `total_experts`, `last_updated_ts`. `scoring` ∈ `STD\|PPR\|HALF`, `type` ∈ `DRAFT\|ADP\|ROS\|…`, `position` ∈ `ALL\|QB\|RB\|WR\|TE\|K\|DST\|FLX\|OP\|…`, optional `include_idp=true`. |
| `/nfl/players?ecr=included&external_ids=yahoo:espn` | `players[]`: `player_id`, `player_name`, `position_id`, `positions[]`, `team_id`, `sportsdata_player_id` (Sportradar uuid), `rank_ecr`, `rank_ecr_ppr`, `rank_ecr_half`, `rank_ecr_pos`, `rank_adp`, `rank_adp_ppr`, `rookie` (`Y`), `age`, plus the requested external ids. No tier, no bye. |
| `/nfl/{season}/projections?position=RB&week=0` | `points`, `points_ppr`, `points_half` + stat lines. Not used in v1. |

Errors: HTTP 400 with `{message, parameter, valid_format}`. FP team
abbreviations differ from Sleeper's in a few cases (`JAC`/`JAX`, `WSH`/`WAS`,
`LA`/`LAR`) — one small map.

**Free-key finding (*verified* 2026-09-05):** with a free key both endpoints
return HTTP 200 but only **10 rows per call**, and the body says so:
`limit: 10`, `public_api_limited: true`, `tier: "free"` (`count` still
reports the full 526/517). Ten rows cannot be a draft board, which is why
ESPN below supplies the board and FantasyPros only decorates the rows it
returns. A paid key that returns ≥ `MIN_BOARD` (100) rows makes FantasyPros
ECR the board again with no code change.

### 2.2 ESPN fantasy API (draft board: rank, ADP, bye weeks)

Base `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl`, no auth, GET
only, served through CloudFront. Undocumented but stable for years (it is what
the ESPN fantasy web app reads). All facts *verified* 2026-09-05 with curl.

| Endpoint | Notes |
|---|---|
| `/seasons/{season}/segments/0/leaguedefaults/3?view=kona_player_info` with header `X-Fantasy-Filter: {"players":{"limit":500,"sortDraftRanks":{"sortPriority":100,"sortAsc":true,"value":"PPR"},"filterRanksForScoringPeriodIds":{"value":[1]},"filterRanksForRankTypes":{"value":["PPR"]}}}` | `players[]`, each `{player: {id (int; **D/ST ids are negative**, e.g. `-16007`), fullName (`"Broncos D/ST"` for defenses), defaultPositionId (1 QB, 2 RB, 3 WR, 4 TE, 5 K, 16 D/ST; other ids are non-fantasy), proTeamId, injuryStatus (`ACTIVE`, `QUESTIONABLE`, …, null for D/ST), injured, eligibleSlots[], ownership: {averageDraftPosition (float, `1.32`), percentOwned, auctionValueAverage}, draftRanksByRankType: {PPR: {rank}, STANDARD: {rank}, SUPERFLEX: {rank}, ELIMINATION: {rank}} — **every rank type is present whatever the filter asks for**, the filter only sorts — stats: [...]}}`. `stats` is ~30–40 KB per player; the 400-row body is 12 MB, so the raw cache strips it. Ranks have gaps (players ESPN ranks but doesn't list). Header `X-Fantasy-Filter-Player-Count` carries the unfiltered total (1036). `limit` up to at least 500 works. `filterSlotIds: {"value":[16]}` restricts to D/ST. |
| `/seasons/{season}?view=proTeamSchedules_wl` | `settings.proTeams[]`: `{id, abbrev, byeWeek, name, location, proGamesByScoringPeriod}` — 33 entries: 32 teams plus `id 0 / FA / byeWeek 0`. Team ids are not contiguous (31–32 unused; BAL 33, HOU 34). `WSH` is the only abbreviation Sleeper spells differently (`WAS`); `players.team()` already maps it. 109 KB. |

Headers: the CLI's own `User-Agent: sleeper-cli/0.1.0` is accepted (HTTP 200
on both endpoints — no browser UA needed). No rate-limit headers are sent;
`cache-control: max-age=5` on the player list and `max-age=300` on the team
list, weak ETags on both. The design still treats ESPN as scarce: cap 5/min,
two calls per `rankings refresh`, never called from the draft loop.

### 2.3 Sleeper projections endpoint (projections, ADP; undocumented)

Base `https://api.sleeper.app` (no `/v1`) — the same host as the public API,
no auth, GET only, read-only. Not in Sleeper's docs; it is what the Sleeper
app reads for its draft board. *Verified* 2026-09-07 with curl.

| Endpoint | Notes |
|---|---|
| `/projections/nfl/{season}?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF&order_by=adp_ppr` | JSON list (3,304 rows for 2026, 3 MB), one row per player: `player_id` (**the Sleeper id**; DEF ids are team abbreviations like `"KC"`), `player: {first_name, last_name, position, team, fantasy_positions, injury_status, …}`, `stats: {pts_ppr, pts_half_ppr, pts_std, adp_ppr, adp_half_ppr, adp_std, adp_2qb, adp_dynasty*, adp_idp*, adp_rookie, gp, rec, rush_yd, …}`, `season`, `week` (null on season rows), `season_type`, `company` (`rotowire`), `updated_at`. `order_by` sorts by the named stat; `position[]` repeats. 631 rows carry `pts_ppr`; every row carries every `adp_*` key, with **`999.0` meaning no ADP** (1,075 rows). Examples: Gibbs `9221` pts_ppr 331.4, adp_ppr 1.7; Kamara `4035` pts_ppr 63.0, adp_ppr 167.6 (ESPN projected him 15.7 ppg — the case that motivated this). DEF rows report `gp` 1 with season totals. `ppg = pts / 17` for every row. 71 `FB` rows are fantasy-eligible only through `fantasy_positions` (as in the dictionary). |

Same host as `/v1`, so the call draws from the Sleeper 500/min budget
(`api.LIMITER_OF` maps the `sleeper_root` host key onto the `sleeper`
limiter). One call per `rankings refresh`; never called from the draft loop.

## 3. Architecture

```
sleeper/
├── pyproject.toml         uv; deps: httpx; dev: pytest; phase 2 adds mcp
├── .env                   FANTASYPROS_API_KEY=…  (git-ignored, created by hand)
├── .gitignore             .env  .venv/  __pycache__/
├── DESIGN.md
├── CLAUDE.md              ships WITH v1 — for Claude it is the tool schema
├── src/sleeper/
│   ├── api.py             get(host, path) -> json; NotFound; cache-buster; FP key header
│   ├── ratelimit.py       per-host caps shared across processes via a state file + lock
│   ├── players.py         slim dictionary (ETag refresh), name(), search(), pool()
│   ├── rankings.py        FantasyPros fetch + cache, join to Sleeper ids, refresh report
│   ├── league.py          league / roster / matchup / transaction joins
│   ├── draft.py           pick-order math, poll step, outlook assembly
│   ├── tools.py           the command set: pure functions kwargs -> dict; load_config()
│   ├── cli.py             argparse over tools; render(); the wait loop; --notify
│   └── mcp_server.py      phase 2
└── tests/
    ├── fixtures/          captured JSON (ids in §8)
    └── test_*.py
```

Layering: `tools` imports `league`, `draft`, `players`, `rankings`; those
import `api`, which imports `ratelimit`. `cli` and `mcp_server` import only
`tools`. Nothing below `tools` prints or reads argv. The one sleep below
`tools` is the limiter's wait (§3.1); everything else that waits lives in the
wrappers.

No `cache.py`, no TTL table, no generic retries: the only sustained load is
the 3 s draft poll (~40 req/min), the CDN already caches league/draft
endpoints, and the one large payload (players) is handled by ETag in
`players.py`. The limiter exists to keep a bug — a tight loop, two parallel
invocations, a wrong poll interval — from turning into an IP block, not to
serve normal load.

### 3.1 `api.py`

```python
def get(host: str, path: str, params: dict | None = None, fresh: bool = False) -> Any
```
- `host` ∈ `sleeper` (`https://api.sleeper.app/v1`) · `fantasypros`
  (`https://api.fantasypros.com/public/v2/json`, adds `x-api-key` from the
  environment; raises `SleeperError("FANTASYPROS_API_KEY not set")` before
  any request).
- Calls `ratelimit.acquire(host)` **before every request** — the only path
  to the network.
- `httpx.get`, 10 s timeout, `User-Agent: sleeper-cli/<ver>`,
  `raise_for_status()`.
- `fresh=True` appends `_=<ms>` to bypass the CDN (used only by the draft poll).
- Raises `NotFound(path)` on 404 **and on a JSON `null` body**.
- On **429**: calls `ratelimit.block(host, seconds)` with `Retry-After` if
  present, else 60, and raises `RateLimited(host, seconds)`. No retry.
- Errors are `SleeperError` subclasses with a one-line message; that message
  is what the CLI prints and what the MCP wrapper returns as the tool error.
- No retries otherwise. The draft poll loop catches `httpx.HTTPError` and
  `RateLimited` and keeps polling after the indicated wait; after two
  consecutive `RateLimited` it returns `reason: rate_limited`.

#### `ratelimit.py`

Caps: `{"sleeper": 500, "fantasypros": 1}` requests per rolling 60 s. State
per host in `~/.cache/sleeper/ratelimit.<host>.json`:
`{"sent": [<epoch seconds>...], "blocked_until": <epoch seconds or 0>}`.

```python
def acquire(host: str) -> None     # blocks until a request is allowed, then records it
def block(host: str, seconds: float) -> None
```

- `acquire` takes an exclusive `fcntl.flock` on `<state>.lock`, reads the
  file, drops entries older than 60 s, and: if `blocked_until` is in the
  future, or `len(sent) >= cap`, it computes the wait (`blocked_until − now`
  or `sent[0] + 60 − now`), releases the lock, sleeps, and retries; otherwise
  appends `now`, writes, releases. Two processes cannot both see a free slot.
- Waits longer than 2 s print one stderr line:
  `rate limit (fantasypros 1/min): waiting 58 s`.
- `block` sets `blocked_until = max(existing, now + seconds)` under the same
  lock, so a 429 seen by one process pauses every process.
- Clock and sleep are injectable (`now=time.time, sleep=time.sleep`) for
  tests. About 40 lines; no third-party dependency.
- Expected use: draft poll ≈ 40/min, `drafts()` ≈ 11, `league()` 3,
  `players --refresh` 1 — Sleeper never waits in normal use. FantasyPros
  makes 2 calls per `rankings refresh`, so that command takes about a minute.

### 3.2 `players.py`

- **Slim file** `~/.cache/sleeper/players.slim.json`: every entry whose
  `position` or any `fantasy_positions` ∈ `{QB,RB,WR,TE,K,DEF}`, **regardless of
  `active`** (4,388 rows, ~1 MB today) so retired/IR players on dynasty rosters
  still resolve to names. Fields: `player_id, name, key, position,
  fantasy_positions, team, status, age, years_exp, injury_status,
  depth_chart_order, search_rank` where
  `name = full_name or f"{first_name} {last_name}"`,
  `key = re.sub(r'[^a-z0-9]', '', name.lower())` computed locally (never
  `search_full_name`), `search_rank` null → `9999999`.
  DEF rows get search aliases: abbr, city, nickname (`kc`, `kansascity`,
  `chiefs`).
- **Refresh:** `refresh()` GETs `/players/nfl` with `If-None-Match: <stored
  etag>`; 304 ⇒ touch mtime. Called by `sleeper players --refresh` and, lazily,
  by non-draft tools when the file is > 24 h old. **Draft tools never
  refresh:** they use the file if < 7 days old (stderr note with its age) and
  error only if it is missing. `draft_status`, `draft_wait`, `draft_picks` do
  not load the dictionary at all — pick metadata carries names. `refresh()`
  returns what it did so a tool can put a `note` in its result (stderr is
  invisible under stdio MCP).
- `name(pid)` → `"Patrick Mahomes"`, `"Kansas City Chiefs"`, or `"#<pid>"`
  (IDP, unknown). Never raises.
- `search(query, position=None, limit=10)`: normalize the query with the same
  `key` rule, substring match on `key` and DEF aliases; an exact `player_id`
  match returns that one row.
- `pool()` = slim rows with `team is not None and status != "Inactive"` —
  817 rows today (784 `Active`, 32 DEF, 1 practice squad). This is the
  draftable universe; the `search_rank` fallback ordering applies to it.
  Current-season IR/PUP players are **in** it (they carry `status: Active`,
  a team, and `injury_status: IR|PUP` — 13 today); the rows with
  `status: "Injured Reserve"` are stale retired records with `team: null`.

### 3.3 `rankings.py`

Best-available needs a draft ranking Sleeper's public API doesn't have.
**Sleeper's own projections endpoint (§2.3) comes first**: keyed by Sleeper
id, it supplies rank (ADP order), ADP and projected ppg with no join, and
those numbers are what the draft tools read — the board matches what Ishan
sees in the app. **ESPN's board is fetched alongside** (rank, ADP, bye and
its own ppg for the top 500) and is the fallback wherever Sleeper has no
number, and the whole board if the Sleeper call breaks. **FantasyPros is an
overlay**: with the free key it returns 10 rows (§2.1), which decorate those
rows with tier, pos_rank and bye; a paid key that returns ≥ `MIN_BOARD` (100)
joined rows makes FantasyPros ECR the `ecr` field (`source:
sleeper+fantasypros`). All of it is fetched once by `sleeper rankings
refresh` and cached on disk. Sleeper's `search_rank` stays as the fallback
when the cache is missing or holds fewer than `MIN_BOARD` rows, labeled
`source: search_rank`.

- **Scoring is derived from the league**, not configured:
  `scoring_settings.rec` 1.0 → `PPR`, 0.5 → `HALF`, else `STD`. ESPN rank
  type: `PPR` and `HALF` → `PPR`, `STD` → `STANDARD`, and a league whose
  `roster_positions` contains `SUPER_FLEX` → `SUPERFLEX` whatever the
  scoring. FantasyPros keeps `type=DRAFT`, `position=ALL`, `week=0`.
- **Fetch** — `sleeper rankings refresh` (CLI-only). First one Sleeper GET
  (`fetch_sleeper(season, scoring, superflex)`: `order_by` and the points
  field follow the scoring — `adp_ppr`/`pts_ppr`, `adp_half_ppr`/
  `pts_half_ppr`, `adp_std`/`pts_std`; a superflex league orders by
  `adp_2qb`), then always two ESPN GETs (`fetch_espn(season, rank_type,
  limit=500)` and `fetch_espn_teams(season)` for abbreviations and byes),
  then, **only when `FANTASYPROS_API_KEY` is set**, the two FantasyPros GETs
  from before (the second waits ~60 s under the 1/min cap; the command says
  so on stderr). Without the key the FantasyPros path is skipped and
  reported as `skipped: FANTASYPROS_API_KEY not set`. Raw responses are kept
  for debugging and fixtures: `rankings.raw.sleeper.<season>.json` (the
  projections list as received, ~3 MB), `rankings.raw.espn.<season>.json`
  (both ESPN docs with each player's `stats` stripped, ~1.4 MB) and, when
  fetched, `rankings.raw.<season>.<scoring>.json` (FantasyPros, as before).
  Draft tools never call any of these: `draft_available` and `draft_outlook`
  read the cache, report its age in `as_of`, and fall back to `search_rank`
  with a `note` if it is missing.
- **Sleeper rows** (`sleeper_rows`): `{sl_pts, sl_ppg (pts / 17, one
  decimal), sl_adp (one decimal; None for the 999 sentinel), sl_rank (1-based
  position in ADP order among rows with an ADP; None without)}` keyed by
  Sleeper id, fantasy positions only (FB rows qualify through
  `fantasy_positions`); rows with neither points nor ADP are dropped. No
  join, no unmatched report.
- **ESPN rows** (`espn_rows`): `{espn_id (str), name, pos, team (Sleeper
  abbr via proTeamId), rank (draftRanksByRankType[rank_type].rank), adp
  (ownership.averageDraftPosition rounded to 1 decimal, None when 0/absent),
  bye, injury}`; entries without a fantasy `defaultPositionId` or without a
  rank of the requested type are dropped.
- **ESPN join to Sleeper `player_id`** (`join_espn`), first hit wins:
  1. `[rankings.espn_overrides]` from config (`"<espn id>" = "<sleeper id>"`)
  2. `espn_id` == Sleeper `espn_id` (Sleeper carries it for only ~200 of the
     817 draftable rows today — 163 of ESPN's top 500 matched by id, 334 by
     name, 0 unmatched in the top 250)
  3. D/ST rows: `proTeamId` → abbrev → `players.team()` → Sleeper DEF id
  4. the same name fallback FantasyPros uses (§3.3 of v4: suffix-stripped
     key, `(name, pos, team)` then `(name, pos)`, prefer a team then lowest
     `search_rank`, unmatched if 0 or > 1 survive).
  A row that resolves to an already-claimed Sleeper id is reported as
  unmatched rather than overwriting.
- **FantasyPros join** is unchanged (override → sportradar → yahoo → espn →
  DST team → name).
- **Merge** (`merge(espn, fp, sleeper)`): base rows are ESPN's — `ecr` =
  ESPN rank, `tier` None, `pos_rank` = `f"{pos}{n}"` by rank within
  position, `bye`, and ESPN's ADP and ppg under explicit names `espn_adp`
  and `espn_ppg`. Every Sleeper id FantasyPros matched overlays `tier`,
  `pos_rank` and `bye` where FantasyPros has them. If FantasyPros matched ≥
  `MIN_BOARD` rows its `ecr` replaces the ESPN rank on those rows and
  FantasyPros-only rows are added; otherwise FantasyPros-only rows are
  ignored. Then every row gains `sl_rank`, `sl_adp`, `sl_ppg` and the
  **biased** fields the tools read: `rank` = `sl_rank` if present else
  `ecr`, `adp` = `sl_adp` else ESPN's (FantasyPros' on a FantasyPros-only
  row), `ppg` = `sl_ppg` else ESPN's. A Sleeper-only player (no ESPN row)
  still gets a row, with `ecr`, `tier`, `pos_rank`, `bye` None. `source` is
  `sleeper+espn` (or `sleeper+fantasypros` with a paid key) when Sleeper
  rows exist, else `espn` / `fantasypros` as before.
- **Refresh report:** `{source, sleeper: {count, with_pts, with_adp}, espn:
  {count, by_id, by_name, unmatched}, fantasypros: {count, by_id, by_name,
  unmatched} | "skipped: …", path}`, where each `unmatched` lists rows
  inside the top 250 of that source with its own id so an override is one
  copy-paste. **Exit 1 only when the primary joined source (`source` minus
  its `sleeper+` prefix) has unmatched rows** — Sleeper rows are Sleeper ids
  and need no join; ten free FantasyPros rows that don't join must not fail
  the command.
- **Cache doc:** `{source, scoring, season, rank_type, fetched_at,
  last_updated_ts (FantasyPros, else null), sleeper_rows, espn_rows,
  fantasypros_rows, rows: {sleeper_id: {rank, ecr, tier, pos_rank, adp, bye,
  ppg, espn_adp, espn_ppg, sl_rank, sl_adp, sl_ppg}}}` at
  `~/.cache/sleeper/rankings.<season>.<scoring>.json`. `load()` is unchanged
  in shape; `rank_of()` returns every row key (None when absent, `rank`
  falling back to `ecr` on a pre-Sleeper cache) plus the doc's `source`.
- **Secret:** the FantasyPros key is read from the `FANTASYPROS_API_KEY`
  environment variable, which `load_config()` fills from `.env` when it
  isn't already set. It never appears in output, error messages, or cache
  files. `raw` stays Sleeper-only.

### 3.4 `draft.py`

Pure functions over (draft object, picks, traded picks, optional league
rosters); all fixture-tested.

- **Unfilled slots:** `unfilled = {1..teams×rounds} − {p.pick_no}`.
  `next_pick_no = min(unfilled)` or **`None` ⇒ complete** (regardless of
  `status`). Never `len(picks)+1` — keepers pre-fill future picks.
- **Slot for pick `p`:** `round = ceil(p/teams)`, `idx = (p−1) % teams`,
  `R = settings.get("reversal_round") or 0`.
  `forward = True` for `linear`; for `snake`,
  `forward = (round % 2 == 1) XOR (R > 0 and round >= R)`.
  `slot = idx+1 if forward else teams−idx`.
  (*verified*: 0 mismatches over 566 picks across snake, linear and two 3RR
  drafts.) Note `reversal_round` is usually present with value `0`; "if set"
  logic would flip every round.
- **Who picks slot `s` in `round`:** original `roster_id =
  slot_to_roster_id[str(s)]`; for **unfilled** picks,
  `owner = traded.get((round, original), original)` using
  `/draft/<id>/traded_picks` filtered to `season == draft.season`. For
  **filled** picks use `pick.roster_id` as-is. Roster → display name via
  `draft_order` inverted through `slot_to_roster_id`, or league users/rosters
  when `league_id` is set; slots absent from `draft_order` render as
  `slot N (CPU)`; `picked_by == ""` resolves by `roster_id`.
- **Me:** `my_slot = draft_order.get(my_user_id)`. If `None` and the draft has
  a league: find the roster whose `owner_id` or `co_owners` contains me, invert
  `slot_to_roster_id`. Else `SleeperError("you are not in draft <id>")`. Never
  infer my slot while `draft_order` is null.
- **My next pick / picks away:** `my_next = min(q ∈ unfilled : picker(q) ==
  me)`; `picks_away = |{q ∈ unfilled : next_pick_no ≤ q < my_next}|` — counts
  only picks that still have to be made, so keeper-filled slots don't inflate
  it. Empty set ⇒ I have no picks left.
- **Clock:** `pick_timer` falsy ⇒ `null ("no timer")`; `status == paused` or
  `metadata.is_autopaused == "true"` ⇒ `"paused"`; `now − last_picked >
  pick_timer + 6 s` ⇒ `"overdue/stale"`; auction ⇒ from
  `metadata.timer_end_at`; else `pick_timer − (now − last_picked)/1000`,
  always labeled *estimate*.
- **Pool:** `players.pool()` − drafted `player_id`s − (if `league_id`: union
  of every league roster's `players`, which is what makes keeper/dynasty
  drafts right) − (`player_type == 1`: keep `years_exp == 0`;
  `player_type == 2`: drop `years_exp == 0`; `None` counts as non-rookie),
  restricted to positions the league can start (`FLEX` → RB/WR/TE,
  `SUPER_FLEX` → +QB, `REC_FLEX` → WR/TE; K/DEF only if slotted). Ranked by the
  rankings adapter.
- **Needs:** my picks by position vs `roster_positions`: unfilled starter
  slots, flex slots, bench remaining (`my remaining picks`). No weighting.
- **Auction drafts:** `type == auction` ⇒ `my_next`, `picks_away`,
  `on_the_clock` are `null` with `note: "auction: no pick order"`; picks show
  `metadata.amount`; `draft_status` adds `budget_left = settings.budget −
  Σ my amounts`; `draft_wait` returns on any new pick (`reason: new_pick`).
  Nothing more.
- **Poll step** (the only piece the MCP wrapper re-implements around):
  ```python
  def draft_poll(draft_id, picks_away: int, since_pick: int) -> dict
      # one fresh GET /picks (+ GET /draft every 5th call for status/pause)
      # ~40 requests/min at a 3 s interval, 8 % of the Sleeper cap
      # -> None to keep polling, or a result with reason my_turn|complete|
      #    no_picks_left|new_pick (auction)|rate_limited; timeout is the caller's
  ```
  `payload` is the outlook when `done`, else the compact status. Picks since
  `since_pick` are returned by cursor so a re-call after a timeout loses
  nothing. The sleep loop lives in the wrapper.

### 3.5 `tools.py` — the command set

Conventions that let the MCP wrapper register these functions unchanged (asserted by a test):

- Plain functions; **every** parameter annotated (`str | None`, `int`,
  `bool`), defaults for everything optional, `-> dict`, docstring ≤ 2
  sentences (it becomes the tool description; flag details go in argparse
  help, not here).
- `None` for `league_id` / `draft_id` / `season` / `week` ⇒ from config /
  current state. `draft_id` accepts a pasted `sleeper.com/draft/nfl/<id>` URL.
- Return JSON-serializable dicts made of **sections of flat rows** so one
  generic renderer handles them. `limit` defaults ≤ 25, max 200.
- Raise `SleeperError`; never print; never sleep.
- `load_config()` lives here (§6). `EXPORTS` lists what both wrappers expose.

**Season tools (8)**

| Tool | Returns |
|---|---|
| `whoami()` | resolved `user_id`/`username`, NFL season/week, config path, slim-file age, rankings cache age, FantasyPros key present (yes/no), requests sent per host in the last minute and any active 429 pause |
| `leagues(season=None)` | `league_id`, name, teams, type (`redraft/keeper/dynasty/type_N`), `ppr`, `superflex`, `te_premium`, draft status |
| `league(league_id=None)` | format line (below), `roster_positions`, non-zero scoring bonuses, teams: `roster_id`, owner, team name, W-L-T, fpts, waiver pos |
| `roster(league_id=None, team=None)` | one team (default mine; `team` = roster_id, username or team-name substring): starters in slot order, bench, IR, taxi — `pid, name, pos, team, injury` |
| `matchups(league_id=None, week=None, detail=False)` | pairs with names and points; `detail` adds starters with points |
| `transactions(league_id=None, week=None, type=None, limit=25)` | adds/drops/trades with names, FAAB, status |
| `players(query, position=None, limit=10)` | slim-dictionary search; an id returns that player's slim row |
| `trending(kind="add", hours=24, limit=25)` | with names |

**Draft tools (5)**

| Tool | Returns |
|---|---|
| `drafts(season=None)` | enumerate via `/user/<id>/drafts`; for each non-complete draft (cap 10) fetch `/draft/<id>`: league name (or "no league"), status, type, teams, rounds, `player_type`, `pick_timer`, my slot, `start_time` |
| `draft_status(draft_id=None)` | format line; status/type/round/`next_pick_no`; on the clock (slot, roster, name); `my_slot`, `my_next_pick`, `picks_away`, clock; last 5 picks. No dictionary load. |
| `draft_picks(draft_id=None, round=None, team=None, last=None)` | picks with names from pick metadata (`--round N` is the board view) |
| `draft_available(draft_id=None, position=None, limit=20)` | pool ranked by ECR: ecr, tier, pos_rank, name, pos, team, bye, adp, age, yrs, injury; `source`, `as_of` |
| `draft_outlook(draft_id=None, limit=15)` | **the single call to make before a pick**, ≤ 50 lines (asserted): format line · `picks_away`, clock, `as_of` · `needs` one line · `mine` one line per position · top `limit` overall · per position top 3 **excluding** players already in the overall list, header carries tier remaining (`RB (tier 3: 4 left)`) · `likely_gone` = `(adp or ecr) < my_next_pick_no` · `picks_since: [{pick_no, name, pos, team, by}]`. No recommendation — inputs for Claude. |
| `draft_wait(draft_id=None, picks_away=1, timeout=100, since_pick=0)` | loops `draft_poll` every 3 s until `done` or `timeout`. **Default 100 s** (inside Claude Code's 120 s Bash default and MCP client limits), **max 590**. A timeout is not an error: `reason: timeout` with the compact status + `picks_since` so Claude can narrate and re-call. |

`draft_id=None` resolution: `--draft`/config ⇒ else configured league's
`/league/<id>.draft_id` ⇒ else the single non-complete draft from `drafts()`,
preferring `drafting` over `pre_draft` ⇒ else error listing candidates.

**Format line** — first row of `league`, `draft_status`, `draft_outlook`,
`draft_wait`, derived from league + draft:
`12-team snake 3RR | PPR 1.0, 6pt passTD, TE+0.5 | QB RB RB WR WR TE FLEX FLEX SF K DEF BN×6 | 60s clock | ROOKIES ONLY`.
Superflex / TE-premium / rookie-only flip values more than any ranking; this
keeps Claude from advising for the wrong format.

`raw <path>` is **CLI-only** (not in `EXPORTS`): any `/v1` path; if the body
exceeds 8 KB it is written to `~/.cache/sleeper/raw/<hash>.json` and the
command prints `{path, bytes, type, len_or_keys, sample}` instead.

## 4. Draft-day workflow and build order

**Days before**

```
sleeper players --refresh
sleeper rankings refresh                    # ESPN board (+ FP overlay) -> cache; fix unmatched via overrides
sleeper drafts                              # or: sleeper draft status --draft <url>
sleeper draft available --limit 30          # board sanity check
```

Morning of the draft: `sleeper rankings refresh` again — ECR moves daily in
the final week, and the draft tools only read the cache.

Run the whole loop once on a Sleeper **mock draft**, reached by pasting its
URL (`--draft`), since mocks may not be listed under my drafts.

**During** — two supported invocations, documented verbatim in CLAUDE.md:

```
sleeper draft wait --picks-away 2 --notify --timeout 590   # Bash timeout: 600000
sleeper draft wait --picks-away 2 --notify                 # run_in_background: true
```

Each return is either my turn (full outlook) or a timeout (compact status +
picks since last call). Claude reads it, we talk, I pick in the app, repeat.
`draft picks --last 8` for "what just happened", `draft status` for a cheap
check.

**Build order** (draft-first):

1. `ratelimit.py` (with its tests), `api.py`, `players.py` (slim + ETag
   refresh, `name`, `search`, `pool`)
2. `draft.py` pick-order math against fixtures (§8) — the code that cannot be
   validated live during the real draft
3. `draft_status`, `draft_picks`, `draft_available` with the `search_rank`
   fallback
4. `draft_poll` / `draft_wait`, `draft_outlook`
5. `cli.py` with `render()`, `--notify`; **CLAUDE.md** generated from
   `EXPORTS` docstrings + the loop above
6. `rankings.py` + `rankings refresh`
7. Season tools (`league.py`)
8. Phase 2: `mcp_server.py`

## 5. CLI conventions

- `sleeper <group> <cmd> [args] [--json] [--league ID] [--draft ID|URL]`. Groups: `league`, `draft`, `players`, `rankings`; top
  level `whoami`, `raw`.
- One `render(d)` (~30 lines) in `cli.py`: list of dicts → aligned columns,
  scalars → `key: value`, nested dict → header + recurse. `--json` prints the
  dict. No other output flags.
- `--notify` (CLI-only, `draft wait`): macOS notification via `osascript`
  when the wait returns.
- Exit codes: 0 ok, 1 `SleeperError` (message on stderr), 2 usage.

## 6. Config

Two files. `~/.config/sleeper/config.toml`, read with stdlib `tomllib` by
`tools.load_config()`; flags > file; no env layer for these keys:

```toml
username = "ishan"        # or user_id = "..." to skip the lookup call
league_id = "1234567890"  # default league; optional
# draft_id = "..."        # optional pin for draft day

[rankings.overrides]      # FantasyPros player_id -> Sleeper player_id
"6880" = "4046"

[rankings.espn_overrides] # ESPN player id -> Sleeper player_id
"4429795" = "9221"
```

And `.env` at the repository root, **git-ignored, created by hand**, holding
the one secret:

```
FANTASYPROS_API_KEY=<key>
```

`load_config()` locates `.env` relative to the package
(`src/sleeper/../../.env`) so it works from any working directory, including
under the MCP server; it parses `KEY=value` lines and `#` comments (about ten
lines, no `python-dotenv`) and sets only variables that aren't already in the
environment. `username` is resolved with one `/user/<username>` call per
invocation (CDN-cached, ~50 ms); set `user_id` to skip it.

## 7. MCP layer (phase 2)

`mcp_server.py` registers each `EXPORTS` function through a `functools.wraps`
wrapper that converts `SleeperError` into the SDK's tool error (v2 of the
`mcp` package scrubs other exceptions to a generic message), and reimplements
`draft_wait` as `async def` around `draft_poll` with `await anyio.sleep(3)` and
progress reports — a sync 590 s tool runs in a worker thread the client cannot
cancel.

The `mcp` package is at 2.x (*verified*) and renamed `FastMCP`; pin a major
version at phase 2 and write to that API. The contract that makes this a
~40-line file is the `tools.py` convention test (annotations, `-> dict`,
docstring length), not the SDK.

Not exported: `raw`, `rankings refresh`, `players --refresh`. Registered as a
stdio server (`uv run sleeper-mcp`). Same timeout rule as the CLI.

## 8. Testing

Fixtures captured with curl (public data):

| Fixture | Covers |
|---|---|
| draft `1388280410676432896` + picks | 16-team snake, `reversal_round=3`, complete, 240 picks |
| draft `1354612949850800128` + picks + traded_picks, league `1354612949834035200` rosters | linear, `drafting`, 3 traded picks, `player_type=1` (rookie pool + roster subtraction) |
| draft `1395201566562082816` | `pre_draft`, `draft_order` null, `slot_to_roster_id` set |
| draft `1389736356023918593` + picks | `pre_draft` with 14 keeper picks pre-filled |
| draft `1385745991830900736` + picks | 8-team 3RR with CPU slots and `picked_by == ""` |
| league `289646328504385536` (docs example) + users/rosters/matchups/transactions | season-tool fields |
| recorded `null` body | `NotFound` on 200-null |
| FantasyPros `consensus-rankings` + `players` responses, captured once with the key (responses contain no secret) | join coverage: ≥ 95 % of the top 200 ECR resolve by id, DST rows resolve to Sleeper DEF ids, the unmatched list is stable |
| ESPN `kona_player_info` top 60 by PPR rank plus 3 D/ST rows, `stats` stripped (`espn_players_2026.json`, 200 KB) and `proTeamSchedules_wl` trimmed to `settings.proTeams[].{id, abbrev, byeWeek}` | position/team/bye mapping, D/ST join, rank-type selection (SUPERFLEX reorders), ADP rounding; the ESPN-id join, name fallback, overlay/takeover merge, report shape and the no-key refresh run against the synthetic slim table with ESPN monkeypatched |
| Sleeper `projections/nfl/2026` top 60 by `adp_ppr` plus LAR/HOU DEF rows and Kamara `4035`, `stats` trimmed to `pts_*`/`adp_*` and `player` to the fields read (`sleeper_projections_2026.json`, 48 KB) | `sleeper_rows` (ppg rounding, 999 sentinel, DEF ids, ADP-order rank, HALF/STD/2QB fields), Sleeper-over-ESPN merge precedence, and the refresh order (Sleeper first) with `api.get` monkeypatched |

Tests: `ratelimit.py` with an injected clock — the 501st Sleeper call in a
minute waits until the oldest entry expires; the second FantasyPros call
waits ~60 s; `block()` written by one limiter instance is honored by a second
instance on the same state file; entries older than 60 s are dropped. Then:
slot formula across all draft fixtures (every filled pick's
`draft_slot` must equal the formula), `next_pick_no`/`picks_away` with keepers
and at the last pick, my-slot fallback via co-owners, traded-pick ownership
for unfilled picks, auction handling, clock states, pool subtraction, rookie
filter, `name()` for DEF/IDP/unknown, the rankings join order (override →
sportradar → yahoo → espn → DST → name) and the name fallback on the hard
list (*Kenneth Walker III, Patrick Mahomes II, Marvin Harrison Jr., Amon-Ra
St. Brown, Ja'Marr Chase, DJ Moore / D.J. Moore*), `tools.py` conventions and
the 50-line outlook cap. Live smoke on a mock draft before the real one.

## 9. Non-goals (v1)

Writes · IDP · auction strategy beyond the null-with-note handling · FantasyPros
projections (one call away; Sleeper's and ESPN's are in, §2.3) · CSV rankings
import · private GraphQL / ADP scraping · websockets · UI · env-var config
beyond the one secret · disk cache beyond the players and rankings files.

## 10. Verify before build (facts the design assumes)

1. Create a mock draft: does it appear in `/user/<me>/drafts/nfl/2026`? Does
   its `draft_order` contain my user_id? If not, `--draft <url>` is the only
   way in and identity must come from `draft_order` alone.
2. On that mock: cache-buster still gives `cf-cache-status: MISS` on `/picks`,
   and a pick made in the app shows within one 3 s poll. If Cloudflare ignores
   the query string, accept the 15 s lag and say so in `as_of`.
3. `pre_draft → drafting`: status flips and `draft_order` is populated before
   pick 1 lands; my-slot resolution works before pick 1.
4. Run `draft wait --timeout 590` under Bash `timeout: 600000` and once with
   `run_in_background: true`; the result reaches Claude intact on both
   `my_turn` and `timeout`.
5. My league(s): type, `reversal_round`, keeper/dynasty (keeper picks already
   in `/picks` pre-draft, `roster_id` = keeper's team?), superflex /
   TE-premium, `pick_timer`, `cpu_autopick`.
6. FantasyPros, one call each with the key — run these yourself so the key
   stays out of this repo and this chat:
   ```
   curl -s "https://api.fantasypros.com/public/v2/json/nfl/2026/consensus-rankings?position=ALL&type=DRAFT&scoring=PPR&week=0" -H "x-api-key: $FANTASYPROS_API_KEY" -D - | head -40
   ```
   Confirm `count` (expect ≳ 300), that `tier`, `player_bye_week`,
   `sportsdata_id` and `player_yahoo_id` are present and populated, the type
   of `rank_ecr`, and any `x-ratelimit-*` response headers. Then
   `/nfl/players?ecr=included&external_ids=yahoo:espn` to confirm
   `rank_adp_ppr` and the external-id field names.
7. Cross-check five players: FP `sportsdata_id` vs Sleeper `sportradar_id`,
   FP `player_yahoo_id` vs Sleeper `yahoo_id`. If the uuids don't line up the
   join falls through to yahoo/espn ids, which still beats names.
8. Rookies have `years_exp == 0` in the current dictionary (136 in the pool
   today) — Sleeper increments it at some point in the offseason.
9. Phase 2: installed `mcp` version, and that a `SleeperError` inside a tool
   reaches Claude as text.

## 11. Decisions log

- 2026-09-05 — **League: snake, keeper, PPR, 120 s pick timer. Draft:
  Tuesday 2026-09-08.** Keeper picks arrive pre-filled in `/picks` (the
  unfilled-set math and fixture `1389736356023918593` cover it); the pool
  subtracts league rosters so kept players never show as available; the
  120 s clock is well above the 3 s poll and the 100 s wait default. Not
  superflex, not auction.
- 2026-09-05 — **Rankings come from the FantasyPros public API v2**, key in
  a git-ignored `.env`. The CSV adapter from v2 is dropped.
- 2026-09-05 — **Per-host request caps: Sleeper 500/min, FantasyPros
  1/min**, enforced by a limiter shared across processes through a state
  file; any 429 pauses that host for 60 s everywhere. Requested as a hard
  guarantee against an IP block; reverses the v2 review's removal of the
  token bucket, which had assumed only normal load.

- 2026-09-05 — **ESPN's public fantasy API is the draft board; FantasyPros
  is an overlay.** The free FantasyPros key returns 10 rows per call
  (`tier: "free"`, `limit: 10`, `public_api_limited: true`), which cannot
  seed a board. ESPN needs no key, accepts the CLI's User-Agent, and returns
  rank, ADP and (via the team list) bye weeks for 500 players in two calls.
  FantasyPros becomes the ECR source again automatically when a key returns
  ≥ 100 joined rows. Cap: ESPN 5/min.

No open questions.

## 12. Implementation notes (2026-09-05)

Where the code differs from the sections above, the code is right:

- `is_keeper` is null on real keeper picks. A filled pick beyond
  `next_pick_no` is a keeper, but once the draft passes its slot it looks like
  a live pick, so `draft.remember_keepers()` persists the set per draft in
  `~/.cache/sleeper/keepers.<draft_id>.json` and every command merges into it;
  `picks_since`, `last_picks`, `picks_made` and the `keeper` column use it.
  Run `draft status` before the draft so the pre-draft state is captured.
- `Poller.step()` returns `None` to keep polling or a result whose `reason`
  is `my_turn | complete | no_picks_left | new_pick (auction) |
  rate_limited`; `timeout` is added by the caller. Pauses don't end the wait;
  they show in `clock`.
- `tools.draft_wait` contains the 3 s sleep loop (the one sleep below the
  wrappers besides the rate limiter). The phase-2 MCP server writes its own
  async loop around `draft.Poller`.
- 14 tools in `EXPORTS` (`drafts` counted separately from the five `draft_*`).
- `since_pick` returned by `draft wait`/`draft outlook` is the `next_pick` at
  that moment; pass it as `--since` on the next call.
- `raw` always prints JSON (a table of raw objects is unreadable).
- The `league` team table omits `owner_id`.
- Times (`drafts.start`) are rendered in the machine's local timezone.
- Config also accepts `user_id` to skip the username lookup; `draft_id` pins
  a draft.
- Rankings (v5): the board comes from ESPN and FantasyPros overlays it
  (§3.3); Sleeper's dictionary carries `espn_id` for few current players, so
  most ESPN rows join by name (163 by id / 334 by name / 0 unmatched in the
  top 250 on 2026-09-05); `MIN_BOARD` lives in `rankings.py` and `tools`
  re-exports it; `api.get` takes a `headers` argument for the ESPN filter;
  `[rankings.espn_overrides]` is a second override table keyed by ESPN id;
  the cache doc gains `rank_type`, `espn_rows` and `fantasypros_rows`;
  `rankings refresh` exits 1 only for the primary source's unmatched rows.
- Rankings: HALF scoring uses FantasyPros `rank_adp_ppr`; a ranked row that
  resolves to an already-claimed Sleeper id is reported as unmatched rather
  than overwriting; id indexes prefer rows with a team and a lower
  `search_rank` when Sleeper holds duplicate external ids.
- League: `format_line` shows `no clock` for a zero timer and `VETS ONLY` for
  `player_type == 2`; drops read `Name (POS) from Team`.
- Review fixes (2026-09-05, adversarial review): `likely_gone` uses my pick
  *after* the current one when I am on the clock; `draft_wait` returns before
  its deadline when a 429 pause would overrun it (`reason: rate_limited`) and
  never sleeps past the deadline; `picks_since` inserts a `... N earlier picks
  not shown` row instead of dropping silently (wait keeps 40, outlook 6);
  `api.get` turns non-JSON bodies and 3xx into `TransientError`;
  `Retry-After` is clamped to 300 s and non-finite values fall back to 60 s,
  and a poisoned limiter state file self-heals; `draft outlook` defaults to
  12 top rows and `mine` is one line so it renders under 50 lines (tested);
  `on_the_clock` is a string; `matchups` returns flat rows plus a separate
  `starters` table with `--detail`; JSON output keeps emoji; `whoami` lists
  rankings cache ages; the Poller keeps polling while the draft order is
  unset and refetches the draft object whenever the pick count changes.
- Mock rehearsal (2026-09-05, league mock 1401926506271223808): the loop
  worked end to end, and the rehearsal showed the design's real flaw — Claude
  in the relay path costs 30-90 s per turn on a 120 s clock. Added
  `draft watch`, a self-refreshing terminal board with a bell/notification, so
  the ranked list reaches Ishan with no relay; `draft wait` is now only how
  Claude learns when to offer judgment. Fixed along the way: mock picks carry
  no `roster_id` (labels and own-pick detection fall back to `draft_slot`),
  a re-armed wait must not re-report the turn it already returned (the
  cursor is my own pick number), `likely_gone` names its horizon pick.
- 2026-09-06 — Ishan reframed the draft-day role: he sees the board in the
  app and wants **judgment on demand**, not delivery. Added
  `~/.config/sleeper/plan.toml` (notes, targets, avoid), the `plan` and
  `draft plan` tools (expected availability per remaining pick by ADP, with
  targets), and `targets` / `fallers` / `avoid_on_board` lines in the
  outlook. CLAUDE.md now describes the on-demand model; `draft watch` and
  `draft wait` are opt-in. 16 tools in EXPORTS.
- 2026-09-07 — mock 1402549195373568000 (7th): Ishan said the advice leaned
  on the plan and asked for ADP fallers to be highlighted and weighed. The
  outlook now marks QB/RB/WR (TE while open) whose ADP is at least
  `max(FALL_MIN, pick // FALL_DIVISOR)` picks before the current pick with
  `fall`, lists them biggest-fall-first in `fallers` with position, ADP and
  the fall, shows `fall` in `top` and `fell N` in `by_position`, and the late
  table reserves two rows for them like plan targets. Flagged projections
  are excluded. CLAUDE.md: value before plan.
- 2026-09-08 — after the real draft: the late `top` drops TE as soon as the
  TE slot is filled and no flex is open (was: only with ≤ 2 bench spots), so
  the last rounds show the remaining RB/WR options instead of six tight ends.
- 2026-09-08 — injury watching. Sleeper has **no** injuries endpoint
  (`/v1/players/nfl/injuries`, `/v1/injuries/nfl` and the seasonal variants all
  404) and no event feed, so "coming off IR" can only be a snapshot diff. The
  cheap source is an undocumented per-player view,
  `GET /v1/players/nfl/<player_id>` — about 1 KB, `s-maxage=600`, carrying
  `injury_status`, `injury_body_part`, `injury_notes`, `injury_start_date`,
  `practice_participation`, `status` and `depth_chart_order`. `injuries.py`
  watches my whole roster (IR slot included) plus the best unowned sidelined
  players (`search_rank <= FREE_AGENT_RANK`), capped at `WATCH_LIMIT`, one call
  each against the Sleeper budget; it stores `injuries.<league_id>.json` in the
  cache and classifies each change on a severity scale (clear < questionable <
  doubtful < sidelined) into activated / upgraded / downgraded / sidelined,
  plus first practice participation and team changes. A player seen for the
  first time is never a change, so the first run only writes a baseline.
  Delivery is a scheduled Claude task rather than a daemon: a standalone
  process cannot reach Ishan's phone, since the push channel is a Claude tool.
- 2026-09-06 — ESPN's `kona_player_info` rows carry season projections in
  `stats` (statSourceId 1, scoringPeriodId 0: `appliedTotal`, `appliedAverage`);
  `strip_stats` lifts them to `proj_pts`/`proj_ppg` before discarding the block,
  and cache rows, `draft available`, the outlook and `draft plan` show `ppg`.
- 2026-09-06 — mock 1402376028013244416 findings: the outlook's `top` sorts by
  projected ppg from `PPG_SORT_FROM_PICK` (90) and drops QB/K/DEF once their
  starter is filled; `best_ppg` and `check_news` (ECR ≤ 120 with ppg < 6,
  flagged `low-proj`, kept out of `top`, `?` in `by_position`) are new lines;
  `_avail_rows` carries `dc` (depth-chart slot, shown as `WR2` for
  RB/WR/TE below the starter); `targets` is windowed to ADP ≤ next pick + 40
  and capped (8 / 6); `mine` shows 3 names per position then `+N`. The
  outlook's `status` block drops `round` and `my_slot` (both already in
  `on_the_clock`) so the full-extras render still fits 50 lines (tested).
- 2026-09-07 — Mock 3: the ppg-sorted `top` showed no kickers at the last
  pick because every bench WR/RB outscores a K on raw ppg; `_top` now reserves
  `RESERVED_PER_OPEN` (2) rows per still-open starter slot. `draft wait`
  defaults to `--picks-away 0`: Ishan only wants the pick when he is on the
  clock — earlier advice went stale and was ignored.
- 2026-09-07 — Mock 4: `stale-proj` flag after ESPN's cached 15.7 ppg for
  Kamara (Sleeper: 3.3; ESPN itself ranked him RB49). Rule (`_stale_ids`):
  within a position, a ppg-order place at least `STALE_PROJ_GAP` (25) better
  than the consensus positional rank, with ppg ≥ 10, marks the projection
  stale; a depth-chart rule was tried first and rejected because Sleeper's
  preseason charts flag real players (Travis Hunter dc 4). Flagged rows leave
  `top` and `best_ppg` and appear in `check_news`. `avoid_on_board` now checks the
  whole available pool within the ADP window, not just the `top` slice
  (Concepcion was picked with no warning).
- 2026-09-07 — `draft_wait` polls every second throughout (Ishan's call):
  ≈ 60 picks requests + ~12 draft-object requests a minute, under the
  500/min cap. Each turn still produces one result; Claude's
  context sees only that.
- 2026-09-07 — **Sleeper's projections endpoint is the primary board; ESPN
  stays alongside.** Reverses the §9 non-goal against undocumented Sleeper
  endpoints. Ishan asked for both Sleeper's and ESPN's numbers, biased toward
  Sleeper's: the numbers then match the app he is looking at, and the rows
  are keyed by Sleeper id so nothing needs a join (`rankings refresh` exit
  code still only reflects ESPN's unmatched rows). `sleeper_root` is a second
  host key on the same limiter. Cache rows carry `rank`/`adp`/`ppg` biased
  toward Sleeper plus `espn_adp`/`espn_ppg`/`sl_*` explicitly; ESPN remains
  the fallback per field and the whole board if the endpoint breaks (an empty
  Sleeper list yields `source: espn`, and a pre-Sleeper cache still renders).
  Tools: `_avail_rows` sorts by `rank`, shows `rk`/`ecr`/`adp`/`ppg`/`e_ppg`,
  and flags `diverge` when Sleeper's and ESPN's ppg differ by
  `STALE_DIVERGENCE` (5.0) — Kamara's `sl 3.7 / espn 15.7` — with the
  rank-gap `stale-proj` rule now applied only to rows without a Sleeper
  projection; `diverge` outranks `low-proj` because it carries both numbers
  into `check_news`. `top` drops the (mostly empty) `tier` column for
  `e_ppg`. Fixture: `tests/fixtures/sleeper_projections_2026.json` (top 60
  by adp_ppr, two DEF rows, Kamara; stats trimmed to `pts_*`/`adp_*`, player
  block to the fields read).
- 2026-09-07 — Mock 6: `byes` line and `!` bye marker (Ishan's top three WRs
  shared a bye with nothing flagging it); plan targets within 15 picks of
  their ADP get a reserved row in `top`; TE rows leave the late `top` once TE
  is filled and the bench is nearly full; `my_next_round` dropped from the
  outlook status to hold the 50-line budget.
