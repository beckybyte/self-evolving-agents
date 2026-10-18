"""v2 typed update/delete/query, change-set plans (preview -> apply -> undo), .ics and plain-text import.
Mixed into App (app.py)."""
import copy
import datetime as dt
import re
import uuid

import timeparse as tp
from timeparse import from_iso, iso

STOP = {"my", "the", "a", "an", "to", "with", "appointment", "appt", "session", "class", "on", "at", "for", "please",
        "that", "this", "thing", "event", "task", "and"}
WEEKEND_RE = r"\b(?:this\s+|my\s+|the\s+|next\s+)?weekend\b"
CLEAR_RE = re.compile(r"\b(empty(?: out)?|clear(?: out)?|free up|free|wipe|block(?: out)?|cancel everything|cancel all|"
                      r"drop everything|away|"
                      r"out of town|travel(?:l?ing)?|off)\b", re.I)


def q(title):
    return f"“{title}”"


class CrudMixin:
    def wipe_crud(self):
        self.plans = {}

    # ------------------------------------------------------------ target + ranges
    def find_items(self, target, include_done=False):
        words = [w for w in re.findall(r"[a-z0-9']+", target.lower()) if w not in STOP]
        if not words:
            return []
        best, out = 0, []
        for it in self.items.values():
            if it["status"] == "done" and not include_done:
                continue
            tw = re.findall(r"[a-z0-9']+", it["title"].lower())
            score = sum(1 for w in words if any(t == w or t.startswith(w) or w.startswith(t) and len(t) > 3
                                                for t in tw))
            if score > best:
                best, out = score, [it]
            elif score == best and score:
                out.append(it)
        return out

    def weekend_range(self, text):
        now = self.nowdt
        wd = now.weekday()
        sat = now.date() + dt.timedelta(days=(5 - wd) % 7) if wd != 6 else now.date() - dt.timedelta(days=1)
        if re.search(r"\bnext\s+weekend\b", text, re.I):
            sat += dt.timedelta(days=7)
        a = dt.datetime.combine(sat, dt.time(0, 0))
        return a, a + dt.timedelta(days=2), "this weekend" if "next" not in text.lower() else "next weekend"

    def text_range(self, text):
        if re.search(WEEKEND_RE, text, re.I):
            return self.weekend_range(text)
        days = re.findall(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", text, re.I)
        if len(days) >= 2:
            names = [d.lower() for d in days]
            if set(names) == {"saturday", "sunday"}:
                return self.weekend_range(text)
            first = tp.parse(names[0], self.nowdt)["day"]
            last = tp.parse(names[-1], self.nowdt)["day"]
            if last < first:
                last += dt.timedelta(days=7)
            a = dt.datetime.combine(first, dt.time(0, 0))
            return a, dt.datetime.combine(last, dt.time(0, 0)) + dt.timedelta(days=1), \
                f"{days[0].capitalize()} to {days[-1].capitalize()}"
        p = tp.parse(text, self.nowdt)
        if p["day"]:
            a = dt.datetime.combine(p["day"], dt.time(0, 0))
            return a, a + dt.timedelta(days=1), tp.fmt_day(a, self.nowdt)
        return None

    def overlapping(self, a, b):
        out = []
        for it in self.items.values():
            if it["status"] != "open" or not it["start"] or it["kind"] == "note":
                continue
            s, e = from_iso(it["start"]), self.eff_end(it)
            if s < b and e > a:
                out.append(it)
        return out

    # ------------------------------------------------------------ plans
    def make_plan(self, origin, ops, extra_meta=None):
        plan = {"id": "pl_" + uuid.uuid4().hex[:8], "origin": origin, "status": "preview", "ops": ops,
                "affected": [o["item_id"] for o in ops if o["op"] in ("update", "delete")],
                "_meta": extra_meta or {}}
        self.plans[plan["id"]] = plan
        return plan

    def pub_plan(self, plan):
        return {k: v for k, v in plan.items() if not k.startswith("_")}

    def apply_plan(self, pid, source="person"):
        plan = self.plans.get(pid)
        if not plan:
            return None
        if plan["status"] != "preview":
            return self.pub_plan(plan)
        ids = []
        for o in plan["ops"]:
            if o["op"] == "create":
                it = copy.deepcopy(o["after"])
                self.items[it["id"]] = it
                self.meta[it["id"]] = dict(plan["_meta"].get(it["id"], {"streak": 0}))
            elif o["op"] == "delete":
                self.items.pop(o["item_id"], None)
                self.convo.pop(o["item_id"], None)
            elif o["op"] == "update" and o["item_id"] in self.items:
                self.items[o["item_id"]] = copy.deepcopy(o["after"])
            ids.append(o["item_id"])
            self.log_add(o["op"], [o["item_id"]], {"plan_id": pid, "title": (o["after"] or o["before"])["title"]},
                         plan["_meta"].get("_source", "typed"))
        plan["status"] = "applied"
        if plan["origin"] == "import":
            self.log_add("import", ids, {"plan_id": pid, "filename": plan["_meta"].get("_filename")},
                         plan["_meta"].get("_source", "ics"))
        self.log_add("plan_applied", ids, {"plan_id": pid, "ops": len(plan["ops"])}, "person")
        n = len(plan["ops"])
        self.m("info", f"Done, I applied {n} change{'s' if n != 1 else ''}. You can undo it in one step.")
        return self.pub_plan(plan)

    def undo_plan(self, pid):
        plan = self.plans.get(pid)
        if not plan:
            return None
        if plan["status"] != "applied":
            return self.pub_plan(plan)
        ids = []
        for o in reversed(plan["ops"]):
            if o["op"] == "create":
                self.items.pop(o["item_id"], None)
            elif o["op"] in ("delete", "update") and o["before"]:
                self.items[o["item_id"]] = copy.deepcopy(o["before"])
                self.meta.setdefault(o["item_id"], {"streak": 0})
            ids.append(o["item_id"])
        # keep the original order of restored items stable enough: re-sort by original insertion is not needed
        plan["status"] = "undone"
        self.log_add("plan_undone", ids, {"plan_id": pid}, "person")
        self.m("info", "Okay, everything is back the way it was.")
        return self.pub_plan(plan)

    # ------------------------------------------------------------ typed intents
    def intent(self, text):
        """Returns a /api/command response for update/delete/query commands, or None for a plain create."""
        t = text.strip()
        low = t.lower()
        if re.match(r"^(what'?s|what is|what do i have|what have i got|anything|show me|how'?s|how does|"
                    r"what'?s on|do i have|am i free|list)\b", low) or (low.endswith("?") and re.search(
                    r"\b(what|anything|which|free)\b", low)):
            return self.query(t)
        if CLEAR_RE.search(low) and re.search(WEEKEND_RE + r"|\b(saturday|sunday|monday|tuesday|wednesday|thursday|"
                                              r"friday|tomorrow|today)\b", low) and \
                re.search(r"\b(empty|clear|free up|free|wipe|block|away|out of town|cancel everything|cancel all|"
                          r"drop everything|nothing on)\b", low) and not re.match(r"^(move|reschedule|push|shift)\b", low):
            return self.bulk_clear(t)
        m = re.match(r"^(?:please\s+)?(?:cancel|delete|remove|drop|scrap|forget about|nix|get rid of|"
                     r"take off)\s+(.+?)(?:\s+from my (?:calendar|list))?[.!]?$", t, re.I)
        if m:
            return self.delete_one(t, m.group(1))
        m = re.match(r"^(?:please\s+)?(?:move|reschedule|push|shift|change|postpone|put)\s+(.+?)\s+"
                     r"(?:to|until|till|for|on)\s+(.+?)[.!]?$", t, re.I)
        if m:
            return self.move_one(t, m.group(1), m.group(2))
        return None

    def _resp(self, msgs, plan=None):
        r = {"messages": msgs, "created": []}
        if plan:
            r["plan"] = self.pub_plan(plan)
        return r

    def query(self, text):
        out = [self.m("reply", text, role="person")]
        rng = self.text_range(text)
        if not rng:
            a = dt.datetime.combine(self.nowdt.date(), dt.time(0, 0))
            rng = (a, a + dt.timedelta(days=1), tp.fmt_day(a, self.nowdt))
        a, b, label = rng
        found = []
        for it in self.items.values():
            s = from_iso(it["start"])
            if s and it["kind"] != "note" and (a <= s < b or (s < b and self.eff_end(it) > a)):
                found.append(it)
        found.sort(key=lambda i: i["start"])
        if found:
            parts = []
            for it in found:
                when = it["window"] or tp.fmt_clock(from_iso(it["start"]))
                parts.append(f"{q(it['title'])} ({when}{', done' if it['status'] == 'done' else ''})")
            reply = f"On {label}: " + "; ".join(parts) + "."
        else:
            reply = f"{label[:1].upper() + label[1:]} is wide open. Nothing planned."
        out.append(self.m("reply", reply))
        self.log_add("query", [i["id"] for i in found], {"text": text, "range": [iso(a), iso(b)]}, "typed")
        return self._resp(out)

    def delete_one(self, text, target):
        out = [self.m("reply", text, role="person")]
        its = self.find_items(target)
        if not its:
            out.append(self.m("reply", f"I couldn't find {q(target)} on your list, so nothing changed."))
            self.log_add("query", [], {"text": text, "result": "no match"}, "typed")
            return self._resp(out)
        it = its[0]
        plan = self.make_plan("command", [{"op": "delete", "item_id": it["id"], "before": copy.deepcopy(it),
                                           "after": None}], {"_source": "typed"})
        msg = self.m("question", f"I can take {q(it['title'])} off your calendar. Shall I go ahead?", it["id"])
        msg["plan_id"] = plan["id"]
        out.append(msg)
        return self._resp(out, plan)

    def new_start_for(self, it, when):
        s = from_iso(it["start"])
        base = s or self.nowdt.replace(hour=self.pref_minutes() // 60, minute=self.pref_minutes() % 60)
        w = when.lower()
        m = re.search(r"\b(?:next week|a week later|in a week)\b", w)
        if m and not re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", w):
            return base + dt.timedelta(days=7)
        m = re.search(r"\b(?:in\s+)?(\d+|a|one|two|three)\s+days?\s*(?:later)?\b", w)
        if m and not re.search(r"\d\s*(am|pm|:)", w):
            n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1), None) or int(m.group(1))
            return base + dt.timedelta(days=n)
        p = tp.parse(when, base if base > self.nowdt else self.nowdt)
        day = p["day"]
        if re.search(r"\bnext\b", w) and day:
            pass
        if p["start"] and (p["clear"] or not day or p["window"]):
            ns = p["start"]
            if day:
                ns = dt.datetime.combine(day, ns.time())
            elif s and not re.search(r"\b(today|tonight|tomorrow)\b", w):
                ns = dt.datetime.combine(s.date(), ns.time())
                if ns <= self.nowdt:
                    ns += dt.timedelta(days=1)
            return ns
        if day:
            return dt.datetime.combine(day, base.time())
        return None

    def move_one(self, text, target, when):
        out = [self.m("reply", text, role="person")]
        its = self.find_items(target)
        if not its:
            out.append(self.m("reply", f"I couldn't find {q(target)} on your list, so nothing changed."))
            return self._resp(out)
        it = its[0]
        ns = self.new_start_for(it, when)
        if not ns:
            out.append(self.m("question", f"When would you like {q(it['title'])} instead?", it["id"]))
            return self._resp(out)
        before = self.snapshot([it["id"]])
        old = copy.deepcopy(it)
        dur = (self.eff_end(it) - from_iso(it["start"])) if it["start"] else dt.timedelta(
            minutes=it["estimate_min"] or 30)
        it["start"], it["end"] = iso(ns), iso(ns + dur)
        if it["window"] and it["kind"] != "event":
            it["window"] = None
        self.meta.setdefault(it["id"], {})["streak"] = 0
        uid = self.make_undo(before)
        self.log_add("update", [it["id"]], {"text": text, "before": {"start": old["start"]},
                                            "after": {"start": it["start"]}}, "typed")
        out.append(self.m("reply", f"Moved {q(it['title'])} to {tp.fmt_when(ns, self.nowdt)}.", it["id"],
                          undo_id=uid))
        return self._resp(out)

    def clear_ops(self, a, b):
        return [{"op": "delete", "item_id": it["id"], "before": copy.deepcopy(it), "after": None}
                for it in self.overlapping(a, b)]

    def range_event(self, title, a, b, label, source):
        return self.new_item(insert=False, kind="event", title=title, start=iso(a),
                             end=iso(b - dt.timedelta(minutes=1)), window=label, source=source)

    @staticmethod
    def purpose(text):
        t = text.strip().rstrip(".!")
        for pat in (r"\bfor\s+(?:a|an|my|the|some)?\s*(.+)$",
                    r"\b(?:i'?m|i am|we'?re|we are|i'?ll be|we'?ll be)\s+(?:going|heading|off)\s+(?:on\s+|to\s+)?(?:a\s+|an\s+|the\s+)?(.+)$",
                    r"\b(?:going|heading|off)\s+(?:on\s+|to\s+)?(?:a\s+|an\s+|the\s+)?(.+)$"):
            m = re.search(pat, t, re.I)
            if m:
                cand = re.split(r",|;|\bplease\b|\band clear\b", m.group(1))[0].strip()
                cand = re.sub(WEEKEND_RE + r"|\b(?:on |this |next )?(?:saturday|sunday)(?: and sunday)?\b", "", cand,
                              flags=re.I).strip(" ,.")
                if cand:
                    return cand
        m = re.search(r"\b(\w*(?:trip|camping|camp|vacation|holiday|wedding|conference|retreat|visit|festival|"
                      r"hike|hiking|getaway))\b", t, re.I)
        if m:
            return m.group(1)
        return "Kept free"

    def bulk_clear(self, text):
        out = [self.m("reply", text, role="person")]
        a, b, label = self.text_range(text)
        ops = self.clear_ops(a, b)
        title = self.purpose(text)
        if title:
            ev = self.range_event(title, a, b, label, "typed")
            ops.append({"op": "create", "item_id": ev["id"], "before": None, "after": ev})
        plan = self.make_plan("command", ops, {"_source": "typed"})
        names = ", ".join(q(o["before"]["title"]) for o in ops if o["op"] == "delete") or "nothing"
        add = f" and add {q(title)}" if title else ""
        msg = self.m("question", f"Here's the plan for {label}: clear {names}{add}. "
                     "Nothing changes until you say so. Shall I apply it?")
        msg["plan_id"] = plan["id"]
        out.append(msg)
        return self._resp(out, plan)

    # ------------------------------------------------------------ imports
    def import_file(self, filename, content):
        content = content or ""
        if "BEGIN:VCALENDAR" in content.upper() or (filename or "").lower().endswith(".ics"):
            plan = self.import_ics(filename, content)
        else:
            plan = self.import_text(filename, content)
        n = len(plan["ops"])
        msg = self.m("info", f"I read {filename or 'your note'} and prepared {n} change{'s' if n != 1 else ''}. "
                     "Nothing changes until you apply it.")
        msg["plan_id"] = plan["id"]
        return {"plan": self.pub_plan(plan)}

    def import_ics(self, filename, content):
        text = re.sub(r"\r?\n[ \t]", "", content.replace("\r\n", "\n"))
        events, cur = [], None
        for line in text.split("\n"):
            line = line.strip()
            if line.upper() == "BEGIN:VEVENT":
                cur = {}
            elif line.upper() == "END:VEVENT":
                if cur is not None:
                    events.append(cur)
                cur = None
            elif cur is not None and ":" in line:
                head, val = line.split(":", 1)
                parts = head.split(";")
                params = {}
                for p in parts[1:]:
                    if "=" in p:
                        k, v = p.split("=", 1)
                        params[k.upper()] = v
                cur[parts[0].upper()] = (val, params)
        existing_uids = {m.get("uid") for m in self.meta.values() if m.get("uid")}
        existing_keys = {(i["title"].strip().lower(), i["start"]) for i in self.items.values()}
        ops, metas = [], {"_source": "ics", "_filename": filename}
        for ev in events:
            start, allday = self._ics_dt(ev.get("DTSTART"))
            if not start:
                continue
            end, _ = self._ics_dt(ev.get("DTEND"))
            if not end:
                dur = self._ics_duration(ev.get("DURATION", ("", {}))[0])
                end = start + (dur or (dt.timedelta(days=1) if allday else dt.timedelta(hours=1)))
            if allday and end > start:
                end = end - dt.timedelta(minutes=1)
            title = self._ics_text(ev.get("SUMMARY", ("(no title)", {}))[0]) or "(no title)"
            uid = ev.get("UID", ("", {}))[0].strip() or None
            key = (title.strip().lower(), iso(start))
            if (uid and uid in existing_uids) or key in existing_keys:
                continue
            it = self.new_item(insert=False, kind="event", title=title, start=iso(start), end=iso(end),
                               window="all day" if allday else None, source="ics")
            if "RRULE" in ev:
                it["recurrence"] = ev["RRULE"][0]
            ops.append({"op": "create", "item_id": it["id"], "before": None, "after": it})
            metas[it["id"]] = {"streak": 0, "uid": uid}
            if uid:
                existing_uids.add(uid)
            existing_keys.add(key)
        return self.make_plan("import", ops, metas)

    @staticmethod
    def _ics_text(v):
        return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").strip()

    @staticmethod
    def _ics_duration(v):
        m = re.match(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", (v or "").strip())
        if not m or not any(m.groups()):
            return None
        w, d, hh, mm, _ = (int(x) if x else 0 for x in m.groups())
        return dt.timedelta(weeks=w, days=d, hours=hh, minutes=mm)

    @staticmethod
    def _ics_dt(field):
        if not field:
            return None, False
        val, params = field
        val = val.strip()
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", val):
            try:
                return dt.datetime.strptime(val[:8], "%Y%m%d"), True
            except ValueError:
                return None, False
        m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z?)", val)
        if not m:
            return None, False
        # TZID / floating / UTC: kept as the wall-clock time written in the file
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M"), False

    def import_text(self, filename, content):
        ops, metas = [], {"_source": "text", "_filename": filename}
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", content) if s.strip()]
        taken = set()
        for s in sentences:
            s2 = re.sub(r"^(also|and|plus|oh,?|ps:?|p\.s\.)\s+", "", s.strip(), flags=re.I).strip(" .!")
            low = s2.lower()
            if not s2:
                continue
            rng = self.text_range(s2)
            if rng and CLEAR_RE.search(low) and not re.match(r"^(lunch|dinner|coffee|meet)", low):
                a, b, label = rng
                for o in self.clear_ops(a, b):
                    if o["item_id"] not in taken:
                        ops.append(o)
                        taken.add(o["item_id"])
                if re.search(r"\b(away|out of town|travel|trip)\b", low):
                    ev = self.range_event("Away", a, b, label, "text")
                    ops.append({"op": "create", "item_id": ev["id"], "before": None, "after": ev})
                continue
            m = re.match(r"^(?:please\s+)?(?:cancel|delete|remove|drop|skip)\s+(.+)$", s2, re.I)
            if m:
                its = self.find_items(m.group(1))
                if its and its[0]["id"] not in taken:
                    ops.append({"op": "delete", "item_id": its[0]["id"], "before": copy.deepcopy(its[0]),
                                "after": None})
                    taken.add(its[0]["id"])
                continue
            m = re.match(r"^(?:please\s+)?(?:move|reschedule|push|shift)\s+(.+?)\s+(?:to|until|till|on)\s+(.+)$",
                         s2, re.I)
            if m:
                its = self.find_items(m.group(1))
                if its and its[0]["id"] not in taken:
                    ns = self.new_start_for(its[0], m.group(2))
                    if ns:
                        after = copy.deepcopy(its[0])
                        dur = (self.eff_end(after) - from_iso(after["start"])) if after["start"] else \
                            dt.timedelta(minutes=after["estimate_min"] or 30)
                        after["start"], after["end"] = iso(ns), iso(ns + dur)
                        ops.append({"op": "update", "item_id": after["id"], "before": copy.deepcopy(its[0]),
                                    "after": after})
                        taken.add(after["id"])
                continue
            s3 = re.sub(r"^(?:i'?ll|i will|i'?m|i am|i have|i've got|there'?s)\s+(?:have\s+)?", "", s2,
                        flags=re.I).strip()
            it, _ = self.make_item(s3, "text", insert=False)
            ops.append({"op": "create", "item_id": it["id"], "before": None, "after": it})
            metas[it["id"]] = {"streak": 0}
        return self.make_plan("import", ops, metas)
