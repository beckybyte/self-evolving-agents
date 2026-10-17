"""Minimal ReAct-style AppWorld baseline against a local OpenAI-compatible vLLM server.

Each turn the model writes one Python code block; it is run with world.execute(code) and the
printed output is fed back as the next user message. The episode stops when the agent calls
apis.supervisor.complete_task(...), after --max-steps turns, or when the token budget is spent.
Scoring uses AppWorld's official evaluator (world.evaluate()); TGC/SGC are aggregated with the
same rule as appworld.evaluator.Metric (TGC = mean task success, SGC = mean over scenarios of
min task success).

Every task runs in its own OS process: AppWorld freezes time globally and keeps in-process DB
caches, so tasks must not share a process.

Usage:
  APPWORLD_ROOT=$PWD env/bin/python run_baseline.py --split dev --model qwen3-30b-a3b \
      --base-url http://localhost:8101/v1 --workers 8 --exp qwen30b_dev
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("APPWORLD_ROOT", HERE)  # AppWorld root: holds data/ and experiments/

# ---------------------------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an autonomous agent that completes a task for your supervisor by \
writing Python code that calls app APIs. You operate inside an IPython-like REPL: each of your \
replies must contain exactly ONE ```python ... ``` code block. It will be executed and you will \
see its printed output (use print!). Variables persist across turns.

Available objects:
- `apis.<app_name>.<api_name>(**kwargs)` calls an app API and returns its JSON response.
- Discover APIs with:
    print(apis.api_docs.show_app_descriptions())
    print(apis.api_docs.show_api_descriptions(app_name='spotify'))
    print(apis.api_docs.show_api_doc(app_name='spotify', api_name='login'))
  Always read an API's doc before calling it for the first time.

Key rules:
1. NEVER invent API names or parameters. Before calling an API for the first time, print its doc
   with show_api_doc (in its own step) and use exactly the documented parameter names.
2. Supervisor information (name, email, phone) is in the task. Get account passwords with
   `apis.supervisor.show_account_passwords()` (a list of {account_name, password});
   login with `apis.<app>.login(username=<email, or phone_number for the phone app>, password=...)`
   and pass the returned `access_token` to later calls of that app.
3. Many list APIs are paginated with `page_index` (0, 1, 2, ...) and `page_limit` (max 20).
   Loop over page_index until an empty page is returned; never assume the first page is all.
4. Do not guess values; look them up through APIs. There is no human to answer questions.
5. Short code block per turn. ALWAYS print what you need to see; "Execution successful." with
   no output means nothing was printed, not that the task is done. Print only needed fields.
6. Verify your result before finishing. Then call
   `apis.supervisor.complete_task(answer=<answer>)` if the task asks a question (answer must be
   concise: just the value(s), e.g. "3" or "Song A, Song B"), otherwise
   `apis.supervisor.complete_task()` with no answer. This ends the episode.
7. Use the current date-time given below for relative dates ("yesterday", "this month").

Example of a good first two turns:
```python
print(apis.api_docs.show_api_descriptions(app_name='spotify'))
```
```python
print(apis.api_docs.show_api_doc(app_name='spotify', api_name='show_song_library'))
```
"""

TASK_TEMPLATE = """My name is {first_name} {last_name}. My personal email is {email} and phone \
number is {phone_number}.
Current date-time: {datetime}

Task: {instruction}"""

CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str | None:
    blocks = CODE_RE.findall(text)
    if blocks:
        return blocks[0].strip()
    return None


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + f"\n...[truncated {len(text) - limit} chars]...\n" + text[-tail:]


# ---------------------------------------------------------------------------------------------
# Policy interface: anything with reset(task_info) and act(observation) -> code
# ---------------------------------------------------------------------------------------------


class Policy(Protocol):
    def reset(self, task_info: dict[str, Any]) -> None: ...

    def act(self, observation: str | None) -> tuple[str | None, dict[str, Any]]:
        """Return (code_to_execute or None, info). info may hold 'prompt_tokens',
        'completion_tokens', 'raw' (model text)."""
        ...


class _LLMTimeout(Exception):
    pass


