"""Evolving Calendar — hand-written reference app for the Iris contract (Python 3.12 stdlib only).

python app.py --port 8000 --data ./data      (EVOLVE_MOCK=1 -> rules only, no network)
"""
import argparse
import copy
import datetime as dt
import json
import os
import re
import statistics
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain
import llm
from crud import CrudMixin
from designer import CATALOG, DesignerMixin
from learning import LearningMixin
import timeparse as tp
from timeparse import from_iso, iso

HERE = os.path.dirname(os.path.abspath(__file__))
ITEM_DEFAULTS = {"id": None, "kind": "task", "title": "", "start": None, "end": None, "window": None,
                 "status": "open", "why": None, "parent_id": None, "estimate_min": None, "postpone_count": 0,
                 "source": "seed"}
DEFAULT_PREF_MIN = 20 * 60          # evening, when there is no done history
MIN15 = dt.timedelta(minutes=15)


def q(title):
    return f"“{title}”"


def short_when(d):
    return f"{d.strftime('%a %b')} {d.day}, {tp.fmt_clock(d)}"


def part_of_day(d):
    h = d.hour
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"


class App(CrudMixin, DesignerMixin, LearningMixin):
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.RLock()
        self.wipe("2026-10-19T09:00")
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.__dict__.update(json.load(f))
                self.sent = set(self.sent)
                self.meta_state = {**self.meta_state}
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------ storage
    def wipe(self, now):
        self.now = now
        self.items = {}       # id -> Item (contract fields only), insertion ordered
        self.meta = {}        # id -> {streak, skip, checkin, done_at, deadline}
        self.messages = []
        self.undo = {}
        self.convo = {}       # item_id -> conversation state
        self.sent = set()     # reminder keys
        self.pending_q = []   # big items still waiting for their complexity question
        self.wipe_learning()
        self.wipe_crud()

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        state = {k: getattr(self, k) for k in ("now", "items", "meta", "messages", "undo", "convo", "pending_q", "plans")
                 + self.LEARN_FIELDS}
        state["sent"] = sorted(self.sent)
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)

    def state(self):
        return {"now": self.now, "items": list(self.items.values()), "messages": self.messages,
                "view": self.view_spec(), "meta": dict(self.meta_state)}

    # ------------------------------------------------------------ helpers
    @property
    def nowdt(self):
        return from_iso(self.now)

    def new_item(self, insert=True, **kw):
        it = dict(ITEM_DEFAULTS)
        it.update({k: v for k, v in kw.items() if k in ITEM_DEFAULTS})
        if not it["id"]:
            it["id"] = "it_" + uuid.uuid4().hex[:8]
        it["id"] = str(it["id"])
        if not insert:
            return it
        self.items[it["id"]] = it
        self.meta.setdefault(it["id"], {"streak": 0})
        return it

    def m(self, kind, text, item_id=None, undo_id=None, steps=None, role="agent", at=None):
        msg = {"id": "m_" + uuid.uuid4().hex[:8], "role": role, "kind": kind,
               "text": brain.kind(text) if role == "agent" else text,
               "item_id": item_id, "at": at or self.now, "undo_id": undo_id}
        if kind == "breakdown":
            msg["steps"] = [{"title": brain.kind(s["title"]), "estimate_min": s["estimate_min"]} for s in steps]
        self.messages.append(msg)
        return msg

    def eff_end(self, it):
        s = from_iso(it["start"])
        if not s:
            return None
        e = from_iso(it["end"])
        if e and e > s:
            return e
        return s + dt.timedelta(minutes=it.get("estimate_min") or (60 if it["kind"] == "event" else 30))

    def children(self, item_id, open_only=False):
        return [c for c in self.items.values() if c["parent_id"] == item_id
                and (not open_only or c["status"] == "open")]

    def busy(self, exclude=()):
        out = []
        for it in self.items.values():
            if it["id"] in exclude or it["status"] != "open" or it["kind"] == "note" or not it["start"]:
                continue
            out.append((from_iso(it["start"]), self.eff_end(it)))
        return out

    def is_free(self, s, e, exclude=()):
        return all(e <= bs or s >= be for bs, be in self.busy(exclude))

    def pref_minutes(self):
        """When this person usually gets things done (minutes after midnight), learned from done history."""
        mins = []
        for it in self.items.values():
            if it["status"] != "done":
                continue
            d = from_iso(self.meta.get(it["id"], {}).get("done_at")) or from_iso(it["start"]) or from_iso(it["end"])
            if d:
                mins.append(d.hour * 60 + d.minute)
        if not mins:
            return DEFAULT_PREF_MIN
        p = int(statistics.median(mins))
        return p - p % 15

    def find_slot(self, day, dur, pref, earliest=None, latest=None, exclude=(), max_dev=None):
        cands = []
        for c in range(7 * 60, 24 * 60 - dur + 1, 15):
            if max_dev is not None and abs(c - pref) > max_dev:
                continue
            cands.append(c)
        cands.sort(key=lambda c: (abs(c - pref), c < pref))
        base = dt.datetime.combine(day, dt.time(0, 0))
        for c in cands:
            s = base + dt.timedelta(minutes=c)
            e = s + dt.timedelta(minutes=dur)
            if earliest and s < earliest:
                continue
            if latest and e > latest:
                continue
            if self.is_free(s, e, exclude):
                return s
        return None

    def snapshot(self, ids):
        return {i: ([copy.deepcopy(self.items[i]), copy.deepcopy(self.meta.get(i, {}))] if i in self.items else None)
                for i in ids}

    def make_undo(self, before, created=()):
        uid = "u_" + uuid.uuid4().hex[:8]
        self.undo[uid] = {"before": before, "created": list(created), "used": False}
        return uid

    def round_up(self, d):
        r = d.replace(second=0, microsecond=0)
        extra = (-r.minute) % 15
        return r + dt.timedelta(minutes=extra)

    # ------------------------------------------------------------ reset / command
    def reset(self, now, items):
        self.wipe(now or self.now)
        for raw in items or []:
            it = self.new_item(**raw)
            if it["status"] == "done":
                self.meta[it["id"]]["done_at"] = raw.get("done_at") or it["start"] or it["end"]
            if raw.get("recurrence"):
                it["recurrence"] = raw["recurrence"]
        self.update_beliefs()
        return self.state()

    def make_item(self, frag, source, insert=True):
        clean, tentative = brain.clean_fragment(frag)
        est = tp.parse_duration(clean)
        if est:
            clean = tp.strip_duration(clean) or clean
        p = tp.parse(clean, self.nowdt)
        title = p["rest"] or clean
        start, window, clear = p["start"], p["window"], p["clear"]
        if not p["found"] and tp.looks_timeish(clean):
            s, w, exact = brain.llm_time(clean, self.now)
            if s and from_iso(s):
                start, window, clear = from_iso(s), (None if exact else (w or "soon")), exact
        if clear:
            kind = "event"
        elif tentative and not p["found"]:
            kind, title = "note", f"{title} (maybe)"
        else:
            kind = "task"
        end = None
        if start:
            end = start + dt.timedelta(minutes=est or (60 if kind == "event" else 30))
        it = self.new_item(insert=insert, kind=kind, title=title, start=iso(start), end=iso(end), window=window,
                           estimate_min=est if est else (30 if kind == "task" else None), source=source)
        big = False if kind != "task" else brain.is_big(title, est)
        if big:
            it["estimate_min"] = est if est and est > 120 else None
        return it, big

    def describe(self, it):
        if it["kind"] == "event" and it["start"]:
            return f"{it['title']} ({short_when(from_iso(it['start']))})"
        if it["window"]:
            return f"{it['title']} ({it['window']})"
        return it["title"]

    def command(self, text, mode):
        text = (text or "").strip()
        if text and mode != "dump":
            r = self.intent(text)
            if r is not None:
                return r
        out = [self.m("reply", text, role="person")]
        if not text:
            out.append(self.m("reply", "I'm here whenever you want to jot something down."))
            return {"messages": out, "created": []}
        frags = brain.split_dump(text) if mode == "dump" else [text]
        made = [self.make_item(f, "dump" if mode == "dump" else "typed") for f in frags]
        for it, _ in made:
            self.log_add("create", [it["id"]], {"title": it["title"], "kind": it["kind"], "text": text},
                         "dump" if mode == "dump" else "typed")
        undecided = [it["title"] for it, big in made if big is None]
        judged = brain.llm_complexity(undecided) if undecided else {}
        bigs = [it for it, big in made if big or (big is None and judged.get(it["title"]))]
        if mode == "dump":
            listed = "; ".join(self.describe(it) for it, _ in made)
            n = len(made)
            text_r = (f"Got it, I caught {n} thing{'s' if n != 1 else ''}: {listed}. "
                      "Everything is safely written down, so your head can rest.")
        else:
            it = made[0][0]
            if it["kind"] == "event":
                text_r = f"Added {q(it['title'])} for {tp.fmt_when(from_iso(it['start']), self.nowdt)}."
            elif it["window"]:
                text_r = f"Added {q(it['title'])} for {it['window']}. It's a soft window, no exact time needed."
            elif it["kind"] == "note":
                text_r = f"Noted {q(it['title'])} as a maybe. No pressure."
            else:
                text_r = f"Added {q(it['title'])} to your list."
        out.append(self.m("reply", text_r, item_id=made[0][0]["id"] if len(made) == 1 else None))
        if bigs:
            first = bigs[0]
            out.append(self.ask_complexity(first))
            self.pending_q.extend(b["id"] for b in bigs[1:])
        return {"messages": out, "created": [it["id"] for it, _ in made]}

    def ask_complexity(self, it):
        appetite = self.meta_state.get("breakdown_appetite", 0.5)
        if appetite <= 0.25:
            self.log_add("chat", [it["id"]], {"quiet": True, "reason": "low breakdown appetite"}, "agent")
            return self.m("info", f"{q(it['title'])} is on your list as one piece. If you ever want tiny steps, "
                          "just say 'break it down'.", item_id=it["id"])
        if appetite >= 0.8:
            conv = self.convo[it["id"]] = {"stage": "proposed", "variant": 0, "why": it.get("why"), "done": None,
                                           "deadline": iso(self.nowdt.replace(hour=23, minute=59)
                                                           + dt.timedelta(days=7))}
            conv["steps"] = self.gen_steps(it, conv)
            return self.breakdown_msg(it, conv, intro="You usually like tiny steps, so here they are.")
        self.convo[it["id"]] = {"stage": "confirm_break"}
        return self.m("question", f"{q(it['title'])} sounds like a big one. "
                      "Would it help to break it into a few tiny steps?", item_id=it["id"])

    def appetite_answer(self, yes):
        old = self.meta_state.get("breakdown_appetite", 0.5)
        if yes:
            self.accept_streak, self.decline_streak = self.accept_streak + 1, 0
            new = min(1.0, old + 0.1)
            if self.accept_streak >= 3:
                new = max(new, 0.85)
        else:
            self.decline_streak, self.accept_streak = self.decline_streak + 1, 0
            new = max(0.0, old - 0.1)
            if self.decline_streak >= 3:
                new = min(new, 0.2)
        new = round(new, 3)
        self.meta_state["breakdown_appetite"] = new
        if (old < 0.8 <= new) or (old > 0.25 >= new):
            why = ("3 yeses in a row to tiny steps, so next time I'll propose steps straight away" if yes else
                   "3 'no's to breaking things down, so I'll stop asking and just keep big tasks whole")
            self.act("followthrough", "adapt", f"breakdown appetite {old} -> {new}. Reason: {why}.")

    def next_pending_question(self):
        while self.pending_q:
            iid = self.pending_q.pop(0)
            it = self.items.get(iid)
            if it and it["status"] == "open" and iid not in self.convo and not self.children(iid):
                return [self.ask_complexity(it)]
        return []

    # ------------------------------------------------------------ chat
    def chat(self, item_id, text):
        it = self.items.get(item_id)
        if not it:
            return None
        text = (text or "").strip()
        out = [self.m("reply", text, item_id=item_id, role="person")]
        conv = self.convo.get(item_id)
        stage = conv["stage"] if conv else None
        title = it["title"]
        self.log_add("checkin_answer" if stage == "checkin" else "chat", [item_id], {"text": text, "stage": stage},
                     "person")
        if stage == "confirm_break":
            self.asked_breaks += 1
            said_no = brain.is_no(text) and not brain.is_yes(text)
            self.yes_breaks += 0 if said_no else 1
            self.appetite_answer(not said_no)

        if stage is None:
            if re.search(r"\b(break|smaller|steps|split)\b", text, re.I) or (brain.is_yes(text) and brain.is_big(title)):
                conv = self.convo[item_id] = {"stage": "why"}
                out.append(self.m("question", f"Lovely. First, why does {q(title)} matter to you?", item_id))
            else:
                out.append(self.m("reply", f"Noted. I'll keep that with {q(title)}.", item_id))
            return {"messages": out}

        if stage == "confirm_break":
            if brain.is_no(text) and not brain.is_yes(text):
                del self.convo[item_id]
                out.append(self.m("reply", f"Okay, {q(title)} stays as one task. "
                                  "Just say the word if you ever want tiny steps.", item_id))
                out += self.next_pending_question()
            else:
                conv["stage"] = "why"
                out.append(self.m("question", f"Lovely. First, why does {q(title)} matter to you?", item_id))
            return {"messages": out}

        if stage == "why":
            why = re.sub(r"^(because|it'?s|it is|so that|so)\s+", "", text, flags=re.I).strip().rstrip(".") or text
            conv["why"] = why
            it["why"] = why
            conv["stage"] = "done_def"
            out.append(self.m("question", f"That's a good reason: {why}. "
                              f"What would {q(brain.goal_name(title))} look like when it's done?", item_id))
            return {"messages": out}

        if stage == "done_def":
            conv["done"] = text.rstrip(".")
            conv["stage"] = "deadline"
            out.append(self.m("question", "And by when would you like it finished?", item_id))
            return {"messages": out}

        if stage == "deadline":
            dl, ok = tp.parse_deadline(text, self.nowdt)
            if not ok:
                s, _, _ = brain.llm_time("deadline: " + text, self.now)
                if s and from_iso(s) and from_iso(s) > self.nowdt:
                    dl = from_iso(s)
                    if dl.hour == 0 and dl.minute == 0:
                        dl = dl.replace(hour=23, minute=59)
            if dl <= self.nowdt + dt.timedelta(hours=2):
                dl = self.nowdt + dt.timedelta(days=1)
            conv["deadline"] = iso(dl)
            conv["stage"] = "proposed"
            conv["variant"] = 0
            conv["steps"] = self.gen_steps(it, conv)
            out.append(self.breakdown_msg(it, conv))
            return {"messages": out}

        if stage == "proposed":
            if brain.is_accept(text):
                out += self.accept_steps(it, conv)
                del self.convo[item_id]
                out += self.next_pending_question()
            elif brain.is_no(text) and len(text.split()) <= 2:
                out.append(self.m("question", "No problem. What would you like to change?", item_id))
            else:
                conv["variant"] = conv.get("variant", 0) + 1
                new = brain.llm_steps(title, conv.get("why"), conv.get("done"), conv["deadline"],
                                      feedback=text, prev=conv["steps"])
                if not new or new == conv["steps"]:
                    new = brain.revise_rules(conv["steps"], text, title, conv.get("done"), conv["variant"])
                conv["steps"] = new or conv["steps"]
                out.append(self.breakdown_msg(it, conv, revised=True))
            return {"messages": out}

        if stage == "checkin":
            label = brain.classify_checkin(text)
            if label == "big":
                conv.update({"stage": "proposed", "variant": 0, "why": it.get("why"), "done": None,
                             "deadline": iso(self.nowdt.replace(hour=23, minute=59) + dt.timedelta(days=7))})
                self.meta[item_id]["streak"] = 0
                conv["steps"] = self.gen_steps(it, conv)
                out.append(self.breakdown_msg(it, conv, intro="Let's make it smaller."))
            elif label == "time":
                out.append(self.move_better_time(it, text))
                del self.convo[item_id]
            elif label == "drop":
                conv["stage"] = "confirm_drop"
                out.append(self.m("question", f"Okay. Shall I take {q(title)} off your list for good?", item_id))
            else:
                out.append(self.m("question", f"Thanks for telling me. For {q(title)}, would smaller steps, "
                                  "a different time, or letting it go feel best?", item_id))
            return {"messages": out}

        if stage == "confirm_drop":
            if brain.is_yes(text) or re.search(r"\b(delete|remove|drop)\b", text, re.I):
                ids = [item_id] + [c["id"] for c in self.children(item_id)]
                uid = self.make_undo(self.snapshot(ids))
                for i in ids:
                    self.items.pop(i, None)
                    self.convo.pop(i, None)
                self.log_add("delete", ids, {"title": title}, "person")
                out.append(self.m("reply", f"Done. {q(title)} is off your list. One less thing to carry.",
                                  None, undo_id=uid))
            else:
                del self.convo[item_id]
                self.meta[item_id]["streak"] = 0
                out.append(self.m("reply", f"Okay, {q(title)} stays. I'll keep it gently in view.", item_id))
            return {"messages": out}
        return {"messages": out}

    def gen_steps(self, it, conv):
        dl = from_iso(conv["deadline"])
        steps = brain.llm_steps(it["title"], conv.get("why"), conv.get("done"), tp.fmt_day(dl, self.nowdt))
        return steps or brain.rule_steps(it["title"], conv.get("done"), conv.get("variant", 0))

    def breakdown_msg(self, it, conv, revised=False, intro=None):
        steps = conv["steps"]
        dl = from_iso(conv["deadline"])
        lines = "\n".join(f"{i + 1}. {s['title']} ({s['estimate_min']} min)" for i, s in enumerate(steps))
        why = f" ({conv['why']})" if conv.get("why") else ""
        head = intro or ("Here's a revised plan" if revised else "Here's a gentle plan")
        if intro:
            head = f"{intro} Here's a gentle plan"
        text = (f"{head} for {q(it['title'])}{why}, finishing by {tp.fmt_day(dl, self.nowdt)}:\n{lines}\n"
                f"The first step is only {steps[0]['estimate_min']} minutes. Does this look okay?")
        return self.m("breakdown", text, it["id"], steps=steps)

    def schedule_steps(self, steps, deadline):
        pref = self.pref_minutes()
        earliest = self.round_up(self.nowdt)
        placed = []
        taken = []
        for i, s in enumerate(steps):
            dur = s["estimate_min"]
            slot = None
            for max_dev in (180, None):
                day = earliest.date()
                while slot is None and day <= deadline.date():
                    slot = self.find_slot(day, dur, pref, earliest, deadline, max_dev=max_dev)
                    if slot and any(not (slot + dt.timedelta(minutes=dur) <= a or slot >= b) for a, b in taken):
                        slot = None
                    day += dt.timedelta(days=1)
                if slot:
                    break
            if slot is None:
                slot = earliest
            end = slot + dt.timedelta(minutes=dur)
            placed.append((slot, end))
            taken.append((slot, end))
            left = len(steps) - i - 1
            next_day = dt.datetime.combine(slot.date() + dt.timedelta(days=1), dt.time(0, 0))
            if left and (deadline.date() - slot.date()).days >= left:
                earliest = next_day
            else:
                earliest = end
        return placed

    def accept_steps(self, goal, conv):
        dl = from_iso(conv["deadline"])
        before = self.snapshot([goal["id"]])
        slots = self.schedule_steps(conv["steps"], dl)
        created = []
        for s, (a, b) in zip(conv["steps"], slots):
            # create one at a time so later steps see earlier ones as busy
            c = self.new_item(kind="task", title=s["title"], start=iso(a), end=iso(b), window=part_of_day(a),
                              parent_id=goal["id"], estimate_min=s["estimate_min"], source="agent")
            created.append(c)
            self.log_add("create", [c["id"]], {"title": c["title"], "parent_id": goal["id"]}, "agent")
        if conv.get("why"):
            goal["why"] = conv["why"]
        goal["start"], goal["end"] = None, None
        goal["window"] = "by " + tp.fmt_day(dl, self.nowdt).split(" (")[0]
        self.meta[goal["id"]].update({"deadline": conv["deadline"], "streak": 0, "checkin": None})
        uid = self.make_undo(before, [c["id"] for c in created])
        first = created[0]
        return [self.m("reply", f"Done! I added {len(created)} small steps for {q(goal['title'])}. First up: "
                       f"{q(first['title'])}, {tp.fmt_when(from_iso(first['start']), self.nowdt)}. "
                       f"It's only {first['estimate_min']} minutes.", goal["id"], undo_id=uid)]

    def move_better_time(self, it, text):
        before = self.snapshot([it["id"]])
        dur = int((self.eff_end(it) - from_iso(it["start"])).total_seconds() // 60) if it["start"] else \
            (it["estimate_min"] or 30)
        p = tp.parse(text, self.nowdt)
        new = None
        if p["start"] and p["start"] > self.nowdt:
            new = p["start"]
            if not self.is_free(new, new + dt.timedelta(minutes=dur), {it["id"]}):
                new = self.find_slot(new.date(), dur, new.hour * 60 + new.minute, earliest=self.nowdt,
                                     exclude={it["id"]}) or new
        elif p["day"]:
            new = self.find_slot(p["day"], dur, self.pref_minutes(), earliest=self.nowdt, exclude={it["id"]})
        if new is None:
            old = from_iso(it["start"]) or self.nowdt
            pref = 10 * 60 if old.hour >= 15 else 19 * 60     # a different part of the day
            day = self.nowdt.date() + dt.timedelta(days=1)
            for k in range(7):
                new = self.find_slot(day + dt.timedelta(days=k), dur, pref, exclude={it["id"]})
                if new:
                    break
        it["start"], it["end"] = iso(new), iso(new + dt.timedelta(minutes=dur))
        it["window"] = None
        self.meta[it["id"]].update({"streak": 0, "checkin": None})
        self.log_add("update", [it["id"]], {"start": it["start"], "reason": "wrong time"}, "person")
        uid = self.make_undo(before)
        return self.m("reply", f"Okay, I moved {q(it['title'])} to {tp.fmt_when(new, self.nowdt)}, a different "
                      "time of day. If another time suits you better, just tell me.", it["id"], undo_id=uid)

    # ------------------------------------------------------------ done / undo
    def done(self, item_id):
        it = self.items.get(item_id)
        if not it:
            return None
        if it["status"] != "done":
            it["status"] = "done"
            self.meta.setdefault(item_id, {})["done_at"] = self.now
            self.meta[item_id]["done_by_person"] = True
            self.log_add("done", [item_id], {"title": it["title"]}, "person")
            self.meta[item_id]["streak"] = 0
            self.convo.pop(item_id, None)
            self.m("info", f"Nice, {q(it['title'])} is done.", item_id)
            par = self.items.get(it["parent_id"]) if it["parent_id"] else None
            if par and par["status"] == "open" and not self.children(par["id"], open_only=True):
                par["status"] = "done"
                self.meta.setdefault(par["id"], {})["done_at"] = self.now
                self.m("info", f"That was the last step of {q(par['title'])}. Lovely work.", par["id"])
        return {"item": it}

    def do_undo(self, undo_id):
        u = self.undo.get(undo_id)
        if not u:
            return False
        if u["used"]:
            return True
        for i in u["created"]:
            self.items.pop(i, None)
            self.meta.pop(i, None)
        for i, snap in u["before"].items():
            if snap is None:
                self.items.pop(i, None)
                continue
            item, meta = snap
            cur_end = self.eff_end(self.items[i]) if i in self.items else None
            self.items[i] = item
            self.meta[i] = meta
            if u.get("kind") == "postpone":
                # put back where it was and leave it there: no fresh auto-move for that slot
                self.meta[i]["skip"] = iso(self.eff_end(item))
            _ = cur_end
        u["used"] = True
        self.log_add("update", list(u["before"]) + list(u["created"]), {"undo_id": undo_id}, "person")
        self.m("info", "Okay, I put it back the way it was.", next(iter(u["before"]), None))
        return True

    # ------------------------------------------------------------ clock
    def pending_triggers(self, old, new):
        out = []
        for it in self.items.values():
            if it["status"] != "open" or it["kind"] == "note" or not it["start"]:
                continue
            s, e = from_iso(it["start"]), self.eff_end(it)
            if not s:
                continue
            k1 = f"{it['id']}|start|{iso(s)}"
            if k1 not in self.sent and old <= s <= new:
                out.append((s, 0, "start", it["id"], k1))
            t2 = max(e - dt.timedelta(minutes=10), s)   # very short items: right after the start nudge
            k2 = f"{it['id']}|end|{iso(e)}"
            if k2 not in self.sent and old <= t2 <= new:
                out.append((t2, 1, "end", it["id"], k2))
            if it["kind"] == "task" and not self.children(it["id"], open_only=True):
                t3 = e + dt.timedelta(minutes=31)
                meta = self.meta.setdefault(it["id"], {"streak": 0})
                if t3 <= new and meta.get("skip") != iso(e) and meta.get("checkin") != iso(e):
                    out.append((max(t3, old), 2, "postpone", it["id"], None))
        return out

    def clock(self, new_iso):
        new = from_iso(new_iso)
        old = self.nowdt
        if not new or new <= old:
            return {"messages": []}
        out = []
        for _ in range(5000):
            trig = self.pending_triggers(old, new)
            if not trig:
                break
            t, _, kind, iid, key = min(trig, key=lambda x: (x[0], x[1]))
            self.now = iso(max(t, old))
            it = self.items[iid]
            if kind in ("start", "end"):
                self.sent.add(key)
                out.append(self.reminder(it, kind))
            else:
                out.append(self.postpone(it))
            old = max(t, old)
        self.now = iso(new)
        self.learn()
        return {"messages": out}

    def reminder(self, it, which):
        s, e = from_iso(it["start"]), self.eff_end(it)
        if which == "start":
            if it["kind"] == "event":
                text = f"Gentle reminder: {q(it['title'])} is starting now ({tp.fmt_clock(s)})."
            else:
                text = (f"Gentle nudge: {q(it['title'])} starts now ({tp.fmt_clock(s)}). "
                        "Even five minutes counts.")
        else:
            text = (f"About 10 minutes left for {q(it['title'])} (until {tp.fmt_clock(e)}). "
                    "Wrapping up wherever you are is completely fine.")
        self.log_add("reminder", [it["id"]], {"which": which}, "clock")
        return self.m("reminder", text, it["id"])

    def postpone(self, it):
        meta = self.meta.setdefault(it["id"], {"streak": 0})
        e = self.eff_end(it)
        if meta.get("streak", 0) >= 2:
            meta["checkin"] = iso(e)
            self.convo[it["id"]] = {"stage": "checkin"}
            return self.m("checkin", f"{q(it['title'])} has moved a few times now, and that's completely okay. "
                          "What's getting in the way: does it feel too big (I can break it into smaller steps), "
                          "is it the wrong time (I can find a better one), or is it not needed anymore?", it["id"])
        before = self.snapshot([it["id"]])
        s = from_iso(it["start"])
        dur = max(5, int((e - s).total_seconds() // 60))
        pref = self.pref_minutes()
        day0 = self.nowdt.date() + dt.timedelta(days=1)
        new = None
        for max_dev in (180, None):
            for k in range(7):
                new = self.find_slot(day0 + dt.timedelta(days=k), dur, pref, exclude={it["id"]}, max_dev=max_dev)
                if new:
                    break
            if new:
                break
        if new is None:
            new = dt.datetime.combine(day0, dt.time(pref // 60, pref % 60))
        it["start"], it["end"] = iso(new), iso(new + dt.timedelta(minutes=dur))
        if it["window"]:
            it["window"] = part_of_day(new)
        it["postpone_count"] = (it.get("postpone_count") or 0) + 1
        meta["streak"] = meta.get("streak", 0) + 1
        uid = self.make_undo(before)
        self.undo[uid]["kind"] = "postpone"
        self.log_add("postponed", [it["id"]], {"from": before[it["id"]][0]["start"], "to": it["start"]}, "clock")
        habit = " That's when you usually get things done." if abs((new.hour * 60 + new.minute) - pref) <= 180 else ""
        return self.m("postponed", f"No worries, I moved {q(it['title'])} to {tp.fmt_when(new, self.nowdt)}."
                      f"{habit} Tap undo if you'd like it back where it was.", it["id"], undo_id=uid)

    # ------------------------------------------------------------ focus
    def focus(self):
        def key(it):
            return (it["start"] or "9999", list(self.items).index(it["id"]))
        open_steps = [c for c in self.items.values() if c["status"] == "open" and c["kind"] == "task"
                      and c["parent_id"] and self.items.get(c["parent_id"], {}).get("why")]
        item = min(open_steps, key=key) if open_steps else None
        if item is None:
            tasks = [t for t in self.items.values() if t["status"] == "open" and t["kind"] == "task"
                     and not self.children(t["id"], open_only=True)]
            item = min(tasks, key=key) if tasks else None
        parent = self.items.get(item["parent_id"]) if item and item["parent_id"] else None
        why = (parent or {}).get("why") or (item or {}).get("why")
        return {"item": item, "parent": parent, "why": why, "action": "Start 5 minutes"}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        app = self.app
        if path == "/health":
            return self.send_json({"ok": True})
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with app.lock:
            if path == "/api/state":
                return self.send_json(app.state())
            if path == "/api/focus":
                return self.send_json(app.focus())
            if path == "/api/llm_stats":
                return self.send_json(llm.STATS)
            if path == "/api/log":
                return self.send_json({"entries": app.log})
            if path == "/api/beliefs":
                app.update_beliefs()
                return self.send_json({"beliefs": list(app.beliefs.values())})
            if path == "/api/proposal":
                return self.send_json({"proposal": app.open_proposal()})
            if path == "/api/prompts":
                return self.send_json({"prompts": [app.pub(v) for v in app.prompts]})
            if path == "/api/activity":
                return self.send_json({"events": app.activity})
            if path == "/api/view":
                return self.send_json({"view": app.view_spec()})
            if path == "/api/catalog":
                return self.send_json({"primitives": CATALOG})
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self.body()
        app = self.app
        llm.start_request()
        try:
            with app.lock:
                res, code = self.route(app, path, data)
                if code == 200 and path not in ("/api/learn", "/api/reset"):
                    app.maybe_learn()
                if code == 200:
                    app.save()
        except Exception as e:  # never hang or crash the harness
            import traceback
            traceback.print_exc()
            res, code = {"error": str(e), "trace": traceback.format_exc()[-600:]}, 500
        self.send_json(res, code)

    def route(self, app, path, data):
        if path == "/api/reset":
            return app.reset(data.get("now"), data.get("items")), 200
        if path == "/api/command":
            return app.command(data.get("text", ""), data.get("mode", "command")), 200
        if path == "/api/chat":
            r = app.chat(str(data.get("item_id")), data.get("text", ""))
            return (r, 200) if r is not None else ({"error": "unknown item"}, 404)
        m = re.match(r"^/api/items/([^/]+)/done$", path)
        if m:
            r = app.done(m.group(1))
            return (r, 200) if r is not None else ({"error": "unknown item"}, 404)
        if path == "/api/undo":
            ok = app.do_undo(str(data.get("undo_id")))
            return ({"ok": True}, 200) if ok else ({"ok": False, "error": "unknown undo_id"}, 404)
        if path == "/api/clock":
            return app.clock(data.get("now")), 200
        if path == "/api/import":
            return app.import_file(data.get("filename", ""), data.get("content", "")), 200
        m = re.match(r"^/api/plan/([^/]+)/(apply|undo)$", path)
        if m:
            p = app.apply_plan(m.group(1)) if m.group(2) == "apply" else app.undo_plan(m.group(1))
            return ({"plan": p}, 200) if p else ({"error": "unknown plan"}, 404)
        if path == "/api/learn":
            return app.learn(), 200
        if path == "/api/critic":
            return app.critic(data.get("spec")), 200
        if path == "/api/design":
            return app.design_endpoint(data.get("belief_key")), 200
        m = re.match(r"^/api/proposals/([^/]+)/(accept|dismiss|undo)$", path)
        if m:
            r = app.proposal_action(m.group(1), m.group(2))
            return (r, 200) if r else ({"error": "unknown proposal"}, 404)
        if path == "/api/view":
            return {"view": app.set_view(data.get("type", "week"))}, 200
        m = re.match(r"^/api/prompts/([^/]+)/rollback$", path)
        if m:
            r = app.rollback(m.group(1))
            return ({"active": r}, 200) if r else ({"error": "unknown agent"}, 404)
        return {"error": "not found"}, 404

    def do_DELETE(self):
        path = self.path.split("?")[0]
        app = self.app
        with app.lock:
            m = re.match(r"^/api/beliefs/([^/]+)$", path)
            if m:
                import urllib.parse
                ok = app.delete_belief(urllib.parse.unquote(m.group(1)))
                app.save()
                return self.send_json({"ok": True} if ok else {"ok": False, "error": "unknown belief"},
                                      200 if ok else 404)
        self.send_json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    Handler.app = App(a.data)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    print(f"Evolving Calendar on http://localhost:{a.port}  (mock={llm.mock()})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
