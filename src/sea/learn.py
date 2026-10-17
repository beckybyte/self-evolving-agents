"""Gated per-agent continual learning (fast loop). Every llm role owns memory/<role>.md.

One round:
  1. experience: the team works on an experience batch (benchmark: experience split; brief: the judge's critiques
     of the team's own design on fit scenarios),
  2. each llm role sees ITS OWN output + the outcome and proposes new notes (worker model, not the evolver),
  3. GATE: the genome with the candidate notes is evaluated on the holdout split against the genome with the old
     notes; candidates are kept only if they win by `margin` (score - lam*tokens), otherwise the old notes are
     restored and the rejected notes are logged to memory/_rejected.jsonl.
v1's ungated reflection made MATH worse (0.719 -> 0.661); the gate is the fix.

  python -m sea.learn --task bench:math --genome G --worker qwen3-8b --rounds 3 --batch 40 --out runs/learn_math
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .llm import chat, strip_think

NOTE_RULES = ("At most 10 bullets and 900 characters. Every bullet must name a CONCRETE error pattern you saw and the fix "
              "(no generic advice like 'be careful' or 'verify'). Never write answers to specific cases. Keep notes that "
              "still help, fix or drop wrong ones.")
REFLECT = """You are the "{name}" agent in a team. Your role instructions:
---
{prompt}
---
Your current notes from experience:
{notes}

Below is what happened recently: YOUR output and how the team's result was judged.
{cases}

Rewrite your notes so that next time YOUR step avoids these failures and keeps what worked. {rules}
Output ONLY the new notes as a bullet list."""


def _gate_value(rep: dict, lam_tokens: float) -> float:
    return rep["score"] - lam_tokens * rep["tokens_per_sample"] / 1000


def learn_round(task, gdir_in, gdir_out, worker: str, out, offset: int = 0, batch: int = 40, margin: float = 0.01,
                limit_holdout: int | None = None) -> dict:
    gdir_in, gdir_out, out = Path(gdir_in), Path(gdir_out), Path(out)
    if gdir_out.exists():
        shutil.rmtree(gdir_out)
    shutil.copytree(gdir_in, gdir_out, ignore=shutil.ignore_patterns("__pycache__"))
    g = task.load(gdir_in)
    t0 = time.time()
    rows = task.experience(gdir_in, worker, out / "experience", offset, batch)
    (gdir_out / "memory").mkdir(exist_ok=True)
    cand, reflect_tokens = {}, 0
    for r in g["roles"]:
        if r["kind"] != "llm":
            continue
        name = r["name"]
        prompt = REFLECT.format(name=name, prompt=r.get("prompt", "")[:1500] or "(see system prompt)",
                                notes=g["_memory"].get(name, "") or "(none yet)", cases=task.cases(rows, name), rules=NOTE_RULES)
        model = worker if r["model"] == "$WORKER" else r["model"]
        text, u = chat(model, [{"role": "user", "content": prompt}], max_tokens=700, temperature=0.3)
        reflect_tokens += u["prompt"] + u["completion"]
        notes = strip_think(text)[:1200]
        if notes:
            cand[name] = notes
            (gdir_out / "memory" / f"{name}.md").write_text(notes + "\n")
    stat = {"experience_score": round(sum(r["score"] for r in rows) / max(len(rows), 1), 4),
            "reflect_tokens": reflect_tokens, "candidates": sorted(cand)}
    if cand and task.holdout:
        # separate parent dirs: brief mode caches one team run per directory (old genome = the experience run)
        old = task.evaluate(gdir_in, task.holdout, worker, out / "experience" / "holdout", limit=limit_holdout)
        new = task.evaluate(gdir_out, task.holdout, worker, out / "gate_new" / "holdout", limit=limit_holdout)
        vo, vn = _gate_value(old, task.lam_tokens), _gate_value(new, task.lam_tokens)
        accepted = vn > vo + margin
        stat.update(gate_old=round(vo, 4), gate_new=round(vn, 4), accepted=accepted)
        if not accepted:
            with open(gdir_out / "memory" / "_rejected.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "notes": cand, "old": vo, "new": vn},
                                   ensure_ascii=False) + "\n")
            for name in cand:  # roll back to the old notes
                old_f = gdir_in / "memory" / f"{name}.md"
                tgt = gdir_out / "memory" / f"{name}.md"
                shutil.copy(old_f, tgt) if old_f.exists() else tgt.unlink()
    else:
        stat["accepted"] = bool(cand)  # no holdout configured: ungated (v1 behaviour)
    stat["secs"] = round(time.time() - t0)
    with open(gdir_out / "memory" / "_learn_log.jsonl", "a") as f:
        f.write(json.dumps(stat, ensure_ascii=False) + "\n")
    return stat


if __name__ == "__main__":
    import argparse

    from .tasks import make_task
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--genome", required=True)
    ap.add_argument("--worker", default="qwen3-8b")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--margin", type=float, default=0.01)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    task = make_task(a.task, out)
    cur = Path(a.genome)
    for k in range(1, a.rounds + 1):
        nxt = out / f"r{k}"
        st = learn_round(task, cur, nxt, a.worker, out / f"r{k}_work", (k - 1) * a.batch, a.batch, a.margin)
        print(k, json.dumps(st), flush=True)
        cur = nxt
