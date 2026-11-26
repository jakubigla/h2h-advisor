# Discovery: what the Yahoo and Basketball Monster pages look like

Captured 2026-11-25 (fantasy week 6) from a logged-in session, GET and approved
postbacks only, guard on. Sanitised copies live in `tests/fixtures/yahoo/` and
`tests/fixtures/bbm/` (see "Fixtures" at the end). Parsers (tasks 13, 14) are
written against those files. All URL builders are in
`src/h2h_advisor/sources/yahoo/urls.py` and `src/h2h_advisor/sources/bbm/urls.py`.

Locate table columns by header text, never by index: both sites add and remove
columns per league and per view (examples below). Normalise whitespace first (labels can
contain non-breaking spaces, for example `Roster\xa0Positions:` on the settings page) and
match headers by prefix after normalising: some header cells carry extra text, such as the
BBM `GAME` header (its tooltip texts are part of the cell text) and the Yahoo players page
`Current` header, which ends with an icon character (`\ue002`).

## My leagues and teams

| League id | Team id | League name | Scoring type | Max adds/week | Waivers |
|---|---|---|---|---|---|
| 51347 | 9 | League 1 | Head-to-Head - One Win | 5 | FAB w/ continual rolling list tiebreak, 2 days |
| 211727 | 9 | League 4 | Head-to-Head - Categories | 5 | continual rolling list, no waivers (free agents add immediately) |
| 259619 | 9 | League 5 | Head-to-Head - Categories | 5 | continual rolling list, no waivers |
| 57243 | 6 | League 2 | Head-to-Head - Categories | 7 | FAB w/ continual rolling list tiebreak, 2 days |
| 59422 | 7 | League 3 | Head-to-Head - Categories | 5 | continual rolling list, 1 day |

League and team names are placeholders here and in the fixtures (see "Fixtures").
All five: my team is "Team 1", weekly deadline "Daily - Today",
9 categories (FG%, FT%, 3PTM, PTS, REB, AST, ST, BLK, TO), "Max Acquisitions for
Entire Season: No maximum". Roster positions: 51347, 211727, 259619 are
`PG, SG, G, SF, PF, F, C, Util, Util, Util, BN, BN, BN, IL`; 57243 and 59422 are
`PG, SG, G, SF, PF, F, C, Util, BN, BN, BN, BN, BN, IL`.

Basketball Monster (BBM) league switch values (`pageTitleRowLeague`):

| Value | BBM label | Yahoo league |
|---|---|---|
| 1234 | `_1. League 1 (Yahoo!)` | 51347 |
| 1235 | `_2. League 2 (Yahoo!)` | 57243 |
| 1236 | `_3. League 3 (Yahoo!)` | 59422 |
| 1237 | `_4. League 4 (Yahoo!)` | 211727 |
| 1238 | `_5. League 5 (Yahoo!)` | 259619 |

