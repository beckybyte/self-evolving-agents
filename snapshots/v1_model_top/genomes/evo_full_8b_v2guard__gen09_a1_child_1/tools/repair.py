"""Deterministic calendar repair / builder.

MODE = "hybrid": take the LLM calendar (blackboard[LLM_ROLE]); if it is broken (no JSON, overlap,
  out of 10:00-16:00, or - when STRICT - violates a constraint parsed from the user's messages),
  re-place as few events as possible with a small exact search. Otherwise return it unchanged.
MODE = "history": no LLM; start from the last assistant calendar in the chat history, apply the
  final user turn (add / move / remove / revert) and solve.
Constraint semantics: at T -> start == T; after T -> start >= T; before T -> end <= T;
between A and B -> start >= A and end <= B.
"""
import json
import re
import time

MODE = "history"
LLM_ROLE = "assistant"
STRICT = False
PREF_PASS = False

DAY0, DAY1 = 600, 960
PREF_W = 2.5  # one unmet preference is worth moving about one settled event
SAFE_W = 0.6  # move cost of an event whose constraint (or lack of one) we parsed
T0 = time.time()

# ---------------------------------------------------------------- time parsing
MER = r"(?:a\.?\s?m\.?|p\.?\s?m\.?)"
TIME_RE = re.compile(
    r"(?<![\d:.])(\d{1,2})(?::(\d{2}))?\s*(" + MER + r")(?![a-z])|\b(noon)\b|(?<![\d:])(\d{1,2}):(\d{2})(?!\d)",
    re.I)


def _mk(h, m, mer):
    h, m = int(h), int(m or 0)
    if mer:
        p = mer.lower().startswith("p")
        if p and h < 12:
            h += 12
        if not p and h == 12:
            h = 0
    elif h < 8:
        h += 12
    return h * 60 + m


def times_in(text):
    out = []
    for mt in TIME_RE.finditer(text):
        if mt.group(4):
            v = 720
        elif mt.group(1):
            v = _mk(mt.group(1), mt.group(2), mt.group(3))
        else:
            v = _mk(mt.group(5), mt.group(6), None)
        out.append((mt.start(), mt.end(), v))
    return out


