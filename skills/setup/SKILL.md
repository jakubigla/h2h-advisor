---
name: setup
description: Use when setting up or repairing h2h-advisor - log in to Yahoo Fantasy and Basketball Monster, find the leagues, and write or edit the h2h-advisor config (never-drop players, league builds with punt/strong categories, stream slots, the Basketball Monster league id). Use for a first run, a "needs login" or "set bbm_league_id" notice, h2h-advisor tools that are missing or fail with a config error, a newly joined league, or any change to a league's season-long settings, even when the user never says "setup". Short-lived guidance on a player ("keep him while the starter is out") is a note for streaming-advisor, not config.
---

# h2h-advisor setup

Gets h2h-advisor working and keeps its config right. It is re-runnable: each run does what is
missing or what the user came to change, and leaves everything else alone.

Two things never bend:

- **The user logs in, by hand.** No MCP tool can log in and none should try: Google refuses
  sign-in in browsers that automation drives, so login happens in a plain Chrome window the user
  operates.
- **Show the whole config file and get a yes before writing it.** The file holds the user's
  season-long choices; an unseen edit can silently undo one.

## What things cost

Uncached tool calls load real pages, 4-9 s each, against daily caps (Yahoo 250, Basketball
Monster 80), and every result lands in context. Spend both on purpose:

- `status()` loads nothing. `list_leagues` is 1 Yahoo page; `get_league_settings` 1 Yahoo page
  per league (cached 7 days).
- `get_free_agents(league, limit=1)` settles one league's Basketball Monster (BBM) match; a few
  pages, cached an hour, and the check-up brief reuses them. `limit=1`: only its notice matters.
- Do not call `get_projections(scope="ros")` here: it returns ~240K characters of projections.
  The "set bbm_league_id" notice already lists every BBM league with its option value.
- `daily_brief(leagues="all")` loads several pages per league and can take minutes.
- Call each tool once per league; a repeat returns the cached answer and costs only tokens. Put
  independent calls (settings for every league) in one parallel batch.
- Ask in batches: one question round covering every league, not one league per turn.

## Start: read the state, then route

First, both free:

1. Read `~/.config/h2h-advisor/config.toml` (under `$H2H_ADVISOR_HOME` instead of `~` when that
   is set). No file means a first run.
2. Call `status()`. `sites.<site>.logged_in` is `true`, `false` (needs login) or `null` (not
   checked yet).

If the h2h-advisor tools are missing, or every call fails, go to **Diagnose** before anything
else. Otherwise go only where the trigger leads:

| What brought you here | Do |
|---|---|
| First run, or "set it up" | Login (if not both `true`) → Leagues → Choices → Write → Check |
| "<site> needs login", or `logged_in: false` | Login → Verify. The config is not touched. |
| "set bbm_league_id for <league> (BBM leagues: …)" | Choices step 1 for that league → Write → Check |
| Change never-drop, a build or stream slots | that choice → Write → Check |
| A league the config lacks (`list_leagues` shows it) | Leagues and Choices for that league only |
| Guidance that expires ("keep him this week", "drop him first") | Not config: a note. `add_note(...)`, as streaming-advisor does. |

On a re-run, whatever the config already holds is settled: do not ask about it again unless the
user raises it.

## Diagnose

- **A tool error naming `config.toml`** (invalid TOML, an unknown key, `never_drop` given as a
  string): the config is broken and every tool fails until it is fixed. Read the file, find the
  line, then go to Write with the corrected whole file.
- **Tools missing.** In Claude Code they are `mcp__plugin_h2h-advisor_h2h-advisor__*` and may need
  loading with ToolSearch first. If they are truly absent, check `uv --version` (missing → install
  from https://docs.astral.sh/uv/) and Chrome (`ls "/Applications/Google Chrome.app"`, or the path
  in `$H2H_ADVISOR_CHROME`), then start the server by hand to see why it does not come up:

  ```bash
  (echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"setup","version":"0"}}}'; sleep 5) | uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor serve
  ```

  A JSON reply with `serverInfo` means the server is fine and only the connection is not:
  `/mcp` → h2h-advisor → reconnect. A traceback is the real error: show it. A second
  `h2h-advisor` server that fails while the plugin's one works is a duplicate registration (a
  project `.mcp.json`, say); use the plugin's tools.

## Login

Explain, then ask before launching anything:

