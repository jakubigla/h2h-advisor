"""Pure parsers for the Basketball Monster projection pages (docs/discovery.md).

Both pages (``dailyprojections.aspx`` and ``Projections.aspx``) render a player
table. Tables are found by their header text, never by CSS class (BBM gave the
games table the player table's class once) and never by index; columns likewise.
A missing or ambiguous table, or a missing required column, raises ``ParseError``;
nothing is returned partially. The daily page also lists that day's games in a
table headed ``Game``/``Time`` (``MIA @ PHI``), which is the NBA schedule for the
day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from selectolax.lexbor import LexborHTMLParser, LexborNode

from h2h_advisor.identity import player_key, team_code
from h2h_advisor.models import Category, Projection, StatLine
from h2h_advisor.sources.bbm import urls
from h2h_advisor.sources.errors import ParseError

__all__ = [
    "BbmPage",
    "BbmRow",
    "OwnStatus",
    "ParseError",
    "league_name",
    "parse_page",
    "split_positions",
]

Scope = Literal["day", "ros"]
OwnStatus = Literal["mine", "mine_il", "free_agent", "waivers"]

PAGE = "bbm"

# Stat column headers per scope: StatLine field -> accepted headers (exactly one
# of them must be present). The ROS page shows per-game numbers. The daily page
# shows one day's numbers under day names when loaded by GET, but under the
# per-game names after a postback (date or filter change); for a day with one game
# they are the same numbers. The percentages are paired with attempts, so makes
# are derived as pct * attempts.
_PER_GAME: dict[str, str] = {
    "pts": "p/g", "tpm": "3/g", "reb": "r/g", "ast": "a/g", "stl": "s/g",
    "blk": "b/g", "to": "to/g", "fga": "fga/g", "fta": "fta/g",
}  # fmt: skip
_DAY: dict[str, str] = {
    "pts": "pts", "tpm": "3", "reb": "reb", "ast": "ast", "stl": "stl",
    "blk": "blk", "to": "to", "fga": "fga", "fta": "fta",
}  # fmt: skip
_STAT_HEADERS: dict[Scope, dict[str, tuple[str, ...]]] = {
    "day": {field: (_DAY[field], _PER_GAME[field]) for field in _DAY},
    "ros": {field: (name,) for field, name in _PER_GAME.items()},
}
_FG_PCT_HEADER = "fg%"
_FT_PCT_HEADER = "ft%"
_CAT_VALUE_HEADERS: dict[str, Category] = {
    "pV": Category.PTS,
    "3V": Category.TPM,
    "rV": Category.REB,
    "aV": Category.AST,
    "sV": Category.STL,
    "bV": Category.BLK,
    "fg%V": Category.FG_PCT,
    "ft%V": Category.FT_PCT,
    "toV": Category.TO,
}
_BASE_HEADERS = ("Own", "Value", "Name", "Team", "Pos", "g")

# Filters that list players of every team, so a blank Own cell does not mean
# the player is available.
_ALL_TEAMS_FILTERS = frozenset({"TopPlayers", urls.FILTER_ALL, "Rookies"})

_POSITION = re.compile(r"PG|SG|SF|PF|G|F|C")
_LEAGUE_PREFIX = re.compile(r"^_?\d+\.\s*")
_BBM_ID = re.compile(r"[?&]i=(\d+)")
_GAMES_MISSED = re.compile(r"\s+\d+g$")
_GAME = re.compile(r"\b([A-Z]{2,4}) @ ([A-Z]{2,4})\b")
_GAMES_HEADERS = ("Game", "Time")  # the games table's header cells
_PLAYER_HEADERS = ("Name", "Team", "g")  # the player table's header cells
_GAMES_SECTION = "Games for"  # the h3 above the games table
# Assumption (no fixture of an off day): a day without games shows this text in
# the "Games for ..." section, instead of or beside an empty games table.
_NO_GAMES = re.compile(r"\bno games\b", re.IGNORECASE)


@dataclass(frozen=True)
class BbmRow:
    projection: Projection
    # None: the player belongs to another team, or availability cannot be told.
    own: OwnStatus | None
    bbm_id: str
    # The ``g`` column: 1 or 0 for a game that day (daily), games remaining (ROS).
    games: float
    positions: tuple[str, ...]
    # Injury label from the ``Inj`` column's ``span.status-square`` (``Q``, ``D``,
    # ``OUT``, ``INJ``, ``Off Inj``, ``GLG``, ...) without its "<n>g" games-missed
    # suffix; None when blank or no such column.
    status: str | None = None


@dataclass(frozen=True)
class BbmPage:
    rows: list[BbmRow]
    game_date: date | None  # daily page only
    filter_value: str  # selected option of PlayerFilterControl
    league_value: str | None  # selected option of pageTitleRowLeague
    league_options: dict[str, str]  # option value -> visible text
    # Daily page only: the day's (away, home) games in canonical team codes; empty
    # for a day without games and for the ROS pages.
    games: tuple[tuple[str, str], ...] = ()
    # Daily page only: teams with a player at g >= 1 that no listed game mentions.
    # Possible when BBM drops games already started; the caller decides what it means.
    uncovered_teams: tuple[str, ...] = ()


def _norm(text: str | None) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def league_name(text: str) -> str:
    """A league option's text without BBM's leading ``_<n>. `` ordinal."""
    return _LEAGUE_PREFIX.sub("", _norm(text), count=1)


def split_positions(text: str) -> tuple[str, ...]:
    """``SFPF`` or ``SF PF`` -> ``("SF", "PF")``; unknown text is a ParseError."""
    compact = "".join(_norm(text).split())
    found = _POSITION.findall(compact)
    if not found or "".join(found) != compact:
        raise ParseError(PAGE, f"unrecognised positions {text!r}")
    return tuple(found)


def _number(text: str, column: str) -> float | None:
    """A cell's number; None for a blank cell. ``$84``, ``+0.05``, ``.513`` work."""
    cleaned = _norm(text).replace("$", "").replace(",", "").lstrip("+")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        raise ParseError(PAGE, f"{column}: expected a number, got {text!r}") from None


