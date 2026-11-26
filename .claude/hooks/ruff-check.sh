#!/usr/bin/env bash
# PostToolUse hook: lint the Python file Claude just edited and hand problems back.
# Exit 2 sends stderr to Claude; anything else is silent. Never blocks on missing tools.
set -uo pipefail

file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[[ "$file" == *.py && -f "$file" ]] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
command -v uv >/dev/null || exit 0

if ! out=$(uv run --quiet ruff check --output-format concise "$file" 2>&1); then
  printf 'ruff found problems in %s:\n%s\n' "$file" "$out" >&2
  exit 2
fi
exit 0
