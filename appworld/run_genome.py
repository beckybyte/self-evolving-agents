"""Genome-driven AppWorld evaluator (schema: see README.md, section "Genome schema").

  APPWORLD_ROOT=$PWD env/bin/python run_genome.py --genome genomes/P0 --split train \
      --worker qwen3-30b-a3b --out runs/P0_train_30b [--workers 8] [--limit N]

Writes OUTDIR/rows.jsonl (one row per task), OUTDIR/report.json, OUTDIR/traces/<task>.json.
Every task runs in its own OS process (AppWorld freezes the clock and caches DBs per process).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("APPWORLD_ROOT", HERE)
sys.path.insert(0, HERE)

from run_baseline import TASK_TEMPLATE, _call_with_alarm, _LLMTimeout, truncate  # noqa: E402

ENDPOINTS = {
    "qwen3-8b": ("http://localhost:8100/v1", True),  # (base_url, disable_thinking)
    "qwen3-30b-a3b": ("http://localhost:8101/v1", False),
    "qwen2.5-72b": ("http://localhost:8102/v1", False),
}
CTX_TOKENS = 16384
CHARS_PER_TOKEN = 2.8  # conservative for JSON-heavy text
NO_CODE_MSG = "Your reply had no ```python``` code block. Reply with exactly one."
CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{(task|observation|history|blackboard|step)(?:\.([A-Za-z0-9_]+))?\}")

GENOME_DEFAULTS = {
    "max_steps": 30,
    "history_turns": 30,
    "token_budget": 300_000,
    "obs_char_limit": 3000,
    "parent": None,
    "notes": "",
}
ROLE_DEFAULTS = {"model": "$WORKER", "max_tokens": 1536, "temperature": 0.0, "input": "single",
                 "system": "", "prompt": ""}


# ---------------------------------------------------------------------------------------------
# Genome loading / validation
# ---------------------------------------------------------------------------------------------


def load_genome(gdir: str) -> dict[str, Any]:
    import yaml

    with open(os.path.join(gdir, "genome.yaml")) as f:
        g = yaml.safe_load(f)
    g = {**GENOME_DEFAULTS, **g}
    g["dir"] = os.path.abspath(gdir)
    names = set()
    roles = []
    for r in g.get("roles") or []:
        if r.get("type") == "llm":
            r = {**ROLE_DEFAULTS, **r}
            if r["input"] not in ("single", "chat"):
                raise ValueError(f"role {r['name']}: input must be single|chat")
        elif r.get("type") == "tool":
            if not r.get("tool"):
                raise ValueError(f"tool role {r.get('name')} needs `tool:`")
            path = os.path.join(g["dir"], r["tool"])
            if not os.path.exists(path):
                raise ValueError(f"tool file missing: {path}")
        else:
            raise ValueError(f"role {r.get('name')}: type must be llm|tool")
        if not r.get("name") or r["name"] in names:
            raise ValueError(f"role names must be unique and non-empty: {r.get('name')}")
        names.add(r["name"])
        roles.append(r)
    g["roles"] = roles
    act = g.get("act")
    if act not in names or next(r for r in roles if r["name"] == act)["type"] != "llm":
        raise ValueError("`act:` must name an llm role")
    return g


def load_tool(gdir: str, rel: str) -> Any:
    path = os.path.join(gdir, rel)
    spec = importlib.util.spec_from_file_location(f"genome_tool_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise ValueError(f"{rel} must define run(blackboard, ctx)")
    return mod


# ---------------------------------------------------------------------------------------------
# Read-only API docs helper given to tools as ctx["api_docs"]
# ---------------------------------------------------------------------------------------------


class ApiDocs:
    """Read-only view of AppWorld's API documentation (no task execution, no ground truth)."""

    def __init__(self, task: Any) -> None:
        self._docs = task.api_docs
        self._apps = dict(task.app_descriptions)

    def apps(self) -> dict[str, str]:
        """{app_name: description} -- same content as apis.api_docs.show_app_descriptions()."""
        return dict(self._apps)

    def api_list(self, app: str) -> list[dict[str, str]]:
        """[{name, description}] for one app; [] if unknown app."""
        if app not in self._docs:
            return []
        return [{"name": k, "description": v["description"]} for k, v in self._docs[app].items()]

    def api_doc(self, app: str, api: str) -> dict[str, Any] | None:
        """Full doc (parameters, response_schemas) of one API, or None if it does not exist."""
        try:
            return json.loads(json.dumps(self._docs[app][api], default=str))
        except KeyError:
            return None

    def search(self, query: str, k: int = 10) -> list[dict[str, str]]:
        """Naive keyword search over 'app.api: description'; returns top-k {app, api, description}."""
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        scored = []
        for app, apis in self._docs.items():
            for name, d in apis.items():
                text = f"{app} {name} {d['description']}".lower().replace("_", " ")
                s = sum(text.count(w) for w in words)
                if s:
                    scored.append((s, app, name, d["description"]))
        scored.sort(key=lambda x: -x[0])
        return [{"app": a, "api": n, "description": d} for _, a, n, d in scored[:k]]


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------


