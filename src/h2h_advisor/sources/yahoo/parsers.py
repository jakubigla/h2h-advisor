"""Pure parsers for the Yahoo Fantasy NBA pages described in docs/discovery.md.

Each parser takes the page HTML and returns model objects. A missing expected
element raises ``ParseError`` (a layout change); nothing is returned partially.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from h2h_advisor.identity import player_key, team_code
from h2h_advisor.models import (
    Category,
    FreeAgent,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    StatLine,
)
from h2h_advisor.sources.errors import ParseError


@dataclass(frozen=True)
class LeagueRef:
    league_id: str
    name: str
    team_id: str
    team_name: str


# Category display code on the settings page -> model category.
_CATEGORY_CODES: dict[str, Category] = {
    "FG%": Category.FG_PCT,
    "FT%": Category.FT_PCT,
    "3PTM": Category.TPM,
    "PTS": Category.PTS,
    "REB": Category.REB,
    "AST": Category.AST,
    "ST": Category.STL,
    "BLK": Category.BLK,
    "TO": Category.TO,
}
# Shown on the page for display only; the percentages are derived from these.
_DISPLAY_ONLY_CODES = frozenset({"FGM/A", "FTM/A"})

_SCORING_TYPES = {
    "Head-to-Head - One Win": "one_win",
    "Head-to-Head - Categories": "categories",
}
_LINEUP_EFFECTIVE = {"Daily - Today": "today", "Daily - Tomorrow": "tomorrow"}
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip


def _norm(text: str | None) -> str:
    """Text with \\xa0 and runs of whitespace collapsed to single spaces."""
    return " ".join((text or "").replace("\xa0", " ").split())


def _tree(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def _need(node: LexborNode | None, page: str, what: str) -> LexborNode:
    if node is None:
        raise ParseError(page, f"missing {what}")
    return node


def _int(text: str, page: str, what: str) -> int:
    cleaned = _norm(text)
    if not re.fullmatch(r"\d+", cleaned):
        raise ParseError(page, f"{what}: expected an integer, got {cleaned!r}")
    return int(cleaned)


# --- home -------------------------------------------------------------------


def parse_league_list(html: str) -> list[LeagueRef]:
    """My NBA leagues with my team in each (the /nba home page)."""
    page = "home"
    teams = _need(_tree(html).css_first("#gamehome-teams"), page, "#gamehome-teams")
    leagues: list[LeagueRef] = []
    for heading in teams.css("h3"):
        link = heading.css_first("a")
        match = (
            re.fullmatch(r"/nba/(\d+)", link.attributes.get("href") or "")
            if link
            else None
        )
        if link is None or match is None:
            continue  # section heading, not a league
        league_id = match.group(1)
        name = _norm(link.text())
        item = _next_league_item(heading)
        target = _need(item, page, f"team row of league {league_id}").attributes.get(
            "data-target"
        )
        mine = re.search(r"[?&]mid1=(\d+)", target or "")
        if mine is None:
            raise ParseError(page, f"league {league_id}: no mid1 in the matchup link")
        team_id = mine.group(1)
        team_name = ""
        for a in item.css("a"):
            if re.search(
                rf"/nba/{league_id}/{team_id}$", a.attributes.get("href") or ""
            ):
                team_name = _norm(a.text())
                if team_name:
                    break
        if not name or not team_name:
            raise ParseError(page, f"league {league_id}: missing league or team name")
        leagues.append(LeagueRef(league_id, name, team_id, team_name))
    if not leagues:
        raise ParseError(page, "no leagues found")
    return leagues


def _next_league_item(heading: LexborNode) -> LexborNode | None:
    sibling = heading.next
    while sibling is not None:
        if sibling.tag == "h3":
            return None
        item = sibling.css_first("li.Listitem")
        if item is not None:
            return item
        sibling = sibling.next
    return None


# --- matchup page: week, score, totals --------------------------------------


def _week_number(tree: LexborHTMLParser, page: str) -> int:
    nav = _need(tree.css_first("#matchup_selectlist_nav"), page, "week selector")
    match = re.fullmatch(r"Week (\d+)", _norm(nav.attributes.get("title")))
    if match is None:
        raise ParseError(page, "week selector has no 'Week N' title")
    return int(match.group(1))


def parse_matchup_week(html: str) -> int:
    """The week number a matchup page shows."""
    return _week_number(_tree(html), "matchup")


def parse_week(html: str) -> tuple[int, date, date]:
    """(week number, first day, last day) from a matchup page."""
    page = "matchup"
    tree = _tree(html)
    week = _week_number(tree, page)
    days: set[date] = set()
    for a in tree.css("a[href*='date=']"):
        query = parse_qs(urlsplit(a.attributes.get("href") or "").query)
        if query.get("week") != [str(week)]:
            continue
        for value in query.get("date", []):
            if value == "totals":
                continue
            try:
                days.add(date.fromisoformat(value))
            except ValueError as exc:
                raise ParseError(page, f"bad day link date {value!r}") from exc
    if not days:
        raise ParseError(page, f"no day links for week {week}")
    return week, min(days), max(days)


def _stat_table(tree: LexborHTMLParser, page: str) -> LexborNode:
    for table in tree.css("table.Datatable"):
        header = table.css_first("thead th")
        if header is not None and _norm(header.text()).startswith("Team"):
            return table
    raise ParseError(page, "missing category table (header 'Team')")


def _column(headers: list[str], name: str, page: str) -> int:
    for i, header in enumerate(headers):
        if header.rstrip("*").startswith(name):
            return i
    raise ParseError(page, f"missing column {name!r}")


def _number(text: str, page: str, what: str) -> float:
    cleaned = _norm(text)
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ParseError(page, f"{what}: expected a number, got {cleaned!r}") from exc


# Yahoo's matchup cell for a stat with nothing recorded yet (a week not started).
_NO_STAT = "-"


def _total(text: str, page: str, what: str) -> float:
    """A matchup total: a number, or Yahoo's "-" before any game counts (0)."""
    return 0.0 if _norm(text) == _NO_STAT else _number(text, page, what)


