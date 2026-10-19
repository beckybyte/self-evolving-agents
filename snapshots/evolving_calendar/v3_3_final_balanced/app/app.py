import argparse
import json
import os
import re
import threading
import uuid
from datetime import datetime, date, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import v2
import v3
import commands
import convo

STATE = {
    "now": "2026-10-19T19:50",
    "items": [],
    "messages": [],
    "convo": {},
    "undo": {},
    "sent": {},
    "streak_yes": 0,
    "streak_no": 0,
}

LOCK = threading.Lock()
DATA_DIR = "."

BANNED = [
    "overdue", "fail", "miss", "late", "behind", "should have", "forgot",
    "lazy", "urgent", "alert",
]
BANNED_REPLACEMENTS = {
    "overdue": "waiting",
    "fail": "not work out",
    "miss": "skip",
    "late": "later on",
    "behind": "a bit back",
    "should have": "could",
    "forgot": "set aside",
    "lazy": "tired",
    "urgent": "pressing",
    "alert": "note",
}


def clean_text(text):
    if not isinstance(text, str):
        return text
    out = text
    low = out.lower()
    for bad in BANNED:
        if bad in low:
            out = re.sub(re.escape(bad), BANNED_REPLACEMENTS.get(bad, "gently"), out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except Exception:
        return None


def fmt_iso(dt):
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


def now_dt():
    return parse_iso(STATE.get("now")) or datetime(2026, 10, 19, 19, 50)


def new_id(prefix="i"):
    return prefix + "_" + uuid.uuid4().hex[:10]


def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    msg = {
        "id": "a_" + uuid.uuid4().hex[:10],
        "role": "agent",
        "kind": kind,
        "text": clean_text(text),
        "item_id": item_id,
        "at": STATE.get("now"),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        msg["steps"] = steps if steps is not None else []
    STATE["messages"].append(msg)
    return msg


def default_item(raw):
    item = {
        "id": raw.get("id") or new_id(),
        "kind": raw.get("kind") or "task",
        "title": raw.get("title") or "Untitled",
        "start": raw.get("start"),
        "end": raw.get("end"),
        "window": raw.get("window"),
        "status": raw.get("status") or "open",
        "why": raw.get("why"),
        "parent_id": raw.get("parent_id"),
        "estimate_min": raw.get("estimate_min"),
        "postpone_count": raw.get("postpone_count", 0) or 0,
        "source": raw.get("source") or "seed",
    }
    for k, v in raw.items():
        if k not in item:
            item[k] = v
    return item


def find_item(item_id):
    for it in STATE["items"]:
        if it.get("id") == item_id:
            return it
    return None


# ---------------- PARSING ----------------

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

WINDOWS = [
    ("around noon", 12, 0, 13, 0),
    ("noon", 12, 0, 13, 0),
    ("lunch", 12, 0, 13, 0),
    ("this afternoon", 14, 0, 17, 0),
    ("afternoon", 14, 0, 17, 0),
    ("morning", 9, 0, 12, 0),
    ("evening", 18, 0, 21, 0),
    ("tonight", 20, 0, 22, 0),
]

VAGUE_VERBS = [
    "work on", "figure out", "deal with", "sort out", "sort through", "handle",
    "tackle", "get on top of", "catch up on", "clean out", "clean up",
    "get together", "get done", "get sorted", "organize", "organise", "plan",
    "prepare", "research", "finish", "start on", "apply for", "overhaul",
    "redo", "revamp", "set up", "look into", "think about", "go through",
    "review", "arrange", "coordinate", "brainstorm", "outline", "draft",
    "build", "create", "design", "develop", "improve", "fix up", "tidy",
    "declutter", "sort", "process", "manage", "schedule", "write up",
    "put together", "wrap up", "follow up on", "chase", "investigate",
    "explore", "consider", "decide on", "map out", "lay out", "assemble",
]

BIG_NOUNS = [
    "project", "portfolio", "pitch deck", "deck", "thesis", "website",
    "garage", "closet", "inbox", "taxes", "application", "claim", "move",
    "renovation", "novel", "book", "business", "budget", "wedding",
    "conference", "presentation", "report", "essay", "paper", "garden",
    "basement", "attic", "kitchen", "apartment", "house", "career",
    "resume", "cv", "job search", "startup", "app", "product", "launch",
    "campaign", "course", "degree", "exam", "study", "research",
]


def is_big(text, item=None):
    if item is not None:
        est = item.get("estimate_min")
        if isinstance(est, int) and est > 120:
            return True
    if not text:
        return False
    low = text.lower()
    for v in VAGUE_VERBS:
        if re.search(r"\b" + re.escape(v) + r"\b", low):
            return True
    for n in BIG_NOUNS:
        if re.search(r"\b" + re.escape(n) + r"\b", low):
            return True
    return False


def _find_date(text):
    """Return (date_d, matched_span) or (None, None)."""
    low = text.lower()
    base_d = now_dt().date()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, m.span()
        except Exception:
            pass

    m = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\.?\s+(\d{1,2})\b", low)
    if m:
        mon = MONTHS[m.group(1)]
        day = int(m.group(2))
        year = base_d.year
        try:
            d = date(year, mon, day)
            if d < base_d - timedelta(days=180):
                d = date(year + 1, mon, day)
            return d, m.span()
        except Exception:
            pass

    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS.keys()) + r")\b", low)
    if m:
        mon = MONTHS[m.group(2)]
        day = int(m.group(1))
        year = base_d.year
        try:
            d = date(year, mon, day)
            if d < base_d - timedelta(days=180):
                d = date(year + 1, mon, day)
            return d, m.span()
        except Exception:
            pass

    if re.search(r"\btoday\b", low):
        m = re.search(r"\btoday\b", low)
        return base_d, m.span()
    if re.search(r"\btomorrow\b", low):
        m = re.search(r"\btomorrow\b", low)
        return base_d + timedelta(days=1), m.span()

    for name, wd in WEEKDAYS.items():
        m = re.search(r"\b" + name + r"\b", low)
        if m:
            delta = (wd - base_d.weekday()) % 7
            if delta == 0:
                delta = 7
            return base_d + timedelta(days=delta), m.span()
    return None, None