def hhmm(v):
    return "%02d:%02d" % (v // 60, v % 60)


def to_min(s):
    try:
        h, m = str(s).strip().split(":")[:2]
        return int(h) * 60 + int(m[:2])
    except Exception:
        return None


# ---------------------------------------------------------------- constraint parsing
NUMT = r"(\d{1,2}(?::\d{2})?\s*" + MER + r"?|noon)"
BETWEEN_RE = re.compile(r"(?:between|from|window of)\s+" + NUMT + r"\s*(?:and|to|until|till|through|-|–|—)\s*" + NUMT, re.I)
DASH_RE = re.compile(NUMT + r"\s*(?:-|–|—)\s*" + NUMT, re.I)
AFTER_PRE = re.compile(r"(?:after|no earlier than|not earlier than|not before|later than|at or after|starting from|from|beginning at|any ?time after|on or after|past|beyond)\s*$", re.I)
AFTER_POST = re.compile(r"^\s*(?:or later|or after|onwards?|and later|at the earliest|or beyond|or thereafter|or afterwards?|"
                        r"or (?:at )?(?:any ?time|some ?time|whenever|anything) (?:after|later|past)|and (?:any ?time )?after)", re.I)
BEFORE_PRE = re.compile(r"(?:before|by|no later than|not later than|not after|until|till|prior to|earlier than|on or before|at or before|wrap(?:s|ped)? up by|ends? by|finish(?:es|ed)? by|done by)\s*$", re.I)
BEFORE_POST = re.compile(r"^\s*(?:or earlier|or before|at the latest|or sooner|or (?:at )?(?:any ?time|some ?time) (?:before|earlier))", re.I)
# negated cues flip the direction and must be checked first: "no later than 3pm" is a deadline,
# "not start before 2pm" / "no sooner than 2pm" / "can't do it until 1pm" are earliest starts
NEG = r"(?:\bnot\b|n't\b|\bnever\b|\bno\b)"
NEGL = r"(?:\bnot\b|n't\b|\bnever\b)"
NEG_BEFORE = re.compile(r"(?:" + NEG + r"\s+(?:any\s+)?(?:later|further)\s+than|" + NEGL + r"[^.,;!?\d]{0,25}?\b(?:after|past|beyond|later than|go(?:ing)? over|run(?:ning)? over))\s*$", re.I)
NEG_AFTER = re.compile(r"(?:" + NEG + r"\s+(?:any\s+)?(?:earlier|sooner)\s+than|" + NEGL + r"[^.,;!?\d]{0,25}?\b(?:before|until|till|prior to|earlier than|sooner than))\s*$", re.I)


def _pt(s):
    s = s.strip().lower()
    if s == "noon":
        return 720, True
    mt = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(" + MER + r")?", s, re.I)
    if not mt:
        return None, False
    if mt.group(3):
        return _mk(mt.group(1), mt.group(2), mt.group(3)), True
    return None, False


def _pair(a, b):
    va, ka = _pt(a)
    vb, kb = _pt(b)
    raw = lambda s: re.match(r"(\d{1,2})(?::(\d{2}))?", s.strip())
    if not kb:
        if not ka:
            ra, rb = raw(a), raw(b)
            if not (ra and rb and (ra.group(2) or rb.group(2))):
                return None
            va, vb = _mk(ra.group(1), ra.group(2), None), _mk(rb.group(1), rb.group(2), None)
        else:
            rb = raw(b)
            vb = _mk(rb.group(1), rb.group(2), None)
    elif not ka:
        ra = raw(a)
        if ra is None:
            return None
        h, m = int(ra.group(1)), int(ra.group(2) or 0)
        hb = (vb // 60)
        cand_pm = (h % 12 + 12) * 60 + m
        cand_am = (h % 12) * 60 + m if h != 12 else 720 + m
        va = cand_pm if cand_pm < vb and hb >= 12 and h < 12 and (h % 12 + 12) <= hb else cand_am
        if h == 12:
            va = 720 + m
    if va is None or vb is None or vb <= va:
        return None
    return va, vb


def parse_constraint_text(text):
    """Return ('at'|'after'|'before'|'between', a, b) or None from a clause."""
    for rx in (BETWEEN_RE, DASH_RE):
        for mt in rx.finditer(text):
            pr = _pair(mt.group(1), mt.group(2))
            if pr:
                return ("between", pr[0], pr[1])
    ts = times_in(text)
    if not ts:
        return None
    res = None
    for (s, e, v) in ts:
        pre, post = text[max(0, s - 40):s], text[e:e + 30]
        if NEG_BEFORE.search(pre):
            res = ("before", v, None)
        elif NEG_AFTER.search(pre):
            res = ("after", v, None)
        elif AFTER_POST.search(post) or AFTER_PRE.search(pre):
            res = ("after", v, None)
        elif BEFORE_POST.search(post) or BEFORE_PRE.search(pre):
            res = ("before", v, None)
        else:
            res = ("at", v, None)
    return res


# a bare "h" unit must be lower case and attached ("1.5h"): "4-H Club" is a name, not 4 hours
DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:(?:-|‑|–|\s)?\s*(min(?:ute)?s?|hours?|hrs?)|((?-i:h)))\b|\b(an? hour and a half|one and a half hours?|(?:an? |one )?hour and a half|half[ -]an[ -]hour|an?[ -]hour|one[ -]hour|1[ -]hour)\b", re.I)


def parse_duration(text):
    for mt in DUR_RE.finditer(text):
        if mt.group(1):
            n = float(mt.group(1))
            unit = (mt.group(2) or "h").lower()
            d = n if unit.startswith("m") else n * 60
        else:
            w = mt.group(4).lower()
            d = 30 if w.startswith("half") else (90 if "and a half" in w else 60)
        d = int(round(d))
        if 5 <= d <= 360:
            return d
    return None


ID_RE = re.compile(r"\(?\s*event[\s_-]*id\s*[:#=]?\s*(\d+)\s*\)?", re.I)
ID_RE2 = re.compile(r"\bevent\s+#?(\d+)\b", re.I)
PREF_CUE = re.compile(
    r"\b(ideally|prefer|would love|i'd love|if you can|if possible|suits me|would be perfect|would be great|would be nice|"
    r"i like to|i'm sharpest|my mornings|my afternoons|rather|kind of thing|sit somewhere|land in|happen early|toward|towards|"
    r"would ideally|nice to have|if it works|i tend to|best for me|ideal|i'd prefer|i would prefer|preferably|suits me best|works best|fresh mind|wind-down)\b", re.I)
# "clear" only as a verb with an object ("clear the 2pm slot"): "a clear window", "clear risk matrix" are small talk;
# "drop by" / "drop in" are visits, not removals
REMOVE_RE = re.compile(r"\b(remove|delete|cancel|scrap|take off|get rid of)\b|\bdrop\b(?!\s+(?:by|in|into)\b)|"
                       r"\bclear\s+(?:out\s+|off\s+|away\s+)?(?:the|my|it|that|this|them|those|event)\b", re.I)
REVERT_RE = re.compile(r"\b(revert|go back|restore|roll back|undo|return)\b[^.?!]*?\bturn\s*(\d+)", re.I)


MOVE_VERB = r"\b(?:move|moving|shift|push|reschedul\w*|change|bump|switch|slide|swap)\w*\b"
FROM_TO_RE = re.compile(MOVE_VERB + r"[^.?!]*?\bfrom\s+" + NUMT + r"\s*(?:to|until|till|over to)\s*" + NUMT, re.I)
SHIFT_RE = re.compile(
    r"\b(?:push|move|shift|bump|slide|nudge|delay|pull|bring)\w*\b[^.?!]*?(?:\b(back|later|forward|earlier|up|ahead)\b[^.?!]*?\bby\s+"
    r"(\d+(?:\.\d+)?\s*(?:-|‑|–|\s)?\s*(?:min(?:ute)?s?|hours?|hrs?)|an? hour|half an hour)|\bby\s+(\d+(?:\.\d+)?\s*(?:-|‑|–|\s)?\s*"
    r"(?:min(?:ute)?s?|hours?|hrs?)|an? hour|half an hour)\s*(later|earlier|back|forward)\b)", re.I)


def parse_shift(text):
    """'push X back by 30 minutes' -> +30 ; 'pull it forward by an hour' -> -60."""
    mt = SHIFT_RE.search(text)
    if not mt:
        return None
    word = (mt.group(1) or mt.group(4) or "").lower()
    amt = (mt.group(2) or mt.group(3) or "").lower()
    if "half an hour" in amt:
        d = 30
    elif amt.startswith(("an hour", "a hour")):
        d = 60
    else:
        d = parse_duration(amt)
    if not d:
        return None
    return d if word in ("back", "later") else -d


ABBR_RE = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Prof|St|Sr|Jr|vs|etc|approx|Inc|Co|Ltd|Dept|No)\.(?=\s)|\b(?:e\.g|i\.e)\.", re.I)