Values are BBM's own league ids (the ones above are made up; the fixtures keep the real
option values because the tests select by them). The label is BBM's ordinal, the league
name as BBM spells it (which can differ from Yahoo's, e.g. a Roman `I` for a `1`) and the
platform. The select has eight more options for other (non-NBA-fantasy-relevant) leagues,
numbered without the leading `_`; we never switch to them.

## Yahoo

Base `https://basketball.fantasysports.yahoo.com`. Every page below is plain
server-rendered HTML fetched with GET. The site's own scripts come from foreign
hosts that the guard blocks, and the pages still contain all the data, so no
JavaScript is needed and no Yahoo POST is needed.

### Home (league list): `GET /nba`

Fixture `yahoo/home.html`.

- Current week: `#home-myleagues h1 span` text `My Teams and Leagues - Week 6`.
- One block per league inside `#gamehome-teams`: `h3 > a[href="/nba/<league>"]` is
  the league name; the `li.Listitem` below holds my team and the opponent.
- The `li` carries `data-target="/nba/<lg>/matchup?week=6&mid1=<my team>&mid2=<opp team>"`
  (same href in the "View Matchup" link). `mid1` is my team id, `mid2` the opponent's.
- Team links: `a[href$="/nba/<lg>/<team id>"]` with the team name as text. Under each
  name `div.F-shade` shows `W-L-T | rank`, for example `3-2-0 | 8th`.
- Live score for the current week: the two `div.Fz-lg` numbers either side of the
  `vs` span (for example `6 vs 3`). In a One Win league this is categories won.
- Team ids differ per league (see the table), so ids are always read from here.

### Team page: `GET /nba/{league}/{team}`

Fixtures `yahoo/team_51347.html`, `yahoo/team_57243.html`. Optional params the page
itself uses: `?date=YYYY-MM-DD&week=6` (the date nav links, `#selectlist_nav`).
Without them you get today's roster.

- Roster table: `table#statTable0` (class `ysf-rosterswapper`), 14 rows in `tbody`
  (7 position slots, Util x1-3, BN x3-5, IL).
- Two header rows. Second row texts in order: `Pos`, `Edit`, `Players`, `Opp`,
  `Pre-Season`, `% Start`, `% Ros`, `MIN*`, `FGM/A*`, `FG%`, `FTM/A*`, `FT%`, `3PTM`,
  `PTS`, `REB`, `AST`, `ST`, `BLK`, `TO`, then an empty spacer. Group row above:
  `Rank`, `Fantasy` (colspan 2), `Time`, `Field Goals`, `Free Throws`, `3PT`.
  `Pre-Season` is the preseason rank, `% Start`/`% Ros` are Yahoo's start/roster percentages.
  Stat cells are `-` / `-/-` until a game has been played that day.
- Per row: `td.pos` slot label (`span.pos-label[data-pos]`; the `Edit` column select
  also has `name=<yahoo player id>`); player cell `td.player`:
  `a.name` (player name, `data-ys-playerid` is the Yahoo player id, href
  `https://sports.yahoo.com/nba/players/<id>`), `span.D-b > span.Fz-xxs` with
  `<TEAM> - <pos,pos>` (for example `GSW - PG,SG`), and
  `.ysf-player-detail .ysf-game-status` with today's game (`4:00 am vs SAC`,
  `2:30 am @ HOU`; empty when no game). The `Opp` cell holds the opponent code
  (`SAC`, `@HOU`; `@` means away).
- Injury status: `span.ysf-player-status > span[title]`, text a code (`O` Out,
  `D` Doubtful, `Q` Questionable, `INJ` Injured (IL-eligible), `NA`, `GTD`; the title has the long word).
  Select it by class. The legend of the status codes is in a small table further down the page.
- Opponent and week: `#team-card-matchup`, text `Week 6 vs <a href="/nba/<lg>/<opp id>">Team 2</a> * 14th`,
  link `data-target="/nba/<lg>/matchup?week=6&mid1=<me>&mid2=<opp>"`, live score in the two `div.Fz-lg`.
  My record and rank: `div.right span.Fw-b` (`3-2-0`) and `em` (`8th Place`).
- Moves used: `section#teamnotes` has `You have used <b>2</b> of your <b>5</b> player adds for this week.`
  and `You have used <b>1</b> of <b>1</b> IL positions on your roster.` (plain text
  `You have used 2 of your 5 player adds for this week.`). It also shows `Waiver Budget: $73`
  in FAB leagues. The settings page, not this sentence, is the source of truth for the
  maximum (`Max Acquisitions per Week`).
- Roster Status (`W (Nov 26)`, `FA`) is NOT on this page, only on the players page.
- The remaining-games counter is on the matchup page, not here (see `#remaining-games` below).
- The page contains `form#roster-edit-form` (`action="/nba/<lg>/<team>/editroster"`).
  We never submit it; the guard blocks it.

### Matchup: `GET /nba/{league}/matchup[?week=N]`

Fixture `yahoo/matchup_51347.html`. Week param is `week` (checked on the page's
own links: `?week=5&module=matchup&mid1=9`, and the week select options
`?week=1..N&module=matchup&mid1=9`). With no `week` the current week loads;
`mid1`/`mid2` pick the two teams and default to me and my opponent. Day views
add `date=YYYY-MM-DD` (`?week=6&date=2026-11-25&mid1=9&mid2=13`) and
`date=totals` for the week total.

- Week label: `span#matchup_selectlist_nav[title="Week 6"]` (`.flyout-title`).
  Previous/next week links `a.Js-prev`/`a.Js-next` with `?week=<n>&module=matchup&mid1=<me>`.
