"""Build OOD preference splits (W3).

ood_dev  (A-type, seen during evolution): fuzzy time-of-day preferences -> prefer_window
ood_test (B-type, never seen during evolution): keep_free / right_after / order

The preference sentence is appended to the LAST user message. Every sample is checked to be
fully satisfiable (all hard constraints + all prefs) by the reference solver. Interpretations of
fuzzy words are fixed in ood/spec.md.
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from calevo.solver import SearchBudgetExceeded, solve, to_calendar_json  # noqa: E402
from calevo.verifier import extract_calendar, verify  # noqa: E402

A_WINDOWS = [  # phrase templates -> (start, end); fixed in spec.md
    (["I like to make decisions in the early morning, so I'd love {ev} to happen early in the day.",
      "{ev} needs a fresh mind -- first thing in my day would be ideal.",
      "If you can, put {ev} early, I'm sharpest before noon."], ("10:00", "12:00")),
    (["I'd prefer {ev} to be in the afternoon if possible.",
      "My mornings are for deep work, so {ev} would ideally land in the afternoon."], ("12:00", "16:00")),
    (["{ev} is a wind-down kind of thing, so late in the day suits me best.",
      "Ideally {ev} happens toward the end of my day."], ("14:00", "16:00")),
    (["Around lunchtime would be perfect for {ev}.",
      "Could {ev} sit somewhere around midday?"], ("11:30", "13:30")),
]
B_KEEP_FREE = [("I like to keep {a} to {b} free for a quick lunch, please don't put anything there.", ("12:00", "12:30")),
               ("Please leave {a}-{b} open, I take a short walk then.", ("12:30", "13:00")),
               ("Keep {a}-{b} clear if you can, I use it to catch up on email.", ("10:00", "10:30")),
               ("I'd like {a} to {b} to stay free so I can wrap up the day.", ("15:30", "16:00"))]
B_RIGHT_AFTER = ["I'd like {second} to come right after {first}, back to back.",
                 "Could you chain {second} directly after {first}? No gap between them."]
C_AVOID = ["Please keep {ev} out of {a}-{b}, I have a standing call then.",
           "{ev} can't overlap {a} to {b}; that's when I pick up my kid."]
C_GAP = ["I need at least {m} minutes between {first} and {second} to reset.",
         "Don't put {first} and {second} too close together - give me {m} minutes or more in between."]
C_WINDOWS = [("10:30", "11:30"), ("12:00", "13:00"), ("13:30", "14:30"), ("14:30", "15:30")]
B_ORDER = ["I'd rather have {first} done before {second} starts.",
           "Please make sure {second} doesn't start until {first} is finished."]


def h12(t: str) -> str:
    h, m = map(int, t.split(":"))
    suf = "am" if h < 12 else "pm"
    h = h - 12 if h > 12 else h
    return f"{h}{suf}" if m == 0 else f"{h}:{m:02d}{suf}"


def event_names(messages, state):
    names = {}
    for m in messages:
        if m["role"] == "assistant":
            for e in extract_calendar(m["content"]) or []:
                if "event_id" in e and "event_name" in e:
                    names[int(e["event_id"])] = e["event_name"]
        elif m["role"] == "user":  # events added in this turn have no assistant-side name yet
            for nm, i in re.findall(r'"([^"]{3,80})"[^"]{0,40}?\(event id: (\d+)\)', m["content"]):
                names.setdefault(int(i), nm)
    return {i: names.get(i) for i in map(int, state)}


def ref(i, names):
    n = names.get(i)
    short = None
    if n:
        short = re.split(r":\s|\s[-–—]\s|\s\(", n)[0].strip()  # split only at spaced separators
        if len(short.split()) < 2 or short.replace(":", "").isdigit():
            short = n.strip()
    return f'the "{short}" event (event id: {i})' if short else f"event {i}"


def cap(sent: str) -> str:
    return sent[0].upper() + sent[1:] if sent else sent


def satisfiable(state, prefs):
    ev = {int(k): v for k, v in state.items() if not k.startswith("_")}
    try:
        s = solve(ev, prefs, budget=200_000)
    except SearchBudgetExceeded:
        return False
    if s is None:
        return False
    return verify(json.dumps(to_calendar_json(ev, s)), {**state, "_prefs": prefs})["score"] == 1.0


def trivially_met(state, prefs):
    """True if the plain (no-pref) reference schedule already satisfies every pref -> pref adds nothing."""
    ev = {int(k): v for k, v in state.items() if not k.startswith("_")}
    try:
        s = solve(ev, [], budget=200_000)
    except SearchBudgetExceeded:
        return True
    return verify(json.dumps(to_calendar_json(ev, s)), {**state, "_prefs": prefs})["score"] == 1.0


def make(sample_messages, state, kind, rng):
    names = event_names(sample_messages, state)
    ids = [int(k) for k in state]
    if len(ids) < 3:
        return None
    for _ in range(40 if kind in ('B', 'C') else 12):
        prefs, sents = [], []
        if kind == "A":
            for i in rng.sample(ids, 2):
                tpls, (a, b) = rng.choice(A_WINDOWS)
                prefs.append({"type": "prefer_window", "event_id": i, "start": a, "end": b})
                sents.append(rng.choice(tpls).format(ev=ref(i, names)))
        elif kind == "C":
            named = [i for i in ids if names.get(i)] or ids
            for t in rng.sample(["avoid_window", "min_gap"], rng.choice([1, 2])):
                if t == "avoid_window":
                    i = rng.choice(named)
                    a, b = rng.choice(C_WINDOWS)
                    prefs.append({"type": "avoid_window", "event_id": i, "start": a, "end": b})
                    sents.append(rng.choice(C_AVOID).format(ev=ref(i, names), a=h12(a), b=h12(b)))
                else:
                    f, s2 = rng.sample(named if len(named) >= 2 else ids, 2)
                    m = rng.choice([15, 30])
                    prefs.append({"type": "min_gap", "first": f, "second": s2, "minutes": m})
                    sents.append(rng.choice(C_GAP).format(first=ref(f, names), second=ref(s2, names), m=m))
        else:
            kinds = rng.sample(["keep_free", "right_after", "order"], rng.choice([1, 2, 2]))
            if "keep_free" in kinds:
                t, (a, b) = rng.choice(B_KEEP_FREE)
                prefs.append({"type": "keep_free", "start": a, "end": b})
                sents.append(t.format(a=h12(a), b=h12(b)))
            named = [i for i in ids if names.get(i)]
            f, s = rng.sample(named if len(named) >= 2 else ids, 2)
            if "right_after" in kinds:
                prefs.append({"type": "right_after", "first": f, "second": s})
                sents.append(rng.choice(B_RIGHT_AFTER).format(first=ref(f, names), second=ref(s, names)))
            elif "order" in kinds:
                prefs.append({"type": "order", "first": f, "second": s})
                sents.append(rng.choice(B_ORDER).format(first=ref(f, names), second=ref(s, names)))
        new_state = {**state, "_prefs": prefs}
        if satisfiable(state, prefs) and not trivially_met(state, prefs):
            msgs = [dict(m) for m in sample_messages]
            msgs[-1]["content"] = msgs[-1]["content"].rstrip() + "\n\n" + " ".join(cap(x) for x in sents)
            return msgs, new_state
    return None


def build(src_rows, kind, n, seed, out_path):
    rng = random.Random(seed)
    out = []
    for sid, r in src_rows:
        got = make(r["messages"], r["state"], kind, rng)
        if got:
            out.append({"id": f"ood{kind}-{sid}", "messages": got[0], "state": got[1]})
        if len(out) == n:
            break
    with open(out_path, "w") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return len(out)


if __name__ == "__main__":
    sp = ROOT / "data" / "splits"
    train = [json.loads(l) for l in open(ROOT / "data" / "train.jsonl")]
    idx = json.loads((sp / "ood_dev_source_idx.json").read_text())
    dev_src = [(i, {"messages": train[i]["responses_create_params"]["input"], "state": train[i]["exp_cal_state"]}) for i in idx]
    test_src = [(r["id"], r) for r in (json.loads(l) for l in open(sp / "test_in.jsonl"))]
    if not (sp / "ood_dev.jsonl").exists():  # ood_dev is frozen once evolution has used it
        print("ood_dev", build(dev_src, "A", 60, 1, sp / "ood_dev.jsonl"))
    if not (sp / "ood_test.jsonl").exists():
        print("ood_test", build(test_src, "B", 60, 2, sp / "ood_test.jsonl"), flush=True)
    if not (sp / "ood_dev_c.jsonl").exists():  # C-type, disjoint train rows (after dev_in and ood_dev sources)
        allidx = list(range(len(train)))
        random.Random(0).shuffle(allidx)  # same shuffle as runner.make_in_splits
        c_src = [(i, {"messages": train[i]["responses_create_params"]["input"], "state": train[i]["exp_cal_state"]})
                 for i in allidx[180:400]]
        print("ood_dev_c", build(c_src, "C", 50, 3, sp / "ood_dev_c.jsonl"), flush=True)
