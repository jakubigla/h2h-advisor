---
description: Turn a live Yahoo or Basketball Monster page into a sanitized parser test fixture
argument-hint: "<url> [fixture-name]"
---

Make a parser fixture from: `$ARGUMENTS`

1. Work out the site (`yahoo` or `bbm`) from the host. Confirm the URL is a page we already read through `sources/*/urls.py`. The guard must allow it, and this is not the moment to widen the guard.
2. Ask me to run `uv run h2h-advisor discover <url>` myself, since it uses my logged-in profile, and to tell you where it saved the HTML.
3. Sanitize it into `tests/fixtures/<site>/<fixture-name>.html` with `uv run python scripts/sanitize_fixture.py <site> SRC DST`. Use `--map` so names agree with the existing fixtures for the same league, and `--keep-team` (bbm) only where a test needs the real label.
4. **Check the output yourself** before it goes anywhere: grep it for `@`, `crumb`, `__VIEWSTATE`, `__EVENTVALIDATION`, cookies, and any league, team or manager name from my config. If anything personal is left, stop and tell me.
5. Delete the unsanitized source file. Add or extend a parser test in `tests/sources/<site>/` that reads the new fixture, then run just that test file.
