"""Deterministic per-turn reader of the raw chat: hard constraints, durations, removals, names.

Walks the user messages in order; every sentence is attributed to the event id it mentions
("(event id: N)", "event N"), or to the last id mentioned earlier in the same message.
Soft-preference sentences (ideally / prefer / would be perfect / ...) are ignored here.
Returns {id: {"duration", "constraint", "name", "removed", "seen"}}; a later turn overrides
an earlier one, so the result is the state after the final turn.
"""
import json
import re

T = r"(?:(?<![a-z])noon|(?<![a-z])midday|\d{1,2}(?::\d{2}|\.\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"
ID_RE = re.compile(r"\(?\s*event[\s_-]*(?:id)?\s*[:#]?\s*(\d+)\s*\)?", re.I)
PREF_CUES = re.compile(
    r"\b(ideal|ideally|rather|if that works|happy if|prefer|preferably|would be perfect|suits me|if you can|if possible|i like|i'd love|"
    r"i would love|sharpest|mornings are for|works best|best for me|would ideally|happens toward|"
    r"kind of thing|nice to have|if it works|i'd rather|i would rather|bonus|would be great|would be nice|"
    r"somewhere around|sit somewhere|i'd love|i would love|i'd like it to happen|land in)\b", re.I)
REMOVE_RE = re.compile(r"\b(remove|delete|cancel|drop|scrap|take (?:it |that )?off|get rid of|no longer need)\b", re.I)
CLEAR_RE = re.compile(r"\b(any ?time (?:is|works)|no (?:time )?(?:constraint|restriction)|whenever|flexible|"
                      r"remove the (?:time )?(?:constraint|restriction|requirement))\b", re.I)
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
       "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45, "forty five": 45,
       "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "an": 1, "a": 1, "half an": 0.5, "half": 0.5}


def norm(s):
    s = str(s or "")
    for ch in "    ":
        s = s.replace(ch, " ")
    for ch in "‐‑‒–—−":
        s = s.replace(ch, "-")
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def tmin(s):
    s = s.strip().lower().replace(".", ":") if re.match(r"^\d{1,2}\.\d{2}", s.strip()) else s.strip().lower()
    if s in ("noon", "midday"):
        return 720
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if h > 23 or mi > 59:
        return None
    if ap == "p" and h < 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 7:
        h += 12
    return h * 60 + mi


def fmt(t):
    h, m = divmod(t, 60)
    ap = "am" if h < 12 else "pm"
    h12 = h if h <= 12 else h - 12
    return f"{h12}:{m:02d}{ap}"


DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?|half an|an|a|one|two|three|fifteen|twenty|thirty|forty[- ]five|forty|fifty|sixty|ninety)"
    r"\s*-?\s*(?:minutes?|mins?|hours?|hrs?|h)\b(?:\s*(?:and\s*)?(?:a\s*half|(\d+)\s*(?:minutes?|mins?)))?", re.I)


def durations(s):
    out = []
    for m in DUR_RE.finditer(s):
        q = m.group(1).lower()
        v = float(q) if re.match(r"\d", q) else NUM.get(q.replace("-", " ") if q not in NUM else q, None)
        if v is None:
            continue
        unit = m.group(0).lower()
        if re.search(r"\b(hours?|hrs?|h)\b", unit):
            v *= 60
            if "half" in unit[len(m.group(1)):]:
                v += 30
            if m.group(2):
                v += int(m.group(2))
        if 5 <= v <= 360:
            out.append((m.start(), m.end(), int(v)))
    return out


def _blank_durations(s):
    for a, b, _ in durations(s):
        s = s[:a] + " " * (b - a) + s[b:]
    return s