def split_sentences(par):
    par = ABBR_RE.sub(lambda m: m.group(0).replace(".", "\u2024"), par)  # "Dr. Patel" is not a sentence end
    parts = re.split(r"(?<=[.!?])\s+(?=[\"“'‘A-Za-z(])|\n+|\s+--\s+(?=[A-Z])", par)
    parts = [p.replace("\u2024", ".") for p in parts]
    return [p for p in parts if p.strip()]


def ids_in(text):
    ids = [int(x) for x in ID_RE.findall(text)]
    for x in ID_RE2.findall(text):
        if int(x) not in ids:
            ids.append(int(x))
    return ids


def _stem(w):
    # "Outcome Analysis" names "Analyzing Intervention Outcomes": compare 5-letter stems
    return w[:5] if len(w) > 5 else w


def tokens(s):
    return set(_stem(w) for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in STOP)


STOP = set("the and for with our new about could would you can add please slot event session meeting into calendar "
           "set move schedule lock thanks hey this that from my to of on in at it be".split())


def name_scores(text, names):
    """[(score, id)] best first; score = sum of 1/#names containing each shared word + in-order pair bonus."""
    tt = tokens(text)
    toks = {i: tokens(nm) for i, nm in names.items()}
    df = {}
    for nt in toks.values():
        for w in nt:
            df[w] = df.get(w, 0) + 1
    tw = re.findall(r"[a-z0-9]+", (text or "").lower())
    tgrams = set(zip(tw, tw[1:]))

    def run_bonus(nm):
        # "the Standup Vendor" names the event whose title contains that word pair in order
        nw = re.findall(r"[a-z0-9]+", (nm or "").lower())
        return 0.5 * sum(1 for g in zip(nw, nw[1:]) if g in tgrams and not set(g) <= STOP)

    return sorted(((sum(1.0 / df[w] for w in tt & nt) + (run_bonus(names[i]) if tt & nt else 0), i)
                   for i, nt in toks.items()), reverse=True)


def covers(text, name):
    """Share of the name's words that the text repeats (a full or near-full title quote)."""
    nt = tokens(name)
    return len(nt & tokens(text)) / len(nt) if nt else 0.0


def match_name(text, names):
    """Best event whose name shares rare words with text (word weight = 1/#names containing it)."""
    scores = name_scores(text, names)
    if not scores or scores[0][0] < 0.99:
        return None
    if len(scores) > 1 and scores[1][0] >= scores[0][0] - 0.25:
        return None
    return scores[0][1]


# ---------------------------------------------------------------- preferences (soft)
PREF_WINDOWS = [
    (r"before lunch|ahead of lunch|end of (?:my|the) morning", (600, 720)),
    (r"lunch|midday|mid-day|middle of the day|around noon|noon-?ish|centre of the day|center of the day", (690, 810)),
    (r"late in the day|end of (?:my|the) (?:day|afternoon)|wind-?down|late afternoon|toward the end|towards the end|close of|"
     r"evening|last thing|as late as possible|wrap up the day|final slot|tail end|later the better|late as", (840, 960)),
    (r"mid-?afternoon|middle of the afternoon", (780, 900)),
    (r"early afternoon|right after lunch|just after lunch|post-?lunch", (720, 870)),
    (r"mid-?morning|late morning|middle of the morning", (630, 720)),
    (r"early morning|first thing|before noon|start of (?:my|the) day|early in the day|in the morning|morning|early|"
     r"kick off the day|sharpest|as early as possible|earlier the better|first slot|to start the day|bright and early", (600, 720)),
    (r"afternoon|after lunch|after noon|later in the day|second half|later on|back half", (720, 960)),
    (r"first half", (600, 780)),
]


def parse_pref(sentence):
    s = sentence.lower()
    # "my mornings are for deep work, so X would land in the afternoon": the phrase attached to the
    # request (latest in the sentence, longest on ties) wins over context phrases.
    hits = []
    for pat, win in PREF_WINDOWS:
        for mt in re.finditer(pat, s):
            hits.append((mt.end(), mt.end() - mt.start(), pat, win))
    if hits:
        hits.sort(key=lambda h: (h[0], h[1]))
        _, _, pat, win = hits[-1]
        if re.search(r"\b(not|avoid|n't|never|no)\b[^.]*?(" + pat + ")", s):
            # avoid-window: approximate with the complement half of the day
            return (720, 960) if win[1] <= 780 else (600, 780)
        return win
    # a time-anchored wish ("ideally before 2pm", "sometime after 1", "between 1 and 3 would be great")
    c = parse_constraint_text(sentence)
    if c:
        k, a, b = c
        if k == "before" and a > DAY0:
            return (DAY0, a)
        if k == "after" and a < DAY1:
            return (a, DAY1)
        if k == "between":
            return (a, b)
    ts = times_in(sentence)
    if ts:
        v = ts[0][2]
        return (max(DAY0, v - 60), min(DAY1, v + 120))
    return None