def _made_attempted(text: str, page: str, what: str) -> tuple[float, float]:
    made, sep, attempted = _norm(text).partition("/")
    if not sep:
        raise ParseError(page, f"{what}: expected made/attempted, got {text!r}")
    return _total(made, page, what), _total(attempted, page, what)


def _team_row_totals(cells: list[str], columns: dict[str, int], page: str) -> StatLine:
    def at(name: str) -> float:
        return _total(cells[columns[name]], page, name)

    fgm, fga = _made_attempted(cells[columns["FGM/A"]], page, "FGM/A")
    ftm, fta = _made_attempted(cells[columns["FTM/A"]], page, "FTM/A")
    return StatLine(
        fgm=fgm,
        fga=fga,
        ftm=ftm,
        fta=fta,
        tpm=at("3PTM"),
        pts=at("PTS"),
        reb=at("REB"),
        ast=at("AST"),
        stl=at("ST"),
        blk=at("BLK"),
        to=at("TO"),
    )


_TOTAL_COLUMNS = ("FGM/A", "FTM/A", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO")


def _team_rows(tree: LexborHTMLParser, page: str) -> list[LexborNode]:
    table = _stat_table(tree, page)
    rows = table.css("tbody tr")
    if len(rows) < 2:
        raise ParseError(page, "category table has fewer than two team rows")
    return rows[:2]


def parse_my_team_id(html: str, league_id: str) -> str:
    """My team id: the first (mine-first) row of the category table."""
    page = "matchup"
    return _row_team_id(_team_rows(_tree(html), page)[0], league_id, page)


def _row_team_id(row: LexborNode, league_id: str, page: str) -> str:
    for a in row.css("a"):
        match = re.search(rf"/nba/{league_id}/(\d+)/?$", a.attributes.get("href") or "")
        if match:
            return match.group(1)
    raise ParseError(page, "team row has no team link")


def _row_team_name(row: LexborNode, page: str) -> str:
    for a in row.css("a"):
        name = _norm(a.text())
        if name:
            return name
    raise ParseError(page, "team row has no team name")


def parse_matchup(
    html: str, league_id: str, moves_used: int, current: bool = True
) -> Matchup:
    """A matchup page.

    ``moves_used`` is not on this page (see ``parse_adds_used``); the caller
    supplies it. On the current week's page the score block and the
    remaining-games counter are required; for ``current=False`` (a past week)
    they may be absent and the matching fields are ``None``.
    """
    page = "matchup"
    tree = _tree(html)
    week = _week_number(tree, page)
    table = _stat_table(tree, page)
    headers = [_norm(th.text()) for th in table.css("thead th")]
    columns = {name: _column(headers, name, page) for name in _TOTAL_COLUMNS}
    mine, theirs = _team_rows(tree, page)

    def cells(row: LexborNode) -> list[str]:
        return [_norm(td.text()) for td in row.css("td")]

    my_cells, opp_cells = cells(mine), cells(theirs)
    if min(len(my_cells), len(opp_cells)) <= max(columns.values()):
        raise ParseError(page, "team row has fewer cells than the header")
    won, lost = _parse_score(tree, page, current)
    games = _parse_games(tree, page, current)
    return Matchup(
        league_id=league_id,
        week=week,
        opponent_team_id=_row_team_id(theirs, league_id, page),
        opponent_name=_row_team_name(theirs, page),
        my_totals=_team_row_totals(my_cells, columns, page),
        opp_totals=_team_row_totals(opp_cells, columns, page),
        moves_used=moves_used,
        my_games_played=games[0],
        my_games_total=games[1],
        opp_games_played=games[2],
        opp_games_total=games[3],
        my_games_remaining=games[4],
        opp_games_remaining=games[5],
        cats_won=won,
        cats_lost=lost,
    )


def _parse_score(
    tree: LexborHTMLParser, page: str, required: bool
) -> tuple[int | None, int | None]:
    """Categories won/lost: the row `<td>6</td> <th>vs</th> <td>3</td>`."""
    for row in tree.css("tr"):
        if not any(_norm(th.text()) == "vs" for th in row.css("th")):
            continue
        cells = row.css("td")
        if len(cells) == 2:
            return (
                _int(cells[0].text(), page, "score"),
                _int(cells[1].text(), page, "score"),
            )
    if required:
        raise ParseError(page, "missing score row ('N vs N')")
    return None, None


_Games = tuple[int | None, int | None, int | None, int | None, int | None, int | None]


def _parse_games(tree: LexborHTMLParser, page: str, required: bool) -> _Games:
    """(my played, my total, opp played, opp total, my remaining, opp remaining)."""
    block = tree.css_first("#remaining-games")
    if block is None:
        if required:
            raise ParseError(page, "missing #remaining-games")
        return (None,) * 6
    # Label spans carry F-shade; the four value spans are, in order: my
    # remaining, my played/total, opponent played/total, opponent remaining.
    values = [
        _norm(s.text())
        for s in block.css("span")
        if "F-shade" not in (s.attributes.get("class") or "")
    ]
    if len(values) != 4:
        raise ParseError(page, f"remaining-games: expected 4 values, got {len(values)}")

    def played_total(text: str) -> tuple[int, int]:
        played, sep, total = text.partition("/")
        if not sep:
            raise ParseError(page, f"remaining-games: bad played/total {text!r}")
        return _int(played, page, "games played"), _int(total, page, "games total")

    my_played, my_total = played_total(values[1])
    opp_played, opp_total = played_total(values[2])
    return (
        my_played,
        my_total,
        opp_played,
        opp_total,
        _int(values[0], page, "my games remaining"),
        _int(values[3], page, "opp games remaining"),
    )


# --- team page --------------------------------------------------------------

_ADDS_USED = re.compile(r"You have used (\d+) of your (\d+) player adds for this week")


def parse_adds_used(team_html: str) -> tuple[int, int]:
    """(adds used, adds allowed) this week, from the team page's notes."""
    notes = _need(_tree(team_html).css_first("section#teamnotes"), "team", "#teamnotes")
    match = _ADDS_USED.search(_norm(notes.text()))
    if match is None:
        raise ParseError("team", "no 'player adds for this week' sentence")
    return int(match.group(1)), int(match.group(2))


def parse_team_week(team_html: str) -> int:
    """The current week, from the team page's matchup card ("Week 6 vs ...")."""
    card = _need(
        _tree(team_html).css_first("#team-card-matchup"), "team", "#team-card-matchup"
    )
    match = re.match(r"Week (\d+)\b", _norm(card.text()))
    if match is None:
        raise ParseError("team", "matchup card does not start with 'Week N'")
    return int(match.group(1))


def _parse_player(cell: LexborNode, page: str) -> Player:
    link = _need(cell.css_first("a.name"), page, "player name link")
    name = _norm(link.text())
    yahoo_id = link.attributes.get("data-ys-playerid")
    if not name or not yahoo_id:
        raise ParseError(page, "player link without name or data-ys-playerid")
    label = _need(cell.css_first("span.D-b > span.Fz-xxs"), page, "team - positions")
    match = re.fullmatch(r"([A-Za-z]+) - ([A-Za-z,]+)", _norm(label.text()))
    if match is None:
        raise ParseError(page, f"{name}: bad 'TEAM - POS' text {_norm(label.text())!r}")
    team = team_code(match.group(1))
    status = cell.css_first("span.ysf-player-status")
    return Player(
        key=player_key(name, team),
        name=name,
        nba_team=team,
        positions=tuple(match.group(2).split(",")),
        status=(_norm(status.text()) or None) if status is not None else None,
        yahoo_id=yahoo_id,
    )


def parse_roster(html: str) -> list[RosterEntry]:
    page = "team"
    table = _need(_tree(html).css_first("table#statTable0"), page, "table#statTable0")
    entries: list[RosterEntry] = []
    for row in table.css("tbody tr"):
        label = _need(row.css_first("td.pos span.pos-label"), page, "slot label")
        slot = label.attributes.get("data-pos") or _norm(label.text())
        cell = _need(row.css_first("td.player"), page, "player cell")
        if cell.css_first("a.name") is None and (
            cell.css_first(".emptyplayer") is not None
            or "(empty)" in _norm(cell.text()).lower()
        ):
            continue  # an unfilled slot: Yahoo writes "(Empty)" in a div.emptyplayer
        entries.append(RosterEntry(_parse_player(cell, page), slot))
    if not entries:
        raise ParseError(page, "roster table has no players")
    return entries


# --- settings ---------------------------------------------------------------


def _settings_rows(tree: LexborHTMLParser) -> dict[str, str]:
    table = _need(tree.css_first("table#settings-table"), "settings", "#settings-table")
    rows: dict[str, str] = {}
    for tr in table.css("tr"):
        cells = tr.css("td")
        if len(cells) >= 2:
            rows[_norm(cells[0].text()).rstrip(":").strip()] = _norm(cells[1].text())
    return rows


def _setting(rows: dict[str, str], label: str, required: bool = True) -> str | None:
    """Value of the row whose label starts with ``label`` (prefix match)."""
    for key, value in rows.items():
        if key.startswith(label):
            return value
    if required:
        raise ParseError("settings", f"missing {label!r}")
    return None


def parse_settings(
    html: str, league_id: str, week: tuple[int, date, date]
) -> LeagueSettings:
    """League rules. Week info is not on this page; pass ``parse_week``'s result."""
    page = "settings"
    rows = _settings_rows(_tree(html))
    found_id = _setting(rows, "League ID#")
    if found_id != league_id:
        raise ParseError(page, f"page is for league {found_id!r}, not {league_id!r}")
    scoring_text = _setting(rows, "Scoring Type")
    scoring_type = _SCORING_TYPES.get(scoring_text or "")
    if scoring_type is None:
        raise ParseError(page, f"unsupported Scoring Type {scoring_text!r}")
    deadline = _setting(rows, "Weekly Deadline")
    lineup = _LINEUP_EFFECTIVE.get(deadline or "")
    if lineup is None:
        raise ParseError(page, f"unsupported Weekly Deadline {deadline!r}")
    name = _setting(rows, "League Name")
    if not name:
        raise ParseError(page, "empty League Name")
    waiver_type = _setting(rows, "Waiver Type", required=False)
    current_week, week_start, week_end = week
    return LeagueSettings(
        league_id=league_id,
        name=name,
        categories=_categories(_setting(rows, "Players Stat Categories") or ""),
        roster_slots=_roster_slots(_setting(rows, "Roster Positions") or ""),
        max_adds_per_week=_int(
            _setting(rows, "Max Acquisitions per Week") or "",
            page,
            "Max Acquisitions per Week",
        ),
        week_start=week_start,
        week_end=week_end,
        current_week=current_week,
        lineup_effective=lineup,  # type: ignore[arg-type]
        scoring_type=scoring_type,  # type: ignore[arg-type]
        waiver_type=waiver_type,
        faab=bool(
            waiver_type
            and re.search(r"\bFAAB\b|\bFAB\b|budget", waiver_type, re.IGNORECASE)
        ),
    )


def _categories(text: str) -> tuple[Category, ...]:
    cats: list[Category] = []
    for code in re.findall(r"\(([^()]+)\)", text):
        if code in _DISPLAY_ONLY_CODES:
            continue
        if code not in _CATEGORY_CODES:
            raise ParseError("settings", f"unknown stat category {code!r}")
        cats.append(_CATEGORY_CODES[code])
    if not cats:
        raise ParseError("settings", "no stat categories")
    return tuple(cats)


def _roster_slots(text: str) -> dict[str, int]:
    slots: dict[str, int] = {}
    for item in (part.strip() for part in text.split(",")):
        if not item:
            raise ParseError("settings", f"bad Roster Positions {text!r}")
        slots[item] = slots.get(item, 0) + 1
    if not slots:
        raise ParseError("settings", "no roster positions")
    return slots


# --- free agents ------------------------------------------------------------

_WAIVER = re.compile(r"W \((\w{3}) (\d{1,2})\)")


def _waiver_clear(text: str, today: date) -> date:
    match = _WAIVER.fullmatch(text)
    month = _MONTHS.get(match.group(1)) if match else None
    if match is None or month is None:
        raise ParseError("players", f"bad Roster Status {text!r}")
    try:
        clear = date(today.year, month, int(match.group(2)))
    except ValueError as exc:
        raise ParseError("players", f"bad waiver date {text!r}") from exc
    # A January date seen in December belongs to the next season year.
    if today - clear > timedelta(days=180):
        clear = clear.replace(year=today.year + 1)
    return clear


EMPTY_STATE_TEXT = "No players found"


def _players_table(tree: LexborHTMLParser) -> LexborNode:
    return _need(tree.css_first("table.Table-interactive"), "players", "players table")


def parse_free_agents(
    html: str, today: date, allow_empty: bool = False
) -> list[FreeAgent]:
    """Players listed on a players page.

    ``allow_empty`` (the waivers filter, often empty) turns two empty shapes into
    ``[]`` instead of a ParseError:

    * Yahoo's live empty state: no ``table.Table-interactive`` at all, but a
      ``#players-table`` container whose text says "No players found"
      (fixture ``waivers_empty_211727.html``).
    * a players table with no data rows, only while the page still carries the
      players filter form (``form.ysf-form`` with a ``status`` select).

    Anything else stays a ParseError, so a changed layout is still noticed.
    """
    page = "players"
    tree = _tree(html)
    table = tree.css_first("table.Table-interactive")
    if table is None:
        container = tree.css_first("#players-table")
        if allow_empty and container and EMPTY_STATE_TEXT in container.text():
            return []
    table = _players_table(tree)
    header = next(
        (
            [_norm(th.text()) for th in row.css("th")]
            for row in table.css("thead tr")
            if any(_norm(th.text()).startswith("Roster Status") for th in row.css("th"))
        ),
        None,
    )
    if header is None:
        raise ParseError(page, "missing 'Roster Status' column")
    status_col = _column(header, "Roster Status", page)
    free_agents: list[FreeAgent] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) <= status_col:
            raise ParseError(page, "player row has fewer cells than the header")
        player = _parse_player(
            _need(row.css_first("td.player"), page, "player cell"), page
        )
        status = _norm(cells[status_col].text())
        if status == "FA":
            free_agents.append(FreeAgent(player, on_waivers=False))
        else:
            free_agents.append(
                FreeAgent(
                    player, on_waivers=True, waiver_clear=_waiver_clear(status, today)
                )
            )
    if not free_agents:
        if allow_empty and tree.css_first('form.ysf-form select[name="status"]'):
            return []
        raise ParseError(page, "players table has no rows")
    return free_agents


def parse_taken_players(html: str) -> list[Player]:
    """Players on a players page filtered to taken (``status=T``): the Roster Status
    column names the fantasy team, which nothing here needs."""
    page = "players"
    table = _players_table(_tree(html))
    if not any(
        _norm(th.text()).startswith("Roster Status") for th in table.css("thead th")
    ):
        raise ParseError(page, "missing 'Roster Status' column")
    players = [
        _parse_player(_need(row.css_first("td.player"), page, "player cell"), page)
        for row in table.css("tbody tr")
    ]
    if not players:
        raise ParseError(page, "players table has no rows")
    return players


def has_next_page(html: str) -> bool:
    """True when the players page links to a 'Next 25' page."""
    nav = _need(
        _tree(html).css_first("ul#playerspagenav1"), "players", "page navigation"
    )
    return any(_norm(a.text()).startswith("Next") for a in nav.css("a"))
