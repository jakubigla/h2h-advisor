# h2h-advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin with a local, strictly read-only MCP server that reads Jakub's Yahoo H2H 9-cat leagues and Basketball Monster member projections through a dedicated Chrome profile, caches them, and computes week projections and add/drop recommendations.

**Architecture:** Python package `h2h_advisor` exposing FastMCP tools over stdio. Pages are fetched by one Playwright persistent Chrome context guarded by a pure `decide()` request filter and a SQLite-backed rate limiter; parsed records are cached in SQLite with per-type TTLs. Deterministic 9-cat analysis (week projection, win probability, drop pool, streamer ranking) is pure functions over models; a skill tells Claude how to turn them into advice.

**Tech Stack:** Python 3.12, uv, FastMCP (`mcp` SDK), Playwright (Chrome channel), httpx, selectolax, openpyxl, SQLite (stdlib), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-25-h2h-advisor-design.md`

## Global Constraints

- Nothing may ever send a state-changing request to Yahoo or BBM. No MCP tool may expose generic navigate/click/type.
- Yahoo: GET only, except individually allowlisted read-only data requests (endpoint + operation name) recorded during discovery. Action URLs (add, drop, trade, claim, waiver, lineup edit, settings edit, messages) are blocked regardless of method.
- BBM: GET to allowlisted pages; POST only to projections pages with `__EVENTTARGET`/submit name in an explicit allowlist. Account, settings, league-settings, payment pages are always denied.
- Strictly sequential per site; random 4–9 s between page loads; daily caps Yahoo 250, BBM 80 page loads; max 1 retry after ≥30 s; on login page / captcha / unexpected redirect: stop, no retry, report `needs_login`.
- One server instance owns the browser at a time (profile lockfile); other instances serve cache and report "browser busy in another session". Request counts live in SQLite.
- TTLs: settings 7 d, matchup 20 min, rosters 20 min, free agents 60 min, BBM daily projections 3 h, BBM weekly/ROS 12 h, schedule 24 h. `refresh=True` floor: 120 s.
- FG% and FT% always from makes/attempts. TO: lower wins.
- Max adds per week read from league settings, never hard-coded (config override only if parse fails).
- Never-drop list (global + per league) is never recommended as a drop.
- Every tool response carries per-source `fetched_at` and `stale`.
- Paths: profile `~/.local/share/h2h-advisor/chrome-profile`, cache `~/.local/share/h2h-advisor/cache.db`, config `~/.config/h2h-advisor/config.toml`; all overridable with env `H2H_ADVISOR_HOME` (tests always set it to a tmp dir).
- Local test runs: only the test file(s) touched by the task (machine is shared; no full-suite runs). No test may hit a live Yahoo/BBM/NBA site.
- Commits are local only; never push.

## Review Focus

1. **League lineup changes are "tomorrow", not "today"** — a pickup must contribute games only from the next day; `rank_streamers` for horizon `today` then evaluates tomorrow. Test in Task 11.
2. **Free agent is on waivers** — cannot be added immediately; excluded from `today`, and for `rest_of_week` contributes only from the waiver-clear date if known, else excluded. Test in Task 11.
3. **Injured / IL-slotted players** — players with status `O`/`INJ` or in IL/IL+ slots contribute zero games, don't occupy active slots, and IL players are never in the drop pool (dropping them frees nothing for this week's slots but costs value). Tests in Tasks 9 and 11.
4. **Non-standard week length (All-Star break, opening/extended weeks)** — remaining days come from settings' `week_start`/`week_end`, never a Mon–Sun assumption. Test in Task 10.
5. **Session expires mid-question** — fetch raises `NeedsLogin`; the service returns cached data flagged `stale` with a `needs_login` notice instead of an error, and never retries. Test in Task 15.

---

## File Structure

```
h2h-advisor/
  pyproject.toml                         # uv project, console script `h2h-advisor`
  .claude-plugin/plugin.json             # Claude Code plugin manifest
  .mcp.json                              # launches `uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor serve`
  skills/streaming-advisor/SKILL.md      # 9-cat playbook for Claude
  README.md                              # install, login, Claude Desktop snippet
  docs/discovery.md                      # page map produced in Task 12
  src/h2h_advisor/
    paths.py          # data/profile/cache/config locations
    config.py         # Config loading, never-drop merge
    models.py         # Category, StatLine, Player, RosterEntry, LeagueSettings, Matchup, FreeAgent, Projection
    cache.py          # SQLite cache, snapshots, request log, page-load counts
    identity.py       # name normalisation, team codes, PlayerIndex
    browser/
      guard.py        # RequestInfo, Verdict, GuardRules, decide()
      guard_rules.toml# host/path/event-target allowlists (finalised in Task 12)
      ratelimit.py    # RateLimiter, DailyCapReached
      session.py      # ProfileLock, BrowserSession, detect_auth_problem, NeedsLogin, BrowserBusy
    sources/
      schedule.py     # NBA public schedule (httpx)
      yahoo/urls.py, yahoo/parsers.py, yahoo/source.py
      bbm/urls.py,   bbm/parsers.py,   bbm/source.py
    analysis/
      categories.py   # category_value, better()
      projection.py   # assign_active, project_week
      outlook.py      # category_win_prob, analyze_week
      streamers.py    # drop_pool, rank_streamers
    service.py        # Advisor: TTL, fetch-or-cache, envelopes, busy/login fallback
    server.py         # FastMCP tool definitions
    cli.py            # serve | login | discover
  tests/  (mirrors src; fixtures/ for saved pages)
```

---

### Task 1: Project scaffold, paths, config

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/h2h_advisor/__init__.py`, `src/h2h_advisor/paths.py`, `src/h2h_advisor/config.py`
- Test: `tests/test_config.py`, `tests/conftest.py`

**Interfaces:**
- Produces:
  - `paths.home() -> Path` (env `H2H_ADVISOR_HOME` or `~`), `paths.profile_dir()`, `paths.cache_db()`, `paths.config_file() -> Path`
  - `@dataclass Limits(yahoo_daily_pages=250, bbm_daily_pages=80, delay_min_s=4.0, delay_max_s=9.0)`
  - `@dataclass LeagueConfig(never_drop: list[str]=[], stream_slots: int|None=None, max_adds: int|None=None)`
  - `@dataclass Config(never_drop: list[str], leagues: dict[str, LeagueConfig], limits: Limits, ttl_s: dict[str,int], drop_margin: float=0.25, aliases: dict[str,str])`
  - `Config.never_drop_for(league_id: str) -> set[str]` (normalised names via `identity.normalize_name` — Task 8; until then use `str.casefold().strip()` and replace in Task 8)
  - `load_config(path: Path|None=None) -> Config` (missing file → defaults)
  - TTL keys: `settings, matchup, roster, free_agents, bbm_day, bbm_week, bbm_ros, schedule` with the Global Constraints values in seconds.
  - `tests/conftest.py` fixture `home(tmp_path, monkeypatch)` setting `H2H_ADVISOR_HOME`.

- [ ] **Step 1:** `git init` in `h2h-advisor/`; `uv init --package --python 3.12`; add deps `mcp playwright httpx selectolax openpyxl` and dev deps `pytest ruff`; console script `h2h-advisor = "h2h_advisor.cli:main"`; `.gitignore` with `.venv/`, `__pycache__/`, `*.db`.
- [ ] **Step 2: Write failing tests**

