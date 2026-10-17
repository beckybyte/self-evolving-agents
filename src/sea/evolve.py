"""L2 evolution loop, task-agnostic (the Task plugin decides data, scoring and what the evolver sees). Resumable:
all state lives in <run>/state.json.

Recursive: a child that beats its parent by `margin` becomes the new parent.
Iterative: otherwise the evolver retries FROM THE PARENT with condensed feedback about the failed children.
Every attempt: (1) fold human answers in (Task.refresh; if the evaluation changed, re-evaluate the parent on the new
version), (2) build an isolated workspace, (3) evolver (frozen Claude) writes k children, the last one radical if
--radical, (4) children inherit the parent's memory (+ memory/_shared.md) and optionally learn one gated round,
(5) evaluate, (6) select, archive, and write rationale/<tag>.md (+ design_log.md on acceptance).

  PYTHONPATH=src python -m sea.evolve --task brief:briefs/day_planner.md --run runs/brief_demo --start genomes/v2/brief_P0 \
      --gens 3 --k 3 --radical
  PYTHONPATH=src python -m sea.evolve --task bench:calendar --run runs/cal_v2 --start genomes/P0 --lam 0.02
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from .archive import MapElitesArchive
from .genome import structure_signature
from .tasks import make_task

MAX_EVOLVER_CALLS = 300

INSTRUCTIONS = """You are the EVOLVER of a self-improving team of agents. You design the NEXT generation of the team.

{task_desc}

FITNESS = {formula}. A child replaces the parent only if its fitness exceeds the parent's by {margin}.
Fewer/cheaper agents are better unless they buy quality. Worker model: {worker} (write model: $WORKER).

FILES HERE (read them first):
- parent/genome.yaml (+ parent/tools/, parent/memory/): the current best team. Fitness {parent_fit}{ver_note}.
- genome_schema.md: the genome format (write schema: 2).
- feedback.md: children already proposed from this parent that did NOT beat it, and why. Do not repeat them.
- {task_files}

