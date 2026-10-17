## Hard data (dev_hard): free-form, weighted, sometimes conflicting preferences
Preferences are written in natural, varied language. Each has a strength: clearly important (weight 2) or
optional / "if possible" (weight 1). They can CONFLICT: often not all of them can be satisfied at once.
Types you can see here: event inside a time range; event must not overlap a range; at least N minutes between
two events; at most N minutes between two events. Other kinds of preferences exist and will appear in evaluation.

Scoring: hard rules first (official grader; hard fail = 0). Hard pass = 0.5 + 0.5 x (weight of preferences met /
best weight achievable for that sample). Satisfying a weight-2 wish beats satisfying a weight-1 wish.