def _fraction(text: str, column: str) -> float:
    """A percentage shown as ``.513`` or ``51.3``, as a fraction (blank is 0)."""
    value = _number(text, column)
    if value is None:
        return 0.0
    return value / 100 if value > 1 else value


def _selected(tree: LexborHTMLParser, name: str) -> str | None:
    """Value of the selected option of ``<select name=...>`` (first if none)."""
    select = tree.css_first(f'select[name="{name}"]')
    if select is None:
        return None
    options = select.css("option")
    chosen = next((o for o in options if "selected" in o.attributes), None)
    if chosen is None and options:
        chosen = options[0]
    return None if chosen is None else (chosen.attributes.get("value") or "")


def _league_options(tree: LexborHTMLParser) -> dict[str, str]:
    select = tree.css_first(f'select[name="{urls.LEAGUE_CONTROL}"]')
    if select is None:
        return {}
    return {
        o.attributes.get("value") or "": _norm(o.text()) for o in select.css("option")
    }


def _game_date(tree: LexborHTMLParser) -> date:
    field = tree.css_first(f'input[name="{urls.DATE_FIELD}"]')
    if field is None:
        raise ParseError(PAGE, f"missing {urls.DATE_FIELD} input")
    raw = field.attributes.get("value") or ""
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ParseError(
            PAGE, f"{urls.DATE_FIELD}: expected yyyy-mm-dd, got {raw!r}"
        ) from None


def _header_titles(table: LexborNode) -> list[str] | None:
    """Normalised text of the table's first header row (None: no header row)."""
    row = next((r for r in table.css("tr") if r.css_first("th")), None)
    return None if row is None else [_norm(th.text()) for th in row.css("th")]


def _tables_with(tree: LexborHTMLParser, headers: tuple[str, ...]) -> list[LexborNode]:
    """Tables whose first header row has every one of ``headers``."""
    return [
        t
        for t in tree.css("table")
        if all(h in (_header_titles(t) or []) for h in headers)
    ]


