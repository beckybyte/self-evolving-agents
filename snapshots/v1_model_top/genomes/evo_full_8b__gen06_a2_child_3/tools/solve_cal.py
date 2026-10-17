"""Parse the extractor's JSON spec and build a conflict-free calendar.

Hard constraints: "at X" (start == X), "before X" (end <= X), "after X" (start >= X),
"between X and Y" (start >= X and end <= Y); all events inside 10:00-16:00.
Soft preferences (general forms):
  window {event_id, start, end}      event lies inside [start, end]
  avoid  {event_id, start, end}      event does not overlap [start, end]
  order  {first, second}             `first` ends before `second` starts
  buffer {event_id, minutes}         keep `minutes` free after the event
Maximises the number of satisfied preferences via subset search over an exact
sequencing DP (per order, greedy earliest start is optimal for start-time sets).
"""
import itertools
import json
import re

DAY0, DAY1 = 600, 960
STEP = 5


def strip_think(t):
    t = t or ""
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return t


def find_json(text):
    text = strip_think(text)
    # prefer fenced blocks, else the largest balanced {...}
    cands = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for c in cands[::-1]:
        try:
            return json.loads(c)
        except Exception:
            pass
    best = None
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        depth, instr, esc = 0, False, False
        for j in range(i, len(text)):
            c = text[j]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
                continue
            if c == '"':
                instr = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i:j + 1])
                        if best is None or (j + 1 - i) > best[0]:
                            best = (j + 1 - i, obj)
                    except Exception:
                        pass
                    break
    return best[1] if best else None


TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?", re.I)


