"""Framework check roles: app_smoke on real tiny apps (starts processes), and the check kind in genome/executor."""
import json
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


def test_expects_reruns_a_role_that_emitted_no_file_and_check_report_in_trace(tmp_path):
    outs = iter(["sorry, here is a plan only", '<artifact name="v2_py">x = 1</artifact>'])
    calls = []
    register_fake("flaky", lambda m: calls.append(1) or next(outs))
    roles = [{"name": "v2", "kind": "llm", "model": "fake:flaky", "prompt": "write v2", "expects": ["v2_py"]},
             {"name": "smoke", "kind": "check", "check": "app_smoke"}]
    res = run_team(load_genome(_genome(tmp_path, roles, ["v2", "smoke"])), {"id": "e", "input": {"text": "x"}}, "fake:flaky")
    assert len(calls) == 2 and res["artifacts"]["v2_py"] == "x = 1"
    assert [t for t in res["trace"] if t["kind"] == "retry"][0]["missing"] == ["v2_py"]
    chk = [t for t in res["trace"] if t["kind"] == "check"][0]
    assert chk["report"].startswith("SMOKE: FAIL (no app.py)")
    with pytest.raises(AssertionError, match="expects"):
        load_genome(_genome(tmp_path, [{"name": "a", "kind": "llm", "prompt": "p", "expects": ["bad name"]}], ["a"]))


def test_artifacts_named_by_and_role_token_sums(tmp_path):
    from sea.tasks.build import role_token_sums
    assert role_token_sums([{"role": "fixer", "tokens": 5}, {"role": "smoke", "tokens": 0}, {"role": "fixer", "tokens": 7}]) == {"fixer": 12, "smoke": 0}
    seen = {}
    register_fake("w4", lambda m: '<artifact name="app_py">import v2</artifact><artifact name="v2_py">def f(:</artifact>'
                                  '<artifact name="index_html"><html></html></artifact>')
    register_fake("fx", lambda m: seen.setdefault("u", m[-1]["content"]) and "")
    roles = [{"name": "w", "kind": "llm", "model": "fake:w4", "prompt": "write"},
             {"name": "smoke", "kind": "check", "check": "app_smoke"},
             {"name": "fixer", "kind": "llm", "model": "fake:fx", "prompt": "fix", "reads": ["artifacts_named_by.smoke"]}]
    run_team(load_genome(_genome(tmp_path, roles, ["w", "smoke", "fixer"])), {"id": "e", "input": {"text": "x"}}, "fake:w4")
    u = seen["u"]
    assert '<artifact name="v2_py">' in u and '<artifact name="app_py">' not in u and "index_html" not in u


def test_stop_all_tested_after_every_role_and_spec_selector(tmp_path):
    seq = {"smk": iter(["SMOKE: FAIL", "SMOKE: PASS", "SMOKE: PASS"]), "rep": iter(["FIXED", "NO_CHANGE", "NO_CHANGE"])}
    calls = []
    register_fake("smk2", lambda m: calls.append("s") or next(seq["smk"]))
    register_fake("rep", lambda m: calls.append("r") or next(seq["rep"]))
    roles = [{"name": "smoke", "kind": "llm", "model": "fake:smk2", "prompt": "s"},
             {"name": "repair", "kind": "llm", "model": "fake:rep", "prompt": "r"}]
    loop = {"loop": {"body": ["smoke", "repair"], "max_rounds": 3,
                     "stop_all": [{"on": "smoke", "when": "SMOKE: PASS"}, {"on": "repair", "when": "NO_CHANGE"}]}}
    run_team(load_genome(_genome(tmp_path, roles, [loop])), {"id": "e", "input": {"text": "x"}}, "fake:smk2")
    assert calls == ["s", "r", "s", "r"]  # stops right after repair says NO_CHANGE with smoke passing: no extra smoke

    seen = {}
    register_fake("sp", lambda m: seen.setdefault(len(seen), m[-1]["content"]) and "ok")
    d = tmp_path / "gs"
    d.mkdir()
    (d / "genome.yaml").write_text(yaml.safe_dump({"schema": 2, "id": "t", "specs": {"v2": "V2 SPEC TEXT", "ui": "UI SPEC TEXT"},
        "roles": [{"name": "a", "kind": "llm", "model": "fake:sp", "prompt": "write", "reads": ["spec.v2"]},
                  {"name": "b", "kind": "llm", "model": "fake:sp", "prompt": "ui: {spec.ui}", "reads": []}]}))
    run_team(load_genome(d), {"id": "e", "input": {"text": "x"}}, "fake:sp")
    assert "V2 SPEC TEXT" in seen[0] and "UI SPEC" not in seen[0] and "UI SPEC TEXT" in seen[1] and "V2 SPEC" not in seen[1]
    (d / "genome.yaml").write_text(yaml.safe_dump({"schema": 2, "id": "t", "roles": [{"name": "a", "kind": "llm", "prompt": "p", "reads": ["spec.nope"]}]}))
    with pytest.raises(AssertionError, match="no spec"):
        load_genome(d)


