"""Language side of the reference app: splitting, complexity, step generation, reply classification,
kind wording. Every LLM helper has a deterministic rule fallback."""
import json
import re

import llm

# ---------------------------------------------------------------- kind wording
_BANNED_RE = re.compile(r"\b\w*(overdue|failed|missed|late|behind|forgot|lazy)\w*\b", re.I)
_REPL = {"later": "another time", "latest": "newest", "template": "layout", "templates": "layouts",
         "translate": "convert", "translation": "version", "plate": "dish", "plates": "dishes",
         "relate": "connect", "related": "connected", "calculate": "work out", "chocolate": "cocoa",
         "slate": "list", "dismissed": "closed", "update": "update", "translated": "converted",
         "collate": "gather", "late": "", "overdue": "", "behind": "", "missed": "", "failed": "",
         "forgot": "", "lazy": "", "articulate": "explain", "isolate": "separate", "emulate": "copy",
         "simulated": "practice", "accumulate": "build up", "regulate": "adjust", "elated": "happy",
         "belated": "", "palate": "taste", "lately": "recently"}


def kind(text):
    """Remove guilt wording from any agent-facing text."""
    if not text:
        return text
    text = re.sub(r"should(?:'ve| have)", "could have", text, flags=re.I)
    text = re.sub(r"red[- ]alert", "heads-up", text, flags=re.I)

    def sub(m):
        w = m.group(0)
        rep = _REPL.get(w.lower(), "")
        if rep and w[:1].isupper():
            rep = rep[:1].upper() + rep[1:]
        return rep

    text = _BANNED_RE.sub(sub, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r" ([,.!?])", r"\1", text).strip()


# ---------------------------------------------------------------- yes / no
def is_yes(t):
    t = t.strip().lower()
    return bool(re.search(r"^(y|yes|yeah|yep|yup|sure|ok|okay|please|definitely|absolutely|of course|"
                          r"sounds good|go ahead|let'?s do it|let'?s|do it|why not|great|perfect|alright|"
                          r"looks good|that works|works for me|good)\b", t)) and not is_no(t)


def is_no(t):
    t = t.strip().lower()
    return bool(re.search(r"^(no|nope|nah|not now|not really|don'?t|no thanks|leave it|keep it)\b", t))


_CHANGE_RE = re.compile(r"\b(but|change|instead|fewer|less|more|shorter|longer|smaller|bigger|add|remove|drop|"
                        r"different|split|merge|replace|swap|too|without|include|skip|rather|easier|tiny|"
                        r"tinier|simpler|combine|move|earlier|first step)\b", re.I)


def is_accept(t):
    t = t.strip().lower()
    if not re.search(r"\b(yes|yeah|yep|yup|looks good|look good|sounds good|ok|okay|sure|great|perfect|accept|"
                     r"good|love it|let'?s do it|do it|works|fine|go ahead|lgtm|deal)\b", t):
        return False
    return not _CHANGE_RE.search(t) and not is_no(t)


# ---------------------------------------------------------------- splitting
def _words(s):
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in ("and", "the", "a", "to", "i", "my")]


def split_dump(text):
    parts = re.split(r"[,;\n•]+|\s+(?:and then|also|plus)\s+|(?<=[.!?])\s+|\s+-\s+", text)
    parts = [p.strip(" .-*\t") for p in parts if p and p.strip(" .-*\t")]
    if len(parts) <= 1 and len(text.split()) >= 8:
        got = llm.chat_json([
            {"role": "system", "content": "You split a person's brain dump into separate to-do fragments. "
             "Keep each fragment's original words (including times and words like 'maybe'). "
             "Reply with only a JSON list of strings."},
            {"role": "user", "content": text}])
        if isinstance(got, list) and 1 < len(got) <= 15 and all(isinstance(x, str) and x.strip() for x in got):
            orig = set(_words(text))
            frag_words = set(w for x in got for w in _words(x))
            if orig and len(orig & frag_words) / len(orig) >= 0.8 and frag_words <= orig | {"s"}:
                return [x.strip() for x in got]
    return parts or [text.strip()]