def parse_time(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = int(s)
        return v if v >= 600 else (v * 60 if v < 24 else None)
    s = str(s).strip().lower()
    if "noon" in s or "midday" in s:
        return 720
    m = TIME_RE.search(s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if not ap and 1 <= h <= 7:  # bare "2:30" in a 10-16 calendar means pm
        h += 12
    return h * 60 + mi


def times_in(s):
    out = []
    s = str(s).lower().replace("noon", "12:00pm")
    for m in re.finditer(r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s*m\.?)?", s):
        t = parse_time(m.group(0))
        if t is not None:
            out.append(t)
    return out


def hard_window(c, dur):
    """Return (lo_start, hi_start) for an event's hard constraint, or None if unparseable."""
    lo, hi = DAY0, DAY1 - dur
    if not c:
        return lo, hi
    if isinstance(c, dict):
        k = str(c.get("type", "")).lower()
        ts = [parse_time(c.get(x)) for x in ("time", "start", "end") if c.get(x) is not None]
        c = " ".join([k] + [f"{t // 60}:{t % 60:02d}" for t in ts if t is not None])
    s = str(c).lower()
    ts = times_in(s)
    if not ts:
        return lo, hi
    if "between" in s or "from" in s or (len(ts) >= 2 and ("and" in s or "-" in s or "to" in s)):
        if len(ts) >= 2:
            return max(lo, ts[0]), min(hi, ts[1] - dur)
    if "before" in s or "by" in s or "end" in s or "finish" in s or "until" in s:
        return lo, min(hi, ts[0] - dur)
    if "after" in s or "later" in s or "from" in s:
        return max(lo, ts[0]), hi
    # "at X" / plain time
    return ts[0], ts[0]


def hhmm(t):
    return f"{t // 60:02d}:{t % 60:02d}"


def intersect(sets, other):
    out = []
    for a, b in sets:
        for c, d in other:
            lo, hi = max(a, c), min(b, d)
            if lo <= hi:
                out.append((lo, hi))
    return sorted(out)


def schedule(ev, allowed, durs, order_pairs):
    """Subset DP. allowed[i] = list of (lo,hi) start intervals. Returns {id: start} or None."""
    n = len(ev)
    INF = 10 ** 9
    dp = {0: (DAY0, None, None)}
    pred = {i: 0 for i in range(n)}
    for a, b in order_pairs:
        pred[b] |= 1 << a
    for mask in range(1 << n):
        if mask not in dp:
            continue
        t = dp[mask][0]
        for i in range(n):
            if mask >> i & 1 or (pred[i] & mask) != pred[i]:
                continue
            st = None
            for lo, hi in allowed[i]:
                if hi < t:
                    continue
                s = max(lo, t)
                s = ((s - DAY0 + STEP - 1) // STEP) * STEP + DAY0
                if s <= hi:
                    st = s
                    break
            if st is None:
                continue
            nt = st + durs[i][1]
            nm = mask | 1 << i
            if nm not in dp or dp[nm][0] > nt:
                dp[nm] = (nt, mask, (i, st))
    full = (1 << n) - 1
    if full not in dp:
        return None
    res, m = {}, full
    while m:
        _, pm, (i, st) = dp[m]
        res[ev[i]] = st
        m = pm
    return res


def build(spec):
    events = spec.get("events") if isinstance(spec, dict) else spec
    if not isinstance(events, list):
        return None
    prefs = (spec.get("preferences") or spec.get("prefs") or []) if isinstance(spec, dict) else []
    evs = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        try:
            i = int(e.get("event_id", e.get("id")))
            d = int(float(e.get("duration") or 30))
        except Exception:
            continue
        if e.get("deleted") or e.get("removed"):
            evs.pop(i, None)
            continue
        evs[i] = {"name": str(e.get("event_name") or e.get("name") or f"Event {i}"),
                  "duration": d, "constraint": e.get("constraint")}
    if not evs:
        return None
    ids = sorted(evs)
    idx = {i: k for k, i in enumerate(ids)}
    base = []
    for i in ids:
        lo, hi = hard_window(evs[i]["constraint"], evs[i]["duration"])
        base.append([(lo, hi)] if lo <= hi else [])
    # normalise preferences
    P = []
    for p in prefs if isinstance(prefs, list) else []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("type", "window")).lower()
        tier = p.get("tier", 1)
        n0 = len(P)
        try:
            if "order" in t or "before_event" in t or "after_event" in t:
                a, b = int(p.get("first")), int(p.get("second"))
                if a in idx and b in idx and a != b:
                    P.append(("order", idx[a], idx[b]))
            elif "buffer" in t or "gap" in t:
                e = int(p.get("event_id"))
                if e in idx:
                    P.append(("buffer", idx[e], int(p.get("minutes") or 15)))
            else:
                e = int(p.get("event_id"))
                s, en = parse_time(p.get("start")), parse_time(p.get("end"))
                if e not in idx:
                    continue
                s = DAY0 if s is None else s
                en = DAY1 if en is None else en
                P.append(("avoid" if ("avoid" in t or "not" in t) else "window", idx[e], s, en))
        except Exception:
            continue
        if len(P) > n0:
            P[-1] = P[-1] + (tier,)

    def attempt(chosen, hard_mask):
        allowed = []
        durs = []
        for k, i in enumerate(ids):
            d = evs[i]["duration"]
            pad = sum(p[2] for p in chosen if p[0] == "buffer" and p[1] == k)
            sets = list(base[k]) if hard_mask >> k & 1 else [(DAY0, DAY1 - d)]
            for p in chosen:
                if p[1] != k:
                    continue
                if p[0] == "window":
                    sets = intersect(sets, [(p[2], p[3] - d)])
                elif p[0] == "avoid":
                    sets = intersect(sets, [(DAY0, p[2] - d), (p[3], DAY1)])
            sets = intersect(sets, [(DAY0, DAY1 - d)])
            allowed.append(sets)
            durs.append((d, d + pad))
        # padding must not push past day end for the final event: allow pad overflow by
        # clipping window check on real duration only (sets already use real d)
        orders = [(p[1], p[2]) for p in chosen if p[0] == "order"]
        return schedule(list(range(len(ids))), allowed, durs, orders)

    # previous placements (last assistant calendar) and parser-flagged suspect rules
    prev, suspect = {}, set()
    if isinstance(spec, dict):
        for i, st in (spec.get("prev") or {}).items():
            try:
                i, st = int(i), parse_time(st)
            except Exception:
                continue
            if i in idx and st is not None:
                prev[idx[i]] = st
        for i in spec.get("suspect") or []:
            try:
                if int(i) in idx:
                    suspect.add(idx[int(i)])
            except Exception:
                pass

    def optimise(PP, hard_mask):
        """Lexicographic: lower tier first; tiers <= 1 exhaustively, tier >= 2 greedily.
        Returns (res, {tier: n satisfied})."""
        chosen, got = [], {}
        if attempt([], hard_mask) is None:
            return None, got
        for tier in sorted({p[-1] for p in PP}):
            items = [p[:-1] for p in PP if p[-1] == tier]
            if tier >= 2:
                for p in items:
                    if attempt(chosen + [p], hard_mask) is not None:
                        chosen.append(p)
                        got[tier] = got.get(tier, 0) + 1
                continue
            items = items[:10]
            for r in range(len(items), 0, -1):
                hit = None
                for combo in itertools.combinations(items, r):
                    if attempt(chosen + list(combo), hard_mask) is not None:
                        hit = list(combo)
                        break
                if hit:
                    chosen += hit
                    got[tier] = len(hit)
                    break
        return attempt(chosen, hard_mask), got

    full_hard = (1 << len(ids)) - 1
    res, _ = optimise(P, full_hard)
    if res is None:
        # some rule was misread: find the smallest sets of rules whose removal makes the day feasible,
        # then pick the set that best keeps dropped events where the last calendar had them (that
        # calendar honoured the real rule) and best serves the preferences. Never drop preferences.
        cands = []
        for r in range(1, len(ids) + 1):
            for drop in itertools.combinations(range(len(ids)), r):
                m = full_hard
                for k in drop:
                    m &= ~(1 << k)
                if attempt([], m) is not None:
                    cands.append((drop, m))
            if cands:
                break
        best = None
        for drop, m in cands[:60]:
            # exact previous slot first; else stay on the same side of it (later, then earlier)
            keep = []
            for k in drop:
                if k in prev:
                    d = evs[ids[k]]["duration"]
                    keep += [("window", k, prev[k], prev[k] + d, -1), ("window", k, prev[k], DAY1, -0.9),
                             ("window", k, DAY0, prev[k] + d, -0.9)]
            r_, got = optimise(P + keep, m)
            if r_ is None:
                continue
            tiers = sorted({p[-1] for p in P + keep})
            sc = (sum(k in suspect for k in drop), sum(1 for k in drop if k in prev and r_[k] == prev[k]),
                  len(drop) - sum(1 for k in drop if k not in prev)) + tuple(got.get(t, 0) for t in tiers)
            if best is None or sc > best[0]:
                best = (sc, r_)
        res = best[1] if best else None
    if res is None:
        return None
    out = []
    for k, i in enumerate(ids):
        st = res[k]
        out.append({"event_id": i, "event_name": evs[i]["name"],
                    "start_time": f"{st // 60:02d}:{st % 60:02d}", "duration": evs[i]["duration"]})
    return out




def salvage(text):
    """Recover a spec from truncated / malformed extractor output: collect every complete
    {...} object that looks like an event or a preference."""
    text = strip_think(text)
    evs, prefs = [], []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            o = json.loads(m.group(0))
        except Exception:
            try:
                o = json.loads(re.sub(r",\s*}", "}", m.group(0)))
            except Exception:
                continue
        if not isinstance(o, dict):
            continue
        if "type" in o:
            prefs.append(o)
        elif "event_id" in o or "id" in o:
            evs.append(o)
    # compact list rows: [id, "name", duration, "constraint"]
    for m in re.finditer(r"\[\s*(\d+)\s*,\s*(\"[^\"]*\")\s*,\s*(\d+)\s*(?:,\s*(\"[^\"]*\"|null)\s*)?\]", text):
        evs.append({"event_id": int(m.group(1)), "event_name": json.loads(m.group(2)),
                    "duration": int(m.group(3)), "constraint": json.loads(m.group(4) or "null")})
    if not evs and not prefs:
        return None
    return {"events": evs, "preferences": prefs}


def get_spec(text):
    spec = find_json(text)
    if isinstance(spec, dict) and isinstance(spec.get("events"), list) and spec["events"]:
        spec["events"] = [rowify(e) for e in spec["events"]]
        return spec
    return salvage(text)


def rowify(e):
    if isinstance(e, list) and len(e) >= 3:
        return {"event_id": e[0], "event_name": e[1], "duration": e[2],
                "constraint": e[3] if len(e) > 3 else None}
    return e

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turnparse as tp


def user_times(messages):
    """Clock times the user stated outside preference sentences (hard-constraint grounding)."""
    ts = set()
    for m in messages:
        if m.get("role") == "user":
            for sent, _ in tp.message_sentences(tp.wordtimes(tp.norm(m.get("content", "")))):
                if tp.PREF_CUES.search(sent):
                    continue
                for x in re.finditer(tp.T, sent, re.I):
                    t = tp.tmin(x.group(0))
                    if t is not None:
                        ts.add(t)
    return ts


def merge_prefs(llm_prefs, messages):
    """Rule-read windows for the known fuzzy families replace the LLM's windows for that event;
    LLM windows spanning the whole day are no-ops and dropped; other LLM types are kept."""
    try:
        out = tp.rule_general(messages)
    except Exception:
        out = []
    ruled = {p["event_id"] for p in out if p["type"] in ("window", "avoid")}
    for p in llm_prefs if isinstance(llm_prefs, list) else []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("type", "window")).lower()
        if "window" in t or "avoid" in t or "not" in t or t in ("prefer", "prefer_window"):
            try:
                eid = int(p.get("event_id"))
            except Exception:
                continue
            if eid in ruled:  # the rule-read window wins; an LLM avoid there is usually a flipped window
                continue
            s_, e_ = parse_time(p.get("start")), parse_time(p.get("end"))
            if (s_ is None or s_ <= DAY0) and (e_ is None or e_ >= DAY1):
                continue
        out.append(p)
    return out


def grounded(c, uts):
    """An LLM-written hard constraint counts only if all its clock times occur in user text."""
    if not c:
        return True
    ts = [t for t in times_in(c if isinstance(c, str) else json.dumps(c))]
    return all(t in uts or t in (DAY0, DAY1) for t in ts)


def llm_spec(blackboard):
    for key in reversed(list(blackboard.keys())):
        try:
            spec = get_spec(blackboard[key])
        except Exception:
            spec = None
        if spec:
            return spec
    return {"events": [], "preferences": []}


def finish(spec, messages):
    cal = None
    try:
        cal = build(spec) if spec.get("events") else None
    except Exception:
        cal = None
    if cal is None:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                j = find_json(m.get("content", ""))
                if isinstance(j, list):
                    return {"text": json.dumps(j)}
        return {"text": "[]"}
    return {"text": json.dumps(cal, ensure_ascii=False)}


def pref_list(blackboard):
    """Preference objects from the preference reader's output (a JSON list or {"preferences": [...]})."""
    txt = strip_think(blackboard.get("prefs") or "")
    j = find_json(txt)
    if isinstance(j, dict):
        j = j.get("preferences") or j.get("prefs") or []
    if not isinstance(j, list):
        sv = salvage(txt)
        j = (sv or {}).get("preferences") or []
    return [p for p in j if isinstance(p, dict)]


def run(blackboard, messages):
    # no full-transcript extractor: events, durations and hard rules come from turnparse
    # (+ the last assistant calendar); the LLM only reads preference sentences
    spec = {"events": [], "preferences": pref_list(blackboard)}
    try:
        rx = tp.parse(messages)
    except Exception:
        rx = {}
    try:
        acal = tp.last_assistant_calendar(messages)
    except Exception:
        acal = {}
    uts = user_times(messages)
    evs = {}
    for e in spec.get("events") or []:
        if not isinstance(e, dict):
            continue
        try:
            i = int(e.get("event_id", e.get("id")))
        except Exception:
            continue
        evs[i] = dict(e, event_id=i)
    for i, a in acal.items():  # events already on the calendar that the user text never touched
        if i not in rx:
            evs.setdefault(i, {"event_id": i, "event_name": a.get("name"), "duration": a.get("duration"),
                               "constraint": None})
    for i, r in rx.items():
        if r["removed"]:
            evs.pop(i, None)
            continue
        e = evs.setdefault(i, {"event_id": i, "event_name": None, "duration": None, "constraint": None})
        if r["duration"]:
            e["duration"] = r["duration"]
        if r["constraint"]:
            e["constraint"] = r["constraint"]
    # history check: the assistant calendar that answered the turn stating a rule honoured the real
    # rule; if it breaks the parsed one, the parse is wrong -> drop it, hold the event near its last slot
    keep_prev = set()
    for i, r in rx.items():
        if i not in evs or not r.get("constraint") or r.get("c_at") is None:
            continue
        nxt = tp.assistant_calendar_after(messages, r["c_at"]).get(i)
        if not nxt or parse_time(nxt.get("start")) is None:
            continue
        d = int(nxt.get("duration") or 0)
        if r.get("duration") and int(r["duration"]) != d:
            continue
        lo, hi = hard_window(r["constraint"], d)
        if not lo <= parse_time(nxt["start"]) <= hi:
            r["suspect"] = True
            evs[i]["constraint"] = None
            keep_prev.add(i)
    for i, e in evs.items():
        if not grounded(e.get("constraint"), uts):
            e["constraint"] = None
        a = acal.get(i, {})
        if not e.get("duration"):
            e["duration"] = a.get("duration") or 30
        if not e.get("event_name"):
            e["event_name"] = a.get("name") or (rx.get(i) or {}).get("name") or f"Event {i}"
    prefs = merge_prefs(spec.get("preferences") or [], messages)
    for i, r in rx.items():
        pc = r.get("pref_constraint")
        if i in evs and pc:
            d = int(evs[i]["duration"])
            lo, hi = hard_window(pc, d)
            if lo <= hi:
                prefs.append({"type": "window", "event_id": i, "start": hhmm(lo), "end": hhmm(hi + d), "tier": 0})
    # tier 2: keep each event where the assistant last put it (that calendar already honoured
    # the earlier rules), unless that clashes with anything above; recovers rules the parser missed
    # or misread. Applies to events with a parsed rule too: a stale slot that breaks a new rule is
    # simply infeasible and skipped.
    for i, a in acal.items():
        st = parse_time(a.get("start"))
        if i in evs and st is not None and int(a.get("duration") or 0) == int(evs[i]["duration"]):
            prefs.append({"type": "window", "event_id": i, "start": hhmm(st),
                          "end": hhmm(st + int(evs[i]["duration"])), "tier": 2})
    for i in keep_prev:
        a = acal.get(i) or {}
        st = parse_time(a.get("start"))
        if st is not None and int(a.get("duration") or 0) == int(evs[i]["duration"]):
            d = int(evs[i]["duration"])
            prefs += [{"type": "window", "event_id": i, "start": hhmm(st), "end": hhmm(st + d), "tier": -1},
                      {"type": "window", "event_id": i, "start": hhmm(st), "end": hhmm(DAY1), "tier": -0.9},
                      {"type": "window", "event_id": i, "start": hhmm(DAY0), "end": hhmm(st + d), "tier": -0.9}]
    prev = {i: a.get("start") for i, a in acal.items()
            if i in evs and int(a.get("duration") or 0) == int(evs[i]["duration"])}
    spec = {"events": list(evs.values()), "preferences": prefs, "prev": prev,
            "suspect": [i for i, r in rx.items() if r.get("suspect")]}
    return finish(spec, messages)
