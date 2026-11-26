"""Turn a saved, logged-in Yahoo/BBM page into a safe test fixture.

Usage:
    uv run python scripts/sanitize_fixture.py yahoo SRC.html DST.html [--name NAME ...] [--map NAME=LABEL ...]
    uv run python scripts/sanitize_fixture.py bbm SRC.html DST.html [--name NAME ...] [--map NAME=LABEL ...] [--keep-team TEAM]

Removes everything that is not page content (scripts, styles, comments, ...),
blanks session tokens (crumbs, __VIEWSTATE*, __EVENTVALIDATION), replaces email
addresses and manager names, and drops per-site page chrome that carries
account details. Tables, form fields and the text the parsers read are kept.

League names become "League N" and fantasy team names "Team N", stable within a
file (Yahoo: league and team links, the players filter, the settings table and
the custom league URL; BBM: the league switch options and the Own/Owner columns),
and replaced wherever else they occur on the page, never inside player links.
``--map`` pins a name to a label so several pages agree. A name that already is
a "League N"/"Team N" label keeps it, so the script can re-run on its own output.
BBM ``--keep-team`` leaves the given team name(s) as they are.
"""

import argparse
import re
import sys
from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

REMOVED_TAGS = "script, style, noscript, svg, iframe, link, meta"
# Page chrome that identifies the account and holds no data we parse.
SITE_DROP_SELECTORS = {
    "yahoo": ["#yspcontentuh"],  # global header: account menu, avatar, nav
    "bbm": ["nav.navbar"],  # top nav: account menu with the BBM user name
}
# The per-cell stat "monster bar" is decoration made of ~20 tooltips per cell.
# BBM fixtures keep the cell but empty the bar so the tables stay fixture-sized.
SITE_EMPTIED_SELECTORS = {"bbm": ["td div.range-badge"], "yahoo": []}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico)$", re.IGNORECASE)
CRUMB_IN_URL = re.compile(r"(crumb=)[^&\"'\s;]*", re.IGNORECASE)
GENERIC_LINK_TEXT = {"view profile", "view your profile", "profile"}
# Blanked to "X". __VIEWSTATEFIELDCOUNT is a plain number, not a secret: kept.
TOKEN_FIELD_PREFIXES = ("__viewstate", "__eventvalidation")
KEPT_FIELDS = frozenset({"__viewstatefieldcount"})
MIN_NAME_LENGTH = 4
# Text a manager name may legitimately share (players, teams, league titles).
PROTECTED_TEXT_SELECTORS = (
    "a.name, a[href*='playerinfo'], a[href*='/nba/'], title, option, td.player"
)


def _warn(message: str) -> None:
    print(f"sanitize_fixture: {message}", file=sys.stderr)


def _mask_email(match: re.Match[str]) -> str:
    # "chat@6x.png" style image names are not addresses.
    return match.group(0) if IMAGE_SUFFIX.search(match.group(0)) else "user@example.com"


def _whole_word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


