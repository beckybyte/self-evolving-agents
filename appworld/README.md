# AppWorld offline harness (local vLLM)

AppWorld (StonyBrookNLP, arXiv 2407.18901): 9 simulated apps (spotify, venmo, gmail, amazon,
phone, splitwise, simple_note, todoist, file_system) + supervisor, ~457 APIs. The agent writes
Python that calls `apis.<app>.<api>(...)` in a stateful REPL; grading is by hidden unit tests on
the final DB state and the answer.

## Install (done, reproducible)

```bash
cd <repo>/appworld
source <conda>/etc/profile.d/conda.sh
conda create -p ./env python=3.11 -y
./env/bin/pip install appworld openai
PATH=$PWD/env/bin:$PATH appworld install          # unpacks encrypted app/test bundles
PATH=$PWD/env/bin:$PATH appworld download data --root $PWD   # -> ./data (164 MB)
```

Versions: Python 3.11.17, appworld 0.1.3.post1 (data version 0.1.0), openai 3.33.0,
freezegun 1.5.1, pydantic 1.10.26, fastapi 0.110.3. `appworld install` also unpacks its test
sources into `~/.appworld/tests` (package default, outside this dir). Task data/ground truth
are encrypted bundles; we only touch them through the official API (`AppWorld`, `evaluate`).
Do not copy `data/` anywhere public.

Always set `APPWORLD_ROOT=<repo>/appworld` (holds `data/`
and `experiments/outputs/`).

## Splits (`appworld.load_task_ids(name)`)

| split | tasks | scenarios (3 tasks each, approx.) |
|---|---|---|
| train | 90 | 30 |
| dev | 57 | 19 |
| test_normal | 168 | 56 |
| test_challenge | 417 | 139 |

TGC (task goal completion) = % tasks whose tests all pass. SGC (scenario goal completion) = %
scenarios where all of its task variants pass. Difficulty 1/2/3 is in the ground truth.

## Run the baseline

```bash
cd <repo>/appworld
APPWORLD_ROOT=$PWD ./env/bin/python run_baseline.py --split dev --workers 8 \
    --model qwen3-30b-a3b --base-url http://localhost:8101/v1 --exp qwen30b_dev
# qwen3-8b: --model qwen3-8b --base-url http://localhost:8100/v1 --disable-thinking
# subset: --limit 20 ; resumable: finished tasks (results/<exp>/<task>.json) are skipped
# official cross-check (needs the full split run):
./env/bin/appworld evaluate qwen30b_dev dev --root $PWD
```

Outputs: `results/<exp>/<task_id>.json` (score, tokens, steps, failed tests, full trace) and
`results/<exp>/summary.json`; AppWorld's own logs/DB dumps in `experiments/outputs/<exp>/`.

Agent: ReAct-style, one ```python``` block per turn -> `world.execute(code)` -> output
(truncated to 3000 chars) fed back. Stops on `apis.supervisor.complete_task(...)`, 30 steps, or
300k tokens. Context kept under 16k by dropping oldest turns. temperature 0, max 1536 new tokens.
Env options: `timeout_seconds=60` per cell, `raise_on_extra_parameters=True` (unknown kwargs
raise instead of being silently ignored; evaluation unaffected).

## Baseline numbers (dev)

| model | tasks | TGC | SGC | avg tokens/task (prompt+compl) | avg steps | wall clock (8 workers) |
|---|---|---|---|---|---|---|
| qwen3-30b-a3b (Instruct-2507) | 57 (full dev) | **5.3** | **0.0** | 46.4k (44.7k + 1.7k) | 13.7 | 621 s (~10 min) |
| qwen3-8b, no thinking | first 20 of dev | 5.0 | 0.0 | 80.6k (79.3k + 1.4k) | 17.9 | n/a (see below) |
| qwen3-30b-a3b on the same 20 | first 20 of dev | 5.0 | 0.0 | | | |

30B per-difficulty TGC: d1 10.0, d2 0.0, d3 0.0 (official `appworld evaluate` matches: 5.3/0.0).
8B hits the 30-step cap on 9/20 tasks (30B: 3/57), looping on the same errors with long
observations, hence ~2x tokens. Both models pass the same kind of easy task only.
Partial credit (passed unit tests / all tests) is 46% even though only 3/57 tasks fully pass.
Evaluating a finished split with the official CLI takes ~15 s.

Failure modes (30B, counted over all failed cells in traces):
1. **Hallucinated API names** (largest group): `show_saved_albums`, `show_playlists`,
   `show_followed_artists`, `show_payment_history`, ... instead of listing APIs first.
2. **Login misuse**: passing the whole `{account_name, password}` dict as password ->
   "Invalid credentials", then the model concludes "auth is broken" and gives up with a
   sentence as the answer.
3. **Response-shape errors**: treating list responses as dicts (`AttributeError`, `KeyError`).
4. **Premature / wrong `complete_task`**: answer is a sentence, empty, or computed from the
   first page only; or calls complete_task after a failed cell assuming success.

