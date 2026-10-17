"""Model catalog: which worker models a team may pick per role, what each is good at, and what it costs.

A genome role picks its model with `model: crusoe:<id>[#nothink]` (or `$WORKER`). The evolver reads catalog_text() so it
can choose per role (e.g. a strong coder for backend, a fast model for a checker). Tasks weight team tokens by `cost`
(relative to Nemotron Super = 1.0, from active parameters), so a big model must earn its price.

Probe (2026-10-17, Crusoe): one small stdlib coding task with self-written asserts, 2 trials per mode, max_tokens
16000; "pass" = code ran and its asserts held. Small sample: treat as a prior, not a ranking.
Pool: SEA_MODEL_POOL=nemotron (default: Nemotron family only, the hackathon's sponsor models) or all.
"""
from __future__ import annotations

import os

CATALOG = {
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL": dict(
        family="nemotron", cost=1.0, think="2/2 62s", nothink="1/2 51s",
        good="strongest Nemotron coder; with thinking on it can spend a whole 32k budget thinking on a big file "
             "(emits nothing) -> use #nothink for long files, thinking for short, hard ones"),
    "nvidia/Nemotron-3.5-Lightning-30B-A3B": dict(
        family="nemotron", cost=0.25, think="2/2 49s", nothink="0/2 56s",
        good="cheap and good WITH thinking (short outputs: plans, reviews, small modules); poor with #nothink"),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": dict(
        family="nemotron", cost=0.25, think="1/2 21s", nothink="0/2 23s",
        good="fastest Nemotron: classification, checklists, short JSON; not for writing whole files"),
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B": dict(
        family="nemotron", cost=1.0, think="1/2 108s", nothink="0/2 56s",
        good="older Super: slow and less reliable than 3.5-Super-VL; rarely the right pick"),
    "openai/gpt-oss-120b": dict(
        family="other", cost=0.5, think="2/2 6s", nothink="2/2 7s",
        good="fast and reliable at code and tool-style JSON (pool=all only)"),
    "deepseek-ai/DeepSeek-V4.1-Flash": dict(
        family="other", cost=0.5, think="2/2 36s", nothink="2/2 73s", good="reliable coder (pool=all only)"),
    "zai-org/GLM-5.3-Flash": dict(
        family="other", cost=0.5, think="2/2 79s", nothink="2/2 144s", good="reliable but slow, very verbose (pool=all only)"),
    "Qwen/Qwen3.8-27B": dict(
        family="other", cost=0.5, think="2/2 46s", nothink="1/2 50s", good="decent coder with thinking (pool=all only)"),
    "google/gemma-4-31b-it": dict(
        family="other", cost=0.5, think="2/2 15s", nothink="1/2 46s", good="short outputs, fast (pool=all only)"),
}


def pool() -> str:
    return os.environ.get("SEA_MODEL_POOL", "nemotron")


def allowed(model_id: str) -> bool:
    """model_id: a crusoe id without the crusoe: prefix, with or without #nothink."""
    m = CATALOG.get(model_id.partition("#")[0])
    return m is not None and (pool() == "all" or m["family"] == "nemotron")


def cost(model: str) -> float:
    """Relative token price of a role model string ('crusoe:<id>[#nothink]', anything else = 1.0)."""
    if model.startswith("crusoe:"):
        m = CATALOG.get(model[7:].partition("#")[0])
        return m["cost"] if m else 1.0
    return 1.0


def catalog_text() -> str:
    rows = [f"- crusoe:{mid}  (cost x{m['cost']}; probe thinking {m['think']}, #nothink {m['nothink']}): {m['good']}"
            for mid, m in CATALOG.items() if pool() == "all" or m["family"] == "nemotron"]
    return ("MODELS a role may pick (`model: crusoe:<id>` or `crusoe:<id>#nothink`; `$WORKER` = the run's default). "
            "Team tokens are weighted by cost. Probe = 2 trials of one small coding task, a prior only:\n" + "\n".join(rows))
