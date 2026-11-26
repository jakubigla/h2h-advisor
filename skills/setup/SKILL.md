---
name: setup
description: Use when setting up h2h-advisor for the first time or again - logging in to Yahoo and Basketball Monster, a "needs login" notice, adding or changing leagues, never-drop players, league builds (punt/strong categories), stream slots, a "set bbm_league_id" notice, or editing the h2h-advisor config.
---

# h2h-advisor setup

Guided, re-runnable setup. It is idempotent: read what exists, keep it, and ask only about what is
new or missing. Go step by step; do not skip ahead.

**Never log in through MCP tools** - no tool can, and none should try. Login is always the user's,
in a plain Chrome window. Never write the config without showing the full file and getting a yes.

## 1. Prerequisites

Check with Bash and report each one:

- `uv --version` (missing → install from https://docs.astral.sh/uv/).
- Google Chrome: `ls "/Applications/Google Chrome.app"` (or the path in env `H2H_ADVISOR_CHROME`).
- The MCP server answers: call `status()`. If the tool is missing, the plugin's server did not
  start: suggest `/mcp` to see the error.

## 2. Login

From `status()`: `sites.yahoo.logged_in` / `sites.bbm.logged_in` are `true`, `false` (needs login)
or `null` (not checked yet - `status()` loads no pages). If both are `true`, skip to step 4.

Otherwise explain, then ask before launching anything:

> A plain Google Chrome window will open with Yahoo Fantasy and Basketball Monster. Log in to both
> by hand (Google sign-in works there), then quit that Chrome with **Cmd+Q** (closing the window
> is not enough on macOS). The cookies stay in h2h-advisor's own Chrome profile.

Two ways, the user picks:

- The user runs it: `! uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login` (give them the
  command with the plugin's real path filled in)
- With their explicit OK, you run it via Bash: `uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login`
  with `run_in_background: true` (it waits until Chrome quits, which can take minutes).

If it says "That Chrome profile is already open", that h2h-advisor Chrome is still running: quit
it with Cmd+Q and rerun. If it reports the browser is busy, another session's MCP server holds the
browser: retry when that session is idle.

## 3. Verify

After Chrome has quit: `list_leagues(refresh=true)` (one Yahoo page), then `status()`.
`sites.yahoo.logged_in` must be `true`. BBM is checked by the first BBM page in step 5; if a
"needs login" notice appears there, go back to step 2.

## 4. Discover leagues

`list_leagues()`: show each league's id, name and team name.

## 5. Per league

For each league (and only for what the current config lacks, on a re-run):

1. `get_league_settings(league)`: show categories, roster slots, max adds per week, scoring type
   (`categories` or `one_win`) and whether waivers use FAB.
2. **BBM league match**: `get_free_agents(league, limit=5)`. A notice "matched <league> ↔ BBM …"
   means it is matched by name; nothing to write. A notice "set bbm_league_id for <league> …"
   means it is not: call `get_projections(scope="ros")` once, show
   `data.league_options` (option value → BBM league name) and ask which one is this league. Write
   the **option value** (the key, e.g. `"1234"`), not the visible name, as `bbm_league_id`.
3. **Never-drop**: make sure the global list holds Victor Wembanyama (it is empty on a first run;
   add him). Ask for additions, global or for this
   league only.
4. **Build**: pin `strong` / `punt` / `weak` categories, or leave unpinned to infer from rosters
   and history. Ask how the user built the team; two common shapes, as examples only (never assume one):
   - Guard-heavy: punt `FG_PCT`, `TO`, `BLK`; weak `REB` (still swing, not punted); strong
     `FT_PCT`, `TPM`, `PTS`, `AST`, `STL`.
   - Big-man: strong `BLK`, `TO`, `FG_PCT`, `STL`, `REB`; weak `PTS`, `TPM`, `AST`, `FT_PCT`.
5. **Stream slots** (optional): `stream_slots = N` forces the drop pool to the bottom N roster
   players by rest-of-season value. Default: automatic (players near replacement level).

## 6. Write the config

File: `~/.config/h2h-advisor/config.toml` (under `$H2H_ADVISOR_HOME` instead of `~` when that is
set). Read the existing file first and **merge**: keep every key and league already there, add or
change only what the user agreed to. Show the **full resulting file** and ask for confirmation; write
it only after a yes. Then validate it (the TOML, the keys and every pinned build's category names;
fix and re-confirm on any error):

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT} python -c "from h2h_advisor.config import load_config; from h2h_advisor.analysis.build import build_from_config; c = load_config(); [build_from_config(lc.build) for lc in c.leagues.values()]; print('config OK:', sorted(c.leagues))"
```

Schema the loader accepts (unknown keys in a league table are an error):

```toml
[global]
never_drop = ["Victor Wembanyama"]   # a list, never a string; merged into every league
# drop_margin = 0.25                 # ROS value margin above the best free agent for the drop pool
# light_day_max_games = 4            # days with this many NBA games or fewer are "light"

[league."<yahoo league id>"]         # the id from list_leagues, quoted
never_drop = []                      # added to the global list for this league
# stream_slots = 5                   # drop pool = bottom 5 by ROS value
# max_adds = 7                       # only used if the settings page gives no max adds
# bbm_league_id = "1234"             # BBM league option value when the name match fails
# build = { strong = ["FT_PCT", "TPM"], punt = ["FG_PCT", "TO"], weak = ["REB"] }

# [limits]                           # daily page-load caps and the delay between loads
# yahoo_daily_pages = 250
# bbm_daily_pages = 80
# delay_min_s = 4.0
# delay_max_s = 9.0

# [ttl_s]                            # cache lifetimes in seconds: settings, matchup, roster,
# matchup = 1200                     # free_agents, bbm_day, bbm_week, bbm_ros

# [aliases]                          # player name as one site spells it = canonical name
# "Nic Claxton" = "Nicolas Claxton"
```

Category names: `FG_PCT`, `FT_PCT`, `TPM`, `PTS`, `REB`, `AST`, `STL`, `BLK`, `TO`.

The running MCP server read the config when it started: ask the user to reconnect it (`/mcp` →
h2h-advisor → reconnect, or restart Claude) so the new file is used.

## 7. Smoke test

`daily_brief(leagues="all")`, presented as the all-leagues brief from the streaming-advisor skill
(status per league, adds left, data age, every notice). Any remaining "set bbm_league_id" or
"needs login" notice means the matching step above is not done yet.
