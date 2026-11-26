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

Chain the adds across the days left. The planner already chains (it can drop an earlier
planned add once his games are played), and in a categories league it maximises expected
categories, so a matchup that is already won still gets moves for the categories in play.
Present the chain day by day: Fri's add, then Sat's or Sun's swap, each add dropped once his
games are played and replaced by whoever fits the next days. Name how many adds the chain uses
and how many stay unused, and say why when a day has no worthwhile swap. A chain that leaves
adds unused with open slots on days with games is the same fault as an open slot.
Drops in a chain are throwaways or players the user noted `drop_first`; a player the user said
to keep is never one once noted. If the user names a keep or a drop in conversation, `add_note`
first, then call `plan_adds` so the plan follows it.

Each move has `on` (the day to make it), `usable_days` (games the add gives you from then),
`drop_days_given_up` (what you lose by dropping him that day) and `gain` (what it added when
picked, in the plan's `objective` units: expected categories in a `categories` plan,
win-probability points in a `one_win` plan). Present them in date order: "Today: ...", "Sat: ...". Say what
today's moves are first; later ones are to prepare and may change as injuries and rest days
move. `claim: true` means the add is on waivers now: bid (FAB) or claim before he clears, or
add him after `waiver_clear` if he is still free. Waiting risks someone else taking him.
Past Sunday nothing is reliable (next week's schedule may not be set).