YOUR JOB: diagnose the parent's weaknesses from the evidence, then write {k} DIFFERENT child genomes into
child_1/ ... child_{k}/ (each a genome.yaml, plus tools/*.py if it uses tool roles). Typical moves - pick what the
evidence supports: {moves}
Make the children explore distinct hypotheses (e.g. one cheap prompt fix, one structural change).{radical}
Each genome.yaml must set id: {gen_prefix}_c<N>, parent: {parent_id}, `mechanism:` (2-4 words naming the team's core
mechanism) and `notes:` (2-4 sentences: what you changed and which evidence motivated it).
If the genome schema or the framework cannot express a mechanism you want (and you had to work around it or give it
up), describe it in FRAMEWORK_WISHES.md: what you wanted, why the evidence calls for it, what you did instead.
Do not read anything outside this directory. When done, reply with one line per child: id - one-sentence summary.
"""
RADICAL = """
child_{k} MUST be RADICAL: a structurally different team (different topology, role kinds, channel or division of
labour), not a tweak of the parent. It is judged by a looser bar: it survives in the diversity archive if it opens
a new niche, even when it does not beat the parent."""


def call_evolver(ws: Path, prompt: str, log: Path, timeout: int = 1800) -> str:
    if os.environ.get("SEA_FAKE_EVOLVER"):  # tests: copy the parent into child_1..k
        k = int(os.environ["SEA_FAKE_EVOLVER"])
        for i in range(1, k + 1):
            shutil.copytree(ws / "parent", ws / f"child_{i}", ignore=shutil.ignore_patterns("memory"))
            g = yaml.safe_load(open(ws / f"child_{i}" / "genome.yaml"))
            g.update(id=f"fake_c{i}", notes=f"fake child {i}")
            (ws / f"child_{i}" / "genome.yaml").write_text(yaml.safe_dump(g, allow_unicode=True))
        return "fake"
    cmd = ["claude", "-p", "--model", os.environ.get("SEA_EVOLVER_MODEL", "opus"), "--permission-mode", "acceptEdits",
           "--allowedTools", "Read,Write,Edit,Glob,Grep", "--output-format", "json", prompt]
    p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    log.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    try:
        return json.loads(p.stdout).get("result", "")
    except Exception:
        return p.stdout[-2000:]


class Evolution:
    def __init__(self, task, run_dir, worker, start, gens=10, k=3, max_attempts=3, patience=2, allow_tools=True,
                 margin=0.0, radical=False, learn=False, learn_batch=40):
        self.task, self.run_dir, self.worker = task, Path(run_dir), worker
        self.gens, self.k, self.max_attempts, self.patience = gens, k, max_attempts, patience
        self.margin, self.radical, self.learn, self.learn_batch = margin, radical, learn, learn_batch
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.sf = self.run_dir / "state.json"
        self.st = json.loads(self.sf.read_text()) if self.sf.exists() else {
            "gen": 0, "attempt": 0, "parent": None, "parent_fit": None, "stale": 0, "evolver_calls": 0,
            "feedback": [], "done": False, "allow_tools": allow_tools, "worker": worker, "start": str(start)}
        if self.st["done"] and self.st["gen"] < gens and self.st["stale"] < patience:  # resumed with more generations
            self.st["done"] = False
        self.archive = MapElitesArchive(self.run_dir / "archive.json")

    # ------------------------------------------------------------ helpers
    def save(self):
        self.sf.write_text(json.dumps(self.st, indent=1, ensure_ascii=False))

    def log_hist(self, rec):
        with open(self.run_dir / "history.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def ev_dir(self, name: str) -> Path:
        return self.run_dir / "evals" / name / (self.task.version() or "v0")

    def evaluate(self, gdir: Path) -> dict:
        return self.task.evaluate_fit(gdir, self.worker, self.ev_dir(gdir.name))

    def brief(self, ev: dict, gdir: Path) -> dict:
        g = self.task.load(gdir)
        return {"metrics": ev["metrics"], "scores": {s: r["score"] for s, r in ev["reports"].items()},
                "n_roles": len(g["roles"]), "n_llm_roles": sum(r.get("kind", r.get("type", "llm")) == "llm" for r in g["roles"]),
                "roles": [r["name"] for r in g["roles"]], "mechanism": g.get("mechanism", ""),
                "notes": g.get("notes", ""), "eval_version": self.task.version()}

    def write_shared(self, gdir: Path):
        notes = getattr(self.task, "shared_notes", lambda: "")()
        if notes:
            (gdir / "memory").mkdir(exist_ok=True)
            (gdir / "memory" / "_shared.md").write_text(notes + "\n")

    # ------------------------------------------------------------ main loop
    def run(self) -> dict:
        st = self.st
        if st["parent"] is None:
            g0 = self.run_dir / "genomes" / "gen00"
            if not g0.exists():
                shutil.copytree(st["start"], g0, ignore=shutil.ignore_patterns("__pycache__"))
            self.write_shared(g0)
            r = self.evaluate(g0)
            st.update(parent=str(g0), parent_fit=r["fitness"], parent_eval=str(self.ev_dir("gen00")))
            self.archive.add("gen00", g0, self.task.load(g0), r["metrics"], r["fitness"])
            self.log_hist({"gen": 0, "id": "gen00", "accepted": True, "fitness": r["fitness"], **self.brief(r, g0)})
            self.save()
        while not st["done"]:
            if st["gen"] >= self.gens or st["stale"] >= self.patience or st["evolver_calls"] >= MAX_EVOLVER_CALLS:
                st["done"] = True
                self.save()
                self.framework_signals()
                break
            self.attempt()
        return st

    def framework_signals(self):
        """Detectors + evolver wishes -> framework proposal backlog (§8.2). Never fails the run. SEA_META_SIGNALS=0 disables."""
        if os.environ.get("SEA_META_SIGNALS", "1") == "0":
            return
        try:
            from .meta.backlog import Backlog, detect
            b = Backlog()
            for kw in detect(self.run_dir):
                p = b.add(**kw)
                self.log_hist({"event": "framework_proposal", "id": p["id"], "title": p["title"]})
        except Exception as e:
            self.log_hist({"event": "framework_signals_error", "error": repr(e)[:300]})

    def maybe_refresh(self, tag: str) -> str:
        """Fold in human answers; if the evaluation changed, re-evaluate the parent on the new version."""
        st = self.st
        old_ver = self.task.version()
        if not self.task.refresh():
            return ""
        parent = Path(st["parent"])
        self.write_shared(parent)
        r = self.evaluate(parent)
        msg = (f"Evaluation revised {old_ver} -> {self.task.version()} after human input; parent re-scored "
               f"{st['parent_fit']} -> {r['fitness']}.")
        st.update(parent_fit=r["fitness"], parent_eval=str(self.ev_dir(parent.name)))
        st["feedback"].append(f"### NOTE: {msg} Earlier feedback was measured on the old evaluation.")
        self.log_hist({"event": "eval_revised", "tag": tag, "from": old_ver, "to": self.task.version(), "parent_fit": r["fitness"]})
        self.save()
        return msg

    def attempt(self):
        st, task = self.st, self.task
        gen, att = st["gen"] + 1, st["attempt"] + 1
        tag = f"gen{gen:02d}_a{att}"
        refresh_msg = self.maybe_refresh(tag)
        ws = self.run_dir / "workspaces" / tag
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        shutil.copytree(st["parent"], ws / "parent", ignore=shutil.ignore_patterns("__pycache__", "_rejected.jsonl"))
        files = task.workspace_files(Path(st["parent_eval"]))
        for name, text in files.items():
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(text)
        (ws / "feedback.md").write_text("\n\n".join(st["feedback"]) or "(none yet)")
        (ws / "genome_schema.md").write_text(task.schema_doc(st["allow_tools"]))
        (ws / "task.md").write_text(task.describe())
        parent_g = task.load(st["parent"])
        ver = task.version()
        prompt = INSTRUCTIONS.format(
            task_desc=task.describe(), formula=task.fitness_spec().formula(), margin=self.margin, worker=self.worker,
            parent_fit=st["parent_fit"], ver_note=f" (evaluation version {ver})" if ver else "",
            task_files="\n- ".join(sorted(files)), k=self.k, moves=task.moves()
            + ("" if st["allow_tools"] else " (tool roles are disabled in this run)"),
            radical=RADICAL.format(k=self.k) if self.radical else "", gen_prefix=f"gen{gen:02d}a{att}", parent_id=parent_g["id"])
        t0 = time.time()
        summary = call_evolver(ws, prompt, self.run_dir / "workspaces" / f"{tag}.evolver.log")
        st["evolver_calls"] += 1
        self.save()

        children = []
        for c in sorted(ws.glob("child_*")):
            try:
                g = task.load_child(c)
                if not st["allow_tools"] and any(r["kind"] == "tool" for r in g["roles"]):
                    raise ValueError("tool roles not allowed")
                dst = self.run_dir / "genomes" / f"{tag}_{c.name}"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(c, dst, ignore=shutil.ignore_patterns("__pycache__"))
                self._inherit_memory(Path(st["parent"]), dst, g)
                if self.learn:
                    from .learn import learn_round
                    tmp = dst.with_name(dst.name + "_learned")
                    ls = learn_round(task, dst, tmp, self.worker, self.run_dir / "learn" / dst.name,
                                     st.get("learn_offset", 0) * self.learn_batch, self.learn_batch)
                    st["learn_offset"] = st.get("learn_offset", 0) + 1
                    shutil.rmtree(dst)
                    tmp.rename(dst)
                    st.setdefault("learn_log", []).append({"child": dst.name, **ls})
                children.append(dst)
            except Exception as e:
                st["feedback"].append(f"### {tag}/{c.name}: INVALID genome, not evaluated: {e!r}"[:600])
                self.log_hist({"event": "invalid_genome", "tag": tag, "child": c.name, "error": repr(e)[:300]})
        results = {}
        with cf.ThreadPoolExecutor(len(children) or 1) as ex:
            futs = {ex.submit(self.evaluate, c): c for c in children}
            for fu in cf.as_completed(futs):
                c = futs[fu]
                try:
                    results[c] = fu.result()
                except Exception as e:
                    st["feedback"].append(f"### {c.name}: evaluation crashed: {e!r}"[:600])
        best = max(results, key=lambda c: results[c]["fitness"], default=None)
        accepted = best is not None and results[best]["fitness"] > st["parent_fit"] + self.margin
        parent_sig = structure_signature(parent_g)
        lines = []
        for c, r in sorted(results.items(), key=lambda x: x[0].name):
            g = task.load(c)
            is_radical = self.radical and c.name.endswith(f"child_{self.k}")
            new_cell = self.archive.add(c.name, c, g, r["metrics"], r["fitness"], radical=is_radical)
            verdict = ("ACCEPTED (new parent)" if accepted and c == best else
                       "kept in archive (new niche)" if new_cell else "rejected")
            self.log_hist({"gen": gen, "attempt": att, "id": c.name, "accepted": accepted and c == best,
                           "fitness": r["fitness"], "parent_fit": st["parent_fit"], "radical": is_radical,
                           "new_cell": new_cell, "structure_changed": structure_signature(g) != parent_sig,
                           "evolver_secs": round(time.time() - t0), **self.brief(r, c)})
            lines.append(f"### {c.name}{' (radical)' if is_radical else ''}: fitness {r['fitness']} -> {verdict}\n"
                         f"- mechanism: {g.get('mechanism', '')}; roles: {[x['name'] for x in g['roles']]}\n"
                         f"- idea (evolver notes): {g.get('notes', '')}\n- evaluation: {task.summarize_child(r)}")
        self._rationale(tag, parent_g, refresh_msg, summary, lines, accepted, best, results)
        if accepted:
            if hasattr(task, "design_log_entry"):
                with open(self.run_dir / "design_log.md", "a") as f:
                    f.write(f"\n\n## {tag}: {best.name} (fitness {results[best]['fitness']}, eval {task.version()})\n"
                            f"{task.load(best).get('notes', '')}\n\n{task.design_log_entry(self.ev_dir(best.name))}\n")
            st.update(parent=str(best), parent_fit=results[best]["fitness"], parent_eval=str(self.ev_dir(best.name)),
                      gen=gen, attempt=0, stale=0, feedback=[])
        else:
            for c, r in results.items():
                g = task.load(c)
                st["feedback"].append(f"### {c.name} (fitness {r['fitness']} <= parent {st['parent_fit']} + margin {self.margin})\n"
                                      f"idea: {g.get('notes', '')}\n{task.summarize_child(r)}")
            if att >= self.max_attempts:
                st.update(gen=gen, attempt=0, stale=st["stale"] + 1, feedback=[])
            else:
                st["attempt"] = att
        self.save()

    def _inherit_memory(self, parent: Path, child: Path, g: dict):
        pm = parent / "memory"
        if not pm.is_dir():
            return
        (child / "memory").mkdir(exist_ok=True)
        for r in g["roles"]:
            src = pm / f"{r['name']}.md"
            if src.exists() and not (child / "memory" / src.name).exists():
                shutil.copy(src, child / "memory" / src.name)
        if (pm / "_shared.md").exists():
            shutil.copy(pm / "_shared.md", child / "memory" / "_shared.md")

    def _rationale(self, tag, parent_g, refresh_msg, summary, lines, accepted, best, results):
        d = self.run_dir / "rationale"
        d.mkdir(exist_ok=True)
        h = self.task.human
        opened = h.open_questions() if h else []
        txt = [f"# {tag}", f"parent: {parent_g['id']} (fitness {self.st['parent_fit']}, eval {self.task.version() or '-'})",
               f"fitness = {self.task.fitness_spec().formula()}"]
        if refresh_msg:
            txt.append(f"\n**{refresh_msg}**")
        txt += ["\n## Evolver summary", summary.strip() or "(none)", "\n## Children"] + lines
        txt.append("\n## Decision")
        if accepted:
            txt.append(f"{best.name} replaces the parent: fitness {results[best]['fitness']} > {self.st['parent_fit']} + {self.margin}.")
        elif results:
            b = max(results.values(), key=lambda r: r["fitness"])["fitness"]
            txt.append(f"No child beat the parent (best {b} vs {self.st['parent_fit']} + {self.margin}); retry from the parent with feedback.")
        else:
            txt.append("No valid child; retry from the parent.")
        if opened:
            txt += ["\n## Open questions to the human (proceeding on defaults)"] + [f"- {q['qid']}: {q['question']} -> assumed: {q['default']}"
                                                                                 for q in opened]
        (d / f"{tag}.md").write_text("\n".join(txt) + "\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="bench:calendar | bench:math | bench:drop | appworld | brief:<file>")
    ap.add_argument("--run", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--worker", default="qwen3-30b-a3b")
    ap.add_argument("--gens", type=int, default=10)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--patience", type=int, default=2)
    ap.add_argument("--margin", type=float, default=None, help="accept margin (default 0 for benchmarks, 0.02 for briefs)")
    ap.add_argument("--fit", help='benchmark fitness splits, e.g. "dev_in:0.6,ood_dev:0.4"')
    ap.add_argument("--holdout")
    ap.add_argument("--lam", type=float, default=None, help="token penalty (default 0.02 benchmarks, 0.002 briefs)")
    ap.add_argument("--lam-complexity", type=float, default=0.1)
    ap.add_argument("--judge")
    ap.add_argument("--framer")
    ap.add_argument("--radical", action="store_true")
    ap.add_argument("--learn", action="store_true", help="gated per-agent learning for every child")
    ap.add_argument("--learn-batch", type=int, default=40)
    ap.add_argument("--no-tools", action="store_true")
    a = ap.parse_args()
    brief = a.task.startswith("brief:")
    opts = {k: v for k, v in {"judge": a.judge, "framer": a.framer}.items() if v}
    if brief:
        opts["lam_complexity"] = a.lam_complexity
    lam = a.lam if a.lam is not None else (0.002 if brief else 0.02)
    task = make_task(a.task, a.run, fit=a.fit, holdout=a.holdout, lam_tokens=lam, **opts)
    ev = Evolution(task, a.run, a.worker, a.start, a.gens, a.k, a.max_attempts, a.patience, not a.no_tools,
                   a.margin if a.margin is not None else (0.02 if brief else 0.0), a.radical, a.learn, a.learn_batch)
    print(json.dumps(ev.run(), indent=1, ensure_ascii=False))