## Plugging into the evolution framework

Everything a policy needs is in `run_baseline.py`:

```python
class Policy(Protocol):
    def reset(self, task_info: dict) -> None: ...      # first_name, last_name, email,
                                                       # phone_number, datetime, instruction, allowed_apps
    def act(self, observation: str | None) -> tuple[str | None, dict]:
        """Return (python code to execute, info{'prompt_tokens','completion_tokens','raw'})."""

def run_task(task_id, policy, experiment_name, max_steps=30, token_budget=300_000) -> dict:
    # -> {"task_id", "score" (0/1 strict), "pass_count", "num_tests", "difficulty",
    #     "failed_tests", "prompt_tokens", "completion_tokens", "tokens", "steps",
    #     "stop_reason", "instruction", "trace": [{"step","raw","code","output"}]}
```

A genome (list of roles run in sequence) becomes the policy like this:

- **Environment-loop role** (new, Python, fixed): owns the `AppWorld` object and the
  `for step in range(max_steps)` loop; each iteration it calls the genome's inner roles with the
  shared state `{task_info, history, last_observation}` and expects a `code` field back, then
  `world.execute(code)`, appends to history, checks `world.task_completed()`. After the loop it
  calls `world.evaluate()` -> score. This is `run_task` above.
- **Inner roles** (one "turn" of the genome, executed in order on a dict blackboard): e.g.
  `planner` (LLM: instruction -> plan, run only at step 1), `api_lookup` (Python tool: calls
  `apis.api_docs.show_api_descriptions/show_api_doc` for apps named in the plan and puts docs in
  the blackboard -- directly targets failure mode 1), `coder` (LLM: history + docs -> code),
  `critic` (LLM: inspect output, decide retry vs. complete). Wrap them in a class implementing
  `reset/act`; `act` runs the role list and returns the blackboard's `code` plus summed token
  usage. Python tool roles that need the world can get it injected (`world.execute` for
  read-only doc lookups costs no score).
- **Fitness**: TGC over a task set (use `train` for evolution, `dev` for selection, keep
  `test_*` for final reporting), plus token cost from the returned `tokens`. Also log
  `pass_count/num_tests` as a dense secondary signal (TGC is very sparse at this model size).

Operational constraints:
- **One task per OS process.** AppWorld freezes the clock globally (freezegun, no tick) and
  caches DBs in-process; `run_baseline.py` spawns a process per task with a hard kill timeout.
- Because the clock is frozen inside the worker, httpx/openai timeouts never fire; LLM calls
  are guarded by a real-time `SIGALRM` (180 s, 1 retry). Don't rely on `time.time()` there.
- History trimming (`_fit_context`) keeps system + task + the newest turns within ~2.8
  chars/token of the 16k window. (An early version had an infinite loop here that hung 8B tasks
  until the 20-min kill; fixed, and those tasks were rerun. The 30B dev run never reached the
  trimming threshold -- ~3.3k prompt tokens per call -- so its numbers are unaffected.)
- Each run needs a unique `experiment_name` per genome evaluation (outputs are keyed by it).
- Cost: dev (57 tasks) ~10 min wall clock with 8 parallel tasks on qwen3-30b-a3b, ~2.6M tokens.
  The 8B server is shared (69 concurrent requests seen during our run) and our 8B run was
  done in pieces, so no clean 8B wall clock; its last batch of 5 tasks took ~4 min. Rough: train (90) ~16 min, test_normal (168) ~30 min.

## Genome schema (run_genome.py) -- read this to write AppWorld genomes

```bash
cd <repo>/appworld
APPWORLD_ROOT=$PWD env/bin/python run_genome.py --genome genomes/P0 --split train \
    --worker qwen3-30b-a3b --out runs/P0_train_30b [--workers 8] [--limit N] [--task-timeout 1200]
```
`--worker` in {qwen3-8b (:8100, thinking disabled automatically), qwen3-30b-a3b (:8101),
qwen2.5-72b (:8102)}; `$WORKER` in a genome means this model. Use `train` (90 tasks) for
evolution fitness, `dev` (57) for selection, `test_normal` only for final reports.

A genome is a directory: `genome.yaml` + optional `tools/*.py`.

