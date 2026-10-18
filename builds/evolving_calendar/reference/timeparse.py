"""Rule-based time parsing for the Evolving Calendar reference app (stdlib only).

parse(text, now) finds day + time phrases in a fragment and returns what it understood plus the
text with those phrases removed (used as the item title). Clear clock times ("3pm", "15:30") are
`clear`; soft ones ("this afternoon", "around noon") keep a `window` and an approximate start.
"""
import datetime as dt
import re

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
WD_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)"

# (pattern, window label, hour, minute)
PARTS = [
    (r"\b(?:around|about|at about|roughly)\s+(?:noon|lunch(?:time)?|midday)\b|\bnoon-?ish\b|\b(?:at|over)\s+lunch(?:time)?\b", "around noon", 12, 0),
    (r"\b(?:early\s+)?(?:this\s+)?morning\b", "morning", 9, 0),
    (r"\b(?:this\s+)?afternoon\b", "afternoon", 14, 0),
    (r"\bafter work\b", "after work", 18, 0),
    (r"\b(?:this\s+)?evening\b", "evening", 20, 0),
    (r"\btonight\b", "tonight", 20, 0),
    (r"\b(?:at\s+)?night\b", "tonight", 21, 0),
]

ISO_FMT = "%Y-%m-%dT%H:%M"


def iso(d):
    return d.strftime(ISO_FMT) if d else None


def from_iso(s):
    if not s:
        return None
    s = str(s)[:16]
    try:
        return dt.datetime.strptime(s, ISO_FMT)
    except ValueError:
        try:
            return dt.datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _weekday_date(now, wd, force_next=False):
    d = (wd - now.weekday()) % 7
    if d == 0:
        d = 7
    return (now + dt.timedelta(days=d)).date()


def _cut(text, m):
    return (text[:m.start()] + " " + text[m.end():]).strip()


def _find_day(text, now):
    """Returns (date|None, label|None, remaining_text)."""
    t = text
    m = re.search(r"\bday after tomorrow\b", t, re.I)
    if m:
        return (now + dt.timedelta(days=2)).date(), "day after tomorrow", _cut(t, m)
    m = re.search(r"\b(?:tomorrow|tmrw|tmr|tomorow)\b", t, re.I)
    if m:
        return (now + dt.timedelta(days=1)).date(), "tomorrow", _cut(t, m)
    m = re.search(r"\btoday\b", t, re.I)
    if m:
        return now.date(), "today", _cut(t, m)
    m = re.search(r"\bin\s+(\d+|a|one|two|three|four|five|six|seven)\s+(day|days|week|weeks)\b", t, re.I)
    if m:
        words = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
        n = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1).lower()]
        days = n * (7 if m.group(2).lower().startswith("week") else 1)
        return (now + dt.timedelta(days=days)).date(), m.group(0), _cut(t, m)
    m = re.search(r"\b(?:on\s+)?(?:(this|next|coming)\s+)?" + WD_RE + r"\b", t, re.I)
    if m:
        wd = WEEKDAYS[m.group(2).lower()]
        d = _weekday_date(now, wd)
        return d, DAY_NAMES[wd], _cut(t, m)
    m = re.search(r"\b(?:on\s+)?" + MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t, re.I)
    if m:
        mon = MONTHS[m.group(1).lower()[:3]]
        try:
            d = dt.date(now.year, mon, int(m.group(2)))
            if d < now.date():
                d = dt.date(now.year + 1, mon, int(m.group(2)))
            return d, d.strftime("%b %d"), _cut(t, m)
        except ValueError:
            pass
    m = re.search(r"\b(?:on\s+)?(\d{1,2})/(\d{1,2})\b", t)
    if m:
        try:
            d = dt.date(now.year, int(m.group(1)), int(m.group(2)))
            if d < now.date():
                d = dt.date(now.year + 1, int(m.group(1)), int(m.group(2)))
            return d, d.strftime("%b %d"), _cut(t, m)
        except ValueError:
            pass
    m = re.search(r"\b(?:this\s+|next\s+)?weekend\b", t, re.I)
    if m:
        d = _weekday_date(now, 5) if now.weekday() != 5 else now.date()
        if "next" in m.group(0).lower():
            d = d + dt.timedelta(days=7)
        return d, "this weekend" if "next" not in m.group(0).lower() else "next weekend", _cut(t, m)
    m = re.search(r"\bnext week\b", t, re.I)
    if m:
        d = now.date() + dt.timedelta(days=7 - now.weekday())
        return d, "next week", _cut(t, m)
    return None, None, t


