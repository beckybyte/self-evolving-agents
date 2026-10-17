"""Rule-based verifier for the Nemotron calendar_scheduling task (+ OOD soft preferences).

Hard rules (from the dataset's system prompt):
  - final answer = a JSON list of {event_id, event_name, start_time "HH:MM", duration}
  - every expected event present exactly once, with the expected duration, no extra events
  - all events inside [min_time, max_time] (10:00-16:00), no overlaps
  - "before X" = ends <= X; "after X" = starts >= X; "between A and B" = start >= A and end <= B; "at X" = start == X

Soft preferences (OOD only) live in exp_cal_state["_prefs"]: a list of dicts, see check_pref().
Score = 0 if any hard rule fails; else 1.0 without prefs, or 0.5 + 0.5 * fraction_of_prefs_met.
"""
from __future__ import annotations

import json
import re

from .official_grader import grade_assistant_response

_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def to_min(s: str) -> int:
    """'2pm' / '11:30am' / '14:05' -> minutes since midnight."""
    m = _TIME.fullmatch(s.strip())
    if not m:
        raise ValueError(f"bad time {s!r}")
    h, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mm


def parse_constraint(c: str | None):
    """-> (kind, a, b) in minutes, or None."""
    if not c:
        return None
    c = c.strip().lower()
    if m := re.fullmatch(r"between (.+?) and (.+)", c):
        return ("between", to_min(m.group(1)), to_min(m.group(2)))
    if m := re.fullmatch(r"(before|after|at) (.+)", c):
        return (m.group(1), to_min(m.group(2)), None)
    raise ValueError(f"unknown constraint {c!r}")


def hard_ok(kind_a_b, start: int, dur: int) -> bool:
    if kind_a_b is None:
        return True
    kind, a, b = kind_a_b
    end = start + dur
    return {
        "before": end <= a,
        "after": start >= a,
        "at": start == a,
        "between": start >= a and end <= (b if b is not None else a),
    }[kind]


def extract_calendar(text: str):
    """Last parseable JSON list of dicts in the text, else None."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    ends = [i for i, ch in enumerate(text) if ch == "]"]
    for e in reversed(ends):
        depth = 0
        for s in range(e, -1, -1):
            if text[s] == "]":
                depth += 1
            elif text[s] == "[":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[s : e + 1])
                    except Exception:
                        break
                    if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
                        return obj
                    break
    return None


def check_pref(p: dict, sched: dict[int, tuple[int, int]]) -> bool:
    """sched: event_id -> (start, end)."""
    t = p["type"]
    if t == "prefer_window":  # event starts and ends within [a, b]
        s, e = sched[p["event_id"]]
        return s >= to_min(p["start"]) and e <= to_min(p["end"])
    if t == "keep_free":  # no event overlaps [a, b)
        a, b = to_min(p["start"]), to_min(p["end"])
        return all(e <= a or s >= b for s, e in sched.values())
    if t == "right_after":  # event b starts exactly when a ends
        return sched[p["second"]][0] == sched[p["first"]][1]
    if t == "order":  # first ends before second starts
        return sched[p["first"]][1] <= sched[p["second"]][0]
    if t == "avoid_window":  # C-type: event does not overlap [a, b)
        s, e = sched[p["event_id"]]
        return e <= to_min(p["start"]) or s >= to_min(p["end"])
    if t == "min_gap":  # C-type: at least `minutes` between the two events, either order
        (s1, e1), (s2, e2) = sched[p["first"]], sched[p["second"]]
        return s2 - e1 >= p["minutes"] or s1 - e2 >= p["minutes"]
    if t == "max_gap":  # v2: the two events at most `minutes` apart (either order)
        (s1, e1), (s2, e2) = sched[p["first"]], sched[p["second"]]
        return max(s2 - e1, s1 - e2) <= p["minutes"]
    if t == "buffer_around":  # v2: no other event within `minutes` of this one
        s, e = sched[p["event_id"]]
        m = p["minutes"]
        return all(e2 <= s - m or s2 >= e + m for j, (s2, e2) in sched.items() if j != p["event_id"])
    raise ValueError(f"unknown pref {t}")


def verify(response: str, exp_cal_state: dict) -> dict:
    """-> {"score", "hard", "reason", "detail", "soft"}.

    Hard pass/fail is decided by the vendored official NeMo Gym grader (Apache-2.0);
    `detail` is our finer-grained diagnosis (e.g. violates_before) for the evolver.
    """
    hard_state = {k: v for k, v in exp_cal_state.items() if not k.startswith("_")}
    ok, official_reason = grade_assistant_response(response or "", hard_state)
    mine = _verify_mine(response, exp_cal_state)
    if not ok:
        return {"score": 0.0, "hard": False, "reason": official_reason, "detail": mine["reason"], "soft": None}
    if mine["hard"] is False:  # official passed but our stricter parse failed: trust official, keep a note
        return {"score": 1.0, "hard": True, "reason": "pass", "detail": "mine_disagrees:" + mine["reason"], "soft": None}
    return {**mine, "detail": mine["reason"]}


def _verify_mine(response: str, exp_cal_state: dict) -> dict:
    cal = extract_calendar(response)
    if cal is None:
        return {"score": 0.0, "hard": False, "reason": "no_json", "soft": None}
    expected = {int(k): v for k, v in exp_cal_state.items() if not k.startswith("_")}
    prefs = exp_cal_state.get("_prefs", [])
    sched: dict[int, tuple[int, int]] = {}
    try:
        for ev in cal:
            eid = int(ev["event_id"])
            if eid in sched:
                return _fail("duplicate_event")
            start = to_min(str(ev["start_time"]))
            sched[eid] = (start, start + int(ev["duration"]))
    except Exception:
        return _fail("bad_fields")
    if set(sched) - set(expected):
        return _fail("extra_event")
    if set(expected) - set(sched):
        return _fail("missing_event")
    for eid, exp in expected.items():
        s, e = sched[eid]
        if e - s != int(exp["duration"]):
            return _fail("wrong_duration")
        if s < to_min(exp.get("min_time", "10:00")) or e > to_min(exp.get("max_time", "16:00")):
            return _fail("out_of_window")
        if not hard_ok(parse_constraint(exp.get("constraint")), s, e - s):
            kind = parse_constraint(exp.get("constraint"))[0]
            return _fail(f"violates_{kind}")
    iv = sorted(sched.values())
    if any(iv[i][1] > iv[i + 1][0] for i in range(len(iv) - 1)):
        return _fail("overlap")
    if not prefs:
        return {"score": 1.0, "hard": True, "reason": "ok", "soft": None}
    # weighted prefs (v2 data): normalise by the best achievable weight, precomputed at build time
    got = sum(p.get("weight", 1) * check_pref(p, sched) for p in prefs)
    best = exp_cal_state.get("_best_w") or sum(p.get("weight", 1) for p in prefs)
    met = min(1.0, got / best) if best else 1.0
    return {"score": 0.5 + 0.5 * met, "hard": True, "reason": "ok" if met >= 1 else "soft_miss", "soft": met}


def _fail(reason: str) -> dict:
    return {"score": 0.0, "hard": False, "reason": reason, "soft": None}
