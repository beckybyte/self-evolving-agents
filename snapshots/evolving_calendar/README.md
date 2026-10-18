# Evolving Calendar — apps written by evolved agent teams (2026-10-17)

Each folder holds `genome/` (the team the evolver designed: roles, per-role models, workflow) and `app/` (the code that
team wrote; nothing in `app/` was written by hand or by the evolver). Scores are from the frozen harnesses in `builds/`.

| Folder | Evaluator | fit | holdout | Team's code models |
|---|---|---|---|---|
| `v2_best_8818` | v2 (learning loop) | 1.0 (2 builds) | 0.989 | DeepSeek-V4.1-Flash code, gpt-oss UI, Claude Haiku as rescue only |
| `v2_sonnet_8817` | v2 | 1.0 (2 builds) | 0.951 | Claude Sonnet app + fixer, Nemotron UI |
| `v1_best_8814` | v1 (Iris flow) | 1.0 (6 builds) | 1.0 | Claude Haiku app, Nemotron UI + fixer |
| `v1_nemotron_only_8812` | v1 | 0.93 (6 builds) | 0.90 | Nemotron only |

Run one (Python 3.12, stdlib only; set CRUSOE_API for live models, or EVOLVE_MOCK=1 for rules only):
    cd snapshots/evolving_calendar/v2_best_8818/app && python app.py --port 8818 --data /tmp/evolve_data
Demo script and the full story: `DEMO.md`.
