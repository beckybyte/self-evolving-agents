"""Framework-provided check roles (`kind: check`): frozen code in sea, not in the genome, that lets a team run its
own product and read what happened. Build mode bans tool roles so the evolver cannot write the product; a check
role is the sanctioned way for the team to "run your own code".

A check takes the team's current artifacts and returns a plain-text report whose first line is
`<NAME>: PASS` or `<NAME>: FAIL (...)`, so a loop can stop on it (stop_when: "SMOKE: PASS").

app_smoke never reads the build's harness, scenarios or any evaluation data: it compiles the files, starts the app,
and makes a few generic contract calls with neutral inputs, checking only status codes and JSON shapes.
"""
from __future__ import annotations

import json
import os
import py_compile
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SMOKE_BUDGET = 38.0  # seconds for the whole check, process start to last call
HEALTH_WAIT = 15.0
TAIL = 1500

ITEM_KEYS = ("id", "kind", "title", "start", "end", "window", "status", "why", "parent_id", "estimate_min",
             "postpone_count", "source")
MESSAGE_KEYS = ("id", "role", "kind", "text", "item_id", "at", "undo_id")
# (method, path, body, shape check) -- generic contract calls with neutral inputs only
SMOKE_CALLS = [
    ("POST", "/api/reset", {"now": "2026-10-19T09:00", "items": []}, "state"),
    ("GET", "/api/state", None, "state"),
    ("POST", "/api/command", {"text": "call mom", "mode": "dump"}, "command"),
    ("GET", "/api/focus", None, "focus"),
]


def _missing(obj, keys) -> list[str]:
    return [k for k in keys if k not in obj] if isinstance(obj, dict) else ["(not an object)"]