def constraint(s):
    """Hard constraint from one sentence fragment, or None."""
    s = " " + s.lower() + " "
    m = re.search(rf"(?:between|from)\s+({T})\s*(?:and|to|-|until|till)\s*({T})", s) or \
        re.search(rf"\b({T})\s*(?:-|to)\s*({T})\b", s)
    if m:
        a, b = tmin(m.group(1)), tmin(m.group(2))
        if a is not None and b is not None and a < b:
            return f"between {fmt(a)} and {fmt(b)}"
    pats = [
        ("after", rf"(?:at or after|no earlier than|not before|any ?time after|after|later than|from|starting)\s+({T})(?!\s*(?:-|to)\s*\d)"),
        ("after", rf"({T})\s+or\s+later"),
        ("after", rf"({T})\s+(?:onward|onwards)"),
        ("before", rf"(?:at or before|no later than|before|by|until|till|ends? by|wrap(?:ped)? up by|done by|finish(?:es|ed)? by)\s+({T})"),
        ("at", rf"(?:at|for|to|into|@)\s+(?:a\s+|an\s+|the\s+)?({T})"),
        ("at", rf"({T})\s+(?:slot|start|sharp|kick-?off)\b"),
    ]
    found = []
    for kind, p in pats:
        for m in re.finditer(p, s):
            t = tmin(m.group(1))
            if t is None or not (480 <= t <= 1080):
                continue
            raw = m.group(1)
            if not re.search(r"[ap]\.?m|:|\.\d|noon|midday", raw.lower()) and kind == "at":
                # bare number after "for/to" is usually a quantity; "at 3" is a clock time
                if not re.search(r"(?:\bat|@)\s+$", s[:m.start(1)]) or re.match(r"\d+\s*%", s[m.start(1):]):
                    continue
            kind_ = kind
            found.append((m.start(), kind_, t))
    if not found:
        return None
    # "before" / "after" / "at" priority: an explicit relation word beats plain "at"
    for k in ("after", "before"):
        hits = [f for f in found if f[1] == k]
        if hits:
            return f"{k} {fmt(hits[0][2])}"
    return f"at {fmt(found[0][2])}"


SENT_RE = re.compile(r"(?<![ap]\.m\.)(?<=[.!?])\s+(?=[A-Za-z\"'(])|\n+")


def name_guess(frag):
    m = re.search(r'"([^"]{3,})"', frag)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:add|schedule|book|put|slot in|slot|include|set up|squeeze in)\s+(?:a |an |the |my |in )?(.+?)\s*(?:\(|$)", frag, re.I)
    if m:
        n = re.sub(r"\s*\(\s*\d+\s*-?\s*min\w*\s*\)", "", m.group(1)).strip(" ,.?!")
        n = re.sub(r"^\d+\s*-?\s*(?:minute|min|hour)s?\s+", "", n, flags=re.I)
        return n or None
    return None


def good_name(n):
    return bool(n) and len(n) >= 3 and not re.search(r"event id|^(it|that|this|sometime|somewhere)\b|\d\s*[ap]\.?m\b", n, re.I)


HOURW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
MINW = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30, "forty-five": 45, "forty five": 45}
_HW = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
_AP = r"(\s*(?:a\.?m\.?|p\.?m\.?|in the morning|in the afternoon))?"


def _hh(w):
    return int(w) if w.isdigit() else HOURW[w.lower()]


def _ap(a, h):
    a = (a or "").lower()
    if "a" in a[:3] or "morning" in a:
        return "am"
    if "p" in a[:3] or "afternoon" in a:
        return "pm"
    return "am" if 8 <= h <= 11 else "pm"


