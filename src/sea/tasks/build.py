"""BuildTask: the team writes a runnable app against a frozen contract; a frozen harness drives it over HTTP.

Spec `build:<dir>`; <dir> holds contract.md, harness.py (run_split(app_dir, split, timeout, workers) -> rows
{id, score, hard, reason, detail, checks, secs}) and scenarios.py (SPLITS = {"fit": [...], "holdout": [...]}, each
scenario {id, persona, description}). One evaluation = the team runs ONCE on contract + fit scenario descriptions
(never holdout); its code comes back as artifacts, is materialised into <eval dir>/app/, and the harness runs the fit
and the holdout split on that same app.

Artifact name -> file: `__` -> `/`, then the last `_<ext>` -> `.<ext>` for ext in FILE_EXTS
(app_py -> app.py, index_html -> index.html, static__app_js -> static/app.js). Other artifacts are not files.

Fitness = score(fit) - lam_tokens * team_tokens/1000.
"""
from __future__ import annotations

import os
import concurrent.futures as cf
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from .. import models
from ..executor import run_team
from ..fitness import FitnessSpec, Term
from ..human import HumanChannel
from .base import Task, summarize

FILE_EXTS = ("py", "html", "js", "css", "json", "md", "txt")
FENCE_RE = re.compile(r"\A\s*```[^\n]*\n(.*?)\n?```\s*\Z", re.S)
HARNESS_TIMEOUT, HARNESS_WORKERS = 60, 3
APP_FILE_CAP = 40000  # chars of each produced file shown to the evolver

DESC = """TASK the team solves: WRITE A RUNNABLE APP that implements the frozen contract below. There is no dataset;
the team runs ONCE per evaluation and its code is then started and driven over HTTP by a frozen harness.

HOW CODE COMES BACK: any llm role emits files as artifacts, `<artifact name="app_py">...full file...</artifact>`.
Artifact name -> file path: `__` means `/`, and the last `_<ext>` means `.<ext>` for ext in py, html, js, css, json,
md, txt. So app_py -> app.py, index_html -> index.html, agents_py -> agents.py, static__app_js -> static/app.js.
Artifacts with other names (e.g. a plan) are not files. Always emit the WHOLE file; a later artifact with the same
name replaces the earlier one (so an integrator role can re-emit a fixed file). A ``` fence inside an artifact is
stripped. Roles can read earlier roles' files with reads: [artifacts] or artifact.<name>.

RUNTIME: Python 3.12, standard library only. The harness runs `python app.py --port <PORT> --data <DIR>` in the app
directory, waits for GET /health, then plays each scenario as a person would (reset, commands, chat, clock ticks) and
checks the contract's required behaviour. Every request must answer within 60 s. Model calls from the app go to
Crusoe (env vars in the contract) and must fall back to rules on failure. The app's own LLM calls are NOT counted in
team tokens; the team's tokens (writing the code) are.

PARALLEL WORK: a workflow step `{{parallel: [backend, agents, ui]}}` runs those roles concurrently, each starting from
the blackboard/artifacts as they were before the step (they cannot see each other's output). Use it for independent
files after an architect has fixed the interfaces, then a sequential integrator to reconcile them.

WHO WRITES THE CODE: the team's models (Nemotron). You design the team; you do not write the product. Tool roles
are rejected, and a genome whose soul/prompts/notes hold more than 25 lines of code is rejected. Prompts may name
functions, JSON shapes, ids and rules in prose or short snippets.
RUN YOUR OWN CODE: a `kind: check` role with `check: app_smoke` (framework code, 0 tokens, <= 40 s) materialises the
current file artifacts, compiles every .py, starts `python app.py`, waits for /health and makes generic contract calls
(POST /api/reset with no items, GET /api/state, POST /api/command "call mom" dump, GET /api/focus). Its output
(blackboard[<role name>]) starts with "SMOKE: PASS" or "SMOKE: FAIL (...)" and lists compile errors, the startup
traceback, HTTP status and JSON-shape problems per call. It is NOT the scoring harness (no scenarios, no behaviour
checks): it catches code that does not compile, does not start or answers with the wrong shape. Add `calls: deep` to
the check role to also chain chat "yes" on the created item, items/<id>/done, clock +2 h and undo (crash/shape only).
Add `selftest: true` to also run `python app.py --selftest --data <tmp>` in a separate process first (<= 12 s, must exit;
nonzero exit or output = the team's own assertions failing, reported to the fixer): behaviour checks the team writes
itself, without making the real start-up crash. Typical use, after the
files are written: `smoke, {{loop: {{body: [fixer, smoke], max_rounds: 2, stop_when: "SMOKE: PASS", check_first: true}}}}`
where fixer reads blackboard.smoke and artifacts and re-emits only the broken files (check_first skips the loop when
the first smoke already passed).
THINKING: the worker is a reasoning model; with thinking on it can spend the whole max_tokens budget thinking and
emit no file at all. A role may pin `model: crusoe:<model id>#nothink` (thinking off: much faster and cheaper, good
for writing a file from a precise plan) or keep $WORKER for planning/review roles.

Context selectors for roles: input (= contract + fit scenarios), contract, scenarios (fit scenario descriptions).
Each scenario's harness checks are listed per row in fit_rows.jsonl (check name, pass/fail, detail). Further HIDDEN
scenarios (other personas, same contract) are used for gating; implement the contract, not the visible scenarios.

THE CONTRACT:
{contract}

FIT SCENARIOS (descriptions):
{scenarios}"""


