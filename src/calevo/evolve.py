"""L2 evolution loop (frozen during runs). Resumable: all state lives in <run>/state.json.

Recursive: a child genome that beats its parent becomes the new parent.
Iterative: if no child beats the parent, the evolver is called again FROM THE PARENT with condensed
           feedback about the failed children (their full traces never reach the next generation).
Stop: gens exhausted, `patience` consecutive generations without improvement, or evolver-call cap.

The evolver is a headless Claude Code agent (`claude -p`) working in an isolated workspace that holds
only: the parent genome, its dev reports, failing dev samples, feedback, and the tool/genome docs.
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

from .runner import ROOT, evaluate, load_genome

LAMBDA = float(os.environ.get("CALEVO_LAMBDA", "0.02"))
# fitness splits + weights; override with CALEVO_SPLITS="dev_in:0.4,ood_dev:0.3,dev_v2:0.3"
SPLIT_W = {k: float(v) for k, v in (x.split(":") for x in
           os.environ.get("CALEVO_SPLITS", "dev_in:0.6,ood_dev:0.4").split(","))}
SPLITS = tuple(SPLIT_W)
MAX_EVOLVER_CALLS = 300


def fitness(reps: dict) -> float:
    tok = sum(reps[s]["tokens_per_sample"] for s in SPLITS) / len(SPLITS)
    return round(sum(w * reps[s]["score"] for s, w in SPLIT_W.items()) - LAMBDA * tok / 1000, 4)


def eval_genome(gdir: Path, worker: str, out: Path) -> dict:
    reps = {}
    for s in SPLITS:
        f = out / s / "report.json"
        reps[s] = json.loads(f.read_text()) if f.exists() else evaluate(gdir, s, worker, out / s, workers=24)
    return {"reports": reps, "fitness": fitness(reps)}


def failing_examples(out: Path, k: int = 24) -> list[dict]:
    ex = []
    for s in SPLITS:
        rows = [json.loads(l) for l in open(out / s / "rows.jsonl")]
        bad = [r for r in rows if r["score"] < 1.0][: k // 2]
        sf = ROOT / "data" / "splits" / f"{s}.jsonl"
        if not sf.exists():  # e.g. AppWorld: task data is encrypted; show the trace summary only
            ex += [{"split": s, "id": r["id"], "reason": r["reason"], "detail": r.get("detail"), "score": r["score"],
                    "trace": r["answer"][-3000:], "blackboard": r.get("blackboard", {})} for r in bad]
            continue
        data = {json.loads(l)["id"]: json.loads(l) for l in open(sf)}
        for r in bad:
            d = data[r["id"]]
            ex.append({"split": s, "id": r["id"], "reason": r["reason"], "detail": r.get("detail"), "score": r["score"],
                       "last_user_message": d["messages"][-1]["content"][-1200:],
                       "expected": d["state"], "model_answer": r["answer"][-1500:],
                       "blackboard": r.get("blackboard", {})})
    return ex


CALENDAR_TASK = """TASK the team solves: a multi-turn conversation where a user adds/changes calendar events with time constraints.
The team must output ONE final reply containing the full calendar as a JSON list
[{"event_id": int, "event_name": str, "start_time": "HH:MM", "duration": int}], all events 10:00-16:00,
no overlaps, every constraint honoured. Some samples (split ood_dev) also contain soft user PREFERENCES phrased
in natural, sometimes fuzzy language; satisfying them earns extra credit (see spec_A.md). Future evaluation will
include preference types you have NOT seen, so prefer general mechanisms over hard-coding the ones you see."""
# override with CALEVO_TASK_DESC=<file> for other benchmarks (math, drop, ...)
TASK_DESC = (Path(os.environ["CALEVO_TASK_DESC"]).read_text() if os.environ.get("CALEVO_TASK_DESC") else CALENDAR_TASK)

INSTRUCTIONS = """You are the EVOLVER of a self-improving team of LLM agents. You design the NEXT generation of the team.

{task_desc}

FITNESS = {fitness_formula} - {lam} * (avg tokens per sample / 1000).
Fewer/cheaper agents are better unless they buy accuracy. Worker model is fixed: {worker} (write model: $WORKER).

FILES HERE (read them first):
- parent/genome.yaml (+ parent/tools/) : the current best team. Fitness {parent_fit}.
- reports.json : parent's scores, token use and failure-reason counts per split.
- failures.jsonl : failing samples with the expected constraints, the team's answer and every role's output.
- feedback.md : children you already proposed from this parent that did NOT beat it, and why. Do not repeat them.
- genome_schema.md : the genome format and what tools may do. spec_*.md : how the preferences you can see are scored.