- Score block: first `table.M-a`; last row is `<td>6</td> vs <td>3</td>` (categories won so far).
- Category table (second `table`, class `Datatable`): header `Team`, `FGM/A*`, `FG%`,
  `FTM/A*`, `FT%`, `3PTM`, `PTS`, `REB`, `AST`, `ST`, `BLK`, `TO`, empty (score).
  Two body rows, mine first: team name link, then `48/120`, `.400`, `22/28`, `.786`, `24`,
  `142`, `43`, `35`, `12`, `8`, `17`, and the score `6`. A category winner's cell has classes
  `Fw-b Bg-selected`; for TO the lower number wins (17 vs 11, the opponent wins it).
- Per-player table `table#statTable3`: my roster on the left, opponent roster on the right
  (header `Pos`, `Player`, `FG%`, `FT%`, `3PTM`, `PTS`, `REB`, `AST`, `ST`, `BLK`, `TO`,
  spacer, then the same again). The second header row has `Pos` twice in a row
  (`..., '', 'Pos', 'Pos', 'Player', ...`): the right-hand block starts with an extra `Pos`
  cell, so do not map columns by header text alone across the two halves; split the row at the
  empty spacer first. It carries FG%/FT% only, not makes/attempts.
  Use the category table for FGM/A and FTM/A.
- Games remaining: `div#remaining-games`. Its text reads
  `27 Remaining 11/38 Games Played 10/34 Remaining 24`. Value spans (those without class
  `F-shade`), in document order: mine remaining (`27`), mine played/total (`11/38`),
  opponent played/total (`10/34`), opponent remaining (`24`). The `F-shade` spans are labels.
- The last `Tst-table` is the manager comparison (Level, Rating, Record, ...) and holds
  manager names. In the fixture those names are replaced with `Manager N`. Do not parse it.

### Settings: `GET /nba/{league}/settings`

Fixtures `yahoo/settings_<id>.html` for 51347, 211727, 259619, 57243, 59422.

- `table#settings-table`: each `tr` has `td` (label, with a trailing colon) and `td`
  (value). Labels used: `League ID#:`, `League Name:`, `Scoring Type:`
  (`Head-to-Head - One Win`, `Head-to-Head - Categories`), `Max Acquisitions per Week:`
  (a number; the add cap for the week), `Max Acquisitions for Entire Season:`
  (`No maximum`), `Waiver Type:`, `Waiver Time:`, `Weekly Deadline:` (`Daily - Today`),
  `Roster Positions:` (comma list), `Players Stat Categories:` (comma list of
  `Name (CODE)`), `IL-eligible statuses:`, `Playoffs:`.
- Do not follow the `editleaguesettings` link in the header (guard blocks it).

### Players / free agents: `GET /nba/{league}/players`

Fixture `yahoo/players_51347.html` (`status=A`, season average view). The page's
own filter form is `form[action="/nba/<lg>/players"][method=get]`:

| Param | Meaning | Values seen |
|---|---|---|
| `status` | availability | `ALL`, `A` (all available), `PON_<date>` (available that day), `STATE_IL`, `FA` (free agents only), `W` (waivers only), `K`, `WL` (watch list), `T` (taken) |
| `pos` | position radio | `P` (all players, the default), `PG`, `SG`, `G`, `SF`, `PF`, `F`, `C`, `Util` |
| `eteam` | NBA team | `ALL`, `ET_<n>` |
| `fteam` | fantasy team | `NONE`, or a team id (shows that roster) |
| `stat1` | stat view | `S_AS_2026` (season avg), `S_PS7`, `S_PSR` (remaining games projected), `S_L7`... |
| `sort` / `sdir` | sort column and direction | `AR` (actual rank), `NAME`, ...; `sdir=0/1` |
| `myteam` | my team only | `0`/`1` |
| `count` | offset into the list | `25`, `50`, ... |

`free_agents()` sends `status=A`, optionally `pos=`, and `count=` for pages after the
first. Other params (`stat1`, `sort`, `cut_type`) keep their defaults.

- Pagination: 25 players per page, offset in `count` (the offset, despite the name).
  `ul#playerspagenav1` holds `Previous 25` / `Next 25`; the next link is
  `.../players?status=A&eteam=ALL&fteam=NONE&pos=P&cut_type=33&stat1=S_AS_2026&myteam=0&sort=AR&sdir=1&count=25`.
  Stop when there is no `Next 25` link (it is plain text instead).
