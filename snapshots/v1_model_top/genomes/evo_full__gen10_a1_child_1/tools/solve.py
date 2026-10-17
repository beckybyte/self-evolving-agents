"""Parse the extractor's JSON state (objects or compact [id, name, duration, rule] rows) and place events with an exact search.

Hard constraints (10:00-16:00 window, no overlap, at/before/after/between) are never
violated. Soft preferences are maximised by branch & bound. Supported preference types:
  window  {event_id, start, end}       event starts >= start and ends <= end
  avoid   {event_id, start, end}       event does not intersect [start, end)
  order   {event_id, other_id}         event ends before other starts
  adjacent{event_id, other_id}         the two events are back-to-back (either order)
  apart   {event_id, other_id, gap}    at least `gap` minutes (default 30) between them
  earliest / latest {event_id}         as early / as late as possible (graded)
Other type names are mapped onto these by norm_type (aliases, then the fields present); soft wishes the
extractor missed are read from the user's sentences by fill_prefs when no pref already covers that event.
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
                    if isinstance(obj, dict) and ("events" in obj or "e" in obj):
                        best = obj
                    break
    if best is None:  # maybe a bare object of events
        try:
            obj = json.loads(text[text.index("{"):text.rindex("}") + 1])
            if isinstance(obj, dict) and any(isinstance(v, dict) and "duration" in v for v in obj.values()):
                best = {"events": obj, "prefs": []}
        except Exception:
            pass
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
    raw = state.get("events") or state.get("e") or []
    if isinstance(raw, dict):  # {"0": {...}, "1": {...}} instead of a list
        raw = [dict(v, event_id=v.get("event_id", k)) if isinstance(v, dict) else None for k, v in raw.items()]
    for e in raw:
        if isinstance(e, list) and len(e) >= 3:  # compact row [id, name, duration, rule]
            e = {"event_id": e[0], "event_name": e[1], "duration": e[2], "constraint": e[3] if len(e) > 3 else None}
        if not isinstance(e, dict):
            continue
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
        n0 = len(out)
        _pref_fn(p, evs, out)
        if len(out) > n0:
            ids, f = out[-1]
            w = float(p.get("_w", 1.0))
            out[-1] = (ids, lambda st, f=f, w=w: w * f(st), w)
    return out


ALIAS = {"prefer_window": "window", "time_window": "window", "during": "window", "within": "window", "prefer_time": "window",
         "avoid_window": "avoid", "not_during": "avoid", "exclude": "avoid", "avoid_time": "avoid",
         "before": "order", "after": "order", "precede": "order", "sequence": "order", "prefer_order": "order",
         "back_to_back": "adjacent", "together": "adjacent", "consecutive": "adjacent", "next_to": "adjacent",
         "prefer_adjacent": "adjacent", "separate": "apart", "spread": "apart", "gap": "apart", "buffer": "apart",
         "not_adjacent": "apart", "prefer_apart": "apart", "early": "earliest", "as_early_as_possible": "earliest",
         "late": "latest", "as_late_as_possible": "latest"}


def norm_type(p):
    """Map whatever pref name the extractor invented onto a supported type (by name, then by fields)."""
    t = re.sub(r"[\s-]+", "_", str(p.get("type") or "").strip().lower())
    t = ALIAS.get(t, t)
    if t in ("window", "avoid", "order", "adjacent", "apart", "earliest", "latest"):
        if t == "order" and str(p.get("type", "")).lower().strip() == "after":
            p["event_id"], p["other_id"] = p.get("other_id"), p.get("event_id")
        return t
    if p.get("other_id") is not None:
        return "apart" if re.search(r"apart|gap|separ|spread|space|buffer|not", t) else (
            "adjacent" if re.search(r"adj|back|together|consec|next", t) else "order")
    if p.get("start") is not None and p.get("end") is not None:
        return "avoid" if re.search(r"avoid|not|free|block|exclu", t) else "window"
    if re.search(r"early|first|morning", t):
        return "earliest"
    if re.search(r"late|last|end", t):
        return "latest"
    return t


def _pref_fn(p, evs, out):
    try:
        typ = norm_type(p)
        a = int(p.get("event_id"))
    except Exception:
        return
    if a not in evs:
        return
    da = evs[a]["duration"]
    if typ in ("window", "prefer_window"):
        s, e = to_min(p.get("start")), to_min(p.get("end"))
        if s is None or e is None:
            return
        out.append(((a,), lambda st, a=a, s=s, e=e, da=da: float(st[a] >= s and st[a] + da <= e)))
    elif typ == "avoid":
        s, e = to_min(p.get("start")), to_min(p.get("end"))
        if s is None or e is None:
            return
        out.append(((a,), lambda st, a=a, s=s, e=e, da=da: float(st[a] + da <= s or st[a] >= e)))
    elif typ == "earliest":
        out.append(((a,), lambda st, a=a: 1.0 - (st[a] - DAY_START) / 360.0))
    elif typ == "latest":
        out.append(((a,), lambda st, a=a, da=da: 1.0 - (DAY_END - st[a] - da) / 360.0))
    elif typ in ("order", "adjacent", "apart"):
        try:
            b = int(p.get("other_id"))
        except Exception:
            return
        if b not in evs or b == a:
            return
        db = evs[b]["duration"]
        if typ == "order":
            f = lambda st, a=a, b=b, da=da: float(st[a] + da <= st[b])
        elif typ == "adjacent":
            f = lambda st, a=a, b=b, da=da, db=db: float(st[a] + da == st[b] or st[b] + db == st[a])
        else:
            g = int(p.get("gap") or 30)
            f = lambda st, a=a, b=b, da=da, db=db, g=g: float(st[a] + da + g <= st[b] or st[b] + db + g <= st[a])
        out.append(((a, b), f))


def search(evs, dom, prefs):
    order = sorted(evs, key=lambda i: (len(dom[i]), -evs[i]["duration"]))
    pos = {eid: k for k, eid in enumerate(order)}
    # a pref becomes checkable once all its events are placed
    ready = [[] for _ in order]
    for ids, f, w in prefs:
        ready[max(pos[i] for i in ids)].append((f, w))
    total = sum(w for _, _, w in prefs)
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
            gain = sum(f(starts) for f, _ in ready[k])
            cand.append((-gain, s))
        starts.pop(eid, None)
        cand.sort()
        rem = remaining - sum(w for _, w in ready[k])
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
    # infeasible: the extraction is wrong somewhere. Drop the least-trusted constraints first
    # (trust 2 = read from a user sentence by regex, 1 = LLM time seen in a user message naming
    # the event, 0 = LLM time not tied to the event), one at a time, then all untrusted ones.
    cons = [i for i in evs if evs[i]["constraint"] is not None]
    cons.sort(key=lambda i: (evs[i].get("trust", 1), -i))
    for eid in cons:
        sol = search(evs, domains(evs, 5, drop=(eid,)), pf)
        if sol is not None:
            return sol
    weak = tuple(i for i in cons if evs[i].get("trust", 1) < 2)
    if weak:
        sol = search(evs, domains(evs, 5, drop=weak), pf)
        if sol is not None:
            return sol
    return search(evs, domains(evs, 5, drop=tuple(evs)), [])


# ---------------------------------------------------------------------------
# Deterministic reader for hard rules the user states with a clock time.
# The LLM extractor often drops earlier-turn rules, copies the assistant's own placements
# in as "at" rules, or hardens soft wishes; a user sentence naming exactly one event and a
# clock-time phrase is unambiguous, so the latest such sentence per event overrides the LLM.

EID = re.compile(r"event\s*(?:id)?\s*[:#]?\s*(\d+)", re.I)
CLK = r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)|\d{1,2}:\d{2}|\bnoon\b|\bmidday\b)"
NUMCLK = re.compile(r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}", re.I)  # a written clock time
SOFT = re.compile(r"ideal|prefer|would love|i'd love|love (?:it|the|to)|would be (?:perfect|great|nice|ideal)|"
                  r"suits me|if possible|if you can|rather|happy if|nice if|bonus|sharpest|wind-down|"
                  r"would like it to|i like to", re.I)
FREE = re.compile(r"no longer|any ?time|anywhere|flexible|doesn't matter|does not matter|remove the (?:time|constraint)|"
                  r"drop the (?:time|constraint)|unlock|free(?:ly)? (?:to )?(?:move|place)", re.I)
LOOSE = r"((?<![\d:])\d{1,2}(?::\d{2})?(?![\d:])\s*(?:[ap]\.?m\.?)?|noon|midday)"
NOTDUR = r"(?!\s*(?:min|minute|hour|hr|h\b))"
RULES = [
    ("between", re.compile(r"between\s+" + LOOSE + r"\s+(?:and|to|-|–)\s+" + LOOSE + NOTDUR, re.I)),
    ("between", re.compile(r"\bfrom\s+" + LOOSE + r"\s*(?:to|until|till|through|-|–)\s*" + LOOSE + NOTDUR, re.I)),
    ("between", re.compile(CLK + r"\s*(?:-|–|to)\s*" + CLK + r"\s+(?:window|range|slot|block)", re.I)),
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
    text = re.sub(r"\b(Dr|Mr|Mrs|Ms|Prof|St|Sr|Jr|vs|approx|e\.g|i\.e)\.\s", r"\1 ", text)  # not sentence ends
    parts = re.split(r"(?<=[.?!])\s+|\n+", text)
    segs = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        has_id = bool(EID.search(p))
        if segs and not has_id and not re.search(CLK, p, re.I) and not (SOFT.search(p) and not SOFT.search(segs[-1])):
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
            if r != "__free__" and at is None and not (DAY_START <= lo < hi <= DAY_END):
                continue  # e.g. a duration range misread as clock times
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


# ---------------------------------------------------------------------------
# Grounding of events and soft preferences in the user's own words.

def _user_texts(messages):
    out = []
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
        out.append(_norm(str(c or "")))
    return out


def user_ids(messages):
    ids = set()
    for t in _user_texts(messages):
        ids.update(int(x) for x in EID.findall(t))
    return ids


def constraint_trust(eid, constraint, messages):
    """1 if every clock time of the LLM's rule appears in a user message naming this event, else 0."""
    s = str(constraint).lower()
    vals = [to_min(t) for t in re.findall(TIME, s) if to_min(t) is not None]
    near = set()
    for t in _user_texts(messages):
        if eid in set(int(x) for x in EID.findall(t)):
            near.update(v for v in (to_min(x) for x in re.findall(CLK, t, re.I)) if v is not None)
    return 1 if all(v in near for v in vals) else 0


