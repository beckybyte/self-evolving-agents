"""Framework check roles: app_smoke on real tiny apps (starts processes), and the check kind in genome/executor."""
import os
import time
from pathlib import Path

import pytest
import yaml

from sea.checks import app_smoke, shape_problems
from sea.executor import run_team
from sea.genome import load_genome
from sea.llm import register_fake
from sea.tasks import make_task

GOOD_APP = r'''
import argparse, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ITEM = {"id": "i1", "kind": "task", "title": "call mom", "start": None, "end": None, "window": None, "status": "open",
        "why": None, "parent_id": None, "estimate_min": None, "postpone_count": 0, "source": "dump"}
MSG = {"id": "m1", "role": "agent", "kind": "reply", "text": "ok", "item_id": None, "at": "2026-10-19T09:00", "undo_id": None}
class H(BaseHTTPRequestHandler):
    def send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        PIDFILE
        if self.path == "/health": return self.send({"ok": True})
        if self.path == "/api/state": return self.send({"now": "2026-10-19T09:00", "items": [ITEM], "messages": [MSG]})
        if self.path == "/api/focus": return self.send({"item": ITEM, "parent": None, "why": None, "action": "Start 5 minutes"})
        self.send_error(404)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/reset": return self.send({"now": "2026-10-19T09:00", "items": [], "messages": []})
        if self.path == "/api/command": return self.send({"messages": [dict(MSG, undo_id="u1")], "created": ["i1"]})
        if self.path == "/api/chat":
            CHAT
            return self.send({"messages": [MSG]})
        if self.path == "/api/items/i1/done": return self.send({"item": dict(ITEM, status="done")})
        if self.path == "/api/clock": return self.send({"messages": []})
        if self.path == "/api/undo": return self.send({"ok": True})
        self.send_error(404)
    def log_message(self, *a): pass
p = argparse.ArgumentParser(); p.add_argument("--port", type=int); p.add_argument("--data")
a = p.parse_args()
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
'''


def good(pidfile: str = "pass", chat: str = "pass") -> str:
    code = f"open({pidfile!r}, 'w').write(str(__import__('os').getpid()))" if pidfile != "pass" else "pass"
    return GOOD_APP.replace("PIDFILE", code).replace("CHAT", chat)


# the gen00 r1 failure of evo_iris2: class body `store = store` -> NameError at startup
CRASH_APP = good().replace("ThreadingHTTPServer((", "class X:\n    store = store\nThreadingHTTPServer((")
WRONG_SHAPE_APP = good().replace('"created": ["i1"]', '"made": ["i1"]')