def _games(tree: LexborHTMLParser) -> tuple[tuple[str, str], ...]:
    """The daily page's games, from the table whose header has ``Game`` and ``Time`` columns.

    Found by header, never by class or position. A row whose ``Game`` cell has no
    ``AWAY @ HOME`` (a placeholder such as "No games scheduled") is skipped, and an
    annotated cell ("MIA @ PHI (PPD)") still yields its two codes; ``parse_page``
    then checks that every team with a game in the player table is covered. No
    games table: ``()`` only when the "Games for ..." section itself says "no
    games", else ParseError (the layout changed). Page-wide text never counts:
    player news says "no games for 4 days".
    """
    tables = [
        t
        for t in _tables_with(tree, _GAMES_HEADERS)
        if "Name" not in (_header_titles(t) or [])
    ]
    if len(tables) > 1:
        raise ParseError(PAGE, "several games tables (headers 'Game', 'Time')")
    for table in tables:
        column = (_header_titles(table) or []).index("Game")
        games: list[tuple[str, str]] = []
        for tr in table.css("tr"):
            cells = tr.css("td")
            if tr.css_first("th") or column >= len(cells):
                continue
            match = _GAME.search(_norm(cells[column].text()))
            if match is not None:
                away, home = match.groups()
                games.append((team_code(away), team_code(home)))
        return tuple(games)
    for heading in tree.css("h3"):
        if _norm(heading.text(deep=False)).startswith(_GAMES_SECTION):
            section = heading.parent
            if section is not None and _NO_GAMES.search(_norm(section.text(separator=" "))):
                return ()
    raise ParseError(PAGE, "missing games table (headers 'Game', 'Time')")