# ---------------------------------------------------------------- history analysis
def content(m):
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
    return c or ""


def extract_calendar(text):
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    cands = []
    for mt in re.finditer(r"\[\s*\{.*?\}\s*\]", text, re.S):
        cands.append(mt.group(0))
    # greedy fallback
    mt = re.search(r"\[\s*\{.*\}\s*\]", text, re.S)
    if mt:
        cands.append(mt.group(0))
    best = None
    for c in cands:
        try:
            v = json.loads(c)
        except Exception:
            continue
        if isinstance(v, list) and v and all(isinstance(e, dict) and "event_id" in e for e in v):
            best = v
    if best is None:
        return None
    out = []
    for e in best:
        try:
            out.append({"event_id": int(e["event_id"]), "event_name": str(e.get("event_name", "")),
                        "start_time": e.get("start_time"), "duration": int(e.get("duration", 0))})
        except Exception:
            return None
    return out


ADD_RE = re.compile(r"\b(add|create|new|slot in|put|book|include|schedule|set up|squeeze in|fit in|pencil in|insert)\b", re.I)


REQ_RE = re.compile(r"\b(could|can|would|will you|please|let's|i'd like|i would like|i want|i need|i'd love|need to|want to)\b|\?", re.I)
SELF_RE = re.compile(r"\b(?:i'll|i will|i'm going to|i am going to|i plan to)\b", re.I)


def scrub(text, names):
    """Blank out quoted titles and known event names so digits in them ("4-H Club", "Q3", "9am Standup")
    are not read as durations or times."""
    out = re.sub(r"[\"“][^\"”]{4,}[\"”]", " EVT ", text)
    for nm in sorted({n for n in names.values() if n and len(n) >= 6}, key=len, reverse=True):
        if re.search(r"\d", nm):
            out = re.sub(re.escape(nm), " EVT ", out, flags=re.I)
    return out