def test_smoke_passes_good_app_and_kills_it(tmp_path):
    pidf = tmp_path / "pid"
    ok, rep = app_smoke({"app_py": good(str(pidf)), "index_html": "<html></html>"})
    assert ok and rep.startswith("SMOKE: PASS"), rep
    assert "POST /api/command" in rep and "shape ok" in rep
    pid = int(pidf.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_smoke_reports_startup_traceback():
    ok, rep = app_smoke({"app_py": CRASH_APP})
    assert not ok and rep.startswith("SMOKE: FAIL (app did not start)") and "NameError" in rep


def test_smoke_reports_compile_error_and_missing_app():
    ok, rep = app_smoke({"app_py": good(), "agents_py": "def f(:\n  pass\n"})
    assert not ok and "compile errors" in rep and "agents.py" in rep
    ok, rep = app_smoke({"index_html": "<html></html>"})
    assert not ok and "no app.py" in rep


def test_smoke_reports_wrong_shape():
    ok, rep = app_smoke({"app_py": WRONG_SHAPE_APP})
    assert not ok and "missing key 'created'" in rep


def test_shape_problems():
    assert shape_problems("focus", {"item": None, "parent": None, "why": None, "action": "x"}) == []
    assert shape_problems("state", []) and shape_problems("state", {"now": "", "items": [{"id": 1}], "messages": []})


def _genome(tmp: Path, roles, workflow) -> Path:
    d = tmp / "g"
    d.mkdir(exist_ok=True)
    (d / "genome.yaml").write_text(yaml.safe_dump({"schema": 2, "id": "t", "roles": roles, "workflow": workflow}))
    return d


def test_check_role_drives_fixer_loop(tmp_path):
    calls = []
    register_fake("writer", lambda m: calls.append("w") or f'<artifact name="app_py">{CRASH_APP}</artifact>')

    def fixer(msgs):
        calls.append("f")
        assert "NameError" in msgs[-1]["content"]  # the fixer sees the smoke report
        return f'<artifact name="app_py">{good()}</artifact>'
    register_fake("fixer", fixer)
    roles = [{"name": "writer", "kind": "llm", "model": "fake:writer", "prompt": "write"},
             {"name": "smoke", "kind": "check", "check": "app_smoke"},
             {"name": "fixer", "kind": "llm", "model": "fake:fixer", "prompt": "fix {blackboard.smoke}",
              "reads": ["artifacts"]}]
    loop = {"loop": {"body": ["fixer", "smoke"], "max_rounds": 3, "stop_when": "SMOKE: PASS", "check_first": True}}
    g = load_genome(_genome(tmp_path, roles, ["writer", "smoke", loop]))
    res = run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:writer")
    assert calls == ["w", "f"] and res["blackboard"]["smoke"].startswith("SMOKE: PASS")
    checks = [t for t in res["trace"] if t["kind"] == "check"]
    assert [c["ok"] for c in checks] == [False, True] and all(c["tokens"] == 0 for c in checks)

    # check_first: the smoke before the loop already passed -> the fixer never runs
    calls.clear()
    register_fake("writer", lambda m: calls.append("w") or f'<artifact name="app_py">{good()}</artifact>')
    res = run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:writer")
    assert calls == ["w"]


def test_unknown_check_rejected_and_build_accepts_check_role(tmp_path):
    with pytest.raises(AssertionError, match="unknown check"):
        load_genome(_genome(tmp_path, [{"name": "s", "kind": "check", "check": "harness"}], ["s"]))
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "contract.md").write_text("# c\n")
    (spec / "harness.py").write_text("def run_split(*a, **k): return []\n")
    (spec / "scenarios.py").write_text("SPLITS = {'fit': [], 'holdout': []}\n")
    task = make_task(f"build:{spec}", tmp_path / "run")
    d = _genome(tmp_path, [{"name": "dev", "kind": "llm", "prompt": "write app_py"},
                           {"name": "smoke", "kind": "check", "check": "app_smoke"}], ["dev", "smoke"])
    g = task.load_child(d)
    assert [r["kind"] for r in g["roles"]] == ["llm", "check"]
    assert "app_smoke" in task.describe() and "app_smoke" in task.moves() and "check_first" in task.schema_doc(False)


def test_loop_stop_on_first_body_role_and_literal_braces(tmp_path):
    calls = []
    register_fake("chk", lambda m: calls.append("c") or "ALL_CLEAR")

    def fix(msgs):
        calls.append("f")
        return "fixed"
    register_fake("fix", fix)

    def lit(msgs):
        assert 'answer {"ok": true} and {unknown_word}' in msgs[-1]["content"]  # literal braces survive
        return "x"
    register_fake("lit", lit)
    roles = [{"name": "lit", "kind": "llm", "model": "fake:lit", "prompt": 'answer {"ok": true} and {unknown_word}'},
             {"name": "checker", "kind": "llm", "model": "fake:chk", "prompt": "check"},
             {"name": "fixer", "kind": "llm", "model": "fake:fix", "prompt": "fix"}]
    loop = {"loop": {"body": ["checker", "fixer"], "max_rounds": 3, "stop_when": "ALL_CLEAR", "stop_on": "checker"}}
    g = load_genome(_genome(tmp_path, roles, ["lit", loop]))
    run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:lit")
    assert calls == ["c"]  # fixer skipped
    # default stop_on (last body role) keeps the old semantics: full rounds until the fixer says ALL_CLEAR
    calls.clear()
    g2 = load_genome(_genome(tmp_path, roles, [{"loop": {"body": ["checker", "fixer"], "max_rounds": 2, "stop_when": "ALL_CLEAR"}}]))
    run_team(g2, {"id": "e", "input": {"text": "x"}}, "fake:lit")
    assert calls == ["c", "f", "c", "f"]
    with pytest.raises(AssertionError, match="stop_on"):
        load_genome(_genome(tmp_path, roles, [{"loop": {"body": ["checker"], "stop_when": "X", "stop_on": "fixer"}}]))


