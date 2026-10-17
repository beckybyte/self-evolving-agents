"""Frozen safety checks (tier A), run from the BASE tree against the code under test (PYTHONPATH=<tree>/src).
Isolation must hold whatever the framework improver changed."""
import json
import shutil
from pathlib import Path

import yaml

from sea.genome import check_tool_safety
from sea.llm import register_fake
from sea.tasks import make_task

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parents[1]


def test_tool_safety_rejects_eval_access(tmp_path):
    for src in ["open('data/splits/test_in.jsonl')", "p = 'runs/x/goal/v1/scenarios.jsonl'", "x = '../secret'",
                "s = 'holdout'"]:
        d = tmp_path / str(abs(hash(src)))
        (d / "tools").mkdir(parents=True)
        (d / "tools" / "t.py").write_text(src + "\ndef run(ctx):\n    return {}\n")
        try:
            check_tool_safety(d)
        except AssertionError:
            continue
        raise AssertionError(f"tool safety accepted: {src}")


def test_brief_workspace_hides_holdout(tmp_path):
    goal = json.loads((BENCH / "goal_day_planner" / "goal.json").read_text())
    hold = [s for s in goal["scenarios"] if s["split"] == "holdout"]
    assert hold
    register_fake("w", lambda m: '<artifact name="prd">P</artifact><artifact name="decisions">D</artifact>')
    register_fake("j", lambda m: json.dumps({"scores": {c["id"]: 3 for c in goal["rubric"]}, "walkthrough": "w",
                                             "worst_gap": "g", "features": []}))
    run = tmp_path / "run"
    (run / "goal").mkdir(parents=True)
    shutil.copytree(BENCH / "goal_day_planner", run / "goal" / "v1")
    (run / "goal" / "CURRENT").write_text("v1")
    task = make_task(f"brief:{ROOT / 'briefs' / 'day_planner.md'}", run, framer="fake:j", judge="fake:j")
    ev = run / "ev"
    task.evaluate(ROOT / "genomes" / "v2" / "brief_P0", "fit", "fake:w", ev / "fit")
    files = task.workspace_files(ev)
    blob = "\n".join(files.values())
    for s in hold:
        assert s["situation"][:40] not in blob, f"holdout scenario {s['id']} leaked into the evolver workspace"


def test_benchmark_workspace_only_fit_splits(tmp_path):
    register_fake("w2", lambda m: "[]")
    task = make_task("bench:calendar", tmp_path / "cal", fit="dev_in@0:3:1.0")
    ev = tmp_path / "ev"
    task.evaluate(ROOT / "genomes" / "P0", "dev_in@0:3", "fake:w2", ev / "dev_in@0:3")
    files = task.workspace_files(ev)
    blob = "\n".join(files.values())
    for split in ("test_in", "ood_test", "test_v2", "ood_dev_c"):
        rows = [json.loads(l) for l in open(ROOT / "data" / "splits" / f"{split}.jsonl")][:20]
        assert not any(f'"{r["id"]}"' in blob for r in rows), f"{split} sample leaked"
