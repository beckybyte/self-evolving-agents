"""Framework improver pipeline (§8.3-8.6). One proposal = one branch meta/<PID> in its own git worktree, cut from the
integration branch evolve/main. The improver is a Claude Code session: headless (`run`) or a visible session you
start yourself on the task file that `prepare` writes. `finish` then checks the protected surface, runs the
meta-evaluation (tiers A-C, base = evolve/main) and merges into evolve/main on success. Merging into main is yours.

  python -m sea.meta.improve prepare P001        # worktree + task file for a visible Claude Code session
  python -m sea.meta.improve run P001            # prepare + headless `claude -p` improver + finish
  python -m sea.meta.improve finish P001 [--tier-c on|off|auto]
  python -m sea.meta.improve status
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .backlog import STATE, Backlog
from .bench import tier_a, tier_b, tier_c
from .protect import EVALUATOR, PROTECTED, changed_files, violations

ROOT = Path(__file__).resolve().parents[3]
META_ROOT = Path(os.environ.get("SEA_META_ROOT", ROOT.parent / f"{ROOT.name}-meta"))
INTEGRATION = os.environ.get("SEA_META_BRANCH", "evolve/main")
BASE_TREE = META_ROOT / "_integration"
C_DEFAULT = {"learning": "on", "search": "on", "evaluator": "on", "expressiveness": "off", "infra": "off"}
MAX_ATTEMPTS = 2

TASK = """You are the FRAMEWORK IMPROVER of the self-evolving agents system (read docs/architecture_v2.md, §8 is your
job description). You work ONLY in this git worktree: {wt} (branch meta/{pid}, cut from {integration}).

{body}

{retry}RULES
- Implement the smallest change that resolves the proposal, in the style of the surrounding code. Keep v1 genomes and
  benchmark behaviour unchanged unless the proposal says otherwise.
