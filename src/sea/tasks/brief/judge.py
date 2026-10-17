"""Judge (frozen): scores a team's design artifacts with the current rubric, per scenario (persona walk-through)
and once globally (global criteria + complexity estimate). Sees only the artifacts, never the genome or its notes."""
from __future__ import annotations

import json
import re

from ...llm import chat, strip_think

MAX_ART_CHARS = 20000

SCENARIO = """You are a demanding product reviewer. Walk through the design below AS this persona in this scenario,
then score the design on each criterion (integer 1-5, use the anchors). Think as the persona: what would
confuse, annoy or fail you in this situation?

{calibration}

PERSONA: {persona}
SCENARIO: {scenario}

CRITERIA:
{criteria}

DESIGN DOCUMENTS:
{artifacts}

Return ONE JSON object: {{"walkthrough": "<=4 sentences: what happens to the persona",
"problems": {{"<criterion id>": ["concrete problem", ...], ...}}, "scores": {{"<criterion id>": 1-5, ...}},
"worst_gap": "the single most important thing the design gets wrong or misses for this persona (one sentence)"}}"""

GLOBAL = """You are a demanding product reviewer. Score the design below on each GLOBAL criterion (integer 1-5, use the
anchors), and estimate build complexity: list the features the design commits to for its first release and size each
with the cost model.

{calibration}

CRITERIA:
{criteria}

COST MODEL:
{cost_model}

DESIGN DOCUMENTS:
{artifacts}

Return ONE JSON object: {{"problems": {{"<criterion id>": ["concrete problem", ...], ...}},
"scores": {{"<criterion id>": 1-5, ...}}, "features": [{{"name": "...", "size": "S|M|L"}}],
"worst_gap": "the most important global weakness (one sentence)"}}"""

SIZE = {"S": 1, "M": 3, "L": 8}
CALIBRATION = """Scoring discipline (follow strictly):
- First list the concrete problems for each criterion (quote or point to the document), THEN score.
- Start every criterion at 3 = a competent first draft with clear gaps. Go to 4 only if the document handles this
  criterion better than most real PRDs, with specific evidence; 5 is reserved for nothing a senior PM would change
  (fewer than 1 in 10 designs). Each serious problem you listed costs a point. A 1 means the criterion is ignored.
- Claims are not features: a goal stated without a mechanism, flow or acceptance criterion earns no credit.
- Typical first drafts score 2-3. Do not reward length, formatting or confident tone."""


def render_artifacts(arts: dict) -> str:
    s = "\n\n".join(f"===== {k} =====\n{v}" for k, v in arts.items()) or "(the team produced no documents)"
    return s[:MAX_ART_CHARS] + ("\n[... truncated]" if len(s) > MAX_ART_CHARS else "")


def _crit_text(crits):
    return "\n".join(f"- {c['id']}: {c['description']} | anchors: " + "; ".join(f"{k}={v}" for k, v in c.get("anchors", {}).items())
                     for c in crits)


def _json(text: str) -> dict:
    text = strip_think(text)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def _ask(model, prompt, ids, tries=2):
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    for _ in range(tries):
        text, u = chat(model, [{"role": "user", "content": prompt}], max_tokens=2000, temperature=0.0)
        for k in usage:
            usage[k] += u.get(k, 0)
        try:
            d = _json(text)
            sc = {i: min(5, max(1, int(round(float(d["scores"][i]))))) for i in ids}
            return d, sc, usage
        except Exception:
            prompt += "\n\nReturn valid JSON with an integer score for EVERY criterion id."
    return {"worst_gap": "judge output unparseable"}, {i: 1 for i in ids}, usage


def judge_scenario(model, goal, scenario, arts) -> dict:
    crits = [c for c in goal["rubric"] if c["scope"] == "scenario"]
    persona = next((p for p in goal["personas"] if p["id"] == scenario.get("persona")), {})
    p = SCENARIO.format(persona=json.dumps(persona, ensure_ascii=False), scenario=json.dumps(
        {k: v for k, v in scenario.items() if k != "split"}, ensure_ascii=False), criteria=_crit_text(crits),
        artifacts=render_artifacts(arts), calibration=CALIBRATION)
    d, sc, u = _ask(model, p, [c["id"] for c in crits])
    return {"scores": sc, "walkthrough": str(d.get("walkthrough", ""))[:800], "worst_gap": str(d.get("worst_gap", ""))[:400],
            "problems": d.get("problems", {}), "usage": u}


def judge_global(model, goal, arts) -> dict:
    crits = [c for c in goal["rubric"] if c["scope"] == "global"]
    p = GLOBAL.format(criteria=_crit_text(crits), cost_model=goal.get("cost_model", ""), artifacts=render_artifacts(arts),
                      calibration=CALIBRATION)
    d, sc, u = _ask(model, p, [c["id"] for c in crits])
    feats = [f for f in d.get("features", []) if isinstance(f, dict)]
    points = sum(SIZE.get(str(f.get("size", "M")).upper()[:1], 3) for f in feats)
    return {"scores": sc, "features": feats, "complexity": points, "worst_gap": str(d.get("worst_gap", ""))[:400],
            "problems": d.get("problems", {}), "usage": u}