# Fuzzy time-of-day phrase families -> window. The phrase ending last in a sentence wins
# ("my mornings are for deep work, so ... the afternoon" -> afternoon); ties go to the longer.
FAMILIES = [
    ("14:00", "16:00", r"late\s+in\s+the\s+day|later\s+in\s+the\s+day|end\s+of\s+(?:my|the)\s+(?:work\s*)?day|"
                       r"toward(?:s)?\s+the\s+end|wind[\s-]*down|late\s+afternoon|close\s+of\s+(?:the\s+)?day|"
                       r"last\s+thing|day'?s\s+end|tail\s+end"),
    ("12:00", "16:00", r"afternoon|after\s+lunch|post[\s-]*lunch"),
    ("11:30", "13:30", r"lunch\s*time|lunchtime|around\s+lunch|over\s+lunch|midday|mid-day|around\s+noon|noon[\s-]*ish"),
    ("10:00", "12:00", r"early\s+morning|first\s+thing|before\s+noon|early\s+in\s+the\s+day|start\s+of\s+(?:my|the)\s+day|"
                       r"\bmorning|before\s+lunch|kick\s+off\s+the\s+day"),
]
NEG = re.compile(r"\b(?:not|never|avoid|away\s+from|out\s+of|keep\s+\w+\s+off|anything\s+but|other\s+than)\b|n't\b", re.I)
FUZZY = re.compile("|".join(f for _, _, f in FAMILIES), re.I)