def _find_time(text):
    """Return (hour, minute, span) or (None, None, None)."""
    low = text.lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi, m.span()
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h, 0, m.span()
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            return h, 0, m.span()
    return None, None, None


def _find_window(text):
    low = text.lower()
    for phrase, h1, m1, h2, m2 in WINDOWS:
        idx = low.find(phrase)
        if idx >= 0:
            return phrase, h1, m1, h2, m2, (idx, idx + len(phrase))
    return None, None, None, None, None, None


def _strip_spans(text, spans):
    out = text
    for s, e in sorted(spans, key=lambda x: -x[0]):
        out = out[:s] + " " + out[e:]
    return out


def _title_from(text):
    t = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    t = re.sub(r"^(maybe|perhaps|also)\s+", "", t, flags=re.IGNORECASE)
    t = t.strip(" ?")
    if not t:
        t = "Something"
    return t[0].upper() + t[1:]


def parse_item(text, source="typed"):
    raw = text.strip()
    if not raw:
        return None
    low = raw.lower()
    spans = []

    date_d, dspan = _find_date(raw)
    if dspan:
        spans.append(dspan)

    hour, minute, tspan = _find_time(raw)
    win_phrase, wh1, wm1, wh2, wm2, wspan = _find_window(raw)

    if tspan:
        spans.append(tspan)
    if wspan:
        spans.append(wspan)

    title = _title_from(_strip_spans(raw, spans))

    base_d = now_dt().date()
    if date_d is None:
        if hour is not None:
            cand = datetime.combine(base_d, dtime(hour, minute))
            if cand <= now_dt():
                date_d = base_d + timedelta(days=1)
            else:
                date_d = base_d
        elif win_phrase is not None:
            cand = datetime.combine(base_d, dtime(wh1, wm1))
            if cand <= now_dt():
                date_d = base_d + timedelta(days=1)
            else:
                date_d = base_d
        else:
            date_d = base_d

    item = {
        "id": new_id(),
        "kind": "task",
        "title": title,
        "start": None,
        "end": None,
        "window": None,
        "status": "open",
        "why": None,
        "parent_id": None,
        "estimate_min": None,
        "postpone_count": 0,
        "source": source,
    }

    if hour is not None:
        start_dt = datetime.combine(date_d, dtime(hour, minute))
        item["kind"] = "event"
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(start_dt + timedelta(minutes=60))
        item["window"] = None
        return item

    if win_phrase is not None:
        start_dt = datetime.combine(date_d, dtime(wh1, wm1))
        end_dt = datetime.combine(date_d, dtime(wh2, wm2))
        item["window"] = win_phrase
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(end_dt)
        appt = re.search(r"\b(with|meet|lunch|dinner|call with|coffee)\b", low)
        if appt:
            item["kind"] = "event"
        else:
            item["kind"] = "task"
        return item

    if low.startswith("maybe") or raw.rstrip().endswith("?"):
        item["kind"] = "note"
    else:
        item["kind"] = "task"
    return item


