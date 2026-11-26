"""Plugin packaging: manifests, MCP launcher and the two skills."""

import asyncio
import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest

from h2h_advisor import server

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("streaming-advisor", "setup")
# Tool-like identifiers: a call `name(` or a name with a tool prefix.
TOOL_CALL = re.compile(r"`([a-z_]+)\(")
TOOL_PREFIXED = re.compile(r"`((?:get|list|rank|analyze|daily|long_term)_[a-z_]+)")


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def server_tools() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """The simple `key: value` YAML header of a SKILL.md, and the body after it."""
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, "SKILL.md must start with a --- frontmatter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def skill(name: str) -> tuple[dict[str, str], str]:
    return frontmatter(
        (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    )


def test_plugin_manifest_valid_json_and_name():
    manifest = load_json(".claude-plugin/plugin.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["name"] == "h2h-advisor"
    assert manifest["version"] == project["project"]["version"]
    assert manifest["description"]
    assert manifest["author"] == {"name": "Jakub Igla"}  # name only, no email


def test_plugin_contacts_only_yahoo_and_basketball_monster():
    hosts = load_json(".claude-plugin/plugin.json")["network"]["hosts"]
    assert hosts == ["basketball.fantasysports.yahoo.com", "basketballmonster.com"]


def test_marketplace_lists_the_plugin_from_repo_root():
    market = load_json(".claude-plugin/marketplace.json")
    assert market["name"] == "h2h-advisor"
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [
        ("h2h-advisor", "./")
    ]


def test_mcp_json_points_to_serve():
    servers = load_json(".mcp.json")["mcpServers"]
    assert servers == {
        "h2h-advisor": {
            "command": "uv",
            "args": [
                "run",
                "--directory",
                "${CLAUDE_PLUGIN_ROOT}",
                "h2h-advisor",
                "serve",
            ],
        }
    }


@pytest.mark.parametrize("name", SKILLS)
def test_skill_frontmatter_has_name_and_description(name):
    fields, body = skill(name)
    assert fields["name"] == name
    assert fields["description"].startswith("Use when")
    assert len(fields["description"]) <= 1024
    assert body.strip()


@pytest.mark.parametrize("name", SKILLS)
def test_skill_mentions_only_real_tools(name):
    _, body = skill(name)
    named = set(TOOL_CALL.findall(body)) | set(TOOL_PREFIXED.findall(body))
    assert named, "the skill should name the tools it uses"
    assert named <= server_tools()


def test_streaming_skill_covers_the_core_tools_and_rules():
    _, body = skill("streaming-advisor")
    for tool in ("status", "daily_brief", "analyze_week", "rank_streamers",
                 "long_term_upgrades"):  # fmt: skip
        assert f"`{tool}(" in body, tool
    for phrase in ("This week", "Long-term upgrades", "on waivers", "never claim",
                   "targets", "ros_cost_flag", "negligible", "at_risk_strong"):  # fmt: skip
        assert phrase.lower() in body.lower(), phrase


@pytest.mark.parametrize("name", SKILLS)
def test_skill_commands_use_the_plugin_root_variable(name):
    _, body = skill(name)
    assert "<plugin root>" not in body
    for command in re.findall(r"uv run --directory (\S+)", body):
        assert command == "${CLAUDE_PLUGIN_ROOT}", command


def test_streaming_skill_defines_modes_and_one_win_need():
    _, body = skill("streaming-advisor")
    assert "none qualify" in body
    assert "3PM" not in body  # Yahoo/engine name is TPM
    for status in ("locked_win", "likely_win"):
        assert f"`{status}`" in body
    assert "you need n of" in body.lower()


def test_setup_skill_never_logs_in_through_tools():
    _, body = skill("setup")
    assert "h2h-advisor login" in body
    assert "Cmd+Q" in body
    assert "bbm_league_id" in body
    assert "config.toml" in body
    assert "build_from_config" in body  # bad category names are caught before use
    assert "Victor Wembanyama" in body


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_config_example_loads(tmp_path):
    from h2h_advisor.analysis.build import build_from_config
    from h2h_advisor.config import load_config

    blocks = re.findall(r"```toml\n(.*?)```", readme(), re.DOTALL)
    assert len(blocks) == 1, "the README has exactly one config example"
    path = tmp_path / "config.toml"
    path.write_text(blocks[0], encoding="utf-8")
    config = load_config(path)
    assert config.never_drop
    assert config.leagues
    for league in config.leagues.values():
        build_from_config(league.build)


def test_readme_links_point_to_repo_files():
    targets = re.findall(r'(?:src|srcset|href)="([^"#:]+)"', readme())
    targets += re.findall(r"\]\(([^)#:\s]+)\)", readme())
    assert targets
    for target in targets:
        assert (ROOT / target).exists(), target


def test_readme_names_every_tool_and_desktop_config():
    text = readme()
    for tool in server_tools():
        assert f"`{tool}`" in text, tool
    assert "claude_desktop_config.json" in text
    assert "claude plugin install h2h-advisor@h2h-advisor" in text
    assert "/h2h-advisor:setup" in text


# SHA-256 of the casefolded real league and team names that must never be
# committed (the names themselves are not written down anywhere in the repo).
PRIVATE_NAME_HASHES = frozenset(
    {
        "836111611220587b15e181a6ad2c497f6cd4de8d277c6cc5e3ebb95561a35095",
        "f58d2c4c9903b958c4602ff1ffc9d54a8247680778d611c2e92a78a59a9e384f",
        "b566c1850ed8b2868601460d01d19dda04acc1801e1f9752c6132a0551a7c4ce",
        "e52da15f92c74149b756f3fa2a06982d274a155778fc15681c465c650a6a5980",
        "f4d9b5e134142ab7aa3871cb217f7e1267db2374a1cc7c96ec94e4fe9e9fd044",
        "1573952f87c33fdedc95a44039f36eb470c103708ac4f7f8ddd2b8ad1e2071ad",
        "b6e36ddb1128e517fc6fd3591eca065485aa32308072de6302aaef737aff808b",
        "8cba27dc9dcaa45d68344bf3258b216a7f4a943373dd978eb3fef213d6b52742",
    }
)
MAX_NAME_WORDS = 5
# Letters and digits only: "_", "/", "-" and "." split words, so a name inside a
# file name or a path ("x_name.html") is still a word of its own.
WORD = re.compile(r"[^\W_]+")
SKIPPED_DIRS = {".git", ".venv", ".superpowers", "__pycache__", ".pytest_cache",
                ".ruff_cache"}  # fmt: skip
TEXT_SUFFIXES = {".md", ".py", ".toml", ".json", ".html", ".svg", ".txt", ".lock",
                 ".yml", ".yaml", ".cfg", ".ini", ""}  # fmt: skip


def name_hashes(text: str) -> set[str]:
    """SHA-256 of every run of 1..MAX_NAME_WORDS consecutive casefolded words."""
    words = WORD.findall(text.casefold())
    found = set()
    for size in range(1, MAX_NAME_WORDS + 1):
        for start in range(len(words) - size + 1):
            gram = " ".join(words[start : start + size])
            found.add(hashlib.sha256(gram.encode()).hexdigest())
    return found


def repo_text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if set(relative.parts) & SKIPPED_DIRS or not path.is_file():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith("."):
            files.append(path)
    return files


def test_name_check_finds_names_in_text_and_paths():
    # Self-check with a stand-in hash: words, case, n-grams and path parts.
    def probe(text: str, name: str) -> bool:
        target = hashlib.sha256(name.casefold().encode()).hexdigest()
        return target in name_hashes(text)

    assert probe("tests/fixtures/x_Quokka.html", "quokka")
    assert probe("<b>H2H Red-Quokka/Wombat Cup</b>", "red quokka")
    assert probe("a b c d e", "A B C D E")
    assert not probe("a b c d e f", "a b c d e f")  # longer than MAX_NAME_WORDS
    assert not probe("quokkas", "quokka")


def test_repo_names_no_real_leagues_or_teams():
    hits = []
    for path in repo_text_files():
        relative = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8", errors="ignore")
        if name_hashes(relative + "\n" + text) & PRIVATE_NAME_HASHES:
            hits.append(relative)
    assert not hits, f"private league/team names in: {hits}"
