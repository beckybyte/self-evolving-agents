"""BuildTask end to end with a fake team and a stub harness (no servers, no real harness)."""
import json
import os
from pathlib import Path

import pytest

from sea.evolve import Evolution
from sea.llm import register_fake
from sea.tasks import make_task
from sea.tasks.build import artifact_path, materialise, strip_fence

ROOT = Path(__file__).resolve().parents[2]

STUB_HARNESS = '''
from pathlib import Path
CALLS = []
def run_split(app_dir, split, timeout=60, workers=3):
    app_dir = Path(app_dir)
    CALLS.append((str(app_dir), split))
    src = (app_dir / "app.py").read_text()
    ok_health = "health" in src
    ok_ui = (app_dir / "index.html").exists()
    ids = {"fit": ["F1", "F2"], "holdout": ["H1"]}[split]
    rows = []
    for i in ids:
        checks = [{"name": "health", "ok": ok_health, "detail": ""}, {"name": "ui", "ok": ok_ui, "detail": ""}]
        s = sum(c["ok"] for c in checks) / len(checks)
        rows.append({"id": i, "score": s, "hard": s == 1.0, "reason": "ok" if s == 1 else "checks_failed",
                     "detail": "", "checks": checks, "secs": 0.1})
    return rows
'''
STUB_SCENARIOS = '''
SPLITS = {"fit": [{"id": "F1", "persona": "iris", "description": "types a dentist appointment"},
                  {"id": "F2", "persona": "iris", "description": "dumps four things"}],
          "holdout": [{"id": "H1", "persona": "sam", "description": "SECRET holdout scenario text"}]}
'''
APP = "```python\nimport json\n# GET /health\nprint('hi')\n```"


def spec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "contract.md").write_text("# Contract\nGET /health -> {\"ok\": true}\n")
    (d / "harness.py").write_text(STUB_HARNESS)
    (d / "scenarios.py").write_text(STUB_SCENARIOS)
    return d


def fake_dev(msgs):
    u = msgs[-1]["content"]
    assert "SECRET" not in u  # holdout content never reaches the team
    return (f'plan first\n<artifact name="app_py">{APP}</artifact>\n'
            '<artifact name="index_html"><html id="chat-input"></html></artifact>\n'
            '<artifact name="__evil_py">x</artifact><artifact name="notes">not a file</artifact>')


def test_artifact_mapping():
    assert artifact_path("app_py") == "app.py"
    assert artifact_path("index_html") == "index.html"
    assert artifact_path("static__app_js") == "static/app.js"
    assert artifact_path("my_agents_py") == "my_agents.py"
    assert artifact_path("plan") is None and artifact_path("_py") is None and artifact_path("app_exe") is None
    assert strip_fence("```python\nx = 1\n```") == "x = 1\n"
    assert strip_fence("x = 1") == "x = 1\n"
    assert strip_fence("```\n<html></html>\n```\n") == "<html></html>\n"


def test_materialise_rejects_escapes(tmp_path):
    app = tmp_path / "app"
    written, rejected = materialise({"app_py": "a", "__etc__passwd_txt": "x", "static__app_js": "b", "plan": "p"}, app)
    assert written == ["app.py", "static/app.js"] and rejected == ["__etc__passwd_txt"]
    assert not (tmp_path / "etc").exists() and (app / "static" / "app.js").read_text() == "b\n"


def test_build_task_end_to_end(tmp_path, monkeypatch):
    register_fake("dev", fake_dev)
    d = spec_dir(tmp_path)
    task = make_task(f"build:{d}", tmp_path / "run")
    assert task.lam_tokens == 0.001
    assert "app_py -> app.py" in task.describe() and "parallel" in task.describe() and "SECRET" not in task.describe()
    assert "parallel" in task.moves()
    ev = task.evaluate_fit(ROOT / "genomes/v2/build_P0", "fake:dev", tmp_path / "ev")
    fit, hold = ev["reports"]["fit"], ev["holdout"]
    assert fit["score"] == 1.0 and fit["n"] == 2 and hold["score"] == 1.0 and hold["n"] == 1
    assert fit["files"] == ["app.py", "index.html"] and fit["rejected_files"] == ["__evil_py"]
    assert (tmp_path / "ev" / "app" / "app.py").read_text().startswith("import json")
    # one team run; both splits used the same materialised app
    team = json.loads((tmp_path / "ev" / "team.json").read_text())
    assert team["usage"]["calls"] == 1 and fit["tokens_per_sample"] == team["tokens"] > 0
    calls = task.harness().CALLS
    assert [s for _, s in calls] == ["fit", "holdout"] and len({a for a, _ in calls}) == 1
    assert ev["fitness"] == round(1.0 - 0.001 * team["tokens"] / 1000, 4)
    assert ev["metrics"]["score:holdout"] == 1.0
    files = task.workspace_files(tmp_path / "ev")
    assert {"contract.md", "reports.json", "fit_rows.jsonl", "app/app.py", "app/index.html"} <= set(files)
    blob = "\n".join(files.values())
    assert "SECRET" not in blob and "H1" not in blob
    assert json.loads(files["reports.json"])["holdout_aggregate"] == {"score": 1.0}
    assert "checks" in json.loads(files["fit_rows.jsonl"].splitlines()[0])
    assert "holdout 1.0" in task.summarize_child(ev)