- Table: first `table` on the page, class `Table-interactive`, 25 rows in `tbody`. Second
  header row: `` `, `` (two action icons), `Players`, `Opp: 11/25`, `Roster Status`, `GP*`,
  `Pre-Season`, `Current`, `% Ros`, `MPG`, `FGM / A*`, `FG%`, `FTM / A*`, `FT%`, `3PTM`, `PTS`,
  `REB`, `AST`, `ST`, `BLK`, `TO`. Player cell as on the team page (`a.name`,
  `data-ys-playerid`, team-position span, injury status span).
  This page's stat cells are per game averages and the makes/attempts columns hold
  decimals (`3.0/5.0`).
- Roster Status text: `FA` (free agent) or `W (Nov 26)` (on waivers until that date).
  Seen: `FA`, `W (Nov 26)`, `W (Nov 27)`, `W (Nov 28)`. In leagues without waivers
  there is no `W`.
- The first action cell links to `/nba/<lg>/addplayer?apid=<id>`. Those are links in the
  markup only; we never request them (the guard denies add URLs).

## Yahoo requests that are blocked, and that is fine

During discovery the page scripts tried these. The guard blocked all of them and every
page rendered completely anyway.

- `POST https://basketball.fantasysports.yahoo.com/sports_ms/_rcv/remote`: widget calls,
  body not JSON with an operationName (and, in login mode, any POST). 8 attempts, all blocked.
- Telemetry and ads to foreign hosts: `POST geo.yahoo.com/p`, `/c`,
  `nexus-gateway-prod.media.yahoo.com`, `udc.yahoo.com/v2/public/yql`,
  `apis.mail.yahoo.com/ws/v3/batch`, `pbd.yahoo.com/data/logs/benji`, and `GET`
  scripts on `sp.yimg.com`, `edge-mcdn.secure.yahoo.com`, `jsapi.login.yahoo.com`,
  `pub-api.fantasysports.yahoo.com`, ad hosts.
- The roster form `POST /nba/{league}/{team}/editroster` (never submitted).
- No read-only Yahoo POST or GraphQL call is needed, so none is allowlisted.

Yahoo team codes seen in the pages (player cell and `Opp` column): ATL, BKN, BOS, CHA,
CHI, CLE, DAL, DET, GSW, HOU, IND, LAC, LAL, MEM, MIA, MIL, MIN, NOP, NYK, OKC, ORL, PHI,
PHX, POR, SAC, SAS, TOR, UTA, WAS. All are already canonical (DEN does not occur in the
captured rosters; it is canonical too).

## Basketball Monster

Base `https://basketballmonster.com`. ASP.NET WebForms: one form, `form#form1`,
`method=post`, `action=./dailyprojections.aspx` (or `./Projections.aspx`). Controls
reload the page by posting the form back. There is no URL per date, league or filter,
so a page load is `GET` and every change is an approved postback. BBM remembers the
selected league (see below); always set what you need explicitly.

### Pages

| Page | Constant | Fixture | Purpose |
|---|---|---|---|
| `/dailyprojections.aspx` | `PROJECTIONS_PAGE["day"]` | `bbm/daily_2026-11-25.html` | projections for one date |
| `/Projections.aspx` | `PROJECTIONS_PAGE["ros"]` | `bbm/ros.html`, `bbm/ros_league.html` | rest-of-season projections |

There is no week page. A week is the daily page loaded once per remaining day
(`GAMEDATE` postback).

### Form controls

| Name | Kind | Constant | Notes |
|---|---|---|---|
| `GAMEDATE` | `input type=date`, ISO `yyyy-mm-dd` | `DATE_FIELD` | daily page only. Set it and post back (event target `PlayerFilterControl`) to load any date; `d1` (2026-11-27) loaded this way. |
| `PlayerFilterControl` | `select` | `FILTER_CONTROL` | row filter, `onchange` postback |
| `pageTitleRowLeague` | `select` | `LEAGUE_CONTROL` | league, `onchange` postback |

`PlayerFilterControl` option values: `TopPlayers` (Only Top Players), `AllPlayers`
(`FILTER_ALL`), `MyPlayers`, `AvailablePlayers` (Free Agents), `AvailableAndMyPlayers`
(Top Free Agents+My, `FILTER_AVAILABLE_AND_MINE`), `AllAvailableAndMyPlayers`
(All Free Agents+My, `FILTER_ALL_AVAILABLE_AND_MINE`), `Rookies`. The three constants
were verified against the select options in the fixtures.

