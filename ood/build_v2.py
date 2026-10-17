"""Data v2 ("calendar-hard"): free-form, weighted, partly conflicting preferences.

Pipeline per sample:
  1. sample 2-4 preferences from a type pool, each with weight 2 ("really matters") or 1 ("if possible");
     keep the sample only if (a) hard constraints + best pref subset are feasible, (b) the plain
     no-preference schedule scores below the optimum, and (c) for ~half the samples the prefs conflict
     (best achievable weight < total weight).
  2. Claude paraphrases the canonical sentences into natural, varied language (batch, `claude -p`).
  3. A second, blind Claude call parses each paraphrase back to structured form; samples whose parse
     does not match the ground truth exactly are dropped (=> wording varies, meaning stays recoverable).
  4. `_best_w` (max achievable weight) is stored so the verifier can normalise without a solver.

Type pools:  VISIBLE (evolution may see)  = prefer_window, avoid_window, min_gap, max_gap
             HELDOUT (test only)          = keep_free, right_after, order, buffer_around
Splits: dev_hard (train rows, VISIBLE) · test_hard_v (val rows, VISIBLE) · test_hard_h (val rows, HELDOUT)
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ood"))
from build import event_names, h12, ref  # noqa: E402
from calevo.solver import SearchBudgetExceeded, solve, solve_best, to_calendar_json  # noqa: E402
from calevo.verifier import verify  # noqa: E402

VISIBLE = ["prefer_window", "avoid_window", "min_gap", "max_gap"]
HELDOUT = ["keep_free", "right_after", "order", "buffer_around"]
WINDOWS = [("10:00", "11:30"), ("10:30", "12:00"), ("11:30", "13:00"), ("12:00", "13:30"),
           ("13:00", "14:30"), ("14:00", "15:30"), ("14:30", "16:00")]
FREE = [("10:00", "10:30"), ("12:00", "12:30"), ("12:30", "13:00"), ("15:30", "16:00")]


def canonical(p, names) -> str:
    t = p["type"]
    if t in ("prefer_window", "avoid_window", "buffer_around"):
        ev = ref(p["event_id"], names)
    if t in ("min_gap", "max_gap", "right_after", "order"):
        f, s = ref(p["first"], names), ref(p["second"], names)
    body = {
        "prefer_window": lambda: f"{ev} should happen between {h12(p['start'])} and {h12(p['end'])}.",
        "avoid_window": lambda: f"{ev} must not overlap {h12(p['start'])}-{h12(p['end'])}.",
        "min_gap": lambda: f"leave at least {p['minutes']} minutes between {f} and {s}.",
        "max_gap": lambda: f"{f} and {s} should be no more than {p['minutes']} minutes apart.",
        "keep_free": lambda: f"keep {h12(p['start'])}-{h12(p['end'])} completely free of events.",
        "right_after": lambda: f"{s} should start exactly when {f} ends.",
        "order": lambda: f"{f} should be finished before {s} starts.",
        "buffer_around": lambda: f"leave {p['minutes']} free minutes both before and after {ev}.",
    }[t]()
    lead = "This one really matters to me: " if p["weight"] == 2 else "If possible, "
    return lead + body


def sample_prefs(types, ids, named, rng):
    out = []
    for t in rng.sample(types, rng.choice([2, 2, 3])) + ([rng.choice(types)] if rng.random() < 0.3 else []):
        w = rng.choice([1, 2])
        if t in ("prefer_window", "avoid_window"):
            a, b = rng.choice(WINDOWS)
            out.append({"type": t, "event_id": rng.choice(named), "start": a, "end": b, "weight": w})
        elif t == "keep_free":
            a, b = rng.choice(FREE)
            out.append({"type": t, "start": a, "end": b, "weight": w})
        elif t == "buffer_around":
            out.append({"type": t, "event_id": rng.choice(named), "minutes": rng.choice([15, 30]), "weight": w})
        else:
            f, s = rng.sample(named, 2)
            p = {"type": t, "first": f, "second": s, "weight": w}
            if t in ("min_gap", "max_gap"):
                p["minutes"] = rng.choice([15, 30, 45]) if t == "min_gap" else rng.choice([15, 30, 45])
            out.append(p)
    return out


def score_of(ev, starts, state, prefs, best):
    return verify(json.dumps(to_calendar_json(ev, starts)), {**state, "_prefs": prefs, "_best_w": best})["score"]


def make(messages, state, types, rng, want_conflict):
    names = event_names(messages, state)
    ids = [int(k) for k in state]
    named = [i for i in ids if names.get(i)]
    if len(named) < 3:
        return None
    ev = {i: state[str(i)] for i in ids}
    for _ in range(30):
        prefs = sample_prefs(types, ids, named, rng)
        starts, best, chosen = solve_best(ev, prefs)
        if starts is None or best == 0:
            continue
        total = sum(p["weight"] for p in prefs)
        if want_conflict != (best < total):
            continue
        try:
            plain = solve(ev, [], budget=200_000)
        except SearchBudgetExceeded:
            continue
        if plain is None or score_of(ev, plain, state, prefs, best) >= 1.0:
            continue  # preferences must matter
        return {"prefs": prefs, "best_w": best, "canon": [canonical(p, names) for p in prefs], "names": names}
    return None


def claude_json(prompt: str, timeout=900):
    p = subprocess.run(["claude", "-p", "--model", "opus", "--output-format", "json", prompt],
                       capture_output=True, text=True, timeout=timeout, cwd="/tmp")
    txt = json.loads(p.stdout)["result"]
    m = re.search(r"\[.*\]", txt, re.S)
    return json.loads(m.group(0))


PARA = """Rewrite each scheduling request below as ONE natural sentence a busy person would type to their
assistant. Vary the wording a lot (casual, indirect, with a reason, different vocabulary); do NOT reuse the
template phrasing. Hard rules: keep the exact clock times / minute amounts; keep every "(event id: N)" tag
exactly once next to the event it belongs to (you may shorten the quoted name); keep the strength:
"really matters" -> clearly important/non-negotiable-sounding; "If possible" -> clearly optional/soft.
Return ONLY a JSON list of strings, same order and length.

