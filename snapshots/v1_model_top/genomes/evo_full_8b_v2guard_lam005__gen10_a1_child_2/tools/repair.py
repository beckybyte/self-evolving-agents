"""Deterministic repair + preference optimisation of the LLM's calendar.

Starting point is the LLM's answer (event set, names, positions). We then
  * fix the event set: "revert to Turn N" rebuilds from that turn's calendar; events of the
    previous calendar the user did not delete are re-added;
  * lock durations of existing events to the previous calendar unless the user asked to resize;
  * recover hard constraints by regex from user messages (latest mention wins; wish-like
    sentences are skipped) and soft preferences from wish-like sentences (phrase lexicon,
    generic soft time expressions, plus optional LLM-extracted PREFS);
  * search for the placement that moves the fewest events (previous-calendar positions are
    known-legal anchors) while honouring as many preferences as possible;
  * if the parsed constraints are infeasible, degrade gracefully instead of returning an
    overlapping calendar.
If the LLM produced no JSON calendar (chit-chat turn), the previous calendar is used.
"""
import json
import re
import time

WIN_START, WIN_END, STEP = 600, 960, 5
PREF_PEN = 2.6          # one unmet preference ~ moving two events whose constraints we know
MOVE_NEW = 0.15         # moving an event added this turn (constraint known from its own message)
MOVE_KNOWN = 0.25       # moving an old event whose parsed constraint its previous slot satisfies;
                        # ~ P(misparse) x 1.0 hard loss vs 0.25 soft loss of an unmet pref
HULL_EXT = 0.5          # unknown-constraint event moved outside its known-legal hull but within the LLM's spot
HULL_OUT = 1.8          # ... moved outside both (direction of its hidden constraint is a coin flip)
TRUST_LLM = False       # True: unknown-constraint events keep the LLM's spot instead of their previous one
USE_LLM_CONS = False    # True: read a 'CONS:' JSON list of per-event constraints from the LLM output
INFER_DIR = True        # unknown-constraint events: infer a one-sided safe bound from direction words
DIST_UNK = 1 / 300.0    # per-minute cost of moving an unknown-constraint event away from its legal hull
DIST_OUT = 1 / 75.0     # extra per-minute cost beyond the hull widened by the LLM's spot: in packed days
                        # (train-1005, v2-1254, v2-272) repair carried such events 25-150 min the wrong way
T0 = time.time()
WISH = {}            # {event_id: unary prefs} of the current turn, read by move_cost
WISH_MOVE = 1.0     # cost of moving an unknown-constraint event into its own wished window


# ---------------------------------------------------------------- parsing helpers
def strip_think(text):
    text = text or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:
        text = text.split("<think>")[0]
    return text


def json_lists(text):
    """All parseable JSON lists of dicts in text (in order)."""
    out = []
    for m in re.finditer(r"\[", text):
        start = m.start()
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        val = json.loads(text[start:i + 1])
                    except Exception:
                        break
                    if isinstance(val, list) and all(isinstance(e, dict) for e in val):
                        out.append(val)
                    break
    return out


def last_json_list(text):
    """Return the last JSON calendar (list of events with start_time) in text, or None."""
    best = None
    for val in json_lists(strip_think(text)):
        if val and all("event_id" in e and "start_time" in e for e in val):
            best = val
    return best


def hm_to_min(s):
    s = str(s).strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 8:  # 1:30 meaning 13:30
            h += 12
        return h * 60 + mi
    return parse_time(s)


TIME_RE = r"(noon|midday|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2})"


def parse_time(tok):
    tok = tok.strip().lower().replace(".", "")
    if tok in ("noon", "midday"):
        return 720
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", tok)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif ap is None and h < 8:
        h += 12
    if not (8 * 60 <= h * 60 + mi <= 17 * 60):
        return None
    return h * 60 + mi


SOFT = re.compile(r"\b(ideally|ideal|prefer|preferably|preference|would love|i'd love|love it|"
                  r"would be (?:perfect|great|nice|ideal|best)|be perfect|if possible|"
                  r"works best|suits? me|i like to|i tend to|rather|my mornings|my afternoons|"
                  r"if you can|nice to|happy if|bonus|sharpest|somewhere around|kind of thing|"
                  r"fresh mind|my energy|i'm at my|i focus|i'd like it to be (?:early|late))\b", re.I)


def norm(s):
    return s.replace("‑", "-").replace("‐", "-").replace("–", "-") \
            .replace("—", "-").replace("’", "'")


BARE = r"(\d{1,2}(?::\d{2})?)(?:\s*o'?clock)?(?!\s*(?:min|hour|hr|h\b|people|person|%|\d|[ap]\.?m))"
TB = r"(" + TIME_RE[1:-1] + r"|\d{1,2})"   # time, or bare hour (only used next to a real time)


NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
NUMW_RE = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
MINW = {"oh five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
        "forty[- ]five": 45, "fifty": 50}


def words_to_digits(low):
    """'half past two', 'two thirty', 'three pm', 'by four', 'between two and four' -> digits.
    Only number words next to clock vocabulary are touched ('one-on-one', 'one more' stay)."""
    w = lambda m, g=1: str(NUMW[m.group(g)])
    low = re.sub(r"\b(half (?:past|after)|(?:a )?quarter (?:past|after|to|till|til|of|before)) " + NUMW_RE + r"\b",
                 lambda m: m.group(1) + " " + w(m, 2), low)
    for pat, mi in MINW.items():
        low = re.sub(r"\b" + NUMW_RE + r"[ -]" + pat + r"\b", lambda m, mi=mi: "%s:%02d" % (w(m), mi), low)
    low = re.sub(r"\b" + NUMW_RE + r"(?=\s*(?:o'?clock|[ap]\.?m\b|in the (?:morning|afternoon|evening)))",
                 lambda m: w(m), low)
    low = re.sub(r"\b(at|by|before|after|until|till|from|between|past|than) " + NUMW_RE +
                 r"(?=\s*(?:$|[,.;!?)]|and |to |until |till |or (?:later|earlier|after|before)|sharp|at the|onwards?))",
                 lambda m: m.group(1) + " " + w(m, 2) + ":00", low)
    low = re.sub(r"\b((?:between|from) \d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?\s*(?:and|to|until|till|-)\s*)" + NUMW_RE +
                 r"\b(?!\s*(?:people|persons?|guests|of|more|other|hours?|minutes?|mins?|days?|weeks?|times?))",
                 lambda m: m.group(1) + w(m, 2) + ":00", low)
    return low