def test_rendered_artifacts_round_trip_and_legacy_emissions(tmp_path):
    from sea.executor import parse_artifacts
    big = "import os\n" + "x = 1\n" * 50

    def writer(msgs):
        return (f'<artifact name="app_py">{big}</artifact>\n<artifact name="index_html"><html>v1</html></artifact>\n'
                '<artifact name="agents_py">def a(): pass</artifact>')
    register_fake("w2", writer)

    def echo(msgs):  # copies the rendered input format verbatim, changing only app_py
        u = msgs[-1]["content"]
        assert '<artifact name="app_py">' in u and "### artifact:" not in u
        start = u.index('<artifact name="app_py">')
        end = u.index("</artifact>", start) + len("</artifact>")
        return "fixed:\n" + u[start:end].replace("x = 1", "x = 2")
    register_fake("echo", echo)
    roles = [{"name": "w", "kind": "llm", "model": "fake:w2", "prompt": "write"},
             {"name": "fixer", "kind": "llm", "model": "fake:echo", "prompt": "fix {artifacts}"}]
    g = load_genome(_genome(tmp_path, roles, ["w", "fixer"]))
    res = run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:w2")
    assert "x = 2" in res["artifacts"]["app_py"] and "x = 1" not in res["artifacts"]["app_py"]
    assert res["artifacts"]["index_html"] == "<html>v1</html>" and res["artifacts"]["agents_py"] == "def a(): pass"

    # legacy "### artifact: name" + fence (ablation_p1_smoke r1) and without fence (r0)
    fenced = f"### artifact: app_py\n```python\n{big}```\n\n### artifact: index_html\n```html\n<html>v2</html>\n```\n"
    assert parse_artifacts(fenced) == {"app_py": big.strip(), "index_html": "<html>v2</html>"}
    assert parse_artifacts(f"### artifact: app_py\n{big}") == {"app_py": big.strip()}
    # a reviewer quoting a short snippet under the header does not overwrite the file
    review = "### artifact: app_py\n```python\nx = 3\n```\nline 2 is wrong"
    assert parse_artifacts(review, {"app_py": big}) == {}
    # tags win over a legacy section for the same name
    both = '<artifact name="a_py">tag</artifact>\n### artifact: a_py\n```\nlegacy\n```'
    assert parse_artifacts(both) == {"a_py": "tag"}


def test_smoke_deep_calls_opt_in():
    ok, rep = app_smoke({"app_py": good()}, calls="deep")
    assert ok, rep
    for path in ("/api/chat", "/api/items/i1/done", "/api/clock", "/api/undo"):
        assert f"POST {path}" in rep
    ok, rep = app_smoke({"app_py": good()})  # default: basic, none of the deep paths
    assert ok and "/api/chat" not in rep
    # chat crashes: basic passes (never calls it), deep reports the 500
    crash = good(chat="raise ValueError('a, b = (1,)')")
    assert app_smoke({"app_py": crash})[0]
    ok, rep = app_smoke({"app_py": crash}, calls="deep")
    assert not ok and "POST /api/chat" in rep and ("HTTP 500" in rep or "no answer" in rep or "Traceback" in rep)
    # a slow deep call is a note, not a failure
    slow = good(chat="__import__('time').sleep(20)")
    ok, rep = app_smoke({"app_py": slow}, budget=8, calls="deep")
    assert ok, rep


def test_check_options_validated_and_passed(tmp_path):
    roles = [{"name": "w", "kind": "llm", "model": "fake:w3", "prompt": "write"},
             {"name": "smoke", "kind": "check", "check": "app_smoke", "calls": "deep"}]
    register_fake("w3", lambda m: f'<artifact name="app_py">{good()}</artifact>')
    g = load_genome(_genome(tmp_path, roles, ["w", "smoke"]))
    res = run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:w3")
    assert res["blackboard"]["smoke"].startswith("SMOKE: PASS") and "/api/undo" in res["blackboard"]["smoke"]
    for bad in ({"calls": "all"}, {"depth": 3}):
        with pytest.raises(AssertionError, match="check option"):
            load_genome(_genome(tmp_path, [{"name": "s", "kind": "check", "check": "app_smoke", **bad}], ["s"]))


