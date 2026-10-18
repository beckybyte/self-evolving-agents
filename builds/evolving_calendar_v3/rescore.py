"""Re-score saved evolve_v3 apps with the current v3 harness, in place.

    python builds/evolving_calendar_v3/rescore.py [--runs runs] [--parallel 6] [--dirs evo_v3_a evo_v3_b ...]

For every <runs>/<dir>/evals/*/v0/r*/app: re-runs run_split for fit and holdout, rewrites r*/{split}/rows.jsonl and
report.json (scores, rows, fail_reasons, failed_checks replaced; team fields such as tokens, files, role_tokens,
team_secs kept), then rebuilds <eval>/{split}/report.json + rows.jsonl with BuildTask._merge. Runs whose team
produced no app.py keep their old rows. A tarball of the old reports is written first (runs/rescore_backup_*.tgz).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib.util
import json
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sea.tasks.base import summarize  # noqa: E402
from sea.tasks.build import BuildTask  # noqa: E402

_spec = importlib.util.spec_from_file_location("evolving_calendar_v3_harness_rescore", HERE / "harness.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

SPLITS = ("fit", "holdout")


def _rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()] if path.exists() else []


def _failed_checks(rows: list[dict]) -> dict:
    cf_: dict[str, int] = {}
    for r in rows:
        for c in r.get("checks") or []:
            if isinstance(c, dict) and not c.get("ok", c.get("pass", True)):
                n = str(c.get("name", c.get("id", "?")))
                cf_[n] = cf_.get(n, 0) + 1
    return dict(sorted(cf_.items(), key=lambda x: -x[1]))


def rescore_run(rdir: Path, timeout: float, workers: int) -> dict:
    """rdir = <eval>/v0/rK. Returns {split: (old_score, new_score)}."""
    out = {}
    for split in SPLITS:
        rep_p, rows_p = rdir / split / "report.json", rdir / split / "rows.jsonl"
        if not rep_p.exists():
            continue
        old = json.loads(rep_p.read_text())
        old_rows = _rows(rows_p)
        if "app.py" not in (old.get("files") or []) or not (rdir / "app" / "app.py").is_file():
            out[split] = (old.get("score"), old.get("score"))  # team made no app: nothing to re-score
            continue
        tokens = old_rows[0].get("tokens") if old_rows else old.get("tokens_per_sample", 0)
        rows = list(harness.run_split(rdir / "app", split, timeout=timeout, workers=workers))
        for r in rows:
            r.setdefault("detail", "")
            r.setdefault("hard", r.get("score", 0) >= 1.0)
            r["tokens"] = tokens
        with open(rows_p, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        new = dict(old)
        new.update(summarize(rows))
        new.update({"n": len(rows), "failed_checks": _failed_checks(rows)})
        rep_p.write_text(json.dumps(new, indent=1, ensure_ascii=False))
        out[split] = (old.get("score"), new.get("score"))
    return out


def merge_eval(edir: Path):
    """edir = <eval>/v0: rebuild merged reports exactly as BuildTask.evaluate_fit does."""
    ks = sorted(int(p.name[1:]) for p in edir.glob("r[0-9]*") if p.is_dir())
    for split in SPLITS:
        if not all((edir / f"r{k}" / split / "report.json").exists() for k in ks):
            continue
        reps = [json.loads((edir / f"r{k}" / split / "report.json").read_text()) for k in ks]
        BuildTask._merge(None, edir, split, reps)


def _merged_score(edir: Path, split: str):
    p = edir / split / "report.json"
    return json.loads(p.read_text()).get("score") if p.exists() else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", default=str(ROOT / "runs"))
    ap.add_argument("--dirs", nargs="*", default=["evo_v3_a", "evo_v3_b", "evo_v3_c"])
    ap.add_argument("--parallel", type=int, default=6, help="apps re-scored at the same time")
    ap.add_argument("--workers", type=int, default=3, help="harness scenarios in parallel per app")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args(argv)
    runs = Path(a.runs)
    edirs = sorted(e for d in a.dirs for e in (runs / d / "evals").glob("*/v0") if e.is_dir())
    rdirs = sorted(r for e in edirs for r in e.glob("r[0-9]*") if (r / "fit" / "report.json").exists())
    old = {e: {s: _merged_score(e, s) for s in SPLITS} for e in edirs}

    backup = runs / f"rescore_backup_{time.strftime('%Y%m%d_%H%M%S')}.tgz"
    with tarfile.open(backup, "w:gz") as tf:
        for e in edirs:
            for p in list(e.glob("*/report.json")) + list(e.glob("*/rows.jsonl")) \
                    + list(e.glob("r*/*/report.json")) + list(e.glob("r*/*/rows.jsonl")):
                tf.add(p, arcname=str(p.relative_to(runs)))
    print(f"backup: {backup}  ({len(edirs)} evals, {len(rdirs)} runs)", flush=True)

    t0 = time.time()
    with cf.ThreadPoolExecutor(max(1, a.parallel)) as ex:
        futs = {ex.submit(rescore_run, r, a.timeout, a.workers): r for r in rdirs}
        for i, f in enumerate(cf.as_completed(futs), 1):
            r = futs[f]
            try:
                res = f.result()
            except Exception as e:  # keep going; that run keeps whatever it had
                res = {"error": repr(e)[:200]}
            print(f"[{i}/{len(rdirs)} {time.time() - t0:.0f}s] {r.relative_to(runs)} {res}", flush=True)
    for e in edirs:
        merge_eval(e)

    print(f"\n{'eval':<40} {'fit old':>8} {'fit new':>8} {'hold old':>9} {'hold new':>9}")
    for e in edirs:
        new = {s: _merged_score(e, s) for s in SPLITS}
        f = lambda x: "-" if x is None else f"{x:.4f}"
        name = str(e.relative_to(runs).parent).replace("/evals", "")
        print(f"{name:<40} {f(old[e]['fit']):>8} {f(new['fit']):>8} {f(old[e]['holdout']):>9} {f(new['holdout']):>9}")


if __name__ == "__main__":
    main()
