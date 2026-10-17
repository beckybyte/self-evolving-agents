"""Tier B driver: run a fixed set of genomes on fixed episodes with a deterministic recording fake LLM and dump every
LLM call. Run it from the BASE tree with PYTHONPATH pointing at the tree under test, so the driver cannot be changed
by the candidate while the code it exercises can:

  PYTHONPATH=<tree>/src python <base>/src/sea/meta/trace.py --bench <base>/meta/bench --root <base> --out traces.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

CAL_GENOMES = ["genomes/P0", "genomes/S0"]
CAL_EPISODES = "dev_in@0:5"


def fake_reply(messages) -> str:
    h = hashlib.sha1(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]
    return (f'<artifact name="prd">PRD-{h}</artifact>\n<artifact name="decisions">D-{h} depends on A1</artifact>\n'
            f'APPROVE PASS NO_BREAKS\n[{{"event_id": 0, "event_name": "x", "duration": 30, "constraint": null}}]')


def main(bench: Path, root: Path, out: Path):
    from sea.llm import register_fake
    calls: list = []
    register_fake("trace", lambda m: (calls.append(m), fake_reply(m))[1])
    from sea.executor import run_team
    from sea.tasks import make_task
    traces = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cal = make_task("bench:calendar", tmp / "cal")
        for gd in CAL_GENOMES:
            g = cal.load(root / gd)
            for ep in cal.episodes(CAL_EPISODES):
                calls.clear()
                run_team(g, ep, "fake:trace", None)
                traces[f"{gd}|{ep['id']}"] = list(calls)
        run = tmp / "brief"
        (run / "goal").mkdir(parents=True)
        shutil.copytree(bench / "goal_day_planner", run / "goal" / "v1")
        (run / "goal" / "CURRENT").write_text("v1")
        task = make_task(f"brief:{root / 'briefs' / 'day_planner.md'}", run, framer="fake:trace", judge="fake:trace")
        q = task.human.ask("第一版主要面向谁?", "会议多的上班族", asker="framer")
        task.human.answer(q["qid"], "自主时间多的学生和自由职业者")
        for gd in ["genomes/v2/brief_P0"] + sorted(str(p.relative_to(root)) for p in (bench / "genomes").iterdir()):
            g = task.load(root / gd)
            calls.clear()
            run_team(g, task.episodes("fit")[0], "fake:trace", task.human)
            traces[f"{gd}|brief"] = list(calls)
    out.write_text(json.dumps(traces, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sys.setrecursionlimit(10000)
    main(Path(a.bench), Path(a.root), Path(a.out))