YOUR JOB: diagnose the parent's failures, then write {k} DIFFERENT child genomes into child_1/ ... child_{k}/
(each a genome.yaml, plus tools/*.py if it uses tool roles). Typical moves - pick what the evidence supports:
hire (add a role), fire/merge (remove redundant roles), rewrite a role's prompt, change the order/topology,
{tool_line}
Make the children explore distinct hypotheses (e.g. one cheap prompt fix, one structural change).
Each genome.yaml must set id: {gen_prefix}_c<N>, parent: {parent_id}, and a `notes:` field explaining in 2-4 sentences
what you changed and which failure evidence motivated it. Do not read anything outside this directory (in particular never read data/splits, ood/, or src/).
When done, reply with one line per child: id - one-sentence summary.
"""

SCHEMA_DOC = (ROOT / "src" / "calevo" / "runner.py").read_text().split('"""')[1]
TOOL_DOC = """
TOOLS (only when tool roles are allowed): a tool role runs tools/<file>.py in a subprocess (60 s limit) calling
run(blackboard: dict, messages: list[dict]) -> dict. Return {"text": "..."} - that text becomes blackboard[role_name].
blackboard holds every earlier role's output text by role name. Importable helper library:
  from calevo.verifier import extract_calendar, to_min, parse_constraint, check_pref, hard_ok
  from calevo.solver import solve, to_calendar_json   # solve(events, prefs=None) -> {event_id: start_minute} | None
     events = {id: {"duration": int, "constraint": "before 2pm"|"after 11:30am"|"at 1pm"|"between 10am and 2pm"|None,
                     "min_time": "10:00", "max_time": "16:00"}}
     prefs  = [{"type": "prefer_window", "event_id": i, "start": "HH:MM", "end": "HH:MM"}]   # the only built-in pref type
You may also write your own code. The final answer must not contain "<think>".
"""


def call_evolver(ws: Path, prompt: str, log: Path, timeout: int = 1500) -> str:
    cmd = ["claude", "-p", "--model", "opus", "--permission-mode", "acceptEdits",
           "--allowedTools", "Read,Write,Edit,Glob,Grep", "--output-format", "json", prompt]
    p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout)
    log.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    try:
        return json.loads(p.stdout).get("result", "")
    except Exception:
        return p.stdout[-2000:]


def run(run_dir: str, worker: str, start: str, gens: int = 10, k: int = 3, max_attempts: int = 3,
        patience: int = 2, allow_tools: bool = True):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    sf = run_dir / "state.json"
    st = json.loads(sf.read_text()) if sf.exists() else {
        "gen": 0, "attempt": 0, "parent": None, "parent_fit": None, "stale": 0, "evolver_calls": 0,
        "feedback": [], "done": False, "allow_tools": allow_tools, "worker": worker}

    def save():
        sf.write_text(json.dumps(st, indent=1))

    def log_hist(rec):
        with open(run_dir / "history.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if st["parent"] is None:
        g0 = run_dir / "genomes" / "gen00"
        if not g0.exists():
            shutil.copytree(start, g0)
        r = eval_genome(g0, worker, run_dir / "evals" / "gen00")
        st.update(parent=str(g0), parent_fit=r["fitness"], parent_eval=str(run_dir / "evals" / "gen00"))
        log_hist({"gen": 0, "id": "gen00", "accepted": True, "fitness": r["fitness"], **_brief(r, g0)})
        save()

    while not st["done"]:
        if st["gen"] >= gens or st["stale"] >= patience or st["evolver_calls"] >= MAX_EVOLVER_CALLS:
            st["done"] = True
            save()
            break
        gen, att = st["gen"] + 1, st["attempt"] + 1
        tag = f"gen{gen:02d}_a{att}"
        ws = run_dir / "workspaces" / tag
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        shutil.copytree(st["parent"], ws / "parent")
        pe = Path(st["parent_eval"])
        (ws / "reports.json").write_text(json.dumps(
            {s: json.loads((pe / s / "report.json").read_text()) for s in SPLITS}, indent=1))
        with open(ws / "failures.jsonl", "w") as f:
            for e in failing_examples(pe):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        (ws / "feedback.md").write_text("\n\n".join(st["feedback"]) or "(none yet)")
        if os.environ.get("CALEVO_SCHEMA_DOC"):  # task-specific genome schema (e.g. appworld/README.md section)
            (ws / "genome_schema.md").write_text(Path(os.environ["CALEVO_SCHEMA_DOC"]).read_text())
        else:
          (ws / "genome_schema.md").write_text(SCHEMA_DOC + (TOOL_DOC if st["allow_tools"] else
                                             "\nTool roles are NOT allowed in this run: llm roles only.\n"))
        for spec in filter(None, os.environ.get("CALEVO_SPECS", "spec_A.md").split(",")):
            shutil.copy(ROOT / "ood" / spec, ws / spec)
        parent_id = yaml.safe_load(open(Path(st["parent"]) / "genome.yaml")).get("id")
        prompt = INSTRUCTIONS.format(
            task_desc=TASK_DESC,
            fitness_formula=" + ".join(f"{w}*score({k})" for k, w in SPLIT_W.items()), lam=LAMBDA,
            worker=worker, parent_fit=st["parent_fit"], k=k, gen_prefix=f"gen{gen:02d}a{att}", parent_id=parent_id,
            tool_line=("add or improve tool code (tools/*.py), extend the constraint/preference representation."
                       if st["allow_tools"] else "(tool roles are disabled in this run - llm roles only)."))
        t0 = time.time()
        summary = call_evolver(ws, prompt, run_dir / "workspaces" / f"{tag}.evolver.log")
        st["evolver_calls"] += 1
        save()

        children = []
        for c in sorted(ws.glob("child_*")):
            try:
                g = load_genome(c) if not SPLITS[0].startswith("appworld_") else _load_appworld_genome(c)
                if not st["allow_tools"] and any(r.get("type") == "tool" for r in g["roles"]):
                    raise ValueError("tool roles not allowed")
                dst = run_dir / "genomes" / f"{tag}_{c.name}"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(c, dst)
                children.append(dst)
            except Exception as e:
                st["feedback"].append(f"### {tag}/{c.name}: INVALID genome, not evaluated: {e!r}"[:600])
        results = {}
        with cf.ThreadPoolExecutor(len(children) or 1) as ex:
            futs = {ex.submit(eval_genome, c, worker, run_dir / "evals" / c.name): c for c in children}
            for fu in cf.as_completed(futs):
                c = futs[fu]
                try:
                    results[c] = fu.result()
                except Exception as e:
                    st["feedback"].append(f"### {c.name}: evaluation crashed: {e!r}"[:600])
        best = max(results, key=lambda c: results[c]["fitness"], default=None)
        for c, r in results.items():
            log_hist({"gen": gen, "attempt": att, "id": c.name, "accepted": c == best and r["fitness"] > st["parent_fit"],
                      "fitness": r["fitness"], "parent_fit": st["parent_fit"], "evolver_secs": round(time.time() - t0),
                      **_brief(r, c)})
        if best is not None and results[best]["fitness"] > st["parent_fit"]:
            st.update(parent=str(best), parent_fit=results[best]["fitness"],
                      parent_eval=str(run_dir / "evals" / best.name), gen=gen, attempt=0, stale=0, feedback=[])
        else:
            for c, r in results.items():
                notes = yaml.safe_load(open(c / "genome.yaml")).get("notes", "")
                st["feedback"].append(
                    f"### {c.name} (fitness {r['fitness']} <= parent {st['parent_fit']})\nidea: {notes}\n"
                    + " ".join(f"{s_} {r['reports'][s_]['score']}" for s_ in SPLITS)
                    + f" tokens {r['reports'][SPLITS[0]]['tokens_per_sample']}; fails: {r['reports'][SPLITS[0]]['fail_reasons']}")
            if att >= max_attempts:
                st.update(gen=gen, attempt=0, stale=st["stale"] + 1, feedback=[])  # parent survives this generation
            else:
                st["attempt"] = att
        save()
    return st


def _load_appworld_genome(c: Path) -> dict:
    g = yaml.safe_load(open(c / "genome.yaml"))
    roles = g.get("roles") or []
    assert roles and all(r.get("name") for r in roles), "roles must be a non-empty list of named roles"
    names = {r["name"] for r in roles}
    assert g.get("act") in names, "act must name an llm role"
    for r in roles:
        if r.get("type", "llm") == "tool":
            assert (c / r["tool"]).exists(), f"missing tool file {r['tool']}"
    g["_dir"] = str(c)
    return g


def _brief(r: dict, gdir: Path) -> dict:
    g = yaml.safe_load(open(Path(gdir) / "genome.yaml"))
    rep = r["reports"]  # noqa
    return {"dev_in": rep[SPLITS[0]]["score"], "ood_dev": rep[SPLITS[1]]["score"] if len(SPLITS) > 1 else None,
            "scores": {s_: rep[s_]["score"] for s_ in SPLITS},
            "tokens": sum(rep[s_]["tokens_per_sample"] for s_ in SPLITS) / len(SPLITS), "n_roles": len(g["roles"]),
            "n_llm_roles": sum(x.get("type", "llm") == "llm" for x in g["roles"]),
            "roles": [x["name"] for x in g["roles"]], "notes": g.get("notes", "")}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--worker", default="qwen3-30b-a3b")
    ap.add_argument("--start", default=str(ROOT / "genomes" / "P0"))
    ap.add_argument("--gens", type=int, default=10)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--no-tools", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.run, a.worker, a.start, a.gens, a.k, allow_tools=not a.no_tools), indent=1))
