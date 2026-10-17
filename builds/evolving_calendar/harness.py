"""Frozen harness for the Evolving Calendar (Iris build).

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
    spec = importlib.util.spec_from_file_location("evolving_calendar_scenarios", HERE / "scenarios.py")
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
    s = ctx.state()
    ids = set(item_map(s))
    missing = sorted({i["id"] for i in st.get("items", [])} - ids)
    ctx.check("reset_ok", str(s.get("now", ""))[:16] == st["now"] and not missing,
              f"now={s.get('now')} missing_ids={missing[:5]}")


def op_ui(ctx: Ctx, st: dict):
    ctx.plan(["html_ids"])
    html = ctx.client.call("GET", "/", raw=True)
    missing = [i for i in ("chat-input", "chat-log", "focus", "timeline")
               if not re.search(r"""id\s*=\s*["']?%s["'\s>]""" % re.escape(i), html)]
    ctx.check("html_ids", not missing, f"missing ids: {missing}")


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


OPS = {"reset": op_reset, "ui": op_ui, "command": op_command, "decline": op_decline, "breakdown": op_breakdown,
       "focus": op_focus, "done": op_done, "reminders": op_reminders, "postpone": op_postpone,
       "checkin": op_checkin, "final": op_final}

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


def app_env() -> dict:
    env = dict(os.environ)
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
                                            cwd=app_dir, env=app_env(), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=ferr, start_new_session=True)
                started = _wait_health(base, proc, HEALTH_TIMEOUT) and proc.poll() is None
                if started or not _port_race(proc, err_path, time.time() - t_start):
                    break
                _kill(proc)
        else:
            base = "http://127.0.0.1:9"
        ctx = Ctx(Client(base, timeout, dead=not started))
        for i, step in enumerate(sc["steps"] + [{"op": "final", "label": "final"}]):
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
