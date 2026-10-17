## Genome schema (run_genome.py) -- read this to write AppWorld genomes

```bash
cd <appworld dir>
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
