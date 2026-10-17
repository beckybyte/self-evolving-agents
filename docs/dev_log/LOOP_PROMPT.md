# Calendar-Evo overnight loop — one tick

You are continuing the Calendar-Evo project autonomously until the user says they are taking over.
Project: <repo> (NOT the <another project> repo — never touch <another project>).
Target doc (Claude Docs, project id 36d2a598-fabd-408b-806e-ee9a864ae4be, body node 239d221a-06b7):
sections "Targets 与里程碑" (W1–W6 table) and "进度日志" (newest row on top). Local mirror: STATUS.md.

Each tick:
1. Read STATUS.md. Check health of everything running — not just pid: output mtime must move.
   - vLLM: docker containers ke-calevo-8b/30b/72b on ports 8100/8101/8102 (`curl localhost:PORT/v1/models`).
   - Evolution: `pgrep -af calevo.evolve`; runs/evo_full and runs/evo_prompt (state.json, history.jsonl,
     workspaces/*.evolver.log, logs/evo_*.log). Stalled >15 min with no new file = hung.
   - ood_test: data/splits/ood_test.jsonl must exist with ~60 rows (ood/build.py).
2. Compare against the Targets (W1–W6). Decide the most valuable next action and DO it:
   crash -> read the traceback, fix root cause, restart (evolve.py is resumable via state.json);
   evolver produced invalid genomes repeatedly -> improve INSTRUCTIONS/validation in src/calevo/evolve.py;
   no improvement for a whole generation -> inspect failures and decide whether the evolver needs better evidence;
   evolution finished -> run final evals of the best genomes (P0, S0, best evo_full, best evo_prompt) on
   test_in, ood_test, test_v2 for the 30B worker (and 8B/72B if time allows) and fill the W6 3x3 table;
   72B baseline missing -> check runs/baseline.
   Run verification (pytest src/calevo/*_test.py) after any code change.
3. Record: add a row to STATUS.md and to the doc's 进度日志 table (newest first), update the W1–W6 状态 column,
   then `git commit` in calendar-evo (end message with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>").
   If the docs tool fails, keep going and sync next tick.
Boundaries: no SLURM, never kill other users' GPU processes or containers, only GPUs this project started.
Evolver cap 300 Claude calls total. Questions that need the user -> write them under "待用户决定" in STATUS.md, continue other work.
Pacing: next wakeup = max(60s, 1800s − time this tick took). Never stop the loop yourself unless the user says they took over;
if all targets are met, keep improving (more generations, 8B/72B workers, ablations) and keep logging.

Doc block ids (re-read outline if a write is refused): Targets table mx1exgyzhqr.4415 (rev 11), 进度日志 table mx1exgyzhqr.7494 (rev 17).
To add a log row: insert {"as":"blocks"} rows with side "after" the header row mx1exgyzhqr.7495 (newest first) — no need to resend the table. Watch for the user's own edits (read sinceRev first).
GIT SAFETY: always `git -C <repo> ...`; never put `cd X && cmd &` (cd lands in the background subshell). Never commit in <another project>.
Doc: 中期结果 section heading mx1exgyzhqr.9838, table mx1exgyzhqr.9932 (rev 25).
TIMESTAMPS: always run `date -u +%H:%M` and use that value; never estimate the time.

PHASE 2 (user chose A at 14:23): iterate the DATA. Plan, in order (check off in STATUS.md):
 1. ood/build_v2.py builds data/splits/dev_hard (80, VISIBLE types), test_hard_v (60, VISIBLE), test_hard_h (80, HELDOUT types)
    with Claude paraphrases + blind back-check (log: logs/build_v2.log). Verify counts, read ~10 samples, spot-check wording.
 2. Baselines on all three hard splits, 30B worker: P0, S0, v1 best (snapshots/v1_model_top/genomes/evo_full__gen10_a1_child_1)
    and the λ=0.005 8B genome on 8B. Use calevo.runner, outputs under runs/hard_baseline/. Confirm there is headroom.
 3. Launch evo_hard (30B), starting FROM the v1 best genome:
    CALEVO_SPLITS="dev_in:0.3,dev_hard:0.7" CALEVO_SPECS="spec_hard.md" CALEVO_LAMBDA=0.01
    python3 -m calevo.evolve --run runs/evo_hard --worker qwen3-30b-a3b --start snapshots/v1_model_top/genomes/evo_full__gen10_a1_child_1
    (evolver never sees test_hard_* or HELDOUT type names).
 4. When it finishes: evaluate best on test_hard_v / test_hard_h (+ test_in for regression), report visible vs held-out
    preference score and agent count per generation (same format as snapshots/v1_model_top/AGENTS_BY_GEN.md).
evo_full_diverse (C-type diversity on v1 data) keeps running in parallel.

PHASE 3 (user, 14:45): add AppWorld + MATH Lv5/DROP in parallel with phase 2.
 a. MATH/DROP are wired in (src/calevo/tasks.py; splits math_dev 121 / math_test 484, drop_dev 200 / drop_test 800,
    AFlow-style 1:4). Task descs: tasks_desc/{math,drop}.md. P0 baselines -> runs/bench_baseline/.
 b. After baselines, launch (evo_math on 8B worker = GEPA Qwen3-8B reference; evo_drop on 30B) — LAUNCHED:
    CALEVO_SPLITS="math_dev:1.0" CALEVO_SPECS="" CALEVO_TASK_DESC=tasks_desc/math.md CALEVO_LAMBDA=0.01 python3 -m calevo.evolve --run runs/evo_math --worker qwen3-8b
    CALEVO_SPLITS="drop_dev:1.0" CALEVO_SPECS="" CALEVO_TASK_DESC=tasks_desc/drop.md CALEVO_LAMBDA=0.01 python3 -m calevo.evolve --run runs/evo_drop --worker qwen3-30b-a3b
    NOTE evolve.py needs >=1 split; ood_dev in history will be None for single-split runs.
 c. When done: evaluate best genomes on math_test / drop_test; compare gain over P0 with AFlow/ADAS reported gains
    (gpt-4o-mini: MATH AFlow 56.2 vs ADAS 35.4; DROP 80.6 vs 76.6) -> notes/next_benchmarks.md.
 d. AppWorld: installed (appworld/README.md). Baseline ReAct P0 dev TGC 5.3% (30B), SGC 0 -> big headroom.
    Subagent is building appworld/run_genome.py + appworld/genomes/P0 (genome-driven policy). runner.evaluate dispatches
    splits named appworld_<split> to it. When it reports: write tasks_desc/appworld.md, extract the genome-schema
    section of appworld/README.md to appworld/genome_schema.md, then launch
    CALEVO_SPLITS="appworld_train:1.0" CALEVO_SPECS="" CALEVO_TASK_DESC=tasks_desc/appworld.md CALEVO_SCHEMA_DOC=appworld/genome_schema.md CALEVO_LAMBDA=0.002
    python3 -m calevo.evolve --run runs/evo_appworld --worker qwen3-30b-a3b --start appworld/genomes/P0 --k 3
    (score = unit-test pass fraction; tokens are ~46k/task so lambda must be small). Test on appworld_dev + appworld_test_normal.
 (old note) AppWorld: a subagent is setting it up in appworld/ (README.md there when done: install, splits, ReAct baseline,
    run_task interface). Then add an environment-loop role type to the runner and launch evo_appworld on dev.

PENDING DOC SYNC: insert section "## 第二阶段：迭代数据（方向 A）" after the 中期结果 bullet list mx1exgyzhqr.10226 (content = PHASE 2 plan + table of changes: free-form wording / 8 types with fixed held-out / conflicting weighted prefs / normalised scoring). Delete this line when done.