def analyse(messages, extra_names=None):
    """Walk the user turns; return per-event info and final-turn info.
    A clause that names no id and matches no known event (typically "add X for 50 min" without an id)
    is held back and attached to the event that first appears in the next assistant calendar."""
    info = {}  # id -> {"cons": tuple|None, "known": bool, "dur": int|None, "name": str}
    prefs = []
    names = dict(extra_names or {})
    titles = {}  # id -> title on the latest calendar that lists it (names keeps the first-seen wording)
    asst_cals = []
    final = {"ids": set(), "remove": set(), "revert": None, "pref_ids": set(),
             "rm_at": {}, "mention_at": {}, "cal_turn": []}
    pending = []  # (gtext, uturn, is_last)
    cur_ids = set()  # ids on the latest calendar
    uturn = -1
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    last_u = users[-1] if users else -1

    def apply(gtext, gid, turn, is_last):
        ent = info.setdefault(gid, {"cons": None, "known": False, "dur": None, "name": None})
        is_rm = bool(REMOVE_RE.search(gtext)) and not re.search(r"\badd\b", gtext, re.I)
        if is_rm:
            final["rm_at"][gid] = turn
        else:
            final["mention_at"][gid] = turn
        if is_last:
            final["ids"].add(gid)
            if is_rm:
                final["remove"].add(gid)
        ptext = scrub(gtext, names)
        sh = parse_shift(ptext)
        d = None if sh else parse_duration(ptext)
        if d:
            ent["dur"] = d
            ent["dur_turn"] = turn
        ft = FROM_TO_RE.search(ptext)
        ftp = _pair(ft.group(1), ft.group(2)) if ft else None
        if ft and (_pt(ft.group(2))[0] is not None or ftp):
            v = _pt(ft.group(2))[0]
            c = ("at", v if v is not None else ftp[1], None)
        elif sh:
            c = None
            ent["shift"] = (sh, turn)
        else:
            c = parse_constraint_text(re.sub(r"\d+(?:\.\d+)?\s*[-‑–]?\s*min\w*", " ", ptext))
        if c:
            ent["cons"], ent["known"] = c, True
            ent["cons_turn"] = turn
            ent.pop("shift", None)
        elif ADD_RE.search(gtext) and d:
            ent["cons"], ent["known"] = None, True
        elif not ent["known"] and re.search(r"\b(add|create|new|book)\b", gtext, re.I):
            ent["cons"], ent["known"] = None, True
        nm = re.search(r"[\"“]([^\"”]{4,})[\"”]", gtext)
        if nm:
            ent["name"] = nm.group(1)
            names.setdefault(gid, ent["name"])
        elif ent["name"] is None:
            nm = re.search(r"(?:add|called|titled|named|schedule|slot in|book)\s+(?:a|an|the|new|event)?\s*(?:\d+\s*[-‑–]?\s*minute\s+)?(?:event\s+)?(?:called\s+|titled\s+)?(.+?)\s*(?:\(|\bfor\b|\bto my\b|\binto\b|\?|$)", gtext, re.I)
            if nm:
                ent["name"] = nm.group(1).strip(" ,.:")
                names.setdefault(gid, ent["name"])

    def resolve_alias(text):
        cur = {i: n for i, n in names.items() if i in cur_ids}
        mid = match_name(text, cur) if cur else None
        alt = match_name(text, names)
        if mid is None:
            return alt
        if alt is not None and alt != mid:
            # an event the latest calendar lost (or never placed) can still be the one meant: take it
            # when it fits the words clearly better than the best event on the calendar
            sc = dict((i, v) for v, i in name_scores(text, names))
            if sc.get(alt, 0) >= sc.get(mid, 0) + 1.0 and covers(text, names[alt]) > covers(text, names[mid]):
                return alt
        return mid

    def explained(text, i):
        # how many of the reference's words this event accounts for, under either of its names
        return len(tokens(text) & (tokens(names.get(i)) | tokens(titles.get(i))))

    def resolve(text, is_last):
        """Name reference -> id: events on the current calendar first, then anything ever named.
        In the final turn the user usually paraphrases the calendar title, while names may hold an
        older user wording (only an inner quote, or another phrasing): when matching against the
        current titles disagrees, keep whichever event accounts for more of the user's words."""
        mid = resolve_alias(text)
        if not is_last:
            return mid
        tcur = {i: t for i, t in titles.items() if i in cur_ids}
        tid = match_name(text, tcur) if tcur else None
        if tid is None or tid == mid:
            return mid
        if mid is None or explained(text, tid) > explained(text, mid):
            return tid
        return mid

    def split_to_time_sentence(gtext, is_last):
        """An id-less clause naming several events ("I'm glad the Brand Workshop wraps by 11:30. The Client
        Pitch is sparking ideas"): the time belongs to the event named in the sentence that holds it, not
        to whichever event the whole clause matches best. Returns True when it applied the clause itself."""
        ss = split_sentences(gtext)
        if len(ss) < 2:
            return False
        tss = [s for s in ss if times_in(scrub(s, names))]
        if not tss:
            return False
        tids = {resolve(s, is_last) for s in tss}
        if len(tids) != 1 or None in tids:
            return False
        x = tids.pop()
        if resolve(gtext, is_last) == x:
            return False
        apply(" ".join(tss), x, uturn, is_last)
        rest = " ".join(s for s in ss if s not in tss)
        if rest and parse_duration(scrub(rest, names)):
            rid = resolve(rest, is_last)
            if rid is not None and rid != x:
                apply(rest, rid, uturn, is_last)
        return True

    for idx, m in enumerate(messages):
        role = m.get("role")
        text = content(m)
        if role == "assistant":
            cal = extract_calendar(text)
            asst_cals.append(cal)
            final["cal_turn"].append(uturn + 1)
            if cal:
                new = [e for e in cal if e["event_id"] not in cur_ids]
                for e in cal:
                    names.setdefault(e["event_id"], e["event_name"])
                    if e["event_name"]:
                        titles[e["event_id"]] = e["event_name"]
                if pending:
                    newn = {e["event_id"]: e["event_name"] for e in new}
                    for gtext, t, il in pending:
                        gid = None
                        if len(pending) == 1 and len(newn) == 1:
                            gid = list(newn)[0]
                        elif newn:
                            gid = match_name(gtext, newn)
                        if gid is None:
                            gid = match_name(gtext, {e["event_id"]: e["event_name"] for e in cal})
                        if gid is not None:
                            apply(gtext, gid, t, il)
                pending = []
                cur_ids = {e["event_id"] for e in cal}
            continue
        if role == "system":
            sc = extract_calendar(text)
            if sc:
                final["sys_cal"] = sc
                cur_ids = {e["event_id"] for e in sc}
                for e in sc:
                    names.setdefault(e["event_id"], e["event_name"])
                    if e["event_name"]:
                        titles[e["event_id"]] = e["event_name"]
            continue
        if role != "user":
            continue
        is_last = idx == last_u
        uturn += 1
        pending = []
        if is_last:
            rv = REVERT_RE.search(text)
            if rv:
                final["revert"] = int(rv.group(2))
        pars = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        for pi, par in enumerate(pars):
            sents = split_sentences(par)
            par_ids = ids_in(par)
            # attach id-less sentences to neighbouring id sentence when paragraph has one id
            groups = []
            if len(set(par_ids)) <= 1:
                groups = [(par, sorted(set(par_ids)))]
            else:
                cur, cids = "", []
                for s in sents:
                    sid = ids_in(s)
                    if sid and cids and set(sid) != set(cids):
                        groups.append((cur, cids))
                        cur, cids = "", []
                    cur += " " + s
                    cids = sorted(set(cids) | set(sid))
                if cur:
                    groups.append((cur, cids))
            for gtext, gids in groups:
                prefy = bool(PREF_CUE.search(gtext)) and (pi > 0 or not times_in(gtext)) and \
                    (any(re.search(pat, gtext.lower()) for pat, _ in PREF_WINDOWS) or (pi > 0 and times_in(gtext)))
                if not gids:
                    # only whole turns that name no id at all (ids come from the assistant), phrased as a request
                    adding = not ids_in(text) and bool(ADD_RE.search(gtext)) and bool(REQ_RE.search(gtext)) \
                        and not SELF_RE.search(gtext) and parse_duration(scrub(gtext, names)) is not None \
                        and not prefy and not REMOVE_RE.search(gtext)
                    if adding:
                        # "slot the Stakeholder Briefing: New ... for 50 minutes at 3pm" re-times a known event:
                        # a text quoting most of an existing title is not a new event
                        hit = match_name(gtext, names)
                        if hit is not None and covers(gtext, names[hit]) >= 0.6 and len(tokens(names[hit])) >= 2:
                            adding = False
                    if not adding and not prefy and split_to_time_sentence(gtext, is_last):
                        continue
                    mid = None if adding else resolve(gtext, is_last)
                    if mid is None:
                        if adding or not prefy:
                            if is_last and adding and not ids_in(text) and pi == 0:
                                # a new event in the final turn: next free id
                                known = set(names) | set(info)
                                for c in asst_cals:
                                    known |= {e["event_id"] for e in (c or [])}
                                nid = max(known) + 1 if known else 0
                                apply(gtext, nid, uturn, True)
                            elif not is_last and not ids_in(text) and not SELF_RE.search(gtext):
                                pending.append((gtext, uturn, is_last))
                        continue
                    gids = [mid]
                if prefy:
                    for gid in gids:
                        # one preference per sentence mentioning gid
                        ss = split_sentences(gtext)
                        cand = [s for s in ss if gid in ids_in(s)] or (ss if len(gids) == 1 else [])
                        for s in cand + ([gtext] if len(gids) == 1 else []):
                            w = parse_pref(s)
                            if w:
                                prefs.append({"event_id": gid, "start": w[0], "end": w[1]})
                                if is_last:
                                    final["pref_ids"].add(gid)
                                break
                    continue
                if len(gids) > 1:
                    # several ids in one clause: only durations/names are safe
                    continue
                apply(gtext, gids[0], uturn, is_last)
    return info, prefs, names, asst_cals, final