def wordtimes(s):
    """Rewrite spelled-out clock times ("half past ten", "quarter to two", "eleven o'clock",
    "two thirty pm") as digits so the regexes below can read them."""
    def past(m):
        h = _hh(m.group(2))
        mi = 30 if m.group(1).lower() == "half" else 15
        return f"{h}:{mi:02d}{_ap(m.group(3), h)}"

    def to(m):
        h = _hh(m.group(1)) - 1 or 12
        return f"{h}:45{_ap(m.group(2), h)}"

    def hm(m):
        h = _hh(m.group(1))
        mi = MINW[m.group(2).lower().replace("-", " ") if m.group(2).lower() not in MINW else m.group(2).lower()]
        return f"{h}:{mi:02d}{_ap(m.group(3), h)}"
    s = re.sub(rf"\b(half|quarter)\s+past\s+{_HW}\b{_AP}", past, s, flags=re.I)
    s = re.sub(rf"\bquarter\s+(?:to|till|of)\s+{_HW}\b{_AP}", to, s, flags=re.I)
    s = re.sub(rf"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(o'clock|oclock|fifteen|thirty|forty[- ]five)\b(?!\s*-?\s*min){_AP}",
               hm, s, flags=re.I)
    s = re.sub(rf"\b(\d{{1,2}})\s+(o'clock|oclock)\b{_AP}", hm, s, flags=re.I)
    s = re.sub(r"\b(?<!-)(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(a\.?m\.?|p\.?m\.?)(?=\W|$)",
               lambda m: f"{_hh(m.group(1))}{m.group(2)}", s, flags=re.I)
    s = re.sub(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+in the (morning|afternoon)\b",
               lambda m: f"{_hh(m.group(1))}" + ("am" if m.group(2).lower() == "morning" else "pm"), s, flags=re.I)
    s = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s+in the (morning|afternoon)\b",
               lambda m: m.group(1) + ("am" if m.group(2).lower() == "morning" else "pm"), s, flags=re.I)
    s = re.sub(r"\b(at|by|before|after|until|till|around|from|to)\s+(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
               r"(?!\s*-?\s*(?:minutes?|mins?|hours?|hrs?|people|of|more|other|times?|days?|weeks?|events?|sessions?|meetings?|point|thing|things|items?|slots?))",
               lambda m: f"{m.group(1)} {_hh(m.group(2))}{_ap(None, _hh(m.group(2)))}" if m.group(2).lower() != "twelve"
               else f"{m.group(1)} 12pm", s, flags=re.I)
    return s


ONLY_ID_RE = re.compile(r"^\s*\(?\s*event[\s_-]*(?:id)?\s*[:#]?\s*\d+\s*\)?\s*[.!?]?\s*$", re.I)


def sentences(text):
    """Split into sentences; a trailing "(event id: N)" that follows the sentence's own
    full stop / question mark is glued back onto that sentence."""
    out = []
    for sent in SENT_RE.split(text):
        if not sent or not sent.strip():
            continue
        if out and ONLY_ID_RE.match(sent):
            out[-1] = out[-1] + " " + sent.strip()
        else:
            out.append(sent)
    return out


def message_sentences(text):
    """[(sentence, target)]: a sentence without an id inherits the last id seen earlier in the
    message, or else (at the start of a message) the first id of the very next sentence."""
    sents = sentences(text)
    ids = [[int(m.group(1)) for m in ID_RE.finditer(x)] for x in sents]
    out, cur = [], None
    for k, x in enumerate(sents):
        if ids[k]:
            cur = ids[k][-1]
        tgt = cur
        if tgt is None:
            tgt = ids[k + 1][0] if k + 1 < len(ids) and ids[k + 1] else None
        out.append((x, tgt))
    return out


