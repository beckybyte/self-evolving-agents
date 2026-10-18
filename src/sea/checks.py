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
SELFTEST_WAIT = 12.0
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


SMART = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-", "\u00a0": " "}


def non_ascii_hint(path: Path, limit: int = 8) -> str:
    """Lines of a .py file that hold typographic quotes/dashes outside comments (a frequent model slip that breaks
    compilation: f\u201c...\u201d). Only listed when the file failed to compile."""
    hits, in_str = [], None  # in_str: the ASCII quote that opened the current string (carried across lines)
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        bad, j = set(), 0
        while j < len(line):
            c = line[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if line.startswith(in_str, j):
                    j += len(in_str)
                    in_str = None
                    continue
            elif c == "#":
                break
            elif c in "'\"":
                in_str = line[j:j + 3] if line[j:j + 3] in ('"""', "'''") else c
                j += len(in_str)
                continue
            elif c in SMART:
                bad.add(c)  # used as code (e.g. a string delimiter), not inside an ASCII-quoted string
            j += 1
        if in_str in ('"', "'"):
            in_str = None  # single-quoted strings end at the line end
        bad = sorted(bad)
        if bad:
            hits.append(f"  line {i}: {' '.join(repr(c) for c in bad)}: {line.strip()[:100]}")
    if not hits:
        return ""
    return ("\n typographic quotes/dashes in code (replace with ASCII \" ' -; inside string literals use \\u2019 escapes):\n"
            + "\n".join(hits[:limit]) + (f"\n  ... {len(hits) - limit} more lines" if len(hits) > limit else ""))


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


V2_KEYS = {  # contract v2 (builds/evolving_calendar_v2/contract.md) top-level and record keys, shape only
    "log": ("entries", ("id", "at", "kind", "item_ids", "payload", "source")),
    "plan": ("plan", ("id", "origin", "status", "ops", "affected")),
    "beliefs": ("beliefs", ("key", "level", "statement", "value", "confidence", "evidence", "updated_at")),
    "view": ("view", ("type", "params", "hidden_counts")),
    "prompts": ("prompts", ("agent", "version", "core_hash", "learned", "diff", "reason", "evidence", "replay", "active")),
    "activity": ("events", ("at", "agent", "kind", "text")),
}
SMOKE_ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:smoke-1\r\nSUMMARY:Smoke check event\r\n"
             "DTSTART:20261021T100000\r\nDTEND:20261021T110000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def v2_shape_problems(kind: str, data) -> list[str]:
    if not isinstance(data, dict):
        return [f"expected a JSON object, got {type(data).__name__}"]
    if kind == "learn":
        return [f"missing key {k!r}" for k in ("beliefs", "proposal") if k not in data]
    if kind == "proposal":
        return [] if "proposal" in data else ["missing key 'proposal'"]
    if kind == "ok":
        return [] if "ok" in data else ["missing key 'ok'"]
    top, keys = V2_KEYS[kind]
    if top not in data:
        return [f"missing key {top!r}"]
    val = data[top]
    recs = val if isinstance(val, list) else [val]
    if isinstance(val, list) is not (kind in ("log", "beliefs", "prompts", "activity")):
        return [f"{top!r} has the wrong type ({type(val).__name__})"]
    for i, r in enumerate(recs[:5]):
        m = _missing(r, keys)
        if m:
            return [f"{top}[{i}] missing {m}" if isinstance(val, list) else f"{top} missing {m}"]
    return []


def v2_calls(do, lines: list):
    """calls: v2 -- the v2 endpoints with neutral inputs (a one-event .ics, an empty learn, a week view), crash and
    shape problems only. Mutating calls run only on ids the app itself returned."""
    do("GET", "/api/log", None, "v2:log", strict=False)
    plan = (do("POST", "/api/import", {"filename": "smoke.ics", "content": SMOKE_ICS}, "v2:plan", strict=False) or {}).get("plan")
    pid = plan.get("id") if isinstance(plan, dict) else None
    if pid:
        do("POST", f"/api/plan/{pid}/apply", {}, "v2:plan", strict=False)
        do("POST", f"/api/plan/{pid}/undo", {}, "v2:plan", strict=False)
    else:
        lines.append("v2: import returned no plan id; plan apply/undo not run")
    do("POST", "/api/learn", {}, "v2:learn", strict=False)
    bel = (do("GET", "/api/beliefs", None, "v2:beliefs", strict=False) or {}).get("beliefs") or []
    if bel and isinstance(bel[0], dict) and bel[0].get("key"):
        do("DELETE", f"/api/beliefs/{bel[0]['key']}", None, "v2:ok", strict=False)
    do("GET", "/api/proposal", None, "v2:proposal", strict=False)
    do("POST", "/api/view", {"type": "week"}, "v2:view", strict=False)
    do("GET", "/api/prompts", None, "v2:prompts", strict=False)
    do("GET", "/api/activity", None, "v2:activity", strict=False)


def v3_shape_problems(kind: str, data) -> list[str]:
    if not isinstance(data, dict):
        return [f"expected a JSON object, got {type(data).__name__}"]
    keys = {"catalog": ("primitives",), "critic": ("ok", "problems", "repaired"),
            "design": ("drafts", "critique", "repairs", "chosen", "proposal")}[kind]
    out = [f"missing key {k!r}" for k in keys if k not in data]
    if kind == "catalog" and isinstance(data.get("primitives"), list):
        m = next((_missing(p, ("type", "props", "children")) for p in data["primitives"][:20]
                  if _missing(p, ("type", "props", "children"))), None)
        out += [f"primitives entry missing {m}"] if m else []
    for k in ("problems", "drafts", "critique"):
        if k in data and not isinstance(data[k], list):
            out.append(f"{k!r} must be a list")
    return out


def v3_calls(do, lines: list):
    """calls: v3 -- v2 plus the designer/critic endpoints: catalog, critic on a one-element spec built from the
    app's own first catalog type, design with no belief. Crash and JSON-shape problems only."""
    cat = (do("GET", "/api/catalog", None, "v3:catalog", strict=False) or {}).get("primitives") or []
    first = cat[0].get("type") if cat and isinstance(cat[0], dict) else None
    if first:
        spec = {"root": "r", "elements": {"r": {"type": first, "props": {}, "children": []}}}
        do("POST", "/api/critic", {"spec": spec}, "v3:critic", strict=False)
    else:
        lines.append("v3: catalog returned no primitive type; /api/critic not run")
    do("POST", "/api/design", {"belief_key": None}, "v3:design", strict=False)


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


def run_selftest(app: Path, tmp: Path, wait: float) -> tuple[int, str]:
    """selftest: true -- `python app.py --selftest --data <tmp>` in its own process before the server starts; the
    team's own self-test (assertions it wrote) reports through its exit code and output. The shipped start-up path is
    untouched, so a failing self-test never stops the scored app from starting. Returns (problems, report line)."""
    data = tmp / "selftest_data"
    data.mkdir(exist_ok=True)
    with open(tmp / "selftest.txt", "w") as outf:
        p = subprocess.Popen([sys.executable, "app.py", "--selftest", "--data", str(data)], cwd=app, stdout=outf,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_app_env(), start_new_session=True)
        t0 = time.time()
        try:
            code = p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            _kill_tree(p)
    out = (tmp / "selftest.txt").read_text(errors="replace")[-TAIL:]
    if code is None:
        return 1, f"selftest: did not finish within {wait:.0f} s (it must exit; do not start the server under --selftest)\noutput tail:\n{out}"
    return int(code != 0), f"selftest: exit {code} in {time.time() - t0:.1f} s" + (f"\noutput tail:\n{out}" if out.strip() else "")


def app_smoke(artifacts: dict[str, str], budget: float = SMOKE_BUDGET, calls: str = "basic",
              selftest: bool = False, selftest_secs: int = 12) -> tuple[bool, str]:
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
                    bad.append(f"{f}: {str(e.msg).strip()[-600:]}" + non_ascii_hint(app / f))
        if bad:
            return False, "SMOKE: FAIL (compile errors)\n" + "\n".join(lines + ["compile errors:"] + bad)
        lines.append("compile: ok")
        if selftest:
            problems_st, st_line = run_selftest(app, Path(tmp), min(selftest_secs, budget / 2))
            lines.append(st_line)
            pre_problems = problems_st
        else:
            pre_problems = 0
        port, data, log = _free_port(), Path(tmp) / "data", Path(tmp) / "stderr.txt"
        data.mkdir()
        with open(log, "w") as errf:
            proc = subprocess.Popen([sys.executable, "app.py", "--port", str(port), "--data", str(data)], cwd=app,
                                    stdout=errf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=_app_env(), start_new_session=True)
        base, problems = f"http://127.0.0.1:{port}", pre_problems
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
                shp = (v2_shape_problems(kind[3:], obj) if kind.startswith("v2:") else
                       v3_shape_problems(kind[3:], obj) if kind.startswith("v3:") else shape_problems(kind, obj))
                problems += bool(shp)
                lines.append(f"{head}; shape {'ok' if not shp else 'WRONG: ' + '; '.join(shp)}; body: {text[:300]}")
                seen.append(obj)
                return obj

            for method, path, body, kind in SMOKE_CALLS:
                do(method, path, body, kind)
                if proc.poll() is not None:
                    break
            if calls in ("deep", "v2", "v3") and proc.poll() is None:
                deep_calls(do, seen, lines)
            if calls in ("v2", "v3") and proc.poll() is None:
                v2_calls(do, lines)
            if calls == "v3" and proc.poll() is None:
                v3_calls(do, lines)
        finally:
            _kill_tree(proc)
        out = log.read_text(errors="replace")
        if "Traceback" in out:
            problems += 1
            lines.append(f"server output contains a traceback:\n{out[out.rfind('Traceback'):][-TAIL:]}")
    ok = problems == 0
    return ok, (f"SMOKE: PASS" if ok else f"SMOKE: FAIL ({problems} problems)") + "\n" + "\n".join(lines)


CHECKS = {"app_smoke": app_smoke}
CHECK_OPTIONS = {"app_smoke": {"calls": ("basic", "deep", "v2", "v3"), "selftest": (False, True), "selftest_secs": (12, 20)}}  # option -> allowed values (first = default)


def run_check(name: str, artifacts: dict[str, str], **opts) -> tuple[bool, str]:
    try:
        return CHECKS[name](dict(artifacts), **opts)
    except Exception as e:  # a check never fails the team run
        return False, f"{name.upper()}: FAIL (check crashed: {e!r:.300})"


def liveness(app_dir: Path, limit: float = 20.0) -> tuple[str, str]:
    """BuildTask fast-fail probe on the materialised app: start it, then make the basic SMOKE_CALLS within `limit`
    seconds (startup may take the contract's 15 s on top). Returns ("alive" | "no_start" | "hung", detail).
    "hung" = it started but at least one basic call got no answer at all (an HTTP error IS an answer)."""
    app_dir = Path(app_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="sea_live_") as tmp:
        port, data = _free_port(), Path(tmp) / "data"
        data.mkdir()
        with open(Path(tmp) / "out.txt", "w") as outf:
            proc = subprocess.Popen([sys.executable, "app.py", "--port", str(port), "--data", str(data)], cwd=app_dir,
                                    stdout=outf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=_app_env(), start_new_session=True)
        base = f"http://127.0.0.1:{port}"
        try:
            t0 = time.time()
            while time.time() - t0 < HEALTH_WAIT:
                if proc.poll() is not None:
                    return "no_start", f"exited with code {proc.returncode}"
                try:
                    if _call(base, "GET", "/health", None, 2)[0] == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            else:
                return "no_start", f"no /health within {HEALTH_WAIT:.0f} s"
            t1 = time.time()
            for method, path, body, _ in SMOKE_CALLS:
                left = limit - (time.time() - t1)
                try:
                    _call(base, method, path, body, max(left, 0.5))
                except urllib.error.URLError as e:
                    if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                        return "hung", f"{method} {path}: no answer within {limit:.0f} s total for the basic calls"
                    return "hung", f"{method} {path}: connection failed ({e!r:.150})"
                except (TimeoutError, socket.timeout):
                    return "hung", f"{method} {path}: no answer within {limit:.0f} s total for the basic calls"
                except Exception as e:
                    return "hung", f"{method} {path}: {e!r:.150}"
            return "alive", f"basic calls answered in {time.time() - t1:.1f} s"
        finally:
            _kill_tree(proc)
