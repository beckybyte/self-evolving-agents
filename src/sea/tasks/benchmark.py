"""Benchmarks with ground truth (calendar / math / drop) as Task plugins: data/splits + calevo.tasks.score.
Used for regression checks of the v2 core against v1 (calevo.runner / calevo.evolve)."""
from __future__ import annotations

import json
from pathlib import Path

from calevo.tasks import score as calevo_score

from .base import ROOT_DIR, Task, parse_split

SPLITS = ROOT_DIR / "data" / "splits"
DEFAULTS = {  # fit weights, holdout (gating), experience (learning), test
    "calendar": ({"dev_in": 0.6, "ood_dev": 0.4}, "ood_dev_c", "dev_hard@0:40", ["test_in", "ood_test", "test_v2"]),
    "math": ({"math_dev": 1.0}, "math_train@300:360", "math_train@0:300", ["math_test"]),
    "drop": ({"drop_dev@0:150": 1.0}, "drop_dev@150:200", None, ["drop_test"]),
}
CALENDAR_DESC = """TASK the team solves: a multi-turn conversation where a user adds/changes calendar events with time constraints.
The team must output ONE final reply containing the full calendar as a JSON list
[{"event_id": int, "event_name": str, "start_time": "HH:MM", "duration": int}], all events 10:00-16:00,
no overlaps, every constraint honoured. Some samples (split ood_dev) also contain soft user PREFERENCES phrased
in natural, sometimes fuzzy language; satisfying them earns extra credit (see spec_A.md). Future evaluation will
include preference types you have NOT seen, so prefer general mechanisms over hard-coding the ones you see.
Episode input = the chat messages (selector `input`; a v1-style `input: native` role gets them as chat turns).
The scored answer is outputs.answer."""
CALENDAR_TOOLS = """
CALENDAR HELPER LIBRARY (importable from tool code):
  from calevo.verifier import extract_calendar, to_min, parse_constraint, check_pref, hard_ok
  from calevo.solver import solve, to_calendar_json   # solve(events, prefs=None) -> {event_id: start_minute} | None
     events = {id: {"duration": int, "constraint": "before 2pm"|"after 11:30am"|"at 1pm"|"between 10am and 2pm"|None,
                     "min_time": "10:00", "max_time": "16:00"}}
     prefs  = [{"type": "prefer_window", "event_id": i, "start": "HH:MM", "end": "HH:MM"}]   # the only built-in pref type
The final answer must not contain "<think>".
"""


class BenchmarkTask(Task):
    def __init__(self, bench: str, fit=None, holdout=None, test=None, experience=None, **kw):
        d = DEFAULTS[bench]
        super().__init__(fit or d[0], holdout if holdout is not None else d[1], test or d[3],
                         experience if experience is not None else d[2], **kw)
        self.name = bench
        self._cache: dict = {}

    def describe(self) -> str:
        if self.name == "calendar":
            return CALENDAR_DESC
        return (ROOT_DIR / "tasks_desc" / f"{self.name}.md").read_text() + \
            "\nEpisode input = the chat messages (selector `input`). The scored answer is outputs.answer."

    def schema_doc(self, allow_tools: bool = True) -> str:
        return super().schema_doc(allow_tools) + (CALENDAR_TOOLS if self.name == "calendar" and allow_tools else "")

    def rows(self, split: str) -> list[dict]:
        if split not in self._cache:
            name, a, b = parse_split(split)
            self._cache[split] = [json.loads(l) for l in open(SPLITS / f"{name}.jsonl")][a:b]
        return self._cache[split]

    def episodes(self, split: str) -> list[dict]:
        return [{"id": r["id"], "input": {"messages": r["messages"]}, "context": {}, "sample": r} for r in self.rows(split)]

    def score(self, episode: dict, result: dict) -> dict:
        return calevo_score(episode["sample"], result["answer"])

    def workspace_files(self, parent_eval: Path) -> dict[str, str]:
        files = super().workspace_files(parent_eval)
        ex = []
        for s in self.fit:
            data = {r["id"]: r for r in self.rows(s)}
            rows = [json.loads(l) for l in open(Path(parent_eval) / s / "rows.jsonl")]
            for r in [r for r in rows if r["score"] < 1.0][:12]:
                d = data[r["id"]]
                ex.append({"split": s, "id": r["id"], "reason": r["reason"], "detail": r.get("detail"), "score": r["score"],
                           "last_user_message": d["messages"][-1]["content"][-1200:], "expected": d["state"],
                           "model_answer": r["answer"][-1500:], "blackboard": r.get("blackboard", {})})
        files["failures.jsonl"] = "\n".join(json.dumps(e, ensure_ascii=False) for e in ex)
        if self.name == "calendar":
            files["spec_A.md"] = (ROOT_DIR / "ood" / "spec_A.md").read_text()
        return files
