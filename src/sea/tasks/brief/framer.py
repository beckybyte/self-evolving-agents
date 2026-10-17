"""Framer (frozen, outside the evolver's view): turns a vague product brief into a versioned, self-built evaluation,
and revises it when the human answers. Output of one framing = goal/v<N>/ (see docs/architecture_v2.md §4.1)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ...llm import chat, strip_think

SCHEMA = """Return ONE JSON object, no prose around it:
{
 "restatement": "markdown: the goal in your own words: target users, the job to be done, what success looks like, explicit non-goals",
 "assumptions": [{"id": "A1", "text": "...", "default": "the value we assume until told otherwise", "confidence": 0.0-1.0}],
 "questions": [{"question": "...", "default": "best-guess answer we proceed on", "assumption": "A1", "why": "which trade-off it decides"}],
 "personas": [{"id": "P1", "name": "...", "profile": "...", "goals": "...", "constraints": "...", "pain_points": "..."}],
 "scenarios": [{"id": "S1", "persona": "P1", "situation": "a concrete moment in this persona's day", "needs": "what the product must do here", "success": "how the persona would judge it"}],
 "rubric": [{"id": "snake_case", "scope": "scenario|global", "weight": 0-1, "description": "...",
             "anchors": {"1": "...", "3": "...", "5": "..."}}],
 "cost_model": "markdown: how to size features (S=1, M=3, L=8 points) for THIS product, with examples",
 "changelog": "what changed vs the previous version and which human answer caused it (first version: 'initial')"
}"""

FRAME = """You are the FRAMER of a product-design team. Turn the vague brief below into a goal and an evaluation that a
judge can apply to design documents (PRD, feature list with trade-offs, user flows). There is no dataset: you build
the proxy evaluation yourself.

Rules:
- 4-6 assumptions, the ones that most change the design. Up to 4 questions for the human (they will answer later or
  never; every question needs a default we proceed on).
- 6-8 personas including at least one edge user and one skeptic; 1-2 scenarios each (8-12 scenarios total),
  concrete and varied; scenarios must be judgeable from a design document alone.
- Rubric: 5-7 criteria, weights summing to 1. Must include `tradeoff_rationale` (global: are choices, alternatives
  and reasons explicit, tied to assumptions?) and `scope_discipline` (global: is the MVP small and focused rather than
  a feature pile?). Scenario-scoped criteria are judged per scenario from the persona's point of view.
- Write all text in the language of the brief.

BRIEF:
{brief}

{schema}"""

REVISE = """You are the FRAMER of a product-design team. Below are the brief, the CURRENT goal/evaluation (JSON) and NEW input
from the human (answers to our questions, ratings/comments on designs). Revise the goal and evaluation so it reflects
what the human told us: mark assumptions as confirmed/rejected (add field "status"), change defaults, add/remove/edit
personas, scenarios and rubric criteria or weights where the human's input implies it. Keep ids stable for things
that did not change; new things get new ids. Do not drop criteria `tradeoff_rationale` and `scope_discipline`.
Only ask NEW questions (not ones already answered). Write all text in the language of the brief.

BRIEF:
{brief}

CURRENT (version {ver}):
{current}

NEW HUMAN INPUT:
{human}

{schema}"""


def _extract_json(text: str) -> dict:
    text = strip_think(text)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    s = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(s)


def _check(d: dict) -> dict:
    for k in ("restatement", "assumptions", "personas", "scenarios", "rubric"):
        assert d.get(k), f"framer output missing {k}"
    ids = {c["id"] for c in d["rubric"]}
    assert {"tradeoff_rationale", "scope_discipline"} <= ids, "rubric must keep tradeoff_rationale and scope_discipline"
    tot = sum(float(c.get("weight", 0)) for c in d["rubric"]) or 1.0
    for c in d["rubric"]:
        c["weight"] = round(float(c.get("weight", 0)) / tot, 4)
        c["scope"] = c.get("scope", "scenario") if c.get("scope") in ("scenario", "global") else "scenario"
    pids = {p["id"] for p in d["personas"]}
    d["scenarios"] = [s for s in d["scenarios"] if s.get("persona") in pids] or d["scenarios"]
    split_scenarios(d["scenarios"])
    d.setdefault("questions", [])
    d.setdefault("cost_model", "S=1, M=3, L=8 points per feature.")
    return d


def split_scenarios(scen: list[dict]):
    """Deterministic ~2:1 fit/holdout split by id hash (stable across revisions); at least 1 holdout, 2 fit."""
    for s in scen:
        s["split"] = "holdout" if int(hashlib.sha1(s["id"].encode()).hexdigest(), 16) % 3 == 0 else "fit"
    hold = [s for s in scen if s["split"] == "holdout"]
    if not hold and len(scen) >= 3:
        scen[-1]["split"] = "holdout"
    while sum(s["split"] == "fit" for s in scen) < 2 and any(s["split"] == "holdout" for s in scen):
        next(s for s in scen if s["split"] == "holdout")["split"] = "fit"


def _call(model: str, prompt: str, tries: int = 2) -> tuple[dict, dict]:
    err = None
    for _ in range(tries):
        text, u = chat(model, [{"role": "user", "content": prompt}], max_tokens=8000, temperature=0.3)
        try:
            return _check(_extract_json(text)), u
        except Exception as e:  # retry once with the error appended
            err = e
            prompt += f"\n\nYour previous output was invalid ({e!r}). Return valid JSON only."
    raise RuntimeError(f"framer failed: {err!r}")


def frame(brief: str, model: str) -> tuple[dict, dict]:
    return _call(model, FRAME.replace("{brief}", brief).replace("{schema}", SCHEMA))


def revise(brief: str, current: dict, ver: str, human_input: str, model: str) -> tuple[dict, dict]:
    cur = {k: v for k, v in current.items() if k != "changelog"}
    return _call(model, REVISE.replace("{brief}", brief).replace("{ver}", ver).replace(
        "{current}", json.dumps(cur, ensure_ascii=False, indent=1)).replace("{human}", human_input).replace("{schema}", SCHEMA))


def write_version(goal_dir: Path, ver: str, d: dict):
    v = goal_dir / ver
    v.mkdir(parents=True, exist_ok=True)
    (v / "goal.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    (v / "restatement.md").write_text(d["restatement"])
    (v / "cost_model.md").write_text(d["cost_model"])
    (v / "CHANGELOG.md").write_text(d.get("changelog", ""))
    for k in ("assumptions", "personas", "scenarios", "questions"):
        (v / f"{k}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in d[k]))
    (v / "rubric.json").write_text(json.dumps(d["rubric"], ensure_ascii=False, indent=1))
    (goal_dir / "CURRENT").write_text(ver)
