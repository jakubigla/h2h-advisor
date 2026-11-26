# config.toml

Path: `~/.config/h2h-advisor/config.toml`, or `$H2H_ADVISOR_HOME/.config/h2h-advisor/config.toml`
when `H2H_ADVISOR_HOME` is set. No file means every default applies.

## Schema

An unknown key in a league table, or in `[limits]`, fails loading, and then every tool errors.

```toml
[global]
never_drop = ["Victor Wembanyama"]   # a list, never a string; merged into every league
# drop_margin = 0.25                 # ROS value margin above the best free agent for the drop pool
# light_day_max_games = 4            # days with this many NBA games or fewer are "light"
# rotation_mpg = 20.0                # recent minutes a night below which nobody is kept off the drop list
# rotation_gain_mpg = 3.0            # kept off it when recent minutes beat BBM's projection by this

[league."<yahoo league id>"]         # the id from list_leagues, quoted
never_drop = []                      # added to the global list for this league
# stream_slots = 5                   # drop pool = bottom 5 by ROS value
# max_adds = 7                       # only used if the settings page gives no max adds
# bbm_league_id = "1234"             # BBM league option value when the name match fails
# build = { strong = ["FT_PCT", "TPM"], punt = ["FG_PCT", "TO"], weak = ["REB"] }

# [limits]                           # daily page-load caps and the delay between loads
# yahoo_daily_pages = 250
# bbm_daily_pages = 80
# delay_min_s = 4.0
# delay_max_s = 9.0

# [ttl_s]                            # cache lifetimes in seconds: settings, matchup, roster,
# matchup = 1200                     # free_agents, bbm_day, bbm_week, bbm_ros, espn_minutes

# [aliases]                          # player name as one site spells it = canonical name
# "Nic Claxton" = "Nicolas Claxton"
```

- Category names: `FG_PCT`, `FT_PCT`, `TPM`, `PTS`, `REB`, `AST`, `STL`, `BLK`, `TO`.
- `build` keys: only `strong`, `punt`, `weak`; a category belongs to at most one of them.
- `never_drop` is season-long. Guidance that expires is a note (`add_note`), stored in
  `~/.local/share/h2h-advisor/notes.json`, never here.
- Keep the user's comments and key order when merging; add new leagues at the end.

## Validate

After writing, check the TOML, the keys and every pinned build's category names:

```bash
uv run --directory ${CLAUDE_PLUGIN_ROOT} python -c "from h2h_advisor.config import load_config; from h2h_advisor.analysis.build import build_from_config; c = load_config(); [build_from_config(lc.build) for lc in c.leagues.values()]; print('config OK:', sorted(c.leagues))"
```

`config OK: [...]` lists the league ids it read. Anything else is the error to fix.
