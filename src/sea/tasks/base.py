"""Task plugin protocol. A Task owns: where episodes come from, how a run is scored, what the evolver may see,
and (brief mode) how the evaluation itself is revised when the human answers.

Splits: "fit" (fitness; the evolver sees reports + failures), "holdout" (gating only: memory updates; never shown
to the evolver), "test" (final reports only). Split names may be sliced: "math_train@0:300".
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
from collections import Counter
from pathlib import Path

from ..executor import run_team
from ..fitness import FitnessSpec
from ..genome import check_tool_safety, load_genome
from ..human import HumanChannel

ROOT_DIR = Path(__file__).resolve().parents[3]
GENOME_DOC = (Path(__file__).resolve().parents[1] / "genome.py").read_text().split('"""')[1]


def parse_split(spec: str) -> tuple[str, int | None, int | None]:
    name, _, rng = spec.partition("@")
    if not rng:
        return name, None, None
    a, _, b = rng.partition(":")
    return name, int(a) if a else None, int(b) if b else None


class Task:
    name = "task"
    context_keys: tuple[str, ...] = ()

    def __init__(self, fit: dict[str, float], holdout: str | None = None, test: list[str] | None = None,
                 experience: str | None = None, lam_tokens: float = 0.02, human: HumanChannel | None = None):
        self.fit, self.holdout, self.test, self.experience_split = fit, holdout, test or [], experience
        self.lam_tokens, self.human = lam_tokens, human

    # ---------------------------------------------------------------- to override
    def describe(self) -> str:
        raise NotImplementedError

    def schema_doc(self, allow_tools: bool = True) -> str:
        return GENOME_DOC + ("" if allow_tools else "\nTool roles are NOT allowed in this run.\n")

    def episodes(self, split: str) -> list[dict]:
        raise NotImplementedError

    def score(self, episode: dict, result: dict) -> dict:
        raise NotImplementedError

    def workspace_files(self, parent_eval: Path) -> dict[str, str]:
        """Files (name -> text) put into the evolver workspace. Fit-split material only."""
        return {"reports.json": json.dumps(self.fit_reports(parent_eval), indent=1)}

    def refresh(self) -> bool:
        """Called at generation boundaries. True = the evaluation changed (parent must be re-evaluated)."""
        return False

    def version(self) -> str:
        return ""

    def moves(self) -> str:
        return ("hire (add a role), fire/merge (remove redundant roles), rewrite a role's prompt, change the "
                "order/topology (sequence, critique loop, message passing), add or improve tool code (tools/*.py).")

    # ---------------------------------------------------------------- shared machinery
    def fitness_spec(self) -> FitnessSpec:
        return FitnessSpec.weighted_scores(self.fit, self.lam_tokens)

    def load_child(self, gdir: Path) -> dict:
        g = load_genome(gdir, self.context_keys)
        check_tool_safety(gdir)
        return g

    def load(self, gdir) -> dict:
        return load_genome(gdir, self.context_keys)

    def run_one(self, g: dict, ep: dict, worker: str) -> dict:
        try:
            res = run_team(g, ep, worker, self.human)
        except Exception as e:  # a crashing genome scores 0 on that episode
            return {"id": ep["id"], "score": 0.0, "hard": False, "reason": "runtime_error", "detail": repr(e)[:500],
                    "soft": None, "tokens": 0, "usage": {}, "answer": "", "blackboard": {}, "artifacts": {}}
        v = self.score(ep, res)
        return {"id": ep["id"], **v, "tokens": res["tokens"], "usage": res["usage"], "answer": str(res["answer"])[-3000:],
                "blackboard": {k: str(x)[:1500] for k, x in res["blackboard"].items()},
                "artifacts": {k: str(x)[:6000] for k, x in res["artifacts"].items()},
                "questions": res["questions"], "assumptions": res["assumptions"]}

    def evaluate(self, gdir, split: str, worker: str, out, workers: int = 24, limit: int | None = None) -> dict:
        g = self.load(gdir)
        eps = self.episodes(split)[:limit]
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        with cf.ThreadPoolExecutor(max(1, min(workers, len(eps)))) as ex:
            rows = list(ex.map(lambda e: self.run_one(g, e, worker), eps))
        with open(out / "rows.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rep = summarize(rows)
        rep.update({"genome": g.get("id"), "split": split, "worker": worker, "n": len(rows), "secs": round(time.time() - t0, 1),
                    "eval_version": self.version()})
        (out / "report.json").write_text(json.dumps(rep, indent=1))
        return rep

    def evaluate_fit(self, gdir, worker: str, out, splits: list[str] | None = None, workers: int = 24) -> dict:
        out = Path(out)
        reps = {}
        for s in splits or list(self.fit):
            f = out / s / "report.json"
            reps[s] = json.loads(f.read_text()) if f.exists() else self.evaluate(gdir, s, worker, out / s, workers)
        metrics = {f"score:{s}": r["score"] for s, r in reps.items()}
        metrics["tokens"] = sum(r["tokens_per_sample"] for r in reps.values()) / max(len(reps), 1)
        for r in reps.values():
            for k, v in (r.get("metrics") or {}).items():
                metrics[k] = v
        return {"reports": reps, "metrics": metrics, "fitness": self.fitness_spec()(metrics)}

    def experience(self, gdir, worker: str, out: Path, offset: int = 0, batch: int = 40) -> list[dict]:
        """Rows the team produced on a batch of the experience split (disjoint from fit/holdout/test)."""
        name, a, b = parse_split(self.experience_split)
        n = len(self.episodes(self.experience_split))
        start = (a or 0) + (offset % max(n, 1))
        end = min(start + batch, b) if b is not None else start + batch
        self.evaluate(gdir, f"{name}@{start}:{end}", worker, Path(out) / "exp")
        return [json.loads(l) for l in open(Path(out) / "exp" / "rows.jsonl")]

    def fit_reports(self, ev_dir: Path) -> dict:
        return {s: json.loads((Path(ev_dir) / s / "report.json").read_text()) for s in self.fit}

    def summarize_child(self, ev: dict) -> str:
        reps = ev["reports"]
        first = next(iter(reps.values()))
        return (" ".join(f"{s} {r['score']}" for s, r in reps.items())
                + f" tokens {first['tokens_per_sample']}; fails: {first['fail_reasons']}")

    def cases(self, rows: list[dict], role: str, k_fail: int = 6, k_ok: int = 3) -> str:
        """Experience shown to one role when it rewrites its notes (calevo.learn format)."""
        eps = {e["id"]: e for s in [self.experience_split] if s for e in self.episodes(s)}
        out = []
        for r in [r for r in rows if r["score"] < 1.0][:k_fail] + [r for r in rows if r["score"] >= 1.0][:k_ok]:
            s = eps.get(r["id"], {}).get("sample", {})
            ref = (s.get("state") or {}).get("answer") or (s.get("state") or {}).get("answers") or "(see constraints)"
            prob = (s.get("messages") or [{"content": ""}])[-1]["content"]
            out.append(f"### case {r['id']} — team {'CORRECT' if r['score'] >= 1 else 'WRONG'} (reference: {str(ref)[:200]})\n"
                       f"problem: {prob[:600]}\nyour output: {str(r['blackboard'].get(role, ''))[:1200]}\n"
                       f"team final answer (tail): {r['answer'][-300:]}")
        return "\n\n".join(out)


def summarize(rows: list[dict]) -> dict:
    n = max(len(rows), 1)
    soft = [r["soft"] for r in rows if r.get("soft") is not None]
    return {
        "score": round(sum(r["score"] for r in rows) / n, 4),
        "hard_acc": round(sum(bool(r.get("hard")) for r in rows) / n, 4),
        "soft_mean": round(sum(soft) / len(soft), 4) if soft else None,
        "tokens_per_sample": round(sum(r["tokens"] for r in rows) / n, 1),
        "fail_reasons": dict(Counter(r["reason"] for r in rows if not r.get("hard"))),
        "fail_detail": dict(Counter(str(r.get("detail")) for r in rows if not r.get("hard"))),
    }
