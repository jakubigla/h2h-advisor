# Reading a dated plan (`plan_adds`)

Each move has `on` (the day to make it), `usable_days` (games the add gives you from then),
`drop_days_given_up` (what you lose by dropping him that day) and `gain` (win-probability
points it added when picked). Present them in date order: "Today: ...", "Sat: ...". Say what
today's moves are first; later ones are to prepare and may change as injuries and rest days
move. `claim: true` means the add is on waivers now: bid (FAB) or claim before he clears, or
add him after `waiver_clear` if he is still free. Waiting risks someone else taking him.
Past Sunday nothing is reliable (next week's schedule may not be set).