def test_stop_marker_counts_only_at_line_start_and_not_when_truncated(tmp_path, monkeypatch):
    import sea.executor as ex
    outs = iter([("I must not write ALL_CLEAR while smoke fails. Defects: x", "stop"),
                 ("**ALL_CLEAR** nothing left", "length"),  # truncated: never counts
                 ("- ALL_CLEAR", "stop")])
    calls = []

    def chat(model, msgs, max_tokens=2048, temperature=0.0):
        if model == "fake:rev":
            t, f = next(outs)
            calls.append("rev")
            return t, {"prompt": 1, "completion": 1, "calls": 1, "finish": f}
        calls.append("fix")
        return "fixed", {"prompt": 1, "completion": 1, "calls": 1}
    monkeypatch.setattr(ex, "chat", chat)
    roles = [{"name": "rev", "kind": "llm", "model": "fake:rev", "prompt": "r"},
             {"name": "fix", "kind": "llm", "model": "fake:fix", "prompt": "f"}]
    loop = {"loop": {"body": ["rev", "fix"], "max_rounds": 4, "stop_on": "rev", "stop_when": "ALL_CLEAR"}}
    ex.run_team(load_genome(_genome(tmp_path, roles, [loop])), {"id": "e", "input": {"text": "x"}}, "fake:fix")
    assert calls == ["rev", "fix", "rev", "fix", "rev"]


def test_smoke_v3_calls():
    extra = ('\nV2GET["/api/catalog"] = {"primitives": [{"type": "Lane", "props": {"date": "date"}, "children": True}]}\n'
             'V2POST["/api/critic"] = {"ok": True, "problems": [], "repaired": None}\n'
             'V2POST["/api/design"] = {"drafts": [], "critique": [], "repairs": 0, "chosen": 0, "proposal": None}\n')
    app = v2_app().replace("class H(BaseHTTPRequestHandler):", extra + "class H(BaseHTTPRequestHandler):", 1)
    ok, rep = app_smoke({"app_py": app}, calls="v3")
    assert ok and "/api/catalog" in rep and "/api/critic" in rep and "/api/design" in rep, rep
    bad = app.replace('"repairs": 0, ', "")
    ok, rep = app_smoke({"app_py": bad}, calls="v3")
    assert not ok and "missing key 'repairs'" in rep


SLOW_HARNESS = '''
import json, time, types
from pathlib import Path
SCENARIOS = types.SimpleNamespace(SPLITS={"fit": [{"id": "F1"}, {"id": "F2"}, {"id": "SLOW"}], "holdout": [{"id": "H1"}]})
def run_scenario(app_dir, sc, timeout=60, mode=None):
    if sc["id"] == "SLOW":
        time.sleep(60)
    return {"id": sc["id"], "score": 1.0, "hard": True, "reason": "ok", "detail": "", "checks": [], "secs": 0.1}
def run_split(app_dir, split, timeout=60, workers=3, mode=None):
    return [run_scenario(app_dir, sc, timeout) for sc in SCENARIOS.SPLITS[split]]
'''


def _build_spec(tmp_path, harness):
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "contract.md").write_text("# c\n")
    (spec / "harness.py").write_text(harness)
    (spec / "scenarios.py").write_text("SPLITS = {'fit': [{'id': 'F1', 'description': 'a'}, {'id': 'F2', 'description': 'b'}, "
                                       "{'id': 'SLOW', 'description': 'c'}], 'holdout': [{'id': 'H1', 'description': 'd'}]}\n")
    return make_task(f"build:{spec}", tmp_path / "run")


def test_harness_wall_cap_scores_unfinished_scenarios_timeout(tmp_path):
    task = _build_spec(tmp_path, SLOW_HARNESS)
    task.harness_wall = 4
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text(good())
    t0 = time.time()
    rows = task.run_harness(app, "fit", {"files": ["app.py"]})
    assert time.time() - t0 < 30
    by = {r["id"]: r for r in rows}
    assert by["F1"]["score"] == 1.0 and by["F2"]["score"] == 1.0
    assert by["SLOW"]["score"] == 0.0 and by["SLOW"]["reason"] == "timeout"
    def cwd(d):
        try:
            return os.readlink(f"/proc/{d}/cwd")
        except OSError:
            return None
    assert not any(cwd(d) == str(app.resolve()) for d in os.listdir("/proc") if d.isdigit())  # nothing left running


