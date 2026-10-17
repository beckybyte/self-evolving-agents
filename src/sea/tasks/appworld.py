"""AppWorld as a Task plugin. The environment loop stays in appworld/run_genome.py (own genome schema, own python env:
appworld/env); this plugin only adapts evaluation, child loading and the evolver's view."""
from __future__ import annotations

import json
from pathlib import Path

from calevo.evolve import _load_appworld_genome
from calevo.runner import evaluate_appworld

from .base import ROOT_DIR, Task


class AppWorldTask(Task):
    name = "appworld"

    def __init__(self, fit=None, holdout="appworld_dev", test=None, **kw):
        super().__init__(fit or {"appworld_train": 1.0}, holdout, test or ["appworld_test_normal"], None, **kw)

    def describe(self) -> str:
        return (ROOT_DIR / "tasks_desc" / "appworld.md").read_text()

    def schema_doc(self, allow_tools: bool = True) -> str:
        return (ROOT_DIR / "appworld" / "genome_schema.md").read_text()

    def load(self, gdir) -> dict:
        return _load_appworld_genome(Path(gdir))

    load_child = load

    def evaluate(self, gdir, split, worker, out, workers=8, limit=None) -> dict:
        return evaluate_appworld(gdir, split, worker, out, min(workers, 8), limit)

    def workspace_files(self, parent_eval: Path) -> dict[str, str]:
        files = super().workspace_files(parent_eval)
        ex = []
        for s in self.fit:
            for r in [json.loads(l) for l in open(Path(parent_eval) / s / "rows.jsonl")]:
                if r["score"] < 1.0 and len(ex) < 12:
                    ex.append({"split": s, "id": r["id"], "reason": r["reason"], "detail": r.get("detail"),
                               "score": r["score"], "trace": r["answer"][-3000:], "blackboard": r.get("blackboard", {})})
        files["failures.jsonl"] = "\n".join(json.dumps(e, ensure_ascii=False) for e in ex)
        return files
