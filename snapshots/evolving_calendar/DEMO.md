# Evolving Calendar × sea — demo kit (16:00 PDT)

## One-line story
A team of **Nemotron** agents (on Crusoe) writes a runnable personal-agent calendar for Iris, someone who finds planning hard.
**sea**, our self-evolving framework, redesigns that team generation by generation. Its evolver is Claude, which designs the team and never writes product code.
When the framework itself runs out of capability, a **base-keeper agent** extends it. Some of those extensions were requested by the evolver itself.

## Three levels of recursion (what to say on stage)
1. **Product layer**: the Nemotron team writes app.py and index.html. A frozen harness scores the app on 87 fit and 66 holdout mechanical checks (brain dump, asking why, a first step of 15 minutes or less, reminders, gentle postpone, check-in, no-guilt wording).
2. **Team layer**: the evolver reads the failed checks and the code, then rewrites the team: roles, workflow, per-role model, thinking on or off.
3. **Framework layer**: the evolver writes FRAMEWORK_WISHES.md, and the base-keeper agent turns the wishes and failure signals into backlog proposals, code and tests (P022-P028).

## Numbers (real, each genome = mean of 2 independent team runs)
| Team | Structure | fit | holdout | tokens |
|---|---|---|---|---|
| P0 | one dev | 0.574 (1 run) | 0.465 | 13k |
| P1 | architect → parallel backend/agents/ui → integrator | 0.745 → 0.248 → 0.21 / 0 | 0.69 → 0.05 | 98-111k |
| **gen01 (accepted)** | app + ui (architect and integrator dropped) | 0.357 (runs 0 / 0.71) | 0.280 | 22k |
| **gen02 (accepted)** | app + ui + **smoke** (framework trial-run) + fixer + disarm | **0.949** (0.91 / 0.99) | **0.951** | 131k |
| **evo_mixed gen01 child_8 (accepted)** | core + ui + smoke + behaviour + smoke + fixer (Nemotron, fixer on Claude Haiku) | **0.979** (0.97 / 0.98) | **0.968** | |
| evo_mixed gen01 child_4 | app on Claude Haiku, ui + fixer on Nemotron, smoke | **1.0** (1.0 / 1.0) | **1.0** | (cost-weighted 551k, not selected) |
| control: P1 + smoke + fixer by hand (no evolution) | | 0.006 | 0.008 | 167k |
| Reference app (hand-written, calibration) | | 0.99-1.0 | 1.0 | |

Lesson to tell: **one run is not a measurement**. The same P1 team scored 0.745 and then 0.248, so sea now averages 2 independent builds before accepting a child.

Attribution, plainly:
- gen01's 0.357 → 0.774 on re-score is the same genome re-measured, not evolution.
- The trial-run capability alone, bolted onto P1, gives 0.006.
- gen02's 0.95 comes from the evolver putting smoke + fixer into the small 2-role team.
The evolver asked for the framework features itself (FRAMEWORK_WISHES.md) and the base-keeper agent built them.

Confirmation (15:38, 4 MORE independent builds of each genome, so 6 builds in total):
- evo_mixed child_4 (app on Claude Haiku): fit 1.0 / 1.0 / 1.0 / 1.0, holdout 1.0 on all 4 runs.
- evo_mixed child_8 (Nemotron team, fixer on Haiku): fit 0.990 (1.0, 0.97, 0.99, 1.0), holdout 0.989.
- evo_iris2 gen02 child_3 (Nemotron only): fit 0.934 (0.84-0.98), holdout 0.896.
- Same evolved structure with Sonnet writing the app: fit 1.0, holdout 1.0 (2 builds).