> A plain Google Chrome window will open with Yahoo Fantasy and Basketball Monster. Log in to both
> by hand (Google sign-in works there), then quit that Chrome with **Cmd+Q** (closing the window
> is not enough on macOS). The cookies stay in h2h-advisor's own Chrome profile.

The user picks one of two ways:

- They run it: `! uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login`
- With their explicit OK, you run the same command with Bash, `run_in_background: true` (it
  waits until Chrome quits, which can take minutes).

Without Bash (Claude Desktop chat), give the command without the `!` for a terminal.

"That Chrome profile is already open": the h2h-advisor Chrome is still running; Cmd+Q it and
rerun. "browser busy": another session's server holds the browser; retry when it is idle.

**Verify** once Chrome has quit: `list_leagues(refresh=true)` (one Yahoo page), then `status()`;
`sites.yahoo.logged_in` must be `true`. BBM is checked by the first BBM page (Choices step 1 or
the check-up brief); a "bbm needs login" notice there sends you back here.

## Leagues

`list_leagues()`, then `get_league_settings` for each league the config lacks, in one batch.
Show one compact table: league id and name, team, scoring (`categories` or `one_win`), max adds
per week, FAB waivers or not.

## Choices

Gather what you need for every new league first, then ask about all of them in one round.
Where the user has no view, the default is a fine answer; offer it as one.

1. **BBM league.** For each league without `bbm_league_id`: `get_free_agents(league, limit=1)`.
   - "matched <league> ↔ BBM <name>": matched by name, nothing to write. Mention it.
   - "set bbm_league_id for <league> (BBM leagues: 30008 = _2. I Liga H2H … (Yahoo!); …)":
     propose the closest name and ask. BBM decorates names with a leading "_2." and a trailing
     "(Yahoo!)", and small spellings differ ("1 Liga" vs "I Liga"). Write the option **value**,
     quoted (`bbm_league_id = "30008"`), never the visible name, and never a value another league
     already pins.
   - "BBM leagues: none loaded": BBM did not load this time. Retry later; do not guess.
2. **Never-drop.** Players protected all season, globally or for one league. Ask who; a
   franchise player (Victor Wembanyama, say) is the typical case, but add only names the user
   gives. Guidance with an end ("while he's hurt", "this week") is a note, not never-drop.
3. **Build** (`strong` / `punt` / `weak` categories). Unpinned, the advisor infers it: from
   win rates once the league has 3 finished weeks, before that from roster strengths. Pin it when
   the user drafted for a plan. Ask how they built the team and never assume one. Two common
   shapes, as examples only:
   - Guard-heavy: punt `FG_PCT`, `TO`, `BLK`; weak `REB` (still contested, not punted); strong
     `FT_PCT`, `TPM`, `PTS`, `AST`, `STL`.
   - Big-man: strong `BLK`, `TO`, `FG_PCT`, `STL`, `REB`; weak `PTS`, `TPM`, `AST`, `FT_PCT`.
4. **Stream slots** (optional). `stream_slots = N` makes the drop pool the bottom N roster
   players by rest-of-season value. Default: automatic (players near replacement level).

## Write

Read `references/config.md` for the schema and the validation command. Then:

1. **Merge** into the existing file: keep every key and league already there; add or change only
   what the user agreed to.
2. Show the **full resulting file** and its path, and ask for a yes.
3. On a yes: `cp config.toml config.toml.bak` if the file exists, `mkdir -p` the folder, write it,
   and run the validation command. On an error, fix it and show the file again.

Without Bash (Claude Desktop chat), show the file and the path to save it at; the user saves it.

## Check

The server reads the config once, on its first call, so it needs a reconnect after a write:
`/mcp` → h2h-advisor → reconnect in Claude Code; quit and reopen Claude Desktop. Ask the user to
do it and to say when they are done.

Then check in proportion to the change:

- First run: `daily_brief(leagues="all")`, presented as streaming-advisor's all-leagues status
  brief (status per league, adds left, data age, every notice). Say first that it may take a few
  minutes.
- One league's BBM pin: `get_free_agents(league, limit=1)`; no "set bbm_league_id" notice means
  it took.
- Never-drop, build or stream slots: `analyze_week(league)` shows the build in use; nothing more
  is needed for a never-drop edit beyond a valid config.
- Login only: Verify above is the check.

A remaining "needs login" or "set bbm_league_id" notice points to the step still undone. End with
a short summary: what changed, what was left as default, anything still open.
