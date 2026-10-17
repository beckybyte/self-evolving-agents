"""L0 runner: execute a genome on a data split, verify, count tokens. Frozen during evolution.

Genome = directory with genome.yaml (+ optional tools/*.py). Schema (see genomes/P0/genome.yaml):

  id: str
  parent: str | null
  soul: str                       # shared system prompt for every LLM role (may be "")
  roles:                          # executed in order; each writes blackboard[name]
    - name: str
      type: llm | tool
      model: qwen3-8b | qwen3-30b-a3b | qwen2.5-72b      (llm only; "$WORKER" = run-time worker model)
      input: native | transcript                         (llm only)
         native     -> the original chat messages; soul + prompt appended to the system message
         transcript -> one user message: prompt with {conversation} and {blackboard} filled in
      prompt: str                                        (llm only)
      max_tokens: int = 2048                             (llm only)
      tool: tools/<file>.py                              (tool only; must define run(blackboard, messages) -> dict)
  final: str                      # blackboard key whose text is the answer
  notes: str                      # why this genome differs from its parent
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import yaml
from openai import OpenAI

from .tasks import score as score_sample

ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS = {
    "qwen3-8b": "http://localhost:8100/v1",
    "qwen3-30b-a3b": "http://localhost:8101/v1",
    "qwen2.5-72b": "http://localhost:8102/v1",
}
ALLOWED_MODELS = set(ENDPOINTS) | {"$WORKER"}
_clients: dict[str, OpenAI] = {}


def client(model: str) -> OpenAI:
    if model not in _clients:
        _clients[model] = OpenAI(base_url=ENDPOINTS[model], api_key="x", timeout=300, max_retries=2)
    return _clients[model]


# ---------------------------------------------------------------- data
def load_split(name: str) -> list[dict]:
    """dev_in / test_in / ood_dev / ood_test -> list of {"id", "messages", "state"}."""
    path = ROOT / "data" / "splits" / f"{name}.jsonl"
    return [json.loads(l) for l in open(path)]


def make_in_splits(seed: int = 0, n_dev: int = 100):
    out = ROOT / "data" / "splits"
    out.mkdir(parents=True, exist_ok=True)
    train = [json.loads(l) for l in open(ROOT / "data" / "train.jsonl")]
    val = [json.loads(l) for l in open(ROOT / "data" / "validation.jsonl")]
    rng = random.Random(seed)
    idx = list(range(len(train)))
    rng.shuffle(idx)

    def row(split, i, r):
        return {"id": f"{split}-{i}", "messages": r["responses_create_params"]["input"], "state": r["exp_cal_state"]}

    with open(out / "dev_in.jsonl", "w") as f:
        for i in idx[:n_dev]:
            f.write(json.dumps(row("train", i, train[i])) + "\n")
    with open(out / "test_in.jsonl", "w") as f:
        for i, r in enumerate(val):
            f.write(json.dumps(row("val", i, r)) + "\n")
    # reserve the next train indices for OOD-dev construction
    (out / "ood_dev_source_idx.json").write_text(json.dumps(idx[n_dev : n_dev + 80]))


# ---------------------------------------------------------------- genome
def load_genome(gdir: str | Path) -> dict:
    gdir = Path(gdir)
    g = yaml.safe_load(open(gdir / "genome.yaml"))
    g["_dir"] = str(gdir)
    validate_genome(g)
    return g


def validate_genome(g: dict):
    assert isinstance(g.get("roles"), list) and g["roles"], "roles must be a non-empty list"
    names = set()
    for r in g["roles"]:
        assert r.get("name") and r["name"] not in names, f"bad/duplicate role name {r.get('name')}"
        names.add(r["name"])
        if r.get("type", "llm") == "llm":
            assert r.get("model", "$WORKER") in ALLOWED_MODELS, f"unknown model {r.get('model')}"
            assert r.get("input", "native") in ("native", "transcript")
            assert isinstance(r.get("prompt", ""), str)
        else:
            assert r["type"] == "tool" and r.get("tool"), "tool role needs tool:"
            assert (Path(g["_dir"]) / r["tool"]).exists(), f"missing tool file {r['tool']}"
    assert g.get("final") in names, "final must name a role"


def render_transcript(messages: list[dict]) -> str:
    return "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages)


def run_tool(gdir: str, tool: str, blackboard: dict, messages: list[dict]) -> dict:
    """Tools run in a subprocess (they may be LLM-written)."""
    code = (
        "import json,sys,importlib.util\n"
        f"spec=importlib.util.spec_from_file_location('t',{str(Path(gdir) / tool)!r})\n"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        "d=json.load(sys.stdin);print(json.dumps(m.run(d['bb'],d['messages'])))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-c", code], input=json.dumps({"bb": blackboard, "messages": messages}),
                       capture_output=True, text=True, timeout=60, env=env)
    if p.returncode != 0:
        return {"error": p.stderr[-800:]}
    return json.loads(p.stdout.strip().splitlines()[-1])


def run_genome_on_sample(g: dict, sample: dict, worker: str) -> dict:
    bb: dict = {}
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    msgs = sample["messages"]
    for r in g["roles"]:
        if r.get("type", "llm") == "tool":
            out = run_tool(g["_dir"], r["tool"], bb, msgs)
            bb[r["name"]] = out.get("text", json.dumps(out)) if isinstance(out, dict) else str(out)
            if isinstance(out, dict):
                bb[r["name"] + "_data"] = out
            continue
        model = worker if r.get("model", "$WORKER") == "$WORKER" else r["model"]
        sys_extra = "\n\n".join(x for x in [g.get("soul", ""), r.get("prompt", "")] if x)
        if r.get("input", "native") == "native":
            chat = [dict(m) for m in msgs]
            if chat and chat[0]["role"] == "system":
                chat[0]["content"] = chat[0]["content"] + ("\n\n" + sys_extra if sys_extra else "")
            elif sys_extra:
                chat.insert(0, {"role": "system", "content": sys_extra})
        else:
            prompt = r.get("prompt", "").replace("{conversation}", render_transcript(msgs)).replace(
                "{blackboard}", json.dumps({k: v for k, v in bb.items() if not k.endswith("_data")}, ensure_ascii=False))
            chat = ([{"role": "system", "content": g["soul"]}] if g.get("soul") else []) + [{"role": "user", "content": prompt}]
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if model == "qwen3-8b" else {}
        resp = client(model).chat.completions.create(
            model=model, messages=chat, temperature=0.0, max_tokens=int(r.get("max_tokens", 2048)), extra_body=extra)
        bb[r["name"]] = resp.choices[0].message.content or ""
        usage["prompt"] += resp.usage.prompt_tokens
        usage["completion"] += resp.usage.completion_tokens
        usage["calls"] += 1
    answer = bb.get(g["final"], "")
    v = score_sample(sample, answer)
    return {"id": sample["id"], **v, "tokens": usage["prompt"] + usage["completion"], "usage": usage,
            "answer": answer[-3000:], "blackboard": {k: (str(x)[:1500]) for k, x in bb.items()}}


def evaluate_appworld(gdir, split: str, worker: str, out_dir, workers: int = 8, limit: int | None = None) -> dict:
    """AppWorld runs in its own env (appworld/env) via appworld/run_genome.py; same report format."""
    aw = ROOT / "appworld"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(aw / "env" / "bin" / "python"), str(aw / "run_genome.py"), "--genome", str(Path(gdir).resolve()),
           "--split", split.removeprefix("appworld_"), "--worker", worker, "--out", str(out_dir.resolve()),
           "--workers", str(workers)] + (["--limit", str(limit)] if limit else [])
    env = {**os.environ, "APPWORLD_ROOT": str(aw)}
    p = subprocess.run(cmd, cwd=aw, env=env, capture_output=True, text=True, timeout=4 * 3600)
    (out_dir / "run.log").write_text(p.stdout[-20000:] + "\n--- stderr ---\n" + p.stderr[-20000:])
    if p.returncode != 0 or not (out_dir / "report.json").exists():
        raise RuntimeError(f"appworld eval failed ({p.returncode}): {p.stderr[-800:]}")
    return json.loads((out_dir / "report.json").read_text())


def evaluate(gdir: str | Path, split: str, worker: str, out_dir: str | Path, workers: int = 32, limit: int | None = None) -> dict:
    if split.startswith("appworld_"):
        return evaluate_appworld(gdir, split, worker, out_dir, min(workers, 8), limit)
    g = load_genome(gdir)
    data = load_split(split)[:limit]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rows = []

    def one(s):
        try:
            return run_genome_on_sample(g, s, worker)
        except Exception as e:  # a crashing genome scores 0 on that sample
            return {"id": s["id"], "score": 0.0, "hard": False, "reason": "runtime_error", "detail": repr(e)[:500],
                    "soft": None, "tokens": 0, "usage": {}, "answer": "", "blackboard": {}}

    with cf.ThreadPoolExecutor(workers) as ex:
        for r in ex.map(one, data):
            rows.append(r)
    with open(out_dir / "rows.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    rep = summarize(rows)
    rep.update({"genome": g.get("id"), "split": split, "worker": worker, "n": len(rows), "secs": round(time.time() - t0, 1)})
    (out_dir / "report.json").write_text(json.dumps(rep, indent=1))
    return rep


def summarize(rows: list[dict]) -> dict:
    from collections import Counter
    n = max(len(rows), 1)
    soft = [r["soft"] for r in rows if r.get("soft") is not None]
    return {
        "score": round(sum(r["score"] for r in rows) / n, 4),
        "hard_acc": round(sum(bool(r["hard"]) for r in rows) / n, 4),
        "soft_mean": round(sum(soft) / len(soft), 4) if soft else None,
        "tokens_per_sample": round(sum(r["tokens"] for r in rows) / n, 1),
        "fail_reasons": dict(Counter(r["reason"] for r in rows if not r["hard"])),
        "fail_detail": dict(Counter(r.get("detail") for r in rows if not r["hard"])),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--genome", required=True)
    ap.add_argument("--split", default="dev_in")
    ap.add_argument("--worker", default="qwen3-30b-a3b")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=32)
    a = ap.parse_args()
    print(json.dumps(evaluate(a.genome, a.split, a.worker, a.out, a.workers, a.limit), indent=1))
