"""Brief mode + evolution loop end to end with fake models (no servers, no Claude)."""
import json
import os
from pathlib import Path

from sea.evolve import Evolution
from sea.llm import register_fake
from sea.tasks import make_task

ROOT = Path(__file__).resolve().parents[2]

GOAL = {
    "restatement": "A day planner for busy people.",
    "assumptions": [{"id": "A1", "text": "mobile first", "default": "mobile", "confidence": 0.5}],
    "questions": [{"question": "Who is the main user?", "default": "knowledge workers", "assumption": "A1", "why": "scope"}],
    "personas": [{"id": f"P{i}", "name": f"p{i}", "profile": "", "goals": "", "constraints": "", "pain_points": ""} for i in range(1, 5)],
    "scenarios": [{"id": f"S{i}", "persona": f"P{1 + i % 4}", "situation": "", "needs": "", "success": ""} for i in range(1, 7)],
    "rubric": [{"id": "coverage", "scope": "scenario", "weight": 2, "description": "", "anchors": {}},
               {"id": "tradeoff_rationale", "scope": "global", "weight": 1, "description": "", "anchors": {}},
               {"id": "scope_discipline", "scope": "global", "weight": 1, "description": "", "anchors": {}}],
    "cost_model": "S=1 M=3 L=8", "changelog": "initial"}


def fake_framer(msgs):
    g = dict(GOAL)
    if "NEW HUMAN INPUT" in msgs[-1]["content"]:
        g = {**g, "assumptions": [{**GOAL["assumptions"][0], "status": "rejected"}], "changelog": "Q1 answered: students"}
    return "```json\n" + json.dumps(g) + "\n```"


def fake_judge(msgs):
    u = msgs[-1]["content"]
    good = "CRITIC-APPROVED" in u
    if "GLOBAL criterion" in u:
        return json.dumps({"scores": {"tradeoff_rationale": 4 if good else 2, "scope_discipline": 3},
                           "features": [{"name": "a", "size": "M"}, {"name": "b", "size": "S"}], "worst_gap": "g"})
    return json.dumps({"walkthrough": "w", "scores": {"coverage": 5 if good else 3}, "worst_gap": "misses offline use"})


def fake_worker(msgs):
    return '<artifact name="prd">PRD</artifact><artifact name="decisions">D1 -> A1</artifact>\nASK_HUMAN: Web too? | DEFAULT: no'


def test_brief_task_and_evolution(tmp_path, monkeypatch):
    register_fake("framer", fake_framer)
    register_fake("judge", fake_judge)
    register_fake("worker", fake_worker)
    run = tmp_path / "run"
    task = make_task(f"brief:{ROOT / 'briefs/day_planner.md'}", run, lam_tokens=0.002, framer="fake:framer", judge="fake:judge")
    assert task.version() == "v1" and (run / "goal/v1/rubric.json").exists()
    assert [q["asker"] for q in task.human.questions()] == ["framer"]
    split = {s["split"] for s in task.goal()["scenarios"]}
    assert split == {"fit", "holdout"}

    rep = task.evaluate(ROOT / "genomes/v2/brief_P0", "fit", "fake:worker", run / "e" / "fit")
    # coverage 3 -> 0.5, rationale 2 -> 0.25, scope 3 -> 0.5; weights .5/.25/.25
    assert abs(rep["score"] - (0.5 * 0.5 + 0.25 * 0.25 + 0.25 * 0.5)) < 1e-6 and rep["metrics"]["complexity"] == 4
    assert rep["artifacts_present"] == 1.0 and any(q["question"] == "Web too?" for q in rep["team_questions"])

    monkeypatch.setenv("SEA_FAKE_EVOLVER", "2")
    monkeypatch.setenv("SEA_META_SIGNALS", "0")  # fake runs must not file framework proposals
    ev = Evolution(task, run, "fake:worker", ROOT / "genomes/v2/brief_P0", gens=1, k=2, max_attempts=1, margin=0.02, radical=True)
    task.human.answer("Q1", "students")  # arrives before the first attempt -> evaluation revised to v2
    st = ev.run()
    assert st["done"] and task.version() == "v2"
    hist = [json.loads(l) for l in open(run / "history.jsonl")]
    assert any(h.get("event") == "eval_revised" for h in hist)
    assert (run / "rationale" / "gen01_a1.md").exists() and "Evaluation revised v1 -> v2" in (run / "rationale" / "gen01_a1.md").read_text()
    assert "students" in (Path(st["parent"]) / "memory" / "_shared.md").read_text()
    assert "REVIEW" in task.workspace_files(Path(st["parent_eval"]))["human.md"]
    assert json.loads((run / "archive.json").read_text())
    os.environ.pop("SEA_FAKE_EVOLVER", None)


