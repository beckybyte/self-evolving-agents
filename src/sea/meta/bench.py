"""Meta-evaluation (§8.5). Evaluator-side code always comes from the BASE tree; only the code under test comes from
the candidate tree. Tiers run in order; a later tier runs only if the earlier ones pass.

  A correctness  candidate unit tests + frozen safety tests (meta/bench/safety_test.py from base)
  B regression   recorded LLM-call traces on fixed genomes/episodes (fake deterministic LLM), base vs candidate
  C efficacy     short real evolution runs (brief + calendar), candidate vs base (base cached per commit)
"""
from __future__ import annotations

import concurrent.futures as cf
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PY = sys.executable
C_RUNS = {
    "brief": ["--task", "brief:briefs/day_planner.md", "--start", "genomes/v2/brief_P0", "--gens", "2", "--k", "2",
              "--max-attempts", "1", "--judge", "claude:sonnet", "--worker", "qwen3-30b-a3b"],
    "calendar": ["--task", "bench:calendar", "--start", "genomes/P0", "--fit", "dev_in@0:30:0.6,ood_dev@0:20:0.4",
                 "--gens", "2", "--k", "2", "--max-attempts", "1", "--worker", "qwen3-30b-a3b"],
}
C_TOLERANCE = 0.05


def _env(tree: Path) -> dict:
    return {**os.environ, "PYTHONPATH": str(tree / "src")}


def tier_a(tree: Path, base: Path, out: Path) -> dict:
    r1 = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "src/sea", "src/calevo/verifier_test.py"],
                        cwd=tree, env=_env(tree), capture_output=True, text=True, timeout=900)
    r2 = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(base / "meta" / "bench" / "safety_test.py")],
                        cwd=out, env=_env(tree), capture_output=True, text=True, timeout=900)
    (out / "tier_a.log").write_text(r1.stdout[-6000:] + r1.stderr[-3000:] + "\n--- safety ---\n" + r2.stdout[-6000:] + r2.stderr[-3000:])
    return {"pass": r1.returncode == 0 and r2.returncode == 0, "unit": r1.stdout.strip().splitlines()[-1:] or [""],
            "safety": r2.stdout.strip().splitlines()[-1:] or [""]}


def _trace(tree: Path, base: Path, out_f: Path) -> dict:
    p = subprocess.run([PY, str(base / "src" / "sea" / "meta" / "trace.py"), "--bench", str(base / "meta" / "bench"),
                        "--root", str(base), "--out", str(out_f)], env=_env(tree), capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return json.loads(out_f.read_text())


def tier_b(tree: Path, base: Path, out: Path, expected: str = "none") -> dict:
    tb = _trace(base, base, out / "trace_base.json")
    try:
        tc = _trace(tree, base, out / "trace_cand.json")
    except RuntimeError as e:
        return {"pass": False, "error": str(e)[-1500:]}
    keys = sorted(set(tb) | set(tc))
    changed, diffs, chars = [], [], {}
    for k in keys:
        a, b = tb.get(k), tc.get(k)
        ca = sum(len(m["content"]) for c in a or [] for m in c)
        cb = sum(len(m["content"]) for c in b or [] for m in c)
        chars[k] = {"base": ca, "cand": cb}
        if a != b:
            changed.append(k)
            ta = "\n".join(m["content"] for c in a or [] for m in c).splitlines()
            tb_ = "\n".join(m["content"] for c in b or [] for m in c).splitlines()
            diffs.append(f"===== {k} (calls {len(a or [])} -> {len(b or [])}, chars {ca} -> {cb})\n"
                         + "\n".join(list(difflib.unified_diff(ta, tb_, lineterm="", n=1))[:60]))
    (out / "tier_b_diff.txt").write_text("\n\n".join(diffs) or "(identical)")
    cal_changed = [k for k in changed if not k.endswith("|brief")]
    ok = {"none": not changed, "brief": not cal_changed, "any": True}[expected]
    tot = lambda side: sum(v[side] for v in chars.values())
    return {"pass": ok, "expected_prompt_changes": expected, "changed": changed, "n_keys": len(keys),
            "prompt_chars_base": tot("base"), "prompt_chars_cand": tot("cand"),
            "brief_chars": {k: v for k, v in chars.items() if k.endswith("|brief")}}


def _seed(run: Path, tree: Path, base: Path, which: str):
    run.mkdir(parents=True, exist_ok=True)
    if which == "brief" and not (run / "goal" / "CURRENT").exists():
        shutil.copytree(base / "meta" / "bench" / "goal_day_planner", run / "goal" / "v1")
        (run / "goal" / "CURRENT").write_text("v1")


def _evolve(tree: Path, base: Path, run: Path, which: str, timeout: int) -> dict:
    _seed(run, tree, base, which)
    if not (run / "state.json").exists() or not json.loads((run / "state.json").read_text()).get("done"):
        with open(run / "evolve.log", "a") as log:
            subprocess.run([PY, "-m", "sea.evolve", "--run", str(run)] + C_RUNS[which], cwd=tree, env=_env(tree),
                           stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    st = json.loads((run / "state.json").read_text())
    return {"parent_fit": st["parent_fit"], "gen": st["gen"], "cost_usd": round(run_cost(run), 2)}


def run_cost(run: Path) -> float:
    cost = 0.0
    for f in run.glob("workspaces/*.evolver.log"):
        try:
            cost += json.loads(f.read_text().split("\n--- stderr ---")[0]).get("total_cost_usd", 0.0)
        except Exception:
            pass
    for f in run.glob("evals/*/*/fit/rows.jsonl"):
        for l in open(f):
            cost += json.loads(l).get("judge_usage", {}).get("cost_usd", 0.0)
    for f in run.glob("evals/*/*/judge_global.json"):
        cost += json.loads(f.read_text()).get("usage", {}).get("cost_usd", 0.0)
    return cost


def tier_c(tree: Path, base: Path, out: Path, base_cache: Path, budget_usd: float = 15.0, timeout: int = 3600) -> dict:
    t0 = time.time()
    jobs = {}
    with cf.ThreadPoolExecutor(4) as ex:
        for which in C_RUNS:
            jobs[("base", which)] = ex.submit(_evolve, base, base, base_cache / which, which, timeout)
            jobs[("cand", which)] = ex.submit(_evolve, tree, base, out / "c" / which, which, timeout)
        res = {}
        for (side, which), fu in jobs.items():
            try:
                res.setdefault(which, {})[side] = fu.result()
            except Exception as e:
                res.setdefault(which, {})[side] = {"error": repr(e)[:500]}
    ok, cost = True, 0.0
    for which, r in res.items():
        b, c = r.get("base", {}), r.get("cand", {})
        cost += c.get("cost_usd", 0.0)
        r["pass"] = "parent_fit" in b and "parent_fit" in c and c["parent_fit"] >= b["parent_fit"] - C_TOLERANCE
        ok &= r["pass"]
    return {"pass": ok, "runs": res, "candidate_cost_usd": round(cost, 2), "over_budget": cost > budget_usd,
            "tolerance": C_TOLERANCE, "secs": round(time.time() - t0)}
