# Snapshot v1 — model iteration has hit the top (2026-10-17, 14:23 UTC)

**Verdict:** on this data the evolution loop saturates. In-distribution the task is solved (dev 1.0 by gen 3–5,
test_in 1.000); later generations only shave tokens. The next round must iterate the **data**, not the team:
harder, non-templated ambiguity that has no closed-form solver.

## Test-set results (score; tokens/sample)
| worker | team | test_in | ood_test (unseen prefs) | test_v2 (long convos) |
|---|---|---|---|---|
| 30B | P0 single prompt | 0.320 (4141) | 0.150 | 0.359 |
| 30B | S0 fixed-DSL solver | 0.414 (4294) | 0.121 | 0.418 |
| 30B | **evo_full** (10 gens, clean) | **1.000 (907)** | 0.625 | **0.824** |
| 30B | evo_full_leaky (saw B pref names; reference only) | 0.984 (2981) | 0.925 | 0.723 |
| 8B | P0 | 0.242 | 0.092 | 0.383 |
| 8B | evo_full_8b (0-token regex) | 1.000 (0) | 0.575 | 0.410 |
| 8B | + dev_v2 in fitness (v2guard; test_v2 in-dist) | 0.984 (0) | 0.583 | 0.981 |
| 8B | + dev_v2, λ=0.005 (keeps the LLM) | 1.000 (2566) | 0.629 | 0.953 |

evo_prompt (prompt-only, 30B) was still evolving at snapshot time (best dev fitness 0.523); genome included as-is.

## Did the number of agents ("employees") change?
Yes — but it grew from 1 to at most 3 and then *specialised*, and under a strong token penalty it shrank to **zero LLM agents**.

| run | accepted parent's role count by generation | final team |
|---|---|---|
| evo_full (30B) | 1 → 2 → 3, then stable 3 for 8 gens | digest (code) + extract (LLM) + answer (code) |
| evo_prompt (30B, no tools) | 1 → 2, stable | draft (LLM) + verifier (LLM) — 7–8k tok, never above 0.52 |
| evo_full_8b | 1 → 2 → 3 → **1 tool, 0 LLM** | solver (pure regex + search) |
| v2guard (8B) | 1 → **1 tool, 0 LLM** from gen 1 | planner (pure code) |
| λ=0.005 (8B) | 1 → 2 (×7 gens) → 3 | brief (code) + assistant (LLM) + repair (code) |

Children proposed (all attempts): 2- and 3-role teams dominate; 4 roles were proposed once and never won.
Pattern: the evolver "hires" deterministic code roles around a single LLM role, rather than more LLM employees;
with λ=0.02 on 8B it fires the last LLM. More LLM employees never paid for their tokens on this task.

## Why this is the top
- Closed grammar (5 hard-constraint types, ≤7 events, 10:00–16:00, template-generated phrasing) → solvable by code.
- The intelligence sits in the evolver (Claude Opus writing ~1000-line solvers), not in multi-agent collaboration;
  worker size barely matters once code takes over.
- Remaining failures are exactly what the templates do not cover: unseen preference types (right_after ~20% met)
  and new phrasing (regex 0.41 on v2).

## What is in here
genomes/ (P0, S0 and every run's best genome incl. tools) · *_history.jsonl (every evaluated child) ·
final_reports/ (test-split report.json) · REPORT.md · report_curves.png · STATUS.md (full overnight log) · spec.md
Code at git tag `v1-model-top`.
