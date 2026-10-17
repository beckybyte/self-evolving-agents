TASK the team solves: AppWorld (9 simulated apps: spotify, venmo, gmail, amazon, phone, splitwise, simple_note,
todoist, file_system; ~457 APIs). Each sample is a natural-language task from a supervisor. The team acts in a
stateful Python REPL: every step, all roles run in order and the `act` role's last ```python``` block is executed
(apis.<app>.<api>(...)); the observation is fed back. The episode ends when the code calls
apis.supervisor.complete_task(...), or at max_steps / token_budget.
SCORE = fraction of the task's hidden unit tests passed (database end-state + answer); full pass = task goal completion.
Read genome_schema.md: it defines llm roles (system/prompt templates, chat vs single input), tool roles
(run(blackboard, ctx) with read-only ctx.api_docs: apps(), api_list(app), api_doc(app, api), search(q)), and `act`.
Tool roles may ONLY read docs and the given ctx; they must never import appworld internals, read files, or touch
ground truth / evaluation (such genomes are rejected automatically).
Typical failures of the baseline: inventing API names/parameters, wrong login usage (passing a dict as password),
treating list responses as dicts, answering with full sentences or before paging through all results.
Evaluation is on unseen tasks (dev / test_normal), so prefer general mechanisms (planning, doc lookup, verification,
pagination helpers) over task-specific hacks.
