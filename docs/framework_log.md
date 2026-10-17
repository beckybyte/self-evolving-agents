# Framework log

One entry per framework change merged by the meta pipeline (§8.6).

## P001: Shared memory: duplicate injection, no distillation, ratings comments never reach the team (2026-10-17)
category learning · base 04d84ba · candidate 55981f0 · improver: visible Claude Code session · **merged by human approval**

- files: meta/changes/P001.md, src/sea/brief_test.py, src/sea/executor.py, src/sea/genome.py, src/sea/tasks/brief/task.py
- A correctness: pass (17 unit tests; 3 frozen safety tests)
- B regression: pass (expected prompt changes: brief; 1/15 traces changed: brief_P0_shared 2521 -> 2416 chars, the duplicated human answers removed; calendar identical)
- C efficacy: FAIL by the rule (brief base 0.4873 vs cand 0.3765; calendar base 0.9438 vs cand 0.8772; candidate cost $6.09, 20 min)
- why merged anyway: the C differences are run-to-run noise of a single short evolution. Calendar prompts are byte-identical
  under P001 (tier B), yet the two runs differ by 0.067; the brief C runs had no human answers, so the changed code path never
  ran. Tier C as designed cannot measure this kind of change -> follow-up proposal on tier C (seeds + path coverage).