def render_history(history: list[dict[str, Any]], n: int, obs_limit: int) -> str:
    if n <= 0 or not history:
        return "(none)"
    parts = []
    for h in history[-n:]:
        if h["code"] is None:
            parts.append(f"## Step {h['step']}\n(no code block produced)")
        else:
            parts.append(f"## Step {h['step']}\n```python\n{h['code']}\n```\nOutput:\n"
                         f"{truncate(h['output'] or '', obs_limit)}")
    return "\n\n".join(parts)


def render_blackboard(bb: dict[str, str], key: str | None) -> str:
    if key is not None:
        return str(bb.get(key, ""))
    if not bb:
        return "(empty)"
    return "\n\n".join(f"[{k}]\n{v}" for k, v in bb.items())


def render(template: str, vals: dict[str, str], bb: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name, key = m.group(1), m.group(2)
        if name == "blackboard":
            return render_blackboard(bb, key)
        return vals[name]

    return PLACEHOLDER_RE.sub(sub, template)


def fit_single(text: str, max_tokens: int) -> str:
    budget = int((CTX_TOKENS - max_tokens - 512) * CHARS_PER_TOKEN)
    return truncate(text, budget)


def build_chat(system: str, first_user: str, history: list[dict[str, Any]], n: int,
               obs_limit: int, max_tokens: int) -> list[dict[str, str]]:
    """system + task + last n steps as (assistant raw, user Output) turns, trimmed to budget.
    Identical to run_baseline.ReActLLMPolicy for the default P0 genome."""
    msgs: list[dict[str, str]] = []
    for h in history[-n:] if n > 0 else []:
        msgs.append({"role": "assistant", "content": h["raw"]})
        if h["code"] is None:
            msgs.append({"role": "user", "content": NO_CODE_MSG})
        else:
            msgs.append({"role": "user", "content": "Output:\n" + truncate(h["output"] or "", obs_limit)})
    head = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": first_user}]
    budget = int((CTX_TOKENS - max_tokens - 512) * CHARS_PER_TOKEN)
    used = sum(len(m["content"]) for m in head) + 100
    tail: list[dict[str, str]] = []
    for m in reversed(msgs):
        if used + len(m["content"]) > budget:
            break
        tail.insert(0, m)
        used += len(m["content"])
    if not tail and msgs:
        m = msgs[-1]
        tail = [{"role": m["role"], "content": truncate(m["content"], max(budget - used, 1000))}]
    dropped = len(tail) < len(msgs) or (n > 0 and len(history) > n)
    if tail and tail[0]["role"] == "user":
        tail.insert(0, {"role": "assistant", "content": "[earlier turns omitted to save context]"})
    elif dropped and tail:
        tail = [{"role": "assistant", "content": "[earlier turns omitted to save context]"},
                {"role": "user", "content": "Continue."}] + tail
    return head + tail


# ---------------------------------------------------------------------------------------------
# One task
# ---------------------------------------------------------------------------------------------


class LLM:
    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}

    def call(self, model: str, messages: list[dict[str, str]], max_tokens: int,
             temperature: float, timeout: int = 180) -> tuple[str, int, int]:
        from openai import OpenAI

        base_url, no_think = ENDPOINTS[model]
        if model not in self.clients:
            self.clients[model] = OpenAI(base_url=base_url, api_key="EMPTY", max_retries=0)
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if no_think else None
        for attempt in range(2):
            try:
                resp = _call_with_alarm(timeout, self.clients[model].chat.completions.create,
                                        model=model, messages=messages, max_tokens=max_tokens,
                                        temperature=temperature, extra_body=extra)
                break
            except _LLMTimeout:
                if attempt == 1:
                    raise
        u = resp.usage
        return resp.choices[0].message.content or "", (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


def run_task(task_id: str, genome: dict[str, Any], worker: str, experiment_name: str) -> dict[str, Any]:
    from appworld import AppWorld

    llm = LLM()
    tools = {r["name"]: load_tool(genome["dir"], r["tool"]) for r in genome["roles"] if r["type"] == "tool"}
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    history: list[dict[str, Any]] = []
    role_log: list[dict[str, Any]] = []
    bb: dict[str, str] = {}
    stop_reason = "max_steps"
    obs_limit = genome["obs_char_limit"]
    n_hist = genome["history_turns"]
    step = 0
    with AppWorld(task_id=task_id, experiment_name=experiment_name, timeout_seconds=60,
                  raise_on_extra_parameters=True) as world:
        sup = world.task.supervisor
        task_text = TASK_TEMPLATE.format(
            first_name=sup.first_name, last_name=sup.last_name, email=sup.email,
            phone_number=sup.phone_number, datetime=str(world.task.datetime),
            instruction=world.task.instruction)
        api_docs = ApiDocs(world.task)
        observation = ""
        done = False
        for step in range(1, genome["max_steps"] + 1):
            vals = {
                "task": task_text,
                "observation": truncate(observation, obs_limit) if observation else "(none yet)",
                "history": render_history(history, n_hist, obs_limit),
                "step": str(step),
            }
            act_raw = None
            try:
                for r in genome["roles"]:
                    if r["type"] == "tool":
                        ctx = {
                            "instruction": world.task.instruction,
                            "task": task_text,
                            "supervisor": dict(sup),
                            "datetime": str(world.task.datetime),
                            "step": step,
                            "observation": observation,
                            "history": [dict(h) for h in history],
                            "api_docs": api_docs,
                        }
                        out = tools[r["name"]].run(dict(bb), ctx) or {}
                        bb[r["name"]] = str(out.get("text", ""))
                        role_log.append({"step": step, "role": r["name"], "out": bb[r["name"]][:2000]})
                        continue
                    model = worker if r["model"] == "$WORKER" else r["model"]
                    if r["input"] == "chat":
                        msgs = build_chat(render(r["system"], vals, bb), render(r["prompt"], vals, bb),
                                          history, n_hist, obs_limit, r["max_tokens"])
                    else:
                        msgs = []
                        if r["system"]:
                            msgs.append({"role": "system", "content": render(r["system"], vals, bb)})
                        msgs.append({"role": "user", "content": fit_single(render(r["prompt"], vals, bb), r["max_tokens"])})
                    text, pt, ct = llm.call(model, msgs, r["max_tokens"], r["temperature"])
                    usage["prompt"] += pt
                    usage["completion"] += ct
                    usage["calls"] += 1
                    bb[r["name"]] = text
                    if r["name"] == genome["act"]:
                        act_raw = text
                    else:
                        role_log.append({"step": step, "role": r["name"], "out": text[:2000]})
            except _LLMTimeout as e:
                stop_reason = f"llm_timeout"
                break
            except Exception as e:  # LLM 400 (context), tool crash, ...
                stop_reason = f"role_error"
                role_log.append({"step": step, "error": f"{type(e).__name__}: {str(e)[:500]}"})
                break
            blocks = CODE_RE.findall(act_raw or "")
            code = blocks[-1].strip() if blocks else None
            output = world.execute(code) if code is not None else None
            history.append({"step": step, "raw": act_raw or "", "code": code, "output": output})
            observation = output if output is not None else NO_CODE_MSG
            if world.task_completed():
                stop_reason = "completed"
                done = True
                break
            if usage["prompt"] + usage["completion"] > genome["token_budget"]:
                stop_reason = "token_budget"
                break
        tracker = world.evaluate()
        failed = [str(f.get("requirement", f)) for f in tracker.failures]
        pass_count, num_tests = tracker.pass_count, tracker.num_tests
        difficulty = tracker.difficulty
    return {
        "task_id": task_id, "pass_count": pass_count, "num_tests": num_tests, "difficulty": difficulty,
        "failed_tests": failed, "stop_reason": stop_reason, "steps": step, "usage": usage,
        "history": history, "role_log": role_log, "blackboard": bb, "completed": done,
    }


def trace_summary(res: dict[str, Any]) -> str:
    parts = []
    for h in res.get("history", []):
        parts.append(f"[{h['step']}] code:\n{truncate(h['code'] or '(none)', 600)}\n"
                     f"-> {truncate(h['output'] or '', 300)}")
    parts.append(f"STOP: {res.get('stop_reason')} | failed tests: {res.get('failed_tests')}")
    return "\n".join(parts)[-3000:]


def short_test(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()[:80]


def to_row(task_id: str, res: dict[str, Any]) -> dict[str, Any]:
    if "error" in res:
        return {"id": task_id, "score": 0.0, "hard": False, "reason": res["error"].split(":")[0],
                "detail": res["error"][:200], "soft": None, "tokens": 0,
                "usage": {"prompt": 0, "completion": 0, "calls": 0}, "answer": "", "blackboard": {}}
    n = res["num_tests"] or 1
    hard = res["pass_count"] == res["num_tests"]
    if hard:
        reason, detail = "ok", "ok"
    else:
        first = short_test(res["failed_tests"][0]) if res["failed_tests"] else "unknown"
        reason = first if res["stop_reason"] == "completed" else res["stop_reason"]
        detail = first
    u = res["usage"]
    return {"id": task_id, "score": round(res["pass_count"] / n, 4), "hard": hard, "reason": reason,
            "detail": detail, "soft": None, "tokens": u["prompt"] + u["completion"], "usage": u,
            "answer": trace_summary(res),
            "blackboard": {k: v[-2000:] for k, v in res.get("blackboard", {}).items()},
            "steps": res["steps"], "stop_reason": res["stop_reason"], "difficulty": res["difficulty"]}


# ---------------------------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------------------------


def _worker(task_id: str, gdir: str, worker: str, exp: str, out_path: str) -> None:
    try:
        genome = load_genome(gdir)
        res = run_task(task_id, genome, worker, exp)
    except Exception as e:
        res = {"task_id": task_id, "error": f"crash: {type(e).__name__}: {e}",
               "traceback": traceback.format_exc()[-3000:]}
    with open(out_path, "w") as f:
        json.dump(res, f, default=str)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genome", required=True)
    ap.add_argument("--split", required=True, choices=["train", "dev", "test_normal", "test_challenge"])
    ap.add_argument("--worker", required=True, choices=sorted(ENDPOINTS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--task-timeout", type=int, default=1200)
    ap.add_argument("--resume", action="store_true",
                    help="keep finished traces in OUT/traces (rerun only missing/errored tasks)")
    args = ap.parse_args()

    genome = load_genome(args.genome)  # validate before spawning
    from appworld import load_task_ids
    from appworld.task import task_id_to_generator_id

    task_ids = load_task_ids(args.split)
    if args.limit:
        task_ids = task_ids[: args.limit]
    out = os.path.abspath(args.out)
    tdir = os.path.join(out, "traces")
    os.makedirs(tdir, exist_ok=True)
    exp = f"g_{genome['id']}_{args.split}_{args.worker}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"

    ctx = mp.get_context("spawn")
    def finished(t: str) -> bool:
        path = os.path.join(tdir, f"{t}.json")
        if not (args.resume and os.path.exists(path)):
            return False
        return "error" not in json.load(open(path))

    pending = [t for t in task_ids if not finished(t)]
    running: dict[str, tuple[Any, float]] = {}
    t0 = time.time()
    while pending or running:
        while pending and len(running) < args.workers:
            tid = pending.pop(0)
            p = ctx.Process(target=_worker, args=(tid, genome["dir"], args.worker, exp, os.path.join(tdir, f"{tid}.json")))
            p.start()
            running[tid] = (p, time.time())
        time.sleep(0.5)
        for tid, (p, ts) in list(running.items()):
            path = os.path.join(tdir, f"{tid}.json")
            if not p.is_alive():
                p.join()
                del running[tid]
                if not os.path.exists(path):
                    json.dump({"task_id": tid, "error": f"crash: exit {p.exitcode}"}, open(path, "w"))
            elif time.time() - ts > args.task_timeout:
                p.kill()
                p.join()
                del running[tid]
                json.dump({"task_id": tid, "error": "timeout: task wall clock"}, open(path, "w"))

    rows = [to_row(t, json.load(open(os.path.join(tdir, f"{t}.json")))) for t in task_ids]
    with open(os.path.join(out, "rows.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    by_scen: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_scen[task_id_to_generator_id(r["id"])].append(r["hard"])
    fails = [r for r in rows if not r["hard"]]
    n = len(rows)
    report = {
        "score": round(sum(r["score"] for r in rows) / n, 4),
        "hard_acc": round(sum(r["hard"] for r in rows) / n, 4),
        "soft_mean": None,
        "tokens_per_sample": round(sum(r["tokens"] for r in rows) / n, 1),
        "fail_reasons": dict(Counter(r["reason"] for r in fails).most_common()),
        "fail_detail": dict(Counter(r["detail"] for r in fails).most_common()),
        "sgc": round(sum(all(v) for v in by_scen.values()) / len(by_scen), 4),
        "genome": genome["id"], "split": args.split, "worker": args.worker,
        "experiment_name": exp, "n": n, "secs": round(time.time() - t0, 1),
    }
    json.dump(report, open(os.path.join(out, "report.json"), "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
