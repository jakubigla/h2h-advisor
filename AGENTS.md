# AGENTS.md

Guide for coding agents working on h2h-advisor. Users install it through `README.md`. This file is for changing it.

## What this is

A **read-only** fantasy basketball advisor shipped as a Claude Code plugin:

- An MCP server (`src/h2h_advisor/server.py`) over a stdio transport, started by `.mcp.json`.
- Two plugin skills: `skills/streaming-advisor/` (answers) and `skills/setup/` (login and config).
- Data comes from Yahoo Fantasy NBA and Basketball Monster, loaded in a guarded headless Chrome
  (Playwright), plus ESPN's public game-log API for recent minutes.

The product promise is that it **never changes anything** on Yahoo or Basketball Monster. Treat
any change that could weaken that promise as a security change.

## Commands

```sh
uv sync                                        # install deps (Python 3.12, uv)
uv run pytest tests/test_service.py -q         # run the test file(s) for the code you changed
uv run pytest tests/test_service.py -k notes   # or narrower
uv run ruff check src tests                    # lint (must be clean)
uv run h2h-advisor serve                       # MCP server on stdio
uv run h2h-advisor discover <url>              # save a page's HTML for parser work
```

- Run only the tests for what you changed, never the whole suite. Tests marked `browser` launch a
  real Chrome: skip them unless you changed `browser/session.py`.
- **Never run `h2h-advisor login`.** It opens a headed Chrome for a human to type credentials.
  Ask the user to run it.
- No test touches a live site. Keep it that way: parsers are tested against saved fixtures in
  `tests/fixtures/{yahoo,bbm,espn}/`.

## Map

| Path | What lives there |
|---|---|
| `server.py` | MCP tool definitions. Thin: validates args, calls `service.py`. |
| `service.py` | The `Advisor`: orchestrates sources, cache, notes and analysis per tool call. |
| `analysis/` | Pure maths: `projection` (rest-of-week), `categories` (win odds), `streamers` (rankings, beam-search `plan_adds`), `outlook`, `build` (punt/strong categories). |
| `sources/yahoo`, `sources/bbm` | `urls.py` builds URLs, `parsers.py` turns HTML into models (no I/O), `source.py` fetches through the browser. |
| `sources/espn.py`, `sources/schedule.py` | ESPN minutes check; the week's game schedule. |
| `browser/guard.py` + `guard_rules.toml` | **The read-only guard.** A pure `decide()` for every outgoing request. Deny by default. |
| `browser/session.py`, `browser/ratelimit.py` | One Chrome per machine, 4–9 s between loads, daily page caps, 15-minute backoff. |
| `cache.py`, `notes.py`, `config.py`, `paths.py` | SQLite cache with TTLs, notes JSON, `config.toml`. Every path hangs off `paths.home()`. |
| `skills/*/SKILL.md` + `references/` + `evals/` | Plugin skills, their lookup files, `claude plugin eval` cases. |
| `scripts/sanitize_fixture.py` | Strips tokens, emails and manager or team names from a saved page before it becomes a fixture. |

## Rules

- **Guard first.** The guard denies when in doubt. Do not add an allow rule, loosen a pattern or
  bypass `decide()` without a test in `tests/browser/test_guard.py` that shows the new request is
  a read. Login hosts are never fetched.
- **No live data in the repo.** Every fixture goes through `scripts/sanitize_fixture.py`. No real
  league ids, team or manager names, emails, cookies, crumbs or `__VIEWSTATE`.
- **Parsers are pure.** HTML in, models out, no network. A layout change becomes a notice
  ("could not read the … page"), not a crash.
- **Tests stay hermetic.** The autouse fixture in `tests/conftest.py` points `H2H_ADVISOR_HOME` at
  a temp dir. Never read the real `~/.config/h2h-advisor` or `~/.local/share/h2h-advisor`.
- **Every response is honest about freshness.** Keep `fetched_at`/`stale` and notices flowing
  through to tool output. The skills rely on them.
- **Skills are tested.** `tests/test_packaging.py` checks the frontmatter (`description` starts with
  "Use when") and that every `references/*.md` file is linked from `SKILL.md`, and every link resolves.
  After a skill change, run it, and run the skill's evals if its behaviour changed.

## Conventions

- Ruff-clean, typed and plain. Prefer dataclasses in `models.py` over loose dicts.
- Conventional commits (`feat(plan): …`, `fix(bbm): …`, `chore(release): …`). One PR per change,
  squash-merged into `main`.
- PR bodies say **why**, **what**, and **testing**: which test files ran and which did not.
- The version lives in four places and changes together: `pyproject.toml`,
  `.claude-plugin/plugin.json`, the README badge and `uv.lock` (`uv lock`). Plugin installs only
  update when it changes. `/release` does it.