# ---------------------------------------------------------------- solver
def allowed(cons, dur):
    lo, hi = DAY0, DAY1 - dur
    if cons:
        k, a, b = cons
        if k == "at":
            lo, hi = max(lo, a), min(hi, a)
        elif k == "after":
            lo = max(lo, a)
        elif k == "before":
            hi = min(hi, a - dur)
        elif k == "between":
            lo, hi = max(lo, a), min(hi, b - dur)
    return lo, hi


def solve(events, deadline=40.0):
    """events: id -> {dur, lo, hi, base(start|None), w(move cost), pref(list of (s,e))}.
    Minimise sum of move costs + 0.3*unmet prefs + tiny displacement."""
    ids = sorted(events)
    n = len(ids)
    best = [None, 1e18]

    def pref_pen(i, s):
        ev = events[i]
        pen = 0.0
        for (a, b) in ev["pref"]:
            if not (s >= a and s + ev["dur"] <= b):
                pen += PREF_W
        return pen

    order_subsets = []
    for mask in range(1 << n):
        moved = [ids[k] for k in range(n) if mask >> k & 1]
        fixed = [i for i in ids if i not in moved]
        ok = True
        for i in fixed:
            ev = events[i]
            if ev["base"] is None or not (ev["lo"] <= ev["base"] <= ev["hi"]):
                ok = False
                break
        if not ok:
            continue
        cost = sum(events[i]["w"] for i in moved)
        order_subsets.append((cost, len(moved), mask, moved, fixed))
    order_subsets.sort(key=lambda x: (x[0], x[1]))

    for cost, _, mask, moved, fixed in order_subsets:
        if cost >= best[1] or time.time() - T0 > deadline:
            break
        iv = sorted((events[i]["base"], events[i]["base"] + events[i]["dur"]) for i in fixed)
        if any(iv[k][1] > iv[k + 1][0] for k in range(len(iv) - 1)):
            continue
        fixed_pen = sum(pref_pen(i, events[i]["base"]) for i in fixed)
        if cost + fixed_pen >= best[1]:
            continue
        mv = sorted(moved, key=lambda i: (events[i]["hi"] - events[i]["lo"], -events[i]["dur"]))
        assign = {i: events[i]["base"] for i in fixed}
        occ = list(iv)
        nodes = [0]

        def free(s, d):
            for (a, b) in occ:
                if s < b and a < s + d:
                    return False
            return True

        def cands(i):
            ev = events[i]
            d, lo, hi = ev["dur"], ev["lo"], ev["hi"]
            pts = {lo, hi}
            if ev["base"] is not None:
                pts.add(ev["base"])
            for (a, b) in occ:
                pts.add(b)
                pts.add(a - d)
            for (a, b) in ev["pref"]:
                pts.add(a)
                pts.add(b - d)
            pts |= set(range(lo - lo % 15 + 15 if lo % 15 else lo, hi + 1, 15))
            res = [p for p in pts if lo <= p <= hi and free(p, d)]
            res.sort(key=lambda p: (pref_pen(i, p), abs(p - ev["base"]) if ev["base"] is not None else p))
            return res

        def dfs(k, acc):
            nodes[0] += 1
            if nodes[0] > 60000 or acc >= best[1]:
                return
            if k == len(mv):
                tot = acc + 1e-4 * sum(abs(assign[i] - events[i]["base"]) for i in mv if events[i]["base"] is not None)
                if tot < best[1]:
                    best[0], best[1] = dict(assign), tot
                return
            i = mv[k]
            for p in cands(i):
                assign[i] = p
                occ.append((p, p + events[i]["dur"]))
                dfs(k + 1, acc + pref_pen(i, p))
                occ.pop()
                del assign[i]

        dfs(0, cost + fixed_pen)
    if best[0] is None and time.time() - T0 < deadline:
        # the bounded search can miss an exact packing on a full day; the subset DP cannot
        return dp_schedule(events)
    return best[0]