Approved postbacks (guard rules): `__EVENTTARGET` is `PlayerFilterControl` or
`pageTitleRowLeague`, with the whole form posted back as rendered. The driver may only
change `GAMEDATE`, `PlayerFilterControl` and `pageTitleRowLeague`. `DateNextButton` and
`DateBackButton` (submit buttons) are allowed as the event as well.

League switch persists: after posting `pageTitleRowLeague=1234` on the ROS page, the
next plain GET of `Projections.aspx` came back still on league 1234 (the selected
option of `pageTitleRowLeague` in that response is `_1. League 1`).
BBM stores the league on the account, so a source must set the league explicitly on
every fetch, and restore or re-set it afterwards; never assume the page is in the
state the last call left it.

### Games table (daily page)

The daily page lists the day's games above the player table, under `h3` "Games for
<weekday>" (9 games on 2026-11-25 in both daily fixtures). Never select either table by class:
in the first fixture only the player table is `table.modern-stats-table`, in the live page of
2026-11-25 (`bbm/daily_2026-11-25_live2.html`, which also has no `$` column) both are. The
parser finds the player table by the header cells `Name`, `Team`, `g` and the games table by
`Game` and `Time` (without `Name`); no match or several matches is a `ParseError`.
Header cells: `#`, (blank checkbox column), `Game`, `Time`, (blank odds column). It is found by
its `Game` header, never by position. Each row's `Game` cell reads `AWAY @ HOME` in BBM team
codes (`MIA @ PHI`, `PHO @ HOU`), canonicalised through `identity.team_code` (`PHO` -> `PHX`,
`NOR` -> `NOP`). The parser (`BbmPage.games`) is the only source of the NBA schedule: the
engine's per-day team set is the teams in each loaded day page's games, so a week needs no
extra page load. The NBA schedule CDN (`cdn.nba.com`) answers automated clients with `403`
(bot protection) and is not used.

Assumption, no fixture: a day without games (e.g. Thanksgiving) renders an empty games table
or the text "no games"; the parser reads either as no games that day. A page with neither the
table nor that text is a `ParseError`. Another assumption: games already under way stay listed. If they do not, `BbmPage.uncovered_teams`
(teams with a player at `g >= 1` that no game lists) is non-empty: for today's page the service
adds those teams to the day's schedule with a notice, a future page is rejected as unreadable.

### Table (both pages)

`table.modern-stats-table` (one per page; on the daily page it sits in `div.results-table`,
on the ROS pages directly in `div.container-fluid`, so do not select through the wrapper),
40 or 46 header cells. The header row is repeated inside `tbody` after every 16 data rows
(`tbody` row indices 16, 33, 50, ...); skip any `tr` containing `th`. Data rows have as many `td` as the header has `th`.

Daily (`dailyprojections.aspx`, 40 columns): `Own`, `Rank`, `Adv%`, `Adv+-`, `Value`,
`$`, `GAME` (monster bar, decoration), `Name`, `Team`, `Pos`, `Inj`, `Inj Risk`,
`Status`, `g`, `b2b`, `Analysts`, `m/g`, `Opp`, `Ease`, `pts`, `3`, `reb`, `ast`,
`stl`, `blk`, `fg%`, `fga`, `ft%`, `fta`, `to`, `USG`, `pV`, `3V`, `rV`, `aV`, `sV`,
`bV`, `fg%V`, `ft%V`, `toV`.
Stats are for that one day. `fg%`/`ft%` come together with `fga`/`fta` (attempts), so makes are
`pct * attempts`, never an average of percentages. `Opp` is `@ POR` away or `SAC` home. `g` is 1
when the player has a game that day.

Rest of season (`Projections.aspx`, 46 columns): `Own`, `Round`, `Rank`, `Adv%`,
`Adv+-`, `Value`, `1W+-`, `$`, `JoshG`, `JoshT`, `BonusG`, `BonusT`, `FrV`, `GAME`, `Name`,
`Team`, `Pos`, `Inj`, `Inj Risk`, `Status`, `Role`, `g`, `b2b`, `Analysts`, `m/g`, `p/g`,
`3/g`, `r/g`, `a/g`, `s/g`, `b/g`, `fg%`, `fga/g`, `ft%`, `fta/g`, `to/g`, `USG`, `pV`,
`3V`, `rV`, `aV`, `sV`, `bV`, `fg%V`, `ft%V`, `toV`. Per-game numbers; `g` is games
remaining. In `ros_league.html` (the first league of the switch) there is an `Owner` column right after
`Own` and no `$` column (still 46 columns). The two columns come and go with the
league, hence "find columns by header".