def canon_times(low):
    """Rewrite spoken clock phrases into forms TIME_RE understands."""
    low = words_to_digits(low)
    def hh(h):
        return int(h) if int(h) <= 12 else int(h) - 12
    low = re.sub(r"\bhalf (?:past|after) (\d{1,2})\b", lambda m: "%d:30" % hh(m.group(1)), low)
    low = re.sub(r"\b(?:a )?quarter (?:past|after) (\d{1,2})\b", lambda m: "%d:15" % hh(m.group(1)), low)
    low = re.sub(r"\b(?:a )?quarter (?:to|till|til|of|before) (\d{1,2})\b",
                 lambda m: "%d:45" % ((hh(m.group(1)) - 2) % 12 + 1), low)
    low = re.sub(r"\b12 ?noon\b", "noon", low)
    low = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s*(?:o'?clock\s*)?in the (?:afternoon|evening)\b", r"\1pm", low)
    low = re.sub(r"\b(\d{1,2}(?::\d{2})?)\s*(?:o'?clock\s*)?in the morning\b", r"\1am", low)
    low = re.sub(r"\b(\d{1,2})\s*o'?clock\b", r"\1:00", low)
    return low


def sentence_constraint(s):
    """Return a constraint tuple ('at'|'before'|'after'|'between', a, b) or None."""
    low = canon_times(norm(s.lower()))
    T = TIME_RE
    m = re.search(r"between\s+" + T + r"\s+(?:and|to|-)\s+" + T, low) or \
        re.search(r"from\s+" + T + r"\s+(?:to|until|till|-)\s+" + T, low) or \
        re.search(T + r"\s*-\s*" + T, low) or \
        re.search(r"(?:between|from)\s+" + TB + r"\s+(?:and|to|until|till|-)\s+" + T, low) or \
        re.search(r"(?:between|from)\s+" + T + r"\s+(?:and|to|until|till|-)\s+" + TB + r"\b", low)
    if m:
        a, b = parse_time(m.group(1)), parse_time(m.group(2))
        if a is not None and b is not None and a < b:
            return ("between", a, b)
    AFTER = r"(?:at or after|no earlier than|not earlier than|no sooner than|not before|any ?time after|" \
            r"(?<!no )(?<!not )later than|after|past|from|starting|beginning)"
    m = re.search(AFTER + r"\s+" + T, low) \
        or re.search(T + r"\s+or (?:any ?time )?(?:later|after)", low) \
        or re.search(T + r"\s+(?:or|and) (?:beyond|onwards?)", low) \
        or re.search(T + r"\s+(?:onwards?|at the earliest)", low) \
        or re.search(r"(?:not before|no earlier than|any ?time after|(?<!no )(?<!not )later than)\s+" + BARE, low)
    if m and not re.search(r"(?:finish|end|wrap|done|complete)\w*\s+(?:up\s+)?(?:by|before)", low):
        t = parse_time(m.group(1))
        if t is not None:
            return ("after", t, None)
    m = re.search(r"(?:no later than|earlier than|prior to|ahead of|before|by|until|till|ends? by|"
                  r"finish\w* by|wrap\w* up by|done by)\s+" + T, low) \
        or re.search(r"(?:no later than|wrap\w* up by|finish\w* by|done by)\s+" + BARE, low)
    if m:
        t = parse_time(m.group(1))
        if t is not None:
            return ("before", t, None)
    m = re.search(r"(?:at|for|to|@)\s+(?:an?\s+|the\s+)?" + T, low)
    if m:
        t = parse_time(m.group(1))
        if t is not None and not re.search(r"or (?:earlier|so|around)|around|about|roughly|approx", low):
            return ("at", t, None)
    return None


HINT_RE = re.compile(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m|p\.?m)|\d{1,2}:\d{2}|noon|midday|morning|afternoon|"
                     r"lunch|evening|o'?clock|quarter|half past|end of|start of|beginning of|early|earlier|"
                     r"late|later|before|after|until|till|no sooner|at the latest|at the earliest|wrap)", re.I)


def time_hint(s):
    s = re.sub(r"event[\s_-]*id\s*[:#=]?\s*\d+", " ", norm(s), flags=re.I)
    s = DUR_RE.sub(" ", s)
    return bool(HINT_RE.search(s))


ABBR = re.compile(r"(?<![.\w])(Dr|Mr|Mrs|Ms|Mx|Prof|St|Jr|Sr|Mt|Ft|Capt|Lt|Col|Gen|Sgt|Rev|Hon|vs|approx|dept|e\.g|i\.e|[A-Z])\.(?=\s)")


def split_sentences(text):
    # "sync with Dr. Patel (event id: 0) ..." must stay one sentence (oodA-1771 lost its preference)
    text = ABBR.sub(lambda m: m.group(1) + "\u2024", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in parts if p.strip()]


STOP = {"event", "could", "would", "please", "thanks", "calendar", "minute", "minutes",
        "session", "meeting", "with", "schedule", "move", "start", "that", "this", "slot"}


