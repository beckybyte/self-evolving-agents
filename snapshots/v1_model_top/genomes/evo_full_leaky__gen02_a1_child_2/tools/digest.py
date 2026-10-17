"""Compact the chat for the extractor: keep every user turn verbatim (numbered), drop the
assistant's calendars (its own placements are NOT user constraints and confuse extraction).
"""
import re

KEEP_ASSISTANT_CHARS = 200


def _text(c):
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def strip_calendar(t):
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\[\s*\{.*?\}\s*\]", " ", t, flags=re.S)
    t = re.sub(r"\{[^{}]*\}", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:KEEP_ASSISTANT_CHARS]


def run(blackboard, messages):
    out, n = [], 0
    for m in messages or []:
        role, c = m.get("role"), _text(m.get("content")).strip()
        if role == "system":
            if c:
                out.append("SYSTEM: " + c)
        elif role == "user":
            n += 1
            out.append("USER TURN %d: %s" % (n, c))
        elif role == "assistant":
            s = strip_calendar(c)
            out.append("ASSISTANT (its own placement, NOT a user rule): " + (s or "[calendar]"))
    return {"text": "\n\n".join(out)}