def _find_time(text):
    """Returns ((hour, minute)|None, clear: bool, window|None, remaining_text)."""
    t = text
    approx = r"(?:(around|about|roughly|approx\.?|ish)\s+)?"
    m = re.search(r"\b(?:at\s+)?" + approx + r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(\s*-?ish)?\b", t, re.I)
    if m:
        h = int(m.group(2)) % 12
        mi = int(m.group(3) or 0)
        if m.group(4).lower().startswith("p"):
            h += 12
        if h < 24 and mi < 60:
            vague = bool(m.group(1) or m.group(5))
            label = None
            if vague:
                label = "around " + _clock_label(h, mi)
            return (h, mi), not vague, label, _cut(t, m)
    m = re.search(r"\b(?:at\s+)?" + approx + r"([01]?\d|2[0-3]):([0-5]\d)\b", t, re.I)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        vague = bool(m.group(1))
        return (h, mi), not vague, ("around " + _clock_label(h, mi)) if vague else None, _cut(t, m)
    m = re.search(r"\bat\s+" + approx + r"(\d{1,2})\b(?!\s*(?:min|minutes|hours|h\b))", t, re.I)
    if m:
        h = int(m.group(2))
        if 1 <= h <= 7:
            h += 12
        if h < 24:
            vague = bool(m.group(1))
            return (h, 0), not vague, ("around " + _clock_label(h, 0)) if vague else None, _cut(t, m)
    for pat, label, h, mi in PARTS:
        m = re.search(pat, t, re.I)
        if m:
            return (h, mi), False, label, _cut(t, m)
    m = re.search(r"\b(?:at\s+)?(noon|midday)\b", t, re.I)
    if m:
        return (12, 0), True, None, _cut(t, m)
    return None, False, None, t


def _clock_label(h, mi):
    ap = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh}:{mi:02d} {ap}" if mi else f"{hh} {ap}"


def parse(text, now):
    """Parse day/time phrases out of a fragment.

    Returns dict: start (datetime|None), window (str|None), clear (bool), rest (str), day (date|None),
    found (bool: any time phrase recognised).
    """
    day, day_label, rest = _find_day(text, now)
    hm, clear, part_label, rest = _find_time(rest)
    start = None
    window = None
    if hm:
        h, mi = hm
        if day is None:
            cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand <= now:
                cand += dt.timedelta(days=1)
                if part_label and not part_label.startswith("around "):
                    part_label = "tomorrow " + part_label
            start = cand
        else:
            start = dt.datetime.combine(day, dt.time(h, mi))
        if not clear:
            window = part_label
            if day_label and day_label not in ("today",):
                window = f"{day_label} {part_label}" if part_label else day_label
            elif day_label == "today" and part_label:
                window = f"today {part_label}" if not part_label.startswith("around") else part_label
    elif day is not None:
        window = day_label
        start = None
    rest = re.sub(r"\s+", " ", rest).strip(" ,.;:-")
    rest = re.sub(r"\b(on|at|by|for)$", "", rest, flags=re.I).strip(" ,.;:-")
    return {"start": start, "window": window, "clear": bool(hm and clear), "rest": rest, "day": day,
            "found": bool(hm or day)}


TIMEISH = re.compile(r"\b(\d|noon|morning|afternoon|evening|tonight|night|tomorrow|today|week|weekend|month|"
                     r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|"
                     r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|end of|soon|days?)\b", re.I)


def looks_timeish(text):
    return bool(TIMEISH.search(text))


def parse_deadline(text, now):
    """Deadline from a free-text reply. Returns (datetime, understood: bool)."""
    t = text.strip().lower()
    if re.search(r"\b(no idea|not sure|whenever|no rush|don'?t know|dunno|no deadline|any ?time|idk)\b", t):
        return _eod(now.date() + dt.timedelta(days=7)), True
    m = re.search(r"\bend of (?:the |this )?month\b", t)
    if m:
        first_next = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return _eod((first_next - dt.timedelta(days=1)).date()), True
    m = re.search(r"\bend of (?:the |this )?week\b", t)
    if m:
        return _eod(now.date() + dt.timedelta(days=(4 - now.weekday()) % 7 or 7)), True
    m = re.search(r"\bend of next week\b|\bnext week\b", t)
    if m:
        return _eod(now.date() + dt.timedelta(days=(7 - now.weekday()) + 4)), True
    p = parse(text, now)
    if p["start"] and p["clear"]:
        return p["start"], True
    if p["day"]:
        return _eod(p["day"]), True
    if p["start"]:
        return p["start"], True
    return _eod(now.date() + dt.timedelta(days=7)), False


def _eod(d):
    return dt.datetime.combine(d, dt.time(23, 59))


def parse_duration(text):
    """Minutes mentioned in a fragment ("2 hours", "30 min", "1.5h"), or None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", text, re.I)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"(\d+)\s*(minutes?|mins?|m)\b", text, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"\b(all day|whole day)\b", text, re.I):
        return 480
    return None


def strip_duration(text):
    text = re.sub(r"\bfor\s+(?:about\s+)?\d+(?:\.\d+)?\s*(hours?|hrs?|h|minutes?|mins?)\b", "", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,.;:-")


def fmt_clock(d):
    return d.strftime("%I:%M %p").lstrip("0")


def fmt_when(d, now):
    """Kind, unambiguous: 'tomorrow (Tue Oct 20) at 8:00 PM (20:00)'."""
    if d is None:
        return "sometime soon"
    delta = (d.date() - now.date()).days
    clock = f"{fmt_clock(d)} ({d.strftime('%H:%M')})"
    date_s = d.strftime("%a %b ") + str(d.day)
    if delta == 0:
        return f"today ({date_s}) at {clock}"
    if delta == 1:
        return f"tomorrow ({date_s}) at {clock}"
    return f"{d.strftime('%A')} ({date_s}) at {clock}"


def fmt_day(d, now):
    delta = (d.date() - now.date()).days
    date_s = d.strftime("%a %b ") + str(d.day)
    if delta == 0:
        return f"today ({date_s})"
    if delta == 1:
        return f"tomorrow ({date_s})"
    return f"{d.strftime('%A')} ({date_s})"