def family_window(seg):
    best = None
    for s, e, rx in FAMILIES:
        for m in re.finditer(rx, seg, re.I):
            key = (m.end(), m.end() - m.start())
            if best is None or key > best[0]:
                best = (key, s, e)
    if best is None:
        return None
    return best[1], best[2]


def soft_segments(messages):
    segs = []
    for t in _user_texts(messages):
        for seg in _sentences(t):
            if SOFT.search(seg) or (FUZZY.search(seg) and not NUMCLK.search(seg)):
                segs.append((seg, set(int(x) for x in EID.findall(seg))))
    return segs


def ground_prefs(prefs, evs, messages):
    """Weight the LLM's prefs by grounding; replace window prefs the phrase table can read itself."""
    segs = soft_segments(messages)
    det = {}
    for seg, ids in segs:
        if len(ids) != 1 or NEG.search(seg) or NUMCLK.search(seg):
            continue
        fw = family_window(seg)
        if fw:
            det[next(iter(ids))] = {"type": "window", "event_id": next(iter(ids)),
                                    "start": fw[0], "end": fw[1], "_w": 1.0}
    out = list(det.values())
    llm = []
    for p in prefs or []:
        if not isinstance(p, dict):
            continue
        try:
            a = int(p.get("event_id"))
        except Exception:
            continue
        typ = str(p.get("type", "window")).lower()
        if a in det and typ in ("window", "prefer_window"):
            continue
        b = p.get("other_id")
        try:
            b = int(b) if b is not None else None
        except Exception:
            b = None
        if b is None:
            ok = any(a in ids for _, ids in segs)
        else:
            ok = any(a in ids and b in ids for _, ids in segs)
        q = dict(p)
        q["_w"] = 1.0 if ok else 0.25
        out.append(q)
        llm.append(q)
    try:
        out += fill_prefs(segs, evs, out)
    except Exception:
        pass
    return out


