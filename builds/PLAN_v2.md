# Evolving Calendar v2: required features → evolved web app by 10 PM

Goal: the web app described in the design doc, with every required feature working and demonstrable. Method: sea multi-agent evolution, with Claude allowed as a worker.

## Required features (design doc Must/Key flows + requirements Must (demo))

| # | Feature (doc id) | Status now | Who builds it | How it is accepted (mechanical, frozen harness v2) |
|---|---|---|---|---|
| 1 | Typed command create/update/delete/query with vague time (P1, P2, Flow 1) | create ✓; update/delete/query ✗ | evolved team | "move my dentist to next week" moves exactly that event; "what's on Thursday?" lists it; "around noon" keeps a window |
| 2 | Brain dump (F1) | ✓ | — | existing checks |
| 3 | Bulk change as preview diff + one confirm + one undo (P3, E12, Flow 1.3-1.4) | ✗ | evolved team | "empty out my weekend for a business trip" → preview lists the exact affected ids, nothing changes until apply, apply → gone + trip added, undo → restored |
| 4 | .ics import with dedupe and preview (P4, P7) | ✗ | evolved team | fixed .ics file → preview → apply adds N, re-import adds 0, source = "ics" |
| 5 | Complexity check → breakdown chat, why first, ≤15-min first step, tree (P28-P32, F5) | ✓ | — | existing checks + a step can be broken down again (parent_id chain) |
| 6 | Focus screen, next tiny step (F2) | ✓ | — | existing checks |
| 7 | Reminders, auto-postpone with kind wording + undo, check-in on the 3rd postpone (P22-P25, E16-E18, Flow 3) | ✓ | — | existing checks + the check-in reply "wrong time" moves the task |
| 8 | Interaction log of every action (E1) | partial | evolved team | GET /api/log has typed entries for each action the harness took, timestamped on the simulated clock |
| 9 | Habit learner: beliefs with confidence + evidence ids (P8, E2, Flow 2.1) | ✗ | evolved team | after seeded history (e.g. 8 evening completions, 0 weekend events), GET /api/beliefs holds a matching belief with confidence ≥ 0.7 and evidence ids from the log; a counter-signal lowers it |
| 10 | Proposal gate + view proposal citing the habit and its number (P9, P18, P19) | ✗ | evolved team | a belief over threshold → exactly one proposal whose reason contains the number; nothing changes in the view until accept; dismissed → not re-proposed |
| 11 | Accept / dismiss / undo proposal; revert lowers confidence (P10) | ✗ | evolved team | accept → state.view changes; undo → back; the belief's confidence drops after a revert |
| 12 | "What I've learned" panel, deletable beliefs (P11) | ✗ | evolved team | DELETE /api/beliefs/<key> works; the UI has element id `learned` |
| 13 | View switching as JSON specs, no hidden events (P13, P17, E11) | ✗ | evolved team | GET /api/state.view is a spec (month/week/workweek/day); a filtered view reports overflow counts for hidden items |
| 14 | Meta-learner: meta-preferences, prompt "learned" section versions, replay check, rollback (E3, E4, E6, E7, Flow 4) | ✗ | evolved team | 2 reverts of novel views → novelty preference drops, a new prompt version with diff + reason is listed at /api/prompts, the core section is unchanged, rollback restores the previous version, and the next proposal is a classic view |
| 15 | Activity panel / event stream (E9, SSE) | ✗ | evolved team | GET /api/activity lists belief updates, proposals and prompt rewrites; the UI has element id `activity` |
| 16 | Offline mock mode (E15) | partial | evolved team | the harness runs a split with network disabled (EVOLVE_MOCK=1), and the core flows still pass |
| 17 | UI: chat box, timeline, focus card, learned panel, activity, proposal card | partial | evolved team | element ids present; plus a human look at 2-3 screenshots (not scored) |

Out of scope tonight (the doc's own cut list or not checkable): Gmail import (P6), animation quality (P14-P16: checked only by eye), drag-to-reschedule, F3/F4/F6-F12 (Should/Stretch), and porting into tslmy/cal (each team builds its own, as the user said).

## How
1. **Contract v2 + harness v2 + scenarios v2** in a NEW spec dir `builds/evolving_calendar_v2/`. The v1 harness stays frozen and unchanged; v2 = all v1 checks + the new ones above. fit and holdout are both extended. **This is an evaluator change → needs your OK.**
2. **Calibrate** with an extended reference app (hand-written, not shown to teams). v2 is accepted only if the reference scores ≈1.0 and a stub scores ≈0.
3. **Evolve**: 3 runs in parallel, k=6, 2 builds per child. Start from today's best genomes (evo_mixed child_8, child_4, the Sonnet variant). `claude:sonnet` is allowed on the bottleneck roles (app / fixer), Nemotron on the rest; the token penalty stays on so the evolver weighs cost.
4. **Speed**: harness and reference built in parallel by two sessions (≈45 min). Evolution starts ≈17:00; ≈25 min per generation → 4-5 generations by 20:00, leaving time for demo prep and backup recording.

## Budget
New cap: **$150 total** for the rest of the day (today so far: $10.4). Estimate: ~$1.2 per Sonnet team build of the bigger app × 2 builds × 6 children × 3 runs × 4 gens ≈ $115, plus evolvers ≈ $15, plus calibration ≈ $5 → ~$135. Spend alerts at $100 and $135; I stop the weakest run first if the pace overshoots.

## Risks
- A bigger app means longer outputs: Nemotron truncates, while Sonnet is slow (~5 min/file). Mitigation: split files (app/learner/meta), the parallel step, the smoke+fix loop.
- New checks may be wrong at first. Mitigation: calibrate on the reference before any evolution; the base-keeper watches for harness_error/no_start.
- Time: if v2 is not calibrated by 17:15, fall back to evolving within v1 only and demo the learner loop from the reference app.
