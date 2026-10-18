# usersim — LLM user simulator (generalization metric, not in fitness)

Written only from `builds/evolving_calendar_v3/contract.md` (no harness / scenarios / reference code read).

```
python builds/usersim/run.py <app_dir> --split fit|holdout --episodes N [--model crusoe:openai/gpt-oss-120b]
       [--parallel 3] [--max-steps 14] [--seed-base B] [--seeds 0,0,2] [--mock] [--out res.json]
python -m pytest builds/usersim/sim_test.py -q
```
- `personas.py`: 4 doc personas (Iris from the contract; Mina, Jordan, Sam invented) + `generate(n, seed)` (gpt-oss-120b,
  cached in `cache/personas_<seed>_<n>.json`).
- `sim.py`: `run_episode(base_url, persona, seed, model, max_steps=14)`; the `Checker` runs 12 invariants after every step:
  http (no 5xx, < 60 s), no_guilt, one_question, breakdown_shape, delete_preview, no_lost_item, undo_exact,
  proposal_number, no_hidden_item, names_task, plus two from contract 2 and 6-8: dump_fragments (no dump fragment lost,
  one check per dump) and clock_followthrough (a passed start gets a reminder; a task ended > 30 min ago is moved or
  checked in on). Seeded RNG picks the persona (fit: seed % 4), clock jumps and "idea for this step" nudges toward
  under-used actions. The user LLM runs at temperature 0.
- fit = seeds 0..N-1 over the doc personas; holdout = seed base from the clock (printed) + 4 generated personas.

## Calibration (2026-10-18, 6 episodes x 14 steps, holdout seed base 288532: Leila, Ana, Tobias, Ravi)

14 invariants: the 10 asked for, plus dump_fragments, clock_followthrough, needs_yes (contract 3: a new goal typed
into an open "break it down?" conversation is not a yes) and time_only_item (a time phrase such as "in the next 3 months"
must not become its own item). needs_yes is driven by a seeded probe: once per episode, while a breakdown offer is
open, the simulator types a new goal sentence ("I want to learn to swim in the next 3 months.") into that
conversation, as the UI does with chat-box text.

| app | fit mean pass | holdout mean pass | failing invariants (violations/checks, fit + holdout) |
|---|---|---|---|
| v3 reference | 0.994 | 0.992 | needs_yes 5/7, one_question 1/79 (echoes the person's own "?") |
| runs/demo/app_v3a5 | 0.986 | 0.988 | needs_yes 2/2, clock_followthrough 5/14, dump_fragments 2/8 |

Before the probe and the 4 extra invariants (an earlier run): reference 1.000 / 1.000, app_v3a5 0.976 / 0.994.
Noise (same seed again, temperature 0, measured before the probe): reference 1.0 every time; app_v3a5 seed 0
1.0 / 1.0 / 1.0, seed 2 0.927 / 0.982 / 0.982.
Cost: about $0.007 per episode (about 26k prompt + 4.7k completion tokens, 14 calls); 15-20 s per episode on
app_v3a5, 30-45 s on the reference. The apps' own model calls are not counted.