def test_smoke_selftest_option():
    st = good().replace('a = p.parse_args()', 'p.add_argument("--selftest", action="store_true")\na = p.parse_args()\n'
                        'if a.selftest:\n    print("SELFTEST lunch parsed as window"); raise SystemExit(SELFCODE)')
    ok, rep = app_smoke({"app_py": st.replace("SELFCODE", "1")}, selftest=True)
    assert not ok and "selftest: exit 1" in rep and "lunch parsed as window" in rep
    assert "POST /api/command" in rep  # the normal start-up path still ran
    ok, rep = app_smoke({"app_py": st.replace("SELFCODE", "0")}, selftest=True)
    assert ok and "selftest: exit 0" in rep
    hang = st.replace('raise SystemExit(SELFCODE)', '__import__("time").sleep(60)')
    ok, rep = app_smoke({"app_py": hang}, selftest=True, budget=15)  # a self-test that never exits
    assert not ok and "did not finish" in rep


def test_loop_stop_all_needs_every_condition(tmp_path):
    seq = {"aud": iter(["ALL_CLEAR", "ALL_CLEAR", "ALL_CLEAR"]), "smk": iter(["SMOKE: FAIL", "SMOKE: PASS", "SMOKE: PASS"])}
    calls = []
    register_fake("aud", lambda m: calls.append("a") or next(seq["aud"]))
    register_fake("smk", lambda m: calls.append("s") or next(seq["smk"]))
    roles = [{"name": "auditor", "kind": "llm", "model": "fake:aud", "prompt": "a"},
             {"name": "smoke", "kind": "llm", "model": "fake:smk", "prompt": "s"}]
    loop = {"loop": {"body": ["auditor", "smoke"], "max_rounds": 3,
                     "stop_all": [{"on": "smoke", "when": "SMOKE: PASS"}, {"on": "auditor", "when": "ALL_CLEAR"}]}}
    run_team(load_genome(_genome(tmp_path, roles, [loop])), {"id": "e", "input": {"text": "x"}}, "fake:aud")
    assert calls == ["a", "s", "a", "s"]  # round 1: auditor clear but smoke failing -> keep going
    with pytest.raises(AssertionError, match="stop_all"):
        load_genome(_genome(tmp_path, roles, [{"loop": {"body": ["auditor"], "stop_all": [{"on": "nobody", "when": "x"}]}}]))


def test_smoke_names_typographic_quotes():
    bad = good().replace('ThreadingHTTPServer((', 'x = f“I’ve moved {1}”\nThreadingHTTPServer((')
    ok, rep = app_smoke({"app_py": bad})
    assert not ok and "compile errors" in rep and "typographic quotes" in rep and "line " in rep


def test_identical_repeat_call_is_sampled_hotter(tmp_path, monkeypatch):
    import sea.executor as ex
    temps = []

    def chat(model, msgs, max_tokens=2048, temperature=0.0):
        temps.append((msgs[-1]["content"][:5], temperature))
        return ("SMOKE: FAIL" if "check" in msgs[-1]["content"] else "same fix"), {"prompt": 1, "completion": 1, "calls": 1}
    monkeypatch.setattr(ex, "chat", chat)
    roles = [{"name": "chk", "kind": "llm", "model": "fake:x", "prompt": "check", "reads": ["input"]},
             {"name": "fixer", "kind": "llm", "model": "fake:x", "prompt": "fix {blackboard.chk}", "reads": ["artifacts"]}]
    g = load_genome(_genome(tmp_path, roles, [{"loop": {"body": ["chk", "fixer"], "max_rounds": 3, "stop_when": "PASS", "stop_on": "chk"}}]))
    res = ex.run_team(g, {"id": "e", "input": {"text": "x"}}, "fake:x")
    fix = [t for c, t in temps if c == "fix S"]
    assert fix == [0.0, 0.7, 0.7]  # first call as configured, identical repeats sampled hotter
    assert sum("repeat_temperature" in t for t in res["trace"]) == 4  # chk and fixer, rounds 2 and 3


