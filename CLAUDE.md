@AGENTS.md

## Claude Code

- Project tooling lives in `.claude/`:
  - `/release [patch|minor|major]` cuts a tagged GitHub release.
  - `/fixture <url>` turns a live page into a sanitized parser fixture.
  - The `guard-reviewer` subagent reviews a diff for anything that could make a request change state.
    Use it on any change under `browser/` or `sources/*/source.py`.
- A hook runs `ruff check` on each Python file you edit and reports problems back. Fix them before
  you move on.
- To try the plugin itself from this checkout, run `claude --plugin-dir .`, then `/mcp` to
  reconnect after a server change.