_LEAD_RE = re.compile(r"^(?:(?:i\s+)?(?:need|have|want|got|ought)\s+to|i\s+should|gotta|remember\s+to|"
                      r"remind\s+me\s+to|don'?t\s+forget\s+to|add|todo:?|to\s+do:?|please)\s+", re.I)


def clean_fragment(frag):
    """Returns (cleaned text, tentative)."""
    f = frag.strip()
    tentative = bool(re.search(r"\b(maybe|perhaps|possibly|might)\b", f, re.I)) or f.endswith("?")
    f = f.rstrip("?!. ").strip()
    f = re.sub(r"\b(maybe|perhaps|possibly)\b\s*", "", f, flags=re.I).strip()
    for _ in range(2):
        f = _LEAD_RE.sub("", f).strip()
    return f or frag.strip(" ?"), tentative


# ---------------------------------------------------------------- complexity
_SIMPLE_VERBS = {"call", "text", "email", "mail", "phone", "ring", "buy", "pay", "book", "send", "pick", "drop",
                 "water", "feed", "walk", "take", "return", "reply", "message", "schedule", "cancel", "renew",
                 "print", "sign", "post", "grab", "order", "wash", "charge", "check", "ask", "tell", "remind",
                 "meet", "visit", "go", "submit", "file", "fill", "refill", "wish", "thank", "invite", "rsvp",
                 "confirm", "download", "install", "watch", "read", "listen", "eat", "cook", "make", "get",
                 "yoga", "run", "gym", "dentist", "doctor", "groceries", "laundry", "dishes", "vacuum"}
_VAGUE_RE = re.compile(r"\b(work on|working on|figure out|deal with|sort out|get .{1,40} together|look into|"
                       r"think about|catch up on|prepare for|prep for|start on|get started on|get going on|"
                       r"clean up|overhaul|revamp|organi[sz]e|plan out|apply (?:for|to)|finish|finally|"
                       r"tackle|handle|redo|research)\b", re.I)
_BIG_NOUNS = re.compile(r"\b(portfolio|pitch deck|deck|thesis|dissertation|project|application|applications|"
                        r"website|resume|résumé|cv|novel|book|taxes|tax return|presentation|report|proposal|"
                        r"business plan|move|moving|garage|wedding|essay|paper|course|grant|album|exhibition|"
                        r"residency|startup|budget|closet|apartment|house|renovation|job search|career)\b", re.I)


def is_big(title, estimate=None):
    """True / False / None (undecided -> ask the model)."""
    if estimate and estimate > 120:
        return True
    t = title.strip().lower()
    first = (re.findall(r"[a-z']+", t) or [""])[0]
    if first in _SIMPLE_VERBS and not _VAGUE_RE.search(t):
        return False
    if _VAGUE_RE.search(t) or _BIG_NOUNS.search(t):
        return True
    if len(t.split()) <= 3 or re.search(r"\b(lunch|dinner|coffee|breakfast|drinks|party|meet|meeting|call|chat)\b", t):
        return False
    return None


def llm_complexity(titles):
    """{title: bool} for titles the model judged; empty on failure."""
    if not titles:
        return {}
    got = llm.chat_json([
        {"role": "system", "content": "For each to-do, decide if it is big or vague: more than about two hours, "
         "or no clear first physical action (e.g. 'work on X', 'figure out Y'). Most to-dos are NOT big: errands, "
         "appointments, meals, calls and one-sitting chores are false. Answer true only for multi-hour projects or vague goals. "
         "Reply with only JSON: {\"<to-do>\": true|false, ...}"},
        {"role": "user", "content": json.dumps(titles)}])
    out = {}
    if isinstance(got, dict):
        for t in titles:
            v = got.get(t)
            if isinstance(v, bool):
                out[t] = v
    return out


def goal_name(title):
    g = title.strip()
    g = re.sub(r"^(get|work on|working on|figure out|deal with|sort out|finish|finally|start on|tackle|do)\s+",
               "", g, flags=re.I)
    g = re.sub(r"\s+(together|done|sorted|finished|ready)$", "", g, flags=re.I)
    g = re.sub(r"^(my|the|a|our)\s+", "", g, flags=re.I)
    return g.strip() or title.strip()


