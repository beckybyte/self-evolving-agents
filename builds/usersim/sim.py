"""LLM user simulator: one episode = a persona driving the app for up to `max_steps` actions over HTTP.

The simulated person (an LLM) picks ONE JSON action per step; after every step the `Checker` tests contract
invariants against the API state and the response. Written only from contract.md (no harness/scenario/reference code).
"""
import calendar
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

BASE_DAY = datetime(2026, 10, 19)  # a Monday
FIELDS = ("title", "kind", "start", "end", "window", "status")
GUILT = re.compile(r"\b(overdue|failed|missed|late|behind|should have|should've|forgot|forgotten|lazy|red alert)\b",
                   re.I)
INVARIANTS = ["http", "no_guilt", "one_question", "breakdown_shape", "delete_preview", "no_lost_item",
              "undo_exact", "proposal_number", "no_hidden_item", "names_task", "dump_fragments",
              "clock_followthrough", "needs_yes", "time_only_item"]
STOP = set("the and for with maybe need about some that this from into just get got have really also then".split())
TIMEWORD = re.compile(r"(mon|tues|wednes|thurs|fri|satur|sun)day|today|tonight|tomorrow|morning|afternoon|evening|"
                      r"night|noon|week|weekend|next|\d+(am|pm)?|am|pm|around|before|after|later|soon")
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|please|let'?s|go ahead|sounds good|do it|alright|why not|break|split|steps?|smaller)\b", re.I)
OFFER = re.compile(r"break|smaller|tiny steps|steps", re.I)
TIME_ONLY = set("in the next within by until before after end of this coming few couple month months week weeks day "
                "days year years summer winter spring fall autumn soon later someday".split())
ACTIONS = ["command", "dump", "chat", "done", "clock", "proposal", "undo", "plan", "import", "view", "learn"]


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse(s):
    try:
        return datetime.fromisoformat(str(s)[:16])
    except Exception:
        return None


# ---------------------------------------------------------------- HTTP