# Deterministic reader for soft wishes the extractor may have missed. A wish read here is only
# added when no pref of the same kind already covers its event(s), so it never fights the LLM.
P_APART = re.compile(r"\bapart\b|\bgap\b|buffer|breather|breathing\s+room|\bspace\b|spaced|separate|spread|"
                     r"(?:not|n't|never|avoid)\b[^.?!]{0,25}back[\s-]*to[\s-]*back|break\s+between", re.I)
P_ADJ = re.compile(r"back[\s-]*to[\s-]*back|right\s+(?:after|before)|immediately\s+(?:after|before)|straight\s+after|"
                   r"directly\s+(?:after|before)|next\s+to|adjacent|consecutive|together|one\s+after\s+(?:the\s+)?other", re.I)
P_ORD = re.compile(r"\b(before|after|ahead\s+of|prior\s+to|follow(?:s|ing)?|precede[sd]?|earlier\s+than|later\s+than)\b", re.I)
P_EARLY = re.compile(r"as\s+early\s+as|earliest|soonest|as\s+soon\s+as", re.I)
P_LATE = re.compile(r"as\s+late\s+as|latest\s+possible|\bas\s+late\b", re.I)
SOFT_RULES = [
    ("between", re.compile(r"between\s+" + LOOSE + r"\s+(?:and|to|-|–)\s+" + LOOSE + NOTDUR, re.I)),
    ("between", re.compile(r"\bfrom\s+" + LOOSE + r"\s*(?:to|until|till|-|–)\s*" + LOOSE + NOTDUR, re.I)),
    ("between", re.compile(CLK + r"\s*(?:-|–|to)\s*" + CLK, re.I)),
    ("before", re.compile(r"(?:no\s+later\s+than|by|before|until|till)\s+" + CLK, re.I)),
    ("after", re.compile(r"(?:no\s+earlier\s+than|after|from)\s+" + CLK, re.I)),
]


def _soft_range(body):
    for kind, rx in SOFT_RULES:
        m = rx.search(body)
        if not m:
            continue
        if kind == "between":
            a, b = to_min(m.group(1)), to_min(m.group(2))
        elif kind == "before":
            a, b = DAY_START, to_min(m.group(1))
        else:
            a, b = to_min(m.group(1)), DAY_END
        if a is not None and b is not None and DAY_START <= a < b <= DAY_END:
            return fmt(a), fmt(b)
    return None