- Do NOT edit these protected paths (the pipeline rejects the branch if you do): {protected}
- Add or adjust unit tests next to the code (src/sea/*_test.py) and run them:
    PYTHONPATH=src python3 -m pytest -q src/sea src/calevo/verifier_test.py
- Write meta/changes/{pid}.md: what changed, why (cite the evidence), risks, how to roll back, and the expected
  effect on prompts/tokens.
- Commit your work on branch meta/{pid} (git add, git commit). Do not push, do not switch branches, do not start
  evolution runs or call paid models: after you finish, the pipeline runs the meta-evaluation itself
  (A: tests + frozen safety tests, B: prompt-trace regression with expected_prompt_changes={epc}, C: short real
  evolution runs vs the base framework).
- If the proposal is wrong or needs a human decision, say so in meta/changes/{pid}.md, commit that, and stop.
When done, reply with a 3-line summary: what changed, test result, anything the human must decide.
"""


def git(*args, cwd=ROOT, check=True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def ensure_integration():
    if not (BASE_TREE / ".git").exists():
        META_ROOT.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", str(BASE_TREE), INTEGRATION)


def task_file(pid: str) -> Path:
    return META_ROOT / f"{pid}.task.md"


def prepare(pid: str) -> Path:
    b = Backlog()
    p = b.get(pid)
    ensure_integration()
    wt = META_ROOT / pid
    if not (wt / ".git").exists():
        exists = git("branch", "--list", f"meta/{pid}")
        git("worktree", "add", str(wt), f"meta/{pid}") if exists else git("worktree", "add", "-b", f"meta/{pid}", str(wt), INTEGRATION)
    prot = PROTECTED + ([] if p["category"] == "evaluator" else EVALUATOR)
    retry = ""
    if p["attempts"]:
        retry = f"THIS IS ATTEMPT {p['attempts'] + 1}. The previous attempt failed the meta-evaluation; see the notes at the end of the proposal.\n\n"
    task_file(pid).write_text(TASK.format(wt=wt, pid=pid, integration=INTEGRATION, body=p["body"], retry=retry,
                                          protected=", ".join(prot), epc=p["expected_prompt_changes"]))
    b.set_status(pid, "in_progress", f"worktree {wt}, task file {task_file(pid)}")
    return wt


def run_headless(pid: str, timeout: int = 3600) -> str:
    wt = prepare(pid)
    tools = ("Read,Edit,Write,Glob,Grep,Bash(PYTHONPATH=src python3 -m pytest:*),Bash(python3 -m pytest:*),"
             "Bash(git add:*),Bash(git commit:*),Bash(git diff:*),Bash(git status:*),Bash(git log:*)")
    cmd = ["claude", "-p", "--model", os.environ.get("SEA_IMPROVER_MODEL", "opus"), "--permission-mode", "acceptEdits",
           "--allowedTools", tools, "--output-format", "json"]
    p = subprocess.run(cmd, input=task_file(pid).read_text(), cwd=wt, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "PYTHONPATH": str(wt / "src")})
    (META_ROOT / f"{pid}.improver.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    try:
        return json.loads(p.stdout).get("result", "")
    except Exception:
        return p.stdout[-2000:]


def finish(pid: str, tier_c_mode: str = "auto") -> dict:
    b = Backlog()
    p = b.get(pid)
    ensure_integration()
    wt = META_ROOT / pid
    if git("status", "--porcelain", cwd=wt):
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", f"{pid}: uncommitted improver changes", cwd=wt)
    out = STATE / "evals" / pid / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    b.set_status(pid, "evaluating", f"meta-evaluation in {out}")
    base_commit = git("rev-parse", "HEAD", cwd=BASE_TREE)
    files = changed_files(str(ROOT), INTEGRATION, f"meta/{pid}")
    forbidden, needs_human = violations(files, p["category"])
    rep = {"pid": pid, "base_commit": base_commit, "candidate_commit": git("rev-parse", "HEAD", cwd=wt),
           "files": files, "forbidden": forbidden, "needs_human_files": needs_human, "tiers": {}}
    ok = bool(files) and not forbidden
    if ok:
        rep["tiers"]["A"] = tier_a(wt, BASE_TREE, out)
        ok = rep["tiers"]["A"]["pass"]
    if ok:
        rep["tiers"]["B"] = tier_b(wt, BASE_TREE, out, p["expected_prompt_changes"])
        ok = rep["tiers"]["B"]["pass"]
    mode = C_DEFAULT[p["category"]] if tier_c_mode == "auto" else tier_c_mode
    if ok and mode == "on":
        rep["tiers"]["C"] = tier_c(wt, BASE_TREE, out, STATE / "base" / base_commit[:12])
        ok = rep["tiers"]["C"]["pass"]
    rep["pass"] = ok
    changes = wt / "meta" / "changes" / f"{pid}.md"
    rep["changes_md"] = changes.read_text() if changes.exists() else "(improver wrote no meta/changes file)"
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    summary = _summary(rep)
    (out / "report.md").write_text(summary)
    if ok and needs_human:
        b.set_status(pid, "needs_human", f"passed meta-evaluation but touches evaluator files {needs_human}: "
                                         f"merge by hand after review.\n{summary}")
    elif ok:
        merge(pid, rep, summary)
        b.set_status(pid, "merged", summary)
    else:
        p = b.get(pid)
        p["attempts"] += 1
        b.save(p)
        b.set_status(pid, "needs_human" if p["attempts"] >= MAX_ATTEMPTS else "open", f"meta-evaluation failed\n{summary}")
    return rep


def merge(pid: str, rep: dict, summary: str):
    git("merge", "--no-ff", "-q", f"meta/{pid}", "-m", f"framework: merge {pid} (meta-evaluation passed)", cwd=BASE_TREE)
    log = BASE_TREE / "docs" / "framework_log.md"
    head = "" if log.exists() else "# Framework log\n\nOne entry per framework change merged by the meta pipeline (§8.6).\n"
    p = Backlog().get(pid)
    with open(log, "a") as f:
        f.write(f"{head}\n## {pid}: {p['title']} ({time.strftime('%Y-%m-%d %H:%M')})\n"
                f"category {p['category']} · base {rep['base_commit'][:10]} · candidate {rep['candidate_commit'][:10]}\n\n"
                f"{summary}\n")
    git("add", "docs/framework_log.md", cwd=BASE_TREE)
    git("commit", "-q", "-m", f"framework_log: {pid}", cwd=BASE_TREE)


def _summary(rep: dict) -> str:
    t = rep["tiers"]
    lines = [f"- files: {', '.join(rep['files']) or '(none)'}"]
    if rep["forbidden"]:
        lines.append(f"- REJECTED: touches protected paths {rep['forbidden']}")
    if "A" in t:
        lines.append(f"- A correctness: {'pass' if t['A']['pass'] else 'FAIL'} (unit: {t['A']['unit'][0]}; safety: {t['A']['safety'][0]})")
    if "B" in t:
        b = t["B"]
        lines.append(f"- B regression: {'pass' if b['pass'] else 'FAIL'} (expected prompt changes: {b.get('expected_prompt_changes')}; "
                     f"changed traces {len(b.get('changed', []))}/{b.get('n_keys')}; prompt chars {b.get('prompt_chars_base')} -> "
                     f"{b.get('prompt_chars_cand')}){' ' + b['error'][:300] if b.get('error') else ''}")
    if "C" in t:
        c = t["C"]
        runs = "; ".join(f"{w}: base {r.get('base', {}).get('parent_fit')} vs cand {r.get('cand', {}).get('parent_fit')}"
                         for w, r in c["runs"].items())
        lines.append(f"- C efficacy: {'pass' if c['pass'] else 'FAIL'} ({runs}; candidate cost ${c['candidate_cost_usd']}, "
                     f"{c['secs']}s, tolerance {c['tolerance']})")
    lines.append(f"- verdict: {'PASS' if rep['pass'] else 'FAIL'}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "run", "finish", "status"])
    ap.add_argument("pid", nargs="?")
    ap.add_argument("--tier-c", default="auto", choices=["auto", "on", "off"])
    a = ap.parse_args()
    if a.cmd == "prepare":
        wt = prepare(a.pid)
        print(f"worktree: {wt}\ntask file: {task_file(a.pid)}")
    elif a.cmd == "run":
        print(run_headless(a.pid))
        print(json.dumps(finish(a.pid, a.tier_c), indent=1, ensure_ascii=False))
    elif a.cmd == "finish":
        print(json.dumps(finish(a.pid, a.tier_c), indent=1, ensure_ascii=False))
    else:
        for p in Backlog().all():
            print(f"{p['id']} [{p['status']}] ({p['category']}, attempts {p['attempts']}) {p['title']}")