```python
def test_defaults_when_no_file(home):
    c = load_config()
    assert c.limits.yahoo_daily_pages == 250 and c.limits.bbm_daily_pages == 80
    assert c.ttl_s["matchup"] == 1200 and c.ttl_s["settings"] == 7 * 86400
    assert c.ttl_s["bbm_day"] == 3 * 3600 and c.ttl_s["bbm_ros"] == 12 * 3600

def test_never_drop_merges_global_and_league(home):
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(
        '[global]\nnever_drop=["Victor Wembanyama"]\n'
        '[league."123"]\nnever_drop=["Tyrese Haliburton"]\nstream_slots=5\n')
    c = load_config()
    assert c.never_drop_for("123") == {"victor wembanyama", "tyrese haliburton"}
    assert c.never_drop_for("999") == {"victor wembanyama"}
    assert c.leagues["123"].stream_slots == 5

def test_paths_respect_env(home):
    assert paths.cache_db().is_relative_to(home)
```

- [ ] **Step 3:** Run `uv run pytest tests/test_config.py -v` → FAIL (import error).
- [ ] **Step 4:** Implement `paths.py` and `config.py` (stdlib `tomllib`).
- [ ] **Step 5:** Run `uv run pytest tests/test_config.py -v` → PASS.
- [ ] **Step 6:** Commit `feat: scaffold, paths and config` (include the `docs/` spec).

---

### Task 2: Models and category math

**Files:**
- Create: `src/h2h_advisor/models.py`, `src/h2h_advisor/analysis/__init__.py`, `src/h2h_advisor/analysis/categories.py`
- Test: `tests/analysis/test_categories.py`

**Interfaces:**
- Produces (`models.py`, frozen dataclasses):
  - `class Category(StrEnum): FG_PCT, FT_PCT, TPM, PTS, REB, AST, STL, BLK, TO`; `NINE_CAT: tuple[Category, ...]`
  - `StatLine(fgm=0., fga=0., ftm=0., fta=0., tpm=0., pts=0., reb=0., ast=0., stl=0., blk=0., to=0.)` with `__add__` and `scale(k: float) -> StatLine`
  - `Player(key: str, name: str, nba_team: str, positions: tuple[str,...], status: str|None=None, yahoo_id: str|None=None)`
  - `RosterEntry(player: Player, slot: str)`  — slot e.g. `PG, SG, G, SF, PF, F, C, Util, BN, IL, IL+`
  - `LeagueSettings(league_id, name, categories: tuple[Category,...], roster_slots: dict[str,int], max_adds_per_week: int, week_start: date, week_end: date, current_week: int, lineup_effective: Literal["today","tomorrow"])`
  - `Matchup(league_id, week, opponent_team_id, opponent_name, my_totals: StatLine, opp_totals: StatLine, moves_used: int)`
  - `FreeAgent(player: Player, on_waivers: bool, waiver_clear: date|None=None)`
  - `Projection(player_key: str, name: str, nba_team: str, per_game: StatLine, value: float|None)`
- Produces (`categories.py`):
  - `category_value(s: StatLine, c: Category) -> float` (FG_PCT = fgm/fga, 0.0 when fga == 0)
  - `better(c: Category, a: float, b: float) -> int` (1 if a wins, -1 if b, 0 tie; TO lower wins)

- [ ] **Step 1: Write failing tests**

```python
def test_fg_pct_from_makes_attempts():
    s = StatLine(fgm=5, fga=10) + StatLine(fgm=1, fga=10)
    assert category_value(s, Category.FG_PCT) == pytest.approx(0.30)  # not mean(0.5, 0.1)

def test_zero_attempts_is_zero():
    assert category_value(StatLine(), Category.FT_PCT) == 0.0

def test_turnovers_lower_wins():
    assert better(Category.TO, 10, 12) == 1 and better(Category.PTS, 10, 12) == -1
    assert better(Category.REB, 5, 5) == 0
```

- [ ] **Step 2:** Run `uv run pytest tests/analysis/test_categories.py -v` → FAIL.
- [ ] **Step 3:** Implement models and `categories.py`.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: domain models and 9-cat category math`.

---

### Task 3: SQLite cache, snapshots, request log

**Files:**
- Create: `src/h2h_advisor/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Produces:
  - `@dataclass CachedItem(key: str, value: Any, fetched_at: datetime)`
  - `class Cache(path: Path)`:
    - `get(key: str) -> CachedItem|None`, `put(key: str, value: Any, *, raw: str|bytes|None=None, page_type: str|None=None, now: datetime|None=None) -> None` (value JSON-serialised; dataclasses via `dataclasses.asdict` + `date`/`datetime` isoformat)
    - `is_fresh(item: CachedItem, ttl_s: int, now: datetime) -> bool`
    - `snapshots(page_type: str) -> list[tuple[datetime, bytes]]` — keeps last 5 per page_type
    - `log_request(site: str, method: str, url: str, verdict: str, reason: str, now: datetime|None=None) -> None`
    - `page_loads(site: str, day: date) -> int` (counts rows with verdict `"load"`)
    - `last_load_at(site: str) -> datetime|None`
    - `recent_blocked(limit: int=20) -> list[dict]`
  - WAL mode; safe for two processes.