def test_build_task_no_app(tmp_path):
    register_fake("mute", lambda msgs: "I could not write anything")
    task = make_task(f"build:{spec_dir(tmp_path)}", tmp_path / "run")
    ev = task.evaluate_fit(ROOT / "genomes/v2/build_P0", "fake:mute", tmp_path / "ev")
    assert ev["reports"]["fit"]["score"] == 0.0 and ev["reports"]["fit"]["fail_reasons"] == {"no_app_py": 2}
    assert task.harness().CALLS == []


def test_build_p1_parallel_and_evolution(tmp_path, monkeypatch):
    seen = []

    def team(msgs):
        u = msgs[-1]["content"]
        if "You are the architect" in u:
            seen.append("architect")
            return "plan: app.py imports agents.parse(text)"
        if "You write app.py" in u:
            return '<artifact name="app_py">import agents\n# /health\n</artifact>'
        if "You write agents.py" in u:
            return '<artifact name="agents_py">def parse(t): return []</artifact>'
        if "You write index.html" in u:
            return '<artifact name="index_html"><html></html></artifact>'
        assert "### artifact: app_py" in u and "### artifact: agents_py" in u and "### artifact: index_html" in u
        return 'fixed\n<artifact name="agents_py">def parse(text): return []</artifact>'
    register_fake("team", team)
    task = make_task(f"build:{spec_dir(tmp_path)}", tmp_path / "run")
    ev = task.evaluate_fit(ROOT / "genomes/v2/build_P1", "fake:team", tmp_path / "ev")
    assert ev["reports"]["fit"]["files"] == ["agents.py", "app.py", "index.html"]
    assert (tmp_path / "ev" / "app" / "agents.py").read_text() == "def parse(text): return []\n"
    assert seen == ["architect"] and ev["reports"]["fit"]["score"] == 1.0

    monkeypatch.setenv("SEA_FAKE_EVOLVER", "1")
    monkeypatch.setenv("SEA_META_SIGNALS", "0")
    st = Evolution(task, tmp_path / "evo", "fake:team", ROOT / "genomes/v2/build_P1", gens=1, k=1, max_attempts=1,
                   patience=1).run()
    assert st["done"] and st["parent_fit"] is not None
    ws = next((tmp_path / "evo" / "workspaces").glob("gen01_a1"))
    assert (ws / "app" / "app.py").exists() and (ws / "fit_rows.jsonl").exists()
    assert "SECRET" not in "".join(p.read_text() for p in ws.rglob("*") if p.is_file() and "parent" not in p.parts)


@pytest.mark.skipif(not (ROOT / "builds/evolving_calendar/scenarios.py").exists(), reason="real scenarios not written yet")
def test_real_spec_loads(tmp_path):
    task = make_task(f"build:{ROOT / 'builds/evolving_calendar'}", tmp_path)
    fit, hold = task.scenarios("fit"), task.scenarios("holdout")
    assert fit and hold and not ({s["id"] for s in fit} & {s["id"] for s in hold})
    assert all(s["description"] not in task.describe() for s in hold)


def test_genome_code_guard():
    from sea.tasks.build import MAX_GENOME_CODE_LINES, code_lines
    prose = "Write app_py. Endpoints: POST /api/chat returns {messages}. Ask why first; first step <= 15 min."
    assert code_lines(prose) == 0
    code = "\n".join(["import json", "def f(x):", "    if x:", "        return 1", "y = 2"] * 6)
    assert code_lines(code) > MAX_GENOME_CODE_LINES