def parse(messages):
    ev = {}
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        text = wordtimes(norm(msg.get("content", "")))
        for sent, cur in message_sentences(text):
            ids = [(m.start(), m.end(), int(m.group(1))) for m in ID_RE.finditer(sent)]
            is_pref = bool(PREF_CUES.search(sent))
            # fragments: text before the 2nd id belongs to the 1st id, etc.
            if len({i[2] for i in ids}) > 1:
                cuts = [i[0] for i in ids[1:]] + [len(sent)]
                frags, start = [], 0
                for k, i in enumerate(ids):
                    frags.append((i[2], sent[start:cuts[k]]))
                    start = cuts[k]
            else:
                tgt = ids[0][2] if ids else cur
                if tgt is None:
                    continue
                frags = [(tgt, sent)]
            for i, frag in frags:
                clean = ID_RE.sub(" ", frag)
                e = ev.setdefault(i, {"duration": None, "constraint": None, "name": None,
                                      "removed": False, "seen": False, "pref_constraint": None})
                e["seen"] = True
                adds = bool(re.search(r"\b(add|schedule|book|put|include|slot)\b", clean, re.I))
                if is_pref and not adds:
                    # soft sentence about an existing event: only an explicit clock time is read
                    c = constraint(_blank_durations(clean))
                    if c:
                        e["pref_constraint"] = c
                    continue
                if REMOVE_RE.search(clean) and not re.search(r"\b(add|schedule|book)\b", clean, re.I):
                    e["removed"] = True
                    continue
                if re.search(r"\b(add|schedule|book|put|include|slot)\b", clean, re.I):
                    e["removed"] = False
                ds = durations(clean)
                if ds:
                    v = ds[-1][2]
                    if re.search(r"\b(extend|lengthen|add(?:ing)? another|longer|increase)\w*\b.*\bby\b|\banother\b|\bextra\b", clean, re.I):
                        v = e["duration"] + v if e["duration"] else None
                    elif re.search(r"\b(shorten|cut|trim|reduce|shorter)\w*\b.*\bby\b", clean, re.I):
                        v = e["duration"] - v if e["duration"] else None
                    if v:
                        e["duration"] = v
                    else:
                        e["dur_relative"] = True
                    for a, b, _ in ds:  # do not let "30 minutes" look like a time
                        clean = clean[:a] + " " * (b - a) + clean[b:]
                c = constraint(clean)
                if is_pref:
                    # "I'd love to lock in X for 1 pm": an explicit clock time stated with a soft cue.
                    # Kept as a top-priority wish so it never overrides a real hard rule.
                    if c:
                        e["pref_constraint"] = c
                elif c:
                    e["constraint"] = c
                    e["c_at"] = mi  # message index where the rule was (last) stated
                    e["pref_constraint"] = None
                elif CLEAR_RE.search(clean) and not is_pref:
                    e["constraint"] = None
                if e["name"] is None:
                    g = name_guess(frag)
                    e["name"] = g if good_name(g) else None
    return ev


def find_list_json(text):
    """Last JSON list of event dicts in a text (assistant calendars)."""
    text = str(text or "")
    if "</think>" in text:
        text = text.split("</think>")[-1]
    best = None
    for m in re.finditer(r"\[", text):
        depth = 0
        for j in range(m.start(), len(text)):
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[m.start():j + 1])
                        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
                            best = obj
                    except Exception:
                        pass
                    break
    return best


def assistant_calendar_after(messages, k):
    """First assistant calendar after message index k: {id: {"name", "duration", "start"}}."""
    for msg in messages[k + 1:]:
        if msg.get("role") == "assistant":
            cal = find_list_json(msg.get("content", ""))
            if cal:
                out = {}
                for x in cal:
                    try:
                        out[int(x.get("event_id"))] = {"name": x.get("event_name"),
                                                        "duration": int(x.get("duration")),
                                                        "start": x.get("start_time")}
                    except Exception:
                        pass
                return out
    return {}


def last_assistant_calendar(messages):
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            cal = find_list_json(msg.get("content", ""))
            if cal:
                out = {}
                for x in cal:
                    try:
                        out[int(x.get("event_id"))] = {"name": x.get("event_name"),
                                                        "duration": int(x.get("duration")),
                                                        "start": x.get("start_time")}
                    except Exception:
                        pass
                return out
    return {}


# ---------------------------------------------------------------- soft preferences
# Phrase families -> windows (spec_A); checked in order, first hit wins per clause.
PREF_FAMILIES = [
    (r"around lunch|lunchtime|lunch time|\bmidday\b|middle of the day|around noon", ("window", "11:30", "13:30")),
    (r"late in the day|end of (?:my|the) day|toward(?:s)? the end|wind[- ]?down|late afternoon|close of (?:the )?day|last thing",
     ("window", "14:00", "16:00")),
    (r"early morning|first thing|before noon|before lunch|early in the day|\bearly\b|in the morning|morning person|start of (?:my|the) day",
     ("window", "10:00", "12:00")),
    (r"not in the morning|mornings are for|afternoon|after lunch|later in the day", ("window", "12:00", "16:00")),
]
SPLIT_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\s*\n+\s*")


