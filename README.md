# Self-Evolving Agent Teams

A small, task-agnostic workflow in which a frozen **evolver** (Claude, run headless via the `claude` CLI)
repeatedly redesigns a **team of agents** for a benchmark. The team is described by a *genome*: a list of
roles (LLM roles with a prompt and model, or Python tool roles), their order, and optional tool code.
Each generation the evolver reads the parent genome, its scores and its failing samples, and writes
3 child genomes; the best child replaces the parent only if it is better (greedy, with retries and patience).

```
L2  evolver (Claude, frozen)      reads parent + report + failures + feedback  ->  writes child genomes
L1  genome  (the only thing that changes)   roles[] (llm | tool) · prompts · models · tools/*.py
L0  environment (frozen)          data splits · local vLLM workers · verifier · token counter
```

- **Recursive**: a child that beats its parent becomes the new parent.
- **Iterative**: if no child wins, the evolver retries *from the same parent* with condensed feedback about
  the failed children (their full traces never reach the next generation).
- **Fitness** = weighted task score − λ · tokens/sample / 1000, so the evolver must trade accuracy for cost.
- **No test leakage**: the evolver works in an isolated workspace that only contains the parent, dev-split
  reports and failures; test splits are evaluated only in `calevo.report`.

## Benchmarks wired in

| task | data | scorer | splits |
|---|---|---|---|
| calendar | [nvidia/Nemotron-RL-agent-calendar_scheduling](https://huggingface.co/datasets/nvidia/Nemotron-RL-agent-calendar_scheduling) (+ v2) | official NeMo Gym grader (vendored, Apache-2.0) + soft-preference scoring | dev_in, test_in, test_v2, ood_dev/ood_test (generated preference sets), dev_hard/test_hard_v/test_hard_h (Claude-paraphrased, weighted, conflicting) |
| math | MATH Level 5 (4 types, as in AFlow) | last `\boxed{}` + `math-verify` equivalence | math_dev 121 / math_test 484 |
| drop | DROP validation, 1000 sampled (as in AFlow) | token F1, max over annotator alternatives | drop_dev 200 / drop_test 800 |
| appworld | [AppWorld](https://github.com/StonyBrookNLP/appworld) | official unit-test evaluation | train / dev / test_normal (data NOT included — install it yourself, see `appworld/README.md`) |

## Layout

```
src/calevo/
  runner.py        L0: execute a genome on a split, score, count tokens  (python -m calevo.runner)
  evolve.py        L2: resumable evolution loop                          (python -m calevo.evolve)
  tasks.py         per-task scoring + MATH/DROP split builder           (python -m calevo.tasks)
  verifier.py      calendar verifier (wraps official_grader.py) + preference checks
  solver.py        reference backtracking / max-weight scheduler (feasibility checks, S0 baseline)
  report.py        final test-split table + evolution curves             (python -m calevo.report)
ood/               preference-set builders (build.py: template OOD; build_v2.py: paraphrased hard set) + specs
genomes/           P0 (single prompt) and S0 (fixed-DSL solver) starting genomes
tasks_desc/        task descriptions handed to the evolver for non-calendar tasks
appworld/          AppWorld harness: run_baseline.py, run_genome.py, genomes/P0
data/splits/       prepared splits (see Data licences)
snapshots/v1_model_top/   best genomes (incl. evolved tool code), per-run histories, test reports, write-up
viz/team_evolution.html   per-generation team size / score / tokens visualisation
docs/              REPORT.md, curves, development log of the first overnight run
```

## Quick start

```bash
pip install -r requirements.txt
# 1. serve worker models with any OpenAI-compatible server (we used vLLM), ports as in runner.ENDPOINTS:
#    qwen3-8b :8100, qwen3-30b-a3b :8101, qwen2.5-72b :8102
# 2. baseline
PYTHONPATH=src python -m calevo.runner --genome genomes/P0 --split dev_in --worker qwen3-30b-a3b --out runs/p0_dev
# 3. evolve (needs the `claude` CLI logged in; env vars choose splits / weights / lambda / task description)
CALEVO_SPLITS="dev_in:0.6,ood_dev:0.4" PYTHONPATH=src python -m calevo.evolve --run runs/evo_full --worker qwen3-30b-a3b
CALEVO_SPLITS="math_dev:1.0" CALEVO_SPECS="" CALEVO_TASK_DESC=tasks_desc/math.md CALEVO_LAMBDA=0.01 \
  PYTHONPATH=src python -m calevo.evolve --run runs/evo_math --worker qwen3-8b
# 4. final test-split report
PYTHONPATH=src python -m calevo.report --run-missing
```

Configuration knobs (environment variables read by `evolve.py`): `CALEVO_SPLITS` (fitness splits and
weights), `CALEVO_LAMBDA` (token penalty), `CALEVO_SPECS` (spec files the evolver may read),
`CALEVO_TASK_DESC` (task description file), `CALEVO_SCHEMA_DOC` (genome schema doc, e.g. for AppWorld).
Evolved tool code runs in a subprocess with a 60 s limit; it is written by the evolver, so run this only
on machines where that is acceptable.

## Findings so far (details in `snapshots/v1_model_top/SNAPSHOT.md`, `docs/dev_log/`)

- Calendar v1 saturates: the evolved team (3 roles, 1 LLM, ~900 tokens/sample) scores 1.000 on test_in vs
  0.320 for a single prompt, but unseen preference types (ood_test 0.625) and free-form paraphrases
  (test_hard_h 0.415) remain hard.
- A strong token penalty drives evolution to delete the LLM entirely (0-token regex solver): perfect
  in-distribution, brittle under new phrasing (test_v2 0.41). Lowering λ keeps the LLM and generalises better.
- On harder tasks the team grows instead: MATH Lv5 → 6 roles (3 LLM), DROP → 3 roles (2 LLM).

## Data licences

Splits in `data/splits/` are derived from: Nemotron-RL calendar scheduling v1 and v2 (CC-BY-4.0, NVIDIA),
MATH (MIT, Hendrycks et al.), DROP (CC-BY-SA-4.0, AllenAI). Generated preference sentences were written by
Claude. `src/calevo/official_grader.py` is copied from NVIDIA NeMo Gym (Apache-2.0). AppWorld data is not
redistributed here.