# ---------------------------------------------------------------- steps
VERBS = set("""
add apply arrange ask assemble attach book brainstorm browse buy calculate call capture change check choose
circle clean clear close collect color combine compare compile complete compose confirm copy create crop cut
decide define delete describe design download draft drag draw edit email export file fill find finalize fix
format gather go grab group highlight identify import jot label lay list look make map mark measure meet
message move name note number open order organize outline pack paste pay pick pin place plan polish post
practice prepare print prioritize put read record reorder replace reply request research reserve resize
review rewrite save scan schedule search select send set share shoot sign sketch skim sort spell split start
submit summarize swap take talk text tidy time tag test type underline update upload walk watch write
proofread rename refine retouch photograph scan curate assess trim tweak frame caption fold rehearse spend wrap
""".split())
_BAD_FIRST = {"work", "think", "deal", "figure", "handle", "try", "consider", "be", "do", "continue", "keep",
              "the", "a", "an", "your", "my", "this", "that", "some", "step", "first", "then", "next", "finally",
              "get", "stuff", "things", "maybe", "it", "start"}


def step_ok(title):
    w = (re.findall(r"[A-Za-z']+", title) or [""])[0].lower()
    if not w or w in _BAD_FIRST:
        return False
    if w in VERBS:
        return True
    return not (w.endswith("ing") or w.endswith("ly") or w.endswith("ed"))


def normalize_steps(steps):
    """Validate/clamp a list of {title, estimate_min}. Returns list or None."""
    if not isinstance(steps, list) or not (2 <= len(steps) <= 5):
        return None
    out = []
    for s in steps:
        if isinstance(s, str):
            s = {"title": s, "estimate_min": 30}
        if not isinstance(s, dict):
            return None
        title = kind(str(s.get("title", "")).strip().strip(".").strip())
        title = re.sub(r"^\d+[.)]\s*", "", title)
        if not title or len(title) > 90 or not step_ok(title):
            return None
        try:
            est = int(round(float(s.get("estimate_min") or s.get("minutes") or 30)))
        except (TypeError, ValueError):
            est = 30
        est = max(5, min(60, 5 * round(est / 5)))
        out.append({"title": title[:1].upper() + title[1:], "estimate_min": est})
    out[0]["estimate_min"] = min(out[0]["estimate_min"], 15)
    return out


_TEMPLATES = {
    "portfolio": [("Pick 3 favorite pieces for the {g}", 10), ("Gather the files and photos of those pieces", 30),
                  ("Write a short note about each piece", 45), ("Lay out the pieces in one simple document", 45),
                  ("Review the {g} against {done}", 30)],
    "deck": [("Write the 3 main points of the {g} on a sticky note", 10), ("Sketch one slide per main point", 30),
             ("Fill in the slides with words and pictures", 50), ("Rehearse the {g} out loud once", 20)],
    "default": [("Take 10 minutes to jot down a mini plan for: {g}", 10), ("Gather what you need for: {g}", 25),
                ("Spend 45 minutes on the first half of: {g}", 45), ("Finish and check it against {done}: {g}", 30)],
    "alt": [("Write one sentence about why this matters: {g}", 5), ("Collect everything you already have for: {g}", 25),
            ("Make a rough first pass at: {g}", 45), ("Polish and wrap up: {g}", 30)],
}


def rule_steps(goal_title, done=None, variant=0):
    g = goal_name(goal_title)
    low = goal_title.lower()
    key = "portfolio" if "portfolio" in low else "deck" if re.search(r"deck|presentation|talk|pitch", low) else "default"
    if variant % 2 == 1:
        key = "alt"
    done_s = (done or "").strip().rstrip(".") or "your picture of done"
    if len(done_s) > 50:
        done_s = "your picture of done"
    steps = [{"title": t.format(g=g, done=done_s), "estimate_min": m} for t, m in _TEMPLATES[key]]
    return normalize_steps(steps)


