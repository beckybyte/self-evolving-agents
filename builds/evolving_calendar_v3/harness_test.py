"""Tests for the frozen v3 harness: a stub app that only answers /health and empty state, and a missing app."""
import importlib.util
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("evolving_calendar_v3_harness", HERE / "harness.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

STUB = textwrap.dedent('''
    import argparse, json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/api/state":
                return self._send(200, {"now": "2026-10-19T09:00", "items": [], "messages": []})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int)
    ap.add_argument("--data")
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
''')


def _stub(tmp_path):
    (tmp_path / "app.py").write_text(STUB)
    return tmp_path


def test_splits_shape():
    for split, scs in harness.SCENARIOS.SPLITS.items():
        assert scs, split
        ids = [s["id"] for s in scs]
        assert len(ids) == len(set(ids))
        for sc in scs:
            assert {"id", "persona", "description", "steps"} <= set(sc)
            labels = [st.get("label") for st in sc["steps"]]
            assert len(labels) == len(set(labels)), sc["id"]
            assert all(st["op"] in harness.OPS for st in sc["steps"])


def test_text_rules():
    assert harness.is_verb_first("Open a blank doc")
    assert harness.is_verb_first("Shortlist three projects")
    assert not harness.is_verb_first("The portfolio site")
    assert not harness.is_verb_first("3 quick sketches")
    assert not harness.is_verb_first("Writing the intro")
    assert harness.guilty("This is overdue") == "overdue"
    assert harness.guilty("You should have done it") is not None
    assert harness.guilty("Let's do it later tonight") is None


def test_stub_scores_near_zero_without_crash(tmp_path):
    rows = harness.run_split(_stub(tmp_path), "fit", timeout=10, workers=4)
    assert len(rows) == len(harness.SCENARIOS.SPLITS["fit"])
    for r in rows:
        assert r["reason"] not in ("no_start", "crash"), r
        assert r["score"] < 0.2, r
        assert not r["hard"]
        assert r["checks"] and all({"name", "ok", "detail"} <= set(c) for c in r["checks"])


def test_missing_app_is_no_start(tmp_path):
    rows = harness.run_split(tmp_path, "holdout", timeout=5, workers=2)
    assert rows and all(r["reason"] == "no_start" and r["score"] == 0 for r in rows)


def test_concurrent_run_splits_never_no_start(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    app = _stub(tmp_path)
    with ThreadPoolExecutor(3) as ex:
        runs = list(ex.map(lambda _: harness.run_split(app, "fit", timeout=10, workers=4), range(3)))
    assert all(r["reason"] != "no_start" for rows in runs for r in rows)


def test_port_in_use_is_retried(tmp_path):
    # first start dies like a lost port race, the second one comes up
    flaky = ("import os, sys\n"
             "if not os.path.exists('raced'):\n"
             "    open('raced', 'w').close()\n"
             "    sys.exit('OSError: [Errno 98] Address already in use')\n") + STUB
    (tmp_path / "app.py").write_text(flaky)
    r = harness.run_scenario(tmp_path, harness.SCENARIOS.SPLITS["fit"][0], timeout=5)
    assert r["reason"] != "no_start", r["detail"]


def test_broken_app_no_start_shows_stderr(tmp_path):
    (tmp_path / "app.py").write_text("def broken(:\n")
    t0 = __import__("time").time()
    r = harness.run_scenario(tmp_path, harness.SCENARIOS.SPLITS["fit"][0], timeout=5)
    assert r["reason"] == "no_start" and "SyntaxError" in r["detail"]
    assert __import__("time").time() - t0 < 10  # no retries for a broken app


def test_same_check_count_for_stub_and_missing(tmp_path):
    sc = harness.SCENARIOS.SPLITS["fit"][0]
    a = harness.run_scenario(_stub(tmp_path), sc, timeout=5)
    b = harness.run_scenario(tmp_path / "nope", sc, timeout=5)
    assert len(a["checks"]) == len(b["checks"])


def test_mock_env_has_no_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSOE_API", "secret")
    probe = ("import os, json\n"
             "open('env_seen.json', 'a').write(json.dumps([os.environ.get('EVOLVE_MOCK'), "
             "'CRUSOE_API' in os.environ]) + '\\n')\n") + STUB
    (tmp_path / "app.py").write_text(probe)
    mock = {"id": "m", "persona": "x", "description": "", "mock": True, "steps": [{"op": "ui", "label": "ui"}]}
    live = dict(mock, id="l", mock=False)
    harness.run_scenario(tmp_path, mock, timeout=5)
    harness.run_scenario(tmp_path, live, timeout=5)
    seen = [l for l in (tmp_path / "env_seen.json").read_text().splitlines() if l]
    assert seen == ['["1", false]', '[null, true]']


def test_v2_ops_all_covered():
    used = {st["op"] for scs in harness.SCENARIOS.SPLITS.values() for sc in scs for st in sc["steps"]}
    assert {"move", "cancel", "query", "bulk", "ics", "text_import", "learn", "counter", "delete_belief",
            "proposal", "view", "meta", "activity", "log"} <= used
    assert any(sc.get("mock") for sc in harness.SCENARIOS.SPLITS["fit"])


GOOD_PAGE = """<!doctype html><html><head><style>
%s
#chat-log{overflow:auto} #chat-input{width:100%%} #focus{padding:8px} #timeline{list-style:none}
#proposal{display:none}
</style></head><body>
<div id="chat-log"></div><input id="chat-input"><div id="focus"><button>Start 5 minutes</button></div>
<ul id="timeline"></ul><section id="learned"></section><section id="activity"></section>
<div id="proposal" style="display:none"></div><nav id="view-switcher"></nav>
<script>
const api = (p, b) => fetch('/api/' + p, b ? {method: 'POST', body: JSON.stringify(b)} : {}).then(r => r.json());
function render(s) { s.messages.forEach(m => { if (m.undo_id) { const b = document.createElement('button');
  b.textContent = 'Undo'; b.onclick = () => api('undo', {undo_id: m.undo_id}); } }); }
setInterval(() => { api('state').then(render); api('focus'); api('proposal'); }, 3000);
function send(t) { api('command', {text: t}); api('chat', {text: t}); api('clock', {now: ''}); }
</script></body></html>""" % "\n".join(".r%d{margin:%dpx}" % (i, i) for i in range(25))


def _sc(sid):
    return next(s for v in harness.SCENARIOS.SPLITS.values() for s in v if s["id"] == sid)


def _page_app(tmp_path, page):
    (tmp_path / "index.html").write_text(page)
    (tmp_path / "app.py").write_text(STUB.replace(
        'if self.path == "/health":',
        'if self.path == "/":\n'
        '            body = open("index.html", "rb").read()\n'
        '            self.send_response(200); self.send_header("Content-Type", "text/html")\n'
        '            self.send_header("Content-Length", str(len(body))); self.end_headers()\n'
        '            return self.wfile.write(body)\n'
        '        if self.path == "/health":'))
    return tmp_path


def test_ui_usability_good_page_passes(tmp_path):
    sc = _sc("fit_ui")
    r = harness.run_scenario(_page_app(tmp_path, GOOD_PAGE), sc, timeout=5)
    assert r["score"] == 1.0, r["detail"]


def test_ui_usability_plain_page_fails(tmp_path):
    plain = "<html><body>" + "".join(f'<div id="{i}"></div>' for i in harness.UI_IDS) + "</body></html>"
    r = harness.run_scenario(_page_app(tmp_path, plain), _sc("fit_ui"), timeout=5)
    assert r["score"] <= 0.25, r


class _FakeClient:
    def __init__(self, events):
        self.events = events

    def call(self, method, path, body=None, raw=False):
        return {"events": self.events}


def _rewrite_ok(events):
    ctx = harness.Ctx(_FakeClient(events))
    ctx.prefix = "activity"
    harness.op_activity(ctx, {})
    return ctx.results["activity:rewrite_has_diff"][0]


def test_rewrite_has_diff_forms():
    ev = lambda text, **kw: dict({"at": "2026-10-19T09:00", "agent": "learner", "kind": "prompt_rewrite",
                                  "text": text}, **kw)
    assert _rewrite_ok([ev("Designer prompt v2: + Prefer classic, familiar views.")])
    assert _rewrite_ok([ev("Designer prompt v2: - Try invented layouts")])
    assert _rewrite_ok([ev("--- v1\n+++ v2\n+ Prefer classic views")])
    assert _rewrite_ok([ev("Designer prompt v2 written", diff="+ Prefer classic views")])
    assert not _rewrite_ok([ev("Designer prompt v2 written")])
    assert not _rewrite_ok([ev("Rewrote the designer prompt for a well-known re-run")])


CATALOG = {"primitives": [
    {"type": "MonthGrid", "props": {"month": "date"}, "children": False},
    {"type": "WeekColumns", "props": {"start": "date", "days": "int"}, "children": False},
    {"type": "DayTimeline", "props": {"date": "date", "from_hour": "int", "to_hour": "int"}, "children": False},
    {"type": "AgendaList", "props": {"from": "date", "to": "date"}, "children": False},
    {"type": "Lane", "props": {"label": "str", "filter": "str"}, "children": True},
    {"type": "Bucket", "props": {"label": "str", "filter": "str"}, "children": False}]}


def test_validate_spec():
    cat = {p["type"]: p for p in CATALOG["primitives"]}
    good = {"root": "r", "elements": {
        "r": {"type": "Lane", "props": {"label": "Evening", "filter": {"hour_from": 18}}, "children": ["d", "b"]},
        "d": {"type": "DayTimeline", "props": {"date": "2026-10-19", "from_hour": 18, "to_hour": 23}, "children": []},
        "b": {"type": "Bucket", "props": {"label": "Other", "filter": {}}, "children": []}}}
    assert harness.validate_spec(good, cat) == []
    bad = harness.SCENARIOS._bad_specs("SpiralClock", "ghost",
                                       {"type": "AgendaList", "props": {"from": "soon", "to": "2027-01-01"}, "children": []},
                                       {"type": "MonthGrid", "props": {"month": "2031-05"}, "children": []})
    assert harness.validate_spec(bad["reject_unknown_type"]["spec"], cat)
    assert harness.validate_spec(bad["reject_missing_child"]["spec"], cat)
    assert harness.validate_spec(bad["reject_bad_date"]["spec"], cat)
    assert harness.validate_spec(bad["reject_hidden_item"]["spec"], cat) == []  # statically fine, hides items
    assert harness.validate_spec({"root": "r", "elements": {"r": {"type": "Bucket", "props": {}, "children": ["r"]}}}, cat)


def test_v3_ops_present():
    used = {st["op"] for v in harness.SCENARIOS.SPLITS.values() for sc in v for st in sc["steps"]}
    assert {"critic", "design", "view_motion", "knobs_dismiss", "knobs_accept", "appetite"} <= used


def test_dom_text_by_id():
    dom = ('<html><body><div id="focus"><h3>Next</h3><p>Open a blank doc &amp; list</p><input value="x"><br>'
           '<button>Start 5 minutes</button></div><section id="learned"><ul><li>Finishes tasks after 8 PM (8 of 9)'
           '</ul></section><div id="activity"></div><script>var t="Book club";</script><p>Book club</p></body></html>')
    body, by_id = harness.dom_text(dom)
    assert "open a blank doc & list" in by_id["focus"] and "start 5 minutes" in by_id["focus"]
    assert "after 8 pm (8 of 9)" in by_id["learned"]
    assert by_id["activity"] == ""
    assert "book club" in body and 'var t' not in body


def test_ui_render_without_chrome_is_harness_error(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "CHROME", str(tmp_path / "no-chrome"))
    r = harness.run_scenario(_stub(tmp_path), _sc("fit_ui_render"), timeout=5)
    render = [c for c in r["checks"] if c["name"].startswith("render:")]
    assert len(render) == 5 and not any(c["ok"] for c in render)
    with __import__("pytest").raises(harness.AppError, match="harness_error"):
        harness.render_dom("http://127.0.0.1:9/")


def test_ui_render_calibration():
    """Reference renders its state; the v2 champion (empty panels in a real browser) does not."""
    import os
    import pytest
    ref = HERE / "reference"
    bad = HERE.parents[1] / "runs" / "demo" / "app_v2b3"
    if not (os.access(harness.CHROME, os.X_OK) and (ref / "app.py").is_file() and (bad / "app.py").is_file()):
        pytest.skip("needs Chrome, the v3 reference and runs/demo/app_v2b3")
    assert harness.run_scenario(ref, _sc("fit_ui_render"), timeout=60)["score"] == 1.0
    r = harness.run_scenario(bad, _sc("fit_ui_render"), timeout=60)
    assert not any(c["ok"] for c in r["checks"] if c["name"].startswith("render:")), r["detail"]