```yaml
id: P1                    # required, unique
parent: P0
notes: "what changed and why"
max_steps: 30             # default 30: environment steps (one code execution each)
history_turns: 30         # default 30: how many past steps are visible ({history} / chat turns)
token_budget: 300000      # default: stop when prompt+completion tokens of all roles exceed it
obs_char_limit: 3000      # default: execution outputs are middle-truncated to this many chars
act: coder                # required: name of the llm role whose LAST ```python``` block is executed
roles:                    # ordered; the WHOLE list runs once per step, then the act code is executed
  - name: planner         # unique; its output is stored in blackboard["planner"]
    type: llm
    model: $WORKER        # or qwen3-8b / qwen3-30b-a3b / qwen2.5-72b
    input: single         # single (default): [system?, user=rendered prompt]
                          # chat: [system, user=rendered prompt, then the last history_turns steps
                          #        as assistant=<act role raw reply> / user="Output:\n<obs>"]
    system: "..."         # optional template
    prompt: "..."         # template
    max_tokens: 1536      # default 1536 (context window is 16384 tokens total; prompts are
                          # auto-truncated to fit)
    temperature: 0.0      # default 0
  - name: api_hints
    type: tool
    tool: tools/api_hints.py
  - name: coder
    type: llm
    ...
```

Template placeholders (only these are substituted; other braces are left untouched):
`{task}` supervisor name/email/phone + current date-time + instruction; `{observation}` output
of the previous step's code (or "(none yet)"); `{history}` last `history_turns` steps rendered as
"## Step k / code / Output"; `{step}` 1-based step number; `{blackboard}` all blackboard entries
as "[name]\ntext"; `{blackboard.NAME}` one entry. The blackboard persists across steps; each role
overwrites its own entry every step (so a role at step k sees later roles' outputs from step k-1).

If the act reply has no code block, nothing is executed and the next observation is
"Your reply had no ```python``` code block. Reply with exactly one." The episode ends when the
code calls `apis.supervisor.complete_task(...)`, at `max_steps`, at `token_budget`, or on a role
error (LLM error / tool exception -> reason `role_error`, see trace).

### Tool roles
`tools/x.py` must define `run(blackboard: dict, ctx: dict) -> dict`; `result["text"]` (str) is stored
in `blackboard[name]`. `blackboard` is a copy. Tools run in the task's worker process, must be
deterministic and cheap, and must NOT execute task code, call `apis`, or read anything under
`data/` (ground truth). `ctx` keys:

| key | value |
|---|---|
| `instruction` | task instruction string |
| `task` | the full `{task}` text |
| `supervisor` | `{first_name, last_name, email, phone_number}` |
| `datetime` | current simulated date-time string |
| `step` | 1-based step number |
| `observation` | previous step's execution output ("" at step 1) |
| `history` | list of `{step, raw, code, output}` for all past steps (raw = act role reply) |
| `api_docs` | read-only docs helper (below) |

`ctx["api_docs"]` methods: `apps() -> {app: description}`; `api_list(app) -> [{name, description}]`
(`[]` for unknown app); `api_doc(app, api) -> dict | None` (same JSON as
`apis.api_docs.show_api_doc`: parameters with types/required/defaults, response_schemas);
`search(query, k=10) -> [{app, api, description}]` (keyword match). Apps: api_docs, supervisor,
amazon, phone, file_system, spotify, venmo, gmail, splitwise, simple_note, todoist.

Example: `genomes/X_tool_example/` (tool injects the API-name catalog of apps mentioned in the
instruction; single-mode coder with `{history}`).

### Outputs
- `OUT/rows.jsonl`, one per task: `id`, `score` (= passed unit tests / all tests, dense),
  `hard` (all tests passed = TGC credit), `reason` ("ok"; else stop reason if the agent did not
  call complete_task -- max_steps / token_budget / role_error / llm_timeout / timeout / crash --
  else the first failed test, e.g. "assert answers match."), `detail` (first failed test),
  `soft` null, `tokens`, `usage {prompt, completion, calls}`, `answer` (last 3000 chars of a
  step-by-step trace summary), `blackboard` (final, each entry last 2000 chars), plus
  `steps`, `stop_reason`, `difficulty`.
- `OUT/report.json`: `score` (mean pass fraction), `hard_acc` (TGC, 0-1), `soft_mean` null,
  `tokens_per_sample`, `fail_reasons`, `fail_detail` (counts), `sgc` (0-1), `genome`, `split`,
  `worker`, `experiment_name` (unique per evaluation, AppWorld logs in
  `experiments/outputs/<it>`), `n`, `secs`.
- `OUT/traces/<task>.json`: full history, role outputs, failed tests.

### P0 genome reference numbers (qwen3-30b-a3b, `runs/`)

| split | hard_acc (TGC) | score (mean pass fraction) | sgc | tokens/task | wall clock (8 workers) |
|---|---|---|---|---|---|
| dev (57) | 0.070 (4/57) | 0.443 | 0.0 | 57k | 57 min under heavy contention on :8101 (3 tasks hit the 20-min kill and were rerun with `--resume`, 7 min) |
| train (90) | 0.178 (16/90) | 0.482 | 0.067 | 60k | 15 min |

P0 = run_baseline.py prompt and loop (TGC 5.3 there; the 1-task difference is vLLM batch
nondeterminism at temperature 0). The 30B server is shared; with other agents on it, dev
went from ~10 min to ~57 min. `--resume` reruns only missing or errored (timeout/crash) tasks;
`secs` in report.json then covers only the resumed part.