def artifact_path(name: str) -> str | None:
    """Artifact name -> relative file path, or None if the name does not denote a file."""
    head, sep, ext = name.replace("__", "/").rpartition("_")
    if not sep or ext not in FILE_EXTS or not head or head.endswith("/"):
        return None
    return f"{head}.{ext}"


def strip_fence(text: str) -> str:
    m = FENCE_RE.match(text)
    return (m.group(1) if m else text).strip("\n") + "\n"


def materialise(artifacts: dict[str, str], app_dir: Path) -> tuple[list[str], list[str]]:
    """Write file artifacts under app_dir. Returns (written paths, rejected names). Paths escaping app_dir are rejected."""
    app_dir = Path(app_dir)
    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True)
    root = app_dir.resolve()
    written, rejected = [], []
    for name, body in artifacts.items():
        rel = artifact_path(name)
        if rel is None:
            continue
        dst = (app_dir / rel).resolve()
        if rel.startswith("/") or not dst.is_relative_to(root) or dst == root:
            rejected.append(name)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(strip_fence(body))
        written.append(str(dst.relative_to(root)))
    return sorted(written), rejected


MAX_GENOME_CODE_LINES = 25
CODE_LINE = re.compile(r"^\s*(def |class |import |from \S+ import |return\b|if .*:\s*$|for .*:\s*$|while .*:\s*$|elif |else:|try:|except|"
                       r"function\b|const |let |var |document\.|<(div|script|style|button|input|section|html|body)\b|\}\s*$|"
                       r"[A-Za-z_][A-Za-z0-9_.\[\]'\"]*\s*=\s*\S)")


def code_lines(text: str) -> int:
    """Lines that look like Python/JS/HTML source (a crude guard against the evolver writing the product itself)."""
    return sum(bool(CODE_LINE.match(l)) for l in text.splitlines())


def trace_health(trace: list[dict]) -> dict:
    """Framework-level failure signals of one team run: roles cut off at max_tokens (finish=length; a cut-off file
    artifact is never closed, so it is lost), llm roles that emitted nothing, and 429/5xx/timeout retries per model."""
    llm = [t for t in trace if t.get("kind") == "llm"]
    retries: dict[str, int] = {}
    for t in llm:
        if t.get("retries"):
            retries[t.get("model", "?")] = retries.get(t.get("model", "?"), 0) + t["retries"]
    return {"truncated_roles": [t["role"] for t in llm if t.get("finish") == "length"],
            "empty_roles": [t["role"] for t in llm if not t.get("chars")],
            "model_retries": retries}


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m  # dataclasses / pickling inside the harness need the module registered
    spec.loader.exec_module(m)
    return m


