import json
from pathlib import Path

import yaml

from sea.executor import run_team
from sea.fitness import FitnessSpec
from sea.genome import check_tool_safety, load_genome, structure_signature
from sea.human import HumanChannel
from sea.llm import register_fake

ROOT = Path(__file__).resolve().parents[2]


def _write(d: Path, genome: dict, tools: dict | None = None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "genome.yaml").write_text(yaml.safe_dump(genome, allow_unicode=True))
    for name, src in (tools or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(src)
    return d


def test_v1_genomes_load_and_adapt():
    from sea.tasks.brief.task import BriefTask
    from sea.tasks.build import BuildTask
    keys = tuple(BriefTask.context_keys) + tuple(BuildTask.context_keys)  # snapshots hold brief and build genomes
    for gd in ["genomes/P0", "genomes/S0"] + [str(p.parent) for p in (ROOT / "snapshots").rglob("genome.yaml")]:
        g = load_genome(ROOT / gd, keys)
        if g["_legacy"]:
            assert g["outputs"]["answer"].startswith("blackboard.")


def test_v1_native_and_transcript_semantics(tmp_path):
    seen = []
    register_fake("echo", lambda msgs: (seen.append(msgs), "out:" + msgs[-1]["content"][:20])[1])
    gd = _write(tmp_path / "g", {"id": "x", "soul": "", "final": "b", "roles": [
        {"name": "a", "type": "llm", "model": "fake:echo", "input": "native", "prompt": "EXTRA"},
        {"name": "b", "type": "llm", "model": "fake:echo", "input": "transcript", "prompt": "C={conversation} B={blackboard}"}]})
    g = load_genome(gd)
    ep = {"id": "1", "input": {"messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]}}
    r = run_team(g, ep, "fake:echo")
    assert seen[0][0]["content"] == "SYS\n\nEXTRA" and seen[0][1]["content"] == "hi"
    assert seen[1][-1]["content"] == 'C=[SYSTEM]\nSYS\n\n[USER]\nhi B={"a": "out:hi"}'
    assert r["answer"] == r["blackboard"]["b"] and r["usage"]["calls"] == 2


def test_v2_artifacts_messages_loop_and_ask_human(tmp_path):
    def fake(msgs):
        u = msgs[-1]["content"]
        if u.startswith("DRAFT"):
            return ('<artifact name="prd">PRD v</artifact>\nASK_HUMAN: Who is the user? | DEFAULT: students\n'
                    'ASK_HUMAN: Mobile or web? | DEFAULT: mobile')
        return "APPROVE" if "PRD v" in u else "revise"
    register_fake("pm", fake)
    gd = _write(tmp_path / "g", {
        "schema": 2, "id": "t", "channels": "messages",
        "roles": [{"name": "pm", "kind": "llm", "model": "fake:pm", "prompt": "DRAFT {input}", "reads": ["input", "inbox"],
                   "writes": "message:critic"},
                  {"name": "critic", "kind": "llm", "model": "fake:pm", "prompt": "Review {inbox}", "reads": ["inbox"],
                   "writes": "artifact:review"},
                  {"name": "owner", "kind": "human", "question": "Ship {artifact.prd}?", "default": "yes"},
                  {"name": "pack", "kind": "tool", "tool": "tools/pack.py"}],
        "workflow": [{"loop": {"body": ["pm", "critic"], "max_rounds": 3, "stop_when": "APPROVE"}}, "owner", "pack"],
        "artifacts": [{"name": "prd"}], "outputs": {"answer": "artifact.final"}},
        {"tools/pack.py": "def run(ctx):\n    return {'text': 'packed', 'artifacts': {'final': ctx['artifacts']['prd'] + '!'}}\n"})
    g = load_genome(gd)
    h = HumanChannel(tmp_path / "inbox")
    r = run_team(g, {"id": "b", "input": {"text": "a day planner"}}, "fake:pm", h)
    assert r["artifacts"]["prd"] == "PRD v" and r["artifacts"]["review"] == "APPROVE" and r["answer"] == "PRD v!"
    assert [q["qid"] for q in h.questions()] == ["Q1", "Q2", "Q3"]
    assert {a["qid"] for a in r["assumptions"]} == {"Q1", "Q2", "Q3"}  # nobody answered yet: all are assumptions
    assert r["usage"]["calls"] == 2  # loop stopped after round 1
    h.answer("Q2", "web only")
    r2 = run_team(g, {"id": "b", "input": {"text": "a day planner"}}, "fake:pm", h)
    assert len(h.questions()) == 3  # deduplicated across runs
    assert {a["qid"] for a in r2["assumptions"]} == {"Q1", "Q3"}  # the answered question is no longer an assumption
    assert structure_signature(g).startswith("llht|m|L1")


def test_tool_safety_and_fitness(tmp_path):
    gd = _write(tmp_path / "g", {"schema": 2, "id": "t", "roles": [{"name": "a", "kind": "tool", "tool": "tools/a.py"}]},
                {"tools/a.py": "open('data/splits/test_in.jsonl')\ndef run(ctx):\n    return {}\n"})
    try:
        check_tool_safety(gd)
        assert False
    except AssertionError as e:
        assert "forbidden" in str(e)
    f = FitnessSpec.weighted_scores({"dev_in": 0.6, "ood_dev": 0.4}, 0.02, complexity=(-0.1, 100))
    assert f({"score:dev_in": 1.0, "score:ood_dev": 0.5, "tokens": 1000, "complexity": 50}) == round(0.8 - 0.02 - 0.05, 4)
    assert "0.6*score(dev_in)" in f.formula() and "- 0.02*tokens_per_run/1000" in f.formula()
