"""Task registry: how each benchmark's samples are scored. The runner stays task-agnostic.

Sample rows (data/splits/*.jsonl): {"id", "task", "messages", "state"}
  calendar : state = exp_cal_state (+ _prefs/_best_w)           -> calevo.verifier.verify
  math     : state = {"answer": "<latex>"}  (MATH Lv5)          -> boxed answer, math-verify equivalence
  drop     : state = {"answers": [["alt answer 1"], ["alt answer 2"], ...]} -> max token-F1 over alternatives (DROP)

Split construction follows AFlow (arXiv 2410.10762): MATH = level-5 test problems from four types
(counting & probability, number theory, prealgebra, precalculus); DROP = 1000 sampled validation passages
questions; each split 1:4 into dev (used by evolution) : test (final report only), seed 0.
Deviation: AFlow reports 617 MATH problems; the four level-5 test pools here give the count printed by build().
"""
from __future__ import annotations

import json
import random
import re
import string
from collections import Counter
from pathlib import Path

from .verifier import verify as verify_calendar

ROOT = Path(__file__).resolve().parents[2]
MATH_TYPES = ["counting_and_probability", "number_theory", "prealgebra", "precalculus"]
MATH_SYS = "Solve the problem. Show your reasoning briefly, then give the final answer in \\boxed{}."
DROP_SYS = ("Answer the question using the passage. Reason briefly if needed, then give the final answer on the "
            "last line as: Answer: <answer>. For numbers give just the number; for spans give the exact span(s), "
            "comma-separated if several.")


# ------------------------------------------------------------------ scoring
def score(sample: dict, answer: str) -> dict:
    task = sample.get("task", "calendar")
    if task == "calendar":
        return verify_calendar(answer, sample["state"])
    if task == "math":
        ok = math_equal(answer, sample["state"]["answer"])
        return {"score": float(ok), "hard": ok, "reason": "ok" if ok else "wrong_answer", "detail": None, "soft": None}
    if task == "drop":
        f1 = drop_f1(answer, sample["state"]["answers"])
        return {"score": f1, "hard": f1 >= 0.999, "reason": "ok" if f1 >= 0.999 else ("partial" if f1 > 0 else "wrong_answer"),
                "detail": None, "soft": None}
    raise ValueError(f"unknown task {task}")


def last_boxed(text: str) -> str | None:
    i = text.rfind("\\boxed")
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j + 1:k]
    return None


def math_equal(pred_text: str, gold: str) -> bool:
    pred = last_boxed(pred_text or "")
    if pred is None:
        return False
    if _norm_tex(pred) == _norm_tex(gold):
        return True
    try:
        from math_verify import parse, verify
        return bool(verify(parse("$" + gold + "$"), parse("$" + pred + "$")))
    except Exception:
        return False


def _norm_tex(s: str) -> str:
    s = s.strip().replace(" ", "").replace("\\!", "").replace("\\left", "").replace("\\right", "")
    s = s.replace("dfrac", "frac").replace("tfrac", "frac").replace("^\\circ", "").replace("^{\\circ}", "")
    s = re.sub(r"\\text\{(.*?)\}", r"\1", s)
    return s.rstrip(".").rstrip("$").lstrip("$")


def _drop_tokens(s: str) -> list[str]:
    s = s.lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation) - {"."})
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    toks = []
    for t in s.split():
        t = t.strip(".")
        try:
            t = str(float(t.replace(",", ""))).rstrip("0").rstrip(".")
        except ValueError:
            pass
        if t:
            toks.append(t)
    return toks


def _f1(pred: str, gold: str) -> float:
    p, g = _drop_tokens(pred), _drop_tokens(gold)
    if not p and not g:
        return 1.0
    common = Counter(p) & Counter(g)
    n = sum(common.values())
    if n == 0:
        return 0.0
    prec, rec = n / len(p), n / len(g)
    return 2 * prec * rec / (prec + rec)


def drop_f1(pred_text: str, golds: list[list[str]]) -> float:
    m = re.findall(r"Answer:\s*(.+)", pred_text or "")
    lines = (pred_text or "").strip().splitlines()
    pred = m[-1].strip() if m else (lines[-1] if lines else "")
    best = 0.0
    for spans in golds:
        best = max(best, _f1(pred or "", " ".join(spans)))
    return best


# ------------------------------------------------------------------ data
def build(seed: int = 0):
    from datasets import load_dataset
    out = ROOT / "data" / "splits"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in MATH_TYPES:
        for r in load_dataset("EleutherAI/hendrycks_math", t, split="test"):
            if r["level"] == "Level 5":
                gold = last_boxed(r["solution"])
                if gold:
                    rows.append({"id": f"math-{t}-{len(rows)}", "task": "math",
                                 "messages": [{"role": "system", "content": MATH_SYS}, {"role": "user", "content": r["problem"]}],
                                 "state": {"answer": gold, "type": t}})
    _split(rows, seed, out, "math")
    d = list(load_dataset("ucinlp/drop", split="validation"))
    random.Random(seed).shuffle(d)
    rows = []
    for r in d[:1000]:
        # answers_spans.spans lists alternative annotations of the answer -> each unique one is a valid gold
        golds = [[a] for a in dict.fromkeys(r["answers_spans"]["spans"])]
        if not golds:
            continue
        rows.append({"id": f"drop-{r['query_id']}", "task": "drop",
                     "messages": [{"role": "system", "content": DROP_SYS},
                                  {"role": "user", "content": f"Passage: {r['passage']}\n\nQuestion: {r['question']}"}],
                     "state": {"answers": golds}})
    _split(rows, seed, out, "drop")


def build_math_train(n: int = 400, seed: int = 0):
    """Experience stream for per-agent learning: MATH *train* Level 5, same 4 types (disjoint from dev/test)."""
    from datasets import load_dataset
    rows = []
    for t in MATH_TYPES:
        for r in load_dataset("EleutherAI/hendrycks_math", t, split="train"):
            if r["level"] == "Level 5" and (gold := last_boxed(r["solution"])):
                rows.append({"id": f"mathtr-{t}-{len(rows)}", "task": "math",
                             "messages": [{"role": "system", "content": MATH_SYS}, {"role": "user", "content": r["problem"]}],
                             "state": {"answer": gold, "type": t}})
    random.Random(seed).shuffle(rows)
    out = ROOT / "data" / "splits" / "math_train.jsonl"
    with open(out, "w") as f:
        for r in rows[:n]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("math_train", min(n, len(rows)), "of", len(rows))


def _split(rows, seed, out, name):
    random.Random(seed).shuffle(rows)
    k = len(rows) // 5
    for split, part in ((f"{name}_dev", rows[:k]), (f"{name}_test", rows[k:])):
        with open(out / f"{split}.jsonl", "w") as f:
            for r in part:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(split, len(part))


if __name__ == "__main__":
    build()
