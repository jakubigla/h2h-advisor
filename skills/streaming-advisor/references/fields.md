# Tool fields and notices

## Every response
- `ages`: per source `fetched_at` and `stale`. Say "Yahoo 12 min old, BBM 2 h old"; flag `stale: true`.
- `notices`: relay each, in plain words:
  - "browser busy in another session": another Claude session holds the browser; these are cached numbers.
  - "<site> needs login": the user runs `/h2h-advisor:setup`, or
    `! uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login` with the plugin's real path filled in,
    logs into Yahoo and Basketball Monster in the plain Chrome window, then quits it with Cmd+Q.
    Never try to log in yourself.
  - "set bbm_league_id for <league> (BBM leagues: ...)": the BBM league was not matched by name, so free
    agents came from Yahoo only; offer `/h2h-advisor:setup` to pin it. "BBM leagues: none loaded" means
    BBM failed this time: say to try again later, don't ask the user to choose.
  - "daily page-load cap reached": cached data only until tomorrow.
  - a backed-off site (403/429/5xx): it is resting for a while; cached data meanwhile.

## `daily_brief` per league
- `analysis`: the week (below). `null` with other fields present = the week could not be analysed
  (relay the notice; still show `upgrades` and `claims`). A `null` league entry failed entirely.
- `potential`: the best use of the adds left - `matchup_win_prob`, `adds_used`, dated `moves` (`on`).
- `streamers` (top 3) or `streamer_reason`; `upgrades` (up to 5); `claims`; `facts` (2-4 ready lines:
  usable games me vs opponent, light days, no-game days, close categories, kept-off-drop-list players).

## `analysis` (also `analyze_week(`)
- per category: `status` = `locked_win`, `likely_win`, `swing`, `tied`, `likely_loss`, `locked_loss`;
  `win_prob`; now and projected totals for both sides.
- `objective` (`one_win` or `categories`), `matchup_win_prob`, `expected_cats_won`, `targets` (one-win),
  `build` (`punt`, `strong`, `weak`), `at_risk_strong`, `adds_left`, `days_left`.

## Streamer options (`rank_streamers(`)
- `add`, `drop`, `usable_days` with `slot_by_day`, `raw_games` (not what counts), `light_days`
  (days with ≤ 4 NBA games: open slots are likeliest there).
- `delta_objective` (ranked on), `delta_expected_cats`, `adds_left_after`.
- `ros_cost_flag: true`: the drop is above replacement level - the stream costs long-term value.
- `follow_up`: a second stream later in the week with one more add; part of the same option.
- `reason` instead of options: "no adds left this week", "drop pool is empty" (everyone droppable is
  protected or too valuable), "no eligible free agent", "no move improves this week's outlook".

## Upgrades (`long_term_upgrades(`)
- `add`, `drop`, `ros_value_gain`, key categories, games this week, `ros_games` (low or missing =
  injured or few games left: the gain may not materialise - flag it).

## Claims (waivers)
- keyed by player key: `waiver_clear` (date or null), `faab` (true = a bid league).