## Framework changes made today (backlog ids)
P007 Crusoe backend + `#nothink` (thinking used up 32k tokens, 0 points) · P008 BuildTask: the team writes code and a harness scores it ·
P009 `parallel:` workflow step · P022 per-role model catalog with cost weighting · P023 trace of truncation, empty output and 429 ·
P024 framework check role `app_smoke` ("run your own code") · P025 no stale cache on resume · P026 skip the fixer when the check passes (the evolver's wish) ·
P027 retry with #nothink when thinking produces nothing · P028 re-emitted files no longer dropped · plus: no tool roles and no code pasted into genomes,
so the code is written by the team's models (Nemotron, plus Claude Haiku in some mixed-run roles), never by the evolver.

## Live product (on the dev box)
- **Main: evo_mixed gen01 child_4 (fit 1.0 and holdout 1.0 on all 6 builds; app written by Claude Haiku, UI + fixer by Nemotron): `http://localhost:8814/`**. It has a styled UI: chat bubbles, a focus card with the why and Start 5 minutes, a today/tomorrow timeline.
- Mostly-Nemotron alternative: evo_mixed gen01 child_8 (fit 0.99, holdout 0.99): `http://localhost:8813/`. Its API is excellent, but its page is unstyled plain HTML. Quote its numbers; don't show its page.
- Nemotron-only: evo_iris2 gen02 child_3 (fit 0.95): `http://localhost:8812/` (focus-card wrap bug, see below)
- Older: gen01 child_1: `http://localhost:8791/`
- Reference app: `http://localhost:8790/`
- From a laptop: `ssh -L 8813:localhost:8813 -L 8814:localhost:8814 -L 8812:localhost:8812 -L 8790:localhost:8790 <devbox>`, then open the URLs.

### Iris 3-minute script (verified on 8814 and 8813 at 15:26; run it on 8814)
1. Brain dump: `call mom, portfolio, maybe yoga?, dentist thursday 3pm` → 4 items, dentist on Thu 15:00, one gentle question about Portfolio.
2. Type `get my portfolio together` → "This one sounds big. Want me to break it into small steps?" → `yes please`.
3. "Why does this matter to you?" → `for the residency application I care about` → "What does done look like?" → `five pieces online with a short statement` → deadline? → `by next friday`.
4. Breakdown: 4 steps, **first step 10 minutes, at 21:00** (Iris works in the evening) → `looks good`. The why is stored on the goal.
5. Focus card: only "List the projects you are proudest of", with Start 5 minutes.
6. Scrub the clock to 21:00: a reminder at the start and one 10 minutes before the end.
7. Scrub past the end without marking it done: **"No worries, I moved 'List the projects you are proudest of' to Tue Oct 20 at 23:00. Undo if you'd rather keep it."** Press Undo.
- Note: the harness checks behaviour, not visual quality (8813 scores 0.99 with no CSS). v2 adds basic UI checks.
- Known flaws in 8812 only (8813 and 8814 do not show them):
  - A 4-step breakdown can wrap its last step to the same morning, and then the focus card shows that step. If it happens, use 8791 for the focus card.
  - Brain-dump tasks default to 9 AM, so their postpones land in the morning.

## v2: the design doc's learning loop (added 15:50-16:50)
Evaluator v2 (`builds/evolving_calendar_v2/`): v1 behaviours 1-9 + 10-21 (typed CRUD, bulk preview/apply/undo, .ics and text import,
interaction log, habit beliefs with confidence and evidence, proposal gate and actions, hidden_counts, meta-learner prompt versions with
replay and rollback, activity, mock mode) + UI checks. 176 fit / 126 holdout checks. Hand-written reference: 1.0 / 1.0. Stub: ~0.
One check (activity diff) was stricter than the contract; with user approval it was fixed and all v2 results were rescored.

| Team (v2) | fit | holdout | Note |
|---|---|---|---|
| v1 champion child_8, as is | 0.705 | 0.690 | new parts missing |
| v1 champion child_4 (Haiku), as is | 0.465 | 0.428 | one build hit a timeout (fixed: P040 + 2400 s) |
| Sonnet variant, as is | 0.796 | 0.770 | |
| evo_v2_c0 gen01 child_4 (Sonnet app) | 1.0 (1.0 / 1.0) | 0.951 | http://localhost:8817/ |
| evo_v2_a gen01 child_4 (from the Haiku team, cost-aware) | 0.987 | 0.946 | |
| **evo_v2_b gen02 child_3** (DeepSeek-V4.1-Flash code, gpt-oss UI, Haiku only as rescue; 42k weighted tokens) | **1.0 (1.0 / 1.0)** | **0.989** | **http://localhost:8818/ (main v2 demo)** |
| evo_v2_b gen01 child_3 (mostly Nemotron) | 0.955 | 0.906 | |

Main v2 demo is 8818 (best holdout, all on Crusoe except a Haiku rescue role). Its team: core + v2 + ui write 3 files in parallel → smoke → behaviour review → smoke → fixer loop (2) → Haiku rescue loop (1).
The evolver chose these models from the catalog itself (pool 'all').
Learning-loop demo path (8817, same on 8818): seed history → POST /api/learn → belief "Finishes tasks after 8 PM (8 of 8)" (0.95) → proposal
"Try an evening-first day view?" → Try it / Undo → after 2 reverted invented proposals the designer prompt is rewritten to v2
("+ Prefer classic, familiar views (2 invented proposals were undone)") and can be rolled back. Activity panel shows each step.
Known gap in 8817 only: bulk changes in other wording are applied without a preview (8818 handles them: holdout 0.989).
Not covered by any evaluator yet: designer+critic generating new view specs, view-switch animations, adaptive threshold/cadence (E5).
