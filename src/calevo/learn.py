"""Per-agent continual learning (fast loop). Every LLM role keeps its own notes in <genome>/memory/<role>.md.

One learning round:
  1. the team solves a batch from an *experience* split (never the fitness or test split),
  2. each LLM role is shown ITS OWN outputs on that batch (failures first, a few successes), the team's final
     result and the reference, and rewrites its own notes with the worker model itself (not the evolver),
  3. the updated genome (same structure, new memory) is written to a new directory.

The slow loop (calevo.evolve) can call learn_round() on children before evaluating them, so memory is inherited
(Lamarckian) and the evolver can read what each agent learned.

CLI (fixed structure, learning only):
  python -m calevo.learn --genome G --worker qwen3-8b --rounds 5 --batch 40 \
      --split math_train --eval-split math_dev --out runs/learn_math
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import shutil
import time
from pathlib import Path

from .runner import client, evaluate, load_genome, load_split, run_genome_on_sample

TASK_NAMES = {"math": "competition math problems (answer in \\boxed{})", "drop": "DROP reading comprehension",
              "calendar": "calendar scheduling"}
NOTE_RULES = ("At most 12 bullets and 900 characters. Lessons must be GENERAL (methods, checks, pitfalls of your step), "
              "never answers to specific problems. Keep notes that still help, fix or drop wrong ones.")
REFLECT = """You are the "{name}" agent in a team that solves {task}. Your role instructions:
---
{prompt}
---
Your current notes from experience:
{notes}

Below are recent cases: YOUR output in each, the team's final result and whether it was correct.
{cases}

Rewrite your notes so that next time YOUR step avoids these failures and keeps what worked. {rules}
Output ONLY the new notes as a bullet list."""


def _cases(rows, samples, name, k_fail=6, k_ok=3):
    fails = [r for r in rows if r["score"] < 1.0][:k_fail]
    oks = [r for r in rows if r["score"] >= 1.0][:k_ok]
    out = []
    for r in fails + oks:
        s = samples[r["id"]]
        ref = s["state"].get("answer") or s["state"].get("answers") or "(see constraints)"
        out.append(f"### case {r['id']} — team {'CORRECT' if r['score'] >= 1 else 'WRONG'} (reference: {str(ref)[:200]})\n"
                   f"problem: {s['messages'][-1]['content'][:600]}\n"
                   f"your output: {str(r['blackboard'].get(name, ''))[:1200]}\n"
                   f"team final answer (tail): {r['answer'][-300:]}")
    return "\n\n".join(out)


def learn_round(gdir_in, gdir_out, worker: str, split: str, start: int, batch: int, workers: int = 24) -> dict:
    gdir_in, gdir_out = Path(gdir_in), Path(gdir_out)
    if gdir_out.exists():
        shutil.rmtree(gdir_out)
    shutil.copytree(gdir_in, gdir_out, ignore=shutil.ignore_patterns("__pycache__"))
    g = load_genome(gdir_out)
    data = load_split(split)
    data = (data * 2)[start % len(data): start % len(data) + batch]
    samples = {s["id"]: s for s in data}
    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(lambda s: _safe(g, s, worker), data))
    task = TASK_NAMES.get(data[0].get("task", "calendar"), "the task")
    (gdir_out / "memory").mkdir(exist_ok=True)
    reflect_tokens, updated = 0, []
    for r in g["roles"]:
        if r.get("type", "llm") != "llm":
            continue
        name = r["name"]
        old = g["_memory"].get(name, "")
        prompt = REFLECT.format(name=name, task=task, prompt=r.get("prompt", "")[:1500] or "(see system prompt)",
                                notes=old or "(none yet)", cases=_cases(rows, samples, name), rules=NOTE_RULES)
        model = worker if r.get("model", "$WORKER") == "$WORKER" else r["model"]
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if model == "qwen3-8b" else {}
        resp = client(model).chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                                     temperature=0.3, max_tokens=700, extra_body=extra)
        notes = re.sub(r"<think>.*?</think>", "", resp.choices[0].message.content or "", flags=re.S).strip()[:1200]
        reflect_tokens += resp.usage.prompt_tokens + resp.usage.completion_tokens
        if notes:
            (gdir_out / "memory" / f"{name}.md").write_text(notes + "\n")
            updated.append(name)
    batch_score = sum(r["score"] for r in rows) / max(len(rows), 1)
    stat = {"batch_score": round(batch_score, 4), "batch_tokens": round(sum(r["tokens"] for r in rows) / max(len(rows), 1)),
            "reflect_tokens": reflect_tokens, "updated_roles": updated, "secs": round(time.time() - t0)}
    (gdir_out / "memory" / "_last_round.json").write_text(json.dumps(stat, indent=1))
    return stat


def _safe(g, s, worker):
    try:
        return run_genome_on_sample(g, s, worker)
    except Exception as e:
        return {"id": s["id"], "score": 0.0, "hard": False, "reason": "runtime_error", "detail": repr(e)[:300],
                "tokens": 0, "answer": "", "blackboard": {}}


def main(genome, worker, rounds, batch, split, eval_split, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cur = out / "r0"
    if not cur.exists():
        shutil.copytree(genome, cur, ignore=shutil.ignore_patterns("__pycache__"))
    curve = out / "curve.jsonl"
    done = {json.loads(l)["round"] for l in open(curve)} if curve.exists() else set()
    for k in range(rounds + 1):
        gk = out / f"r{k}"
        if k > 0 and not gk.exists():
            st = learn_round(out / f"r{k - 1}", gk, worker, split, (k - 1) * batch, batch)
        else:
            st = {}
        if k in done:
            continue
        rep = evaluate(gk, eval_split, worker, gk / "eval", workers=24)
        with open(curve, "a") as f:
            f.write(json.dumps({"round": k, "eval_score": rep["score"], "eval_tokens": rep["tokens_per_sample"], **st}) + "\n")
        print(k, rep["score"], st, flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--genome", required=True)
    ap.add_argument("--worker", default="qwen3-8b")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--split", default="math_train")
    ap.add_argument("--eval-split", default="math_dev")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    main(a.genome, a.worker, a.rounds, a.batch, a.split, a.eval_split, a.out)