class BuildTask(Task):
    name = "build"
    context_keys = ("contract", "scenarios")

    def __init__(self, spec_dir: str | Path, human: HumanChannel | None = None, lam_tokens: float = 0.001,
                 harness_timeout: int = HARNESS_TIMEOUT, harness_workers: int = HARNESS_WORKERS, repeats: int = 2):
        super().__init__({"fit": 1.0}, "holdout", [], "fit", lam_tokens, human)
        self.spec_dir = Path(spec_dir).resolve()
        self.contract = (self.spec_dir / "contract.md").read_text()
        self.harness_timeout, self.harness_workers = harness_timeout, harness_workers
        self.repeats = max(1, int(os.environ.get("SEA_BUILD_REPEATS", repeats)))
        self._harness = self._scen = None
        self._load_lock = threading.Lock()

    # ---------------------------------------------------------------- frozen spec pieces (loaded lazily)
    def harness(self):
        with self._load_lock:  # team runs evaluate concurrently: load the module once
            if self._harness is None:
                self._harness = _load_module(self.spec_dir / "harness.py", f"sea_build_harness_{abs(hash(self.spec_dir))}")
        return self._harness

    def scenarios(self, split: str) -> list[dict]:
        with self._load_lock:
            if self._scen is None:
                self._scen = _load_module(self.spec_dir / "scenarios.py", f"sea_build_scenarios_{abs(hash(self.spec_dir))}").SPLITS
        return list(self._scen[split])

    def scenario_text(self) -> str:
        return "\n".join(f"- {s['id']} (persona {s.get('persona', '-')}): {s['description']}" for s in self.scenarios("fit"))

    # ---------------------------------------------------------------- Task protocol
    def describe(self) -> str:
        return DESC.format(contract=self.contract, scenarios=self.scenario_text()) + "\n\n" + models.catalog_text()

    def moves(self) -> str:
        return ("pick a model per role from MODELS (strong coder for big files, a cheap fast one for plans/checks; thinking on or #nothink per role), split the work by file (architect -> {parallel: [backend, agents, ui]} -> integrator), merge roles that "
                "only add tokens, add a reviewer that checks the code against the contract's numbered behaviours and "
                "re-emits fixed files, give a role the exact JSON shapes / ids it keeps getting wrong, make the app "
                "rule-based where the harness checks fail on LLM flakiness, or add a smoke check (kind: check, check: app_smoke) "
                "with a fixer loop so the team runs its own app and repairs crashes before it is scored (a reviewer-gated fix needs no "
                "conditional step: {loop: {body: [reviewer, fixer, smoke], stop_on: reviewer, stop_when: ALL_CLEAR}} ends "
                "as soon as the reviewer is clean, before the fixer reads anything). Tool roles are not "
                "allowed and the genome may hold at most 25 lines of code: describe what to build, the models write it. Look at fit_rows.jsonl (failed checks) "
                "and app/ (the parent's code) before changing anything.")

    def load_child(self, gdir: Path) -> dict:
        """The product must be written by the team's models, not by the evolver: no tool roles, and no product code
        pasted into the genome (soul, prompts, memory notes) beyond short snippets."""
        g = super().load_child(gdir)
        assert all(r["kind"] != "tool" for r in g["roles"]), "build mode: tool roles are not allowed (the team's models write the code)"
        for r in g["roles"]:
            m = r.get("model", "$WORKER")
            assert m == "$WORKER" or (m.startswith("crusoe:") and models.allowed(m[7:])) or (
                m.startswith("claude:") and models.allowed(m)), (
                f"build mode: role {r['name']} model {m!r} is not in the catalog pool ({models.pool()}); see MODELS")
        text = "\n".join([g.get("soul", "")] + [r.get("prompt", "") for r in g["roles"]] + list(g["_memory"].values()))
        n = code_lines(text)
        assert n <= MAX_GENOME_CODE_LINES, (f"build mode: the genome holds {n} lines of code (max {MAX_GENOME_CODE_LINES}); "
                                            "describe what to build, the team writes the code")
        return g

    def fitness_spec(self) -> FitnessSpec:
        return FitnessSpec([Term("score:fit", 1.0, label="score(fit)"),
                            Term("tokens", -self.lam_tokens, 1000, "team_tokens/1000")])

    def episodes(self, split: str) -> list[dict]:
        scen = self.scenario_text()
        text = f"# Contract\n\n{self.contract}\n\n# Scenarios the app will be tested on (examples)\n\n{scen}"
        return [{"id": "build", "input": {"text": text}, "context": {"contract": self.contract, "scenarios": scen},
                 "sample": {}}]

    # ---------------------------------------------------------------- one evaluation
    def team_run(self, gdir, worker: str, root: Path) -> dict:
        """Run the team once (cached in root/team.json) and materialise its files into root/app/."""
        f = root / "team.json"
        if f.exists():
            return json.loads(f.read_text())
        root.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            res = run_team(self.load(gdir), self.episodes("fit")[0], worker, self.human)
        except Exception as e:
            res = {"artifacts": {}, "tokens": 0, "usage": {}, "questions": [], "assumptions": [], "blackboard": {},
                   "trace": [], "answer": "", "error": repr(e)[:800]}
        res["team_secs"] = round(time.time() - t0, 1)
        res["files"], res["rejected"] = materialise(res["artifacts"], root / "app")
        f.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        return res

    def run_harness(self, app_dir: Path, split: str, res: dict) -> list[dict]:
        ids = [s["id"] for s in self.scenarios(split)]

        def zero(reason, detail):
            return [{"id": i, "score": 0.0, "hard": False, "reason": reason, "detail": detail, "checks": [], "secs": 0}
                    for i in ids]
        if "app.py" not in res["files"]:
            return zero("no_app_py", res.get("error") or f"team produced files {res['files']}")
        try:
            return list(self.harness().run_split(app_dir, split, timeout=self.harness_timeout, workers=self.harness_workers))
        except Exception as e:
            return zero("harness_error", repr(e)[:500])

    def evaluate(self, gdir, split: str, worker: str, out, workers: int = 3, limit: int | None = None) -> dict:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        res = self.team_run(gdir, worker, out.parent)
        rows = self.run_harness(out.parent / "app", split, res)[:limit]
        g = self.load(gdir)
        role_model = {r["name"]: (worker if r.get("model", "$WORKER") == "$WORKER" else r["model"]) for r in g["roles"]}
        weighted = round(sum(t.get("tokens", 0) * models.cost(role_model.get(t["role"], worker))
                             for t in res.get("trace", [])) if res.get("trace") else res["tokens"] * models.cost(worker))
        for r in rows:  # summarize() averages r["tokens"]: every row carries the team run's cost-weighted tokens
            r.setdefault("detail", "")
            r.setdefault("hard", r.get("score", 0) >= 1.0)
            r["tokens"] = weighted
        with open(out / "rows.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rep = summarize(rows)
        check_fail: dict[str, int] = {}
        for r in rows:
            for c in r.get("checks") or []:
                if isinstance(c, dict) and not c.get("ok", c.get("pass", True)):
                    check_fail[str(c.get("name", c.get("id", "?")))] = check_fail.get(str(c.get("name", c.get("id", "?"))), 0) + 1
        rep.update({"genome": g.get("id"), "split": split, "worker": worker, "n": len(rows), "raw_tokens": res["tokens"],
                    "role_models": role_model,
                    "secs": round(time.time() - t0, 1), "team_secs": res.get("team_secs"), "eval_version": "",
                    "files": res["files"], "rejected_files": res["rejected"], "team_error": res.get("error"),
                    "failed_checks": dict(sorted(check_fail.items(), key=lambda x: -x[1])),
                    "role_tokens": {t["role"]: t.get("tokens", 0) for t in res.get("trace", [])},
                    **trace_health(res.get("trace", []))})
        (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
        return rep

    def evaluate_fit(self, gdir, worker: str, out, splits: list[str] | None = None, workers: int = 3) -> dict:
        """The team writes the app `repeats` times independently (concurrently); each app is scored on fit and holdout,
        and the per-run scores are averaged. One team run varies a lot (P1: fit 0.745 then 0.248, same genome: files
        written in parallel disagreed on interfaces), so a single sample decides acceptance by luck.
        Fit drives fitness; holdout is logged (score:holdout) but not used."""
        out = Path(out)
        merged = {s: out / s / "report.json" for s in ("fit", "holdout")}
        if not all(f.exists() for f in merged.values()):
            with cf.ThreadPoolExecutor(self.repeats) as ex:
                runs = list(ex.map(lambda k: {s: self.evaluate(gdir, s, worker, out / f"r{k}" / s) for s in ("fit", "holdout")},
                                   range(self.repeats)))
            for s in ("fit", "holdout"):
                self._merge(out, s, [r[s] for r in runs])
        fit, hold = (json.loads(merged[s].read_text()) for s in ("fit", "holdout"))
        metrics = {"score:fit": fit["score"], "tokens": fit["tokens_per_sample"], "score:holdout": hold["score"],
                   "fit_min": min(fit["per_run"]), "fit_max": max(fit["per_run"])}
        return {"reports": {"fit": fit}, "holdout": hold, "metrics": metrics, "fitness": self.fitness_spec()(metrics)}

    def _merge(self, out: Path, split: str, reps: list[dict]):
        rows = []
        for k in range(len(reps)):
            for l in open(out / f"r{k}" / split / "rows.jsonl"):
                r = json.loads(l)
                rows.append({**r, "id": f"{r['id']}@r{k}"})
        rep = summarize(rows)
        check_fail: dict[str, int] = {}
        for x in reps:
            for c, n in (x.get("failed_checks") or {}).items():
                check_fail[c] = check_fail.get(c, 0) + n
        rep.update({"genome": reps[0]["genome"], "split": split, "worker": reps[0]["worker"], "n": len(rows),
                    "repeats": len(reps), "per_run": [x["score"] for x in reps],
                    "team_secs": [x.get("team_secs") for x in reps], "files": [x.get("files") for x in reps],
                    "rejected_files": [x.get("rejected_files") for x in reps],
                    "team_error": [x.get("team_error") for x in reps if x.get("team_error")] or None,
                    "failed_checks": dict(sorted(check_fail.items(), key=lambda x: -x[1])),
                    "role_tokens": [x.get("role_tokens") for x in reps], "eval_version": "",
                    "truncated_roles": [x.get("truncated_roles") for x in reps],
                    "empty_roles": [x.get("empty_roles") for x in reps],
                    "model_retries": [x.get("model_retries") for x in reps]})
        (out / split).mkdir(parents=True, exist_ok=True)
        with open(out / split / "rows.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        (out / split / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))

    # ---------------------------------------------------------------- what the evolver sees
    def workspace_files(self, parent_eval: Path) -> dict[str, str]:
        parent_eval = Path(parent_eval)
        rep = json.loads((parent_eval / "fit" / "report.json").read_text())
        rows = [json.loads(l) for l in open(parent_eval / "fit" / "rows.jsonl")]
        hf = parent_eval / "holdout" / "report.json"
        hold = {"score": json.loads(hf.read_text())["score"]} if hf.exists() else None
        scen = {s["id"]: s for s in self.scenarios("fit")}
        files = {
            "contract.md": self.contract,
            "reports.json": json.dumps({"fit": rep, "holdout_aggregate": hold}, indent=1, ensure_ascii=False),
            "fit_rows.jsonl": "\n".join(json.dumps({**{k: r.get(k) for k in ("id", "score", "hard", "reason", "detail", "checks", "secs")},
                                                    "scenario": scen.get(r["id"].split("@")[0])}, ensure_ascii=False) for r in rows),
        }
        roots = sorted(parent_eval.glob("r*/")) or [parent_eval]
        for root in roots:
            tag = root.name if root != parent_eval else ""
            team = root / "team.json"
            if team.exists():
                t = json.loads(team.read_text())
                files[f"team_trace{'_' + tag if tag else ''}.json"] = json.dumps(
                    {"trace": t.get("trace", []), "tokens": t.get("tokens"), "team_secs": t.get("team_secs"),
                     "error": t.get("error"), "rejected_files": t.get("rejected", [])}, indent=1)
            app = root / "app"
            for p in sorted(app.rglob("*")) if app.is_dir() else []:
                if p.is_file() and p.suffix.lstrip(".") in FILE_EXTS:
                    txt = p.read_text(errors="replace")
                    files[f"app{'_' + tag if tag else ''}/{p.relative_to(app)}"] = (
                        txt if len(txt) <= APP_FILE_CAP else txt[:APP_FILE_CAP] + "\n# ... (truncated)\n")
        return files

    def summarize_child(self, ev: dict) -> str:
        r = ev["reports"]["fit"]
        hold = ev.get("holdout", {}).get("score")
        worst = list((r.get("failed_checks") or {}).items())[:4]
        return (f"score(fit) {r['score']} (per team run {r.get('per_run')}), holdout {hold}, team tokens {r['tokens_per_sample']}, files {r.get('files')}; "
                f"fails: {r['fail_reasons']}; most-failed checks {worst}"
                + (f"; team error {r['team_error']}" if r.get("team_error") else "")
                + (f"; roles cut off at max_tokens per run {r['truncated_roles']}" if any(r.get("truncated_roles") or []) else "")
                + (f"; roles that emitted nothing per run {r['empty_roles']}" if any(r.get("empty_roles") or []) else ""))

    def cases(self, rows: list[dict], role: str, k_fail: int = 6, k_ok: int = 2) -> str:
        rows = sorted(rows, key=lambda r: r["score"])
        out = [f"### scenario {r['id']} — score {r['score']} ({r['reason']})\n{r.get('detail', '')}\n"
               f"checks: {json.dumps(r.get('checks'), ensure_ascii=False)[:1500]}" for r in rows[:k_fail] + rows[-k_ok:]]
        return "HARNESS RESULTS ON THE TEAM'S APP:\n" + "\n\n".join(out)

    def experience(self, gdir, worker: str, out: Path, offset: int = 0, batch: int = 0) -> list[dict]:
        self.evaluate(gdir, "fit", worker, Path(out) / "fit")
        return [json.loads(l) for l in open(Path(out) / "fit" / "rows.jsonl")]
