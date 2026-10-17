"""Parse the extractor's JSON state and place events with an exact search.

Hard constraints (10:00-16:00 window, no overlap, at/before/after/between) are never
violated. Soft preferences are maximised by branch & bound. Supported preference types:
  window  {event_id, start, end}       event starts >= start and ends <= end
  avoid   {event_id, start, end}       event does not intersect [start, end)
  order   {event_id, other_id}         event ends before other starts
  adjacent{event_id, other_id}         the two events are back-to-back (either order)
  apart   {event_id, other_id, gap}    at least `gap` minutes (default 30) between them
  earliest / latest {event_id}         as early / as late as possible (graded)
Unknown types are ignored.
"""
import json
import re
from math import gcd

DAY_START, DAY_END = 600, 960
NODE_LIMIT = 400000


def to_min(t):
    if t is None:
        return None
    s = str(t).strip().lower().replace(".", "")
    if s in ("noon", "midday"):
        return 720
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if ap is None and h < 8:  # bare "2" or "3:30" in a 10-16 calendar means pm
        h += 12
    return h * 60 + mi


TIME = r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|noon|midday)"


def parse_constraint(c):
    """-> (lo_start, hi_end, exact_start)"""
    lo, hi, at = DAY_START, DAY_END, None
    if not c:
        return lo, hi, at
    s = str(c).strip().lower()
    m = re.search(r"between\s+" + TIME + r"\s+(?:and|to|-)\s+" + TIME, s)
    if m:
        a, b = to_min(m.group(1)), to_min(m.group(2))
        if a is not None:
            lo = max(lo, a)
        if b is not None:
            hi = min(hi, b)
        return lo, hi, at
    m = re.search(r"\bat\s+" + TIME, s)
    if m and to_min(m.group(1)) is not None:
        return lo, hi, to_min(m.group(1))
    m = re.search(r"(?:before|by|until)\s+" + TIME, s)
    if m and to_min(m.group(1)) is not None:
        hi = min(hi, to_min(m.group(1)))
    m = re.search(r"(?:after|from)\s+" + TIME, s)
    if m and to_min(m.group(1)) is not None:
        lo = max(lo, to_min(m.group(1)))
    return lo, hi, at


def fmt(m):
    return "%02d:%02d" % (m // 60, m % 60)


def strip_think(t):
    t = re.sub(r"<think>.*?</think>", "", t or "", flags=re.S)
    return t.split("</think>")[-1]


def parse_state(text):
    text = strip_think(text)
    text = re.sub(r"```(?:json)?", "", text)
    best = None
    for i in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:j + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict) and "events" in obj:
                        best = obj
                    break
    if best is None:  # maybe a bare list of events
        m = re.search(r"\[.*\]", text, flags=re.S)
        if m:
            try:
                lst = json.loads(m.group(0))
                if isinstance(lst, list):
                    best = {"events": lst, "prefs": []}
            except Exception:
                pass
    return best


def clean_events(state):
    evs = {}
    for e in state.get("events") or []:
        try:
            eid = int(e.get("event_id"))
            dur = int(e.get("duration"))
        except Exception:
            continue
        if dur <= 0:
            continue
        evs[eid] = {"name": str(e.get("event_name") or e.get("name") or "Event %d" % eid),
                    "duration": dur, "constraint": e.get("constraint")}
    return evs


def domains(evs, step, drop=()):
    dom = {}
    for eid, e in evs.items():
        d = e["duration"]
        lo, hi, at = parse_constraint(None if eid in drop else e["constraint"])
        if at is not None:
            vals = [at] if DAY_START <= at and at + d <= DAY_END else []
        else:
            first = lo + (-(lo - DAY_START)) % step
            vals = list(range(first, hi - d + 1, step))
        dom[eid] = vals
    return dom


