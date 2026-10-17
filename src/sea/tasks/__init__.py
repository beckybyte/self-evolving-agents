"""Task registry. Spec strings:
  bench:calendar | bench:math | bench:drop     ground-truth benchmarks (regression)
  appworld                                     AppWorld (external env loop)
  brief:<path/to/brief.md>                     open-ended product brief (self-built evaluation)
  build:<spec dir>                             team writes a runnable app; frozen harness drives it (contract.md,
                                               harness.py, scenarios.py in <spec dir>)
"""
from __future__ import annotations

from pathlib import Path

from ..human import HumanChannel


def parse_weights(s: str | None) -> dict[str, float] | None:
    if not s:
        return None
    return {k: float(v) for k, v in (x.rsplit(":", 1) for x in s.split(","))}


def make_task(spec: str, run_dir: str | Path, fit: str | None = None, holdout: str | None = None,
              lam_tokens: float | None = None, **opts):
    """lam_tokens=None: the task's default (0.001 for build, 0.02 otherwise)."""
    run_dir = Path(run_dir)
    if spec.startswith("build:"):
        from .build import BuildTask
        return BuildTask(spec[6:], human=HumanChannel(run_dir / "inbox"), lam_tokens=0.001 if lam_tokens is None else lam_tokens)
    lam_tokens = 0.02 if lam_tokens is None else lam_tokens
    human = HumanChannel(run_dir / "inbox")
    if spec.startswith("bench:"):
        from .benchmark import BenchmarkTask
        return BenchmarkTask(spec[6:], fit=parse_weights(fit), holdout=holdout, lam_tokens=lam_tokens, human=human)
    if spec == "appworld":
        from .appworld import AppWorldTask
        return AppWorldTask(fit=parse_weights(fit), lam_tokens=lam_tokens, human=human)
    if spec.startswith("brief:"):
        from .brief.task import BriefTask
        return BriefTask(spec[6:], run_dir, lam_tokens=lam_tokens, human=human, **opts)
    raise ValueError(f"unknown task spec {spec!r}")
