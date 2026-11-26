# Reading a dated plan (`plan_adds`)

The search is the tool's job; yours is to read it with the user's eyes and say what it means.
Adds are use-it-or-lose-it each week, so a throwaway drop (a bench player who would not have
started, below replacement level) is free: moves that use several adds are normal, and
streaming means the day's best player, then a swap for whoever fits the next days. Check
the plan against the lineup before presenting it:
- Open starting slots on a day with games (`starters per day` below the active slots) mean a
  player who plays that day should be coming in; if the plan leaves one open, say so and ask
  `plan_adds` again rather than accepting it.
- A player added for a single day should be dropped after that day, not carried; a plan that
  holds one add all week while a better Fri/Sun fit exists is a worse plan.
- A player who just returned from injury (`Off Inj`, `Q`) may not play: name that risk.
- Say why each move is dated when it is: the drop plays until then, or the add's first game.

Each move has `on` (the day to make it), `usable_days` (games the add gives you from then),
`drop_days_given_up` (what you lose by dropping him that day) and `gain` (win-probability
points it added when picked). Present them in date order: "Today: ...", "Sat: ...". Say what
today's moves are first; later ones are to prepare and may change as injuries and rest days
move. `claim: true` means the add is on waivers now: bid (FAB) or claim before he clears, or
add him after `waiver_clear` if he is still free. Waiting risks someone else taking him.
Past Sunday nothing is reliable (next week's schedule may not be set).