def dp_schedule(events):
    """Exact feasibility for one machine with start windows: over subsets, keep the earliest finish
    time of a left-packed order. Returns {id: start} or None. Ignores bases and preferences."""
    ids = sorted(events)
    n = len(ids)
    dp = {0: (DAY0, ())}
    for mask in range(1 << n):
        if mask not in dp:
            continue
        t, order = dp[mask]
        for j in range(n):
            if mask >> j & 1:
                continue
            ev = events[ids[j]]
            s = max(t, ev["lo"])
            if s % 5:
                s += 5 - s % 5
            if s > ev["hi"]:
                continue
            nm = mask | (1 << j)
            if nm not in dp or dp[nm][0] > s + ev["dur"]:
                dp[nm] = (s + ev["dur"], order + ((ids[j], s),))
    full = (1 << n) - 1
    return dict(dp[full][1]) if full in dp else None


def overlaps_or_oow(cal):
    iv = []
    for e in cal:
        s = to_min(e["start_time"])
        if s is None or e["duration"] <= 0:
            return True
        if s < DAY0 or s + e["duration"] > DAY1:
            return True
        iv.append((s, s + e["duration"]))
    iv.sort()
    if len({e["event_id"] for e in cal}) != len(cal):
        return True
    return any(iv[k][1] > iv[k + 1][0] for k in range(len(iv) - 1))


def violates(cal, info):
    for e in cal:
        ent = info.get(e["event_id"])
        if not ent or not ent["cons"]:
            continue
        lo, hi = allowed(ent["cons"], e["duration"])
        s = to_min(e["start_time"])
        if s is None or not (lo <= s <= hi):
            return True
    return False


def build(cal, info, prefs, base_from_cal=True, changed=(), soft=()):
    events = {}
    for e in cal:
        i = e["event_id"]
        ent = info.get(i, {})
        dur = ent.get("dur") or e["duration"]
        cons = ent.get("cons")
        lo, hi = allowed(cons, dur)
        base = to_min(e["start_time"]) if base_from_cal else None
        if base is not None and base % 5:
            base = None
        w = 1.0 if ent.get("known") else 3.0
        if i in changed:
            w = 0.5
        if i in soft and ent.get("known"):
            w = 0.1
        events[i] = {"dur": dur, "lo": lo, "hi": hi, "base": base, "w": w,
                     "pref": [(p["start"], p["end"]) for p in prefs if p["event_id"] == i],
                     "name": e["event_name"]}
    return events


def render(cal_names, sol, events):
    out = []
    for i in sorted(sol, key=lambda k: sol[k]):
        out.append({"event_id": i, "event_name": cal_names.get(i, "Event %d" % i),
                    "start_time": hhmm(sol[i]), "duration": events[i]["dur"]})
    return json.dumps(out, ensure_ascii=False)


def _free(ev):
    e = dict(ev)
    e["lo"], e["hi"] = DAY0, DAY1 - e["dur"]
    return e


def _feasible(events):
    """Cheap packing check: one search with every event movable."""
    return dp_schedule(events) is not None


def relax(events, keep=()):
    """Infeasible: some parsed constraint is wrong. Drop as few as possible, most suspicious first:
    (1) constraints the previous calendar already violates, (2) one older constraint at a time,
    (3) all older constraints, (4) everything. Constraints from the final turn (keep) go last."""
    def attempt(drop):
        ev2 = {i: (_free(e) if i in drop else dict(e)) for i, e in events.items()}
        if not _feasible(ev2):
            return None
        sol = solve(ev2, deadline=55.0)
        return (sol, ev2) if sol else None

    old = [i for i in events if i not in keep]
    bad = {i for i in old if events[i]["base"] is not None and not (events[i]["lo"] <= events[i]["base"] <= events[i]["hi"])}
    levels = []
    if bad:
        levels.append(bad)
    singles = sorted(old, key=lambda i: (-events[i]["w"], events[i]["hi"] - events[i]["lo"]))
    levels += [bad | {i} for i in singles]
    levels += [set(old), set(events)]
    for drop in levels:
        if time.time() - T0 > 55:
            break
        r = attempt(drop)
        if r:
            return r
    return None, events


def run_hybrid(blackboard, messages):
    raw = blackboard.get(LLM_ROLE, "") or ""
    cal = extract_calendar(raw)
    info, prefs, names, asst_cals, final = analyse(
        messages, {e["event_id"]: e["event_name"] for e in (cal or [])})
    if cal is None:
        prev = [c for c in asst_cals if c]
        prev = prev or ([final["sys_cal"]] if final.get("sys_cal") else [])
        if not prev:
            return re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        cal = prev[-1]
    broken = overlaps_or_oow(cal) or (STRICT and violates(cal, info))
    if not broken:
        if PREF_PASS and final["pref_ids"]:
            # only the events the user just voiced a preference about may move
            events = build(cal, info, prefs, soft=final["pref_ids"])
            for i, ev in events.items():
                if ev["w"] > 0.1:
                    ev["w"] = 100.0
            sol = solve(events, deadline=20.0)
            if sol:
                return render({e["event_id"]: e["event_name"] for e in cal}, sol, events)
        return json.dumps(cal, ensure_ascii=False)
    # dedupe ids
    seen, c2 = set(), []
    for e in cal:
        if e["event_id"] not in seen:
            seen.add(e["event_id"])
            c2.append(e)
    cal = c2
    events = build(cal, info, prefs, changed=final["ids"], soft=final["pref_ids"])
    sol = solve(events)
    if sol is None:
        sol, events = relax(events)
    if sol is None:
        return json.dumps(cal, ensure_ascii=False)
    return render({e["event_id"]: e["event_name"] for e in cal}, sol, events)


