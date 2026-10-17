"""Protected surface (§8.4): paths a framework improver may not touch, checked mechanically on the branch diff."""
from __future__ import annotations

import fnmatch
import subprocess

PROTECTED = [
    "data/*", "data/**",                                  # evaluation data
    "src/calevo/verifier.py", "src/calevo/official_grader.py", "src/calevo/tasks.py",   # ground-truth scorers
    "meta/bench/*", "meta/bench/**",                       # meta-evaluation fixtures
    "src/sea/meta/*",                                      # the meta pipeline itself (backlog, protect, bench, trace, improve)
    "docs/framework_log.md",                               # written by the pipeline, not by the improver
]
EVALUATOR = ["src/sea/tasks/brief/judge.py", "src/sea/tasks/brief/framer.py"]   # only with category=evaluator + human approval


def changed_files(repo: str, base: str, branch: str) -> list[str]:
    p = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=repo, capture_output=True, text=True, check=True)
    return [l for l in p.stdout.splitlines() if l]


def violations(files: list[str], category: str) -> tuple[list[str], list[str]]:
    """-> (forbidden, needs_human). evaluator files are forbidden unless the proposal category is evaluator."""
    hit = lambda f, pats: any(fnmatch.fnmatch(f, p) for p in pats)
    forbidden = [f for f in files if hit(f, PROTECTED)]
    ev = [f for f in files if hit(f, EVALUATOR)]
    if category != "evaluator":
        return forbidden + ev, []
    return forbidden, ev