def stem(w):
    for suf in ("ing", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    return w[:5]


def content_words(s):
    return {stem(w) for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP} - STOP


def ids_in(s, names):
    """Event ids referred to in sentence s; (ids, by_name)."""
    ids = [int(x) for x in re.findall(r"event[\s_-]*id\s*[:#=]?\s*(\d+)", s, re.I)]
    if not ids:
        ids = [int(x) for x in re.findall(r"\bevent\s+#?(\d+)\b", s, re.I)]
    if ids:
        return ids, False
    ws = content_words(s)
    scores = sorted(((len(ws & content_words(n)), i) for i, n in names.items()), reverse=True)
    if scores and scores[0][0] >= 1 and (len(scores) == 1 or scores[0][0] > scores[1][0]):
        return [scores[0][1]], True
    return [], True


PRON = re.compile(r"^\W*(?:and |but |also |oh,? |please |so )?(?:make sure |ensure |note that )?"
                  r"(?:it|this|that one|this one|the (?:meeting|session|event|call|class|appointment|slot|one))\b",
                  re.I)


def collect_constraints(user_msgs, names, when=None):
    """names: {event_id: name}. Returns {event_id: constraint or None} for mentioned events.
    If `when` is a dict it receives {event_id: index of the user message that set the constraint}."""
    cons = {}
    for k, text in enumerate(user_msgs):
        prev_eid = None
        for s in split_sentences(text):
            ids, by_name = ids_in(s, names)
            if prev_eid is not None and (not ids or by_name) and PRON.search(norm(s)):
                ids, by_name = [prev_eid], True   # "It needs to end by 2pm." -> event of previous sentence
            prev_eid = ids[0] if len(set(ids)) == 1 else None
            if SOFT.search(s):
                continue
            if len(set(ids)) != 1:
                continue
            eid = ids[0]
            c = sentence_constraint(s)
            if c is not None:
                cons[eid] = c
                if when is not None:
                    when[eid] = k
            elif not by_name and re.search(r"\b(add|create|new|slot|put|book|include)\b", s, re.I) and eid not in cons \
                    and not time_hint(s):
                # an add sentence that still talks about time holds a constraint we failed to parse:
                # leave the event unknown rather than "known to be unconstrained"
                cons[eid] = None
    return cons


DEL_RE = re.compile(r"\b(remove|delete|cancel|drop|scrap|get rid|take .* off|no longer|clear)\b", re.I)
ADD_RE = re.compile(r"\b(add|create|new|put .* back|bring .* back|re-?add|restore|reinstate)\b", re.I)


def deleted_since(user_msgs, names, explicit=None):
    """Event ids whose latest explicit action in user_msgs is a deletion.
    If `explicit` is a set it receives the ids whose deletion named the event id explicitly."""
    state = {}
    for text in user_msgs:
        for s in split_sentences(norm(text)):
            ids, by_name = ids_in(s, names)
            if len(set(ids)) != 1:
                continue
            if DEL_RE.search(s) and not SOFT.search(s):
                state[ids[0]] = "del"
                if explicit is not None:
                    (explicit.discard if by_name else explicit.add)(ids[0])
            elif ADD_RE.search(s):
                state[ids[0]] = "add"
    return {i for i, v in state.items() if v == "del"}


def llm_cons(blackboard):
    """{event_id: constraint tuple or None} from a 'CONS:' JSON list in an LLM role's output."""
    out = {}
    for key in ("assistant", "cons"):
        t = strip_think(blackboard.get(key, ""))
        if "CONS" not in t:
            continue
        t = t.split("CONS", 1)[1].split("PREFS", 1)[0]
        for val in json_lists(t):
            for p in val:
                try:
                    i = int(p["event_id"])
                    c = p.get("constraint")
                    if c is None or str(c).strip().lower() in ("", "null", "none"):
                        out[i] = None
                    else:
                        out[i] = sentence_constraint(" " + str(c))
                except Exception:
                    continue
    return out


def pack(events, cons, deadline):
    """Complete feasibility search for one machine with release times / deadlines: try event
    orders, each event as early as possible after the previous one. Returns {id: start} or None."""
    lo, hi, dur = {}, {}, {}
    for e in events:
        i, d = e["event_id"], e["duration"]
        dur[i] = d
        a, b = WIN_START, WIN_END - d
        c = cons.get(i)
        if c:
            kind, x, y = c
            if kind == "at":
                a = b = x
            elif kind == "before":
                b = min(b, x - d)
            elif kind == "after":
                a = max(a, x)
            elif kind == "between":
                a, b = max(a, x), min(b, y - d)
        lo[i], hi[i] = a, b
        if a > b:
            return None
    return pack_bounds(dur, lo, hi, deadline)


def pack_bounds(dur, lo, hi, deadline):
    """Exact feasibility search given start-time bounds lo[i] <= start <= hi[i]."""
    if any(lo[i] > hi[i] for i in dur):
        return None
    ids = sorted(dur, key=lambda i: (hi[i], lo[i]))
    failed = set()
    cur = {}

    def dfs(rem, t):
        if not rem:
            return True
        if time.time() > deadline:
            return False
        key = (rem, t)
        if key in failed:
            return False
        if any(max(t, lo[j]) > hi[j] for j in rem):
            failed.add(key)
            return False
        for i in ids:
            if i in rem:
                s = max(t, lo[i])
                cur[i] = s
                if dfs(rem - {i}, s + dur[i]):
                    return True
                del cur[i]
        failed.add(key)
        return False

    if dfs(frozenset(ids), WIN_START):
        return dict(cur)
    return "timeout" if time.time() > deadline else None



# ---------------------------------------------------------------- preferences
H = lambda h, m=0: h * 60 + m
PHRASES = [  # first match wins; general day-part vocabulary
    (r"early afternoon|right after lunch", (H(12), H(14))),
    (r"late afternoon|late in the day|later in the day|end of (?:my|the) (?:work ?)?day|toward(?:s)? the end|"
     r"wind[- ]?down|close of (?:the )?day|evening|last thing|end the day", (H(14), H(16))),
    (r"mid-?afternoon", (H(13), H(15))),
    (r"afternoon|after lunch|post-?lunch", (H(12), H(16))),
    (r"late morning|late-morning", (H(11), H(12))),
    (r"mid-?morning", (H(10, 30), H(11, 30))),
    (r"lunch|midday|mid-day|around noon|middle of (?:my|the) day|noon-?ish", (H(11, 30), H(13, 30))),
    (r"early|first thing|morning|before noon|before lunch|start of (?:my|the) day|beginning of (?:my|the) day|"
     r"kick off (?:my|the) day|fresh mind|a\.?m\.? slot", (H(10), H(12))),
]
NEG = re.compile(r"\b(?:avoid|not|n't|never|rather not|keep (?:it )?(?:out of|away from)|anything but|no)\b"
                 r"(?:\W+\w+){0,3}\W+$", re.I)


def sentence_pref(s):
    low = canon_times(norm(s.lower()))
    for pat, (a, b) in PHRASES:
        m = re.search(r"\b(?:" + pat + r")", low)
        if m:
            neg = NEG.search(low[:m.start()])
            return {"type": "avoid" if neg else "window", "start": a, "end": b}
    c = sentence_constraint(s)
    if c:
        kind, a, b = c
        if kind == "before":
            return {"type": "window", "start": WIN_START, "end": a}
        if kind == "after":
            return {"type": "window", "start": a, "end": WIN_END}
        if kind == "between":
            return {"type": "window", "start": a, "end": b}
        if kind == "at":
            return {"type": "at", "start": a, "end": a}
    return None


def collect_prefs(user_msgs, names):
    prefs = {}
    for text in user_msgs:
        for s in split_sentences(text):
            if not SOFT.search(s) and not re.search(r"\b(around|sometime|somewhere|ish)\b", s, re.I):
                continue
            ids, _ = ids_in(s, names)
            if len(set(ids)) != 1:
                continue
            p = sentence_pref(s)
            if p:
                p["event_id"] = ids[0]
                prefs[ids[0]] = p
    return prefs


def llm_prefs(blackboard):
    """Preferences an LLM role extracted: JSON list after 'PREFS:' in the assistant output, or a
    dedicated 'prefs' role. Generic schema:
      {"type":"window","event_id":i,"earliest":"HH:MM","latest":"HH:MM"}  (start>=earliest, end<=latest)
      {"type":"avoid","event_id":i,"earliest":..,"latest":..}           (no overlap with that span)
      {"type":"order","first":i,"second":j}                           (i ends before j starts)
      {"type":"adjacent","event_id":i,"other":j}                      (back to back, either order)
      {"type":"gap","event_id":i,"minutes":m}                         (m minutes free around i)"""
    texts = []
    a = strip_think(blackboard.get("assistant", ""))
    if "PREFS" in a:
        texts.append(a.split("PREFS", 1)[1])
    if blackboard.get("prefs"):
        texts.append(strip_think(blackboard["prefs"]))
    out = []
    for t in texts:
        for val in json_lists(t):
            for p in val:
                try:
                    typ = str(p.get("type", "window")).lower()
                    q = {"type": typ}
                    if typ in ("window", "avoid"):
                        q["event_id"] = int(p["event_id"])
                        q["start"] = max(WIN_START, hm_to_min(p.get("earliest") or "10:00"))
                        q["end"] = min(WIN_END, hm_to_min(p.get("latest") or "16:00"))
                        if q["start"] >= q["end"]:
                            continue
                    elif typ == "order":
                        q["first"], q["second"] = int(p["first"]), int(p["second"])
                    elif typ == "adjacent":
                        q["event_id"], q["other"] = int(p["event_id"]), int(p["other"])
                    elif typ == "gap":
                        q["event_id"], q["minutes"] = int(p["event_id"]), int(p.get("minutes") or 15)
                    else:
                        continue
                    out.append(q)
                except Exception:
                    continue
    return out


def unary_ok(p, s, d):
    t = p["type"]
    if t == "window":
        return s >= p["start"] and s + d <= p["end"]
    if t == "avoid":
        return s + d <= p["start"] or s >= p["end"]
    if t == "at":
        return s == p["start"]
    return True


def rel_ok(p, pos, dur):
    t = p["type"]
    if t == "order":
        a, b = p["first"], p["second"]
        return a not in pos or b not in pos or pos[a] + dur[a] <= pos[b]
    if t == "adjacent":
        a, b = p["event_id"], p["other"]
        return a not in pos or b not in pos or pos[a] + dur[a] == pos[b] or pos[b] + dur[b] == pos[a]
    if t == "gap":
        a, g = p["event_id"], p["minutes"]
        if a not in pos:
            return True
        return all(j == a or pos[j] >= pos[a] + dur[a] + g or pos[j] + dur[j] + g <= pos[a] for j in pos)
    return True


# ---------------------------------------------------------------- constraints / search
def satisfies(c, s, d):
    if s is None or s < WIN_START or s + d > WIN_END:
        return False
    if c is None:
        return True
    kind, a, b = c
    if kind == "at":
        return s == a
    if kind == "before":
        return s + d <= a
    if kind == "after":
        return s >= a
    if kind == "between":
        return s >= a and s + d <= b
    return True


def move_base(i, d, cons, p_pos, hulls):
    """Lower bound of the cost of moving event i anywhere."""
    if i not in p_pos:
        return MOVE_NEW if i in cons else 1.0
    if i in cons:
        return MOVE_KNOWN if satisfies(cons[i], p_pos[i], d) else 1.0
    return 1.0 if i in hulls else 1.5


def move_cost(i, s, d, cons, p_pos, hulls):
    """Risk cost of moving event i to start s. The grader does not count moves; a move only risks
    breaking a constraint we cannot see. Unknown-constraint events: starts between two legal
    starts are legal for any at/before/after/between constraint, so their known-legal hull is safe."""
    if i in cons or i not in p_pos:
        return move_base(i, d, cons, p_pos, hulls)
    if WISH.get(i) and all(unary_ok(p, s, d) for p in WISH[i]):
        # the user asked for this event to sit in that window: a hidden rule that forbids the
        # window is unlikely, so a move into it is priced like moving a known event
        return WISH_MOVE
    if i not in hulls:
        p = p_pos[i]
        return 1.5 + (abs(s - p) * (DIST_UNK + DIST_OUT) if p is not None else 0.0)
    lo, hi, elo, ehi = hulls[i]
    far = (lo - s if s < lo else s - hi if s > hi else 0) * DIST_UNK
    far += (elo - s if s < elo else s - ehi if s > ehi else 0) * DIST_OUT
    return far + (1.0 if lo <= s <= hi else (1.0 + HULL_EXT if elo <= s <= ehi else 1.0 + HULL_OUT))


def sol_cost(sol, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls):
    """Same objective as solve(), evaluated on a complete placement."""
    dur = {e["event_id"]: e["duration"] for e in events}
    tot = 0.0
    for i, d in dur.items():
        s = sol[i]
        if a_pos.get(i) == s and satisfies(cons.get(i), s, d):
            tot += 0.3 if i in suspicious else 0.0
        elif p_pos.get(i) == s and satisfies(cons.get(i), s, d):
            tot += 0.0 if i in suspicious else 0.1
        else:
            ref = a_pos.get(i) if a_pos.get(i) is not None else p_pos.get(i, 780)
            tot += move_cost(i, s, d, cons, p_pos, hulls) + abs(s - ref) / 1000.0
        tot += sum(PREF_PEN for p in unary.get(i, []) if not unary_ok(p, s, d))
    tot += sum(PREF_PEN for p in rel if not rel_ok(p, sol, dur))
    return tot


def settle(cand, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls, deadline=None):
    """Put events back on their own (cheaper) slot whenever that keeps things legal; with a
    deadline also slide single events to any free legal start that lowers the cost."""
    dur = {e["event_id"]: e["duration"] for e in events}
    cost = lambda c: sol_cost(c, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls)
    cur_c = cost(cand)
    changed = True
    while changed:
        changed = False
        if deadline is not None and time.time() > deadline:
            break
        for i, d in dur.items():
            opts = [a_pos.get(i), p_pos.get(i)]
            if deadline is not None:   # also slide to any free legal start
                opts += list(range(WIN_START, WIN_END - d + 1, STEP))
            for s in opts:
                if s is None or s == cand[i] or not satisfies(cons.get(i), s, d):
                    continue
                if any(j != i and s < cand[j] + dur[j] and cand[j] < s + d for j in dur):
                    continue
                trial = dict(cand)
                trial[i] = s
                tc = cost(trial)
                if tc < cur_c - 1e-9:
                    cand, cur_c, changed = trial, tc, True
                    break
            if changed:
                break
    return cand


def pref_pack(sol, events, a_pos, p_pos, cons, unary, rel, suspicious, deadline, hulls):
    """The edge-aligned min-move search misses chained rearrangements (several known events
    shift to make room for a preferred slot). Try preference windows as hard bounds: events of
    unknown constraint stay where `sol` put them, known ones are re-packed exactly, then
    every event that can go back to its own slot does. Returns a cheaper placement or None."""
    from itertools import combinations
    dur = {e["event_id"]: e["duration"] for e in events}
    wins = [(i, p) for i, ps in unary.items() for p in ps if p["type"] in ("window", "at") and i in dur]
    if not wins:
        return None
    best_c, best = sol_cost(sol, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls), None
    for k in range(len(wins), 0, -1):
        for sub in combinations(wins, k):
            if time.time() > deadline:
                return best
            lo, hi = {}, {}
            for i, d in dur.items():
                if i not in cons and not unary.get(i):
                    lo[i] = hi[i] = sol[i]
                    continue
                a, b = WIN_START, WIN_END - d
                c = cons.get(i)
                if c:
                    kind, x, y = c
                    if kind == "at":
                        a = b = x
                    elif kind == "before":
                        b = min(b, x - d)
                    elif kind == "after":
                        a = max(a, x)
                    elif kind == "between":
                        a, b = max(a, x), min(b, y - d)
                lo[i], hi[i] = a, b
            for i, p in sub:
                lo[i] = max(lo[i], p["start"])
                hi[i] = min(hi[i], p["end"] - dur[i]) if p["type"] == "window" else min(hi[i], p["start"])
            cand = pack_bounds(dur, lo, hi, min(deadline, time.time() + 2))
            if not isinstance(cand, dict):
                continue
            cand = settle(cand, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls)
            c = sol_cost(cand, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls)
            if c < best_c - 1e-9:
                best_c, best = c, cand
        if best is not None:
            return best
    return best


def solve(events, a_pos, p_pos, cons, deadline, unary=None, rel=None, suspicious=(), pin_unknown=False,
          hulls=None, seed=None):
    """Min-cost placement: moving an event costs ~1 (more if its constraint is unknown),
    each unmet preference costs PREF_PEN. Unmoved events stay at the LLM's position or at
    their previous-calendar position (known legal). Subsets of moved events are enumerated by
    size (branch and bound)."""
    from itertools import combinations
    unary = unary or {}
    rel = rel or []
    hulls = hulls or {}
    ids = [e["event_id"] for e in events]
    dur = {e["event_id"]: e["duration"] for e in events}

    def pen(i, s):
        return sum(PREF_PEN for p in unary.get(i, []) if not unary_ok(p, s, dur[i]))

    stay, move, base = {}, {}, {}
    for i in ids:
        d, c = dur[i], cons.get(i)
        opts = {}
        if a_pos.get(i) is not None and satisfies(c, a_pos[i], d):
            # LLM moved an event it was not asked about and whose constraint we cannot see
            opts[a_pos[i]] = 0.3 if i in suspicious else 0.0
        if p_pos.get(i) is not None and satisfies(c, p_pos[i], d):
            opts[p_pos[i]] = min(opts.get(p_pos[i], 9), 0.0 if i in suspicious else 0.1)
        stay[i] = sorted(((s, v + pen(i, s)) for s, v in opts.items()), key=lambda kv: kv[1])
        base[i] = move_base(i, d, cons, p_pos, hulls)
        ref = a_pos.get(i) if a_pos.get(i) is not None else p_pos.get(i, 780)
        mv = []
        # an event the user attached a wish to is not pinned: its old slot is what the wish asks to change
        # (oodA-1177, oodA-195, oodA-1486 kept unknown-constraint events off their preferred window)
        if not (pin_unknown and i not in cons and i in p_pos and stay[i] and not unary.get(i)):
            for s in range(WIN_START, WIN_END - d + 1, STEP):
                if s not in opts and satisfies(c, s, d):
                    mv.append((s, move_cost(i, s, d, cons, p_pos, hulls) + abs(s - ref) / 1000.0 + pen(i, s)))
        move[i] = sorted(mv, key=lambda kv: kv[1])

    ref_of, bounds = {}, {}
    for i in ids:
        ref_of[i] = a_pos.get(i) if a_pos.get(i) is not None else p_pos.get(i, 780)
        c = cons.get(i)
        bounds[i] = set()
        if c:
            kind, a, b = c
            bounds[i] = {a, a - dur[i]} | ({b - dur[i]} if b else set())
        for p in unary.get(i, []):
            bounds[i] |= {p["start"], p["start"] - dur[i], p["end"], p["end"] - dur[i]}
    moved_now, aligned = [set()], [True]
    must = [i for i in ids if not stay[i]]
    optional = sorted((i for i in ids if stay[i]), key=lambda i: base[i])
    opt_base = [base[i] for i in optional]
    best = [float("inf"), None]
    if seed is not None:
        # a legal placement found by the exact packer: an upper bound for branch and bound and the
        # answer if the min-move search runs out of time (zero-slack days: train-1005 lost 48 s here
        # and then dropped every constraint)
        best = [sol_cost(seed, events, a_pos, p_pos, cons, unary, rel, suspicious, hulls) + 1e-9, dict(seed)]
    rel_ids = {i for p in rel for i in (p.get("first"), p.get("second"), p.get("event_id"), p.get("other")) if i is not None}

    def place(order, doms):
        cur = {}

        def dfs(k, cost, busy):
            if time.time() > deadline:
                return None
            if k == len(order):
                extra = sum(PREF_PEN for p in rel if not rel_ok(p, cur, dur))
                if cost + extra >= best[0]:
                    return None
                return cost + extra, dict(cur)
            i = order[k]
            if aligned[0] and i in moved_now[0]:
                edges = {WIN_START, WIN_END - dur[i], ref_of[i]} | {be for _, be in busy} | \
                        {bs - dur[i] for bs, _ in busy} | bounds[i]
                if i in rel_ids:
                    edges |= {be + g for _, be in busy for g in (5, 10, 15, 30)} | \
                             {bs - dur[i] - g for bs, _ in busy for g in (5, 10, 15, 30)}
                cand = [(s, c) for s, c in doms[i] if s in edges]
            else:
                cand = doms[i]
            for s, c in cand:
                if cost + c >= best[0]:
                    break
                if any(s < be and bs < s + dur[i] for bs, be in busy):
                    continue
                cur[i] = s
                r = dfs(k + 1, cost + c, busy + [(s, s + dur[i])])
                del cur[i]
                if r and not rel:
                    return r
                if r and r[0] < best[0]:
                    best[0], best[1] = r
            return None

        return dfs(0, 0.0, [])

    def search():
        for k in range(0, len(optional) + 1):
            if len(must) + k > 0 and sum(base[i] for i in must) + sum(opt_base[:k]) >= best[0]:
                break
            for extra in combinations(optional, k):
                if time.time() > deadline:
                    return
                moved = set(must) | set(extra)
                lb = sum(base[i] for i in moved)
                if lb >= best[0]:
                    continue
                doms = {i: (move[i] if i in moved else stay[i]) for i in ids}
                moved_now[0] = moved
                if any(not doms[i] for i in ids):
                    continue
                order = sorted(ids, key=lambda i: (i in moved, len(doms[i]), -dur[i]))
                r = place(order, doms)
                if r and r[0] < best[0]:
                    best[0], best[1] = r

    search()
    if best[1] is None and time.time() < deadline:
        aligned[0] = False  # exhaustive 5-minute grid as a fallback
        search()
    return best[1]


def legal_hulls(events, cons, last_ids, p_pos, a_pos, users, asst, names):
    """For events whose constraint we could not parse: the span of start times the gold history
    gave them since the user last mentioned them (same duration). Every hard-constraint type is an
    interval of legal starts, so any start inside that span is legal too. Returns
    {id: (lo, hi, lo_with_llm_spot, hi_with_llm_spot)}."""
    out = {}
    cals = [last_json_list(a) for a in asst]
    for e in events:
        i, d = e["event_id"], e["duration"]
        if i in cons or i in last_ids or i not in p_pos:
            continue
        u = -1
        for k, text in enumerate(users[:-1]):
            for s in split_sentences(norm(text)):
                if i in ids_in(s, names)[0]:
                    u = k
        starts = []
        for k, cal in enumerate(cals):
            if cal is None or k < u:
                continue
            for x in cal:
                try:
                    if int(x["event_id"]) == i and int(x.get("duration") or 0) == d:
                        st = hm_to_min(x["start_time"])
                        if st is not None and satisfies(None, st, d):
                            starts.append(st)
                except Exception:
                    pass
        if not starts:
            continue
        lo, hi = min(starts), max(starts)
        a = a_pos.get(i)
        elo, ehi = (min(lo, a), max(hi, a)) if a is not None and satisfies(None, a, d) else (lo, hi)
        out[i] = (lo, hi, elo, ehi)
    return out


LOW_W = re.compile(r"\b(?:after|later than|no earlier|not earlier|not before|no sooner|from|starting|start(?:s)? at|"
                   r"onwards?|at the earliest|or later|past|beginning|begin)\b", re.I)
UP_W = re.compile(r"\b(?:before|by|until|till|no later|not later|at the latest|wrap\w*|finish\w*|end(?:s|ed)?|"
                  r"done|prior|ahead of|or earlier|complete\w*|over by|out by|close)\b", re.I)
BOTH_W = re.compile(r"\b(?:between|from .* (?:to|until|till)|window|range|at \d|sharp|exactly)\b", re.I)


def infer_direction(i, users, names):
    """'low' / 'up' when every non-wish sentence about event i that talks about time points one way."""
    dirs = set()
    for text in users:
        prev_eid = None
        for s in split_sentences(norm(text)):
            ids, by_name = ids_in(s, names)
            if prev_eid is not None and (not ids or by_name) and PRON.search(s):
                ids, by_name = [prev_eid], True
            prev_eid = ids[0] if len(set(ids)) == 1 else None
            if set(ids) != {i} or SOFT.search(s) or not time_hint(s):
                continue
            t = re.sub(r"event[\s_-]*id\s*[:#=]?\s*\d+", " ", canon_times(s.lower()))
            lo_, up_ = bool(LOW_W.search(t)), bool(UP_W.search(t))
            if BOTH_W.search(t) or lo_ == up_:
                return None
            dirs.add("low" if lo_ else "up")
    return dirs.pop() if len(dirs) == 1 else None


def fmt(m):
    return "%02d:%02d" % (m // 60, m % 60)


DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(min(?:ute)?s?|hours?|hrs?)\b|"
                    r"(an hour and a half|hour and a half|half an hour|half-hour|an hour|one hour|a full hour)", re.I)


def durations_in(text):
    out = []
    for m in DUR_RE.finditer(norm(text)):
        if m.group(3):
            g = m.group(3).lower()
            out.append(90 if "and a half" in g else 30 if "half" in g else 60)
        else:
            v = float(m.group(1))
            out.append(int(round(v * 60)) if m.group(2).lower().startswith("h") else int(v))
    return [d for d in out if 5 <= d <= 360]


# ---------------------------------------------------------------- router (brief role)
# The same file serves two roles: run() without an 'assistant' key on the blackboard is the
# brief role in front of the LLM, with it it is the repair role behind the LLM.
# A "pure add" turn (one new event id, one duration, no edit/delete/revert words, a timing rule
# the regex reads or no timing at all, no wish about other events) is fully determined by the
# gold previous calendar plus the new event: the LLM is only asked for the event's title.
# Every other turn gets the whole conversation verbatim.
BROAD = False      # also route lexicon-parsable wishes and adds whose rule the regex misses (LLM reads it)
COMPRESS = True    # complex turns: only the latest (and revert-target) calendar is shown to the LLM
EDIT_RE = re.compile(r"\b(revert|go back|roll ?back|restore|undo|return|move|moving|reschedul\w*|shift|swap|push|"
                     r"pull|bump|change|changing|instead|update|rename|replace|extend|shorten|lengthen|longer|"
                     r"shorter|resize|remove|delete|cancel|drop|scrap|get rid|no longer|clear|earlier|later|"
                     r"turn\s*#?\s*\d+)\b", re.I)
REL_RE = re.compile(r"\b(next to|back[- ]to[- ]back|right (?:after|before)|followed|following|break|buffer|gap|"
                    r"breathing|space|together|apart|same time|(?:before|after|around) (?:the|my|our)|near|"
                    r"close to|away from|spread|cluster|bunch|order|sequence|first|last|in a row|consecutive|"
                    r"overlap|adjacent|between (?:the|my|our)|other (?:event|meeting|session)s?)\b", re.I)
WISH_RE = re.compile(r"\b(around|sometime|somewhere|ish|prefer\w*|ideal\w*|hope|wish|rather|better|best|"
                     r"perfect|happy|love|keen|if possible|if you can|suits?)\b", re.I)
PREF_GATE = re.compile(r"\b(around|sometime|somewhere|ish)\b", re.I)   # collect_prefs' own gate (+ SOFT)
ID_RE = re.compile(r"event[\s_-]*id\s*[:#=]?\s*(\d+)|\bevent\s+#?(\d+)\b", re.I)


def explicit_ids(s):
    return {int(a or b) for a, b in ID_RE.findall(s)}


def chitchat_after(users, asst, k):
    """True when every user message answered by asst[k+1:] is calendar-free small talk, so the
    calendar of assistant turn k is still the current one."""
    for u in users[k + 1:len(asst)]:
        u = norm(u)
        if explicit_ids(u) or EDIT_RE.search(u) or ADD_RE.search(u) or DEL_RE.search(u) or durations_in(u) \
                or re.search(r"\b(calendar|schedule|event|meeting|session|slot|book|appointment)\b", u, re.I) \
                or time_hint(u):
            return False
    return True


def latest_calendar(users, asst):
    """(calendar, index) of the latest assistant calendar if only small talk followed it, else (None, -1)."""
    for k in range(len(asst) - 1, -1, -1):
        cal = last_json_list(asst[k])
        if cal is not None:
            return (cal, k) if chitchat_after(users, asst, k) else (None, -1)
    return None, -1


def route(messages):
    """{'nid', 'dur', 'rule_needed'} for a pure-add turn, else None."""
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
    asst = [m.get("content") or "" for m in messages if m.get("role") == "assistant"]
    if not users or not asst or len(users) != len(asst) + 1:
        return None
    # a small-talk turn in between leaves the calendar unchanged (train-1005, v2-272 went the long way)
    prev = latest_calendar(users, asst)[0]
    if not prev:
        return None
    names = {}
    try:
        for e in prev:
            names[int(e["event_id"])] = str(e.get("event_name", ""))
            if int(e.get("duration") or 0) <= 0:
                return None
    except Exception:
        return None
    last = norm(users[-1])
    new = explicit_ids(last) - set(names)
    if len(new) != 1:
        return None
    nid = new.pop()
    durs = set(durations_in(last))
    if len(durs) != 1 or EDIT_RE.search(last):
        return None
    rule_needed, prev_new = False, False
    for s in split_sentences(last):
        sid = explicit_ids(s)
        about_new = nid in sid or (not sid and prev_new and PRON.search(s))
        if sid - {nid}:
            # a wish about an existing event: only the lexicon window types repair itself reads
            if not BROAD or len(sid) != 1 or REL_RE.search(s) or not (SOFT.search(s) or PREF_GATE.search(s)) \
                    or sentence_pref(s) is None:
                return None
            prev_new = False
            continue
        if not about_new:
            by_name = ids_in(s, names)[0]
            if time_hint(s) or REL_RE.search(s) or SOFT.search(s) or \
                    (by_name and WISH_RE.search(s)):
                return None
            prev_new = False
            continue
        prev_new = True
        if REL_RE.search(s):
            return None
        if SOFT.search(s) or WISH_RE.search(s):
            if not time_hint(s):
                continue
            if not BROAD or not (SOFT.search(s) or PREF_GATE.search(s)) or sentence_pref(s) is None:
                return None
            continue
        if time_hint(s) and sentence_constraint(s) is None:
            if not BROAD:
                return None
            rule_needed = True
    return {"nid": nid, "dur": durs.pop(), "rule_needed": rule_needed}


def brief(messages):
    r = route(messages)
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
    if r is not None:
        tail = (', "rule": "<the time rule this event MUST follow, written like: before 2pm | after 11:30am | '
                'at 1pm | between 10am and 2pm>" or null if the user set no required time}') if r["rule_needed"] else "}"
        return ('A user is adding one event to their calendar. Their message:\n"""\n' + users[-1] + '\n"""\n'
                'Reply with ONLY this JSON object and nothing else:\n'
                '{"event_id": %d, "event_name": "<the new event\'s title as the user wrote it>"' % r["nid"] + tail +
                "\n/no_think")
    keep = set()
    if COMPRESS:
        asst_idx = [k for k, m in enumerate(messages) if m.get("role") == "assistant"]
        for k in reversed(asst_idx):
            if last_json_list(messages[k].get("content") or "") is not None:
                keep.add(k)
                break
        mrev = re.search(r"\bturn\s*#?\s*(\d+)", norm(users[-1]) if users else "", re.I)
        if mrev and 1 <= int(mrev.group(1)) <= len(asst_idx):
            keep.add(asst_idx[int(mrev.group(1)) - 1])
    parts, turn = [], 0
    for k, m in enumerate(messages):
        role = str(m.get("role", "user")).upper()
        c = m.get("content") or ""
        if role == "ASSISTANT":
            turn += 1
            if COMPRESS and k not in keep and last_json_list(c) is not None:
                c = "(calendar of turn %d omitted - only the latest calendar below matters)" % turn
            role = "ASSISTANT (turn %d)" % turn
        parts.append("### " + role + "\n" + c)
    return ("Below is a conversation between a user and a calendar assistant.\n\n" + "\n\n".join(parts) +
            "\n\n### YOUR TASK\nYou are the ASSISTANT. Reply to the last USER message, following the SYSTEM "
            "instructions: output the complete updated calendar as one JSON list "
            '[{"event_id": int, "event_name": str, "start_time": "HH:MM", "duration": int}].')


def brief_fallback(messages):
    return "\n\n".join("### " + str(m.get("role", "")).upper() + "\n" + (m.get("content") or "") for m in messages) + \
        "\n\nReply as the assistant to the last user message with the full updated calendar as one JSON list."


def guess_name(text, nid):
    t = norm(text)
    m = re.search(r"[\"“]([^\"”]{3,120})[\"”][^.?!]*?\(?event[\s_-]*id\s*[:#=]?\s*%d" % nid, t, re.I) or \
        re.search(r"\b(?:add|schedule|book|include|slot in|put in|set up|squeeze in|fit in|pencil in|create)\s+"
                  r"(?:in\s+)?(?:a|an|the|my|our|one more|another|new)?\s*(.{3,160}?)\s*[(,]?\s*\(?event[\s_-]*id"
                  r"\s*[:#=]?\s*%d" % nid, t, re.I)
    if m:
        return m.group(1).strip(" ,:;-\"'")
    return "Event %d" % nid


def simple_answer(r, raw, messages):
    """Previous (gold) calendar + the new event; title (and rule if needed) from the LLM."""
    asst = [m.get("content") or "" for m in messages if m.get("role") == "assistant"]
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
    prev = latest_calendar(users, asst)[0]
    name, rule = None, None
    t = strip_think(raw)
    for m in re.finditer(r"\{[^{}]*\}", t):
        try:
            o = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(o, dict) and str(o.get("event_id", r["nid"])) == str(r["nid"]) and o.get("event_name"):
            name, rule = str(o["event_name"]).strip(), o.get("rule")
    if not name:
        name = guess_name(users[-1], r["nid"])
    c = None
    if r["rule_needed"] and rule and str(rule).strip().lower() not in ("null", "none", ""):
        c = sentence_constraint(" " + str(rule))
    ans = [dict(e) for e in prev] + [{"event_id": r["nid"], "event_name": name, "start_time": "",
                                      "duration": r["dur"]}]
    return ans, c


def run(blackboard, messages):
    raw = blackboard.get("assistant", "")
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
    asst = [m.get("content") or "" for m in messages if m.get("role") == "assistant"]
    last = norm(users[-1]) if users else ""
    prev, prev_k = None, -1
    for k, a in enumerate(asst):
        cal = last_json_list(a)
        if cal is not None:
            prev, prev_k = cal, k
    if "assistant" not in blackboard:   # brief role in front of the LLM
        try:
            return {"text": brief(messages)}
        except Exception:
            return {"text": brief_fallback(messages)}
    try:
        rt = route(messages)
    except Exception:
        rt = None
    rule_llm = None
    if rt is not None:
        ans, rule_llm = simple_answer(rt, raw, messages)
    else:
        ans = last_json_list(raw)
    if ans is None:
        if prev is not None:
            return {"text": json.dumps(prev)}
        return {"text": strip_think(raw).split("CONS")[0].split("PREFS")[0].strip()}
    try:
        cons_msgs = users
        mrev = re.search(r"\b(?:revert|go back|roll ?back|restore|return)\b.*?\bturn\s*#?\s*(\d+)", last, re.I | re.S)
        reverted = False
        if mrev:
            n = int(mrev.group(1))
            if 1 <= n <= len(asst) and last_json_list(asst[n - 1]) is not None:
                prev, prev_k = last_json_list(asst[n - 1]), n - 1
                cons_msgs = users[:n] + users[-1:]
                reverted = True
        # previous calendar is stale when later assistant turns carried no calendar
        stale = not reverted and prev is not None and prev_k < len(asst) - 1 and \
            not chitchat_after(users, asst, prev_k)
        n_before = (n if reverted else prev_k + 1)   # user messages already reflected in prev

        prev_by = {}
        for e in prev or []:
            try:
                prev_by[int(e["event_id"])] = e
            except Exception:
                pass
        last_ids = {int(x) for x in re.findall(r"event[\s_-]*id\s*[:#=]?\s*(\d+)", last, re.I)}
        all_names = {int(e["event_id"]): str(e.get("event_name", "")) for e in ans if "event_id" in e}
        for i, e in prev_by.items():
            all_names.setdefault(i, str(e.get("event_name", "")))
        for s in split_sentences(last):
            ids, by_name = ids_in(s, all_names)
            if by_name and len(ids) == 1:
                last_ids.add(ids[0])
        deleting = re.search(r"\b(remove|delete|cancel|drop|scrap|get rid|take .* off|no longer)\b", last, re.I)
        resizing = re.search(r"\b(extend|shorten|lengthen|longer|shorter|duration|resize|cut .* to|"
                             r"make (?:it|the)|change .* to \d+|instead of)\b", last, re.I)
        new_durs = durations_in(last)
        # deletions requested after the previous calendar was produced (not only in the last turn)
        explicit_del = set()
        gone = set() if reverted else deleted_since(users[max(n_before, 0):], all_names, explicit_del)
        # a name-matched deletion of an event the LLM kept is too often a misattribution
        # (train-94, v2-7041 lost an event): only drop LLM-kept events deleted by explicit id
        llm_ids = {int(e["event_id"]) for e in ans if "event_id" in e}
        gone = {i for i in gone if i not in llm_ids or i in explicit_del}

        new_ids = {int(e["event_id"]) for e in ans} - set(prev_by)
        if reverted:
            new_ids &= last_ids
        if prev_by and not reverted and not stale:
            since = " ".join(norm(u) for u in users[max(n_before, 0):])
            said = {int(x) for x in re.findall(r"event[\s_-]*id\s*[:#=]?\s*(\d+)", since, re.I)} | \
                   {int(x) for x in re.findall(r"\bevent\s+#?(\d+)\b", since, re.I)}
            # an event the history once held but the previous calendar no longer does was deleted:
            # the LLM resurrected it unless the user asked for it back
            old_ids = set()
            for a_ in asst[:max(prev_k, 0)]:
                for e_ in last_json_list(a_) or []:
                    try:
                        old_ids.add(int(e_["event_id"]))
                    except Exception:
                        pass
            if not ADD_RE.search(since):
                invented = {i for i in new_ids if i not in said and i in old_ids}
                if invented and len(invented) < len(ans):
                    gone = set(gone) | invented
                    new_ids -= invented
        events, a_pos, seen = [], {}, set()
        for e in ans:
            i = int(e["event_id"])
            if i in seen or i in gone:
                continue
            if reverted and i not in prev_by and i not in last_ids:
                continue  # event that the reverted calendar did not contain
            seen.add(i)
            d = int(e.get("duration") or 0)
            if i in prev_by:
                pd = int(prev_by[i].get("duration") or 0)
                if pd > 0 and d != pd and not (i in last_ids and resizing and new_durs) and not stale:
                    d = pd
            elif prev_by and len(set(new_durs)) == 1 and new_ids == {i} and i in last_ids:
                d = new_durs[0]
            if d <= 0:
                raise ValueError
            events.append({"event_id": i, "event_name": e.get("event_name", ""), "duration": d})
            a_pos[i] = hm_to_min(e.get("start_time", ""))
        if not events:
            return {"text": json.dumps(ans)}
        if not deleting or reverted:
            for i, e in prev_by.items():
                if i not in seen and i not in gone:
                    events.append({"event_id": i, "event_name": e.get("event_name", ""),
                                   "duration": int(e.get("duration") or 0)})
                    a_pos[i] = None
        p_pos = {}
        for i, e in prev_by.items():
            try:
                p_pos[i] = hm_to_min(e.get("start_time", ""))
            except Exception:
                pass

        names = {e["event_id"]: str(e.get("event_name", "")) for e in events}
        when = {}
        cons = collect_constraints(cons_msgs, names, when)
        ids_now = set(names)
        misparsed = set()
        # a constraint parsed from an old message that both the previous calendar and the LLM
        # violate is almost surely a misparse -> treat the event's constraint as unknown
        for i in list(cons):
            c = cons[i]
            if c is None or when.get(i, 10 ** 6) >= n_before:
                continue
            d = next(e["duration"] for e in events if e["event_id"] == i) if i in ids_now else 0
            if i in p_pos and not satisfies(c, p_pos[i], d) and not satisfies(c, a_pos.get(i), d):
                del cons[i]
                misparsed.add(i)
        if USE_LLM_CONS:
            dmap = {e["event_id"]: e["duration"] for e in events}
            for i, c in llm_cons(blackboard).items():
                if i not in ids_now:
                    continue
                r = cons.get(i)
                if c is None:
                    if i not in cons and i in last_ids and i not in prev_by:
                        cons[i] = None
                    continue
                ok_a = satisfies(c, a_pos.get(i), dmap[i])
                ok_p = i in p_pos and satisfies(c, p_pos[i], dmap[i])
                if r is None:
                    if ok_a or ok_p or i in last_ids:
                        cons[i] = c
                elif r != c and ok_a and not satisfies(r, a_pos.get(i), dmap[i]) and \
                        not (i in p_pos and satisfies(r, p_pos[i], dmap[i])):
                    cons[i] = c
        if rt is not None and rule_llm is not None and cons.get(rt["nid"]) is None:
            cons[rt["nid"]] = rule_llm   # rule the regex could not read, read by the LLM from this message alone
        unary = {}
        for i, p in collect_prefs(users, names).items():
            if i in ids_now:
                unary[i] = [p]
        WISH.clear()
        WISH.update(unary)
        rel = []
        for p in llm_prefs(blackboard):
            if p["type"] in ("window", "avoid"):
                if p["event_id"] in ids_now and p["event_id"] not in unary:
                    unary[p["event_id"]] = [p]
            elif all(x in ids_now for x in (p.get("first"), p.get("second"), p.get("event_id"), p.get("other"))
                     if x is not None):
                rel.append(p)
        if TRUST_LLM or stale:
            suspicious = set()
        else:
            suspicious = {i for i in ids_now if i in p_pos and i not in cons and i not in last_ids
                          and a_pos.get(i) is not None and a_pos[i] != p_pos[i]}
        if reverted:  # the reverted calendar's own positions are the target
            suspicious |= {i for i in ids_now if i in p_pos and i not in last_ids}
        # a stale previous calendar is not known-legal: use it only for events the LLM dropped
        p_use = {i: s for i, s in p_pos.items() if a_pos.get(i) is None} if stale else p_pos

        hulls = {} if (reverted or stale) else legal_hulls(events, cons, last_ids, p_pos, a_pos, users, asst, names)
        if INFER_DIR and not reverted and not stale:
            dmap = {e["event_id"]: e["duration"] for e in events}
            for i in ids_now:
                if i in cons or i in misparsed or i in last_ids or i not in p_pos or p_pos[i] is None:
                    continue
                lo, hi = (hulls[i][0], hulls[i][1]) if i in hulls else (p_pos[i], p_pos[i])
                dr = infer_direction(i, users[:-1], names)
                if dr == "low":
                    cons[i] = ("after", lo, None)
                elif dr == "up":
                    cons[i] = ("before", hi + dmap[i], None)
        last_cons = {i: c for i, c in cons.items() if i in last_ids}
        attempts = [
            (cons, True, unary, rel),
            (cons, False, unary, rel),
            (cons, False, {}, []),
            (last_cons, False, {}, []),
            ({}, False, {}, []),
        ]
        feas = {}
        sol = None
        for k, (cs, pin, un, rl) in enumerate(attempts):
            left = 40 - (time.time() - T0)
            if left <= 1:
                break
            key = id(cs)
            if key not in feas:   # cheap exact feasibility check before the min-move search
                feas[key] = pack(events, cs, time.time() + min(3, left / 4))
            if feas[key] is None:   # provably infeasible: relax right away
                continue
            budget = min(left - 1, 12 if k < 2 else 6)
            seed = None
            if isinstance(feas[key], dict) and not pin:   # the pinned pass must not be pre-empted
                seed = settle(dict(feas[key]), events, a_pos, p_use, cs, un, rl, suspicious, hulls,
                              deadline=time.time() + 2)
            sol = solve(events, a_pos, p_use, cs, time.time() + budget, un, rl, suspicious, pin, hulls, seed)
            if sol is not None:
                if un and cs is cons:
                    left = 40 - (time.time() - T0)
                    better = pref_pack(sol, events, a_pos, p_use, cs, un, rl, suspicious,
                                       time.time() + min(8, max(0, left - 2)), hulls)
                    if better:
                        sol = better
                break
        if sol is None:
            for cs in (cons, last_cons, {}):
                sol = pack(events, cs, time.time() + 3)
                if isinstance(sol, dict):
                    break
                sol = None
        if sol is None:
            return {"text": json.dumps(ans)}
        out = [{"event_id": ev["event_id"], "event_name": ev["event_name"],
                "start_time": fmt(sol[ev["event_id"]]), "duration": ev["duration"]} for ev in events]
        return {"text": json.dumps(out)}
    except Exception:
        return {"text": json.dumps(ans)}