def _call_with_alarm(seconds: int, fn: Callable[..., Any], **kwargs: Any) -> Any:
    import signal

    def handler(signum: int, frame: Any) -> None:
        raise _LLMTimeout(f"LLM call exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        return fn(**kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@dataclass
class ReActLLMPolicy:
    model: str
    base_url: str
    max_tokens: int = 1536
    temperature: float = 0.0
    obs_char_limit: int = 3000
    ctx_tokens: int = 16384
    disable_thinking: bool = False
    llm_timeout: int = 180  # seconds, real time
    messages: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI(base_url=self.base_url, api_key="EMPTY", max_retries=0)
        self._last_prompt_tokens = 0

    def reset(self, task_info: dict[str, Any]) -> None:
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TASK_TEMPLATE.format(**task_info)},
        ]

    def _fit_context(self) -> list[dict[str, str]]:
        """Messages to send: system + task + the most recent turns that fit the budget
        (~2.8 chars/token, conservative for JSON). Full history stays in self.messages."""
        budget = int((self.ctx_tokens - self.max_tokens - 512) * 2.8)
        head, rest = self.messages[:2], self.messages[2:]
        used = sum(len(m["content"]) for m in head) + 100
        tail: list[dict[str, str]] = []
        for m in reversed(rest):
            if used + len(m["content"]) > budget:
                break
            tail.insert(0, m)
            used += len(m["content"])
        if not tail and rest:  # even the last message alone is too big: hard-truncate it
            m = rest[-1]
            tail = [{"role": m["role"], "content": truncate(m["content"], max(budget - used, 1000))}]
        if tail and tail[0]["role"] == "user":  # keep strict user/assistant alternation
            tail.insert(0, {"role": "assistant", "content": "[earlier turns omitted to save context]"})
        if len(tail) < len(rest) and tail and not tail[0]["content"].startswith("[earlier"):
            tail = [{"role": "assistant", "content": "[earlier turns omitted to save context]"},
                    {"role": "user", "content": "Continue."}] + tail
        return head + tail

    def act(self, observation: str | None) -> tuple[str | None, dict[str, Any]]:
        if observation is not None:
            self.messages.append({"role": "user", "content": "Output:\n" + truncate(observation, self.obs_char_limit)})
        to_send = self._fit_context()
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if self.disable_thinking else None
        # AppWorld freezes the clock (freezegun, no tick) for the whole process, so httpx/openai
        # timeouts never fire. Use a real-time SIGALRM watchdog instead, with one retry.
        resp = None
        for attempt in range(2):
            try:
                resp = _call_with_alarm(
                    self.llm_timeout,
                    self.client.chat.completions.create,
                    model=self.model,
                    messages=to_send,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    extra_body=extra,
                )
                break
            except _LLMTimeout:
                if attempt == 1:
                    raise
        text = resp.choices[0].message.content or ""
        self.messages.append({"role": "assistant", "content": text})
        usage = resp.usage
        info = {
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "raw": text,
        }
        code = extract_code(text)
        if code is None:
            # Feed back a format reminder as the "observation" of an empty step.
            self.messages.append({"role": "user", "content": "Your reply had no ```python``` code block. Reply with exactly one."})
        return code, info


# ---------------------------------------------------------------------------------------------
# Environment loop: run_task(task_id, policy) -> {score, tokens, trace}
# ---------------------------------------------------------------------------------------------


