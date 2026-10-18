"""Frozen harness for the Evolving Calendar v2 (Iris build: v1 behaviours 1-9 + learning loop 10-21).

Starts a fresh app process per scenario (`python app.py --port P --data DIR`, cwd = app_dir), drives it over
HTTP the way a person would (see contract.md), and scores the fraction of checks that pass.

    python harness.py <app_dir> [fit|holdout] [--workers 3] [--timeout 60] [--json]

Leniency rules (agent text comes from language models, so meaning is checked, not wording):
- Conversation driver: answers the agent's latest message by keyword (why/matter -> reason, done/finish ->
  definition of done, when/deadline -> date; any other question -> all three at once), max 6 turns.
- Verb-first step title: the first word is in VERBS (~200 English imperatives), OR it is a plain word that is
  not a determiner / pronoun / number / preposition / "-ing" form (NON_VERB_START) and the title has <= 12 words.
- No guilt: GUILT_RE (word boundary, case-insensitive) over every agent message text and step title;
  "later" is fine, "late" is not.
- A check that could not run (HTTP error, bad JSON, app died) counts as failed, so every scenario always has
  the same number of checks and the score is comparable across apps.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEALTH_TIMEOUT = 15.0
MAX_DRIVER_TURNS = 6


def _load_scenarios():
    spec = importlib.util.spec_from_file_location("evolving_calendar_v2_scenarios", HERE / "scenarios.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCENARIOS = _load_scenarios()

# ---------------------------------------------------------------- text rules

GUILT_RE = re.compile(r"\b(overdue|failed|missed|late|behind|forgot|forgotten|lazy)\b|should\s+have|should've",
                      re.I)
SMALLER_RE = re.compile(r"smaller|steps?\b|break|split|chunk|piece|bite", re.I)
WHY_RE = re.compile(r"\bwhy\b|matter|important|mean to you|meaningful|reason|motivat|care about|purpose", re.I)
DONE_RE = re.compile(r"\bdone\b|finish|look like|complete|success|know (you|it)", re.I)
WHEN_RE = re.compile(r"\bwhen\b|deadline|date|by when|\bdue\b|timeline|how soon|time ?frame", re.I)
MSG_KINDS = {"reply", "question", "reminder", "postponed", "checkin", "breakdown", "info"}

VERBS = set("""
add answer apply arrange ask assemble attach back bake batch begin book bring browse buy calculate call cancel
capture change check choose clean clear close collect combine compare complete compose confirm contact copy count
create cut decide declutter define delete deliver describe design devise do download draft draw drink drop
eat edit email empty enter estimate explore export file fill find finish fix fold follow format gather get give go
grab group handle hang hire identify import install invite iron jot join jog keep label lay learn leave list load
locate look mail make map mark measure meet message move name note notify open order organize organise outline
pack paint pay phone pick pin place plan polish post prep prepare present price print prioritize prioritise
publish pull purchase put read rearrange record refine register remove rename renew repair replace reply request
research reserve review revise rewrite run save scan schedule search select sell send set share shop shortlist
sign sketch skim sort spend split start stop store stretch submit summarize summarise switch take talk test text
throw tidy time track transfer try turn type unpack update upload use vacuum verify visit walk wash watch water
wipe work write zip study practice practise brainstorm block confirm crop export gather jot pair rank tag trim
upload walk fetch ring wrap toss discard donate recycle photograph shoot snap film plug charge decide settle
""".split())

NON_VERB_START = set("""
a an the this that these those my your his her its our their some any each every all no none both either neither
i you he she it we they me him us them myself yourself first second third fourth fifth last next one two three
four five six seven eight nine ten step steps to of in on at for by with from about into maybe and or but if
then so because what which who how when where why is are was were be been am
""".split())


def is_verb_first(title: str) -> bool:
    words = re.findall(r"[A-Za-z']+|\d+", title or "")
    if not words:
        return False
    w = words[0].lower()
    if w in VERBS:
        return True
    if w.isdigit() or w in NON_VERB_START or w.endswith("ing"):
        return False
    return len(words) <= 12


def guilty(text: str) -> str | None:
    m = GUILT_RE.search(text or "")
    return m.group(0) if m else None


def dt(s) -> datetime | None:
    if not isinstance(s, str) or len(s) < 16:
        return None
    try:
        return datetime.fromisoformat(s[:16])
    except ValueError:
        return None


def fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def short(x, n=160) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------- HTTP client + checks


class AppError(Exception):
    pass


class Client:
    def __init__(self, base: str, timeout: float, dead: bool = False):
        self.base, self.timeout, self.dead = base, timeout, dead

    def call(self, method: str, path: str, body=None, raw: bool = False):
        if self.dead:
            raise AppError("app not running")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            body_txt = e.read()[:200].decode("utf-8", "replace")
            raise AppError(f"{method} {path} -> HTTP {e.code}: {body_txt}") from None
        except (urllib.error.URLError, OSError) as e:
            raise AppError(f"{method} {path} -> {type(e).__name__}: {e}") from None
        text = payload.decode("utf-8", "replace")
        if raw:
            return text
        try:
            return json.loads(text)
        except ValueError:
            raise AppError(f"{method} {path} -> non-JSON: {short(text, 120)}") from None


class Ctx:
    def __init__(self, client: Client):
        self.client = client
        self.prefix = ""
        self.planned: list[str] = []
        self.results: dict[str, tuple[bool, str]] = {}
        self.vars: dict[str, str] = {}

    # checks
    def plan(self, names):
        for n in names:
            full = f"{self.prefix}:{n}"
            if full not in self.planned:
                self.planned.append(full)

    def check(self, name, ok, detail=""):
        full = f"{self.prefix}:{name}"
        if full not in self.planned:
            self.planned.append(full)
        if full not in self.results:
            self.results[full] = (bool(ok), short(str(detail), 300))

    def fail_rest(self, detail):
        for n in self.planned:
            if n not in self.results:
                self.results[n] = (False, short(detail, 300))

    # http
    def get(self, path):
        return self.client.call("GET", path)

    def post(self, path, body):
        r = self.client.call("POST", path, body)
        if not isinstance(r, dict):
            raise AppError(f"POST {path} -> not a JSON object: {short(r)}")
        return r

    def state(self) -> dict:
        s = self.get("/api/state")
        if not (isinstance(s, dict) and isinstance(s.get("items"), list) and isinstance(s.get("messages"), list)):
            raise AppError(f"GET /api/state bad shape: {short(s)}")
        s["items"] = [i for i in s["items"] if isinstance(i, dict)]
        s["messages"] = [m for m in s["messages"] if isinstance(m, dict)]
        return s

    def clock(self, now: str) -> list:
        r = self.post("/api/clock", {"now": now})
        return [m for m in (r.get("messages") or []) if isinstance(m, dict)]


def agent(msgs) -> list[dict]:
    return [m for m in (msgs or []) if isinstance(m, dict) and m.get("role", "agent") == "agent"]


def text_of(m: dict) -> str:
    return str(m.get("text") or "")


def item_map(s: dict) -> dict:
    return {i.get("id"): i for i in s["items"]}


def msgs_for(s: dict, item_id: str, kind: str) -> list[dict]:
    return [m for m in agent(s["messages"]) if m.get("kind") == kind and m.get("item_id") == item_id]


# ---------------------------------------------------------------- ops


def op_reset(ctx: Ctx, st: dict):
    ctx.plan(["reset_ok"])
    ctx.post("/api/reset", {"now": st["now"], "items": st.get("items", [])})
    ctx.vars["_t0"] = st["now"]
    ctx.vars["_seed_ids"] = [i["id"] for i in st.get("items", [])]
    s = ctx.state()
    ids = set(item_map(s))
    missing = sorted({i["id"] for i in st.get("items", [])} - ids)
    ctx.check("reset_ok", str(s.get("now", ""))[:16] == st["now"] and not missing,
              f"now={s.get('now')} missing_ids={missing[:5]}")


def op_ui(ctx: Ctx, st: dict):
    ctx.plan(["html_ids"])
    html = ctx.client.call("GET", "/", raw=True)
    missing = [i for i in st.get("ids", UI_IDS)
               if not re.search(r"""id\s*=\s*["']?%s["'\s>]""" % re.escape(i), html)]
    ctx.check("html_ids", not missing, f"missing ids: {missing}")


UI_IDS = ("chat-input", "chat-log", "focus", "timeline", "learned", "activity", "proposal", "view-switcher")


def op_command(ctx: Ctx, st: dict):
    exp = st.get("expect", {})
    names = ["responds"]
    names += ["n_created"] if "n_created" in exp else []
    names += ["min_created"] if "min_created" in exp else []
    names += ["no_fragment_lost"] if "fragments" in exp else []
    names += ["event_time"] if "event" in exp else []
    names += ["window_kept"] if "window" in exp else []
    names += ["at_most_one_question"] if "max_questions" in exp else []
    names += ["one_complexity_question", "nothing_broken_down"] if "complexity" in exp else []
    ctx.plan(names)

    r = ctx.post("/api/command", {"text": st["text"], "mode": st.get("mode", "command")})
    msgs, created = r.get("messages"), r.get("created")
    ok = isinstance(msgs, list) and isinstance(created, list) and bool(agent(msgs))
    ctx.check("responds", ok, f"response={short(r)}")
    msgs = agent(msgs if isinstance(msgs, list) else [])
    created = created if isinstance(created, list) else []
    s = ctx.state()
    imap = item_map(s)
    cr = [imap[c] for c in created if c in imap]
    titles = [str(i.get("title") or "") for i in cr]
    if st.get("as") and cr and "complexity" not in exp:
        hit = [i for i in cr if st.get("as_kw", "").lower() in str(i.get("title") or "").lower()]
        ctx.vars[st["as"]] = (hit or cr)[0].get("id")

    def find(kw):
        for i in cr:
            if kw.lower() in str(i.get("title") or "").lower():
                return i
        return None

    if "n_created" in exp:
        ctx.check("n_created", len(created) == exp["n_created"], f"created {len(created)}: {titles}")
    if "min_created" in exp:
        ctx.check("min_created", len(cr) >= exp["min_created"], f"created {len(cr)}: {titles}")
    if "fragments" in exp:
        lost = [kw for kw in exp["fragments"] if not find(kw)]
        ctx.check("no_fragment_lost", not lost, f"lost {lost}; titles={titles}")
    if "event" in exp:
        e = exp["event"]
        it = find(e["kw"])
        ok = bool(it) and it.get("kind") == "event" and str(it.get("start") or "")[:16] == e["start"]
        ctx.check("event_time", ok, f"want event at {e['start']}, saw "
                  + (short({k: it.get(k) for k in ('title', 'kind', 'start')}) if it else f"no '{e['kw']}' item"))
    if "window" in exp:
        w = exp["window"]
        it = find(w["kw"])
        ok, seen = False, f"no '{w['kw']}' item in {titles}"
        if it:
            seen = short({k: it.get(k) for k in ("title", "window", "start")})
            start = dt(it.get("start"))
            ok = bool(str(it.get("window") or "").strip()) and (
                start is None or (start.date().isoformat() == w["date"] and w["hours"][0] <= start.hour <= w["hours"][1]))
        ctx.check("window_kept", ok, f"want window set (start ~{w['date']} {w['hours']}h), saw {seen}")
    if "max_questions" in exp:
        frags = [f.strip().lower() for f in st["text"].split(",") if "?" in f]
        nq = sum(1 for m in msgs if m.get("kind") == "question")
        qm = 0
        for m in msgs:
            t = text_of(m).lower()
            for f in frags:
                t = t.replace(f, "")
            qm += t.count("?")
        ctx.check("at_most_one_question", nq <= exp["max_questions"] and qm <= exp["max_questions"],
                  f"question msgs={nq}, '?' count={qm}: {short([text_of(m) for m in msgs], 240)}")
    if "complexity" in exp:
        goal = find(exp["complexity"]["kw"]) or (cr[0] if cr else None)
        if goal is None:
            raise AppError(f"no item created for '{st['text']}'")
        gid = goal.get("id")
        if st.get("as"):
            ctx.vars[st["as"]] = gid
        qs = msgs_for(s, gid, "question")
        ok = len(qs) == 1 and "?" in text_of(qs[0]) and bool(SMALLER_RE.search(text_of(qs[0])))
        ctx.check("one_complexity_question", ok,
                  f"{len(qs)} question msgs on item: {short([text_of(m) for m in qs], 200)}; all={short([(m.get('kind'), text_of(m)) for m in msgs], 200)}")
        kids = [i for i in s["items"] if i.get("parent_id") == gid]
        bds = msgs_for(s, gid, "breakdown")
        ctx.check("nothing_broken_down", not kids and not bds, f"children={len(kids)} breakdown_msgs={len(bds)}")


def op_decline(ctx: Ctx, st: dict):
    ctx.plan(["no_breakdown_without_yes"])
    gid = ctx.vars.get(st["goal"])
    if not gid:
        raise AppError("goal item unknown")
    ctx.post("/api/chat", {"item_id": gid, "text": st["text"]})
    s = ctx.state()
    kids = [i for i in s["items"] if i.get("parent_id") == gid]
    bds = msgs_for(s, gid, "breakdown")
    ctx.check("no_breakdown_without_yes", not kids and not bds, f"children={len(kids)} breakdown_msgs={len(bds)}")


def _answer(text: str, st: dict) -> str:
    if WHY_RE.search(text):
        return st["reason"]
    if DONE_RE.search(text):
        return st["done"]
    if WHEN_RE.search(text):
        return st["deadline_text"]
    if "?" in text:
        return f"{st['reason']} Done would mean: {st['done']} {st['deadline_text']}"
    return "okay, sounds good"


def _breakdown_in(msgs):
    for m in msgs:
        if m.get("kind") == "breakdown" and isinstance(m.get("steps"), list) and m["steps"]:
            return m
    return None


def op_breakdown(ctx: Ctx, st: dict):
    names = ["why_first", "le3_questions", "one_q_per_message", "got_breakdown", "steps_2_to_5", "verb_first",
             "first_step_le15", "steps_le60"]
    names += ["revised_breakdown"] if st.get("revise") else []
    names += ["children_created", "children_before_deadline", "goal_why"]
    names += ["focus_after_accept"] if st.get("focus") else []
    ctx.plan(names)
    gid = ctx.vars.get(st["goal"])
    if not gid:
        raise AppError("goal item unknown")

    chat_msgs, bd, reply, transcript = [], None, "yes, please", []
    for _ in range(MAX_DRIVER_TURNS):
        r = ctx.post("/api/chat", {"item_id": gid, "text": reply})
        am = agent(r.get("messages"))
        transcript.append(f"P: {reply} | A: " + " / ".join(f"[{m.get('kind')}] {text_of(m)}" for m in am))
        bd = _breakdown_in(am)
        chat_msgs += [m for m in am if m is not bd]
        if bd:
            break
        reply = _answer(text_of(am[-1]) if am else "", st)
    tr = short(" || ".join(transcript), 400)

    first = chat_msgs[0] if chat_msgs else None
    ctx.check("why_first", first is not None and "?" in text_of(first) and bool(WHY_RE.search(text_of(first))),
              f"first agent msg after yes: {short(text_of(first)) if first else None}")
    qs = [m for m in chat_msgs if m.get("kind") == "question" or "?" in text_of(m)]
    ctx.check("le3_questions", bd is not None and len(qs) <= 3, f"{len(qs)} questions before breakdown; {tr}")
    multi = [text_of(m) for m in chat_msgs + ([bd] if bd else []) if text_of(m).count("?") > 1]
    ctx.check("one_q_per_message", bool(chat_msgs or bd) and not multi, f"messages with >1 '?': {short(multi, 200)}")
    ctx.check("got_breakdown", bd is not None, f"no breakdown in {MAX_DRIVER_TURNS} turns; {tr}")
    if bd is None:
        return
    steps = [x for x in bd["steps"] if isinstance(x, dict)]
    titles = [str(x.get("title") or "") for x in steps]
    ests = [x.get("estimate_min") for x in steps]
    ctx.check("steps_2_to_5", 2 <= len(steps) <= 5, f"{len(steps)} steps: {titles}")
    bad = [t for t in titles if not is_verb_first(t)]
    ctx.check("verb_first", bool(titles) and not bad, f"not verb-first: {bad}")
    ctx.check("first_step_le15", bool(ests) and isinstance(ests[0], (int, float)) and 0 < ests[0] <= 15,
              f"first step {titles[:1]} estimate={ests[:1]}")
    ctx.check("steps_le60", bool(ests) and all(isinstance(e, (int, float)) and 0 < e <= 60 for e in ests),
              f"estimates={ests}")

    if st.get("revise"):
        r = ctx.post("/api/chat", {"item_id": gid, "text": "Could the first step be even smaller, like 5 minutes?"})
        bd2 = _breakdown_in(agent(r.get("messages")))
        ok = bd2 is not None and 2 <= len(bd2["steps"]) <= 5
        ctx.check("revised_breakdown", ok,
                  f"reply: {short([(m.get('kind'), text_of(m)) for m in agent(r.get('messages'))], 200)}")

    ctx.post("/api/chat", {"item_id": gid, "text": "looks good, yes"})
    s = ctx.state()
    kids = [i for i in s["items"] if i.get("parent_id") == gid]
    ctx.check("children_created", len(kids) >= 2, f"{len(kids)} child items")
    late = [(k.get("title"), k.get("start"), k.get("window")) for k in kids
            if not (k.get("start") or k.get("window"))
            or (dt(k.get("start")) and dt(k.get("start")).date().isoformat() > st["deadline"])]
    ctx.check("children_before_deadline", bool(kids) and not late, f"deadline {st['deadline']}; bad: {short(late, 200)}")
    goal = item_map(s).get(gid) or {}
    why = str(goal.get("why") or "")
    ctx.check("goal_why", any(k in why.lower() for k in st["reason_kws"]), f"goal.why={short(why)}")
    if st.get("focus"):
        f = ctx.get("/api/focus")
        it = (f or {}).get("item") or {}
        par = (f or {}).get("parent") or {}
        ok = it.get("parent_id") == gid and par.get("id") == gid and bool(f.get("why")) \
            and f.get("action") == "Start 5 minutes"
        ctx.check("focus_after_accept", ok, f"focus={short(f, 240)}")


def op_focus(ctx: Ctx, st: dict):
    ctx.plan(["focus_item", "focus_parent_why", "focus_action"])
    f = ctx.get("/api/focus")
    if not isinstance(f, dict):
        raise AppError(f"/api/focus not an object: {short(f)}")
    it = f.get("item") or {}
    par = f.get("parent") or {}
    ctx.check("focus_item", it.get("id") == st["expect_item"],
              f"want {st['expect_item']}, saw {short({k: it.get(k) for k in ('id', 'title')})}")
    why = str(f.get("why") or "")
    ctx.check("focus_parent_why", par.get("id") == st["expect_parent"] and any(k in why.lower() for k in st["why_kws"]),
              f"parent={par.get('id')} why={short(why)}")
    ctx.check("focus_action", f.get("action") == "Start 5 minutes", f"action={f.get('action')!r}")


def op_done(ctx: Ctx, st: dict):
    ctx.plan(["done_ok"])
    r = ctx.post(f"/api/items/{st['item']}/done", {})
    it = r.get("item") or {}
    ctx.check("done_ok", it.get("id") == st["item"] and it.get("status") == "done", f"item={short(it)}")


def op_reminders(ctx: Ctx, st: dict):
    ctx.plan(["no_early_reminder", "reminder_at_start", "no_duplicate", "reminder_10_before_end",
              "no_extra_after_end", "reminder_names_task", "done_item_quiet"])
    iid, start, end = st["item"], dt(st["start"]), dt(st["end"])

    def count():
        s = ctx.state()
        return msgs_for(s, iid, "reminder"), s

    walk = [("no_early_reminder", start - timedelta(minutes=1), 0),
            ("reminder_at_start", start + timedelta(minutes=1), 1),
            ("no_duplicate", end - timedelta(minutes=11), 1),
            ("reminder_10_before_end", end - timedelta(minutes=9), 2),
            ("no_extra_after_end", end + timedelta(minutes=20), 2)]
    rems, s = [], None
    for name, t, want in walk:
        ctx.clock(fmt(t))
        rems, s = count()
        ctx.check(name, len(rems) == want, f"at {fmt(t)}: {len(rems)} reminders (want {want})")
    unnamed = [text_of(m) for m in rems if st["kw"].lower() not in text_of(m).lower()]
    ctx.check("reminder_names_task", bool(rems) and not unnamed, f"reminders not naming '{st['kw']}': {short(unnamed)}")
    if st.get("done_item"):
        n = len(msgs_for(s, st["done_item"], "reminder"))
        ctx.check("done_item_quiet", n == 0, f"{n} reminders for done item")
    else:
        ctx.check("done_item_quiet", True)


def _time_mentioned(text: str, d: datetime) -> bool:
    h12 = d.hour % 12 or 12
    pats = [rf"\b{d.hour}:{d.minute:02d}\b", rf"\b{h12}(:{d.minute:02d})?\s*([ap]\.?m\.?)\b", rf"\b{d.hour}h"]
    if d.minute == 0:
        pats.append(rf"\b{h12}\s*o'?clock")
    pats.append(rf"\b{h12}:{d.minute:02d}\b")
    return any(re.search(p, text, re.I) for p in pats)


def op_postpone(ctx: Ctx, st: dict):
    ctx.plan(["no_early_postpone", "postponed_message", "next_day", "evening_slot", "same_length", "free_slot",
              "postpone_count_1", "names_task_and_time", "has_undo_id"] + (["undo_restores"] if st.get("undo") else []))
    iid = st["item"]
    s0 = ctx.state()
    orig = item_map(s0).get(iid)
    if not orig:
        raise AppError(f"seed item {iid} missing")
    o_start, o_end = dt(orig.get("start")), dt(orig.get("end"))

    ctx.clock(st["quiet_until"])
    s = ctx.state()
    it = item_map(s).get(iid) or {}
    pm = msgs_for(s, iid, "postponed")
    ctx.check("no_early_postpone", not pm and dt(it.get("start")) == o_start,
              f"at {st['quiet_until']}: {len(pm)} postponed msgs, start={it.get('start')}")

    ctx.clock(st["after"])
    s = ctx.state()
    it = item_map(s).get(iid) or {}
    pm = msgs_for(s, iid, "postponed")
    ctx.check("postponed_message", len(pm) == 1, f"{len(pm)} postponed msgs for item")
    ns, ne = dt(it.get("start")), dt(it.get("end"))
    ctx.check("next_day", ns is not None and ns.date().isoformat() == st["next_day"], f"new start={it.get('start')}")
    ctx.check("evening_slot", ns is not None and ns.hour >= st["min_hour"],
              f"new start={it.get('start')} (want hour >= {st['min_hour']})")
    ctx.check("same_length", ns and ne and o_end and (ne - ns) == (o_end - o_start),
              f"old {orig.get('start')}-{orig.get('end')} new {it.get('start')}-{it.get('end')}")
    clash = []
    if ns and ne:
        for o in s["items"]:
            a, b = dt(o.get("start")), dt(o.get("end"))
            if o.get("id") != iid and o.get("status") != "done" and a and b and a < ne and ns < b:
                clash.append((o.get("title"), o.get("start"), o.get("end")))
    ctx.check("free_slot", bool(ns and ne) and not clash, f"overlaps {clash}")
    ctx.check("postpone_count_1", it.get("postpone_count") == 1, f"postpone_count={it.get('postpone_count')}")
    m = pm[-1] if pm else {}
    t = text_of(m)
    ok = bool(pm) and st["kw"].lower() in t.lower() and ns is not None and _time_mentioned(t, ns)
    ctx.check("names_task_and_time", ok, f"want '{st['kw']}' and {it.get('start')}: {short(t)}")
    ctx.check("has_undo_id", bool(m.get("undo_id")), f"undo_id={m.get('undo_id')!r}")
    if st.get("undo"):
        if not m.get("undo_id"):
            raise AppError("no undo_id to undo")
        ctx.post("/api/undo", {"undo_id": m["undo_id"]})
        it = item_map(ctx.state()).get(iid) or {}
        ctx.check("undo_restores", dt(it.get("start")) == o_start and dt(it.get("end")) == o_end,
                  f"after undo start={it.get('start')} end={it.get('end')} (orig {orig.get('start')})")


def op_checkin(ctx: Ctx, st: dict):
    ctx.plan(["postponed_1", "postponed_2", "checkin_on_third", "checkin_offers_smaller", "too_big_breakdown"])
    iid = st["item"]
    checkins = []
    for k in (1, 2, 3):
        s = ctx.state()
        it = item_map(s).get(iid)
        if not it:
            raise AppError(f"item {iid} gone after {k - 1} postponements")
        end, now = dt(it.get("end")), dt(s.get("now"))
        if end is None or now is None:
            raise AppError(f"item end/now unreadable: end={it.get('end')} now={s.get('now')}")
        target = max(end + timedelta(minutes=35), now + timedelta(minutes=1))
        ctx.clock(fmt(target))
        s = ctx.state()
        pm = msgs_for(s, iid, "postponed")
        checkins = msgs_for(s, iid, "checkin")
        it2 = item_map(s).get(iid) or {}
        seen = f"at {fmt(target)}: {len(pm)} postponed, {len(checkins)} checkins, start={it2.get('start')}"
        if k < 3:
            ctx.check(f"postponed_{k}", len(pm) == k and not checkins, seen)
        else:
            ctx.check("checkin_on_third", bool(checkins) and len(pm) in (2, 3), seen)
    t = text_of(checkins[-1]) if checkins else ""
    ctx.check("checkin_offers_smaller", "?" in t and bool(SMALLER_RE.search(t)),
              f"checkin: {short(t)}")
    r = ctx.post("/api/chat", {"item_id": iid, "text": st["reply"]})
    am = agent(r.get("messages"))
    ok = any(m.get("kind") in ("breakdown", "question") or "?" in text_of(m) for m in am)
    ctx.check("too_big_breakdown", ok, f"reply: {short([(m.get('kind'), text_of(m)) for m in am], 200)}")


def op_final(ctx: Ctx, st: dict):
    ctx.plan(["no_guilt", "messages_well_formed"])
    s = ctx.state()
    am = agent(s["messages"])
    bad = []
    for m in am:
        texts = [text_of(m)] + [str(x.get("title") or "") for x in (m.get("steps") or []) if isinstance(x, dict)]
        for t in texts:
            g = guilty(t)
            if g:
                bad.append(f"'{g}' in: {short(t, 100)}")
    ctx.check("no_guilt", bool(am) and not bad, "no agent messages" if not am else "; ".join(bad[:3]))
    malformed = [short(m, 100) for m in s["messages"]
                 if not (m.get("id") and m.get("role") in ("agent", "person") and m.get("kind") in MSG_KINDS
                         and isinstance(m.get("text"), str))]
    ctx.check("messages_well_formed", bool(s["messages"]) and not malformed,
              "no messages" if not s["messages"] else f"{len(malformed)} malformed, e.g. {malformed[:2]}")


# ---------------------------------------------------------------- v2 ops (behaviours 10-20)

ITEM_FIELDS = ("kind", "title", "start", "end", "status", "window", "parent_id")


def _snap(it: dict) -> tuple:
    return tuple((k, str(it.get(k) or "")[:16] if k in ("start", "end") else it.get(k)) for k in ITEM_FIELDS)


def _snapshot(s: dict) -> dict:
    return {i.get("id"): _snap(i) for i in s["items"]}


def _diff_snap(a: dict, b: dict) -> list:
    out = [f"gone {k}" for k in a if k not in b] + [f"new {k}" for k in b if k not in a]
    out += [f"changed {k}" for k in a if k in b and a[k] != b[k]]
    return out


def _ref(ctx: Ctx, x):
    if isinstance(x, str) and x.startswith("$"):
        v = ctx.vars.get(x[1:])
        if not v:
            raise AppError(f"unknown reference {x}")
        return v
    return x


def _plan_of(r) -> dict:
    p = (r or {}).get("plan")
    return p if isinstance(p, dict) else {}


def _ops(plan: dict, kind: str) -> list:
    return [o for o in (plan.get("ops") or []) if isinstance(o, dict) and o.get("op") == kind]


def _apply(ctx: Ctx, plan: dict, action: str = "apply") -> dict:
    if not plan.get("id"):
        raise AppError("no plan id to " + action)
    return _plan_of(ctx.post(f"/api/plan/{plan['id']}/{action}", {}))


def _has_kw(it: dict, kws) -> bool:
    t = str(it.get("title") or "").lower()
    return any(k.lower() in t for k in ([kws] if isinstance(kws, str) else kws))


def op_log(ctx: Ctx, st: dict):
    exp = st["expect"]
    names = []
    for e in exp:
        n = "log_" + (e["kind"] if isinstance(e["kind"], str) else e["kind"][0])
        while n in names:
            n += "_"
        names.append(n)
    ctx.plan(names)
    r = ctx.get("/api/log")
    entries = [e for e in ((r or {}).get("entries") or []) if isinstance(e, dict)]
    now = dt(ctx.state().get("now"))
    t0 = dt(ctx.vars.get("_t0"))
    for n, e in zip(names, exp):
        kinds = [e["kind"]] if isinstance(e["kind"], str) else e["kind"]
        item = _ref(ctx, e.get("item"))
        hits = [x for x in entries if x.get("kind") in kinds
                and (item is None or item in (x.get("item_ids") or []))]
        bad_at = [x.get("at") for x in hits if not (dt(x.get("at")) and t0 and now and t0 <= dt(x.get("at")) <= now)]
        ok = len(hits) >= e.get("min", 1) and not bad_at
        seen = sorted({str(x.get("kind")) for x in entries})
        ctx.check(n, ok, f"want {kinds} for {item} (>= {e.get('min', 1)}), got {len(hits)}; bad at={bad_at[:3]}; "
                         f"kinds in log={seen}")


def op_move(ctx: Ctx, st: dict):
    ctx.plan(["moved_exact", "others_unchanged"])
    iid = _ref(ctx, st["item"])
    s0 = ctx.state()
    before = _snapshot(s0)
    old = item_map(s0).get(iid) or {}
    r = ctx.post("/api/command", {"text": st["text"], "mode": "command"})
    s1 = ctx.state()
    it = item_map(s1).get(iid) or {}
    os_, oe, ns, ne = dt(old.get("start")), dt(old.get("end")), dt(it.get("start")), dt(it.get("end"))
    ok = ns == dt(st["start"]) and (oe is None or (ne and os_ and ne - ns == oe - os_))
    ctx.check("moved_exact", ok, f"want start {st['start']}, saw {it.get('start')}-{it.get('end')}; "
                                 f"plan={short(_plan_of(r).get('status'))}")
    after = _snapshot(s1)
    changed = [d for d in _diff_snap(before, after) if not d.endswith(f" {iid}")]
    ctx.check("others_unchanged", not changed, f"{changed[:5]}")


def op_cancel(ctx: Ctx, st: dict):
    ctx.plan(["delete_preview", "unchanged_until_apply", "apply_deletes"])
    iid = _ref(ctx, st["item"])
    before = _snapshot(ctx.state())
    r = ctx.post("/api/command", {"text": st["text"], "mode": "command"})
    plan = _plan_of(r)
    ok = plan.get("status") == "preview" and any(o.get("item_id") == iid for o in _ops(plan, "delete"))
    ctx.check("delete_preview", ok, f"plan={short(plan, 200)}")
    ctx.check("unchanged_until_apply", _snapshot(ctx.state()) == before,
              f"{_diff_snap(before, _snapshot(ctx.state()))[:5]}")
    ap = _apply(ctx, plan)
    gone = iid not in item_map(ctx.state())
    ctx.check("apply_deletes", gone and ap.get("status") == "applied", f"applied status={ap.get('status')} gone={gone}")


def op_query(ctx: Ctx, st: dict):
    ctx.plan(["reply_names_all", "nothing_changed"])
    s0 = ctx.state()
    before = _snapshot(s0)
    r = ctx.post("/api/command", {"text": st["text"], "mode": "command"})
    am = agent(r.get("messages"))
    txt = " ".join(text_of(m) for m in am if m.get("kind") == "reply").lower()
    missing = [k for k in st["kws"] if k.lower() not in txt]
    ctx.check("reply_names_all", bool(txt) and not missing, f"missing {missing} in reply: {short(txt, 200)}")
    after = _snapshot(ctx.state())
    ctx.check("nothing_changed", after == before and not (r.get("created") or []),
              f"{_diff_snap(before, after)[:5]} created={r.get('created')}")


def op_bulk(ctx: Ctx, st: dict):
    ctx.plan(["preview_plan", "affected_exact", "delete_ops", "one_create", "unchanged_until_apply",
              "apply_removes", "apply_adds_new", "undo_restores"])
    s0 = ctx.state()
    before = _snapshot(s0)
    want = set(st["affected"])
    r = ctx.post("/api/command", {"text": st["text"], "mode": st.get("mode", "command")})
    plan = _plan_of(r)
    ctx.check("preview_plan", plan.get("status") == "preview", f"plan={short(plan, 200)}")
    aff = set(plan.get("affected") or [])
    extra = {a for a in aff - want if a in before}  # ids of new (created) items are fine
    ctx.check("affected_exact", want <= aff and not extra, f"want {sorted(want)}, affected {sorted(aff)}")
    dels = {o.get("item_id") for o in _ops(plan, "delete")}
    ctx.check("delete_ops", dels == want, f"delete ops for {sorted(map(str, dels))}")
    cre = _ops(plan, "create")
    ok = len(cre) == 1 and _has_kw(cre[0].get("after") or {}, st["new_kw"])
    ctx.check("one_create", ok, f"creates: {short([(o.get('after') or {}).get('title') for o in cre])}")
    ctx.check("unchanged_until_apply", _snapshot(ctx.state()) == before,
              f"{_diff_snap(before, _snapshot(ctx.state()))[:5]}")
    _apply(ctx, plan)
    s1 = ctx.state()
    left = want & set(item_map(s1))
    ctx.check("apply_removes", not left, f"still there: {sorted(left)}")
    new = [i for i in s1["items"] if i.get("id") not in before and _has_kw(i, st["new_kw"])]
    ctx.check("apply_adds_new", len(new) == 1, f"new items matching {st['new_kw']}: {len(new)}")
    _apply(ctx, plan, "undo")
    after = _snapshot(ctx.state())
    ctx.check("undo_restores", after == before, f"{_diff_snap(before, after)[:6]}")


def op_ics(ctx: Ctx, st: dict):
    ctx.plan(["preview_creates", "nothing_before_apply", "apply_adds", "source_ics", "tzid_wallclock",
              "allday_date", "rrule_weekly", "reimport_zero"])
    before = _snapshot(ctx.state())
    r = ctx.post("/api/import", {"filename": st["filename"], "content": st["content"]})
    plan = _plan_of(r)
    cre = _ops(plan, "create")
    ctitles = [str((o.get("after") or {}).get("title") or "") for o in cre]
    miss = [k for k in st["titles"] if not any(k.lower() in t.lower() for t in ctitles)]
    ctx.check("preview_creates", plan.get("status") == "preview" and len(cre) >= st["n"] and not miss,
              f"status={plan.get('status')} creates={len(cre)} missing={miss} titles={short(ctitles)}")
    ctx.check("nothing_before_apply", _snapshot(ctx.state()) == before, "items changed before apply")
    _apply(ctx, plan)
    s = ctx.state()
    new = [i for i in s["items"] if i.get("id") not in before]
    miss = [k for k in st["titles"] if not any(_has_kw(i, k) for i in new)]
    ctx.check("apply_adds", bool(new) and not miss, f"new={len(new)} missing={miss}")
    ctx.check("source_ics", bool(new) and all(i.get("source") == "ics" for i in new),
              f"sources={sorted({str(i.get('source')) for i in new})}")

    def first(kw):
        return next((i for i in new if _has_kw(i, kw)), {})
    tz = first(st["tz"]["kw"])
    ctx.check("tzid_wallclock", str(tz.get("start") or "")[:16] == st["tz"]["start"],
              f"want {st['tz']['start']}, saw {tz.get('start')}")
    ad = first(st["allday"]["kw"])
    ok = str(ad.get("start") or "")[:10] == st["allday"]["date"] and not (
        dt(ad.get("end")) and dt(ad.get("end")).date().isoformat() > st["allday"]["last"])
    ctx.check("allday_date", ok, f"want {st['allday']['date']}, saw {ad.get('start')}-{ad.get('end')}")
    rr = [i for i in new if _has_kw(i, st["rrule"]["kw"])]
    dates = {str(i.get("start") or "")[:10] for i in rr}
    ok = any(i.get("recurrence") for i in rr) or len(dates & set(st["rrule"]["dates"])) >= 2
    ctx.check("rrule_weekly", ok, f"dates={sorted(dates)} recurrence={[i.get('recurrence') for i in rr][:1]}")
    r2 = ctx.post("/api/import", {"filename": st["filename"], "content": st["content"]})
    p2 = _plan_of(r2)
    ctx.check("reimport_zero", bool(p2) and len(_ops(p2, "create")) == 0, f"second import creates={len(_ops(p2, 'create'))}")


def op_text_import(ctx: Ctx, st: dict):
    ctx.plan(["preview_mixed", "unchanged_until_apply", "apply_changes", "undo_restores"])
    before = _snapshot(ctx.state())
    r = ctx.post("/api/import", {"filename": st.get("filename", "note.txt"), "content": st["content"]})
    plan = _plan_of(r)
    ok = plan.get("status") == "preview" and bool(_ops(plan, "delete") + _ops(plan, "update")) and bool(_ops(plan, "create"))
    ctx.check("preview_mixed", ok, f"plan ops={[o.get('op') for o in (plan.get('ops') or []) if isinstance(o, dict)]}")
    ctx.check("unchanged_until_apply", _snapshot(ctx.state()) == before, "items changed before apply")
    _apply(ctx, plan)
    s = ctx.state()
    after = _snapshot(s)
    cleared = all(g not in after or after[g] != before[g] for g in st["cleared"])
    added = any(i.get("id") not in before and _has_kw(i, st["new_kw"]) for i in s["items"])
    ctx.check("apply_changes", cleared and added, f"cleared={cleared} added '{st['new_kw']}'={added}")
    _apply(ctx, plan, "undo")
    after = _snapshot(ctx.state())
    ctx.check("undo_restores", after == before, f"{_diff_snap(before, after)[:6]}")


def _beliefs(ctx: Ctx) -> list:
    r = ctx.get("/api/beliefs")
    return [b for b in ((r or {}).get("beliefs") or []) if isinstance(b, dict)]


def _find_belief(bs: list, rx: str, level="habit"):
    for b in bs:
        if b.get("level", level) == level and re.search(rx, f"{b.get('key')} {b.get('statement')}", re.I):
            return b
    return None


def _learn(ctx: Ctx) -> dict:
    r = ctx.post("/api/learn", {})
    return r


def _open_proposal(ctx: Ctx, r: dict | None = None):
    p = (r or {}).get("proposal")
    if not isinstance(p, dict):
        p = (ctx.get("/api/proposal") or {}).get("proposal")
    return p if isinstance(p, dict) and p.get("id") else None


def op_learn(ctx: Ctx, st: dict):
    names = []
    if st.get("belief"):
        names += ["belief_found", "beliefs_valid"]
    if st.get("no_proposal"):
        names += ["no_proposal"]
    ctx.plan(names)
    r = _learn(ctx)
    bs = _beliefs(ctx)
    if st.get("belief"):
        e = st["belief"]
        b = _find_belief(bs, e["re"])
        conf = (b or {}).get("confidence")
        ok = b is not None and isinstance(conf, (int, float)) and conf >= e.get("min_conf", 0.7) \
            and re.search(rf"\b{e['num']}\b", str(b.get("statement") or ""))
        ctx.check("belief_found", ok, f"match={short(b, 200) if b else None}; keys={[x.get('key') for x in bs]}")
        if b:
            ctx.vars[st["as"]] = b.get("key")
            ctx.vars[st["as"] + "_conf"] = conf
            ctx.vars[st["as"] + "_re"] = e["re"]
        log_ids = {x.get("id") for x in ((ctx.get("/api/log") or {}).get("entries") or []) if isinstance(x, dict)}
        known = log_ids | set(ctx.vars.get("_seed_ids", []))
        bad = [x.get("key") for x in bs
               if not (isinstance(x.get("confidence"), (int, float)) and 0 <= x["confidence"] <= 1)
               or not isinstance(x.get("evidence"), list) or not x["evidence"]
               or any(ev not in known for ev in x["evidence"])]
        ctx.check("beliefs_valid", bool(bs) and not bad, f"beliefs with bad confidence/evidence: {bad[:4]}")
    if st.get("no_proposal"):
        p = _open_proposal(ctx, r)
        ctx.check("no_proposal", p is None, f"proposal={short(p)}")


def op_counter(ctx: Ctx, st: dict):
    ctx.plan(["confidence_drops"])
    ctx.clock(st["clock"])
    for iid in st["done"]:
        ctx.post(f"/api/items/{iid}/done", {})
    _learn(ctx)
    bs = _beliefs(ctx)
    key = ctx.vars.get(st["belief"])
    b = next((x for x in bs if x.get("key") == key), None) or _find_belief(bs, ctx.vars.get(st["belief"] + "_re", "$^"))
    old = ctx.vars.get(st["belief"] + "_conf")
    new = (b or {}).get("confidence", 0.0)
    ctx.check("confidence_drops", old is not None and isinstance(new, (int, float)) and new < old,
              f"confidence {old} -> {new}")


def op_delete_belief(ctx: Ctx, st: dict):
    ctx.plan(["belief_gone", "not_relearned"])
    key = ctx.vars.get(st["belief"])
    if not key:
        raise AppError("no belief to delete")
    ctx.client.call("DELETE", f"/api/beliefs/{urllib.request.quote(str(key), safe='')}")
    gone = all(b.get("key") != key for b in _beliefs(ctx))
    ctx.check("belief_gone", gone, f"{key} still in /api/beliefs")
    _learn(ctx)
    bs = _beliefs(ctx)
    back = [b.get("key") for b in bs if b.get("key") == key]
    ctx.check("not_relearned", not back, f"re-learned: {back}")


def _belief_numbers(bs: list, keys) -> set:
    nums = set()
    for b in bs:
        if b.get("key") in (keys or []):
            nums |= set(re.findall(r"\d+", str(b.get("statement") or "")))
    return nums


def op_proposal(ctx: Ctx, st: dict):
    flow = st["flow"]
    names = ["proposal_opened", "reason_cites_number", "view_unchanged_before_action"]
    if flow == "accept_undo":
        names += ["accept_applies_view", "log_accepted", "undo_restores_view", "status_reverted", "belief_conf_drops"]
    else:
        names += ["dismissed", "dismiss_keeps_view"]
    names += ["not_reshown"]
    ctx.plan(names)
    v0 = (ctx.state().get("view") or {})
    r = _learn(ctx)
    p = _open_proposal(ctx, r)
    ctx.check("proposal_opened", p is not None and p.get("status", "shown") == "shown" and isinstance(p.get("view"), dict),
              f"proposal={short(p, 200)}")
    if p is None:
        return
    bs = _beliefs(ctx)
    nums = _belief_numbers(bs, p.get("belief_keys"))
    reason = str(p.get("reason") or "")
    ctx.check("reason_cites_number", bool(nums) and any(re.search(rf"\b{n}\b", reason) for n in nums),
              f"reason={short(reason)} belief numbers={sorted(nums)}")
    v1 = (ctx.state().get("view") or {})
    ctx.check("view_unchanged_before_action", v1.get("type") == v0.get("type"), f"view {v0.get('type')} -> {v1.get('type')}")
    pid, keys = p["id"], p.get("belief_keys") or []
    conf0 = {b.get("key"): b.get("confidence") for b in bs}
    if flow == "accept_undo":
        ctx.post(f"/api/proposals/{pid}/accept", {})
        v2 = ctx.state().get("view") or {}
        ctx.check("accept_applies_view", v2.get("type") == p["view"].get("type"),
                  f"state.view={v2.get('type')} proposal.view={p['view'].get('type')}")
        log = [e for e in ((ctx.get("/api/log") or {}).get("entries") or []) if isinstance(e, dict)]
        ctx.check("log_accepted", any(e.get("kind") == "proposal_accepted" for e in log), "no proposal_accepted entry")
        r = ctx.post(f"/api/proposals/{pid}/undo", {})
        v3 = ctx.state().get("view") or {}
        ctx.check("undo_restores_view", v3.get("type") == v0.get("type"), f"view after undo={v3.get('type')} (was {v0.get('type')})")
        ctx.check("status_reverted", ((r.get("proposal") or {}).get("status")) == "reverted",
                  f"status={(r.get('proposal') or {}).get('status')}")
        bs2 = {b.get("key"): b.get("confidence") for b in _beliefs(ctx)}
        drops = [k for k in keys if isinstance(conf0.get(k), (int, float))
                 and (bs2.get(k) is None or bs2[k] < conf0[k])]
        ctx.check("belief_conf_drops", bool(keys) and len(drops) == len(keys), f"before={ {k: conf0.get(k) for k in keys} } after={ {k: bs2.get(k) for k in keys} }")
    else:
        r = ctx.post(f"/api/proposals/{pid}/dismiss", {})
        ctx.check("dismissed", ((r.get("proposal") or {}).get("status")) == "dismissed",
                  f"status={(r.get('proposal') or {}).get('status')}")
        v2 = ctx.state().get("view") or {}
        ctx.check("dismiss_keeps_view", v2.get("type") == v0.get("type"), f"view={v2.get('type')}")
    r = _learn(ctx)
    p2 = _open_proposal(ctx, r)
    ok = p2 is None or (p2.get("id") != pid and not set(p2.get("belief_keys") or []) & set(keys))
    ctx.check("not_reshown", ok, f"next proposal={short(p2, 160)}")


def op_view(ctx: Ctx, st: dict):
    ctx.plan(["view_switched", "hidden_counted", "log_view_switch"])
    r = ctx.post("/api/view", {"type": st["type"]})
    v = (r.get("view") or {})
    sv = ctx.state().get("view") or {}
    ctx.check("view_switched", v.get("type") == st["type"] and sv.get("type") == st["type"],
              f"response={v.get('type')} state={sv.get('type')}")
    hc = v.get("hidden_counts") or sv.get("hidden_counts") or {}
    n = hc.get(st["hidden_date"]) if isinstance(hc, dict) else None
    ctx.check("hidden_counted", isinstance(n, int) and n >= 1, f"hidden_counts={short(hc)}")
    log = [e for e in ((ctx.get("/api/log") or {}).get("entries") or []) if isinstance(e, dict)]
    ctx.check("log_view_switch", any(e.get("kind") == "view_switch" for e in log), "no view_switch entry")


def _designer(ctx: Ctx):
    ps = [p for p in ((ctx.get("/api/prompts") or {}).get("prompts") or []) if isinstance(p, dict)
          and p.get("agent") == "designer"]
    active = next((p for p in ps if p.get("active")), None)
    return ps, active


OVERRIDE_RE = re.compile(r"\bignore\b|\boverride\b|core rules", re.I)


def _advance(ctx: Ctx, hours: float):
    s = ctx.state()
    now = dt(s.get("now"))
    ctx.clock(fmt(now + timedelta(hours=hours, minutes=1)))


def op_meta(ctx: Ctx, st: dict):
    ctx.plan(["two_invented_reverted", "novelty_lower", "designer_new_version", "core_hash_same",
              "learned_clean", "next_classic", "rollback"])
    s = ctx.state()
    meta0 = s.get("meta") or {}
    nov0 = meta0.get("novelty")
    ps0, act0 = _designer(ctx)
    reverted, seen, used_keys = 0, [], set()
    for _ in range(st.get("rounds", 8)):
        cad = (ctx.state().get("meta") or {}).get("cadence_hours")
        cad = cad if isinstance(cad, (int, float)) and 0 <= cad <= 24 * 14 else 24
        r = _learn(ctx)
        p = _open_proposal(ctx, r)
        if p:
            seen.append(f"{p.get('novelty')}:{p.get('title')}")
            used_keys |= set(p.get("belief_keys") or [])
            if p.get("novelty") == "invented":
                ctx.post(f"/api/proposals/{p['id']}/accept", {})
                ctx.post(f"/api/proposals/{p['id']}/undo", {})
                reverted += 1
            else:
                ctx.post(f"/api/proposals/{p['id']}/dismiss", {})
        if reverted >= 2:
            break
        _advance(ctx, cad)
    ctx.check("two_invented_reverted", reverted >= 2, f"proposals seen: {short(seen, 240)}")
    nov1 = (ctx.state().get("meta") or {}).get("novelty")
    ctx.check("novelty_lower", isinstance(nov0, (int, float)) and isinstance(nov1, (int, float)) and nov1 < nov0,
              f"meta.novelty {nov0} -> {nov1}")
    ps1, act1 = _designer(ctx)
    a = act1 or {}
    ok = len(ps1) > len(ps0) and bool(str(a.get("diff") or "").strip()) and bool(a.get("reason")) \
        and bool(a.get("evidence")) and (a.get("replay") or {}).get("passed") is True
    ctx.check("designer_new_version", ok, f"versions {len(ps0)} -> {len(ps1)}; active={short(a, 240)}")
    ctx.check("core_hash_same", bool(act0) and bool(a) and a.get("core_hash") == act0.get("core_hash")
              and a.get("version") != act0.get("version"),
              f"core_hash {(act0 or {}).get('core_hash')} -> {a.get('core_hash')}")
    learned = str(a.get("learned") or "")
    ctx.check("learned_clean", bool(a) and len(learned.splitlines()) <= 12 and not OVERRIDE_RE.search(learned),
              f"learned ({len(learned.splitlines())} lines): {short(learned, 160)}")
    nxt = None
    for _ in range(3):
        cad = (ctx.state().get("meta") or {}).get("cadence_hours")
        _advance(ctx, cad if isinstance(cad, (int, float)) and 0 <= cad <= 24 * 14 else 24)
        nxt = _open_proposal(ctx, _learn(ctx))
        if nxt:
            break
    if nxt:
        ctx.check("next_classic", nxt.get("novelty") == "classic", f"next proposal={short(nxt, 160)}")
    else:
        # A next proposal is only observable if some belief is still eligible (>= 0.7 and not used by a
        # reverted/dismissed proposal). If one is, the gate should have opened a proposal for it.
        eligible = [b.get("key") for b in _beliefs(ctx) if b.get("level", "habit") == "habit"
                    and isinstance(b.get("confidence"), (int, float)) and b["confidence"] >= 0.7
                    and b.get("key") not in used_keys]
        ctx.check("next_classic", not eligible,
                  f"no next proposal although beliefs {eligible} are eligible" if eligible
                  else "no eligible belief left after the reverts (not observable)")
    r = ctx.post("/api/prompts/designer/rollback", {})
    _, act2 = _designer(ctx)
    ok = bool(act0) and bool(act2) and act2.get("version") == act0.get("version") \
        and ((r.get("active") or {}).get("version") == act0.get("version"))
    ctx.check("rollback", ok, f"active after rollback={(act2 or {}).get('version')} want {(act0 or {}).get('version')}")


def op_activity(ctx: Ctx, st: dict):
    ctx.plan(["activity_kinds", "activity_ordered", "rewrite_has_diff"])
    ev = [e for e in ((ctx.get("/api/activity") or {}).get("events") or []) if isinstance(e, dict)]
    blob = [f"{e.get('agent')} {e.get('kind')} {e.get('text')}".lower() for e in ev]
    need = {"belief": r"belief", "proposal": r"propos", "prompt": r"prompt|rewrite|version"}
    miss = [k for k, rx in need.items() if not any(re.search(rx, b) for b in blob)]
    ctx.check("activity_kinds", bool(ev) and not miss, f"missing {miss} in {len(ev)} events")
    ats = [dt(e.get("at")) for e in ev]
    ok = bool(ev) and all(ats) and all(a <= b for a, b in zip(ats, ats[1:]))
    ctx.check("activity_ordered", ok, f"ats={[e.get('at') for e in ev][:8]}")
    rw = [e for e, b in zip(ev, blob) if re.search(r"prompt|rewrite", b)]
    ok = any(re.search(r"^[+-]|\n[+-]|(^|\s)[+-] \S|diff|→|->", str(e.get("text") or ""))
             or str(e.get("diff") or "").strip() for e in rw)
    ctx.check("rewrite_has_diff", ok, f"rewrite events: {short([e.get('text') for e in rw], 200)}")


def _assets(ctx: Ctx, html: str, tag_re: str, attr: str) -> list[str]:
    """Fetch same-origin assets (<link href> / <script src>) served by the app."""
    out = []
    for tag in re.findall(tag_re, html, re.I | re.S):
        m = re.search(attr + r"""\s*=\s*["']([^"']+)["']""", tag, re.I)
        if not m or re.match(r"(https?:)?//", m.group(1)):
            continue
        try:
            out.append(ctx.client.call("GET", "/" + m.group(1).lstrip("./").lstrip("/"), raw=True))
        except AppError:
            pass
    return out


def _open_tag(html: str, el_id: str) -> str:
    m = re.search(r"""<[a-zA-Z][^>]*\bid\s*=\s*["']?%s["'\s>][^>]*>""" % re.escape(el_id), html)
    return m.group(0) if m else ""


def op_ui_usability(ctx: Ctx, st: dict):
    """Static usability checks on GET / (no browser): ids, real CSS, nothing hidden, wired-up script, focus/undo."""
    ctx.plan(["ids_present", "css_rules", "css_covers_main", "not_hidden", "script_endpoints", "refresh",
              "start_button", "undo_buttons"])
    html = ctx.client.call("GET", "/", raw=True)
    missing = [i for i in UI_IDS if not _open_tag(html, i)]
    ctx.check("ids_present", not missing, f"missing ids: {missing}")

    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.I | re.S))
    css += "\n".join(_assets(ctx, html, r"<link\b[^>]*>", "href"))
    css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = [r for r in re.findall(r"([^{}]+)\{[^{}]*\}", css_nc) if not r.strip().startswith("@")]
    ctx.check("css_rules", len(rules) >= st.get("min_rules", 25), f"{len(rules)} CSS rules")
    sels = " ".join(rules).lower()
    miss = [k for k in ("chat", "focus", "timeline") if k not in sels]
    ctx.check("css_covers_main", not miss, f"no CSS selectors for {miss}")

    hidden = []
    for i in UI_IDS:
        if i == "proposal":
            continue
        tag = _open_tag(html, i)
        if re.search(r"""style\s*=\s*["'][^"']*display\s*:\s*none""", tag, re.I) or re.search(r"\shidden(\s|>|=)", tag):
            hidden.append(i)
        elif re.search(r"#%s\s*\{[^}]*display\s*:\s*none" % re.escape(i), css_nc, re.I):
            hidden.append(i)
    ctx.check("not_hidden", not missing and not hidden, f"hidden by default: {hidden}")

    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S))
    js += "\n".join(_assets(ctx, html, r"<script\b[^>]*>", "src"))

    def calls(name):
        return bool(re.search(r"api/%s\b|['\"`]/?%s['\"`/]" % (name, name), js))
    need = ["state", "command", "chat", "clock", "focus"]
    miss = [n for n in need if not calls(n)]
    if not (calls("proposal") or calls("proposals") or calls("beliefs")):
        miss.append("proposal|beliefs")
    ctx.check("script_endpoints", bool(js.strip()) and not miss, f"script never calls: {miss}")
    ctx.check("refresh", bool(re.search(r"setInterval|setTimeout|EventSource|requestAnimationFrame", js)),
              "no periodic refresh (setInterval/setTimeout/EventSource)")
    focus_tag = _open_tag(html, "focus")
    k = html.find(focus_tag) if focus_tag else -1
    in_focus_html = k >= 0 and "start 5 minutes" in html[k:k + 4000].lower()
    in_js = bool(re.search(r"start 5 minutes", js, re.I)) and bool(re.search(r"""['"`#]focus\b""", js))
    ctx.check("start_button", bool(focus_tag) and (in_focus_html or in_js), "no 'Start 5 minutes' for #focus")
    ok = bool(re.search(r"undo_id", js)) and bool(re.search(r"\bundo\b", js, re.I)) and calls("undo")
    ctx.check("undo_buttons", ok, "script does not render Undo for messages with undo_id (undo_id + /api/undo)")