def run_history(blackboard, messages):
    info, prefs, names, asst_cals, final = analyse(messages)
    prev = [c for c in asst_cals if c]
    base = None
    if final["revert"] is not None and asst_cals:
        k = final["revert"]
        for cand in (k - 1, k):
            if 0 <= cand < len(asst_cals) and asst_cals[cand]:
                base = asst_cals[cand]
                break
    base_turn = None
    if base is not None:
        base_turn = final["cal_turn"][[id(c) for c in asst_cals].index(id(base))]
    if base is None and prev:
        base = prev[-1]
        base_turn = final["cal_turn"][max(k for k, c in enumerate(asst_cals) if c)]
    if base is None:
        base = final.get("sys_cal")
        base_turn = 0
    if final["revert"] is not None:
        since = 10 ** 6  # reverting: only the final turn's own edits apply
    else:
        since = base_turn if base_turn is not None else 0
    cal = [dict(e) for e in (base or [])]
    have = {e["event_id"] for e in cal}
    # events known from user text but absent from the base calendar
    for i, ent in sorted(info.items()):
        if i in have:
            continue
        if base is not None and i not in final["ids"] and final["mention_at"].get(i, -1) < since:
            continue
        if final["rm_at"].get(i, -1) >= since and final["rm_at"][i] >= final["mention_at"].get(i, -1):
            continue
        if not ent.get("dur"):
            continue
        cal.append({"event_id": i, "event_name": ent.get("name") or names.get(i) or "Event %d" % i,
                    "start_time": None, "duration": ent["dur"]})
    gone = {i for i, t in final["rm_at"].items() if t >= since and t >= final["mention_at"].get(i, -1)}
    cal = [e for e in cal if e["event_id"] not in final["remove"] and e["event_id"] not in gone]
    if not cal:
        return "[]"
    for e in cal:
        if e["start_time"] is None:
            e["start_time"] = "99:99"
    # durations read before the base calendar was written are already reflected in it (it is the latest
    # calendar the user accepted); only a duration given after it - normally in the final turn - overrides it
    n_user = sum(1 for m in messages if m.get("role") == "user")
    cut = (n_user - 1) if final["revert"] is not None else (base_turn if base_turn is not None else 0)
    for e in cal:
        ent = info.get(e["event_id"])
        if ent and ent.get("dur") and e["start_time"] != "99:99" and e.get("duration") and \
                ent.get("dur_turn", -1) < cut and ent["dur"] != e["duration"]:
            info[e["event_id"]] = dict(ent, dur=e["duration"])
    # a relative move ("push it back 30 min") becomes an exact start, from where the event sits now
    for e in cal:
        ent = info.get(e["event_id"])
        if ent and ent.get("shift") and e["start_time"] not in (None, "99:99"):
            ent["cons"], ent["known"] = ("at", to_min(e["start_time"]) + ent["shift"][0], None), True
            ent["cons_turn"] = ent["shift"][1]
    # earlier calendars already honour every earlier constraint: a parsed constraint that a later calendar
    # breaks is a mis-parse, so drop it and keep that event where the history put it
    suspect = set()
    for i, ent in info.items():
        if not ent.get("cons") or i in final["ids"] or ent.get("cons_turn") is None:
            continue
        for c2, t in zip(asst_cals, final["cal_turn"]):
            if not c2 or t <= ent["cons_turn"]:
                continue
            for e2 in c2:
                if e2["event_id"] == i:
                    lo, hi = allowed(ent["cons"], e2["duration"])
                    s2 = to_min(e2["start_time"])
                    if s2 is not None and not (lo <= s2 <= hi):
                        suspect.add(i)
    for i in suspect:
        info[i] = dict(info[i], cons=None)
    events = build(cal, info, prefs, changed=final["ids"])
    for i in suspect:
        if i in events:
            events[i]["w"] = 5.0
    for i, ev in events.items():
        if ev["base"] is not None and ev["base"] >= 99 * 60:
            ev["base"] = None
        if i in final["pref_ids"]:
            ev["w"] = 0.1
        elif i in final["ids"]:
            ev["w"] = 0.5
        elif ev["base"] is not None and ev["w"] < 5.0:
            # moving an event costs nothing in grading; it is only a hedge against constraints we failed
            # to read. Events whose request we parsed (with or without a time limit) are cheap to move so a
            # preference (PREF_W) can always buy a few of them; never-parsed events stay expensive.
            ev["w"] = SAFE_W if info.get(i, {}).get("known") else 2.0
    sol = solve(events) if _feasible(events) else None
    if sol is None:
        sol, events = relax(events, keep=final["ids"])
    if sol is None:
        return json.dumps([e for e in cal if e["start_time"] != "99:99"], ensure_ascii=False)
    return render({e["event_id"]: e["event_name"] for e in cal}, sol, events)


def run(blackboard, messages):
    try:
        if MODE == "history":
            return {"text": run_history(blackboard, messages)}
        return {"text": run_hybrid(blackboard, messages)}
    except Exception:
        raw = blackboard.get(LLM_ROLE, "") or ""
        return {"text": re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip() or "[]"}