def run_task(
    task_id: str,
    policy: Policy,
    experiment_name: str,
    max_steps: int = 30,
    token_budget: int = 300_000,
) -> dict[str, Any]:
    from appworld import AppWorld

    trace: list[dict[str, Any]] = []
    prompt_tokens = completion_tokens = 0
    stop_reason = "max_steps"
    with AppWorld(task_id=task_id, experiment_name=experiment_name, timeout_seconds=60,
                  raise_on_extra_parameters=True) as world:
        sup = world.task.supervisor
        task_info = {
            "first_name": sup.first_name,
            "last_name": sup.last_name,
            "email": sup.email,
            "phone_number": sup.phone_number,
            "datetime": str(world.task.datetime),
            "instruction": world.task.instruction,
            "allowed_apps": list(world.task.allowed_apps),
        }
        policy.reset(task_info)
        observation: str | None = None
        steps = 0
        for steps in range(1, max_steps + 1):
            try:
                code, info = policy.act(observation)
            except Exception as e:  # LLM error (context overflow, server down...)
                stop_reason = f"llm_error: {type(e).__name__}: {str(e)[:200]}"
                break
            prompt_tokens += info.get("prompt_tokens", 0)
            completion_tokens += info.get("completion_tokens", 0)
            if code is None:
                observation = None  # policy already appended a format reminder
                trace.append({"step": steps, "raw": info.get("raw", ""), "code": None, "output": None})
            else:
                output = world.execute(code)
                trace.append({"step": steps, "raw": info.get("raw", ""), "code": code, "output": output})
                observation = output
            if world.task_completed():
                stop_reason = "completed"
                break
            if prompt_tokens + completion_tokens > token_budget:
                stop_reason = "token_budget"
                break
        tracker = world.evaluate()
        ev = tracker.to_dict(stats_only=True)
        ev["pass_count"] = tracker.pass_count
        ev["failed_tests"] = [str(f.get("requirement", f))[:300] for f in tracker.failures]
    return {
        "task_id": task_id,
        "score": 1.0 if ev["success"] else 0.0,  # strict task success (what TGC averages)
        "pass_count": ev.get("pass_count"),
        "num_tests": ev.get("num_tests"),
        "difficulty": ev.get("difficulty"),
        "failed_tests": ev.get("failed_tests"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens": prompt_tokens + completion_tokens,
        "steps": steps,
        "stop_reason": stop_reason,
        "instruction": task_info["instruction"],
        "trace": trace,
    }


# ---------------------------------------------------------------------------------------------
# Parallel driver: one process per task, hard timeout per task
# ---------------------------------------------------------------------------------------------


def _worker(task_id: str, args_dict: dict[str, Any], out_path: str) -> None:
    os.environ["APPWORLD_ROOT"] = args_dict["root"]
    policy = ReActLLMPolicy(
        model=args_dict["model"],
        base_url=args_dict["base_url"],
        disable_thinking=args_dict["disable_thinking"],
        temperature=args_dict["temperature"],
    )
    try:
        res = run_task(task_id, policy, args_dict["exp"], max_steps=args_dict["max_steps"],
                       token_budget=args_dict["token_budget"])
    except Exception as e:
        import traceback

        res = {"task_id": task_id, "score": 0.0, "error": f"{type(e).__name__}: {e}",
               "traceback": traceback.format_exc()[-3000:]}
    with open(out_path, "w") as f:
        json.dump(res, f, default=str, indent=1)


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    from appworld.task import task_id_to_generator_id  # scenario id = generator id

    by_scen: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_scen[task_id_to_generator_id(r["task_id"])].append(r.get("score", 0.0))
    n = len(results)
    ok = [r for r in results if "error" not in r]
    return {
        "n_tasks": n,
        "n_scenarios": len(by_scen),
        "TGC": round(100 * sum(r.get("score", 0.0) for r in results) / max(n, 1), 1),
        "SGC": round(100 * sum(min(v) for v in by_scen.values()) / max(len(by_scen), 1), 1),
        "avg_tokens": round(sum(r.get("tokens", 0) for r in ok) / max(len(ok), 1)),
        "avg_prompt_tokens": round(sum(r.get("prompt_tokens", 0) for r in ok) / max(len(ok), 1)),
        "avg_completion_tokens": round(sum(r.get("completion_tokens", 0) for r in ok) / max(len(ok), 1)),
        "avg_steps": round(sum(r.get("steps", 0) for r in ok) / max(len(ok), 1), 2),
        "stop_reasons": dict(
            sorted(
                {s: sum(1 for r in results if r.get("stop_reason", r.get("error", "?")[:40]) == s)
                 for s in {r.get("stop_reason", r.get("error", "?")[:40]) for r in results}}.items()
            )
        ),
        "n_errors": n - len(ok),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--limit", type=int, default=0, help="first N tasks of the split (0 = all)")
    ap.add_argument("--model", default="qwen3-30b-a3b")
    ap.add_argument("--base-url", default="http://localhost:8101/v1")
    ap.add_argument("--disable-thinking", action="store_true")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--token-budget", type=int, default=300_000)
    ap.add_argument("--task-timeout", type=int, default=1200, help="seconds, hard kill")
    ap.add_argument("--exp", required=True, help="experiment name (output dir)")
    args = ap.parse_args()

    root = os.environ.get("APPWORLD_ROOT", HERE)
    os.environ["APPWORLD_ROOT"] = root
    from appworld import load_task_ids

    task_ids = load_task_ids(args.split)
    if args.limit:
        task_ids = task_ids[: args.limit]
    out_dir = os.path.join(HERE, "results", args.exp)
    os.makedirs(out_dir, exist_ok=True)
    args_dict = {**vars(args), "root": root}

    ctx = mp.get_context("spawn")
    pending = [t for t in task_ids if not os.path.exists(os.path.join(out_dir, f"{t}.json"))]
    running: dict[str, tuple[Any, float]] = {}
    t_start = time.time()
    while pending or running:
        while pending and len(running) < args.workers:
            tid = pending.pop(0)
            p = ctx.Process(target=_worker, args=(tid, args_dict, os.path.join(out_dir, f"{tid}.json")))
            p.start()
            running[tid] = (p, time.time())
        time.sleep(1)
        for tid, (p, ts) in list(running.items()):
            if not p.is_alive():
                p.join()
                del running[tid]
                path = os.path.join(out_dir, f"{tid}.json")
                if not os.path.exists(path):
                    with open(path, "w") as f:
                        json.dump({"task_id": tid, "score": 0.0, "error": f"process exit {p.exitcode}"}, f)
                r = json.load(open(path))
                r["seconds"] = round(time.time() - ts, 1)  # wall clock (worker clock is frozen)
                json.dump(r, open(path, "w"), default=str, indent=1)
                print(f"[{time.time() - t_start:7.0f}s] {tid} score={r.get('score')} steps={r.get('steps')} "
                      f"tok={r.get('tokens')} {r.get('stop_reason', r.get('error', ''))[:60]}", flush=True)
            elif time.time() - ts > args.task_timeout:
                p.kill()
                p.join()
                del running[tid]
                with open(os.path.join(out_dir, f"{tid}.json"), "w") as f:
                    json.dump({"task_id": tid, "score": 0.0, "error": "timeout"}, f)
                print(f"[{time.time() - t_start:7.0f}s] {tid} TIMEOUT", flush=True)

    results = [json.load(open(os.path.join(out_dir, f"{t}.json"))) for t in task_ids]
    summary = aggregate(results)
    summary.update({"split": args.split, "model": args.model, "wall_clock_s": round(time.time() - t_start),
                    "max_steps": args.max_steps, "workers": args.workers})
    json.dump(summary, open(os.path.join(out_dir, "summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
