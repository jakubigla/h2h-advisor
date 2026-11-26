---
description: Cut a tagged GitHub release of h2h-advisor (version bump PR, tag, release notes)
argument-hint: "[patch|minor|major|X.Y.Z]"
---

Cut a release of h2h-advisor. Bump: `$ARGUMENTS`. If it's empty, choose one from the commits since the last tag: `minor` if any are `feat`, otherwise `patch`.

1. **Check**: on `main`, clean tree, up to date with `origin/main`. Find the last tag with `git describe --tags --abbrev=0`. List the PRs merged since then with `git log <tag>..HEAD --oneline`. Stop if there's nothing new.
2. **Bump** on a `release/vX.Y.Z` branch. Change all four places together:
   - `pyproject.toml` `version`
   - `.claude-plugin/plugin.json` `version`
   - the README badge (`version X.Y.Z` alt text and the `version-X.Y.Z-` badge URL)
   - `uv.lock`, via `uv lock`

   Then `grep` for the old version outside `.venv`/`.git` and make sure none is left.
3. **Verify**: `uv run pytest tests/test_packaging.py -q` and `uv run ruff check src tests`. If either fails, stop and report. Do not paper over it.
4. **PR**: commit `chore(release): vX.Y.Z`, push, and open a PR whose body lists the bump and the testing. Show me the PR link, and ask before merging.
5. **Tag and release** after the squash merge: fast-forward `main`, `git tag -a vX.Y.Z -m vX.Y.Z <merge sha>`, push the tag. Then run `gh release create vX.Y.Z --latest` with notes grouped by area (planner, notes, skills, server…), drawn from the merged PR bodies and citing `(#N)`. End with a full-changelog compare link from the previous tag.
6. Report the release URL and anything you skipped.