{items}"""

BACK = """Each sentence below is a calendar preference. Parse each into JSON with keys:
type: one of prefer_window (event inside a time range), avoid_window (event must not overlap a range),
keep_free (no event at all in a range), min_gap (at least N minutes between two events),
max_gap (at most N minutes between two events), right_after (second starts exactly when first ends),
order (first finishes before second starts), buffer_around (N free minutes before and after one event);
event_id or first/second (ints from "(event id: N)"), start/end ("HH:MM" 24h) or minutes (int),
weight: 2 if it sounds important/required, 1 if optional/"if possible".
Return ONLY a JSON list, same order and length.

{items}"""


def matches(gt, got):
    keys = [k for k in gt if k != "weight"] + ["weight"]
    try:
        return all(str(got.get(k)) == str(gt[k]) for k in keys)
    except Exception:
        return False


def build(src, types, n, seed, out_path, tag):
    rng = random.Random(seed)
    cands = []
    for sid, msgs, state in src:
        got = make(msgs, state, types, rng, want_conflict=(len(cands) % 2 == 0))
        if got:
            cands.append((sid, msgs, state, got))
        if len(cands) >= int(n * 1.6):
            break
    flat = [(ci, j, c) for ci, (_, _, _, g) in enumerate(cands) for j, c in enumerate(g["canon"])]
    para, back = [None] * len(flat), [None] * len(flat)
    for b in range(0, len(flat), 30):
        chunk = flat[b:b + 30]
        items = "\n".join(f"{k + 1}. {c}" for k, (_, _, c) in enumerate(chunk))
        out = claude_json(PARA.format(items=items))
        assert len(out) == len(chunk), "paraphrase length mismatch"
        para[b:b + len(chunk)] = out
        items = "\n".join(f"{k + 1}. {s}" for k, s in enumerate(out))
        bk = claude_json(BACK.format(items=items))
        assert len(bk) == len(chunk), "back-parse length mismatch"
        back[b:b + len(chunk)] = bk
    kept, dropped = [], 0
    for ci, (sid, msgs, state, g) in enumerate(cands):
        idx = [k for k, (c, _, _) in enumerate(flat) if c == ci]
        if not all(matches(g["prefs"][flat[k][1]], back[k]) for k in idx):
            dropped += 1
            continue
        m2 = [dict(m) for m in msgs]
        m2[-1]["content"] = m2[-1]["content"].rstrip() + "\n\n" + " ".join(para[k] for k in idx)
        kept.append({"id": f"{tag}-{sid}", "messages": m2,
                     "state": {**state, "_prefs": g["prefs"], "_best_w": g["best_w"]}})
        if len(kept) == n:
            break
    with open(out_path, "w") as f:
        for o in kept:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    conflict = sum(o["state"]["_best_w"] < sum(p["weight"] for p in o["state"]["_prefs"]) for o in kept)
    print(f"{out_path.name}: kept {len(kept)} (dropped {dropped} by back-check, {conflict} with conflicts)", flush=True)


if __name__ == "__main__":
    sp = ROOT / "data" / "splits"
    train = [json.loads(l) for l in open(ROOT / "data" / "train.jsonl")]
    allidx = list(range(len(train)))
    random.Random(0).shuffle(allidx)  # same shuffle as runner.make_in_splits; [0:400] already used
    dev_src = [(i, train[i]["responses_create_params"]["input"], train[i]["exp_cal_state"]) for i in allidx[400:900]]
    val = [json.loads(l) for l in open(ROOT / "data" / "validation.jsonl")]
    v2 = [json.loads(l) for l in open(ROOT / "data" / "validation_v2.jsonl")]
    test_src = [(f"val{i}", r["responses_create_params"]["input"], r["exp_cal_state"]) for i, r in enumerate(val)] + \
               [(f"v2val{i}", r["responses_create_params"]["input"], r["exp_cal_state"]) for i, r in enumerate(v2)]
    random.Random(7).shuffle(test_src)
    which = sys.argv[1:] or ["dev_hard", "test_hard_v", "test_hard_h"]
    if "dev_hard" in which:
        build(dev_src, VISIBLE, 80, 11, sp / "dev_hard.jsonl", "hardV")
    if "test_hard_v" in which:
        build(test_src[:190], VISIBLE, 60, 12, sp / "test_hard_v.jsonl", "hardTV")
    if "test_hard_h" in which:
        build(test_src[190:], HELDOUT, 80, 13, sp / "test_hard_h.jsonl", "hardTH")
