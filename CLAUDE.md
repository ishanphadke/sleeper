# sleeper — how Claude uses this CLI

Read-only tooling over the Sleeper public API (leagues, drafts, players, and its
own projections/ADP list), ESPN's fantasy API (draft board alongside) and
FantasyPros (optional overlay). Run every command as `uv run sleeper ...` from this
directory. Output is an aligned text table; add `--json` only when you need to
compute over the result. Errors print `error: ...` on stderr with exit 1.

## Never

- Never fetch `https://api.sleeper.app/v1/players/nfl` yourself — it is 15 MB.
  Use `uv run sleeper players <name>`.
- Never call ESPN or FantasyPros directly. `uv run sleeper rankings refresh` is
  the only caller: one Sleeper projections call, two ESPN calls (cap 5/min),
  plus two FantasyPros calls only when a key is configured (cap 1/min, so
  about a minute more).
- Never print, read, or ask for the contents of `.env`.
- The tool cannot make picks, set lineups, or claim waivers. Ishan does that in
  the Sleeper app.

## Commands

| Command | What it returns |
|---|---|
| `whoami` | who the CLI acts as, NFL week, cache ages, requests sent per host in the last minute |
| `leagues [--season N]` | my leagues with format and scoring |
| `league [--league ID]` | format line, scoring, team table with records |
| `roster [--league ID] [--team X]` | one roster with names (default mine; X = roster id, username or team-name substring) |
| `matchups [--week N] [--detail]` | matchups with points; `--detail` adds starters |
| `transactions [--week N] [--type waiver\|free_agent\|trade] [--limit N]` | moves with names, newest first |
| `players <query> [--pos QB] [--limit N]` / `players --refresh` | dictionary search / re-download |
| `trending [--kind add\|drop] [--hours N] [--limit N]` | most added/dropped across Sleeper |
| `injuries [--league ID] [--limit N] [--no-save]` | injury and IR changes since the last check: my roster plus the best unowned sidelined players, one `/v1/players/nfl/<id>` call each; each run saves a snapshot, `--no-save` reports without moving it |
| `drafts [--season N]` | my drafts with status, format and my slot |
| `draft status [--draft ID\|URL]` | on the clock, my next pick, clock estimate, last 5 picks (cheap, no dictionary) |
| `draft picks [--round N] [--team X] [--last N]` | picks made so far, names included |
| `draft available [--pos RB] [--limit N]` | best available by `rk` (Sleeper ADP order; ESPN `ecr` alongside, FantasyPros overlay) with `adp`/`ppg` (Sleeper's), `e_ppg` (ESPN's), `dc` (depth-chart slot) and `flag` (`low-proj`, `diverge`, `stale-proj`) columns |
| `draft outlook [--limit N]` | one call to decide a pick: needs, top available (default 12; by projected ppg from pick 90), `best_ppg` per position, per-position tiers, likely gone before my following pick, targets, `check_news`, picks since |
| `draft plan [--limit N]` | for each of my remaining picks: who is likely still there by ADP, and my plan's targets among them |
| `plan` | the draft plan from `~/.config/sleeper/plan.toml`: notes, targets, avoid, unresolved names |
| `draft wait [--picks-away N] [--timeout S] [--since P] [--notify]` | poll until I am on the clock (N = 0, default) or within N picks, the draft ends, or S seconds (default 100, max 590) |
| `draft watch [--picks-away N] [--limit N] [--notify] [--once]` | **Ishan's own terminal view**: the outlook re-rendered every 3 s with a bell/notification when his pick is within N (default 4); `--once` renders it once for Claude |
| `rankings refresh [--scoring PPR\|HALF\|STD]` | fetch Sleeper's projections/ADP (one call), the ESPN draft board (rank, ADP, bye, ppg; superflex rank type when the league has SUPER_FLEX) and the FantasyPros overlay when a key is set; reports `source`, the Sleeper row counts and each joined source's unmatched players; exit 1 only when ESPN (the joined primary) has unmatched rows |
| `raw <path>` | any Sleeper `/v1` path; bodies over 8 KB go to a file and only a summary prints |

`--draft` accepts an id or a pasted `sleeper.com/draft/nfl/<id>` URL. Without
it the configured league's draft is used.