def pref_sentences(messages):
    """[(event_id or None, sentence)] for every user sentence carrying a soft-preference cue."""
    out = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = norm(msg.get("content", ""))
        for sent, cur in message_sentences(text):
            if not PREF_CUES.search(sent) and not re.search(r"\b(could|can) the .* sit somewhere\b|\bsomewhere around\b", sent, re.I):
                continue
            ids = [int(m.group(1)) for m in ID_RE.finditer(sent)]
            out.append((ids[-1] if ids else cur, sent.strip()))
    return out


def rule_prefs(messages):
    """Deterministic windows for the known fuzzy families; [(event_id, start, end)]."""
    res = []
    for i, sent in pref_sentences(messages):
        if i is None:
            continue
        low = sent.lower()
        if re.search(r"\b(avoid|not|never|don't|do not|keep .* free)\b", low) and not re.search(r"mornings are for", low):
            continue  # negative phrasing: leave it to the LLM
        for pat, (_, a, b) in PREF_FAMILIES:
            if re.search(pat, low):
                if (i, a, b) not in res:
                    res.append((i, a, b))
                break
    return res


# ---------------------------------------------------------------- general soft preferences (no LLM)
# Relative day-part words -> windows; finer phrases first. Negated phrasing flips a window into an avoid.
DAYPARTS = [
    (r"early afternoon|right after lunch|just after lunch", ("10:00", "16:00"), ("12:00", "14:00")),
    (r"mid[- ]?morning", None, ("10:30", "11:30")),
    (r"mid[- ]?afternoon", None, ("13:00", "15:00")),
    (r"late morning", None, ("11:00", "12:00")),
] + [(p, None, (a, b)) for p, (_, a, b) in PREF_FAMILIES]
NEG_RE = re.compile(r"\b(avoid|not|never|don't|do not|rather not|no meetings?|keep .{0,40}(?:free|clear|open))\b", re.I)
LUNCH_FREE = re.compile(r"\b(keep|protect|leave|block|avoid|not|don't|do not)\b.{0,40}\blunch\b|\blunch\b.{0,30}\b(free|clear|open|off)\b", re.I)
BUFFER_RE = re.compile(r"\b(breathing room|buffer|breather|gap|cushion|break|space|wiggle room|time to (?:reset|recover|decompress|prep))\b", re.I)
SEQ_RE = re.compile(r"\b(before|after|ahead of|prior to|followed by|then)\b", re.I)


def _named_ids(sent):
    return [int(m.group(1)) for m in ID_RE.finditer(sent)]


def rule_general(messages):
    """[pref dict] in the solver's general forms (window / avoid / order / buffer), read from the
    user's soft-preference sentences without an LLM. Earlier families win per sentence."""
    out = []
    for i, sent in pref_sentences(messages):
        low = sent.lower()
        ids = _named_ids(sent)
        if i is None and not ids:
            continue
        tgt = i if i is not None else ids[-1]
        # relation between two named events: "X (event id: 2) before the review (event id: 5)"
        if len(set(ids)) >= 2:
            m = SEQ_RE.search(ID_RE.sub(" ", low[ID_RE.search(sent).end():]) if ID_RE.search(sent) else low)
            if m:
                a, b = ids[0], ids[1]
                w = m.group(1).lower()
                first, second = (a, b) if w in ("before", "ahead of", "prior to", "followed by", "then") else (b, a)
                out.append({"type": "order", "first": first, "second": second})
                continue
        if LUNCH_FREE.search(low) and not re.search(r"around lunch|lunchtime|over lunch|at lunch", low):
            out.append({"type": "avoid", "event_id": tgt, "start": "12:00", "end": "13:00"})
            continue
        if BUFFER_RE.search(low) and re.search(r"\b(after|following|afterward|afterwards|between)\b", low):
            ms = durations(low)
            out.append({"type": "buffer", "event_id": tgt, "minutes": ms[0][2] if ms else 15})
            continue
        neg = bool(NEG_RE.search(low)) and not re.search(r"mornings are for", low)
        for pat, _, (a, b) in DAYPARTS:
            if re.search(pat, low):
                if neg:
                    out.append({"type": "avoid", "event_id": tgt, "start": a, "end": b})
                else:
                    out.append({"type": "window", "event_id": tgt, "start": a, "end": b})
                break
    return out
