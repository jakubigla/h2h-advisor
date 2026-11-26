# Tool fields and notices

## Every response
- `ages`: per source `fetched_at` and `stale`. Say "Yahoo 12 min old, BBM 2 h old"; flag `stale: true`.
- `notices`: relay each, in plain words:
  - "browser busy in another session": another Claude session holds the browser; these are cached numbers.
  - "<site> needs login": the user runs `/h2h-advisor:setup`, or
    `! uv run --directory ${CLAUDE_PLUGIN_ROOT} h2h-advisor login` with the plugin's real path filled in,
    logs into Yahoo and Basketball Monster in the plain Chrome window, then quits it with Cmd+Q.
    Never try to log in yourself.
  - "<league>: Yahoo's list of rostered players is unavailable; who is free comes from Basketball
    Monster, which can lag behind Yahoo": normally Yahoo's rosters decide who is free. This time the
    list did not load, so an add shown may already be taken: say so next to every add.
  - "set bbm_league_id for <league> (BBM leagues: 30008 = <name>; ...)": only with the notice above:
    the BBM league (the fallback) was not matched by name either, so free agents came from Yahoo's
    first two players pages; offer `/h2h-advisor:setup` to pin it. "BBM leagues: none loaded" means
    BBM failed this time: say to try again later, don't ask the user to choose.
  - "daily page-load cap reached": cached data only until tomorrow.
  - a backed-off site (403/429/5xx): it is resting for a while; cached data meanwhile.

## `daily_brief` per league
- `analysis`: the week (below). `null` with other fields present = the week could not be analysed
  (relay the notice; still show `upgrades` and `claims`). A `null` league entry failed entirely.
- `potential`: the best use of the adds left (a `plan_adds` plan) - `matchup_win_prob`,
  `expected_cats_won`, `adds_used`, dated `moves` (`on`), `objective`: `categories` (it maximised
  expected categories in this week's chase categories; each move's `gain` is in categories) or `one_win` (matchup
  win probability; `gain` in probability).
- `streamers` (top 3) or `streamer_reason`; `upgrades` (up to 5); `claims`; `facts` (2-4 ready lines:
  usable games me vs opponent, light days, no-game days, close categories, kept-off-drop-list players,
  "Your notes" - the user's active notes with `#id`, until when and why).

## `analysis` (also `analyze_week(`)
- per category: `status` = `locked_win`, `likely_win`, `swing`, `tied`, `likely_loss`, `locked_loss`;
  `win_prob`; now and projected totals for both sides.
- `objective` (`one_win` or `categories`), `matchup_win_prob`, `expected_cats_won`, `targets` (one-win),
  `build` (`punt`, `strong`, `weak`), `at_risk_strong`, `adds_left`, `days_left`.

## Streamer options (`rank_streamers(`)
- `add`, `drop`, `usable_days` with `slot_by_day`, `raw_games` (not what counts), `light_days`
  (days with ≤ 4 NBA games: open slots are likeliest there).
- `delta_objective` (ranked on), `delta_expected_cats`, `adds_left_after`. In a categories
  league both count only this week's chase categories: punted ones and strong ones already
  safe weigh 0, and each category's win chance counts only inside 10-90% (1% to 5% TPM, or 94%
  to 99% STL, adds nothing; 8% to 40% REB counts from 10%). `per_cat_delta` stays raw, so it
  can still show a move in a decided category: don't quote that one.
- `drop_ros_value` / `add_ros_value`: rest-of-season value for this league's build (the mean of
  BBM's category values over the categories not punted), so a punt-PTS shot-blocker is not a
  throwaway just because BBM's overall value is low.
- `ros_cost_flag: true`: the drop is above replacement level - the stream costs long-term value.
  On a player the user noted `drop_first` it is still set: say the cost once, it is their call.
- `follow_up`: a second stream later in the week with one more add; part of the same option.
- `reason` instead of options: "no adds left this week", "drop pool is empty" (everyone droppable is
  protected or too valuable: a `drop_first` note opens it), "no eligible free agent", "no move
  improves this week's outlook".

## Notes (`add_note(`, `list_notes(`, `remove_note(`)
- A note: `id`, `kind` (`keep` | `drop_first`), `player`, `league` (id or `all`), `reason`,
  `created`, `until` (last day it counts). `add_note` returns the `note` and every `active` note.
- A new note on the same player and league replaces the old one (the user's last word counts).
  A `drop_first` note beats a `keep` note and `never_drop` for that player, and his remaining
  games this week count for nothing: the as-is outlook leaves them out, and an add that takes
  his place loses none of them.
- Notes write only a local file; nothing on Yahoo changes.

## Upgrades (`long_term_upgrades(`)
- `add`, `drop`, `ros_value_gain`, key categories, games this week, `ros_games` (low or missing =
  injured or few games left: the gain may not materialise - flag it).

## Claims (waivers)
- keyed by player key: `waiver_clear` (date or null), `faab` (true = a bid league).
