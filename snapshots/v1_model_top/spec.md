# OOD preference spec (fixed before any evaluation)

Scoring: hard rules first (official NeMo Gym grader). Hard fail = 0. Hard pass = 0.5 + 0.5 × (fraction of prefs met).

## A-type (ood_dev, visible to evolution) — `prefer_window`: event starts ≥ start AND ends ≤ end
| phrase family | window |
|---|---|
| early morning / first thing / before noon | 10:00–12:00 |
| afternoon | 12:00–16:00 |
| late in the day / end of my day | 14:00–16:00 |
| around lunchtime / midday | 11:30–13:30 |

## B-type (ood_test, never shown to the evolver)
- `keep_free(a, b)`: no event overlaps [a, b) — 30-min windows: 10:00–10:30, 12:00–12:30, 12:30–13:00, 15:30–16:00
- `right_after(first, second)`: second.start == first.end
- `order(first, second)`: first.end ≤ second.start

Every sample: all hard constraints + all prefs jointly satisfiable (reference solver), and the
plain no-preference reference schedule does NOT already satisfy all prefs.

## C-type (ood_dev_c, visible only to "diverse" evolution runs) — see spec_C.md
