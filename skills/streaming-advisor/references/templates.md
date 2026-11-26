# Answer templates

Use markdown tables. Keep facts to 2-4 lines. End every answer with the data-age line.
Category names as the tools give them: `FG_PCT`, `FT_PCT`, `TPM`, `PTS`, `REB`, `AST`, `STL`,
`BLK`, `TO` (lower TO wins).

**(a) Status report, per league**

Take the facts from the brief's `facts` list (usable games left me vs opponent, light days left,
close categories, what this opponent usually wins); with `analyze_week(` alone there are no
facts, so derive light days and close categories from the analysis.

```markdown
### <League name> - vs <opponent>, <days left> days left, <adds left> adds left
As is: <win chance> | With <adds_used> stream(s): <potential.matchup_win_prob> (<moves: add X for Y>)
Objective: <one-win: 62% to win the week. Safe: PTS, TPM. You need 3 of the swing categories: AST, STL, FT_PCT> | <categories: 5.4 expected categories won>

| Cat | Me now | Opp now | Me proj | Opp proj | Win % |
|---|---|---|---|---|---|
| FG_PCT | .471 | .455 | .468 | .462 | 61% |
...
- Usable games left: me 21, opponent 18 (+3)
- Light days left: Thu (3 games), Sun (4 games)
- Close categories: BLK (52%), TO (47%)
- At-risk strong category: STL (swing this week)
Data: Yahoo 8 min old, BBM 1 h old.
```

**(b) Moves**

```markdown
#### This week
| # | Add | Drop | Usable days (slot) | Light days | Categories moved | Adds left after |
|---|---|---|---|---|---|---|
| 1 | Hot Pickup (GSW, SF/PF) | Bench Wing | Thu (F), Sat (Util) | Thu | +PTS, +TPM, -FG_PCT (+0.4 cats) | 2 |
| 2 | Waiver Guy - on waivers, clears Fri (claim) | Spare Guard | Sat (G) | - | +AST (+0.2 cats) | 2 |

#### Long-term upgrades
| Add | Drop | ROS gain | Key categories | Games this week |
|---|---|---|---|---|
| Steady Big (ATL, C) | Bench Wing | +1.3 | +REB, +BLK | 2 |
```

An empty list keeps its heading with "none qualify" (plus the `reason` / `streamer_reason` for
streamers). Then one line per option on the trade-off (cost: add used, who is dropped,
`ros_cost_flag`; gain: categories moved). If nothing beats negligible, say "no move worth an add
today".

**(c) All-leagues brief** (`daily_brief(leagues="all")`)

One compact block per league:

```markdown
### League A - vs Rivals, 58% to win the week, 4 days left, 3 adds left
Winning: PTS 81%, TPM 77% | Swing: AST 55%, STL 49%, FT_PCT 46% | Losing: FG_PCT 12%, TO 20%
- Usable games left: me 21, opponent 18 (+3); light day Thu
- Best stream: add Hot Pickup, drop Bench Wing - Thu (F), Sat (Util), +3 pts win % (2 adds left after)
- Best long-term upgrade: add Steady Big, drop Spare Guard - ROS +1.3 (+REB, +BLK), 2 games this week
```

Write "none qualify" for a missing stream or upgrade. For a status-only question, drop the two
move lines. Then a cross-league summary:

```markdown
| League | Win chance as is -> with streaming | Adds left | Best stream | Best long-term upgrade |
|---|---|---|---|---|
| League A | 58% | 3 | Hot Pickup for Bench Wing (+3 pts) | Steady Big for Spare Guard (+1.3 ROS) |
| League B | 5.1 cats | 0 | none (no adds left) | none qualify |
```

A league whose entry is null failed entirely: name it and relay its notice. An entry whose
`analysis` is null but has other fields means the week could not be analysed (relay the notice);
still show any `upgrades` and claims it has, and say the status table is unavailable.
