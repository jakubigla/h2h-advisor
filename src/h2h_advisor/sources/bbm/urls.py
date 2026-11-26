"""Basketball Monster page addresses and control names, as found in docs/discovery.md.

BBM is an ASP.NET WebForms site: every control change is a postback to the same
page, so there is no URL per date, league or filter. These constants are the
page paths and the form-control names/values the session drives.
"""

from typing import Literal

BASE = "https://basketballmonster.com"

# Daily projections for one date, and rest-of-season projections. There is no
# weekly page: a week is the daily pages for its remaining days.
PROJECTIONS_PAGE: dict[Literal["day", "ros"], str] = {
    "day": f"{BASE}/dailyprojections.aspx",
    "ros": f"{BASE}/Projections.aspx",
}

# Date input on the daily page (ISO yyyy-mm-dd); setting it and posting back
# loads that day.
DATE_FIELD = "GAMEDATE"
# Row filter select; its postback reloads the table.
FILTER_CONTROL = "PlayerFilterControl"
# League select; switching it posts back and BBM then remembers that league.
LEAGUE_CONTROL = "pageTitleRowLeague"

# Option values of FILTER_CONTROL that we use.
FILTER_ALL = "AllPlayers"
FILTER_AVAILABLE_AND_MINE = "AvailableAndMyPlayers"  # "Top Free Agents+My"
FILTER_ALL_AVAILABLE_AND_MINE = "AllAvailableAndMyPlayers"  # "All Free Agents+My"
