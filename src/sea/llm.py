"""LLM backends behind one call: chat(model, messages, ...) -> (text, usage).

  qwen3-8b | qwen3-30b-a3b | qwen2.5-72b   OpenAI-compatible vLLM servers (ENDPOINTS)
  claude:<alias>                           headless `claude -p` (no tools, empty cwd), e.g. claude:sonnet
  crusoe:<model id>                        Crusoe Managed Inference (OpenAI-compatible), e.g.
                                           crusoe:nvidia/NVIDIA-Nemotron-3.5-Super-VL. Key: $CRUSOE_API / $CRUSOE_API_KEY
                                           (or the `export CRUSOE_API=` line in ~/.zshrc). Reasoning models spend
                                           thousands of tokens thinking first, so max_tokens gets a floor.
                                           Suffix `#nothink` (crusoe:<id>#nothink) turns thinking off via
                                           chat_template_kwargs.enable_thinking=false (~4x faster; the only switch
                                           Crusoe honours — /no_think and reasoning_effort do not).
  fake:<name>                              in-process function registered with register_fake() (tests, dry runs)
usage = {"prompt": int, "completion": int, "calls": 1}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import random
import threading
import time
from pathlib import Path
from typing import Callable

ENDPOINTS = {
    "qwen3-8b": "http://localhost:8100/v1",
    "qwen3-30b-a3b": "http://localhost:8101/v1",
    "qwen2.5-72b": "http://localhost:8102/v1",
}
NO_THINKING = {"qwen3-8b"}
_clients: dict = {}
_lock = threading.Lock()
_fakes: dict[str, Callable[[list[dict]], str]] = {}


def register_fake(name: str, fn: Callable[[list[dict]], str]):
    _fakes[name] = fn


def known_model(model: str) -> bool:
    return (model in ENDPOINTS or model.startswith(("claude:", "fake:", "crusoe:")) or model == "$WORKER")


def _client(model: str):
    from openai import OpenAI
    with _lock:
        if model not in _clients:
            _clients[model] = OpenAI(base_url=ENDPOINTS[model], api_key="x", timeout=300, max_retries=2)
        return _clients[model]


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat(model: str, messages: list[dict], max_tokens: int = 2048, temperature: float = 0.0,
         timeout: int = 900) -> tuple[str, dict]:
    if model.startswith("fake:"):
        text = _fakes[model[5:]](messages)
        n = sum(len(m["content"]) for m in messages) // 4
        return text, {"prompt": n, "completion": len(text) // 4, "calls": 1}
    if model.startswith("claude:"):
        return _claude(model[7:], messages, timeout)
    if model.startswith("crusoe:"):
        return _crusoe(model[7:], messages, max_tokens, temperature, timeout)
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if model in NO_THINKING else {}
    resp = _client(model).chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                  max_tokens=max_tokens, extra_body=extra)
    return (resp.choices[0].message.content or ""), {"prompt": resp.usage.prompt_tokens,
                                                      "completion": resp.usage.completion_tokens, "calls": 1}


def _claude(alias: str, messages: list[dict], timeout: int) -> tuple[str, dict]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    body = "\n\n".join(m["content"] if m["role"] == "user" else f"[{m['role']}]\n{m['content']}"
                       for m in messages if m["role"] != "system")
    cmd = ["claude", "-p", "--model", alias, "--tools", "", "--output-format", "json", "--no-session-persistence"]
    if system:
        cmd += ["--system-prompt", system]
    with tempfile.TemporaryDirectory() as cwd:  # empty cwd: the model sees nothing but the prompt
        p = subprocess.run(cmd, input=body, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ})
    try:
        d = json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"claude CLI failed ({p.returncode}): {p.stderr[-500:]} {p.stdout[-500:]}")
    u = d.get("usage") or {}
    prompt = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return d.get("result", ""), {"prompt": prompt, "completion": u.get("output_tokens", 0), "calls": 1,
                                 "cost_usd": d.get("total_cost_usd", 0.0)}


CRUSOE_URL = os.environ.get("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
CRUSOE_CONCURRENCY = int(os.environ.get("CRUSOE_CONCURRENCY", "4"))
CRUSOE_MIN_TOKENS = int(os.environ.get("CRUSOE_MIN_TOKENS", "16000"))
_crusoe_sems: dict[str, threading.Semaphore] = {}


def crusoe_key() -> str:
    k = os.environ.get("CRUSOE_API") or os.environ.get("CRUSOE_API_KEY")
    if k:
        return k
    rc = Path.home() / ".zshrc"
    if rc.exists():
        for line in rc.read_text().splitlines():
            m = re.match(r"\s*export\s+CRUSOE_API(?:_KEY)?=[\"']?([^\"'\s]+)", line)
            if m:
                return m.group(1)
    raise RuntimeError("no Crusoe key: set CRUSOE_API")


def _crusoe(model: str, messages: list[dict], max_tokens: int, temperature: float, timeout: int,
            post: Callable | None = None) -> tuple[str, dict]:
    """One call, at most CRUSOE_CONCURRENCY in flight per model, jittered backoff on 429/5xx for ~4 minutes."""
    import urllib.error
    import urllib.request
    model, _, flag = model.partition("#")
    with _lock:
        sem = _crusoe_sems.setdefault(model, threading.Semaphore(CRUSOE_CONCURRENCY))
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max(max_tokens, CRUSOE_MIN_TOKENS)}
    if flag == "nothink":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    elif flag:
        raise ValueError(f"unknown crusoe model flag #{flag}")

    def _post(b: dict) -> dict:
        req = urllib.request.Request(f"{CRUSOE_URL}/chat/completions", json.dumps(b).encode(),
                                     {"Authorization": f"Bearer {crusoe_key()}", "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    post = post or _post
    delay, deadline = 3.0, time.time() + 240
    while True:
        try:
            with sem:
                r = post(body)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or time.time() > deadline:
                raise RuntimeError(f"crusoe {model} HTTP {e.code}: {e.read()[:300]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            if time.time() > deadline:
                raise RuntimeError(f"crusoe {model}: {e!r}")
        time.sleep(delay * (0.5 + random.random()))
        delay = min(delay * 2, 40)
    msg = r["choices"][0]["message"]
    u = r.get("usage") or {}
    return strip_think(msg.get("content") or ""), {"prompt": u.get("prompt_tokens", 0),
                                                   "completion": u.get("completion_tokens", 0), "calls": 1}


def add_usage(acc: dict, u: dict):
    for k, v in u.items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