def fill_prefs(segs, evs, have):
    def has(kinds, ids):
        for p in have:
            try:
                t = norm_type(dict(p))
                pid = {int(p.get("event_id"))} | ({int(p["other_id"])} if p.get("other_id") is not None else set())
            except Exception:
                continue
            if t in kinds and set(ids) <= pid:
                return True
        return False

    add = []
    for seg, ids in segs:
        ids = [i for i in ids if i in evs]
        body = EID.sub(" EVT ", seg)
        if len(ids) == 2:
            pos = sorted((m.start(), int(m.group(1))) for m in EID.finditer(seg) if int(m.group(1)) in ids)
            a, b = pos[0][1], pos[-1][1]
            if a == b or has(("order", "adjacent", "apart"), (a, b)):
                continue
            if P_APART.search(body):
                add.append({"type": "apart", "event_id": a, "other_id": b})
            elif P_ADJ.search(body):
                add.append({"type": "adjacent", "event_id": a, "other_id": b})
            else:
                m = P_ORD.search(seg, pos[0][0], pos[-1][0])
                if m and not NEG.search(seg):
                    later = re.match(r"after|follow|later", m.group(1), re.I)
                    add.append({"type": "order", "event_id": b if later else a, "other_id": a if later else b})
        elif len(ids) == 1:
            a = ids[0]
            if has(("window", "avoid", "earliest", "latest"), (a,)):
                continue
            if P_EARLY.search(body) and not NEG.search(body):
                add.append({"type": "earliest", "event_id": a})
            elif P_LATE.search(body) and not NEG.search(body):
                add.append({"type": "latest", "event_id": a})
            elif SOFT.search(seg) and NUMCLK.search(body):
                r = _soft_range(body)
                if r:
                    add.append({"type": "avoid" if NEG.search(body) else "window", "event_id": a, "start": r[0], "end": r[1]})
            elif NEG.search(body) and not NUMCLK.search(body):
                fams = [(s0, e0) for s0, e0, rx in FAMILIES if re.search(rx, body, re.I)]
                if len(set(fams)) == 1:
                    add.append({"type": "avoid", "event_id": a, "start": fams[0][0], "end": fams[0][1]})
    for q in add:
        q["_w"] = 1.0
    return add


# ---------------------------------------------------------------------------
# Deterministic duration reader: the latest user request giving an event a length wins.

NUMW = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "fifteen": 15,
        "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fortyfive": 45, "sixty": 60, "ninety": 90}
DNUM = r"(\d+(?:\.\d+)?|one|two|three|four|five|six|ten|fifteen|twenty|thirty|forty-five|forty|sixty|ninety|an?)"
D_HM = re.compile(DNUM + r"\s*-?\s*(?:hours?|hrs?)\s*(?:and\s+)?" + DNUM + r"\s*-?\s*(?:minutes?|mins?)\b", re.I)
D_HHALF = re.compile(r"\b(an?|one|two|three|\d+)\s*-?\s*(?:and\s+a\s+half|½)\s*-?\s*hours?|\b(an?|one|two|three|\d+)\s+hours?\s+and\s+a\s+half", re.I)
D_HALF = re.compile(r"half\s*(?:an?\s*)?-?\s*hour", re.I)
D_QUARTER = re.compile(r"(?:a\s+)?quarter\s*(?:of\s+an?\s*)?-?\s*hour", re.I)
D_H = re.compile(r"\b" + DNUM + r"\s*-?\s*(?:hours?|hrs?)\b", re.I)
D_M = re.compile(r"\b" + DNUM + r"\s*-?\s*(?:minutes?|mins?)\b", re.I)
REL_UP = re.compile(r"\b(?:extend|lengthen|longer|add(?:ing)?\s+(?:another|an\s+extra|extra)|another|extra|more)\b", re.I)
REL_DOWN = re.compile(r"\b(?:shorten|shorter|cut|trim|reduce|less)\b", re.I)
SHIFT = re.compile(r"\b(?:push|move|shift|delay|postpone|later|earlier|bring\s+forward|gap|break|buffer|apart|before|after|between)\b", re.I)


def _num(x):
    x = x.lower()
    if x in NUMW:
        return NUMW[x]
    try:
        return float(x)
    except Exception:
        return None


