"""parallel workflow step + crusoe backend (no network)."""
import json
import threading
import time
import urllib.error

import yaml

from sea import llm
from sea.executor import run_team
from sea.genome import load_genome, structure_signature


def _genome(tmp_path, workflow):
    roles = [{"name": n, "kind": "llm", "model": "fake:slow", "prompt": f"you are {n}\n{{blackboard}}"}
             for n in ("plan", "a", "b", "c", "join")]
    (tmp_path / "genome.yaml").write_text(yaml.safe_dump({"schema": 2, "id": "t", "roles": roles, "workflow": workflow}))
    return load_genome(tmp_path)


def test_parallel_runs_concurrently_from_same_state(tmp_path):
    seen, active, peak = {}, [0], [0]
    lock = threading.Lock()

    def slow(msgs):
        body = msgs[-1]["content"]
        name = body.split("\n")[0].split()[-1]
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        seen[name] = body
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return f'<artifact name="{name}_py">x = 1</artifact>'

    llm.register_fake("slow", slow)
    g = _genome(tmp_path, ["plan", {"parallel": ["a", "b", "c"]}, "join"])
    t = time.time()
    res = run_team(g, {"id": "e", "input": {"text": "go"}}, "fake:slow")
    assert time.time() - t < 1.2 * 0.3 * 3 + 0.3  # a, b, c overlapped
    assert peak[0] == 3
    for n in "abc":  # every parallel role saw plan's output and none of its siblings'
        assert "plan_py" in seen[n] and not any(f"{m}_py" in seen[n] for m in "abc" if m != n)
    assert all(f"{n}_py" in seen["join"] for n in "abc")
    assert set(res["artifacts"]) == {"plan_py", "a_py", "b_py", "c_py", "join_py"}
    assert res["usage"]["calls"] == 5 and len(res["trace"]) == 5
    assert structure_signature(g).endswith("|P1")


def test_parallel_validation(tmp_path):
    import pytest
    for bad in (["plan", {"parallel": ["a"]}], ["plan", {"parallel": ["a", "zz"]}], [{"parallel": ["a", "a"]}]):
        with pytest.raises(AssertionError):
            _genome(tmp_path, bad)


def test_crusoe_backend_retries_and_floors_tokens(monkeypatch):
    monkeypatch.setenv("CRUSOE_API", "k")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = []

    def post(body):
        calls.append(body)
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 429, "rate", {}, None)
        return {"choices": [{"message": {"content": "<think>hmm</think>ok"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}}

    text, u = llm._crusoe("nvidia/x", [{"role": "user", "content": "hi"}], 2048, 0.0, 30, post=post)
    assert text == "ok" and {k: u[k] for k in ("prompt", "completion", "calls")} == {"prompt": 3, "completion": 5, "calls": 1}
    assert u["retries"] == 1 and u["finish"] == "length" and u["secs"] >= 0
    assert len(calls) == 2 and calls[0]["max_tokens"] >= llm.CRUSOE_MIN_TOKENS
    assert llm.known_model("crusoe:nvidia/NVIDIA-Nemotron-3.5-Super-VL")


def test_crusoe_nothink_flag(monkeypatch):
    monkeypatch.setenv("CRUSOE_API", "k")
    bodies = []
    post = lambda b: bodies.append(b) or {"choices": [{"message": {"content": "x"}}], "usage": {}}
    llm._crusoe("nvidia/x#nothink", [{"role": "user", "content": "hi"}], 100, 0.0, 30, post=post)
    llm._crusoe("nvidia/x", [{"role": "user", "content": "hi"}], 100, 0.0, 30, post=post)
    assert bodies[0]["model"] == "nvidia/x" and bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in bodies[1]
