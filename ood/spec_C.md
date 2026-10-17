## C-type (ood_dev_c, visible to evolution in diversity runs)
- `avoid_window(event, a, b)`: the event must not overlap [a, b) ("keep X out of 10:30-11:30", "X can't overlap 1:30 to 2:30")
- `min_gap(first, second, minutes)`: at least that many minutes between the two events, in either order

Scoring: hard rules first (official grader). Hard fail = 0. Hard pass = 0.5 + 0.5 x fraction of preferences met.
