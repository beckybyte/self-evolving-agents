# Evolving Calendar built by sea: what happened on 2026-10-17

The goal was the web app described in the design doc "Evolving Calendar" (persona Iris, who finds planning hard), built by
evolved agent teams. The evolver (Claude Opus) designs the team (roles, models, workflow) and never writes product code.
Tool roles and code pasted into genomes are rejected. Every app in `snapshots/` was written by the team's models.

## Overnight update 2 (2026-10-18, 23:10 PDT): evo_v33 finished 4 generations
Best balanced build: `v3_3_final_balanced/` (evo_v33 gen03 child_5): fit 0.997, holdout 0.934, user simulator 0.975,
screenshot UI judge 0.77 (hand-written reference 0.68). Its fitness of 0.910 fell just short of the 0.915 acceptance bar, so the
run's official parent stays gen01 child_3 (UI 0.70, sim 1.0). Picked by hand as the best overall build.
Next run evo_v34 (3 gens, from this build, with P058/P060/copy roles active) started 23:10.

## Overnight update (2026-10-18, 22:16 PDT)
Best is now `v3_3_best_8835/` (evo_v33 gen01 child_3): screenshot UI judge 0.68 / 0.71 (2 builds), above the hand-written
reference (0.68). Harness v3.2 fit 0.998, LLM user simulator clean-episode rate 1.0 (40 episodes, fresh seeds), holdout 0.934.
Fitness was 0.4 fit + 0.3 sim + 0.3 UI − 0.0001·tokens. The UI weight was raised from 0.2 after the human's preference
for the reference UI. Path: evo_v32 gen02 child_6 (sim 0.975, UI 0.59) → evo_v33 gen01 (UI 0.70).

## Best result (end of 2026-10-17)
`v3_2_best_ui_8833/` (evo_v32 gen01 child_4; written by DeepSeek-V4.1-Flash + gpt-oss + Claude Haiku roles on Crusoe):
- harness v3.2 fit 1.0 / 0.92 (2 builds), holdout 0.93 / 0.85
- LLM user simulator: clean-episode rate 0.875 (40 episodes, fresh seeds)
- screenshot UI judge: 0.64 / 0.61 (the hand-written reference scores 0.68; the evolved UI before the UI work scored 0.23-0.27)
- Features: two-column layout, chat as the main control, focus card (next tiny step, why, Start 5 minutes countdown),
  proposal card citing the habit number, week view with soft blocks, brain dump, breakdown chat asking why first,
  reminders, gentle auto-postpone with undo, check-in, typed CRUD with bulk previews, .ics/text import, habit beliefs,
  view proposals, meta-learner prompt versions with rollback, designer + critic custom views, adaptive knobs.
- Picked by hand on UI + simulator. Fitness (0.5 fit + 0.3 sim + 0.2 UI − tokens) preferred child_5, which has UI 0.39.
  The hand-set weights disagreed with the human's clear preference (see "Open problems").

## How the evaluation grew (each step triggered by saturation or a human noticing a gap)
| Version | Adds | Why |
|---|---|---|
| v1 | Iris main flow, 87 fit / 66 holdout checks | starting contract |
| v2 | learning loop (CRUD, bulk preview, import, beliefs, proposals, meta-learner), UI ids | v1 saturated |
| v3 | designer + critic views, morphs, adaptive knobs, headless-Chrome render check | v2 saturated; a 1.0 app rendered empty panels |
| v3.1 | 12 harder fit scenarios, holdout byte-identical | fit 1.0 vs holdout 0.93: overfitting to wording |
| v3.2 | design-doc UI spec in the contract + LLM user simulator + Claude screenshot judge in fitness | fit rose to 0.99 but holdout did not move; the human found the UI hard to use |

Every evaluator version was calibrated on a hand-written reference app before use (reference ≥ 0.95-1.0, stub ≈ 0).

## Framework changes made while running (backlog P007-P052)
Crusoe backend + `#nothink`; `parallel:` workflow step; BuildTask (teams write code, frozen harness scores it); per-role
model catalog with cost weighting; averaging 2 independent builds per genome (the same genome scored 0.745 and then 0.248);
framework check role `app_smoke` ("run your own code", basic/deep/v2/v3 calls, selftest); P040 one failed model call no
longer zeroes the team; loop stop markers only at line start; ceiling auto-stop (P034); user simulator and UI judge as
fitness terms. A base-keeper agent implemented most of them from the evolver's own FRAMEWORK_WISHES.md and from failure signals.

## Attribution (honest)
- Gains from re-measurement were not counted as evolution: gen01 0.357 → 0.774 came from re-scoring the same genome.
- A capability alone helped little: P1 plus a hand-added smoke/fixer loop scored 0.006. The jump to 0.95 came from evolution
  combining the capability with a smaller team.
- The UI gain was split: 0.25 → 0.46 from putting the UI spec in the contract (goal amplification), and 0.46 → 0.62 in one
  generation of evolution with the UI judge in fitness.
- Same evolved structure, model swap: a Sonnet app scored 1.0/1.0 vs Nemotron 0.95/0.95 on v1.

## Open problems
1. Saturation still needs a human to trigger goal amplification. §7 autonomous amplification is half built; its session's
   permission check needs the user's direct confirmation.
2. The fitness weights were set by hand and disagreed with the human's preference. Next: learn the rubric and weights from
   the human's ratings, adopted only if they agree with past ratings.
3. Builds are unstable: one genome yields apps whose UI score ranges 0.14-0.54 and simulator score 0.15-0.85.
4. Holdout stays around 0.90-0.93 while fit saturates. The simulator and UI judge are the first signals that cannot be
   memorized.