def test_liveness_flags_a_hung_app_and_skips_the_harness(tmp_path):
    from sea.checks import liveness
    hung = good().replace('        if self.path == "/api/reset":', '        if self.path == "/api/reset": __import__("time").sleep(120)\n        if self.path == "/api/reset":')
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text(hung)
    t0 = time.time()
    state, detail = liveness(app, limit=3)
    assert state == "hung" and "/api/reset" in detail and time.time() - t0 < 25
    (app / "app.py").write_text(good())
    assert liveness(app, limit=3)[0] == "alive"
    (app / "app.py").write_text(CRASH_APP)
    assert liveness(app, limit=3)[0] == "no_start"
    task = _build_spec(tmp_path, SLOW_HARNESS)
    (app / "app.py").write_text(hung)
    import sea.tasks.build as b
    old = b.LIVENESS_LIMIT
    b.LIVENESS_LIMIT = 3
    try:
        rows = task.run_harness(app, "fit", {"files": ["app.py"]})
    finally:
        b.LIVENESS_LIMIT = old
    assert {r["reason"] for r in rows} == {"hung"} and all(r["score"] == 0 for r in rows)


def test_smoke_acceptance_steps_and_reviewer_output_in_trace(tmp_path):
    steps = json.dumps([{"method": "POST", "path": "/api/command", "body": {"text": "dentist thursday 3pm", "mode": "command"},
                         "expect": ['"created"']},
                        {"method": "GET", "path": "/api/state", "expect": ["call mom"]},
                        {"method": "GET", "path": "/api/nope", "status": 200}])
    ok, rep = app_smoke({"app_py": good(), "tests_json": steps}, acceptance="tests_json")
    assert not ok and "acceptance step 1 POST /api/command: ok" in rep and "acceptance step 2 GET /api/state: ok" in rep
    assert "acceptance step 3 GET /api/nope: FAIL (status 404" in rep
    ok, rep = app_smoke({"app_py": good()}, acceptance="tests_json")
    assert not ok and "no artifact named 'tests_json'" in rep
    register_fake("rv", lambda m: "ALL_CLEAR: the handlers look right")
    roles = [{"name": "rev", "kind": "llm", "model": "fake:rv", "prompt": "review"},
             {"name": "smoke", "kind": "check", "check": "app_smoke", "acceptance": "tests_json"}]
    res = run_team(load_genome(_genome(tmp_path, roles, ["rev"])), {"id": "e", "input": {"text": "x"}}, "fake:rv")
    assert res["trace"][0]["output_head"].startswith("ALL_CLEAR")
    with pytest.raises(AssertionError, match="check option"):
        load_genome(_genome(tmp_path, [{"name": "s", "kind": "check", "check": "app_smoke", "acceptance": "bad name"}], ["s"]))


def test_parallel_inside_loop_body_and_soul_opt_out(tmp_path):
    seen = []
    smk = iter(["SMOKE: FAIL", "SMOKE: PASS"])
    register_fake("fa", lambda m: seen.append(("fa", m[0]["content"] if m[0]["role"] == "system" else "")) or '<artifact name="a_py">a</artifact>')
    register_fake("fb", lambda m: seen.append(("fb", m[0]["content"] if m[0]["role"] == "system" else "")) or '<artifact name="b_py">b</artifact>')
    register_fake("sm", lambda m: seen.append(("sm", "")) or next(smk))
    d = tmp_path / "gp"
    d.mkdir()
    (d / "genome.yaml").write_text(yaml.safe_dump({"schema": 2, "id": "t", "soul": "BIG SHARED SOUL", "roles": [
        {"name": "fix_a", "kind": "llm", "model": "fake:fa", "prompt": "a"},
        {"name": "fix_b", "kind": "llm", "model": "fake:fb", "prompt": "b", "soul": False},
        {"name": "smoke", "kind": "llm", "model": "fake:sm", "prompt": "s"}],
        "workflow": [{"loop": {"body": [{"parallel": ["fix_a", "fix_b"]}, "smoke"], "max_rounds": 3, "stop_when": "SMOKE: PASS"}}]}))
    res = run_team(load_genome(d), {"id": "e", "input": {"text": "x"}}, "fake:fa")
    names = [n for n, _ in seen]
    assert names.count("sm") == 2 and names.count("fa") == 2 and names.count("fb") == 2
    assert all("BIG SHARED SOUL" in s for n, s in seen if n == "fa") and all("BIG SHARED SOUL" not in s for n, s in seen if n == "fb")
    assert res["artifacts"]["a_py"] == "a" and res["artifacts"]["b_py"] == "b"