class _Managers:
    """Stable 'Manager N' labels per person within one file; exact names only."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}

    def _number(self, key: str) -> int:
        key = key.casefold()
        if key not in self._index:
            self._index[key] = len(self._index) + 1
        return self._index[key]

    def knows(self, name: str) -> bool:
        return name.casefold() in self._index

    def label(self, name: str) -> str:
        return f"Manager {self._number(name)}"

    def slug(self, slug: str, name: str | None = None) -> str:
        """Label for a profile slug: paired with its link text, else its letters
        when they exactly equal a known name, else a person of its own."""
        base = slug.rstrip("0123456789") or slug
        key = name if name else (base if self.knows(base) else slug)
        return f"manager-{self._number(key)}"


def _profile_slug(href: str) -> str | None:
    match = re.search(r"/profile/([^/?\"'#\s&]+)", href)
    return match.group(1) if match else None


def _is_manager_element(node) -> bool:
    classes = (node.attributes.get("class") or "").split()
    if "user-id" in classes:
        return True
    href = node.attributes.get("href") or ""
    return (
        node.tag == "a"
        and "/profile/" in href
        and node.text(strip=True).casefold() not in GENERIC_LINK_TEXT
    )


PLACEHOLDER = re.compile(r"(League|Team|Manager) ([1-9]\d*)")
# Yahoo league home `/nba/<league>` and team `/nba/<league>/<team>` (no query: with
# one it is a stat tab of the team page) or `/nba/<league>/<team>/team?...`.
YAHOO_LEAGUE_HREF = re.compile(r"^(?:https?://[^/]+)?/nba/\d+/?$")
YAHOO_TEAM_HREF = re.compile(
    r"^(?:https?://[^/]+)?/nba/\d+/\d+(?:/?|/team(?:[?#].*)?)$"
)
YAHOO_TITLE = re.compile(r"^(.+?) - (.+?) \| Fantasy Basketball\b")
YAHOO_CUSTOM_URL = re.compile(r"(fantasysports\.yahoo\.com/league/)[\w.-]+")
# BBM league switch option: "_1. <name>  (Yahoo!)" or "3. <name>  (Yahoo!)".
BBM_LEAGUE_OPTION = re.compile(r"^\s*_?\d+\.\s*(.*?)\s*(?:\([^()]*\))?\s*$")
BBM_LEAGUE_SELECT = "select[name='pageTitleRowLeague'] option"
NAME_ATTRS = ("title", "alt", "aria-label")
# Elements whose text is a player name, which a team name must never touch.
PLAYER_LINK_HREF = ("playerinfo", "/players/")


class _Labels:
    """Stable "League N" / "Team N" labels within one page.

    A name that already is such a label keeps it (so a sanitised page comes out
    unchanged), ``name_map`` fixes labels across pages, and every other name gets
    the lowest number its kind has not used yet, in order of first appearance.
    """

    def __init__(self, found: list[tuple[str, str]], name_map: dict[str, str]):
        self.labels: dict[str, str] = {}
        used: dict[str, set[int]] = {}
        preset = {k.casefold(): v for k, v in name_map.items()}
        for label in [*preset.values(), *(n for _, n in found)]:
            match = PLACEHOLDER.fullmatch(label)
            if match:
                used.setdefault(match.group(1), set()).add(int(match.group(2)))
        for kind, name in found:
            key = name.casefold()
            if key in self.labels:
                continue
            if PLACEHOLDER.fullmatch(name):
                self.labels[key] = name
            elif key in preset:
                self.labels[key] = preset[key]
            else:
                taken = used.setdefault(kind, set())
                number = next(n for n in range(1, len(taken) + 2) if n not in taken)
                taken.add(number)
                self.labels[key] = f"{kind} {number}"

    def __getitem__(self, name: str) -> str:
        return self.labels[name.casefold()]


def _norm(text: str | None) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _header(table) -> list[str] | None:
    row = next((r for r in table.css("tr") if r.css_first("th")), None)
    return None if row is None else [_norm(th.text()) for th in row.css("th")]


def _yahoo_names(tree: LexborHTMLParser) -> list[tuple[str, str, object]]:
    """(kind, name, element) for every league and team name the page shows."""
    found = []
    title = tree.css_first("title")
    match = YAHOO_TITLE.match(_norm(title.text())) if title is not None else None
    if match:  # a team page: "<league> - <team> | Fantasy Basketball | Yahoo!"
        found += [("League", match.group(1), title), ("Team", match.group(2), title)]
    for node in tree.css("a[href], select[name='fteam'] option, #settings-table tr"):
        if node.tag == "a":
            href = node.attributes.get("href") or ""
            kind = (
                "League" if YAHOO_LEAGUE_HREF.match(href)
                else "Team" if YAHOO_TEAM_HREF.match(href)
                else None
            )  # fmt: skip
            node_with_name = node
        elif node.tag == "option":
            value = (node.attributes.get("value") or "").upper()
            kind, node_with_name = (None if value == "NONE" else "Team"), node
        else:
            cells = node.css("td")
            is_name = len(cells) == 2 and _norm(cells[0].text()) == "League Name:"
            kind, node_with_name = ("League" if is_name else None), cells[-1:]
            node_with_name = node_with_name[0] if node_with_name else None
        if kind is None or node_with_name is None:
            continue
        name = _norm(node_with_name.text())
        if any(ch.isalnum() for ch in name):  # icon-only links carry no name
            found.append((kind, name, node_with_name))
    return found


def _bbm_names(tree: LexborHTMLParser, keep: set[str]) -> list[tuple[str, str, object]]:
    found = []
    for option in tree.css(BBM_LEAGUE_SELECT):
        match = BBM_LEAGUE_OPTION.match(_norm(option.text()))
        if match and match.group(1):
            found.append(("League", match.group(1), option))
    for table in tree.css("table"):
        titles = _header(table) or []
        if "Own" not in titles:
            continue
        own = titles.index("Own")
        owner = titles.index("Owner") if "Owner" in titles else None
        for row in table.css("tr"):
            cells = row.css("td")
            if row.css_first("th") or own >= len(cells):
                continue
            for span in cells[own].css("span[title]"):
                team = _norm(span.attributes.get("title"))
                if team and team.casefold() not in keep:
                    found.append(("Team", team, span))
            if owner is not None and owner < len(cells):
                team = _norm(cells[owner].text())
                if team and team.casefold() not in keep:
                    found.append(("Team", team, cells[owner]))
    return found


def _name_pattern(names: list[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    ordered = sorted(set(names), key=len, reverse=True)
    alternatives = "|".join(re.escape(n).replace(r"\ ", r"[\s\xa0]+") for n in ordered)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def _in_player_link(node) -> bool:
    while node is not None:
        if node.tag == "a":
            href = node.attributes.get("href") or ""
            classes = (node.attributes.get("class") or "").split()
            if "name" in classes or any(p in href for p in PLAYER_LINK_HREF):
                return True
        node = node.parent
    return False


def _relabel_leagues_and_teams(
    tree: LexborHTMLParser, site: str, keep: list[str], name_map: dict[str, str]
) -> None:
    """Replace real league and fantasy team names with "League N" / "Team N".

    Names are read from where each site shows them (Yahoo: league and team
    links, the players filter, the settings table; BBM: the league switch, the
    Own tooltips and the Owner column), then replaced everywhere on the page in
    text and in title/alt attributes, except inside player links.
    """
    kept = {name.casefold() for name in keep}
    found = _yahoo_names(tree) if site == "yahoo" else _bbm_names(tree, kept)
    # Mapped names are replaced wherever they occur, found by a rule or not.
    found += [("Team", name, None) for name in name_map]
    labels = _Labels([(kind, name) for kind, name, _ in found], name_map)

    def relabel(text: str, pattern: re.Pattern[str] | None) -> str:
        if pattern is None:
            return text
        return pattern.sub(lambda m: labels[_norm(m.group(0))], text)

    # Where a name was found it is replaced whatever its length...
    for _, name, node in found:
        if node is None or labels[name] == name:
            continue
        own = _name_pattern([name])
        if node.tag == "span":
            node.attrs["title"] = labels[name]
            continue
        for child in [c for c in node.traverse(include_text=True) if c.tag == "-text"]:
            old = child.text(deep=False)
            new = relabel(old, own)
            if new != old:
                child.replace_with(new)
    # ...elsewhere only names long enough not to hit unrelated words.
    pattern = _name_pattern(
        [
            n
            for _, n, node in found
            if (node is None or len(n) >= MIN_NAME_LENGTH) and labels[n] != n
        ]
    )
    if pattern is None:
        return
    _replace_text_nodes(
        tree,
        lambda node, text: text if _in_player_link(node) else relabel(text, pattern),
    )
    for node in tree.css(", ".join(f"[{a}]" for a in NAME_ATTRS)):
        for attr in NAME_ATTRS:
            value = node.attributes.get(attr)
            if value:
                node.attrs[attr] = relabel(value, pattern)


def _collect_names(tree: LexborHTMLParser, extra: list[str]) -> list[str]:
    names = list(extra)
    for node in tree.css("[class*='user-id'], a[href*='/profile/']"):
        if _is_manager_element(node):
            names.append(node.text(strip=True))
    return [n for n in dict.fromkeys(names) if n]


def _replace_text_nodes(tree: LexborHTMLParser, replace) -> None:
    # Text nodes cannot be edited in place; swap each for new literal text.
    # Collect first: mutating while traversing crashes lexbor.
    nodes = [n for n in tree.root.traverse(include_text=True) if n.tag == "-text"]
    for node in nodes:
        old = node.text(deep=False)
        new = replace(node, old)
        if new != old:
            node.replace_with(new)


def sanitize(
    html: str,
    site: str,
    extra_names: list[str] | None = None,
    keep_teams: list[str] | None = None,
    name_map: dict[str, str] | None = None,
) -> str:
    tree = LexborHTMLParser(html)
    for node in tree.css(REMOVED_TAGS):
        node.decompose()
    comments = [n for n in tree.root.traverse(include_text=True) if n.tag == "-comment"]
    for node in comments:
        node.decompose()
    for selector in SITE_DROP_SELECTORS[site]:
        for node in tree.css(selector):
            node.decompose()
    for selector in SITE_EMPTIED_SELECTORS[site]:
        for node in tree.css(selector):
            for child in list(node.iter(include_text=True)):
                child.decompose()

    _relabel_leagues_and_teams(tree, site, keep_teams or [], name_map or {})

    for node in tree.css("input"):
        name = (node.attributes.get("name") or "").casefold()
        if name in KEPT_FIELDS:
            continue
        if name.startswith(TOKEN_FIELD_PREFIXES) or "crumb" in name:
            node.attrs["value"] = "X"

    managers = _Managers()
    names = _collect_names(tree, extra_names or [])
    for name in names:
        managers.label(name)
    slug_map: dict[str, str] = {}
    for link in tree.css("a[href*='/profile/']"):
        slug = _profile_slug(link.attributes.get("href") or "")
        if slug and slug not in slug_map:
            text = link.text(strip=True)
            paired = text if text.casefold() not in GENERIC_LINK_TEXT else None
            slug_map[slug] = managers.slug(slug, paired)

    protected = " ".join(
        node.text(separator=" ", strip=True)
        for node in tree.css(PROTECTED_TEXT_SELECTORS)
    )
    patterns: dict[str, re.Pattern[str]] = {}
    for name in names:
        if len(name) < MIN_NAME_LENGTH:
            _warn(
                f"name {name!r} is shorter than {MIN_NAME_LENGTH}: only replaced in manager elements"
            )
        elif _whole_word(name).search(protected):
            _warn(
                f"name {name!r} also occurs in player/team text: only replaced in manager elements"
            )
        else:
            patterns[name] = _whole_word(name)
    ordered = sorted(patterns, key=len, reverse=True)

    def replace_names(text: str) -> str:
        for name in ordered:
            text = patterns[name].sub(managers.label(name), text)
        return text

    def replace_in_text(node, text: str) -> str:
        parent = node.parent
        if parent is not None and _is_manager_element(parent):
            stripped = text.strip()
            if stripped:
                return text.replace(stripped, managers.label(stripped), 1)
        return replace_names(text)

    _replace_text_nodes(tree, replace_in_text)
    for node in tree.css("[title], [alt]"):
        for attr in ("title", "alt"):
            value = node.attributes.get(attr)
            if value:
                node.attrs[attr] = replace_names(value)

    out = tree.html or ""
    # Slugs only in the /profile/<slug> path position, and only whole (a short slug
    # such as "c" must not touch markup or other URLs).
    for slug, label in slug_map.items():
        out = re.sub(rf"(/profile/){re.escape(slug)}(?![\w-])", rf"\1{label}", out)
    out = CRUMB_IN_URL.sub(r"\1X", out)
    out = YAHOO_CUSTOM_URL.sub(r"\1example", out)
    out = EMAIL.sub(_mask_email, out)
    out = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", out)  # drop blank-line runs
    if "<head>" in out:
        out = out.replace("<head>", '<head><meta charset="utf-8">', 1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("site", choices=sorted(SITE_DROP_SELECTORS))
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="extra name to replace with 'Manager N' (repeatable)",
    )
    parser.add_argument(
        "--keep-team",
        action="append",
        default=[],
        help="BBM: an Own/Owner team name to keep as is (repeatable)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="NAME=LABEL",
        help="pin a league/team name to a label, e.g. 'My League=League 1' (repeatable)",
    )
    args = parser.parse_args(argv)
    name_map = dict(item.split("=", 1) for item in args.map)
    html = args.src.read_text(encoding="utf-8")
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    args.dst.write_text(
        sanitize(html, args.site, args.name, args.keep_team, name_map), encoding="utf-8"
    )
    print(f"{args.dst}: {args.dst.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