OPS = {"reset": op_reset, "ui": op_ui, "command": op_command, "decline": op_decline, "breakdown": op_breakdown,
       "focus": op_focus, "done": op_done, "reminders": op_reminders, "postpone": op_postpone,
       "checkin": op_checkin, "final": op_final, "log": op_log, "move": op_move, "cancel": op_cancel,
       "query": op_query, "bulk": op_bulk, "ics": op_ics, "text_import": op_text_import, "learn": op_learn,
       "counter": op_counter, "delete_belief": op_delete_belief, "proposal": op_proposal, "view": op_view,
       "meta": op_meta, "activity": op_activity, "ui_usability": op_ui_usability}

# ---------------------------------------------------------------- process management


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _crusoe_key() -> str | None:
    if os.environ.get("CRUSOE_API"):
        return os.environ["CRUSOE_API"]
    try:
        txt = (Path.home() / ".zshrc").read_text(errors="ignore")
    except OSError:
        return None
    m = re.search(r"""^\s*export\s+CRUSOE_API=["']?([^"'\s]+)""", txt, re.M)
    return m.group(1) if m else None


def app_env(mock: bool = False) -> dict:
    env = dict(os.environ)
    if mock:  # behaviour 21: no network, no keys
        for k in ("CRUSOE_API", "CRUSOE_BASE_URL"):
            env.pop(k, None)
        env["EVOLVE_MOCK"] = "1"
        env.setdefault("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
        env.setdefault("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
        env["PYTHONUNBUFFERED"] = "1"
        return env
    env.pop("EVOLVE_MOCK", None)
    key = _crusoe_key()
    if key:
        env["CRUSOE_API"] = key
    env.setdefault("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
    env.setdefault("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    env.setdefault("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _wait_health(base: str, proc, limit: float) -> bool:
    t_end = time.time() + limit
    while time.time() < t_end:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if json.loads(r.read().decode()).get("ok") is True:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _kill(proc):
    if proc is None or proc.poll() is not None and not _group_alive(proc.pid):
        return
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait)
            if not _group_alive(proc.pid):
                return
        except subprocess.TimeoutExpired:
            pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


START_ATTEMPTS = 3
ADDR_IN_USE_RE = re.compile(r"Address already in use|EADDRINUSE|Errno 98\b|Errno 48\b")


def _port_race(proc, err_path: Path, secs: float) -> bool:
    """Start failed because the picked port was taken (not because the app is broken)."""
    if proc.poll() is None:
        return False  # still running but unhealthy: the app's fault, do not spend another 15 s
    return secs < 3 and bool(ADDR_IN_USE_RE.search(_tail(err_path, 4000)))


def _tail(path: Path, n: int = 600) -> str:
    try:
        return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def run_scenario(app_dir, sc: dict, timeout: float = 60) -> dict:
    t0 = time.time()
    app_dir = Path(app_dir).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="evcal_"))
    data_dir = tmp / "data"
    data_dir.mkdir()
    err_path = tmp / "stderr.log"
    proc, started, crashed = None, False, False
    try:
        if (app_dir / "app.py").is_file():
            # Retry on a new port only for the pick-then-bind race between concurrent harness runs.
            for _attempt in range(START_ATTEMPTS):
                port = _free_port()
                base = f"http://127.0.0.1:{port}"
                t_start = time.time()
                with open(err_path, "wb") as ferr:
                    proc = subprocess.Popen([sys.executable, "app.py", "--port", str(port), "--data", str(data_dir)],
                                            cwd=app_dir, env=app_env(bool(sc.get("mock"))), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=ferr, start_new_session=True)
                started = _wait_health(base, proc, HEALTH_TIMEOUT) and proc.poll() is None
                if started or not _port_race(proc, err_path, time.time() - t_start):
                    break
                _kill(proc)
        else:
            base = "http://127.0.0.1:9"
        ctx = Ctx(Client(base, timeout, dead=not started))
        tail = [{"op": "final", "label": "final"}] if sc.get("final", True) else []
        for i, step in enumerate(sc["steps"] + tail):
            ctx.prefix = step.get("label") or f"{i}_{step['op']}"
            detail = "not reached"
            try:
                OPS[step["op"]](ctx, step)
            except AppError as e:
                detail = str(e)
            except Exception as e:  # app returned an unexpected shape
                detail = f"{type(e).__name__}: {e}"
            ctx.fail_rest(detail)
            if started and proc.poll() is not None and not crashed:
                crashed = True
                ctx.client.dead = True
    finally:
        _kill(proc)
        err_tail = _tail(err_path)
        shutil.rmtree(tmp, ignore_errors=True)

    checks = [{"name": n, "ok": ctx.results[n][0], "detail": ctx.results[n][1]} for n in ctx.planned]
    passed = sum(c["ok"] for c in checks)
    score = 0.0 if not started else passed / max(1, len(checks))
    failed = [c for c in checks if not c["ok"]]
    if not started:
        reason = "no_start"
    elif crashed:
        reason = "crash"
    else:
        reason = failed[0]["name"] if failed else "ok"
    detail = "; ".join(f"{c['name']}: {c['detail']}" for c in failed[:8])
    if len(failed) > 8:
        detail += f"; ... {len(failed) - 8} more failed"
    if not started:
        detail = ("no app.py in app_dir" if not (app_dir / "app.py").is_file()
                  else "no {\"ok\": true} from GET /health within 15 s") + ". stderr: " + err_tail
    elif crashed:
        detail = short(detail, 1200) + " || stderr: " + err_tail
    return {"id": sc["id"], "score": round(score, 4), "hard": score == 1.0, "reason": reason,
            "detail": short(detail, 2400), "checks": checks, "secs": round(time.time() - t0, 1)}


def run_split(app_dir, split: str, timeout: float = 60, workers: int = 3) -> list[dict]:
    scs = SCENARIOS.SPLITS[split]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda sc: run_scenario(app_dir, sc, timeout), scs))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("app_dir")
    ap.add_argument("split", nargs="?", default="fit", choices=sorted(SCENARIOS.SPLITS))
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print rows as JSON")
    a = ap.parse_args(argv)
    rows = run_split(a.app_dir, a.split, a.timeout, a.workers)
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    print(f"{'scenario':<28} {'score':>6} {'pass':>7} {'secs':>6}  reason")
    for r in rows:
        npass = sum(c["ok"] for c in r["checks"])
        print(f"{r['id']:<28} {r['score']:>6.2f} {npass:>3}/{len(r['checks']):<3} {r['secs']:>6.1f}  {r['reason']}")
    total = sum(len(r["checks"]) for r in rows)
    print(f"{'MEAN':<28} {sum(r['score'] for r in rows) / max(1, len(rows)):>6.2f} "
          f"{sum(sum(c['ok'] for c in r['checks']) for r in rows):>3}/{total:<3}  hard={sum(r['hard'] for r in rows)}/{len(rows)}")
    for r in rows:
        bad = [c for c in r["checks"] if not c["ok"]]
        if bad:
            print(f"\n[{r['id']}]")
            for c in bad:
                print(f"  x {c['name']}: {c['detail']}")


if __name__ == "__main__":
    main()