def pref_fns(prefs, evs):
    """Each pref -> (event ids involved, scoring fn(starts)->[0,1])."""
    out = []
    for p in prefs or []:
        try:
            typ = str(p.get("type", "window")).lower()
            a = int(p.get("event_id"))
        except Exception:
            continue
        if a not in evs:
            continue
        da = evs[a]["duration"]
        if typ in ("window", "prefer_window"):
            s, e = to_min(p.get("start")), to_min(p.get("end"))
            if s is None or e is None:
                continue
            out.append(((a,), lambda st, a=a, s=s, e=e, da=da: float(st[a] >= s and st[a] + da <= e)))
        elif typ == "avoid":
            s, e = to_min(p.get("start")), to_min(p.get("end"))
            if s is None or e is None:
                continue
            out.append(((a,), lambda st, a=a, s=s, e=e, da=da: float(st[a] + da <= s or st[a] >= e)))
        elif typ == "earliest":
            out.append(((a,), lambda st, a=a: 1.0 - (st[a] - DAY_START) / 360.0))
        elif typ == "latest":
            out.append(((a,), lambda st, a=a, da=da: 1.0 - (DAY_END - st[a] - da) / 360.0))
        elif typ in ("order", "adjacent", "apart"):
            try:
                b = int(p.get("other_id"))
            except Exception:
                continue
            if b not in evs or b == a:
                continue
            db = evs[b]["duration"]
            if typ == "order":
                f = lambda st, a=a, b=b, da=da: float(st[a] + da <= st[b])
            elif typ == "adjacent":
                f = lambda st, a=a, b=b, da=da, db=db: float(st[a] + da == st[b] or st[b] + db == st[a])
            else:
                g = int(p.get("gap") or 30)
                f = lambda st, a=a, b=b, da=da, db=db, g=g: float(st[a] + da + g <= st[b] or st[b] + db + g <= st[a])
            out.append(((a, b), f))
    return out


def search(evs, dom, prefs):
    order = sorted(evs, key=lambda i: (len(dom[i]), -evs[i]["duration"]))
    pos = {eid: k for k, eid in enumerate(order)}
    # a pref becomes checkable once all its events are placed
    ready = [[] for _ in order]
    for ids, f in prefs:
        ready[max(pos[i] for i in ids)].append(f)
    total = len(prefs)
    best = {"score": -1.0, "sol": None}
    nodes = [0]
    starts, busy = {}, []

    def rec(k, score, remaining):
        nodes[0] += 1
        if nodes[0] > NODE_LIMIT:
            return True
        if score + remaining <= best["score"] + 1e-9:
            return False
        if k == len(order):
            best["score"], best["sol"] = score, dict(starts)
            return best["score"] >= total - 1e-9
        eid = order[k]
        d = evs[eid]["duration"]
        cand = []
        for s in dom[eid]:
            if any(s < be and bs < s + d for bs, be in busy):
                continue
            starts[eid] = s
            gain = sum(f(starts) for f in ready[k])
            cand.append((-gain, s))
        starts.pop(eid, None)
        cand.sort()
        rem = remaining - len(ready[k])
        for neg, s in cand:
            starts[eid] = s
            busy.append((s, s + d))
            stop = rec(k + 1, score - neg, rem)
            busy.pop()
            del starts[eid]
            if stop:
                return True
        return False

    rec(0, 0.0, float(total))
    return best["sol"]


def solve_state(evs, prefs):
    vals = [DAY_START, DAY_END] + [e["duration"] for e in evs.values()]
    for e in evs.values():
        lo, hi, at = parse_constraint(e["constraint"])
        vals += [lo, hi] + ([at] if at is not None else [])
    step = 0
    for v in vals:
        step = gcd(step, int(v))
    step = max(5, step)
    pf = pref_fns(prefs, evs)
    sol = search(evs, domains(evs, step), pf)
    if sol is not None:
        return sol
    # infeasible: the extraction is probably wrong somewhere; drop one constraint at a time
    for eid in sorted(evs, key=lambda i: evs[i]["constraint"] is None):
        if evs[eid]["constraint"] is None:
            continue
        sol = search(evs, domains(evs, 5, drop=(eid,)), pf)
        if sol is not None:
            return sol
    return search(evs, domains(evs, 5, drop=tuple(evs)), [])


# ---------------------------------------------------------------------------
# Deterministic reader for hard rules the user states with a clock time.
# The LLM extractor often drops earlier-turn rules, copies the assistant's own placements
# in as "at" rules, or hardens soft wishes; a user sentence naming exactly one event and a
# clock-time phrase is unambiguous, so the latest such sentence per event overrides the LLM.

EID = re.compile(r"event\s*(?:id)?\s*[:#]?\s*(\d+)", re.I)
CLK = r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)|\d{1,2}:\d{2}|noon|midday)"
SOFT = re.compile(r"ideal|prefer|would love|i'd love|love (?:it|the|to)|would be (?:perfect|great|nice|ideal)|"
                  r"suits me|if possible|if you can|rather|happy if|nice if|bonus|sharpest|wind-down|"
                  r"would like it to|i like to", re.I)
FREE = re.compile(r"no longer|any ?time|anywhere|flexible|doesn't matter|does not matter|remove the (?:time|constraint)|"
                  r"drop the (?:time|constraint)|unlock|free(?:ly)? (?:to )?(?:move|place)", re.I)