## Draft day

Ishan can see the board in the Sleeper app. What he wants from Claude is
**judgment on demand**: he has a plan and asks when something changes — a
target fell, a run started, a trade came in. Speed of the answer matters more
than completeness.

Before: `uv run sleeper players --refresh`, `uv run sleeper rankings refresh`
(the board must say `source: sleeper+espn` (or `espn`, `fantasypros`), never
`search_rank`; fix
unmatched rows via `[rankings.espn_overrides]` (ESPN id → Sleeper id) or
`[rankings.overrides]` (FantasyPros id → Sleeper id) in
`~/.config/sleeper/config.toml`), `uv run sleeper plan` (read his notes once
and keep them in mind), then `uv run sleeper draft plan` to review the plan
against expected availability with him.

During, when he asks a question:

1. Run `uv run sleeper draft outlook` (one call, ~1 s). It carries his
   `targets` (available / gone), `fallers` (QB/RB/WR still available well
   past their ADP, with the fall in picks),
   `avoid_on_board`, needs, tiers and the last picks. For a question about a
   later round, run `uv run sleeper draft plan` as well.
2. Answer in two or three lines: the pick, one alternative, and the one fact
   that decides it (e.g. "4 RBs went in the last 6 picks; London is your
   last WR target likely to reach 30"). No data-source caveats, no summaries.
   **Value before plan:** when `fallers` names a QB, RB or WR whose rank
   beats the plan pick's, lead with him and say the fall ("Maye fell 14"),
   even if the answer is still the plan pick. The plan settles ties between
   players of similar rank; it does not outrank a better player who fell,
   and reaching 10+ ranks past the best available QB/RB/WR for a plan
   position is a reach to be named, not a default.
3. Only if he asks for news on a player: search the web, then answer.

**Ishan drafts from his phone via Remote Control; the laptop runs this session
unattended.** Chat text written in an alert-triggered turn does not reach him
promptly, and macOS notifications (`--notify`) fire on the wrong device.
Pushes are the channel; chat is the record.

When he asks to be alerted, run the loop below and re-arm it immediately and
silently after each return, with `--since` from the result. It fires only when
Ishan is **on the clock** (`--picks-away 0`, the default). It polls Sleeper
every second. Advice given while
picks are still to come goes stale as those picks land, and he will not use it.
Post nothing between alerts — no "armed", no notes, no summaries. When it
returns `my_turn`, **push the pick with the `PushNotification` tool** (under 200
characters: the pick, one alternative, the deciding fact) and post it in chat,
from that result, at once (do not re-run the outlook; the 120 s clock is
running); on `timeout` re-arm and say nothing. If he asks "pick?" before the
loop fires, run `draft outlook` and answer — the API can lag his screen.

```
uv run sleeper draft wait --timeout 590 --since <since_pick>
```

Never edit code, run tests, or diagnose bugs during a draft — note it and
fix it afterwards. `draft watch` is his own terminal view; use it only if he
asks for it.

Plan file format (`~/.config/sleeper/plan.toml`):

```toml
notes = """
RB-RB unless a top-3 WR falls to 6. QB from round 8. K/DEF in the last two rounds.
"""
targets = ["Drake London", "Kyren Williams", "Trey McBride"]
avoid = ["Christian McCaffrey"]
```

## Reading results

- `source:` says where the ranks come from: `sleeper+espn` (Sleeper's own
  projections and ADP supply `rk`, `adp` and `ppg`; ESPN's board sits
  alongside as `ecr` and `e_ppg` and fills any gap; `tier` is only filled for
  the few rows the free FantasyPros key returns), `espn` (ESPN alone — the
  Sleeper call returned nothing, or an older cache), `fantasypros` (a paid key
  supplied ≥ 100 rows, so ECR and tiers are FantasyPros consensus), or
  `search_rank` (no usable rankings cache — ranks are Sleeper search
  popularity, not expert rankings; say so when advising).
- `format:` is the first line of league and draft results. Check it before
  advising: PPR vs standard, superflex, TE premium and rookie-only drafts change
  player values more than any ranking.
- `clock: ~85 s (est)` is computed from the last pick's timestamp. `paused`,
  `no timer`, `overdue/stale`, `pre_draft` are labels, not seconds.
- `#1234` is a player id the dictionary doesn't cover (IDP or long retired).
- `since_pick` in a wait or outlook result is the cursor for the next `--since`.
  If `picks_since` starts with a `... N earlier picks not shown` row, run the
  `draft picks --last N` it names before summarizing.
- `rk`, `adp` and `ppg` are Sleeper's: rank in Sleeper ADP order, Sleeper ADP,
  and Sleeper's projected fantasy points per game (season points / 17, in the
  league's scoring). `ecr` and `e_ppg` are ESPN's rank and projection for the
  same player; where Sleeper has no number the Sleeper columns fall back to
  ESPN's. `by_position` entries read `Name (rk 12, 14.2 ppg / e 13.1)`. Ishan's
  rule: prefer the RB unless the WR projects 5+ ppg higher.
- From pick 90 `top` is sorted by ppg but always keeps two rows for each
  still-open starter slot (K and DEF never win on raw ppg), so the last picks
  show kicker and defense options with their rank and ADP.
- `top` is `rk` order until pick 90; from then on it is sorted by projected ppg
  and keeps only RB/WR/TE plus any of QB/K/DEF whose starter slot is still
  open (`needs`); TE drops out too once the TE slot is filled and no flex is
  open (or the bench is nearly full). `best_ppg` always names the
  best-projected available player at each position, whatever the order of
  `top`.
- `check_news:` lists mid-round players (`rk` ≤ 120) projecting under 6 ppg —
  usually a suspension or injury the rankings have not absorbed. They are kept
  out of `top` and carry `?` after the ppg in `by_position`
  (`Quinshon Judkins (rk 40, 3.1 ppg ?)`); search the news before recommending
  one. It also lists players whose two projections *disagree* by 5+ ppg, shown
  as `Alvin Kamara (sl 3.7 / espn 15.7)` (`flag: diverge` — one source has not
  absorbed news), and, for players Sleeper does not project, *stale* ESPN
  projections: a ppg that would rank him a starter while his consensus
  positional rank says bench. All three kinds carry `?` in the by-position
  rows and are kept out of `top` and `best_ppg`. Treat a `dc` of 3 or worse as
  a warning on its own.
- `WR2`/`RB3`/`TE2` after a `by_position` entry (and the `dc` column) is the
  player's slot on Sleeper's depth chart; no marker means first on the chart.
  Sleeper's preseason depth charts can be stale — treat it as a prompt, not a fact.
- `targets: available:` is the plan's targets whose ADP (or ECR) is within 40
  picks of the current pick, in ADP order, at most 8 then `+N more`; `gone`
  shows 6 then `+N more`. A target further out is on `draft plan` instead.
- `fallers:` is the value line: QB, RB and WR (TE while the TE slot is open)
  still on the board whose ADP is well before this pick — at least 6 picks,
  or an eighth of the pick number late in the draft — biggest fall first,
  as `Drake Maye (QB, adp 47, fell 14)`. Flagged projections never count.
  The same players carry a `fall` column in `top` and `fell N` in
  `by_position`, and from pick 90 the table keeps two rows for the biggest
  fallers even when they lose on ppg.
- `likely_gone_before_pick_N` is ADP-based: players whose ADP (or ECR) is
  before pick N, Ishan's next turn after the one being decided. It is a
  probability, not a fact — a player can last past it.
- Keepers are pre-filled picks. `keepers:` counts them; they never appear in
  `picks_since` or `last_picks`, and carry `keeper: yes` in `draft picks`.
- `note:` carries anything the command wants you to know (stale dictionary, no
  rankings cache). Pass it on.
- `injuries` reports one row per change since the previous run, mine first:
  `activated` (off IR/PUP/Out and playable — the waiver signal), `practice`
  (first practice participation logged), `upgraded`/`downgraded` (designation
  eased or worsened without crossing the line), `sidelined` (newly out) and
  `team`. A player seen for the first time is never a change, so the first run
  only writes the baseline. Sleeper has no injuries endpoint and no event
  feed: this is a snapshot diff, and the per-player view is CDN-cached about
  ten minutes, so checking more often than that adds nothing.