Cell details:

- `Name`: `td > a[href="playerinfo.aspx?i=<bbm id>"]`.
- `Team`: BBM codes. Non-canonical ones seen: `PHO` (Phoenix) and `NOR` (New Orleans);
  `identity.TEAM_ALIASES` maps `PHO` to `PHX` and, added in this task, `NOR` to `NOP`.
  All other codes (ATL, BKN, BOS, CHA, CHI, CLE, DAL, DEN, DET, GSW, HOU, IND, LAC, LAL,
  MEM, MIA, MIL, MIN, NYK, OKC, ORL, PHI, POR, SAC, SAS, TOR, UTA, WAS) are canonical.
- `Pos`: positions concatenated without separator (`SFPF`, `C`).
- `Inj`: injury label in `span.status-square` (`Q`, `D`, `OUT`, `INJ`, `Off Inj`, `GLG`, ...,
  optionally followed by a "<n>g" games-missed suffix; the title has the long text). The
  `Status` column is not the injury label.

### Own column

First `td` of each row. Findings from the three fixtures (counts in parentheses are
for `bbm/daily_2026-11-25.html`):

- Empty text and no `span[title]`: a free agent, available now (168).
- `WW` in `small[title=waivers]`: on waivers, not addable immediately (12).
- A `span[title="Team 1"]` (green background `#339900`, empty text): one of my
  players on my active roster (8). The title is my team name in the selected league.
- `IR` in `small.ir` plus the same span (lime background `#99FF33`): my player on my IL slot (1).

Note the brief for this task described blank as "mine or free agent". More exactly, mine is
the one carrying the team-name span, so tell the two apart by the span.
With filter `Top Free Agents+My` / `All Free Agents+My` the table lists only my players,
free agents and waiver players, never other teams' players.

### Request log notes

Nine BBM page loads were logged over the discovery session (five daily, four ROS), all
allowed, plus the approved postbacks. Blocked requests (images, bootstrap/jQuery from
`cdn.jsdelivr.net`, the Font Awesome kit, ad hosts) are fine; the tables are server-rendered
and complete. No BBM POST was blocked.

## Fixtures

Produced by `scripts/sanitize_fixture.py` (`uv run python scripts/sanitize_fixture.py
<yahoo|bbm> SRC DST [--name NAME ...] [--map NAME=LABEL ...]`) from the raw logged-in pages,
which are not kept. The script is idempotent: the committed fixtures were re-run through it
in place, with `--map` so that league and own-team labels agree across all pages.

- Removed: `script`, `style`, `noscript`, `svg`, `iframe`, `link`, `meta`, HTML comments.
  A single `<meta charset="utf-8">` is put back so the files decode on their own.
- Blanked to `X`: `__VIEWSTATE*`, `__EVENTVALIDATION`, any input named `*crumb*`, and `crumb=`
  query values.
- Emails become `user@example.com` (image names like `chat@6x.png` are left alone).
- Manager names (profile links `profiles.sports.yahoo.com/profile/<slug>`, `span.user-id`,
  names passed with `--name`) become `Manager N` / `manager-N`, stable within a file.
- Yahoo: the global header (`#yspcontentuh`: account menu, avatar, nav) is dropped.
- BBM: the top navbar (`nav.navbar`, has the account menu) is dropped, and the "monster bar" in each
  data row (`td div.range-badge`, about twenty tooltips per cell) is emptied. The cell and all
  other columns are untouched, so every table keeps its row and column counts. Without this
  the daily page is 1.2 MB.
- League names become `League N` and fantasy team names `Team N` (Yahoo: league and team
  links, the team-page title, the players `fteam` filter, the settings "League Name" row; BBM:
  the `pageTitleRowLeague` options and the Own/Owner columns), and the same names are replaced
  everywhere else on the page, never inside player links. The Yahoo custom league URL becomes
  `.../league/example`. In the fixtures League 1..5 are the leagues in the table above and
  `Team 1` is my team in every league.
- Player names, stats, option values and other form options are left as captured.
