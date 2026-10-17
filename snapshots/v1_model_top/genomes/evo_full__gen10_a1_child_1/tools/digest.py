"""Compact the chat for the extractor: only the user turns that make a request, numbered.
Assistant replies, system boilerplate and pure chit-chat turns (no event id, no number, no
time-of-day or wish wording) are dropped; greetings and thanks are trimmed from the rest.
Inside kept turns, pure pleasantry sentences are dropped too and "(event id: N)" is written "(id N)".
Turns are one per line in conversation order (no "Tn:" labels) and request politeness ("could you please")
is cut, since the extractor reads the imperative the same way.
A sentence whose whole content solve.py overrides from the raw messages anyway (one event, a clock
rule user_rules reads and/or a length user_durations reads, nothing else) is not shown to the
extractor: the LLM's value for it is always replaced, so it only cost tokens.
"""
import os
import re
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import solve as S
except Exception:
    S = None

INFO0 = re.compile(r"\d|\b(?:noon|midday|morning|afternoon|evening|lunch|early|earlier|late|later|first|last|before|after|"
                   r"between|until|till|by|hours?|hrs?|minutes?|mins?|half|quarter|scratch|forget|anymore|never\s*mind|actually|"
                   r"wait|oops|mistake|sorry|correction|wrong|not|don't|no|also|"
                   r"move|shift|push|pull|bring|change|make|set|extend|lengthen|shorten|cut|trim|resched\w*|swap|switch|"
                   r"update|instead|keep|need|want|should|must|can't|cannot|unavailable|busy|block|same|rename|call)\b", re.I)
REL1 = re.compile(r"back[\s-]*to[\s-]*back|next\s+to|adjacent|apart|right\s+(?:after|before)|"
                  r"\b(?:before|after)\s+(?:the|my|our|it|lunch|that|this)\b|same|rename|\bname\b|call\s+it|earlier|later|"
                  r"gap|buffer|break|first|last|early|late|evening", re.I)
PRON = re.compile(r"\b(?:it|its|it's|that|this|them|they|both|those|these|one|former|latter)\b", re.I)
QUOTE2 = re.compile(r"[\"“”‘’']([^\"“”‘’']{3,120})[\"“”‘’'](?!\w)")


def _covered(seg):
    """True if every piece of information in this one-event sentence is re-read by solve.py."""
    ids = set(int(x) for x in S.EID.findall(seg))
    if len(ids) != 1:
        return False
    eid = next(iter(ids))
    for part in re.split(r"(?<=[.?!])\s+", seg):
        if not S.EID.search(part) and INFO0.search(part):
            return False
    body = S.EID.sub(" ", seg)
    if (S.SOFT.search(body) or S.FUZZY.search(body) or S.ADD.search(body) or S.REMOVE.search(body)
            or QUOTE2.search(body) or REL1.search(body)):
        return False
    one = [{"role": "user", "content": seg}]
    r = S.read_rule(seg)
    if r == "__free__" and not S.CONSTR_WORD.search(body):
        return False
    if r is not None and S.user_rules(one) != {eid: r}:
        return False
    if re.search(S.CLK, body, re.I) and (r is None or r == "__free__"):
        return False
    d = S.read_duration(seg)
    if d is not None and (d[0] != "set" or S.user_durations(one).get(eid) != [d]):
        return False
    has_len = any(rx.search(body) for rx in (S.D_H, S.D_M, S.D_HALF, S.D_QUARTER))
    if has_len and d is None:
        return False
    if re.search(r"\d", re.sub(S.CLK, " ", body, flags=re.I)) and d is None:
        return False
    return r is not None or d is not None


def _drop_covered(t):
    if S is None:
        return t
    try:
        segs = S._sentences(S._norm(t))
        if len(segs) > 1 and not all(S.EID.search(x) for x in segs):
            return t  # an id-less sentence may point back at a neighbour: keep the turn whole
        cov = [_covered(x) for x in segs]
        if any(cov) and any(PRON.search(S.EID.sub(" ", x)) for x, c in zip(segs, cov) if not c):
            return t  # a kept sentence may say "it" about a dropped one
        keep = [x for x, c in zip(segs, cov) if not c]
    except Exception:
        return t
    if len(keep) == len(segs):
        return t
    return " ".join(keep)

KEEP = re.compile(r"\d|\bnoon\b|midday|morning|afternoon|lunch|remove|delete|cancel|drop|scrap|get rid|"
                  r"no longer|ideal|prefer|would love|i'd love|rather|if possible|suits|"
                  r"next to|back[\s-]to[\s-]back|apart", re.I)
FLUFF = re.compile(r"^\s*(?:hey|hi|hello)(?: there)?[,!.]?\s*|\s*(?:thanks|thank you)(?: so much| a lot)?[.!]*\s*$", re.I)
# a sentence that is only politeness: no request verb, no pronoun pointing at an event, nothing KEEP-worthy
PLEASANT = re.compile(r"^(?:thanks|thank you|cheers|great|perfect|awesome|lovely|wonderful|nice|ok(?:ay)?|cool|good|"
                      r"sounds good|looks good|much appreciated|i appreciate|appreciate it|hope you|have a)\b", re.I)
ACTION = re.compile(r"\b(?:it|that|this|them|one|make|change|move|shift|push|add|put|set|need|want|keep|longer|shorter|"
                    r"extend|cut|could|can|please|should|must|event|meeting|call|session)\b", re.I)
# request politeness the extractor does not need ("Could you please move ..." -> "move ...")
ASK = re.compile(r"\b(?:could|can|would|will)\s+you\s+(?:please\s+|kindly\s+)?(?=[a-z])|\bplease\s+(?=[a-z])|,?\s*\bplease\b,?(?=\s*[?.!]|$)", re.I)
IDREF = re.compile(r"\(\s*event\s*id\s*[:#]?\s*(\d+)\s*\)", re.I)


def _text(c):
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def _trim(t):
    parts = re.split(r"(?<=[.?!])\s+", t)
    keep = [p for p in parts if not (PLEASANT.search(p) and not KEEP.search(p) and not ACTION.search(p))]
    return " ".join(keep) if keep else t


def run(blackboard, messages):
    out, n = [], 0
    for m in messages or []:
        if m.get("role") != "user":
            continue
        n += 1
        t = _text(m.get("content")).strip()
        if not KEEP.search(t):
            continue
        t = _drop_covered(t).strip()
        if not t:
            continue
        t = FLUFF.sub("", FLUFF.sub("", t)).strip()
        t = IDREF.sub(r"(id \1)", _trim(t))
        t = re.sub(r"\s{2,}", " ", ASK.sub("", t)).strip()
        t = t[:1].upper() + t[1:]
        out.append(t)
    return {"text": "\n".join(out)}