RULES = [
    ("between", re.compile(r"between\s+" + CLK + r"\s+(?:and|to|-|–)\s+" + CLK, re.I)),
    ("before", re.compile(r"(?:finish(?:es|ed)?|wrap(?:s|ped)?\s+up|end(?:s|ed)?|done|over|complete[sd]?|out)\s+"
                          r"(?:at\s+or\s+before|by|before|no\s+later\s+than)\s+" + CLK, re.I)),
    ("before", re.compile(r"(?:no\s+later\s+than|by|before)\s+" + CLK, re.I)),
    ("after", re.compile(r"(?:at\s+or\s+after|no\s+earlier\s+than|not\s+before|after)\s+" + CLK, re.I)),
    ("after", re.compile(CLK + r"\s+(?:or\s+later|onwards?|or\s+after)", re.I)),
    ("at", re.compile(r"(?:\bat|\bfor|\bto|\bin\s+for|down\s+for|@)\s+" + CLK, re.I)),
]


def _norm(s):
    s = s.replace(" ", " ").replace(" ", " ").replace("‑", "-").replace("‐", "-")
    return s


def _sentences(text):
    parts = re.split(r"(?<=[.?!])\s+|\n+", text)
    segs = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        has_id = bool(EID.search(p))
        if segs and not has_id and not re.search(CLK, p, re.I):
            segs[-1] += " " + p          # chit-chat: attach to previous
        elif segs and has_id and not re.search(CLK, p, re.I) and not EID.search(segs[-1]) \
                and re.search(CLK, segs[-1], re.I) and len(EID.findall(p)) == 1 and p.startswith("("):
            segs[-1] += " " + p          # "... for 3:30pm? (event id: 5)"
        else:
            segs.append(p)
    return segs


def read_rule(seg):
    """-> constraint string, '__free__', or None for a one-event sentence."""
    if SOFT.search(seg):
        return None
    if FREE.search(seg) and not re.search(CLK, seg, re.I):
        return "__free__"
    body = EID.sub(" ", seg)
    for kind, rx in RULES:
        m = rx.search(body)
        if not m:
            continue
        if kind == "between":
            return "between %s and %s" % (m.group(1), m.group(2))
        if kind == "before" and re.search(r"\bstart", body[:m.start()][-25:], re.I):
            continue  # "start no later than" is not an end bound
        return "%s %s" % (kind, m.group(1))
    return None


def user_rules(messages):
    rules = {}
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        for seg in _sentences(_norm(str(c or ""))):
            ids = set(int(x) for x in EID.findall(seg))
            if len(ids) != 1:
                continue
            r = read_rule(seg)
            if r is None:
                continue
            eid = ids.pop()
            lo, hi, at = parse_constraint(r) if r != "__free__" else (0, 0, None)
            if r != "__free__" and lo == DAY_START and hi == DAY_END and at is None:
                continue  # unparseable / vacuous
            rules[eid] = r
    return rules


def user_clock_minutes(messages):
    """Every clock time the user (or system) wrote; a rule time outside this set is invented."""
    seen = {DAY_START, DAY_END}
    for msg in messages or []:
        if msg.get("role") not in ("user", "system"):
            continue
        c = msg.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        for t in re.findall(CLK, _norm(str(c or "")), re.I):
            v = to_min(t)
            if v is not None:
                seen.add(v)
    return seen


def grounded(constraint, seen):
    if not constraint:
        return True
    s = str(constraint).lower()
    times = re.findall(TIME, s)
    vals = [to_min(t) for t in times if to_min(t) is not None]
    return all(v in seen for v in vals)


def run(blackboard, messages):
    src = blackboard.get("extract", "")
    state = parse_state(src)
    if not state:
        return {"text": strip_think(src).strip() or "[]"}
    evs = clean_events(state)
    try:
        seen = user_clock_minutes(messages)
        for e in evs.values():
            if not grounded(e["constraint"], seen):
                e["constraint"] = None
    except Exception:
        pass
    try:
        for eid, r in user_rules(messages).items():
            if eid in evs:
                evs[eid]["constraint"] = None if r == "__free__" else r
    except Exception:
        pass
    if not evs:
        return {"text": "[]"}
    sol = solve_state(evs, state.get("prefs") or state.get("preferences") or [])
    if sol is None:
        sol, t = {}, DAY_START
        for eid in sorted(evs):
            sol[eid] = t
            t += evs[eid]["duration"]
    cal = [{"event_id": eid, "event_name": evs[eid]["name"], "start_time": fmt(sol[eid]),
            "duration": evs[eid]["duration"]} for eid in sorted(evs)]
    return {"text": json.dumps(cal, ensure_ascii=False)}