def read_duration(seg):
    """-> ("set"|"add", minutes) or None."""
    s = EID.sub(" ", seg)
    s = re.sub(CLK, " ", s, flags=re.I)  # clock times are not lengths
    val = None
    m = D_HM.search(s)
    if m and _num(m.group(1)) is not None and _num(m.group(2)) is not None:
        val, span = _num(m.group(1)) * 60 + _num(m.group(2)), m
    if val is None:
        m = D_HHALF.search(s)
        if m:
            h = _num(m.group(1) or m.group(2))
            if h is not None:
                val, span = h * 60 + 30, m
    if val is None:
        found = []
        for rx, mul in ((D_HALF, None), (D_QUARTER, None), (D_H, 60), (D_M, 1)):
            for m in rx.finditer(s):
                if mul is None:
                    v = 30 if rx is D_HALF else 15
                else:
                    n = _num(m.group(1))
                    if n is None:
                        continue
                    v = n * mul
                found.append((m.start(), v, m))
        vals = set(v for _, v, _ in found)
        if len(vals) != 1:
            return None  # none, or ambiguous
        _, val, span = found[0]
    val = int(round(val))
    if not (5 <= val <= 360):
        return None
    before, after = s[max(0, span.start() - 30):span.start()], s[span.end():span.end() + 20]
    if re.search(r"\bby\s*$", before, re.I):
        if REL_UP.search(s):
            return ("add", val)
        if REL_DOWN.search(s):
            return ("add", -val)
        return None  # "push it by 30 minutes"
    if re.search(r"\b(?:another|extra|more)\s*$", before, re.I) or re.match(r"\s*(?:more|longer|extra)\b", after, re.I):
        return ("add", val)
    if re.match(r"\s*(?:shorter|less)\b", after, re.I):
        return ("add", -val)
    if SHIFT.search(before[-25:]) or re.match(r"\s*(?:later|earlier|before|after|apart|of\s+(?:gap|break|buffer)|gap|break|buffer|between)\b", after, re.I):
        return None  # "push it 30 minutes later", "a 15 minute break before"
    return ("set", val)


def user_durations(messages):
    """-> {event_id: [("set"|"add", minutes), ...]} in conversation order."""
    out = {}
    for t in _user_texts(messages):
        tid = set(int(x) for x in EID.findall(t))
        for seg in re.split(r"(?<=[.?!])\s+|\n+", t):
            ids = set(int(x) for x in EID.findall(seg))
            if len(ids) > 1:
                continue
            if not ids:
                if len(tid) != 1 or SOFT.search(seg) or not re.search(
                        r"could you|can you|please|make (?:it|that|this)|set it|change|update|need it|i'd like|i would like|it should|let's|should be|for\s", seg, re.I):
                    continue
                ids = tid
            r = read_duration(seg)
            if r:
                out.setdefault(next(iter(ids)), []).append(r)
    return out


def apply_durations(evs, messages):
    for eid, ops in user_durations(messages).items():
        if eid not in evs:
            continue
        sets = [i for i, (k, _) in enumerate(ops) if k == "set"]
        if sets:
            d = ops[sets[-1]][1]
            for k, v in ops[sets[-1] + 1:]:
                d += v
        else:
            continue  # only relative edits: trust the LLM's arithmetic
        if 5 <= d <= 360:
            evs[eid]["duration"] = d


# ---------------------------------------------------------------------------
# Event recovery: the extractor sometimes drops an event (often one added in the last turn,
# or one whose JSON object it garbled). An id whose latest user action is an explicit "add"
# with a readable duration is put back; removals are honoured.

ADD = re.compile(r"\b(?:add(?:ing)?|slot|schedule|book|put|pencil|squeeze|fit|create|set\s+up|include|"
                 r"new\s+(?:event|meeting|session)|block\s+(?:out|off)|throw\s+in|bring\s+back|re-?add|restore)\b", re.I)
REMOVE = re.compile(r"\b(?:remove|delete|cancel|drop|scrap|get\s+rid|no\s+longer\s+(?:need|want|happening)|"
                    r"take\s+(?:it\s+|\w+\s+)?off|clear\s+(?:it|out)|axe|nix|won't\s+need|called\s+off)\b", re.I)
CONSTR_WORD = re.compile(r"constraint|restriction|time\s+(?:rule|limit|requirement)|the\s+(?:time|window|slot|deadline)\b", re.I)
QUOTE = re.compile(r"[\"“”']([^\"“”]{3,120})[\"“”]")


def _clauses(seg):
    if len(set(EID.findall(seg))) <= 1:
        return [seg]
    return [c for c in re.split(r"\s*(?:;|,\s*(?:and|but|then)\b|\band\s+(?:also\s+)?(?=(?:please\s+)?\w+\s+(?:the|a|an)\b)|\bthen\b)\s*", seg) if c]


