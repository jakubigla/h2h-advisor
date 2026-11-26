# h2h-advisor — design

Date: 2026-11-25
Status: draft, awaiting approval

## 1. Intent

Jakub plays in several Yahoo Fantasy NBA head-to-head leagues. He wants to ask Claude (Claude Code or Claude Desktop) questions like:

- "Who should I add and start today in league X?"
- "How should I spend my remaining adds this week?"
- "Which categories can I still flip, and which should I let go?"

and get advice grounded in live data: the current matchup state, his roster, the opponent's roster, available free agents, each league's settings (especially the weekly add cap), remaining games, and Basketball Monster (BBM) projections.

Neither Yahoo nor BBM offers usable API access (Yahoo's Fantasy API now requires manual approval, which Jakub was refused; BBM has no API). Data therefore comes from the websites themselves, read through a real Chrome browser session that Jakub logs into.

Deliverable: a **Claude Code plugin** containing a local **MCP server** plus a **skill** with the 9-cat streaming playbook. The same MCP server must also be usable from Claude Desktop.

## 2. Goals

1. Read, for every Yahoo NBA league Jakub is in (auto-discovered): league settings, current matchup (per-category totals for both sides), both rosters, free agents, and moves used/remaining this week.
2. Read BBM member projections: daily (per date), weekly, and rest-of-season.
3. Know the NBA schedule so remaining games per player per day are known.
4. Compute deterministically (in the server, not by the LLM): projected end-of-week category totals for both sides, per-category win likelihood, and the expected impact of add/drop candidates over a horizon (today / rest of week).
5. Let Claude do the judgement: what to chase, what to punt, which add/drop, which day.
6. Cache everything locally so repeated questions are fast and don't hit the sites.
7. Respect a per-league **never-drop list** (e.g. Wembanyama) — never recommended as a drop.

## 3. Non-goals

- Making any change on Yahoo or BBM: no adds, drops, trades, lineup changes, settings changes. Advice only; Jakub executes moves himself.
- Points leagues or non-9-cat formats (all current leagues are H2H 9-cat). Design shouldn't make them impossible later, but don't build for them.
- Keeper / dynasty logic (no league is keeper).
- Other platforms (ESPN, Sleeper, Fantrax).
- Hosting for other users. Single user, runs on Jakub's Mac.

## 4. Anti-goals (failure even if it "works")

- **Account ban or warning** on Yahoo or BBM due to automated traffic.
- **Any state-changing request** reaching either site.
- Behaving like a crawler: bursts, parallel tabs, paging through everything.
- Recommending dropping a never-drop player.
- Presenting stale data as current. Every answer must be able to show data age.

## 5. Constraints

- Yahoo and BBM terms of use likely discourage automated access. This is personal, low-volume use of Jakub's own logged-in accounts; the design minimises volume and never writes. Jakub accepts the residual risk.
- BBM projections require a logged-in membership. BBM is an ASP.NET WebForms site: filters (period, date) and "Export to Excel" are likely **postbacks** (form POSTs with `__EVENTTARGET`, `__VIEWSTATE`), not plain GET links. BBM robots.txt allows everything.
- Chrome no longer allows remote-debugging the user's default profile, so the tool uses its own dedicated profile.
- macOS, single machine.
- 9-cat specifics: FG% and FT% must be aggregated from makes/attempts, never averaged. TO is "lower wins".
- Leagues differ in weekly add cap (most 5, one 7) — read from settings, never hard-coded.

## 6. Approaches considered

| | Approach | Trade-offs | Verdict |
|---|---|---|---|
| A | **Dedicated Playwright-controlled Chrome with its own persistent profile**; server fetches and parses pages; read-only enforced at network layer; SQLite cache | Enforceable read-only guard, deterministic parsing, cacheable, works from Code and Desktop. Cost: occasional re-login, parsers break when sites change | **Chosen** |
| B | Claude drives Jakub's real Chrome via Claude in Chrome extension, guided by a skill | No server to build, but read-only is only an instruction (Claude can click anything), slow, token-heavy, non-deterministic, no cache | Rejected |
| C | Attach to Jakub's everyday Chrome over CDP | Blocked on default profile by modern Chrome; exposes whole browser | Rejected |

## 7. Design (decided parts)

### 7.1 Components

```
Claude Code / Claude Desktop
        │  MCP (stdio)
        ▼
h2h-advisor MCP server (Python, FastMCP)
  ├── tools/         read-only MCP tools (§7.5)
  ├── analysis/      deterministic 9-cat math (§7.6)
  ├── cache/         SQLite: parsed data + raw snapshots + request log
  ├── sources/
  │    ├── yahoo/    page fetch + parsers
  │    ├── bbm/      page fetch/postback + parsers/export reader
  │    └── schedule/ NBA schedule (public static JSON from NBA CDN, no login)
  └── browser/       Playwright persistent context + ReadOnlyGuard + RateLimiter
```

Language/tooling (builder's call confirmed by Jakub's choice of approach A): Python, `uv`, FastMCP, Playwright (Chromium/Chrome channel), SQLite.

### 7.2 Browser session

- A persistent Chrome profile at `~/.local/share/h2h-advisor/chrome-profile` (exact path builder's call), used only by this tool.
- **Login**: a CLI command (`h2h-advisor login`) opens a plain (non-automated) Chrome on the profile with the Yahoo and BBM login pages. Jakub logs in by hand. The guard is never relaxed, and no MCP tool can trigger login. See §13.
- **Fetch mode** (all MCP tool calls): guard fully on. One tab, one request chain at a time.
- **One instance at a time** (designer's call): Chrome allows one process per profile, and rate limits/caps must be shared. A lockfile guards the profile; a second server instance (e.g. Claude Desktop while a Claude Code session is running) can still serve cached data but returns a clear "browser busy in another session" for anything needing a fetch. Request counts live in SQLite so caps are shared across instances. A shared background daemon is a possible later upgrade, not in scope.
- When a fetch lands on a login page, captcha, or unexpected redirect: stop immediately, don't retry, mark the source as `needs_login`, and return that clearly to Claude (which tells Jakub to run `h2h-advisor login`).

### 7.3 ReadOnlyGuard (hard requirement)

Implemented with Playwright request interception on every request from the context:

1. **Host allowlist**: Yahoo fantasy basketball host(s), Yahoo static/asset hosts needed to render, `basketballmonster.com`. Everything else is aborted (also cuts ads/analytics load).
2. **Yahoo**: `GET` by default. `POST/PUT/PATCH/DELETE` are aborted, **except** specific read-only data requests (e.g. internal API/GraphQL reads) that were observed and reviewed during discovery and are allowlisted individually by endpoint + operation name (decided by Jakub). Additionally abort any URL matching transaction/action patterns (add, drop, trade, claim, waiver, lineup edit, settings edit, messages) regardless of method.
3. **BBM**: `GET` to allowlisted pages. `POST` allowed **only** to the specific projections pages, and only when `__EVENTTARGET` (or the submit button name) is in an explicit allowlist of read-only controls (period/date selectors, Apply/Refresh, Export). All account, settings, league-settings, and payment pages are denied regardless of method.
4. **Page-navigation allowlist**: top-level navigations only to known URL patterns the parsers handle.
5. Resource types image/media/font are aborted to reduce load (builder may relax if a page breaks).
6. Every allowed and blocked request is logged to SQLite with timestamp, method, URL, and verdict.
7. The MCP surface has **no** generic navigate/click/type tool. Tools only call fixed fetchers.

Unit tests must prove blocking of: Yahoo POST, Yahoo add/drop URLs, BBM POST with a non-allowlisted event target, BBM settings pages, foreign hosts.

### 7.4 Politeness / rate limiting

- Strictly sequential per site, never parallel tabs.
- Random delay between page loads of 4–9 s per site.
- Daily caps per site (starting values: Yahoo 250 page loads, BBM 80), configurable; when hit, serve cache only and say so.
- Back off and stop on HTTP 429/403/5xx bursts; don't hammer retries (max 1 retry after 30 s+).
- Real Chrome, real profile; no fake headers. Headed-but-minimised vs. headless is builder's call (prefer whichever doesn't trigger extra verification).
- Prefer the fewest pages that answer the question. Example: if one BBM page with Jakub's synced league shows availability/owners for all players, prefer it over paging Yahoo's free agent list (to be confirmed in discovery, §8).

### 7.5 MCP tools (all read-only)

Every response includes `fetched_at` / data age per source and `stale: bool`. Most accept `refresh: bool` (forces fetch, still subject to rate limits and minimum TTL floor of ~2 min).

| Tool | Returns |
|---|---|
| `list_leagues()` | Jakub's NBA leagues: id, name, team name, current week |
| `get_league_settings(league)` | categories, roster slots, max adds/week, week boundaries, playoff info |
| `get_matchup(league, week?)` | opponent, per-category totals both sides (FG/FT with makes/attempts), games played & remaining each side, moves used/remaining |
| `get_roster(league, team="me"\|"opponent"\|team_id)` | players, positions, NBA team, injury status, current lineup slot |
| `get_free_agents(league, limit=50, position?)` | available players (not on waivers by default; waiver status flagged) |
| `get_projections(scope="day"\|"week"\|"ros", date?)` | BBM per-game/total projections incl. makes/attempts for FG/FT |
| `get_schedule(start, end)` | games per NBA team per day |
| `analyze_week(league)` | §7.6 projected final totals and per-category win likelihood, adds left, days left |
| `rank_streamers(league, horizon="today"\|"rest_of_week", max_results=10)` | ranked add/drop pairs with per-category impact and which days to start the pickup; never-drop players excluded |
| `status()` | session health per site, cache freshness, today's request counts vs caps, recent blocked requests |

### 7.6 Analysis (deterministic)

- **Projected final totals** per side = current week totals + Σ over remaining days of (players with a game that day, constrained by active roster slots by position) × BBM per-game projection. Injured/out players count zero. FG%/FT% computed from projected makes/attempts.
- **Daily slot constraint**: if more players have games than active slots on a day, keep the best-projected eligible set (greedy by position eligibility is acceptable).
- **Per-category win likelihood**: from projected margin vs. a variance estimate per category (simple normal approximation is fine; builder's call on variance source).
- **Drop pool (decided by Jakub: depends on league and season projections)**: a roster player is droppable when their BBM rest-of-season value is close to replacement level, i.e. within a margin of the best available free agents' rest-of-season value in that league. Deep teams yield a small pool (1–2), churn teams a larger one (4–5). Per-league override `stream_slots = N` in config forces the pool to the bottom N by ROS value. Never-drop list always excluded. Margin default is builder's call, configurable. This means `rank_streamers` always needs ROS projections as well as the horizon projections.
- **Streamer evaluation**: for each candidate FA × each player in the drop pool, recompute projected totals and expected categories won for the horizon. Respect adds remaining this week; for `rest_of_week`, consider that a pickup can itself be streamed again later (one-level lookahead is enough; no full optimisation).
- Report raw numbers; leave "punt this category" judgement to Claude via the skill.

### 7.7 Cache & freshness

SQLite at `~/.local/share/h2h-advisor/cache.db`. Store parsed records and the last N raw HTML/exports per page type (for debugging and parser fixtures).

Default TTLs (configurable):

| Data | TTL |
|---|---|
| League settings | 7 days |
| Matchup totals | 20 min |
| Rosters | 20 min |
| Free agents | 60 min |
| BBM daily projections (per date) | 3 h |
| BBM weekly / ROS projections | 12 h |
| NBA schedule | 24 h |

### 7.8 Configuration

`~/.config/h2h-advisor/config.toml`:

```toml
[global]
never_drop = ["Victor Wembanyama"]

[league."<yahoo_league_id>"]
never_drop = []          # merged with global
# stream_slots = 5       # optional: force drop pool to bottom N by ROS value
# optional overrides if settings parse fails: max_adds = 7

[limits]
yahoo_daily_pages = 250
bbm_daily_pages = 80
```

Player identity across Yahoo/BBM/NBA schedule: match by normalised name + NBA team, with a manual alias table in config for misses.

### 7.9 Skill: 9-cat streaming playbook

A skill in the plugin (`skills/streaming-advisor/SKILL.md`) that tells Claude how to answer streaming questions:

1. Call `status()`; if a site needs login, say so first.
2. `analyze_week` → identify categories that are locked (won/lost), swing, and out of reach.
3. Decide chase vs punt, considering games remaining for both sides and adds left.
4. `rank_streamers` for the horizon; sanity-check against projections and injury status.
5. Answer: concrete "Add X, drop Y, start on Mon/Wed/Thu", why (categories moved), adds left after, data age. Never claim to have made the move.

### 7.10 Packaging

- Repo layout as Claude Code plugin: `.claude-plugin/plugin.json`, `.mcp.json` (launches the server via `uv run`), `skills/streaming-advisor/`, `src/h2h_advisor/`, `tests/`.
- README section with the Claude Desktop `claude_desktop_config.json` snippet for the same server.

## 8. First milestone: logged-in discovery

Page structures behind login are unknown and must be mapped before writing parsers. With Jakub logged in to the dedicated profile, the builder runs a guarded, manual-paced discovery session to:

- Map Yahoo URLs for: league list, settings, matchup, team roster, free agents, moves-used indicator, and how the current week/opponent are identified.
- Map BBM projections pages (daily/weekly/ROS): URLs, postback control names for period/date, whether Export to Excel exists there, and whether exported data includes makes/attempts.
- Check whether BBM's league integration (Jakub's synced Yahoo leagues, "Owner Names" column) can supply availability for all players in one page, reducing Yahoo free-agent paging.
- Save sanitized HTML/export fixtures for parser tests. Fill in the ReadOnlyGuard allowlists from what was observed.

## 9. Left to the builder

- Exact module structure, parser library (selectolax/BeautifulSoup), Excel reader.
- Headless vs. headed-minimised browser.
- Variance model for win likelihood.
- Exact URL patterns and BBM control names (from discovery).
- Test strategy beyond the mandatory guard tests: parser tests against saved fixtures; no live-site tests in CI.

## 10. Addendum (2026-11-25, approved): advisor skill and engine additions

From a follow-up brainstorm with Jakub about how he actually makes decisions.

### 10.1 Advisory principles (the skill)

1. **Options, not verdicts.** Claude lays out options with trade-offs; Jakub decides. Every "what should I do" answer has two separate lists: **This week** (streamers) and **Long-term upgrades** (shown whenever a candidate exists, even with 1 usable game this week). Each option states its cost (adds used, adds left, who is dropped) and its gain (categories moved this week, or ROS value gained).
2. **Two modes, one league or all leagues.** "How am I doing?" → status report: category table with current totals, projected totals and win chance, usable games left (me vs opponent), adds left, days left, and a few notable facts (light days left, categories within a hair, opponent's games edge). "What should I do?" → moves. Both work for one league or all of Jakub's leagues (5 this season) in a single prompt.
3. **Usable games, not raw games** (BBM's "quality games" idea). A player's value this week is the games he would actually start given open active slots by position on each day. Light days (≤ 4 NBA games, configurable `light_day_max_games`) are highlighted because open slots are likeliest there.
4. **Position fit.** A candidate only counts on days where an eligible active slot is open; show the slot per day. (E.g. a PF/C-heavy roster cannot use another big on most days.)
5. **Respect the league build.** No credit for categories the build punts or reliably wins; no penalty for hurting a punted category. Weekly numbers may override (flag when a "strong" category is actually at risk this week).
6. **Chase the close categories.** Judged by margin and games left for both sides (server win probability). Injuries: rely on projections; no special logic.
7. Always state data age; never claim a move was made.
8. **Waivers are not free agents (2026-11-25, Jakub).** A player on waivers (Yahoo Roster Status `W (<date>)`, BBM Own `WW`) cannot be added now. The engine already counts only his games on/after the waiver-clear date and leaves him out of "today" and long-term lists. The skill must say so up front: never present a waiver player as an immediate add; label him "on waivers — clears <date>", show only the days he can actually play for you, and in FAB leagues call it a claim/bid, not an add. If a waiver player is the best option, present him as a "claim now, plays from <date>" option next to the best immediate free agent.

### 10.2 League build

- `Build(strong: set[Category], punt: set[Category], weak: set[Category])` per league.
- **Pinned** in config (`[league."<id>"] build = { strong = [...], punt = [...] }`) overrides inference. Defaults to pin during setup:
  - League 1: punt FG_PCT, TO, BLK; REB weak-but-swing (not punted); strong FT_PCT, TPM, PTS, AST, STL.
  - Second (big-man) league: strong BLK, TO, FG_PCT, STL, REB (REB is the one to improve — long-term upgrades adding REB rank higher); PTS, TPM, AST, FT_PCT weaker but not punted.
- **Inferred** otherwise from (a) BBM per-category values of the rostered players (sum per category vs league-neutral zero) and (b) matchup history win rate per category. Inference: category win rate ≥ 0.75 over ≥ 3 weeks or per-cat roster value in top third → strong; win rate ≤ 0.2 over ≥ 3 weeks → punt; else neutral.
- Weighting in streamer scoring: categories in `punt` get weight 0; categories in `strong` whose this-week status is `likely_win`/`locked_win` get weight 0; otherwise weight 1.

### 10.3 Matchup history

- Past weeks' matchup pages (one Yahoo page per league per week), parsed with the same matchup parser, cached **permanently** (past weeks never change). Loaded once at first use, then one new page per week.
- Exposes per-category W/L/T for Jakub per week, plus the opponent of each week. Used for build inference and for "this opponent usually wins X" facts.

### 10.4 New/changed server outputs

- Streamer options carry `usable_days`, `slot_by_day`, `raw_games`, `light_days` (subset of usable days that are light).
- `long_term_upgrades(league)`: free agents whose build-weighted ROS value exceeds the weakest drop-pool player's by ≥ `drop_margin`, with their usable games this week and per-category ROS deltas.
- `get_history(league)`.
- `daily_brief(leagues="all", horizon="today")`: one browser session, sequential, for each league: status (as in 10.1.2), top 3 streamers, long-term upgrades, notable facts. Budget ≈ 25–35 Yahoo + 2–4 BBM page loads for 5 leagues.

### 10.5 Later (not in this build)

- Next-week prep: value late-week pickups by next week's Monday/light-day usable games; next opponent's strength and schedule.

## 11. Addendum (2026-11-25, approved): guided setup command

A plugin skill/command `/h2h-advisor:setup`, re-runnable and idempotent (keeps existing config, asks only about what's new or missing):

1. Prerequisites: `uv` present, Google Chrome present, MCP server responding (`status()`).
2. Login: Claude asks Jakub to run `! uv run --directory <plugin root> h2h-advisor login` (plain Chrome window, Jakub logs into Yahoo and BBM by hand, then quits that Chrome with Cmd+Q / presses Enter; see §13). MCP tools can never trigger login; this step is always the user's.
3. Verify: `status()` shows both sites logged in.
4. Discover leagues: `list_leagues`.
5. Per league: show settings (categories, roster slots, max adds/week); ask never-drop additions (global list holds Wembanyama), build (pin strong/punt/weak or infer), optional `stream_slots` override.
6. Write `~/.config/h2h-advisor/config.toml` — show the full file to Jakub and get confirmation before writing; merge with existing content.
7. Smoke test: `daily_brief(leagues="all")`.

## 12. Rename (2026-11-25)

The project is named **h2h-advisor** (package `h2h_advisor`, CLI `h2h-advisor`, env `H2H_ADVISOR_HOME`, data dir `~/.local/share/h2h-advisor`, config `~/.config/h2h-advisor/config.toml`). Earlier sections' "hoops-advisor" names refer to the same thing.

## 13. Login via plain Chrome (2026-11-25)

Google refuses sign-in in automation-controlled browsers ("This browser or app may not be secure"), and Jakub logs into Yahoo via Google. Automation is never disguised; instead login happens outside Playwright, and §7.2's "Login mode" is replaced by this:

- `h2h-advisor login` takes the profile lock (busy → clear message, exit 1), writes the no-session-restore preference, and starts an ordinary Google Chrome process (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`, override with env `H2H_ADVISOR_CHROME`; missing → clear error, exit 1) with `--user-data-dir=<profile> --no-first-run --no-default-browser-check --new-window` and the Yahoo fantasy and BBM login pages. No Playwright, no `--remote-debugging`, no automation flags. A separate `--user-data-dir` runs as an independent Chrome instance even when Jakub's normal Chrome is open.
- With a TTY it waits for Enter (then SIGTERM, up to 15 s for cookies to flush, then kill) or for Chrome to exit. Without a TTY (launched by Claude) it waits for the Chrome process to exit: Jakub quits it with Cmd+Q (closing the window alone keeps Chrome running on macOS). Chrome inherits the locked lock fd, so the profile stays locked for Chrome's whole life even if the `login` process dies; SIGTERM/SIGHUP/SIGINT to `login` quit Chrome gracefully first. If the profile's `SingletonLock` names a live process, or Chrome exits within 3 s (handed off to a Chrome already on the profile, or failed to start), `login` exits 1 with "That Chrome profile is already open — quit it (Cmd+Q) and rerun". Success is reported only when Chrome exited 0 or was terminated by `login`.
- The guard is FETCH-only: there is no login mode, login hosts are always denied (still recognised, so being bounced to one means `needs_login`), and no relaxation of any rule exists anywhere. `discover` and the MCP server use the guarded Playwright FETCH session.
- Playwright launches Chrome without `--use-mock-keychain`, so it decrypts cookies the plain Chrome wrote (same macOS "Chrome Safe Storage" keychain item).

## 14. Addendum (2026-11-25, approved): objective follows league scoring type

- Yahoo "Head-to-Head - One Win": the week is won by taking a majority of categories; the objective is **P(win the matchup)** = P(categories won > categories lost), ties counted half, computed exactly (Poisson-binomial) from per-category win probabilities. Advice concentrates on the ~5–6 most winnable categories and concedes the rest.
- Yahoo "Head-to-Head - Categories": objective stays **expected categories won**.
- `LeagueSettings.scoring_type: Literal["categories","one_win"]`, parsed from the settings page "Scoring Type". `analyze_week` reports both numbers; `rank_streamers` scores deltas with the league's objective (build weights still apply per category before aggregation for categories leagues; for one_win, punted categories are fixed at their current win probability, i.e. not chased).
- Skill: in one_win leagues say "you need N of the remaining swing categories", name the target set of 5, and treat categories outside it as conceded.

## 15. Addendum (2026-11-25): as built

- **NBA schedule.** The schedule comes from Basketball Monster's day pages (the games table
  above the player table on `dailyprojections.aspx`), one page per day the engine needs. The NBA
  schedule CDN (`cdn.nba.com`) answers automated clients with 403, so nba.com is not used and is
  not an allowed host.
- **§7.3.6 logging.** What is logged to SQLite is blocked requests and page loads (the rate
  limiter's record of each navigation), not every allowed sub-request.
- **§7.4 back off and stop.** A page load answered 403, 429 or 5xx (after the session's single
  retry) stops that site for the rest of the call and starts a 15-minute cooldown stored in the
  cache; until it ends, calls serve cached data with a notice and `status()` shows
  `backed_off_until`.
- **Variance model.** A counting category's variance grows with each active player-game in
  proportion to that player's per-game production (relative to a league-typical player), so an
  add who produces nothing in a category adds no uncertainty to it. Moves below the skill's
  "negligible" cut (0.05 expected categories; 0.01 matchup win probability in one-win leagues)
  are not offered.
