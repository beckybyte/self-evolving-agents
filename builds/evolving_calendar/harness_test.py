"""Tests for the frozen harness: a stub app that only answers /health and empty state, and a missing app."""
import importlib.util
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("evolving_calendar_harness", HERE / "harness.py")
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
