import json
import subprocess
from pathlib import Path

from sea.meta.backlog import Backlog, detect
from sea.meta.protect import violations


def test_backlog_dedupe_and_status(tmp_path):
    b = Backlog(tmp_path)
    p = b.add("Judge flat", "evaluator", "e1", key="judge-flat")
    q = b.add("Judge flat again", "evaluator", "e2", key="judge-flat")
    assert p["id"] == q["id"] == "P001" and b.get("P001")["count"] == 2 and "e2" in b.get("P001")["body"]
    b.set_status("P001", "merged", "done")
    assert b.add("Judge flat", "evaluator", "e3", key="judge-flat")["id"] == "P002"  # merged ones are not reopened


def test_detectors(tmp_path):
    run = tmp_path / "run"
    (run / "evals/c1/v1/fit").mkdir(parents=True)
    (run / "evals/c1/v1/fit/rows.jsonl").write_text("\n".join(json.dumps({"score": 0.475, "reason": "low:x"}) for _ in range(4)))
    hist = [{"event": "invalid_genome", "error": f"AssertionError('workflow step {i!r} is not a role')"} for i in range(3)]
    (run / "history.jsonl").write_text("\n".join(json.dumps(h) for h in hist))
    (run / "workspaces/gen01_a1").mkdir(parents=True)
    (run / "workspaces/gen01_a1/FRAMEWORK_WISHES.md").write_text("Parallel branches with a vote\nbecause ...")
    keys = {d["key"] for d in detect(run)}
    assert "judge-flat" in keys and any(k.startswith("invalid:") for k in keys) and any(k.startswith("wish:") for k in keys)


def test_protected_surface():
    f, h = violations(["src/sea/executor.py", "src/sea/tasks/brief/judge.py", "data/splits/x.jsonl", "src/sea/meta/bench.py"], "learning")
    assert set(f) == {"src/sea/tasks/brief/judge.py", "data/splits/x.jsonl", "src/sea/meta/bench.py"} and h == []
    f, h = violations(["src/sea/tasks/brief/judge.py"], "evaluator")
    assert f == [] and h == ["src/sea/tasks/brief/judge.py"]