def llm_steps(goal_title, why, done, deadline_s, feedback=None, prev=None):
    sys = ("You help a person who finds planning hard turn a goal into tiny steps. Reply with only JSON: "
           "{\"steps\": [{\"title\": \"...\", \"estimate_min\": 10}, ...]}. Rules: 2 to 5 steps; every title "
           "starts with a concrete imperative verb (Write, Pick, Email, Open, List, Draft ...), under 60 characters; "
           "the first step takes 15 minutes or less and is very easy to start; every step takes 60 minutes or less; "
           "warm, plain words; never use the words late, overdue, behind, missed, failed.")
    user = {"goal": goal_title, "why_it_matters": why, "done_looks_like": done, "finish_by": deadline_s}
    if prev:
        user["current_steps"] = prev
        user["requested_change"] = feedback
    got = llm.chat_json([{"role": "system", "content": sys}, {"role": "user", "content": json.dumps(user)}],
                        smart=True)
    if isinstance(got, dict):
        got = got.get("steps")
    return normalize_steps(got)


def revise_rules(prev, feedback, goal_title, done, variant):
    f = feedback.lower()
    steps = [dict(s) for s in prev]
    m = re.search(r"\b(?:add|include)\s+(?:a\s+step\s+(?:to|for)\s+)?(.+)", feedback, re.I)
    if m:
        new = m.group(1).strip().rstrip(".")
        if not step_ok(new):
            new = "Plan " + new
        new = new[:1].upper() + new[1:]
        if len(steps) >= 5:
            steps.pop(-2)
        steps.insert(len(steps) - 1 if len(steps) > 2 else len(steps), {"title": new, "estimate_min": 30})
        return normalize_steps(steps)
    if re.search(r"\b(fewer|less|too many|shorter|merge|combine|simpler)\b", f) and len(steps) > 2:
        steps.pop(-2)
        return normalize_steps(steps)
    if re.search(r"\b(smaller|tiny|tinier|easier|too long|too big|quicker|less time)\b", f):
        for s in steps:
            s["estimate_min"] = max(5, s["estimate_min"] // 2)
        steps[0]["estimate_min"] = min(steps[0]["estimate_min"], 10)
        return normalize_steps(steps)
    if re.search(r"\b(more steps|more detail|split)\b", f) and len(steps) < 5:
        big = max(range(1, len(steps)), key=lambda i: steps[i]["estimate_min"])
        s = steps[big]
        half = max(5, s["estimate_min"] // 2)
        steps[big] = {"title": s["title"] + " (first half)", "estimate_min": half}
        steps.insert(big + 1, {"title": s["title"] + " (second half)", "estimate_min": half})
        return normalize_steps(steps)
    return rule_steps(goal_title, done, variant)


# ---------------------------------------------------------------- check-in replies
def classify_checkin(text):
    t = text.lower()
    if re.search(r"\b(not needed|don'?t need|no longer|drop it|drop|remove|delete|forget it|skip it|"
                 r"not important|doesn'?t matter|let it go|cancel)\b", t):
        return "drop"
    if re.search(r"\b(too big|too much|overwhelm\w*|break it|smaller|steps|huge|don'?t know where to start|"
                 r"hard to start|too hard)\b", t):
        return "big"
    if re.search(r"\b(wrong time|bad time|time|timing|busy|tired|morning|afternoon|evening|tonight|tomorrow|"
                 r"weekend|another day|different day|not now|move it|reschedule)\b", t):
        return "time"
    got = llm.chat_json([
        {"role": "system", "content": "A person was asked why a task keeps moving. Classify their reply as "
         "\"big\" (task feels too big), \"time\" (wrong time), \"drop\" (not needed) or \"unclear\". "
         "Reply with only JSON: {\"label\": \"...\"}"},
        {"role": "user", "content": text}])
    if isinstance(got, dict) and got.get("label") in ("big", "time", "drop"):
        return got["label"]
    return None


def llm_time(text, now_iso):
    """Ask the model for a start time when rules found none. Returns (iso|None, window|None)."""
    got = llm.chat_json([
        {"role": "system", "content": "Extract when a to-do should happen. Now is " + now_iso + " (local). "
         "Reply with only JSON: {\"start\": \"YYYY-MM-DDTHH:MM\" or null, \"exact\": true|false, "
         "\"window\": short phrase like \"this afternoon\" or null}"},
        {"role": "user", "content": text}])
    if isinstance(got, dict):
        s = got.get("start")
        if isinstance(s, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
            return s[:16], (None if got.get("exact") else (got.get("window") or None)), bool(got.get("exact"))
    return None, None, False