def _uncovered_teams(
    rows: list[BbmRow], games: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    """Teams with a projected game (``g >= 1``) that the games table does not list."""
    listed = {team for game in games for team in game}
    return tuple(sorted({r.projection.nba_team for r in rows if r.games >= 1} - listed))


def _own_status(
    cell: LexborNode, owner: str | None, filter_value: str
) -> OwnStatus | None:
    """The Own cell: team-name span = mine (``IR`` too = on my IL), ``WW`` =
    waivers; blank = free agent when the page lists only available players."""
    if cell.css_first("span[title]") is not None:
        on_il = any(_norm(s.text()) == "IR" for s in cell.css("small"))
        return "mine_il" if on_il else "mine"
    if any(_norm(s.text()) == "WW" for s in cell.css("small")):
        return "waivers"
    if owner is not None:
        return None if owner else "free_agent"
    return None if filter_value in _ALL_TEAMS_FILTERS else "free_agent"


def _status(cell: LexborNode) -> str | None:
    square = cell.css_first("span.status-square")
    if square is None:
        return None
    label = _GAMES_MISSED.sub("", _norm(square.text(deep=False)))
    return label or None


def _is_day_without_games(tree: LexborHTMLParser) -> bool:
    try:
        return _games(tree) == ()
    except ParseError:
        return False


def _empty_day(tree: LexborHTMLParser) -> BbmPage:
    """A daily page of a day without games (e.g. Thanksgiving): BBM renders the
    games section but no player table. Assumption, no fixture: that is all it shows."""
    return BbmPage(
        rows=[],
        game_date=_game_date(tree),
        filter_value=_selected(tree, urls.FILTER_CONTROL) or "",
        league_value=_selected(tree, urls.LEAGUE_CONTROL),
        league_options=_league_options(tree),
    )


def parse_page(html: str, scope: Scope) -> BbmPage:
    """Parse a daily (``day``) or rest-of-season (``ros``) projections page."""
    tree = LexborHTMLParser(html)
    tables = _tables_with(tree, _PLAYER_HEADERS)
    if not tables:
        if scope == "day" and _is_day_without_games(tree):
            return _empty_day(tree)
        raise ParseError(
            PAGE, f"missing player table (headers {', '.join(_PLAYER_HEADERS)})"
        )
    if len(tables) > 1:
        raise ParseError(PAGE, "several player tables")
    table = tables[0]
    header_titles = _header_titles(table) or []
    columns: dict[str, int] = {}
    header_count = len(header_titles)
    for position, title in enumerate(header_titles):
        columns.setdefault(title, position)

    stat_headers: dict[str, str] = {}
    missing = [h for h in _BASE_HEADERS if h not in columns]
    for field, accepted in _STAT_HEADERS[scope].items():
        present = [h for h in accepted if h in columns]
        if len(present) > 1:
            raise ParseError(PAGE, f"ambiguous columns: {', '.join(present)}")
        if present:
            stat_headers[field] = present[0]
        else:
            missing.append(accepted[0])
    missing += [
        h
        for h in (_FG_PCT_HEADER, _FT_PCT_HEADER, *_CAT_VALUE_HEADERS)
        if h not in columns
    ]
    if missing:
        raise ParseError(PAGE, f"missing columns: {', '.join(missing)}")

    filter_value = _selected(tree, urls.FILTER_CONTROL)
    if filter_value is None:
        raise ParseError(PAGE, f"missing {urls.FILTER_CONTROL} select")
    game_date = _game_date(tree) if scope == "day" else None
    games = _games(tree) if scope == "day" else ()
    has_owner = "Owner" in columns

    rows: list[BbmRow] = []
    for tr in table.css("tr"):
        if tr.css_first("th"):
            continue  # the header, repeated inside tbody
        cells = tr.css("td")
        if not cells:
            continue
        if len(cells) != header_count:
            raise ParseError(
                PAGE, f"row has {len(cells)} cells, the header has {header_count}"
            )
        name_cell = cells[columns["Name"]]
        link = name_cell.css_first("a")
        if link is None:
            continue
        name = _norm(link.text())
        id_match = _BBM_ID.search(link.attributes.get("href") or "")
        bbm_id = id_match.group(1) if id_match else ""

        def text(header: str, cells: list[LexborNode] = cells) -> str:
            return cells[columns[header]].text() or ""

        stats = {
            field: _number(text(header), header) or 0.0
            for field, header in stat_headers.items()
        }
        fga, fta = stats["fga"], stats["fta"]
        per_game = StatLine(
            fgm=_fraction(text(_FG_PCT_HEADER), _FG_PCT_HEADER) * fga,
            fga=fga,
            ftm=_fraction(text(_FT_PCT_HEADER), _FT_PCT_HEADER) * fta,
            fta=fta,
            tpm=stats["tpm"],
            pts=stats["pts"],
            reb=stats["reb"],
            ast=stats["ast"],
            stl=stats["stl"],
            blk=stats["blk"],
            to=stats["to"],
        )
        cat_values: dict[Category, float] = {}
        for header, category in _CAT_VALUE_HEADERS.items():
            number = _number(text(header), header)
            if number is not None:
                cat_values[category] = number
        team = team_code(_norm(text("Team")))
        owner = _norm(text("Owner")) if has_owner else None
        projection = Projection(
            player_key=player_key(name, team),
            name=name,
            nba_team=team,
            per_game=per_game,
            value=_number(text("Value"), "Value"),
            cat_values=cat_values,
            minutes=_number(text("m/g"), "m/g") if "m/g" in columns else None,
        )
        rows.append(
            BbmRow(
                projection=projection,
                own=_own_status(cells[columns["Own"]], owner, filter_value),
                bbm_id=bbm_id,
                games=_number(text("g"), "g") or 0.0,
                positions=split_positions(text("Pos")),
                status=_status(cells[columns["Inj"]]) if "Inj" in columns else None,
            )
        )

    return BbmPage(
        rows=rows,
        game_date=game_date,
        filter_value=filter_value,
        league_value=_selected(tree, urls.LEAGUE_CONTROL),
        league_options=_league_options(tree),
        games=games,
        uncovered_teams=_uncovered_teams(rows, games) if scope == "day" else (),
    )