def test_contract_section_selector(tmp_path):
    seen = []
    register_fake("cs", lambda m: seen.append(m[-1]["content"]) or "ok")
    contract = "# C\nintro\n## Data shapes\nItem: {id}\n## v2 additions\nmore\n### Extra endpoints\nGET /api/log\n## v3 additions\n### Extra endpoints\nGET /api/catalog\n"
    roles = [{"name": "a", "kind": "llm", "model": "fake:cs", "prompt": "shapes: {contract.data-shapes}", "reads": []},
             {"name": "b", "kind": "llm", "model": "fake:cs", "prompt": "x", "reads": ["contract.v3-additions--extra-endpoints"]}]
    g = load_genome(_genome(tmp_path, roles, ["a", "b"]), ("contract", "scenarios"))
    run_team(g, {"id": "e", "input": {"text": "x"}, "context": {"contract": contract, "scenarios": ""}}, "fake:cs")
    assert "Item: {id}" in seen[0] and "api/log" not in seen[0]
    assert "GET /api/catalog" in seen[1] and "GET /api/log" not in seen[1]


def test_stable_report_makes_unchanged_apps_identical():
    from sea.checks import run_check, stable_report
    a = run_check("app_smoke", {"app_py": CRASH_APP})[1]
    b = run_check("app_smoke", {"app_py": CRASH_APP})[1]
    assert a == b and "/tmp/sea_smoke_" not in a and "NameError" in a
    assert stable_report("POST /x: HTTP 200 in 0.3 s; slow GET /y: HTTP 200 in 12.6 s") == "POST /x: HTTP 200; slow GET /y: HTTP 200 in 13 s"


def test_acceptance_save_and_reuse_ids():
    steps = json.dumps([{"method": "POST", "path": "/api/command", "body": {"text": "call mom", "mode": "dump"},
                         "save": {"item": "created.0", "undo": "messages.0.undo_id"}},
                        {"method": "POST", "path": "/api/items/${item}/done", "body": {}, "expect": ['"status": "done"']},
                        {"method": "POST", "path": "/api/undo", "body": {"undo_id": "${undo}"}, "expect": ["ok"]}])
    ok, rep = app_smoke({"app_py": good(), "t_json": steps}, acceptance="t_json")
    assert ok, rep
    assert "acceptance step 2 POST /api/items/i1/done: ok" in rep and "acceptance step 3 POST /api/undo: ok" in rep


PAGE = ('<html><body><input id="chat-input"><div id="chat-log">Hi</div><div id="focus">Start 5 minutes: call mom</div>'
        '<ul id="timeline"></ul><script>document.getElementById("timeline").innerHTML = "<li>Call mom today</li>";</script>'
        '</body></html>')


def page_app(page: str) -> str:
    return good().replace('        if self.path == "/health":',
                          '        if self.path == "/":\n'
                          f'            b = {page!r}.encode(); self.send_response(200); self.send_header("Content-Type", "text/html")\n'
                          '            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return\n'
                          '        if self.path == "/health":')


@pytest.mark.skipif(not os.access("/usr/bin/google-chrome", os.X_OK), reason="no Chrome")
def test_smoke_render_shows_the_page():
    ok, rep = app_smoke({"app_py": page_app(PAGE)}, render=True)
    assert ok, rep
    assert "required ids all present" in rep and "Call mom today" in rep  # script-rendered text is visible
    ok, rep = app_smoke({"app_py": page_app('<html><body><div id="chat-log"></div></body></html>')}, render=True)
    assert not ok and "MISSING ['chat-input', 'focus', 'timeline']" in rep and "almost no text" in rep