- [ ] **Step 1: Write failing tests** — `test_put_get_roundtrip_dataclass`, `test_is_fresh_respects_ttl` (put at t0, fresh at t0+1199 s with ttl 1200, stale at t0+1201), `test_snapshots_keep_last_five` (put 7 with page_type `"matchup"` → 5 newest), `test_page_loads_counts_per_site_per_day` (3 yahoo loads today, 1 yesterday, 2 bbm → `page_loads("yahoo", today) == 3`), `test_two_connections_share_counts` (two `Cache` objects on same path see each other's logs).
- [ ] **Step 2:** Run `uv run pytest tests/test_cache.py -v` → FAIL.
- [ ] **Step 3:** Implement with stdlib `sqlite3`, tables `items`, `snapshots`, `requests`.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: sqlite cache and request log`.

---

### Task 4: Read-only guard decision function

**Files:**
- Create: `src/h2h_advisor/browser/__init__.py`, `src/h2h_advisor/browser/guard.py`, `src/h2h_advisor/browser/guard_rules.toml`
- Test: `tests/browser/test_guard.py`

**Interfaces:**
- Produces:
  - `@dataclass RequestInfo(method: str, url: str, resource_type: str, is_navigation: bool, post_data: str|None=None)`
  - `@dataclass Verdict(allowed: bool, reason: str)`
  - `@dataclass GuardRules(...)` with `GuardRules.load(path: Path|None=None) -> GuardRules` (default: packaged `guard_rules.toml`)
  - `decide(req: RequestInfo, rules: GuardRules) -> Verdict` — pure; FETCH-only (there is no login mode: login runs in a plain Chrome outside Playwright, spec §13).
  - `site_of(url: str) -> Literal["yahoo","bbm"]|None`
- `guard_rules.toml` keys (initial values; Task 12 finalises):
  - `[hosts] yahoo = ["basketball.fantasysports.yahoo.com", "s.yimg.com"]`, `bbm = ["basketballmonster.com", "www.basketballmonster.com"]`, `login = ["login.yahoo.com", "api.login.yahoo.com"]`
  - `[yahoo] action_patterns = ["/addplayer", "/dropplayer", "/trade", "/claim", "/waiver", "editroster", "/editsettings", "/message", "/transactions/add"]`, `post_allow = []` (list of `{url_prefix, operation}`)
  - `[bbm] deny_path_patterns = ["/account", "/settings", "/leaguesettings", "/subscribe", "/payment", "/login", "/logout"]`, `post_pages = []`, `post_event_targets = []`, `export_selectors = []` (exposed as `rules.bbm.export_selectors`)
  - `[resources] block = ["image", "media", "font"]`
- Rules (in order): foreign host → deny; blocked resource type → deny; Yahoo action pattern (any method) → deny; BBM deny path (incl. `/login`, `/logout`) → deny; `login` hosts → deny (only recognised, so a redirect there means NeedsLogin); Yahoo non-GET → allow only if URL prefix + operation (from JSON `operationName` in post_data) in `post_allow`, else deny; BBM POST → allow only if path in `post_pages` and `__EVENTTARGET` (form-decoded post_data) in `post_event_targets` or the submit field name in `post_event_targets`, else deny; GET navigations on allowed hosts pass (page patterns may be added in Task 12).

- [ ] **Step 1: Write failing tests** (one function each; build `GuardRules` from a test TOML string with `post_pages=["/dailyprojections.aspx"]`, `post_event_targets=["ctl00$Main$DateDDL","ctl00$Main$ExportButton"]`, `post_allow=[{url_prefix="https://basketball.fantasysports.yahoo.com/api/", operation="GetMatchup"}]`):
  - `test_yahoo_post_denied` — POST to `https://basketball.fantasysports.yahoo.com/nba/123/` → `allowed is False`
  - `test_yahoo_add_drop_get_denied` — GET `.../nba/123/addplayer?apid=1` and `.../dropplayer` → denied
  - `test_yahoo_allowlisted_read_post_allowed` — POST `.../api/x` with `{"operationName":"GetMatchup"}` → allowed; same URL with `"AddPlayer"` → denied
  - `test_bbm_post_unknown_target_denied` — POST `/dailyprojections.aspx` with `__EVENTTARGET=ctl00$Main$SaveSettings` → denied
  - `test_bbm_post_allowlisted_target_allowed` — `__EVENTTARGET=ctl00$Main$DateDDL` → allowed
  - `test_bbm_post_other_page_denied` — allowlisted target but `/playerrankings.aspx` → denied
  - `test_bbm_settings_pages_denied_even_get` — GET `/settings/leaguesettings.aspx`, `/account.aspx` → denied
  - `test_foreign_host_denied` — GET `https://www.google-analytics.com/collect` → denied
  - `test_login_hosts_denied` — GET and POST `https://login.yahoo.com/` denied
  - `test_images_blocked` — resource_type `image` on yahoo host → denied
- [ ] **Step 2:** Run `uv run pytest tests/browser/test_guard.py -v` → FAIL.
- [ ] **Step 3:** Implement `decide` and `GuardRules.load` (urllib.parse for host/path/form decoding; `json.loads` guarded for Yahoo post_data).
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: read-only request guard`.

---

### Task 5: Rate limiter

**Files:**
- Create: `src/h2h_advisor/browser/ratelimit.py`
- Test: `tests/browser/test_ratelimit.py`

**Interfaces:**
- Consumes: `Cache.page_loads`, `Cache.last_load_at`, `Cache.log_request` (Task 3); `Limits` (Task 1)
- Produces:
  - `class DailyCapReached(Exception)` with `.site`, `.cap`
  - `class RateLimiter(cache: Cache, limits: Limits, *, clock: Callable[[], datetime]=..., sleep: Callable[[float], None]=time.sleep, rng: random.Random|None=None)`
    - `acquire(site: str) -> None` — raises `DailyCapReached` if `page_loads(site, today) >= cap`; else sleeps `max(0, delay - elapsed_since_last_load)` where `delay = rng.uniform(delay_min_s, delay_max_s)`.
    - `record_load(site: str, url: str) -> None` — `log_request(site, "GET"|..., url, "load", "")`.
  - "today" is the local date of `clock()`.

- [ ] **Step 1: Write failing tests** — fake clock + recording `sleep`:
  - `test_first_load_no_sleep`
  - `test_sleeps_remaining_gap` (last load 2 s ago, rng fixed so delay=6 → sleeps 4.0)
  - `test_no_sleep_when_gap_elapsed` (last load 20 s ago → no sleep)
  - `test_daily_cap_raises` (250 yahoo loads today → `DailyCapReached`; bbm still fine)
  - `test_cap_shared_across_instances` (two limiters on same cache file)
- [ ] **Step 2:** Run `uv run pytest tests/browser/test_ratelimit.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: sqlite-backed per-site rate limiter`.

---

### Task 6: Browser session, profile lock, auth detection, CLI login/discover

**Files:**
- Create: `src/h2h_advisor/browser/session.py`, `src/h2h_advisor/cli.py`
- Test: `tests/browser/test_session.py`, `tests/browser/test_session_playwright.py`

**Interfaces:**
- Consumes: `decide`, `GuardRules`, `Mode`, `RequestInfo`, `site_of` (Task 4); `RateLimiter` (Task 5); `Cache` (Task 3)
- Produces:
  - `class BrowserBusy(Exception)`; `class NeedsLogin(Exception)` with `.site`, `.reason`
  - `class ProfileLock(profile_dir: Path)` context manager using `fcntl.flock(LOCK_EX | LOCK_NB)` on `profile_dir / ".h2h.lock"`; raises `BrowserBusy`.
  - `detect_auth_problem(site: str, final_url: str, html: str) -> str|None` — returns reason when final host is a login host, BBM path contains `/login`, or html contains captcha markers (`"captcha"`, `"verify you are human"`, case-insensitive) or BBM's "Available with a membership" on a projections page (logged out).
  - `@dataclass PageResult(url: str, final_url: str, html: str, fetched_at: datetime)`
  - `class BrowserSession(cache: Cache, limiter: RateLimiter, rules: GuardRules, headless: bool=False)` — context manager; acquires `ProfileLock`, launches `chromium.launch_persistent_context(paths.profile_dir(), channel="chrome", headless=headless, ignore_default_args=["--use-mock-keychain"])` (real keychain, so cookies saved by the plain-Chrome login decrypt) with one page; routes every request through `decide` (abort + `log_request(..., "blocked", reason)` on deny).
    - `fetch(site: str, url: str) -> PageResult` — `limiter.acquire`, `page.goto(url, wait_until="domcontentloaded")`, `record_load`, `detect_auth_problem` → raise `NeedsLogin`; on HTTP 429/403/5xx: one retry after 30 s, then raise `FetchFailed(site, status)`.
    - `postback(site: str, url: str, event_target: str, select: dict[str,str]|None=None) -> PageResult` — sets `<select>` values via `page.select_option` then triggers `__doPostBack(event_target, "")` via `page.evaluate`; guard still applies.
    - `download(site: str, url: str, trigger_selector: str) -> bytes` — `page.expect_download()` around `page.click(trigger_selector)`; selector must be in `rules.bbm.export_selectors` (raise `ValueError` otherwise).
  - `cli.main(argv: list[str]|None=None) -> int` with subcommands: `serve` (Task 16 wires it), `login` (takes the `ProfileLock`, starts a plain Google Chrome process — no Playwright, no automation/remote-debugging flags — on the profile with the Yahoo fantasy + BBM login pages, waits for Enter in terminal; spec §13), `discover <url>` (headed, guarded Playwright FETCH session, opens url, saves `page.content()` to `tests/fixtures/raw/<timestamp>.html` on Enter, prints blocked requests). `discover` is for Task 12 only.
- Headed (minimised window) by default; `--headless` flag for experiments.

- [ ] **Step 1: Write failing unit tests (`test_session.py`)**
  - `test_profile_lock_second_holder_busy` — hold lock in context, second `ProfileLock(...).__enter__()` raises `BrowserBusy`
  - `test_detect_login_redirect` — `detect_auth_problem("yahoo", "https://login.yahoo.com/?done=..", "")` is not None
  - `test_detect_captcha` and `test_detect_bbm_logged_out_projections` (html contains `Available with a membership`, url `/dailyprojections.aspx`)
  - `test_clean_page_ok` → None
- [ ] **Step 2: Write failing integration test (`test_session_playwright.py`, marked `@pytest.mark.browser`)** — local `http.server` on 127.0.0.1 serving a page whose inline script does `fetch("/x", {method:"POST"})` on load; test rules list `127.0.0.1` as a yahoo host. Assert the server received zero POSTs and `cache.recent_blocked()` contains the POST. Use `headless=True` and a tmp profile.
- [ ] **Step 3:** Run `uv run pytest tests/browser/test_session.py tests/browser/test_session_playwright.py -v` → FAIL (`uv run playwright install chrome` first if needed).
- [ ] **Step 4:** Implement `session.py` and `cli.py` (`argparse`).
- [ ] **Step 5:** Run same → PASS.
- [ ] **Step 6:** Commit `feat: guarded browser session, profile lock, login/discover cli`.

---

### Task 7: NBA schedule source

**Files:**
- Create: `src/h2h_advisor/sources/__init__.py`, `src/h2h_advisor/sources/schedule.py`
- Test: `tests/sources/test_schedule.py`, `tests/fixtures/schedule_small.json`

**Interfaces:**
- Produces:
  - `SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"`
  - `Schedule = dict[date, frozenset[str]]` (NBA tricodes playing that day, US Eastern date)
  - `parse_schedule(data: dict) -> Schedule` — walks `leagueSchedule.gameDates[].games[]`, uses `gameDateEst` date part and `homeTeam.teamTricode`/`awayTeam.teamTricode`; skips preseason (`gameId` not starting with `"002"`/`"004"`).
  - `games_between(s: Schedule, team: str, start: date, end: date) -> list[date]` (inclusive)
  - `fetch_schedule(client: httpx.Client) -> Schedule` (plain HTTPS, not the browser)

- [ ] **Step 1:** Create a 3-date fixture by hand in the real JSON shape (one preseason game `001...` to be skipped). Tests: `test_parse_groups_by_date`, `test_skips_preseason`, `test_games_between_inclusive`.
- [ ] **Step 2:** Run `uv run pytest tests/sources/test_schedule.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: nba schedule source`.

---

### Task 8: Player identity

**Files:**
- Create: `src/h2h_advisor/identity.py`
- Modify: `src/h2h_advisor/config.py` (use `normalize_name` in `never_drop_for`)
- Test: `tests/test_identity.py`

**Interfaces:**
- Produces:
  - `normalize_name(name: str) -> str` — NFKD strip accents, casefold, drop `.`/`'`/`-`→space, drop suffixes `jr sr ii iii iv`, collapse spaces.
  - `TEAM_ALIASES: dict[str, str]` mapping site codes to NBA tricodes (`GS→GSW, NY→NYK, SA→SAS, NO→NOP, PHO→PHX, UTAH→UTA, WSH→WAS, BKN/BRK→BKN, CHA/CHO→CHA`); Task 12 adds any others seen.
  - `team_code(code: str) -> str`
  - `player_key(name: str, team: str) -> str` = `f"{normalize_name(name)}|{team_code(team)}"`
  - `class PlayerIndex(aliases: dict[str,str])` — `aliases` maps normalised alt name → normalised canonical; `key(name: str, team: str) -> str`; `match(name: str, team: str, candidates: dict[str, T]) -> T|None` tries exact key, then alias, then name-only if unique (handles mid-week trades).

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("a,b", [("Nikola Jokić","Nikola Jokic"), ("Jaren Jackson Jr.","Jaren Jackson"),
    ("Luka Dončić","luka doncic"), ("Shai Gilgeous-Alexander","Shai Gilgeous Alexander"), ("De'Aaron Fox","DeAaron Fox")])
def test_normalize_equivalents(a, b): assert normalize_name(a) == normalize_name(b)

def test_team_aliases(): assert player_key("X Y", "GS") == player_key("X Y", "GSW")

def test_name_only_fallback_when_unique():
    idx = PlayerIndex({})
    assert idx.match("Dennis Schroder", "LAL", {player_key("Dennis Schröder","BKN"): 1}) == 1

def test_alias_table():
    idx = PlayerIndex({"herb jones": "herbert jones"})
    assert idx.match("Herb Jones", "NOP", {player_key("Herbert Jones","NOP"): 7}) == 7
```

- [ ] **Step 2:** Run `uv run pytest tests/test_identity.py tests/test_config.py -v` → FAIL.
- [ ] **Step 3:** Implement; switch `Config.never_drop_for` to `normalize_name`.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: player identity matching`.

---

### Task 9: Week projection with daily active slots

**Files:**
- Create: `src/h2h_advisor/analysis/projection.py`
- Test: `tests/analysis/test_projection.py`

**Interfaces:**
- Consumes: models (Task 2), `Schedule` (Task 7)
- Produces:
  - `INACTIVE_SLOTS = {"IL", "IL+"}`; `OUT_STATUSES = {"O", "INJ", "SUSP", "NA"}`
  - `@dataclass DayPlayer(key: str, positions: tuple[str,...], per_game: StatLine, value: float)`
  - `assign_active(players: list[DayPlayer], slots: dict[str,int]) -> list[DayPlayer]` — only active slots (exclude `BN`, `IL`, `IL+`); position eligibility: `G`←PG/SG, `F`←SF/PF, `Util`←any. Greedy: sort players by `value` desc; for each, fill the most specific eligible open slot first (exact pos → G/F → Util).
  - `lineup_by_day(roster: list[RosterEntry], per_game: dict[str, StatLine], values: dict[str, float], schedule: Schedule, days: list[date], slots: dict[str,int], active_days: dict[str, set[date]]|None=None) -> dict[date, dict[str, str]]` — day → {player key → active slot filled}; same eligibility/greedy rules as `assign_active` (which it uses, returning the slot too). `project_week` is implemented on top of it.
  - `project_week(current: StatLine, roster: list[RosterEntry], per_game: dict[str, StatLine], values: dict[str, float], schedule: Schedule, days: list[date], slots: dict[str,int], active_days: dict[str, set[date]]|None=None) -> StatLine` — `active_days`, when given, restricts a player key to those days (keys absent from it are unrestricted). For each day: players with a game, not in `INACTIVE_SLOTS`, status not in `OUT_STATUSES`, with a projection → `assign_active` → sum `per_game`. Returns `current + Σ`.

- [ ] **Step 1: Write failing tests**
  - `test_adds_remaining_games_to_current` — 1 player, 2 games in `days`, per-game `pts=20` → current `pts=50` becomes `90`
  - `test_slot_cap_keeps_best` — 3 C-only players with games, slots `{"C":1,"Util":1}` → only 2 highest `value` counted
  - `test_il_and_out_players_contribute_zero` (Review Focus 3) — player in slot `IL` and player with status `O` → zero added
  - `test_guard_fills_g_slot` — PG, PG with slots `{"PG":1,"G":1}` → both counted
  - `test_percentages_stay_aggregated` — result FG% equals total fgm/fga
  - `test_active_days_restricts_player` — player with games on d1,d2 and `active_days={key:{d2}}` → only d2 counted
  - `test_lineup_by_day_reports_slots` — PF/C-heavy roster with slots `{"PG":1,"C":1,"Util":1}`: a C candidate gets no slot on a day where C and Util are filled; a PG candidate gets `"PG"`
- [ ] **Step 2:** Run `uv run pytest tests/analysis/test_projection.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: week projection with daily active slots`.

---

### Task 10: Win probability and week outlook

**Files:**
- Create: `src/h2h_advisor/analysis/outlook.py`
- Test: `tests/analysis/test_outlook.py`

**Interfaces:**
- Consumes: Tasks 2, 7, 9
- Produces:
  - `SD_PER_PLAYER_GAME = {PTS: 7.0, REB: 3.0, AST: 2.2, STL: 0.9, BLK: 0.8, TPM: 1.3, TO: 1.3}`
  - `category_win_prob(c: Category, my_proj: StatLine, opp_proj: StatLine, remaining_player_games: float) -> float` — counting cats: `margin = value(my) - value(opp)` (negated for TO), `sd = SD[c] * sqrt(remaining_player_games)`; percentages: `sd = sqrt(p*(1-p)/fga_my_remaining + p*(1-p)/fga_opp_remaining)` approximated with `p` = combined pct and remaining attempts = projected minus current attempts. If `sd == 0`: return `1.0`/`0.0`/`0.5` by `better`. Else `Φ(margin/sd)` (`statistics.NormalDist`).
  - `@dataclass CatOutlook(category, my_now, opp_now, my_proj, opp_proj, win_prob, status: Literal["locked_win","locked_loss","likely_win","likely_loss","swing","tied"])` — `locked_*` when no remaining games either side; `likely_win` ≥ 0.90, `likely_loss` ≤ 0.10.
  - `@dataclass WeekContext(settings: LeagueSettings, matchup: Matchup, my_roster: list[RosterEntry], opp_roster: list[RosterEntry], per_game: dict[str,StatLine], values: dict[str,float], ros_values: dict[str,float], schedule: Schedule, today: date, active_days: dict[str, set[date]]|None=None)` (passed through to `project_week` for my side)
  - `remaining_days(ctx: WeekContext) -> list[date]` — `today .. settings.week_end` inclusive (never assume Mon–Sun).
  - `@dataclass WeekAnalysis(categories: list[CatOutlook], expected_cats_won: float, my_proj: StatLine, opp_proj: StatLine, adds_left: int, days_left: list[date])`
  - `analyze_week(ctx: WeekContext) -> WeekAnalysis` — `adds_left = max(0, settings.max_adds_per_week - matchup.moves_used)`.

- [ ] **Step 1: Write failing tests**
  - `test_locked_when_no_games_left` — days empty, my PTS > opp → `win_prob == 1.0`, status `locked_win`
  - `test_even_margin_is_half` — equal projections with games left → `0.5 ± 1e-9`
  - `test_turnovers_inverted` — fewer TO → `win_prob > 0.5`
  - `test_pct_uses_attempts` — large lead on tiny remaining attempts → `> 0.95`
  - `test_remaining_days_follow_settings_week` (Review Focus 4) — `week_start=2027-02-09`, `week_end=2027-02-22` (All-Star 2-week), `today=2027-02-18` → 5 days ending 2027-02-22
  - `test_adds_left_from_settings` — `max_adds_per_week=7`, `moves_used=3` → `adds_left == 4`
- [ ] **Step 2:** Run `uv run pytest tests/analysis/test_outlook.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: category win probability and week outlook`.

---

### Task 10b: League build and category weights

**Files:**
- Create: `src/h2h_advisor/analysis/build.py`
- Modify: `src/h2h_advisor/models.py` (`Projection.cat_values: dict[Category, float] = field(default_factory=dict)`), `src/h2h_advisor/config.py` (`LeagueConfig.build: dict[str, list[str]]|None = None` parsed from `build = { strong = [...], punt = [...], weak = [...] }`; `Config.light_day_max_games: int = 4`; also: reject `never_drop` given as a string with a clear `ValueError` naming the file, read TOML as UTF-8 and wrap `TOMLDecodeError` with the path — parked Task 1 minors)
- Test: `tests/analysis/test_build.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Category`, `Matchup`, `StatLine`, `better`, `category_value` (Task 2); `CatOutlook` (Task 10)
- Produces:
  - `@dataclass(frozen=True) Build(strong: frozenset[Category]=frozenset(), punt: frozenset[Category]=frozenset(), weak: frozenset[Category]=frozenset(), source: Literal["pinned","inferred","none"]="none")`
  - `build_from_config(raw: dict[str, list[str]]|None) -> Build|None` — names are `Category` values (e.g. `"FG_PCT"`); unknown name → `ValueError`.
  - `matchup_result(m: Matchup, cats: tuple[Category,...]) -> dict[Category, int]` — 1 win / 0 tie / -1 loss for Jakub using `better`.
  - `infer_build(roster_cat_values: dict[Category, float], history: list[dict[Category, int]]) -> Build` — per spec §10.2: win rate (wins / weeks, ties count 0.5) ≥ 0.75 over ≥ 3 weeks → strong; ≤ 0.2 over ≥ 3 weeks → punt; with < 3 weeks, the top third of categories by roster value → strong; else neutral. `source="inferred"`.
  - `resolve_build(pinned: Build|None, inferred: Build) -> Build` — pinned wins whole.
  - `category_weights(build: Build, outlook: list[CatOutlook]) -> dict[Category, float]` — punt → 0.0; strong with status `likely_win`/`locked_win` → 0.0; else 1.0.
  - `at_risk_strong(build: Build, outlook: list[CatOutlook]) -> list[Category]` — strong categories whose status is `swing`/`likely_loss`/`locked_loss`/`tied` (the "weekly override" flag of spec §10.1.5).

- [ ] **Step 1: Write failing tests**
  - `test_build_from_config_pinned_league` — `{"punt":["FG_PCT","TO","BLK"],"weak":["REB"],"strong":["FT_PCT","TPM","PTS","AST","STL"]}` → those sets, `source=="pinned"`; `{"punt":["FG%"]}` → `ValueError`
  - `test_matchup_result_turnovers_inverted`
  - `test_infer_from_history` — 4 weeks always winning BLK, always losing FT_PCT → BLK strong, FT_PCT punt
  - `test_infer_short_history_uses_roster_values` — 1 week → top 3 of 9 by value strong, no punts
  - `test_pinned_overrides_inferred`
  - `test_weights_zero_for_punt_and_safe_strong` — BLK strong + likely_win → 0; BLK strong + swing → 1 and listed by `at_risk_strong`; FT_PCT punt → 0 regardless
  - config: `test_build_parsed_per_league`, `test_never_drop_string_rejected`, `test_bad_toml_error_names_file`
- [ ] **Step 2:** Run `uv run pytest tests/analysis/test_build.py tests/test_config.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: league build inference and category weights`.

---

### Task 11: Drop pool and streamer ranking

**Files:**
- Create: `src/h2h_advisor/analysis/streamers.py`
- Test: `tests/analysis/test_streamers.py`

**Interfaces:**
- Consumes: Tasks 2, 9, 10; `Config.never_drop_for`, `LeagueConfig.stream_slots`, `Config.drop_margin` (Tasks 1, 8)
- Produces:
  - `drop_pool(roster: list[RosterEntry], ros_values: dict[str,float], free_agents: list[FreeAgent], never_drop: set[str], margin: float, stream_slots: int|None) -> list[RosterEntry]` — exclude never-drop (by normalised name) and IL/IL+ slots. If `stream_slots`: bottom N by ROS value. Else: players with `ros_value <= best_fa_ros + margin` where `best_fa_ros` = max ROS value among free agents not on waivers; minimum 1 (lowest remaining) if any eligible. Players with no ROS value are treated as `-inf` (droppable).
  - `@dataclass StreamerOption(add: Player, drop: Player, start_days: list[date], usable_days: list[date], slot_by_day: dict[date, str], raw_games: int, light_days: list[date], delta_expected_cats: float, per_cat_delta: dict[Category,float], adds_left_after: int, follow_up: "StreamerOption|None")` — `usable_days` = horizon days where the add actually gets an active slot (from `lineup_by_day`, Task 9); `light_days` = usable days with ≤ `light_day_max_games` NBA games in the schedule.
  - Scoring is build-weighted: `delta = Σ_c weight[c] × (new_win_prob[c] − base_win_prob[c])` with `weight = category_weights(build, base_outlook)` (Task 10b); `per_cat_delta` reports unweighted deltas. `rank_streamers` takes `build: Build` and `light_day_max_games: int=4`. Candidates with zero usable days are excluded (position fit).
  - `@dataclass LongTermUpgrade(add: Player, drop: Player, ros_value_gain: float, per_cat_ros_delta: dict[Category,float], usable_days_this_week: list[date])`
  - `long_term_upgrades(ctx: WeekContext, free_agents: list[FreeAgent], pool: list[RosterEntry], ros_cat_values: dict[str, dict[Category,float]], build: Build, margin: float, max_results: int=5) -> list[LongTermUpgrade]` — build-weighted ROS value = Σ over non-punt categories of `ros_cat_values[key][c]`; include FAs (not on waivers) whose weighted value exceeds the weakest pool player's by ≥ `margin`; pair with that weakest player; sort by gain desc.
  - `effective_start(settings: LeagueSettings, today: date) -> date` — `today` or `today+1` when `lineup_effective == "tomorrow"`.
  - `rank_streamers(ctx: WeekContext, free_agents: list[FreeAgent], pool: list[RosterEntry], horizon: Literal["today","rest_of_week"], max_results: int=10) -> tuple[list[StreamerOption], str|None]` — returns `([], reason)` when `adds_left == 0`, pool empty, or no eligible FA.
    - Horizon days: `today` → `[effective_start]` (if within week); `rest_of_week` → `effective_start .. week_end`.
    - Waivers: excluded from `today`; in `rest_of_week` contribute from `waiver_clear` if set, else excluded.
    - Candidate set: top 25 FAs by `values` (horizon projection value) with ≥1 game in horizon days.
    - Score for (add, drop): rebuild roster with drop removed from `effective_start` through `week_end` and add present only on horizon days; implemented by `dataclasses.replace(ctx, my_roster=..., active_days=...)` and `analyze_week(...)`; `delta = new.expected_cats_won - base.expected_cats_won`.
    - Follow-up (rest_of_week only, `adds_left_after ≥ 1`): for the top 5 options, try the top 5 other FAs over days after the add's last game in horizon, replacing the add; attach the best positive one as `follow_up` and rank by `delta + follow_up.delta`.
    - Sort desc; return `max_results`.

- [ ] **Step 1: Write failing tests** (small fixture builder `make_ctx(...)` in the test file)
  - `test_never_drop_excluded` — Wembanyama lowest ROS but in never-drop → not in pool
  - `test_pool_size_adapts` — deep roster (all ROS ≫ best FA) → pool size 1; churn roster (5 near FA level) → 5
  - `test_stream_slots_override` → exactly N lowest
  - `test_il_players_not_in_pool` (Review Focus 3)
  - `test_no_adds_left_returns_reason` — `([], "no adds left this week")`
  - `test_more_games_wins_when_cats_are_close` — two FAs equal per-game, one with 3 games vs 1 → 3-game first
  - `test_tomorrow_lineup_shifts_start` (Review Focus 1) — `lineup_effective="tomorrow"`, FA plays only today → excluded; FA plays tomorrow → `start_days == [today+1]`
  - `test_waiver_player_excluded_today` (Review Focus 2) — `on_waivers=True` → not in `today` results; with `waiver_clear=today+2` in `rest_of_week` → `start_days` all ≥ today+2
  - `test_follow_up_attached_when_adds_remain` — add's games end mid-week, second FA plays later → `follow_up is not None`
  - `test_position_fit_excludes_unusable_big` — roster full of PF/C with only guard slots open on every horizon day → C free agent excluded; PG free agent included with `slot_by_day` values `"PG"`/`"G"`
  - `test_usable_vs_raw_games` — FA with 4 games, 2 on days with no eligible open slot → `raw_games == 4`, `len(usable_days) == 2`
  - `test_light_days_flagged` — usable day with 3 NBA games listed in `light_days`; day with 10 games not
  - `test_punted_category_gives_no_credit` — FA who only adds BLK ranks below FA who adds AST when BLK is punted
  - `test_long_term_upgrade_listed_with_one_game` — FA with ROS weighted value ≫ weakest pool player and 1 usable game → in `long_term_upgrades` with `usable_days_this_week` of length 1
  - `test_long_term_ignores_punted_value` — FA whose ROS value is all in a punted category → not listed
- [ ] **Step 2:** Run `uv run pytest tests/analysis/test_streamers.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: adaptive drop pool and streamer ranking`.

---

### Task 12: Logged-in discovery session (with Jakub — CHECKPOINT)

Parsers (Tasks 13–14) cannot be written until real logged-in pages are seen. This task needs Jakub at the keyboard; stop and ask him before starting.

**Files:**
- Create: `docs/discovery.md`, `tests/fixtures/yahoo/*.html`, `tests/fixtures/bbm/*` (html and/or xlsx), `src/h2h_advisor/sources/yahoo/urls.py`, `src/h2h_advisor/sources/bbm/urls.py`
- Modify: `src/h2h_advisor/browser/guard_rules.toml`, `src/h2h_advisor/identity.py` (`TEAM_ALIASES` additions), `tests/browser/test_guard.py`

**Interfaces:**
- Produces:
  - `yahoo/urls.py`: `home() -> str`, `league_settings(league_id) -> str`, `matchup(league_id, week: int|None) -> str`, `team(league_id, team_id) -> str`, `free_agents(league_id, start: int=0, position: str|None=None) -> str`
  - `bbm/urls.py`: `PROJECTIONS_PAGE: dict[Literal["day","week","ros"], str]`, `DATE_CONTROL`, `PERIOD_CONTROL`, `EXPORT_SELECTOR` constants (exact values from discovery); `league_page(...)` only if the league-sync availability page proves useful.
  - `docs/discovery.md`: for each page: URL pattern, request method(s), which elements hold the data, pagination, how current week/opponent/team id appear, moves-used indicator, whether BBM export includes makes/attempts, whether BBM league sync gives availability for all players in one page, Yahoo/BBM team codes seen, any read-only Yahoo POST/GraphQL calls (endpoint + operationName).

- [ ] **Step 1:** Ask Jakub to run `uv run h2h-advisor login` and log into Yahoo and BBM in the dedicated window.
- [ ] **Step 2:** For each page in `docs/discovery.md`'s list, run `uv run h2h-advisor discover <url>` (guard ON), let Jakub navigate inside the window if needed (only the read-only controls: period, date, Apply, Export), save the page. Keep pace manual (≥10 s between pages); total ≤ 40 Yahoo and ≤ 25 BBM loads for the whole session.
- [ ] **Step 3:** Inspect the request log (`cache.recent_blocked()` and allowed POSTs). Record any read-only Yahoo POSTs and BBM postback targets the pages need; review each with Jakub before allowlisting.
- [ ] **Step 4:** Sanitise fixtures: strip cookies, `crumb`/CSRF tokens, `__VIEWSTATE` values (replace with `"X"`), email addresses; keep player/stat content. Move from `tests/fixtures/raw/` to `tests/fixtures/{yahoo,bbm}/<page>.html`. Delete `raw/`.
- [ ] **Step 5:** Update `guard_rules.toml` (hosts actually needed, `post_allow`, `post_pages`, `post_event_targets`, `export_selectors`, navigation page patterns) and add a test per new allow entry plus a denying twin (same endpoint/page, different operation/target).
- [ ] **Step 6:** Run `uv run pytest tests/browser/test_guard.py -v` → PASS.
- [ ] **Step 7:** Write `urls.py` files and `docs/discovery.md`; show `docs/discovery.md` to Jakub.
- [ ] **Step 8:** Commit `docs: discovery map, fixtures, finalised guard rules`.

---

### Task 13: Yahoo parsers and source

**Files:**
- Create: `src/h2h_advisor/sources/yahoo/__init__.py`, `parsers.py`, `source.py`
- Test: `tests/sources/yahoo/test_parsers.py`, `tests/sources/yahoo/test_source.py`

**Interfaces:**
- Consumes: fixtures and `yahoo/urls.py` (Task 12); `BrowserSession.fetch` (Task 6); models (Task 2); `PlayerIndex.key` (Task 8)
- Produces (`parsers.py`, all pure, selectolax):
  - `@dataclass LeagueRef(league_id: str, name: str, team_id: str, team_name: str)`
  - `parse_league_list(html: str) -> list[LeagueRef]` (NBA leagues only)
  - `parse_settings(html: str, league_id: str) -> LeagueSettings`
  - `parse_matchup(html: str, league_id: str) -> Matchup`
  - `parse_roster(html: str) -> list[RosterEntry]`
  - `parse_free_agents(html: str) -> list[FreeAgent]`
  - Every parser raises `ParseError(page, detail)` when an expected element is missing (layout change) — never returns partial data silently.
- Produces (`source.py`): `class YahooSource(session: BrowserSession, index: PlayerIndex)` with `leagues()`, `settings(league_id)`, `matchup(league_id, week=None)`, `roster(league_id, team_id)`, `free_agents(league_id, limit=50, position=None)`, `history(league_id, weeks: list[int]) -> list[Matchup]` (one matchup page per week, same parser) returning the parser types; `free_agents` pages at Yahoo's page size until `limit` reached, never more than `ceil(limit/25)` pages.

- [ ] **Step 1: Write failing parser tests** — one test per parser against its fixture, asserting the concrete values visible in that saved page (league count/names, `max_adds_per_week` = the value shown on the settings page (5 or 7), the 9 categories, `lineup_effective`, week dates, matchup FGM/FGA and PTS for both sides, `moves_used`, roster size and an IL-slotted player's slot, a waiver-flagged FA). Plus `test_parse_error_on_layout_change` — feed `"<html></html>"` to each parser → `ParseError`.
- [ ] **Step 2:** Write failing `test_source.py` with a fake session returning fixtures by URL: `test_free_agents_page_cap` (limit=50 → exactly 2 fetches).
- [ ] **Step 3:** Run `uv run pytest tests/sources/yahoo -v` → FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run same → PASS.
- [ ] **Step 6:** Commit `feat: yahoo parsers and source`.

---

### Task 14: BBM projections parsers and source

**Files:**
- Create: `src/h2h_advisor/sources/bbm/__init__.py`, `parsers.py`, `source.py`
- Test: `tests/sources/bbm/test_parsers.py`, `tests/sources/bbm/test_source.py`

**Interfaces:**
- Consumes: fixtures and `bbm/urls.py` (Task 12); `BrowserSession.fetch/postback/download` (Task 6); `PlayerIndex` (Task 8)
- Produces:
  - `parse_projections(content: str|bytes, scope: Literal["day","week","ros"]) -> list[Projection]` — dispatches on content type (xlsx bytes via openpyxl, or html table via selectolax, whichever discovery chose). Per-game StatLine must include `fgm/fga/ftm/fta`; if BBM gives only percentages + attempts, derive makes = pct × attempts. Per-category values (BBM's pV, 3V, rV, aV, sV, bV, fg%V, ft%V, toV columns or their export equivalents) go into `Projection.cat_values` keyed by `Category`. `ParseError` on missing columns.
  - `class BbmSource(session: BrowserSession, index: PlayerIndex)`: `projections(scope, day: date|None=None) -> list[Projection]` — uses the fewest loads: one GET, plus postback(s) only when the page's default date/period differs from the requested one, plus export if chosen.

- [ ] **Step 1: Write failing tests** — per scope fixture: row count, one named player's per-game PTS and FGA/FGM as shown, `value` present; `test_derives_makes_from_pct_and_attempts` (only if the fixture lacks makes); `test_missing_columns_parse_error`; source test with fake session: `test_day_projection_for_today_needs_no_postback` (1 fetch, 0 postbacks), `test_other_date_uses_one_postback`.
- [ ] **Step 2:** Run `uv run pytest tests/sources/bbm -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: bbm projections parsers and source`.

---

### Task 15: Advisor service (TTL, envelopes, busy/login fallback)

**Files:**
- Create: `src/h2h_advisor/service.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `@dataclass SourceAge(source: str, fetched_at: datetime|None, stale: bool)`
  - `@dataclass Envelope(data: Any, ages: list[SourceAge], notices: list[str])`
  - `class Advisor(config: Config, cache: Cache, session_factory: Callable[[], ContextManager[BrowserSession]], schedule_fetcher: Callable[[], Schedule], clock: Callable[[], datetime])`
    - `list_leagues()`, `league_settings(league)`, `matchup(league, week=None)`, `roster(league, team="me"|"opponent"|str)`, `free_agents(league, limit=50, position=None)`, `projections(scope, day=None)`, `schedule(start, end)`, `analyze_week(league)`, `rank_streamers(league, horizon, max_results=10)`, `status()` — each `-> Envelope`; all accept `refresh: bool=False` where they fetch.
    - `league` accepts id or case-insensitive name substring (unique match, else `ValueError` listing leagues).
    - Fetch-or-cache: fresh cache → return; else open one session for the whole call (lazily, only if something is stale) and fetch. `refresh=True` ignored if item younger than 120 s.
    - Fallbacks: `BrowserBusy` → cached data + notice `"browser busy in another session; showing cached data"`; `NeedsLogin(site)` → cached data, `stale=True`, notice `"{site} needs login: run `h2h-advisor login`"`, no retry; `DailyCapReached` → cached + notice; `ParseError` → notice naming the page, data `None` for that piece. No cache at all → data `None` + notice.
    - `analyze_week`/`rank_streamers` build `WeekContext` from settings, matchup, both rosters, BBM `week` per-game (horizon `today` uses `day`), BBM `ros` values, schedule; `rank_streamers` applies `drop_pool` with config never-drop/stream_slots/margin and passes `max_adds` override when settings lacked it.
    - `status()` returns per site: logged-in state (last NeedsLogin vs last success), today's loads vs cap, cache ages per type, `recent_blocked(10)`.
    - `history(league)` — weeks 1..current-1; each past week cached permanently (key `history:{league}:{week}`, no TTL); fetches only missing weeks.
    - `build(league)` — `resolve_build(build_from_config(...), infer_build(roster ROS cat_values summed, [matchup_result(...) for history]))`.
    - `long_term_upgrades(league, max_results=5)`.
    - `rank_streamers` passes `build(league)` and `config.light_day_max_games`; `analyze_week` envelopes also include `build` and `at_risk_strong`.
    - `daily_brief(leagues: list[str]|Literal["all"]="all", horizon="today")` — one browser session for the whole call, leagues processed sequentially; per league: `analyze_week` data, top 3 `rank_streamers`, `long_term_upgrades`, and `facts: list[str]` (deterministic: light days left with NBA game counts, categories with |win_prob−0.5| < 0.1, usable-games edge me vs opponent, opponent's historically won categories when history exists). A failure in one league becomes a notice; the others still return.

- [ ] **Step 1: Write failing tests** (fake session factory serving fixtures; frozen clock)
  - `test_fresh_cache_does_not_open_browser`
  - `test_stale_triggers_single_session_for_whole_call` (analyze_week with everything stale → factory called once)
  - `test_needs_login_returns_cached_stale_with_notice` (Review Focus 5) — session raises `NeedsLogin("yahoo")` → envelope has prior cached matchup, `stale=True`, notice contains `h2h-advisor login`, factory called once
  - `test_busy_returns_cached_with_notice`
  - `test_refresh_floor_120s`
  - `test_league_name_lookup_ambiguous_raises`
  - `test_rank_streamers_never_suggests_never_drop` — end-to-end with fixtures and config never-drop containing the lowest-value rostered player
  - `test_history_cached_permanently_fetches_only_new_weeks` — weeks 1–3 cached, current week 5 → fetches only week 4
  - `test_daily_brief_one_session_all_leagues` — 2 leagues stale → factory called once; one league raising ParseError → its notice present, other league data present
- [ ] **Step 2:** Run `uv run pytest tests/test_service.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: advisor service with ttl and graceful fallbacks`.

---

### Task 16: MCP server

**Files:**
- Create: `src/h2h_advisor/server.py`
- Modify: `src/h2h_advisor/cli.py` (`serve` runs `server.run()` over stdio)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `Advisor` (Task 15)
- Produces: FastMCP app `mcp = FastMCP("h2h-advisor")` with tools exactly: `list_leagues`, `get_league_settings(league)`, `get_matchup(league, week=None, refresh=False)`, `get_roster(league, team="me", refresh=False)`, `get_free_agents(league, limit=50, position=None, refresh=False)`, `get_projections(scope, date=None, refresh=False)`, `get_schedule(start, end)`, `analyze_week(league, refresh=False)`, `rank_streamers(league, horizon="today", max_results=10, refresh=False)`, `long_term_upgrades(league, max_results=5, refresh=False)`, `get_history(league)`, `daily_brief(leagues="all", horizon="today", refresh=False)`, `status()`. Each returns the `Envelope` as JSON (dates ISO). Tool descriptions state they are read-only and never change anything on Yahoo/BBM. All tools annotated `readOnlyHint=True`.

- [ ] **Step 1: Write failing tests** — `test_tool_names_exact` (set of listed tool names == exactly the thirteen above), `test_tools_read_only_annotated`, `test_rank_streamers_returns_json_envelope` (Advisor stubbed).
- [ ] **Step 2:** Run `uv run pytest tests/test_server.py -v` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5:** Commit `feat: mcp server tools`.

---

### Task 17: Skill, plugin packaging, README, live smoke check

**Files:**
- Create: `.claude-plugin/plugin.json`, `.mcp.json`, `skills/streaming-advisor/SKILL.md`, `README.md`
- Test: `tests/test_packaging.py`

**Interfaces:**
- `.mcp.json`: server `h2h-advisor`, command `uv`, args `["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "h2h-advisor", "serve"]`.
- `SKILL.md` frontmatter `name: streaming-advisor`, description triggering on fantasy basketball add/drop/stream/start/"how am I doing" questions. Body = spec §10.1 principles 1–7 (options not verdicts; two separate lists This week / Long-term upgrades; status-report vs moves modes; all-leagues via `daily_brief`; usable games and light days; position fit; build respect with `at_risk_strong` flag; close categories by margin and games left) and spec §7.9 steps 1–5, with two output templates: (a) status report per league — category table (cat, me now, opp now, me proj, opp proj, win %), usable games left me/opp, adds left, days left, 2–4 facts; (b) moves — This week options (add, drop, usable days with slot, light days, categories moved, adds left after) then Long-term upgrades (add, drop, ROS gain, key categories, games this week). Plus: always state data age; never claim a move was made; name categories to chase/punt with reasons; when `rank_streamers` returns a reason instead of options, explain it; respect `adds_left` across the week; prefer one tool call per data need; waivers (spec §10.1 #8): never present a waiver player as an immediate add — label "on waivers — clears <date>", show only days from that date, call it a claim/bid in FAB leagues, and pair it with the best immediate free agent.
- `cli.py` change (already implemented by the plain-Chrome login, spec §13; nothing to do here): `login` waits for Enter only when stdin is a TTY (Enter → SIGTERM Chrome, up to 15 s for cookies to flush, then kill); otherwise (launched by Claude via Bash) it waits for the Chrome process to exit (user quits with Cmd+Q). Test both paths with fakes.
- `skills/setup/SKILL.md` (frontmatter `name: setup`, description: guided first-time setup / re-setup of h2h-advisor: login, leagues, never-drop, builds, config) implementing spec §11 steps 1–7 exactly; it must show the full config file and get confirmation before writing, merge with existing config, and launches `uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login` via Bash only after the user agrees (user logs in and quits that Chrome with Cmd+Q), never via MCP tools.
- README: install (`uv`, `uv run playwright install chrome`), `h2h-advisor login`, config example (spec §7.8 incl. `stream_slots`), Claude Code plugin install, Claude Desktop `claude_desktop_config.json` snippet using absolute `--directory`, "what it will never do". **README style (Jakub, 2026-11-25): model it on `/Users/jakubigla/Project-Repositories/Personal/xpm/README.md`** — light/dark banner SVGs in `docs/assets/readme/` via `<picture>`, shields.io badges (version, Claude Code plugin, Claude Desktop, read-only), centred one-line pitch + nav links, a short numbered **Quick start**, then two clearly separated install sections — **Claude Code** (`claude plugin marketplace add <path-or-repo>` + `claude plugin install h2h-advisor@...`, then `/h2h-advisor:setup`) and **Claude Desktop** (exact steps verified at T17 time against current Desktop docs: plugin/extension install if supported, else the `claude_desktop_config.json` snippet with absolute paths and where that file lives on macOS) — each ending with "check it works" (ask "how am I doing in all my leagues?"). Then a "Why" feature table (read-only guard, polite rate limits, cache, all leagues in one prompt, This week vs Long-term), a tools table, a `<details>` for config and troubleshooting (needs_login → re-run login; browser busy in another session), a "What it will never do" callout (`> [!IMPORTANT]`), and a mermaid flow (Claude → MCP server → guarded Chrome → Yahoo/BBM, cache). Verify every command in the README by running it (install into a scratch Claude Code config) before claiming done.

- [ ] **Step 1: Write failing tests** — `test_plugin_manifest_valid_json_and_name`, `test_mcp_json_points_to_serve`, `test_skill_frontmatter_has_name_and_description` (both skills: streaming-advisor and setup).
- [ ] **Step 2:** Run `uv run pytest tests/test_packaging.py -v` → FAIL.
- [ ] **Step 3:** Write the files.
- [ ] **Step 4:** Run same → PASS.
- [ ] **Step 5: Live smoke check with Jakub** — install the plugin locally in Claude Code, ask "status", then "analyze my week in <league>" and "best streamer today in <league>". Confirm: answers show data ages; `status()` shows page loads within caps and zero non-allowlisted POSTs reaching the sites (blocked list only); second identical question within 20 min opens no browser.
- [ ] **Step 6:** Commit `feat: streaming-advisor skill, plugin packaging, readme`.