def split_dump(text):
    parts = re.split(r"[,;\n]+", text)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


# ---------------- RESET / STATE ----------------

def do_reset(body):
    now = body.get("now") or "2026-10-19T19:50"
    seed = body.get("items") or []
    STATE["now"] = now
    STATE["items"] = [default_item(dict(x)) for x in seed]
    STATE["messages"] = []
    STATE["convo"] = {}
    STATE["undo"] = {}
    STATE["sent"] = {}
    STATE["streak_yes"] = 0
    STATE["streak_no"] = 0
    try:
        v2.init_v2(STATE)
    except Exception:
        pass
    try:
        convo.reset(STATE)
    except Exception:
        pass
    add_agent("info", "Hi, I'm here whenever you want to plan a little.")
    return state_payload()


def state_payload():
    payload = {
        "now": STATE.get("now"),
        "items": STATE.get("items", []),
        "messages": STATE.get("messages", []),
    }
    try:
        payload["view"] = v2.view_state(STATE)
    except Exception:
        payload["view"] = {"type": "week", "params": {}, "hidden_counts": {}}
    try:
        payload["meta"] = v2.meta_state(STATE)
    except Exception:
        payload["meta"] = {}
    return payload


# ---------------- COMMAND ----------------

def do_command(body):
    text = body.get("text") or ""
    mode = body.get("mode") or "command"
    new_messages = []
    created = []

    if mode == "command":
        try:
            res = commands.handle_command(STATE, text)
        except Exception:
            res = None
        if res is not None:
            return res

    if mode == "dump":
        frags = split_dump(text)
        items = []
        for f in frags:
            it = parse_item(f, source="dump")
            if it is not None:
                items.append(it)
        for it in items:
            STATE["items"].append(it)
            created.append(it["id"])
            try:
                v2.add_log(STATE, "create", [it["id"]], {"title": it["title"]}, "dump")
            except Exception:
                pass
        titles = [it["title"] for it in items]
        if titles:
            add_agent("info", "Added: " + ", ".join(titles) + ".")
        else:
            add_agent("info", "I didn't catch anything to add.")
        big = None
        for it in items:
            if is_big(it["title"], it):
                big = it
                break
        if big is not None:
            try:
                extra = convo.on_big_task(STATE, big)
                if extra:
                    new_messages.extend(extra)
            except Exception:
                pass
        try:
            v2.tick(STATE, False)
        except Exception:
            pass
        return {"messages": new_messages, "created": created}

    # single command
    it = parse_item(text, source="typed")
    if it is None:
        add_agent("info", "I didn't catch anything to add.")
        return {"messages": new_messages, "created": created}
    STATE["items"].append(it)
    created.append(it["id"])
    try:
        v2.add_log(STATE, "create", [it["id"]], {"title": it["title"]}, "typed")
    except Exception:
        pass
    add_agent("info", "Added '" + it["title"] + "'.")
    if is_big(it["title"], it):
        try:
            extra = convo.on_big_task(STATE, it)
            if extra:
                new_messages.extend(extra)
        except Exception:
            pass
    try:
        v2.tick(STATE, False)
    except Exception:
        pass
    return {"messages": new_messages, "created": created}