def test_when_skips_a_role_and_parallel_up_to_eight(tmp_path):
    calls = []
    register_fake("sk", lambda m: calls.append(m[-1]["content"][:4]) or "SMOKE: FAIL\napp.py: NameError")
    roles = [{"name": "smoke", "kind": "llm", "model": "fake:sk", "prompt": "smok", "reads": []},
             {"name": "fix_v2", "kind": "llm", "model": "fake:sk", "prompt": "fxv2", "reads": [], "when": {"on": "smoke", "contains": "v2.py"}},
             {"name": "fix_app", "kind": "llm", "model": "fake:sk", "prompt": "fxap", "reads": [], "when": {"on": "smoke", "contains": "app.py"}}]
    res = run_team(load_genome(_genome(tmp_path, roles, ["smoke", "fix_v2", "fix_app"])), {"id": "e", "input": {"text": "x"}}, "fake:sk")
    assert calls == ["smok", "fxap"] and [t["kind"] for t in res["trace"]] == ["llm", "skip", "llm"]
    with pytest.raises(AssertionError, match="when"):
        load_genome(_genome(tmp_path, [{"name": "a", "kind": "llm", "prompt": "p", "when": {"on": "nobody", "contains": "x"}}], ["a"]))
    eight = [{"name": f"w{i}", "kind": "llm", "model": "fake:sk", "prompt": "p", "reads": []} for i in range(8)]
    load_genome(_genome(tmp_path, eight, [{"parallel": [f"w{i}" for i in range(8)]}]))


def test_images_attach_screenshots_for_crusoe_models_only(tmp_path, monkeypatch):
    import sea.executor as ex
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG fake")
    seen = []

    def chat(model, msgs, max_tokens=2048, temperature=0.0):
        seen.append((model, msgs[-1]["content"]))
        return (f"render: ok\nscreenshot: {shot}" if model == "fake:sh" else "ok"), {"prompt": 1, "completion": 1, "calls": 1}
    monkeypatch.setattr(ex, "chat", chat)
    roles = [{"name": "smoke", "kind": "llm", "model": "fake:sh", "prompt": "s", "reads": []},
             {"name": "look", "kind": "llm", "model": "crusoe:nvidia/NVIDIA-Nemotron-3.5-Super-VL#nothink", "prompt": "look", "reads": [], "images": ["smoke"]},
             {"name": "look2", "kind": "llm", "model": "claude:haiku", "prompt": "look", "reads": [], "images": ["smoke"]}]
    ex.run_team(load_genome(_genome(tmp_path, roles, ["smoke", "look", "look2"])), {"id": "e", "input": {"text": "x"}}, "fake:sh")
    vl = seen[1][1]
    assert isinstance(vl, list) and vl[0]["type"] == "text" and vl[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert isinstance(seen[2][1], str) and "cannot see images" in seen[2][1]
    # no screenshot taken -> the vision role is told so instead of describing an imagined page
    seen.clear()
    shot.unlink()
    ex.run_team(load_genome(_genome(tmp_path, roles, ["smoke", "look"])), {"id": "e", "input": {"text": "x"}}, "fake:sh")
    assert isinstance(seen[1][1], str) and "NO screenshot is attached" in seen[1][1]


@pytest.mark.skipif(not os.access("/usr/bin/google-chrome", os.X_OK), reason="no Chrome")
def test_smoke_render_screenshot():
    ok, rep = app_smoke({"app_py": page_app(PAGE)}, render="screenshot")
    path = next(l.split(": ", 1)[1] for l in rep.splitlines() if l.startswith("screenshot: "))
    assert ok and Path(path).read_bytes()[:4] == b"\x89PNG"
    assert app_smoke({"app_py": page_app(PAGE)}, render="screenshot")[1] == rep  # same page -> byte-identical report


def test_acceptance_expect_not_fails_the_smoke():
    steps = json.dumps([{"method": "GET", "path": "/api/state", "expect_not": ["call mom"]},
                        {"method": "GET", "path": "/api/state", "expect_not": ["breakdown"]}])
    ok, rep = app_smoke({"app_py": good(), "t_json": steps}, acceptance="t_json")
    assert not ok and rep.startswith("SMOKE: FAIL") and "must not contain ['call mom']" in rep
    assert "acceptance step 2 GET /api/state: ok" in rep


def test_unchanged_reemits_recorded(tmp_path):
    register_fake("w5", lambda m: '<artifact name="a_py">a = 1</artifact><artifact name="b_py">b = 1</artifact>')
    register_fake("f5", lambda m: '<artifact name="a_py">a = 1</artifact><artifact name="b_py">b = 2</artifact>')
    roles = [{"name": "w", "kind": "llm", "model": "fake:w5", "prompt": "w", "reads": []},
             {"name": "fix", "kind": "llm", "model": "fake:f5", "prompt": "f", "reads": []}]
    res = run_team(load_genome(_genome(tmp_path, roles, ["w", "fix"])), {"id": "e", "input": {"text": "x"}}, "fake:w5")
    assert "unchanged_reemits" not in res["trace"][0] and res["trace"][1]["unchanged_reemits"] == ["a_py"]