def test_gated_learning_rejects_useless_notes(tmp_path):
    from sea.learn import learn_round
    register_fake("framer", fake_framer)
    register_fake("judge", fake_judge)
    register_fake("worker", lambda m: "- note" if "Rewrite your notes" in m[-1]["content"] else fake_worker(m))
    run = tmp_path / "run"
    task = make_task(f"brief:{ROOT / 'briefs/day_planner.md'}", run, framer="fake:framer", judge="fake:judge")
    st = learn_round(task, ROOT / "genomes/v2/brief_P0", tmp_path / "out_g", "fake:worker", tmp_path / "learn")
    assert st["candidates"] == ["pm"] and st["accepted"] is False  # notes do not change the judged design
    assert not (tmp_path / "out_g/memory/pm.md").exists() and (tmp_path / "out_g/memory/_rejected.jsonl").exists()


def test_shared_taste_single_source_with_comments_and_cap(tmp_path):
    import shutil
    from sea.executor import run_team
    from sea.tasks.brief.task import TASTE_MAX_BULLETS, TASTE_MAX_CHARS
    register_fake("framer", fake_framer)
    register_fake("judge", fake_judge)
    seen = []
    register_fake("rec", lambda m: (seen.append(m), fake_worker(m))[1])
    run = tmp_path / "run"
    task = make_task(f"brief:{ROOT / 'briefs/day_planner.md'}", run, framer="fake:framer", judge="fake:judge")
    task.human.answer("Q1", "students and freelancers")
    task.human.rate("gen01a1_c2", 2, "too many features")
    task.human.rate("gen01a1_c3", 4, "")  # a bare score adds no taste
    taste = task.shared_notes()
    assert "students and freelancers" in taste and "too many features" in taste and "gen01a1_c3" not in taste

    # a role reading {human_answers} gets the taste exactly once (no system-message copy of memory/_shared.md);
    # a role that does not read it still gets it in its system message
    gd = tmp_path / "g"
    shutil.copytree(ROOT / "genomes/v2/brief_P0", gd)
    (gd / "memory").mkdir(exist_ok=True)
    (gd / "memory" / "_shared.md").write_text(taste + "\n")
    run_team(task.load(gd), task.episodes("fit")[0], "fake:rec", task.human)
    prompt = "\n".join(m["content"] for m in seen[-1])
    assert prompt.count("students and freelancers") == 1 and prompt.count("too many features") == 1
    y = (gd / "genome.yaml").read_text()
    (gd / "genome.yaml").write_text(y.replace(", human_answers]", "]").replace("{human_answers}", "(see system message)"))
    run_team(task.load(gd), task.episodes("fit")[0], "fake:rec", task.human)
    assert seen[-1][0]["role"] == "system" and "too many features" in seen[-1][0]["content"]
    assert "too many features" not in seen[-1][-1]["content"]

    # capped: many answers and comments -> at most TASTE_MAX_BULLETS bullets / TASTE_MAX_CHARS chars, newest kept
    for i in range(30):
        q = task.human.ask(f"question number {i}?", "d", asker="pm")
        task.human.answer(q["qid"], f"answer {i} " + "x" * 150)
        task.human.rate(f"g{i}", 3, f"comment {i}")
    taste = task.shared_notes()
    assert len(taste) <= TASTE_MAX_CHARS and len(taste.splitlines()) <= TASTE_MAX_BULLETS
    assert "answer 29" in taste and "comment 29" in taste and "answer 0 " not in taste
    assert task.shared_notes() == taste and task.context()["human_answers"] == taste  # deterministic, one source
