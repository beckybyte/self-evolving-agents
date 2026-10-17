# Open-ended self-improvement: design and status

The first version is a hill-climbing genetic algorithm: fixed goal, fixed data, agents that do not learn
(only the evolver swaps them). The overnight run showed its limits by hand: the target got "solved" by a regex
(calendar v1), the most valuable findings were unplanned, and human input was sparse. This document turns the
missing pieces into four mechanisms, each mapped onto the existing code.

| # | mechanism | idea | status |
|---|---|---|---|
| 1 | goal escalation | when a goal is saturated, the system proposes a bigger / vaguer goal and builds verified data for it | **manual precedent only** (v1 → `ood/build_v2.py`); automation planned |
| 2 | innovation in the blood | keep a diversity archive instead of one champion; force one radical child per generation; small novelty reward | planned |
| 3 | every agent keeps learning | per-role memory + reflection (fast loop) inside evolution (slow loop), memory inherited (Lamarckian) | **implemented** (`src/calevo/learn.py`, runner memory injection, `CALEVO_LEARN` in evolve) — early results below |
| 4 | ask humans, never wait | `ask_human` channel: post the question, continue on a stated best guess, fold the answer back in later | planned (today: "decisions for the user" list in the dev log) |

## 1. Goal escalation
A **goal-setter** role above the evolver. Trigger: dev ≥ 0.98 for two generations, or the team collapses to
zero LLM roles. It must (a) propose a broader goal (e.g. hard constraints → arbitrary, conflicting preferences →
asking / stating assumptions under ambiguity), (b) generate and verify new evaluation data (generate, blind
back-parse, feasibility check — the `build_v2.py` pipeline), (c) show the current champion drops on it.
Anti-cheating: the new goal must include the old one (old data kept as a regression set) and a few fixed
anchors (test_in, MATH test) can never change, so progress stays measurable.

## 2. Innovation
- MAP-Elites archive over (number of LLM roles, tokens, mechanism type) instead of a single greedy parent —
  the λ=0.005 8B team that keeps an LLM generalised better on hard data (0.587 vs 0.415) but would have been
  dropped by pure greed.
- Every generation must contain one structurally new child, judged with a looser survival bar.
- A small "interestingness" bonus from a judge model (has this mechanism been seen before?).

## 3. Continual learning of every agent (implemented)
- Each LLM role owns `memory/<role>.md`, injected into its own prompt.
- **Fast loop** (`calevo.learn.learn_round`): the team solves a batch from an *experience* split (MATH train,
  disjoint from dev/test); each LLM role reflects on its own outputs vs. the team result and reference, using the
  worker model (not the evolver), and rewrites its notes (≤ 12 bullets, ≤ 900 chars, general lessons only).
- **Slow loop** (`calevo.evolve` with `CALEVO_LEARN="math_train:40"`): children inherit the parent's notes for
  roles with the same name, learn one round, then get evaluated; the evolver can read and edit the notes.
- Experiments (MATH Lv5, Qwen3-8B worker): E1a single agent learning only; E1b evolved 6-role team learning
  only; E2 evolution + learning vs. the evolution-only control `evo_math`.

**Early result (honest):** E1a got *worse*: math_dev 0.719 → 0.669 → 0.661 over two rounds, tokens +25%.
The notes are generic platitudes ("verify constraints", "watch edge cases") that lengthen answers without fixing
a concrete error. Next changes: (a) accept a memory update only if it beats the old notes on a small held-out
batch, otherwise roll back; (b) require every note to name a concrete error pattern and its fix.

## 4. Ask humans without waiting
An `ask_human` tool writes the question (with context and the agent's default choice) to a shared inbox/doc,
the agent proceeds on that default and records the assumption; when an answer arrives it becomes memory and the
decisions taken under the wrong assumption are revisited. Human involvement then becomes sparser over time and
every answer is distilled into the system's "taste".

## Risks
Goal-setter gaming its own goals (mitigated by verifiable data + champion must drop + fixed anchors); memory bloat
and contradictions (cap + verified updates); ~30-50% more inference per generation for the fast loop.
