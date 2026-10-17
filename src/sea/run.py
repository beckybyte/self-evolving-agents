"""Evaluate one genome on one split of any task.

  PYTHONPATH=src python -m sea.run --task bench:calendar --genome genomes/S0 --split dev_in --limit 10 --out runs/x
  PYTHONPATH=src python -m sea.run --task brief:briefs/day_planner.md --genome genomes/v2/brief_P0 --split fit --out runs/b
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .tasks import make_task

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--genome", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--worker", default="qwen3-30b-a3b")
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-dir", help="where the task keeps state (inbox, goal/); default = --out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--judge")
    ap.add_argument("--framer")
    a = ap.parse_args()
    opts = {k: v for k, v in {"judge": a.judge, "framer": a.framer}.items() if v}
    task = make_task(a.task, a.run_dir or a.out, **opts)
    rep = task.evaluate(a.genome, a.split, a.worker, Path(a.out), a.workers, a.limit)
    print(json.dumps(rep, indent=1, ensure_ascii=False))