def http(base: str, method: str, path: str, body=None, timeout: float = 70.0) -> dict:
    """{'status': int|None, 'secs': float, 'json': dict|None, 'error': str|None}"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:
        return {"status": None, "secs": round(time.time() - t0, 2), "json": None, "error": repr(e)[:200]}
    try:
        js = json.loads(raw or b"{}")
    except Exception:
        js = None
    return {"status": status, "secs": round(time.time() - t0, 2), "json": js,
            "error": None if js is not None else raw[:200].decode("utf-8", "replace")}


# ---------------------------------------------------------------- view coverage (independent of the app's critic)

def _dates(a: date, b: date):
    while a <= b:
        yield a
        a += timedelta(days=1)


def _el_dates(el: dict) -> set:
    t, p = el.get("type"), el.get("props") or {}
    try:
        if t == "MonthGrid":
            y, m = map(int, str(p["month"])[:7].split("-"))
            return set(_dates(date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])))
        if t == "WeekColumns":
            s = date.fromisoformat(str(p["start"])[:10])
            return set(_dates(s, s + timedelta(days=int(p.get("days", 7)) - 1)))
        if t == "DayTimeline":
            return {date.fromisoformat(str(p["date"])[:10])}
        if t == "AgendaList":
            return set(_dates(date.fromisoformat(str(p["from"])[:10]), date.fromisoformat(str(p["to"])[:10])))
    except Exception:
        return set()
    return set()


def _filter_ok(flt, it: dict) -> bool:
    if not isinstance(flt, dict):
        return True
    st = parse(it.get("start"))
    for k, v in flt.items():
        if k == "kind" and it.get("kind") != v:
            return False
        if k == "window" and not (it.get("window") and str(v).lower() in it["window"].lower()):
            return False
        if k == "hour_from" and (st is None or st.hour < int(v)):
            return False
        if k == "hour_to" and (st is None or st.hour >= int(v)):
            return False
        # "category" is not an Item field in the contract: treated as matching (lenient)
    return True


def hidden_problems(view: dict, items: list, now: datetime) -> list[str]:
    """Open dated items the view leaves out without counting them in hidden_counts."""
    if not isinstance(view, dict):
        return []
    hc = view.get("hidden_counts") or {}
    t = view.get("type")
    dated = [(it, parse(it.get("start"))) for it in items if it.get("status") != "done" and parse(it.get("start"))]
    missing: dict[str, list] = {}
    if t == "workweek":
        mon = (now - timedelta(days=now.weekday())).date()
        weekend = {mon + timedelta(days=5), mon + timedelta(days=6)}
        for it, st in dated:
            if st.date() in weekend:
                missing.setdefault(st.date().isoformat(), []).append(it)
    elif t == "custom":
        spec = view.get("spec") or {}
        els = spec.get("elements") or {}
        root = spec.get("root")
        if root not in els:
            return ["custom view without a valid root"] if els else []
        covered: set = set()
        leaves = []  # (element, [ancestor lane filters])

        def walk(eid, filters, depth=0):
            el = els.get(eid)
            if not isinstance(el, dict) or depth > 20:
                return
            covered.update(_el_dates(el))
            f2 = filters + ([el.get("props", {}).get("filter")] if el.get("type") == "Lane" else [])
            leaves.append((el, f2))
            for c in el.get("children") or []:
                walk(c, f2, depth + 1)

        walk(root, [])
        for it, st in dated:
            d = st.date()
            if d not in covered:
                continue
            shown = False
            for el, flts in leaves:
                if not all(_filter_ok(f, it) for f in flts):
                    continue
                if el.get("type") == "Bucket" and _filter_ok((el.get("props") or {}).get("filter"), it):
                    shown = True
                elif d in _el_dates(el):
                    p = el.get("props") or {}
                    if el.get("type") == "DayTimeline" and not (
                            int(p.get("from_hour", 0)) <= st.hour < int(p.get("to_hour", 24)) or
                            (p.get("to_hour") in (None, 24) and st.hour >= int(p.get("from_hour", 0)))):
                        continue
                    shown = True
                if shown:
                    break
            if not shown:
                missing.setdefault(d.isoformat(), []).append(it)
    out = []
    for d, its in missing.items():
        if int(hc.get(d, 0) or 0) < len(its):
            out.append(f"{t} view hides {len(its)} item(s) on {d} ({its[0].get('title')!r}) but hidden_counts[{d}]="
                       f"{hc.get(d, 0)}")
    return out


# ---------------------------------------------------------------- invariant checker

def _names_task(text: str, title: str) -> bool:
    t, x = (title or "").lower().strip(), (text or "").lower()
    if not t or t in x:
        return True
    words = [w for w in re.findall(r"[a-z0-9']+", t) if len(w) > 2]
    return bool(words) and sum(w in x for w in words) / len(words) >= 0.6


class Checker:
    """Stateful per-episode checker. `step()` returns a list of violation dicts and counts applicable checks."""

    def __init__(self, seed_ids=()):
        self.checks = {k: 0 for k in INVARIANTS}
        self.violations: list[dict] = []
        self.seen_msgs: set = set()
        self.seen_props: set = set()
        self.seed_ids = set(seed_ids)
        self.imported: set = set()
        self.undo_map: dict = {}         # undo_id -> {"item_id", "before": Item|None}
        self.plans: dict = {}            # plan id -> plan as previewed/applied
        self.view_before_accept: dict = {}
        self.last_view_key = None
        self.undone_ids: set = set()     # items restored by an undo: not expected to be re-moved
        self.checkin_ids: set = set()
        self.offer_open: set = set()     # items with an open "break it into steps?" question
        self.step_no = 0

    def _v(self, inv, detail, out):
        d = {"step": self.step_no, "invariant": inv, "detail": detail[:400]}
        out.append(d)
        self.violations.append(d)

    def step(self, action: dict, before: dict, after: dict, resp: dict, proposal_after=None) -> list[dict]:
        self.step_no += 1
        out: list[dict] = []
        a = action.get("action")
        js = resp.get("json") if isinstance(resp.get("json"), dict) else {}
        b_items = {it["id"]: it for it in before.get("items", [])}
        a_items = {it["id"]: it for it in after.get("items", [])}

        # 1. HTTP health
        self.checks["http"] += 1
        if resp.get("status") is None or resp["status"] >= 500 or resp.get("secs", 0) > 60:
            self._v("http", f"{a}: status={resp.get('status')} secs={resp.get('secs')} err={resp.get('error')}", out)

        # 2-5, 9. new agent messages (state + response, by id)
        new = {}
        for m in list(after.get("messages", [])) + list(js.get("messages") or []):
            if isinstance(m, dict) and m.get("id") and m["id"] not in self.seen_msgs:
                new[m["id"]] = m
        self.seen_msgs.update(new)
        for m in new.values():
            if m.get("undo_id"):
                iid = m.get("item_id")
                self.undo_map[m["undo_id"]] = {"item_id": iid, "before": b_items.get(iid), "kind": m.get("kind")}
            if m.get("role") != "agent":
                continue
            text = m.get("text") or ""
            self.checks["no_guilt"] += 1
            g = GUILT.search(text)
            if g:
                self._v("no_guilt", f"[{m.get('kind')}] uses {g.group(0)!r}: {text}", out)
            self.checks["one_question"] += 1
            if text.count("?") > 1:
                self._v("one_question", f"[{m.get('kind')}] {text.count('?')} questions: {text}", out)
            if m.get("kind") == "breakdown":
                self.checks["breakdown_shape"] += 1
                steps = m.get("steps") or []
                est = steps[0].get("estimate_min") if steps and isinstance(steps[0], dict) else None
                if not 2 <= len(steps) <= 5:
                    self._v("breakdown_shape", f"{len(steps)} steps: {json.dumps(steps)[:300]}", out)
                elif not isinstance(est, (int, float)) or est > 15:
                    self._v("breakdown_shape", f"first step {steps[0].get('title')!r} estimate_min={est}", out)
            if m.get("kind") in ("postponed", "reminder"):
                self.checks["names_task"] += 1
                it = a_items.get(m.get("item_id")) or b_items.get(m.get("item_id"))
                if not it:
                    self._v("names_task", f"[{m.get('kind')}] item_id {m.get('item_id')} unknown: {text}", out)
                elif not _names_task(text, it.get("title")):
                    self._v("names_task", f"[{m.get('kind')}] does not name {it.get('title')!r}: {text}", out)

        # 11. (contract 2) no dump fragment lost: each fragment's content words survive in some created title.
        # One check per dump; the violation lists the lost fragments.
        if a == "dump" and resp.get("status") == 200:
            created = [a_items[i] for i in set(a_items) - set(b_items)]
            twords = set(re.findall(r"[a-z0-9]+", " ".join((it.get("title") or "").lower() for it in created)))
            lost_frags, n_frags = [], 0
            for frag in re.split(r"[,;\n]+", str(action.get("text", ""))):
                words = [w for w in re.findall(r"[a-z0-9]+", frag.lower())
                         if len(w) > 2 and w not in STOP and not TIMEWORD.fullmatch(w)]
                if not words:
                    continue
                n_frags += 1
                lost = [w for w in words if w not in twords and not any(t.startswith(w[:5]) for t in twords)]
                if len(lost) / len(words) > 0.4:
                    lost_frags.append(f"{frag.strip()!r} (lost {lost})")
            if n_frags:
                self.checks["dump_fragments"] += 1
                if lost_frags:
                    self._v("dump_fragments", f"{len(lost_frags)}/{n_frags} fragments lost: " + "; ".join(lost_frags[:3])
                            + f"; created titles: {[it.get('title') for it in created][:6]}", out)

        # 12. (contracts 6-8) the clock follows through: an open item whose start the clock passes gets a reminder,
        # and an open task whose end is >30 min in the past after the tick is moved (or gets a check-in)
        if a == "clock" and resp.get("status") == 200:
            old, new_now = parse(before.get("now")), parse(after.get("now"))
            for m in new.values():
                if m.get("kind") == "checkin" and m.get("item_id"):
                    self.checkin_ids.add(m["item_id"])
            told = {m.get("item_id") for m in new.values() if m.get("kind") in ("reminder", "postponed", "checkin")}
            if old and new_now:
                for iid, it in b_items.items():
                    if it.get("status") == "done" or iid in self.undone_ids or iid in self.checkin_ids:
                        continue
                    st, en = parse(it.get("start")), parse(it.get("end"))
                    if st and old < st <= new_now:
                        self.checks["clock_followthrough"] += 1
                        if iid not in told:
                            self._v("clock_followthrough", f"clock {fmt(old)}->{fmt(new_now)} passed the start of "
                                                           f"{it.get('title')!r} ({it.get('start')}) with no reminder", out)
                    now_it = a_items.get(iid)
                    if (it.get("kind") == "task" and en and now_it and now_it.get("status") != "done"
                            and parse(now_it.get("end")) and parse(now_it.get("end")) < new_now - timedelta(minutes=30)):
                        self.checks["clock_followthrough"] += 1
                        if iid not in told:
                            self._v("clock_followthrough", f"task {it.get('title')!r} ended {now_it.get('end')}, clock now "
                                                           f"{fmt(new_now)}: not moved, no check-in", out)

        # 13. (contract 3) nothing is broken down without a yes: a chat reply to an open breakdown offer that has no
        # yes in it (e.g. an unrelated new goal typed into the conversation) must not start the breakdown questions
        if a == "chat" and action.get("item_id") in self.offer_open:
            self.offer_open.discard(action["item_id"])
            text = str(action.get("text", ""))
            if not YES.search(text):
                self.checks["needs_yes"] += 1
                went_on = [m for m in new.values() if m.get("role") == "agent" and m.get("item_id") in
                           (action["item_id"], None) and (m.get("kind") == "breakdown" or
                                                         (m.get("kind") == "question" and
                                                          re.search(r"\bwhy\b|matter", m.get("text") or "", re.I)))]
                if went_on:
                    self._v("needs_yes", f"reply {text[:80]!r} has no yes, but the agent went on: "
                                         f"[{went_on[0].get('kind')}] {went_on[0].get('text', '')[:120]}", out)
        for m in new.values():
            if (m.get("role") == "agent" and m.get("kind") == "question" and m.get("item_id")
                    and OFFER.search(m.get("text") or "")):
                self.offer_open.add(m["item_id"])

        # 14. (contract 2) a time phrase is not an item: no created title made only of time words / numbers
        if a in ("command", "dump"):
            for iid in set(a_items) - set(b_items):
                title = a_items[iid].get("title") or ""
                words = re.findall(r"[a-z]+|\d+", title.lower())
                self.checks["time_only_item"] += 1
                if words and all(w.isdigit() or w in TIME_ONLY or TIMEWORD.fullmatch(w) for w in words):
                    self._v("time_only_item", f"created an item from a time phrase: {title!r}", out)

        # 4. deletes of >1 item or anything imported come back as a preview; a preview changes nothing
        deleted = set(b_items) - set(a_items)
        plan = js.get("plan") if isinstance(js.get("plan"), dict) else None
        handled = set()
        if plan and plan.get("id"):
            self.plans[plan["id"]] = plan
        if a in ("command", "dump", "import"):
            self.checks["delete_preview"] += 1
            if plan and plan.get("status") == "preview":
                changed = [i for i in plan.get("affected") or [] if i in b_items and
                           (i not in a_items or any(b_items[i].get(f) != a_items[i].get(f) for f in FIELDS))]
                if deleted or changed:
                    self._v("delete_preview", f"preview plan but items changed before apply: deleted="
                                              f"{[b_items[i]['title'] for i in deleted]} changed={changed}", out)
                handled |= deleted
            elif a == "import" and (deleted or set(a_items) - set(b_items)):
                self._v("delete_preview", "import changed items without a preview plan", out)
                handled |= deleted
            else:
                bad = [i for i in deleted if b_items[i].get("source") in ("ics", "import") or i in self.imported]
                if len(deleted) > 1 or bad:
                    self._v("delete_preview", f"deleted {[b_items[i]['title'] for i in deleted]} at once "
                                              f"(imported: {len(bad)}) without a preview plan", out)
                handled |= deleted

        # 5. no item lost without a delete the user confirmed
        if deleted:
            self.checks["no_lost_item"] += 1
            allowed = set(handled)
            if a == "plan" and action.get("op") == "apply":
                p = plan or self.plans.get(action.get("plan_id")) or {}
                allowed |= {o.get("item_id") for o in p.get("ops") or [] if o.get("op") == "delete"}
            if a in ("command", "dump") and len(deleted) == 1:
                allowed |= deleted
            if a == "chat" and len(deleted) == 1:  # e.g. check-in "not needed" -> confirm -> delete
                allowed |= deleted
            if a in ("undo", "plan"):  # undoing a create removes the created item
                allowed |= {i for i in deleted if i not in self.seed_ids}
            lost = deleted - allowed
            if lost:
                self._v("no_lost_item", f"{a} removed {[b_items[i]['title'] for i in lost]} with no confirmed delete",
                        out)
        if a == "plan" and action.get("op") == "apply" and (plan or {}).get("origin") == "import":
            self.imported |= set(a_items) - set(b_items)

        # 6. undo restores the exact prior fields
        if a == "undo":
            ent = self.undo_map.get(action.get("undo_id"))
            if ent and ent["item_id"]:
                self.undone_ids.add(ent["item_id"])
                self.checks["undo_exact"] += 1
                iid, prior = ent["item_id"], ent["before"]
                now_it = a_items.get(iid)
                if prior is None and now_it is not None and ent.get("kind") not in ("postponed", "reminder"):
                    self._v("undo_exact", f"undo of the change that created {now_it.get('title')!r} left it in place",
                            out)
                elif prior is not None:
                    if now_it is None:
                        self._v("undo_exact", f"undo removed {prior.get('title')!r}", out)
                    else:
                        diff = {f: (prior.get(f), now_it.get(f)) for f in FIELDS if prior.get(f) != now_it.get(f)}
                        if diff:
                            self._v("undo_exact", f"{prior.get('title')!r} not restored: {diff}", out)
        if a == "plan" and action.get("op") == "undo":
            p = self.plans.get(action.get("plan_id")) or plan or {}
            if p.get("ops"):
                self.checks["undo_exact"] += 1
                for o in p["ops"]:
                    bf = o.get("before")
                    if bf and bf.get("id"):
                        now_it = a_items.get(bf["id"])
                        diff = {f: (bf.get(f), (now_it or {}).get(f)) for f in FIELDS
                                if now_it is None or bf.get(f) != now_it.get(f)}
                        if diff:
                            self._v("undo_exact", f"plan undo: {bf.get('title')!r} not restored: {diff}", out)
                    elif o.get("op") == "create":
                        iid = o.get("item_id") or (o.get("after") or {}).get("id")
                        if iid and iid in a_items:
                            self._v("undo_exact", f"plan undo left created item {a_items[iid].get('title')!r}", out)
        if a == "proposal":
            pid = (js.get("proposal") or {}).get("id") or action.get("proposal_id")
            if action.get("op") == "accept" and pid:
                self.view_before_accept[pid] = (before.get("view") or {}).get("type")
            if action.get("op") == "undo" and pid in self.view_before_accept:
                self.checks["undo_exact"] += 1
                got = (after.get("view") or {}).get("type")
                if got != self.view_before_accept[pid]:
                    self._v("undo_exact", f"proposal undo: view {got!r}, expected {self.view_before_accept[pid]!r}",
                            out)

        # 7. every proposal reason contains a number
        for p in (proposal_after, js.get("proposal")):
            if isinstance(p, dict) and p.get("id") and p["id"] not in self.seen_props:
                self.seen_props.add(p["id"])
                self.checks["proposal_number"] += 1
                if not re.search(r"\d", p.get("reason") or ""):
                    self._v("proposal_number", f"proposal {p.get('title')!r} reason has no number: {p.get('reason')}",
                            out)

        # 8. the view never hides an item without counting it
        view = after.get("view")
        key = json.dumps([view, sorted((i, it.get("start"), it.get("status")) for i, it in a_items.items())],
                         sort_keys=True, default=str)
        if view and key != self.last_view_key:
            self.last_view_key = key
            self.checks["no_hidden_item"] += 1
            now = parse(after.get("now")) or BASE_DAY
            for prob in hidden_problems(view, list(a_items.values()), now)[:1]:
                self._v("no_hidden_item", prob, out)
        return out

    def summary(self) -> dict:
        n = sum(self.checks.values())
        bad = len(self.violations)
        per = {}
        for k, c in self.checks.items():
            v = sum(1 for x in self.violations if x["invariant"] == k)
            if c:
                per[k] = {"checks": c, "violations": v}
        return {"checks": n, "violations": bad, "pass_rate": round(1 - bad / n, 4) if n else 1.0,
                "steps_clean": None, "per_invariant": per}


# ---------------------------------------------------------------- persona -> seed

def seed_items(persona: dict, now: datetime) -> list[dict]:
    items = []
    for k, h in enumerate(persona.get("history", [])):
        st = (now - timedelta(days=int(h["days_ago"]))).replace(hour=int(h["hour"]) % 24,
                                                                  minute=int(h.get("minute", 0)) % 60)
        items.append({"id": f"h{k}", "kind": "task", "title": str(h["title"]), "start": fmt(st),
                      "end": fmt(st + timedelta(minutes=int(h.get("dur_min", 30)))), "status": "done",
                      "source": "seed"})
    for k, o in enumerate(persona.get("open", [])):
        it = {"id": f"o{k}", "kind": o.get("kind") or "task", "title": str(o["title"]), "status": "open",
              "source": "seed", "window": o.get("window"), "start": None, "end": None,
              "estimate_min": o.get("dur_min")}
        if o.get("days_ahead") is not None and o.get("hour") is not None:
            st = (now + timedelta(days=int(o["days_ahead"]))).replace(hour=int(o["hour"]) % 24, minute=0)
            it["start"], it["end"] = fmt(st), fmt(st + timedelta(minutes=int(o.get("dur_min") or 60)))
        items.append(it)
    return items


# ---------------------------------------------------------------- the simulated person

SCHEMA = """Reply with ONE JSON object, nothing else. Allowed actions:
{"action":"command","text":"<what you type, e.g. 'dentist thursday 3pm', 'move X to next week', 'cancel X', 'what's on friday?', 'empty out my weekend for a trip'>"}
{"action":"dump","text":"<a brain dump: several comma-separated fragments>"}
{"action":"chat","item_id":"<id>","text":"<your reply in the conversation about that item, e.g. answering its question, 'yes', 'not now', 'looks good', 'make the first step smaller'>"}
{"action":"done","item_id":"<id>"}
{"action":"clock","advance":"a_bit|few_hours|next_day"}      (let time pass; the app may remind or move things)
{"action":"proposal","op":"accept|dismiss|undo"}               (react to the open view proposal)
{"action":"undo","undo_id":"<undo_id of an agent message>"}    (undo what that message announced)
{"action":"plan","op":"apply|undo","plan_id":"<id>"}           (confirm or revert a previewed change set)
{"action":"import","filename":"<name>.ics","content":"<a small VCALENDAR with 2-4 VEVENTs near the current date>"}
{"action":"view","type":"month|week|workweek|day"}
{"action":"learn"}                                             (ask the app to learn your habits now)
Optional key "why": a few words on your intent."""


def compact_state(st: dict, proposal, plan) -> dict:
    items = sorted(st.get("items", []), key=lambda i: (i.get("status") == "done", i.get("start") or "9999"))
    return {
        "now": st.get("now"),
        "view": (st.get("view") or {}).get("type"),
        "items": [{k: v for k, v in {"id": i.get("id"), "kind": i.get("kind"), "title": i.get("title"),
                                     "start": i.get("start"), "window": i.get("window"), "status": i.get("status"),
                                     "parent_id": i.get("parent_id")}.items() if v is not None}
                  for i in items[:28]],
        "last_messages": [{k: v for k, v in {"role": m.get("role"), "kind": m.get("kind"),
                                             "text": (m.get("text") or "")[:240], "item_id": m.get("item_id"),
                                             "undo_id": m.get("undo_id"),
                                             "steps": m.get("steps")}.items() if v}
                          for m in (st.get("messages") or [])[-6:]],
        "open_proposal": ({"title": proposal.get("title"), "reason": proposal.get("reason"),
                           "status": proposal.get("status")} if isinstance(proposal, dict) else None),
        "last_plan": ({"id": plan.get("id"), "status": plan.get("status"), "origin": plan.get("origin"),
                       "ops": [f"{o.get('op')} {((o.get('before') or o.get('after') or {}).get('title'))}"
                               for o in (plan.get("ops") or [])[:8]]} if isinstance(plan, dict) else None),
    }


def persona_card(p: dict) -> str:
    return (f"You are {p['name']}: {p.get('summary', '')}.\nTraits: " + "; ".join(p.get("traits", [])) +
            "\nHow you react: " + "; ".join(f"{k}: {v}" for k, v in (p.get("reactions") or {}).items()) +
            "\nWhat you want from this session: " + "; ".join(p.get("goals", [])))


def _parse_action(text: str):
    for m in re.finditer(r"\{.*\}", text or "", re.S):
        try:
            a = json.loads(m.group(0))
            if isinstance(a, dict) and a.get("action") in ACTIONS:
                return a
        except Exception:
            pass
    return None


OFFTOPIC_GOALS = ["I want to learn to swim in the next 3 months.", "I'd like to run a 10k by spring.",
                  "Also I want to read 12 books this year.", "I want to learn Spanish in the next 6 months."]


def pick_nudge(rng: random.Random, coverage: dict, view: dict):
    """Seeded hint toward under-used actions, so episodes reach clock/undo/learn/bulk paths. Followed only if it fits."""
    if rng.random() < 0.45:
        return None
    hints = {"clock": "let some time pass (clock, maybe next_day) and see what the app does with your plans",
             "learn": "ask the app to learn your habits (learn)",
             "view": "switch the calendar view",
             "import": "import a small .ics calendar file you write yourself",
             "command": "cancel or move one thing, or ask to clear several things at once (a day, the weekend)",
             "done": "mark something done"}
    if any(m.get("undo_id") for m in view.get("last_messages", [])):
        hints["undo"] = "undo something the app announced (use its undo_id)"
    if view.get("open_proposal") and view["open_proposal"].get("status") == "shown":
        hints["proposal"] = "react to the open proposal the way you would"
    if (view.get("last_plan") or {}).get("status") in ("preview", "applied"):
        hints["plan"] = "apply the previewed plan, or undo an applied one"
    open_q = [m for m in view.get("last_messages", []) if m.get("kind") == "question" and m.get("item_id")
              and OFFER.search(m.get("text") or "")]
    if open_q:
        hints["offtopic"] = (f"ignore the app's question about item {open_q[-1]['item_id']}: in that same conversation "
                             "(chat on that item_id) type a sentence about a different, new goal with a time span, "
                             "e.g. 'I want to learn to swim in the next 3 months.'")
    keys = sorted(hints, key=lambda k: (coverage.get(k, {}).get("tried", 0), k))
    w = [3 if coverage.get(k, {}).get("tried", 0) == 0 else 1 for k in keys]
    if "clock" in keys:
        w[keys.index("clock")] += 2
    if "offtopic" in keys:
        w[keys.index("offtopic")] += 3
    return hints[rng.choices(keys, w)[0]]


def choose_action(model: str, persona: dict, view: dict, history: list, step: int, max_steps: int, usage: dict,
                  nudge: str | None = None):
    from sea import llm
    sys_msg = (persona_card(persona) + "\n\nYou are using a conversational calendar app. Act like this person "
               "would, in your own words and style. Over the session try different things the person would really do "
               "(typing, brain dumps, replying to the app's questions, finishing tasks, letting time pass, reacting "
               "to proposals and previews, undoing things you dislike, importing a calendar file). Do not repeat the "
               "same action type three times in a row. Only use ids and undo_ids that appear in the state.\n\n"
               + SCHEMA)
    user = (f"Step {step + 1} of {max_steps}. Your recent actions and what came back:\n" +
            ("\n".join(history[-5:]) or "(none yet)") +
            "\n\nApp state now:\n" + json.dumps(view, ensure_ascii=False) +
            (f"\n\nIdea for this step (only if it fits you): {nudge}." if nudge else "") +
            "\n\nYour next action (JSON only):")
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]
    for attempt in range(2):
        try:
            text, u = llm.chat(model, msgs, max_tokens=3000, temperature=0.0, timeout=120)
        except Exception as e:
            usage["errors"] = usage.get("errors", 0) + 1
            text, u = "", {}
        llm.add_usage(usage, u)
        a = _parse_action(text)
        if a:
            return a
        msgs.append({"role": "assistant", "content": text[:500]})
        msgs.append({"role": "user", "content": "That was not one valid JSON action. Reply with JSON only."})
    return None


# ---------------------------------------------------------------- one episode

def execute(base: str, a: dict, st: dict, proposal, plan, rng: random.Random, persona: dict) -> tuple[dict, dict]:
    """Run one action; returns (response, normalised action actually sent)."""
    kind = a.get("action")
    if kind in ("command", "dump"):
        return http(base, "POST", "/api/command", {"text": str(a.get("text", ""))[:500],
                                                   "mode": "dump" if kind == "dump" else "command"}), a
    if kind == "chat":
        return http(base, "POST", "/api/chat", {"item_id": a.get("item_id"), "text": str(a.get("text", ""))}), a
    if kind == "done":
        return http(base, "POST", f"/api/items/{a.get('item_id')}/done", {}), a
    if kind == "clock":
        now = parse(st.get("now")) or BASE_DAY
        adv = a.get("advance")
        if adv == "next_day":
            nxt = (now + timedelta(days=1)).replace(hour=int(persona.get("active_hour", 9)) % 24,
                                                    minute=rng.choice([0, 15, 30, 45]))
            if nxt <= now:
                nxt = now + timedelta(hours=12)
        elif adv == "few_hours":
            nxt = now + timedelta(minutes=rng.randint(120, 300))
        else:
            nxt = now + timedelta(minutes=rng.randint(15, 60))
        a = {**a, "now": fmt(nxt)}
        return http(base, "POST", "/api/clock", {"now": fmt(nxt)}), a
    if kind == "proposal":
        pid = (proposal or {}).get("id")
        if not pid:
            return {"status": None, "secs": 0, "json": None, "error": "no open proposal", "skipped": True}, a
        return http(base, "POST", f"/api/proposals/{pid}/{a.get('op', 'dismiss')}", {}), {**a, "proposal_id": pid}
    if kind == "undo":
        return http(base, "POST", "/api/undo", {"undo_id": a.get("undo_id")}), a
    if kind == "plan":
        pid = a.get("plan_id") or (plan or {}).get("id")
        op = a.get("op") if a.get("op") in ("apply", "undo") else "apply"
        return http(base, "POST", f"/api/plan/{pid}/{op}", {}), {**a, "plan_id": pid, "op": op}
    if kind == "import":
        return http(base, "POST", "/api/import", {"filename": a.get("filename") or "cal.ics",
                                                  "content": str(a.get("content", ""))[:6000]}), a
    if kind == "view":
        return http(base, "POST", "/api/view", {"type": a.get("type", "week")}), a
    if kind == "learn":
        return http(base, "POST", "/api/learn", {}), a
    return {"status": None, "secs": 0, "json": None, "error": "unknown action", "skipped": True}, a


def _result_note(resp: dict) -> str:
    js = resp.get("json") if isinstance(resp.get("json"), dict) else {}
    if resp.get("skipped"):
        return f"not possible ({resp.get('error')})"
    bits = [f"HTTP {resp.get('status')}"]
    for m in (js.get("messages") or [])[:3]:
        if isinstance(m, dict):
            bits.append(f"[{m.get('kind')}] {(m.get('text') or '')[:120]}")
    if isinstance(js.get("plan"), dict):
        bits.append(f"plan {js['plan'].get('id')} {js['plan'].get('status')} ({len(js['plan'].get('ops') or [])} ops)")
    if js.get("error"):
        bits.append(f"error: {str(js['error'])[:100]}")
    return " | ".join(bits)


def run_episode(base_url: str, persona: dict, seed: int, model: str, max_steps: int = 14) -> dict:
    t0 = time.time()
    rng = random.Random(seed)
    now = BASE_DAY.replace(hour=int(persona.get("active_hour", 9)) % 24, minute=rng.choice([0, 10, 20, 30]))
    items = seed_items(persona, now)
    usage: dict = {}
    reset = http(base_url, "POST", "/api/reset", {"now": fmt(now), "items": items})
    checker = Checker(seed_ids=[i["id"] for i in items])
    coverage = {k: {"tried": 0, "accepted": 0} for k in ACTIONS}
    features = {}
    steps, history = [], []
    plan = None
    probed = False
    if reset.get("status") != 200:
        checker.checks["http"] += 1
        checker._v("http", f"reset failed: {reset.get('status')} {reset.get('error')}", [])
    st = http(base_url, "GET", "/api/state")["json"] or {}
    proposal = (http(base_url, "GET", "/api/proposal").get("json") or {}).get("proposal")
    checker.seen_msgs.update(m.get("id") for m in st.get("messages", []) if isinstance(m, dict))
    for step in range(max_steps):
        cview = compact_state(st, proposal, plan)
        offer = next((m for m in reversed(cview["last_messages"]) if m.get("kind") == "question" and m.get("item_id")
                      and OFFER.search(m.get("text") or "")), None)
        if offer and not probed and rng.random() < 0.7:
            # probe (the UI routes chat-box text into the open conversation): a new goal typed while the app waits
            # for a yes/no on another item. Contract 3: this is not a yes.
            probed = True
            a = {"action": "chat", "item_id": offer["item_id"], "text": rng.choice(OFFTOPIC_GOALS), "probe": True}
        else:
            a = choose_action(model, persona, cview, history, step, max_steps, usage,
                              pick_nudge(rng, coverage, cview))
        if a is None:
            a = {"action": "clock", "advance": "a_bit", "why": "(simulator fallback: no valid action)"}
        resp, a = execute(base_url, a, st, proposal, plan, rng, persona)
        kind = a.get("action")
        coverage[kind]["tried"] += 1
        if resp.get("skipped"):
            history.append(f"{json.dumps(a)[:200]} -> {_result_note(resp)}")
            steps.append({"action": a, "status": None, "skipped": True})
            continue
        ok = resp.get("status") is not None and 200 <= resp["status"] < 300
        coverage[kind]["accepted"] += int(ok)
        new_st = http(base_url, "GET", "/api/state").get("json") or st
        prop_r = http(base_url, "GET", "/api/proposal").get("json") or {}
        new_prop = prop_r.get("proposal") if isinstance(prop_r, dict) else None
        viol = checker.step(a, st, new_st, resp, new_prop)
        js = resp.get("json") if isinstance(resp.get("json"), dict) else {}
        if isinstance(js.get("plan"), dict):
            plan = js["plan"]
        for m in (js.get("messages") or []):
            if isinstance(m, dict) and m.get("role", "agent") == "agent" and m.get("kind"):
                features[m["kind"]] = features.get(m["kind"], 0) + 1
        if plan and plan.get("status") == "preview":
            features["plan_preview"] = features.get("plan_preview", 0) + int(isinstance(js.get("plan"), dict))
        if new_prop and (not proposal or new_prop.get("id") != proposal.get("id")):
            features["proposal"] = features.get("proposal", 0) + 1
        st, proposal = new_st, new_prop
        history.append(f"{json.dumps({k: v for k, v in a.items() if k != 'content'}, ensure_ascii=False)[:220]}"
                       f" -> {_result_note(resp)}")
        steps.append({"action": {k: (v[:300] if isinstance(v, str) else v) for k, v in a.items()},
                      "status": resp.get("status"), "secs": resp.get("secs"),
                      "result": _result_note(resp)[:400], "violations": viol})
    s = checker.summary()
    s["steps_clean"] = round(sum(1 for x in steps if not x.get("violations")) / max(1, len(steps)), 3)
    return {"seed": seed, "persona": persona["name"], "model": model, "start_now": fmt(now),
            **s, "violation_list": checker.violations, "coverage": coverage, "features": features,
            "usage": usage, "secs": round(time.time() - t0, 1), "steps": steps}