# ---------------- DONE / UNDO ----------------

def do_done(item_id):
    it = find_item(item_id)
    if it is None:
        raise KeyError(item_id)
    it["status"] = "done"
    try:
        v2.add_log(STATE, "done", [item_id], {"title": it.get("title")}, "person")
    except Exception:
        pass
    try:
        v2.tick(STATE, False)
    except Exception:
        pass
    return {"item": it}


def do_undo(body):
    undo_id = body.get("undo_id")
    rec = STATE.get("undo", {}).get(undo_id)
    if not rec:
        return {"ok": True}
    if rec.get("used"):
        return {"ok": True}
    item_id = rec.get("item_id")
    before = rec.get("before")
    if before is not None:
        idx = None
        for i, it in enumerate(STATE["items"]):
            if it.get("id") == item_id:
                idx = i
                break
        if idx is not None:
            STATE["items"][idx] = before
        else:
            STATE["items"].append(before)
    else:
        it = find_item(item_id)
        if it is not None:
            for k in ("start", "end", "window", "postpone_count"):
                if k in rec:
                    it[k] = rec[k]
    rec["used"] = True
    try:
        v2.add_log(STATE, "update", [item_id] if item_id else [], {"undo": undo_id}, "person")
    except Exception:
        pass
    try:
        v2.tick(STATE, False)
    except Exception:
        pass
    return {"ok": True}


# ---------------- FOCUS ----------------

def do_focus():
    items = STATE.get("items", [])
    open_items = [it for it in items if it.get("status") == "open"]
    goals = [it for it in open_items if it.get("why")]
    goal_ids = {g["id"] for g in goals}
    children = [it for it in open_items if it.get("parent_id") in goal_ids]

    def sort_key(it):
        s = it.get("start")
        return (0, s) if s else (1, "")

    chosen = None
    parent = None
    why = None
    if children:
        children_sorted = sorted(children, key=sort_key)
        chosen = children_sorted[0]
        parent = find_item(chosen.get("parent_id"))
        if parent is not None:
            why = parent.get("why")
    else:
        has_children = {it.get("parent_id") for it in items if it.get("parent_id")}
        tasks = [it for it in open_items if it.get("id") not in has_children]
        if tasks:
            tasks_sorted = sorted(tasks, key=sort_key)
            chosen = tasks_sorted[0]
            pid = chosen.get("parent_id")
            if pid:
                parent = find_item(pid)
            if parent is not None and parent.get("why"):
                why = parent.get("why")
            else:
                why = chosen.get("why")
    return {
        "item": chosen,
        "parent": parent,
        "why": why,
        "action": "Start 5 minutes",
    }


# ---------------- HTTP ----------------

FALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Evolving Calendar</title></head>
<body><h1>Evolving Calendar</h1>
<div id="focus">Right now, just this</div>
<div id="timeline">Your calendar</div>
<div id="chat-log"></div>
<input id="chat-input" placeholder="Tell me one thing...">
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        try:
            data = json.dumps(obj).encode("utf-8")
        except Exception:
            data = json.dumps({"error": "serialization"}).encode("utf-8")
            status = 500
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _send_html(self, text, status=200):
        data = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        try:
            with LOCK:
                self._handle_get()
        except KeyError as e:
            self._send_json({"error": "not found: " + str(e)}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            with LOCK:
                self._handle_post()
        except KeyError as e:
            self._send_json({"error": "not found: " + str(e)}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        try:
            with LOCK:
                self._handle_delete()
        except KeyError as e:
            self._send_json({"error": "not found: " + str(e)}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/" or path == "/index.html":
            here = os.path.dirname(os.path.abspath(__file__))
            fp = os.path.join(here, "index.html")
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                    return
                except Exception:
                    pass
            self._send_html(FALLBACK_PAGE)
            return

        if path == "/health":
            self._send_json({"ok": True})
            return

        if path == "/api/state":
            self._send_json(state_payload())
            return

        if path == "/api/focus":
            self._send_json(do_focus())
            return

        if path == "/api/log":
            self._send_json(v2.get_log(STATE))
            return

        if path == "/api/beliefs":
            self._send_json(v2.get_beliefs(STATE))
            return

        if path == "/api/proposal":
            self._send_json(v2.get_proposal(STATE))
            return

        if path == "/api/prompts":
            self._send_json(v2.get_prompts(STATE))
            return

        if path == "/api/activity":
            self._send_json(v2.get_activity(STATE))
            return

        if path == "/api/catalog":
            self._send_json(v3.get_catalog(STATE))
            return

        self._send_json({"error": "not found"}, 404)

    def _handle_post(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_body()

        if path == "/api/reset":
            self._send_json(do_reset(body))
            return

        if path == "/api/command":
            self._send_json(do_command(body))
            return

        if path == "/api/chat":
            item_id = body.get("item_id")
            text = body.get("text", "")
            res = convo.chat(STATE, item_id, text)
            self._send_json(res)
            return

        if path == "/api/clock":
            new_iso = body.get("now")
            res = convo.clock(STATE, new_iso)
            self._send_json(res)
            return

        if path == "/api/undo":
            self._send_json(do_undo(body))
            return

        if path == "/api/import":
            res = commands.import_file(STATE, body.get("filename", ""), body.get("content", ""))
            self._send_json(res)
            return

        if path == "/api/learn":
            self._send_json(v2.learn(STATE))
            return

        if path == "/api/view":
            self._send_json(v2.set_view(STATE, body.get("type")))
            return

        if path == "/api/critic":
            self._send_json(v3.critic(STATE, body.get("spec")))
            return

        if path == "/api/design":
            r = v3.design(STATE, body.get("belief_key"))
            try:
                r["proposal"] = v2.learn(STATE).get("proposal")
            except Exception:
                r["proposal"] = None
            self._send_json(r)
            return

        m = re.match(r"^/api/items/([^/]+)/done$", path)
        if m:
            item_id = unquote(m.group(1))
            self._send_json(do_done(item_id))
            return

        m = re.match(r"^/api/plan/([^/]+)/apply$", path)
        if m:
            plan_id = unquote(m.group(1))
            self._send_json(commands.apply_plan(STATE, plan_id))
            return

        m = re.match(r"^/api/plan/([^/]+)/undo$", path)
        if m:
            plan_id = unquote(m.group(1))
            self._send_json(commands.undo_plan(STATE, plan_id))
            return

        m = re.match(r"^/api/proposals/([^/]+)/([^/]+)$", path)
        if m:
            proposal_id = unquote(m.group(1))
            action = unquote(m.group(2))
            self._send_json(v2.proposal_action(STATE, proposal_id, action))
            return

        m = re.match(r"^/api/prompts/([^/]+)/rollback$", path)
        if m:
            agent = unquote(m.group(1))
            self._send_json(v2.rollback(STATE, agent))
            return

        self._send_json({"error": "not found"}, 404)

    def _handle_delete(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        m = re.match(r"^/api/beliefs/([^/]+)$", path)
        if m:
            key = unquote(m.group(1))
            self._send_json(v2.delete_belief(STATE, key))
            return
        self._send_json({"error": "not found"}, 404)


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="./data")
    args = parser.parse_args()
    DATA_DIR = args.data
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