def test_failed_model_call_fails_the_role_not_the_run(tmp_path, monkeypatch):
    import subprocess
    import sea.executor as ex

    def chat(model, msgs, max_tokens=2048, temperature=0.0):
        if model == "fake:slow":
            raise subprocess.TimeoutExpired(["claude"], 900)
        return '<artifact name="index_html"><html></html></artifact>', {"prompt": 1, "completion": 1, "calls": 1}
    monkeypatch.setattr(ex, "chat", chat)
    roles = [{"name": "ui", "kind": "llm", "model": "fake:ok", "prompt": "ui"},
             {"name": "app", "kind": "llm", "model": "fake:slow", "prompt": "app"},
             {"name": "after", "kind": "llm", "model": "fake:ok", "prompt": "sees {blackboard.app}"}]
    res = ex.run_team(load_genome(_genome(tmp_path, roles, [{"parallel": ["ui", "app"]}, "after"])), {"id": "e", "input": {"text": "x"}}, "fake:ok")
    assert res["artifacts"]["index_html"] == "<html></html>"  # ui's file survives
    assert "failed" in res["blackboard"]["app"] and [t["role"] for t in res["trace"] if t.get("error")] == ["app"]
    assert [t["role"] for t in res["trace"]][-1] == "after"  # the run went on


V2_ROUTES = r'''
LOGE = {"id": "l1", "at": "2026-10-19T09:00", "kind": "create", "item_ids": ["i1"], "payload": {}, "source": "dump"}
PLAN = {"id": "p1", "origin": "import", "status": "preview", "ops": [], "affected": []}
BEL = {"key": "evening", "level": "habit", "statement": "Finishes after 8 PM (8 of 9)", "value": 20, "confidence": 0.8,
       "evidence": ["l1"], "updated_at": "2026-10-19T09:00"}
VIEW = {"type": "week", "params": {}, "hidden_counts": {}}
PROMPT = {"agent": "designer", "version": 1, "core_hash": "x", "learned": "", "diff": "", "reason": "", "evidence": [],
          "replay": {"passed": True, "detail": ""}, "active": True}
V2GET = {"/api/log": {"entries": [LOGE]}, "/api/beliefs": {"beliefs": [BEL]}, "/api/proposal": {"proposal": None},
         "/api/prompts": {"prompts": [PROMPT]}, "/api/activity": {"events": []}}
V2POST = {"/api/import": {"plan": PLAN}, "/api/plan/p1/apply": {"plan": dict(PLAN, status="applied")},
          "/api/plan/p1/undo": {"plan": dict(PLAN, status="undone")}, "/api/learn": {"beliefs": [BEL], "proposal": None},
          "/api/view": {"view": VIEW}}
'''


def v2_app(broken: str = "") -> str:
    app = good().replace("class H(BaseHTTPRequestHandler):", V2_ROUTES + "\nclass H(BaseHTTPRequestHandler):")
    app = app.replace('        if self.path == "/health":', '        if self.path in V2GET: return self.send(V2GET[self.path])\n        if self.path == "/health":')
    app = app.replace('        if self.path == "/api/reset":', '        if self.path in V2POST: BROKEN; return self.send(V2POST[self.path])\n        if self.path == "/api/reset":')
    app = app.replace("    def log_message", '    def do_DELETE(self): return self.send({"ok": True})\n    def log_message')
    return app.replace("BROKEN", broken or "pass")


def test_smoke_v2_calls():
    ok, rep = app_smoke({"app_py": v2_app()}, calls="v2")
    assert ok, rep
    for p in ("/api/log", "/api/import", "/api/plan/p1/apply", "/api/plan/p1/undo", "/api/learn", "/api/beliefs",
              "DELETE /api/beliefs/evening", "/api/proposal", "/api/view", "/api/prompts", "/api/activity", "/api/chat"):
        assert p in rep, p
    ok, rep = app_smoke({"app_py": v2_app("V2POST['/api/learn'] = {'beliefs': []}")}, calls="v2")
    assert not ok and "missing key 'proposal'" in rep
    assert "/api/log" not in app_smoke({"app_py": v2_app()}, calls="deep")[1]