def event_actions(messages):
    """-> {eid: (last action "add"|"remove", name or None, set-duration or None)} from one-event user clauses."""
    acts = {}
    for t in _user_texts(messages):
        for seg in _sentences(t):
            for cl in _clauses(seg):
                ids = set(int(x) for x in EID.findall(cl))
                if len(ids) != 1:
                    continue
                eid = ids.pop()
                body = EID.sub(" ", cl)
                if REMOVE.search(body) and not CONSTR_WORD.search(body) and not FREE.search(body):
                    acts[eid] = ("remove", None, None)
                    continue
                r = read_duration(cl)
                if ADD.search(body) and (QUOTE.search(cl) or r):
                    acts[eid] = ("add", title_of(cl), r[1] if r and r[0] == "set" else None)
    return acts


TITLED = re.compile(r"(?:called|titled|named|entitled)\s+(.{3,120}?)\s*(?:\(|$|\bto\b|\bfor\b|[?.!,])", re.I)
AFTER_DUR = re.compile(r"(?:minutes?|mins?|hours?|hrs?)\s+(?:long\s+)?(?:event\s+|meeting\s+|session\s+)?(?:for\s+|of\s+)?"
                       r"(?:the\s+|a\s+|an\s+|my\s+)?([A-Z][^()?!]{2,120}?)\s*\(", re.I)


def title_of(cl):
    for rx in (QUOTE, TITLED, AFTER_DUR):
        m = rx.search(cl)
        if m:
            return m.group(1)
    return None


def short_name(title, eid):
    if not title:
        return "Event %d" % eid
    t = re.split(r"\s*[:\u2013\u2014(]\s*|\s+-\s+", title.strip())[0].strip(" .,'\"")
    return t or "Event %d" % eid


def recover_events(evs, messages):
    acts = event_actions(messages)
    durs = user_durations(messages)
    for eid, (kind, title, d0) in acts.items():
        if kind == "remove" or eid in evs:  # removals: trust the extractor
            continue
        ops = durs.get(eid) or []
        sets = [i for i, (k, _) in enumerate(ops) if k == "set"]
        if sets:
            d = ops[sets[-1]][1] + sum(v for _, v in ops[sets[-1] + 1:])
        elif d0:
            d = d0
        else:
            continue
        if 5 <= d <= 360:
            evs[eid] = {"name": short_name(title, eid), "duration": d, "constraint": None}


def salvage_events(text):
    """Event objects from a truncated / malformed extractor reply."""
    out = []
    for m in re.finditer(r"\{[^{}]*\}", strip_think(text or "")):
        frag = m.group(0)
        try:
            obj = json.loads(frag)
        except Exception:
            continue
        if isinstance(obj, dict) and "duration" in obj:
            out.append(obj)
    if not out:  # compact rows [id, "name", duration, rule]
        for m in re.finditer(r"\[\s*\d+\s*,\s*\"[^\"\]]*\"\s*,\s*\d+\s*(?:,\s*(?:null|\"[^\"\]]*\"))?\s*\]", strip_think(text or "")):
            try:
                out.append(json.loads(m.group(0)))
            except Exception:
                pass
    return {"events": out, "prefs": []} if out else None


def run(blackboard, messages):
    src = blackboard.get("extract", "")
    state = parse_state(src) or salvage_events(src) or {"events": [], "prefs": []}
    evs = clean_events(state)
    try:
        ids = user_ids(messages)
        if ids and any(i in ids for i in evs):
            evs = {i: e for i, e in evs.items() if i in ids}  # never invent an event the user did not name
    except Exception:
        pass
    try:
        recover_events(evs, messages)
    except Exception:
        pass
    try:
        apply_durations(evs, messages)
    except Exception:
        pass
    try:
        for i, e in evs.items():
            e["trust"] = constraint_trust(i, e["constraint"], messages) if e["constraint"] else 1
    except Exception:
        pass
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
                evs[eid]["trust"] = 2
    except Exception:
        pass
    if not evs:
        return {"text": "[]"}
    prefs = state.get("prefs") or state.get("p") or state.get("preferences") or []
    try:
        prefs = ground_prefs(prefs, evs, messages)
    except Exception:
        pass
    sol = solve_state(evs, prefs)
    if sol is None:
        sol, t = {}, DAY_START
        for eid in sorted(evs):
            sol[eid] = t
            t += evs[eid]["duration"]
    cal = [{"event_id": eid, "event_name": evs[eid]["name"], "start_time": fmt(sol[eid]),
            "duration": evs[eid]["duration"]} for eid in sorted(evs)]
    return {"text": json.dumps(cal, ensure_ascii=False)}