def shape_problems(kind: str, data) -> list[str]:
    """Contract JSON-shape problems of one response (empty list = shape ok)."""
    if not isinstance(data, dict):
        return [f"expected a JSON object, got {type(data).__name__}"]
    out = []
    if kind == "state":
        out += [f"missing key {k!r}" for k in ("now", "items", "messages") if k not in data]
        for i, it in enumerate(data.get("items") or []):
            m = _missing(it, ITEM_KEYS)
            if m:
                out.append(f"items[{i}] missing {m}")
                break
    elif kind == "command":
        out += [f"missing key {k!r}" for k in ("messages", "created") if k not in data]
        if "created" in data and not isinstance(data["created"], list):
            out.append("'created' must be a list of item ids")
    elif kind == "messages":
        out += [] if isinstance(data.get("messages"), list) else ["missing list 'messages'"]
    elif kind == "done":
        m = _missing(data.get("item"), ITEM_KEYS)
        if m:
            out.append(f"item missing {m}")
    elif kind == "undo":
        out += [] if "ok" in data else ["missing key 'ok'"]
    elif kind == "focus":
        out += [f"missing key {k!r}" for k in ("item", "parent", "why", "action") if k not in data]
        if data.get("item") is not None:
            m = _missing(data["item"], ITEM_KEYS)
            if m:
                out.append(f"item missing {m}")
    for i, msg in enumerate((data.get("messages") or []) if kind in ("state", "command", "messages") else []):
        m = _missing(msg, MESSAGE_KEYS)
        if m:
            out.append(f"messages[{i}] missing {m}")
            break
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_tree(proc: subprocess.Popen):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def _call(base: str, method: str, path: str, body, timeout: float):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, 0.5)) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _app_env() -> dict:
    env = dict(os.environ)
    try:
        from .llm import crusoe_key
        env.setdefault("CRUSOE_API", crusoe_key())
    except Exception:
        pass
    env.setdefault("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
    env.setdefault("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    env.setdefault("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def deep_calls(do, seen: list, lines: list):
    """calls: deep -- after SMOKE_CALLS, chain the remaining contract endpoints with neutral inputs on an item the app
    created itself: chat "yes" -> done -> clock +2 h -> undo (if any message carried an undo_id). Crash and JSON-shape
    problems only; what the app answers is not judged."""
    item = None
    for obj in seen:
        if isinstance(obj, dict) and obj.get("created"):
            item = str(obj["created"][0])
            break
    if item is None:
        st = do("GET", "/api/state", None, "state")
        items = (st or {}).get("items") or []
        item = str(items[0]["id"]) if items and isinstance(items[0], dict) and "id" in items[0] else None
    if item is None:
        lines.append("deep: no item to use (the dump created none); chat and done not run")
    else:
        do("POST", "/api/chat", {"item_id": item, "text": "yes"}, "messages", strict=False)
        do("POST", f"/api/items/{item}/done", {}, "done", strict=False)
    do("POST", "/api/clock", {"now": "2026-10-19T11:00"}, "messages", strict=False)
    undo = next((m.get("undo_id") for o in seen if isinstance(o, dict)
                 for m in (o.get("messages") or []) if isinstance(m, dict) and m.get("undo_id")), None)
    if undo:
        do("POST", "/api/undo", {"undo_id": undo}, "undo", strict=False)
    else:
        lines.append("deep: no message carried an undo_id; /api/undo not run")


def app_smoke(artifacts: dict[str, str], budget: float = SMOKE_BUDGET, calls: str = "basic") -> tuple[bool, str]:
    """Compile the team's files, start app.py, make SMOKE_CALLS (+ deep_calls when calls == "deep").
    Returns (passed, report)."""
    from .tasks.build import materialise  # lazy: tasks.build imports the executor, which imports this module
    t0 = time.time()
    lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sea_smoke_") as tmp:
        app = Path(tmp) / "app"
        files, rejected = materialise(artifacts, app)
        lines.append(f"files: {files}" + (f"; rejected paths: {rejected}" if rejected else ""))
        if "app.py" not in files:
            return False, "SMOKE: FAIL (no app.py)\n" + "\n".join(lines) + "\nNo artifact named app_py was emitted."
        bad = []
        for f in files:
            if f.endswith(".py"):
                try:
                    py_compile.compile(str(app / f), cfile=str(Path(tmp) / "pyc"), doraise=True)
                except py_compile.PyCompileError as e:
                    bad.append(f"{f}: {str(e.msg).strip()[-600:]}")
        if bad:
            return False, "SMOKE: FAIL (compile errors)\n" + "\n".join(lines + ["compile errors:"] + bad)
        lines.append("compile: ok")
        port, data, log = _free_port(), Path(tmp) / "data", Path(tmp) / "stderr.txt"
        data.mkdir()
        with open(log, "w") as errf:
            proc = subprocess.Popen([sys.executable, "app.py", "--port", str(port), "--data", str(data)], cwd=app,
                                    stdout=errf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=_app_env(), start_new_session=True)
        base, problems = f"http://127.0.0.1:{port}", 0
        try:
            started = False
            while time.time() - t0 < min(HEALTH_WAIT + 1, budget):
                if proc.poll() is not None:
                    break
                try:
                    st, body = _call(base, "GET", "/health", None, 2)
                    if st == 200 and json.loads(body).get("ok") is True:
                        started = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            if not started:
                why = f"process exited with code {proc.returncode}" if proc.poll() is not None else \
                    f'no {{"ok": true}} from GET /health within {HEALTH_WAIT:.0f} s'
                return False, ("SMOKE: FAIL (app did not start)\n" + "\n".join(lines) + f"\nstart: {why}\n"
                               f"output tail:\n{log.read_text(errors='replace')[-TAIL:]}")
            lines.append(f"start: /health ok after {time.time() - t0:.1f} s")
            seen: list = []  # every JSON response, for ids the deep chain needs

            def do(method, path, body, kind, strict=True):
                """One call; returns the parsed object or None. strict=False (deep calls): a timeout or the budget
                running out is a note, not a problem (a slow LLM path is not a crash)."""
                nonlocal problems
                left = budget - (time.time() - t0)
                label = f"{method} {path} {json.dumps(body) if body else ''}".rstrip()
                if left < 1:
                    lines.append(f"{label}: not run (smoke budget {budget:.0f} s used up)")
                    problems += strict
                    return None
                c0 = time.time()
                try:
                    st, text = _call(base, method, path, body, min(left, 30))
                except Exception as e:
                    lines.append(f"{label}: no answer ({e!r:.200})")
                    problems += strict or proc.poll() is not None
                    if proc.poll() is not None:
                        lines.append("the app process died")
                    return None
                head = f"{label}: HTTP {st} in {time.time() - c0:.1f} s"
                if st != 200:
                    lines.append(f"{head}; body: {text[:400]}")
                    problems += 1
                    return None
                try:
                    obj = json.loads(text)
                except ValueError:
                    lines.append(f"{head}; not JSON: {text[:300]}")
                    problems += 1
                    return None
                shp = shape_problems(kind, obj)
                problems += bool(shp)
                lines.append(f"{head}; shape {'ok' if not shp else 'WRONG: ' + '; '.join(shp)}; body: {text[:300]}")
                seen.append(obj)
                return obj

            for method, path, body, kind in SMOKE_CALLS:
                do(method, path, body, kind)
                if proc.poll() is not None:
                    break
            if calls == "deep" and proc.poll() is None:
                deep_calls(do, seen, lines)
        finally:
            _kill_tree(proc)
        out = log.read_text(errors="replace")
        if "Traceback" in out:
            problems += 1
            lines.append(f"server output contains a traceback:\n{out[out.rfind('Traceback'):][-TAIL:]}")
    ok = problems == 0
    return ok, (f"SMOKE: PASS" if ok else f"SMOKE: FAIL ({problems} problems)") + "\n" + "\n".join(lines)


CHECKS = {"app_smoke": app_smoke}
CHECK_OPTIONS = {"app_smoke": {"calls": ("basic", "deep")}}  # option -> allowed values (first = default)


def run_check(name: str, artifacts: dict[str, str], **opts) -> tuple[bool, str]:
    try:
        return CHECKS[name](dict(artifacts), **opts)
    except Exception as e:  # a check never fails the team run
        return False, f"{name.upper()}: FAIL (check crashed: {e!r:.300})"
