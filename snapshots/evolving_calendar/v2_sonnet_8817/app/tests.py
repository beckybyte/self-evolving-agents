import app

NOW = "2026-10-19T09:00"
BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]
VERBS = ["list", "pick", "write", "open", "collect", "choose", "draft", "email", "call", "book", "clear",
         "sort", "find", "gather", "send", "review", "update", "upload", "read"]
MSG_KEYS = ["id", "role", "kind", "text", "item_id", "at", "undo_id"]
AGENT = []


def call(method, path, body=None):
    r = app.handle(method, path, body if body is not None else {})
    assert isinstance(r, dict), "handle must return a dict for %s %s, got %r" % (method, path, type(r))
    msgs = r.get("messages")
    if method == "POST" and isinstance(msgs, list):
        for x in msgs:
            assert isinstance(x, dict), "message must be a dict"
            for k in MSG_KEYS:
                assert k in x, "message lacks key %s: %r" % (k, x)
            if x["kind"] == "breakdown":
                assert "steps" in x, "breakdown message lacks steps"
            else:
                assert "steps" not in x, "steps only on breakdown, saw on %s" % x["kind"]
            if x["role"] == "agent":
                AGENT.append(x)
    return r


def has_agent(r):
    return any(m.get("role") == "agent" for m in r.get("messages", []))


def it(iid, title, start=None, end=None, kind="task", status="open", **kw):
    d = {"id": iid, "kind": kind, "title": title, "start": start, "end": end, "status": status}
    d.update(kw)
    return d


def seed():
    items = []
    for i in range(8):
        day = 9 + i
        items.append(it("d%d" % (i + 1), "Done task %d" % (i + 1), "2026-10-%02dT20:30" % day,
                        "2026-10-%02dT21:30" % day, status="done"))
    items.append(it("dm", "Morning thing", "2026-10-15T10:00", "2026-10-15T11:00", status="done"))
    items.append(it("c1", "Clean out the garage", "2026-10-19T10:00", "2026-10-19T11:00"))
    items.append(it("den", "Dentist", "2026-10-22T15:00", "2026-10-22T16:00", kind="event"))
    items.append(it("yo", "Yoga class", "2026-10-21T18:00", "2026-10-21T19:00", kind="event"))
    items.append(it("fm", "Farmers market", "2026-10-24T09:00", "2026-10-24T10:00", kind="event"))
    return items


def weekday_items():
    from datetime import date, timedelta
    out = []
    for w in range(10):
        for d in range(5):
            day = date(2026, 8, 3) + timedelta(days=7 * w + d)
            out.append(it("w%d_%d" % (w, d), "Work block", day.isoformat() + "T10:00",
                          day.isoformat() + "T11:00", kind="event"))
    return out


def reset(items=None, now=NOW):
    r = call("POST", "/api/reset", {"now": now, "items": seed() if items is None else items})
    assert has_agent(r), "reset must produce an agent greeting message, got %r" % r.get("messages")
    st = call("GET", "/api/state")
    assert st["now"] == now, "state now %r != %r" % (st["now"], now)
    return st


def items_by_id():
    return dict((x["id"], x) for x in call("GET", "/api/state")["items"])


def cmd(text, mode="command"):
    r = call("POST", "/api/command", {"text": text, "mode": mode})
    assert has_agent(r), "command %r produced no agent message" % text
    assert "created" in r, "command response lacks created"
    return r


def chat(iid, text):
    r = call("POST", "/api/chat", {"item_id": iid, "text": text})
    assert has_agent(r), "chat %r produced no agent message" % text
    return r


def agent_of(r, kind):
    return [m for m in r.get("messages", []) if m["role"] == "agent" and m["kind"] == kind]


def belief(key):
    for b in call("GET", "/api/beliefs")["beliefs"]:
        if b["key"] == key:
            return b
    return None


def start_breakdown():
    reset()
    r = cmd("get my portfolio together")
    gid = r["created"][0]
    chat(gid, "yes")
    chat(gid, "to find a new job")
    chat(gid, "five projects online")
    r = chat(gid, "friday")
    bd = agent_of(r, "breakdown")
    assert bd, "behaviour 4: expected breakdown after deadline, got %r" % r["messages"]
    return gid, bd[0]


def test_state_shape():
    st = reset()
    for k in ["now", "items", "messages", "view", "meta"]:
        assert k in st, "state lacks %s" % k
    for k in ["id", "kind", "title", "start", "end", "window", "status", "why", "parent_id",
              "estimate_min", "postpone_count", "source"]:
        assert all(k in x for x in st["items"]), "item lacks key %s" % k
    assert len(st["items"]) == 13, "expected 13 seed items, saw %d" % len(st["items"])
    assert st["view"]["type"] == "week", "default view should be week, saw %r" % st["view"]
    assert st["meta"]["novelty"] == 0.6, "meta novelty default 0.6"
    assert call("GET", "/health").get("ok") is True, "health"


def test_1_typed():
    reset()
    r = cmd("dentist thursday 3pm")
    assert len(r["created"]) == 1, "behaviour 1: one item expected, saw %r" % r["created"]
    x = items_by_id()[r["created"][0]]
    assert x["kind"] == "event" and x["start"] == "2026-10-22T15:00", "behaviour 1: saw %r" % x
    assert x["title"].lower() == "dentist", "behaviour 1: title saw %r" % x["title"]
    r = cmd("lunch with Ana around noon tomorrow")
    x = items_by_id()[r["created"][0]]
    assert x["window"] and "noon" in x["window"], "behaviour 1: window expected, saw %r" % x
    assert x["start"] == "2026-10-20T12:00", "behaviour 1: start saw %r" % x["start"]
    assert x["title"] == "Lunch with Ana", "behaviour 1: title saw %r" % x["title"]


def test_2_dump():
    reset()
    r = cmd("call mom, portfolio, maybe yoga?, dentist thursday 3pm", "dump")
    assert len(r["created"]) == 4, "behaviour 2: 4 items expected, saw %r" % r["created"]
    its = items_by_id()
    den = [its[i] for i in r["created"] if its[i]["start"] == "2026-10-22T15:00"]
    assert den and den[0]["kind"] == "event", "behaviour 2: dentist event expected"
    assert all(its[i]["source"] == "dump" for i in r["created"]), "behaviour 2: source dump"
    qs = agent_of(r, "question")
    assert len(qs) <= 1, "behaviour 2: at most one question, saw %d" % len(qs)
    assert sum(m["text"].count("?") for m in r["messages"] if m["role"] == "agent" and m["kind"] != "info") <= 1
    r2 = cmd("call mom; buy milk\nwater the garden", "dump")
    assert len(r2["created"]) == 3, "behaviour 2: split on ; and newline, saw %r" % r2["created"]


def test_3_complexity():
    reset()
    for text in ["get my portfolio together", "work on the pitch deck"]:
        r = cmd(text)
        qs = agent_of(r, "question")
        assert len(qs) == 1, "behaviour 3: exactly one question for %r, saw %d" % (text, len(qs))
        assert qs[0]["item_id"] == r["created"][0], "behaviour 3: question item_id"
    r = cmd("call mom")
    assert not agent_of(r, "question"), "behaviour 3: simple task must not get a question"
    r = cmd("get my portfolio together")
    gid = r["created"][0]
    before = len(call("GET", "/api/state")["items"])
    r = chat(gid, "no thanks, not right now")
    assert not agent_of(r, "breakdown") and not agent_of(r, "question"), "behaviour 3: no after no"
    assert len(call("GET", "/api/state")["items"]) == before, "behaviour 3: nothing created after no"


def test_4_breakdown():
    reset()
    r = cmd("get my portfolio together")
    gid = r["created"][0]
    r = chat(gid, "yes")
    q = agent_of(r, "question")
    assert len(q) == 1 and "matter" in q[0]["text"].lower() and q[0]["text"].count("?") == 1, \
        "behaviour 4: why question expected, saw %r" % r["messages"]
    asked = 1
    r = chat(gid, "to find a new job")
    assert agent_of(r, "question"), "behaviour 4: second question"
    asked += 1
    r = chat(gid, "five projects online")
    assert agent_of(r, "question"), "behaviour 4: third question"
    asked += 1
    assert asked <= 3
    r = chat(gid, "friday")
    bd = agent_of(r, "breakdown")[0]
    steps = bd["steps"]
    assert 2 <= len(steps) <= 5, "behaviour 4: 2-5 steps, saw %d" % len(steps)
    assert steps[0]["estimate_min"] <= 15, "behaviour 4: first step <=15, saw %r" % steps[0]
    for s in steps:
        assert s["title"].split()[0].lower() in VERBS, "behaviour 4: verb-first, saw %r" % s["title"]
        assert s["estimate_min"] <= 60, "behaviour 4: step <= 1h"
    assert bd["item_id"] == gid
    assert not [x for x in call("GET", "/api/state")["items"] if x["parent_id"] == gid], \
        "behaviour 4: no children before yes"
    r = chat(gid, "looks good")
    kids = [x for x in call("GET", "/api/state")["items"] if x["parent_id"] == gid]
    assert len(kids) == len(steps), "behaviour 4: children %d vs steps %d" % (len(kids), len(steps))
    for k in kids:
        assert k["start"] and k["start"] < "2026-10-23T00:00", "behaviour 4: before friday, saw %r" % k["start"]
        assert k["kind"] == "task" and k["estimate_min"], "behaviour 4: child task shape"
    assert items_by_id()[gid]["why"] == "to find a new job", "behaviour 4: goal why"
    assert agent_of(r, "info"), "behaviour 4: info after accept"


def test_4b_revise():
    gid, bd = start_breakdown()
    r = chat(gid, "can we make the steps smaller?")
    b2 = agent_of(r, "breakdown")
    assert b2 and 2 <= len(b2[0]["steps"]) <= 5, "behaviour 4: revised breakdown, saw %r" % r["messages"]
    assert not [x for x in call("GET", "/api/state")["items"] if x["parent_id"] == gid]


def test_5_focus():
    gid, bd = start_breakdown()
    chat(gid, "looks good")
    kids = [x for x in call("GET", "/api/state")["items"] if x["parent_id"] == gid]
    first = min(kids, key=lambda k: k["start"])
    f = call("GET", "/api/focus")
    assert f["item"] and f["item"]["id"] == first["id"], "behaviour 5: focus first step, saw %r" % f["item"]
    assert f["parent"]["id"] == gid and f["why"] == "to find a new job", "behaviour 5: parent/why"
    assert f["action"] == "Start 5 minutes", "behaviour 5: action"
    r = call("POST", "/api/items/%s/done" % first["id"], {})
    assert r["item"]["status"] == "done", "behaviour 5: done"
    f2 = call("GET", "/api/focus")
    assert f2["item"] and f2["item"]["id"] != first["id"] and f2["item"]["parent_id"] == gid, "behaviour 5: moves on"
    reset([])
    assert call("GET", "/api/focus")["item"] is None, "behaviour 5: empty focus is null"


def test_6_reminders():
    reset(seed() + [it("pr", "Call with Priya", "2026-10-19T20:00", "2026-10-19T21:00", kind="event"),
                    it("dn", "Done call", "2026-10-19T20:00", "2026-10-19T21:00", kind="event", status="done")])
    assert call("POST", "/api/clock", {"now": "2026-10-19T08:00"})["messages"] == [], "behaviour 6: backwards"
    r = call("POST", "/api/clock", {"now": "2026-10-19T20:01"})
    rem = [m for m in r["messages"] if m["kind"] == "reminder" and m["item_id"] == "pr"]
    assert len(rem) == 1 and "Call with Priya" in rem[0]["text"], "behaviour 6: start reminder, saw %r" % r["messages"]
    r = call("POST", "/api/clock", {"now": "2026-10-19T20:51"})
    rem = [m for m in r["messages"] if m["kind"] == "reminder" and m["item_id"] == "pr"]
    assert len(rem) == 1, "behaviour 6: end reminder, saw %r" % r["messages"]
    r = call("POST", "/api/clock", {"now": "2026-10-19T22:00"})
    assert not [m for m in r["messages"] if m["kind"] == "reminder"], "behaviour 6: no duplicates"
    assert not [m for m in AGENT if m["item_id"] == "dn" and m["kind"] == "reminder"], "behaviour 6: done item"


def test_7_postpone():
    reset(seed() + [it("cl", "Write cover letter", "2026-10-19T14:00", "2026-10-19T15:00")])
    r = call("POST", "/api/clock", {"now": "2026-10-19T15:20"})
    assert not [m for m in r["messages"] if m["item_id"] == "cl" and m["kind"] == "postponed"], \
        "behaviour 7: not before 30 min"
    r = call("POST", "/api/clock", {"now": "2026-10-19T15:40"})
    pm = [m for m in r["messages"] if m["kind"] == "postponed" and m["item_id"] == "cl"]
    assert len(pm) == 1 and pm[0]["undo_id"], "behaviour 7: postponed with undo_id, saw %r" % r["messages"]
    x = items_by_id()["cl"]
    assert x["start"][:10] == "2026-10-20" and x["start"][11:13] >= "20", "behaviour 7: evening slot, saw %r" % x["start"]
    assert x["postpone_count"] == 1 and x["status"] == "open", "behaviour 7: count"
    assert "Write cover letter" in pm[0]["text"] and x["start"][11:16] in pm[0]["text"], \
        "behaviour 7: text names task+time, saw %r" % pm[0]["text"]
    ln = (int(x["end"][11:13]) * 60 + int(x["end"][14:16])) - (int(x["start"][11:13]) * 60 + int(x["start"][14:16]))
    assert ln == 60, "behaviour 7: same length"
    assert call("POST", "/api/undo", {"undo_id": pm[0]["undo_id"]}).get("ok") is True, "behaviour 7: undo ok"
    x = items_by_id()["cl"]
    assert x["start"] == "2026-10-19T14:00" and x["end"] == "2026-10-19T15:00" and x["postpone_count"] == 0, \
        "behaviour 7: undo restores, saw %r" % x


def test_8_checkin():
    reset()
    r = call("POST", "/api/clock", {"now": "2026-10-22T23:59"})
    pm = [m for m in r["messages"] if m["kind"] == "postponed" and m["item_id"] == "c1"]
    ck = [m for m in r["messages"] if m["kind"] == "checkin" and m["item_id"] == "c1"]
    assert len(pm) == 2, "behaviour 8: two postponements, saw %d" % len(pm)
    assert len(ck) == 1, "behaviour 8: one checkin, saw %d" % len(ck)
    assert ck[0]["text"].count("?") == 1 and "Clean out the garage" in ck[0]["text"], "behaviour 8: checkin text"
    assert items_by_id()["c1"]["postpone_count"] == 2, "behaviour 8: count stays 2"
    r2 = chat("c1", "it is too big")
    assert agent_of(r2, "breakdown"), "behaviour 8: too big -> breakdown, saw %r" % r2["messages"]
    r3 = call("POST", "/api/clock", {"now": "2026-10-23T23:59"})
    assert not [m for m in r3["messages"] if m["kind"] == "checkin"], "behaviour 8: checkin never repeated"


def test_8b_not_needed():
    reset()
    call("POST", "/api/clock", {"now": "2026-10-22T23:59"})
    r = chat("c1", "I don't need this, not needed")
    q = agent_of(r, "question")
    assert q and "remove" in q[0]["text"].lower(), "behaviour 8: confirm question, saw %r" % r["messages"]
    chat("c1", "yes")
    assert "c1" not in items_by_id(), "behaviour 8: deleted after confirm"


def test_8c_wrong_time():
    reset()
    call("POST", "/api/clock", {"now": "2026-10-22T23:59"})
    before = items_by_id()["c1"]["start"]
    chat("c1", "wrong time, I am busy then")
    assert items_by_id()["c1"]["start"] > before, "behaviour 8: wrong time moves it later in time"


def test_9_noguilt():
    reset()
    cmd("call mom, portfolio", "dump")
    call("POST", "/api/clock", {"now": "2026-10-20T23:59"})
    assert AGENT, "behaviour 9: some agent messages"


def test_10_crud():
    reset()
    before = dict((k, dict(v)) for k, v in items_by_id().items())
    r = cmd("what's on thursday?")
    rep = agent_of(r, "reply")
    assert rep and "Dentist" in rep[0]["text"], "behaviour 10: reply names Dentist, saw %r" % r["messages"]
    assert r["created"] == [] and items_by_id() == before, "behaviour 10: query changes nothing"
    r = cmd("move my dentist to next week")
    assert items_by_id()["den"]["start"] == "2026-10-29T15:00", "behaviour 10: moved, saw %r" % items_by_id()["den"]
    assert items_by_id()["den"]["end"] == "2026-10-29T16:00", "behaviour 10: end moves too"
    assert not r.get("plan"), "behaviour 10: single move has no plan"
    yo = dict(items_by_id()["yo"])
    r = cmd("cancel yoga")
    plan = r.get("plan")
    assert plan and plan["status"] == "preview" and plan["origin"] == "command", "behaviour 10: preview plan"
    assert plan["affected"] == ["yo"] and plan["ops"][0]["op"] == "delete", "behaviour 10: delete op"
    assert "yo" in items_by_id(), "behaviour 10: yoga still there"
    r = call("POST", "/api/plan/%s/apply" % plan["id"], {})
    assert r["plan"]["status"] == "applied" and "yo" not in items_by_id(), "behaviour 10: applied"
    r = call("POST", "/api/plan/%s/apply" % plan["id"], {})
    assert r["plan"]["status"] == "applied", "behaviour 10: apply twice is a no-op"
    r = call("POST", "/api/plan/%s/undo" % plan["id"], {})
    assert r["plan"]["status"] == "undone" and items_by_id().get("yo") == yo, "behaviour 10: undo restores"
    assert "error" in call("POST", "/api/plan/nope/apply", {}), "behaviour 10: unknown plan -> error"


def test_11_bulk():
    reset()
    before = dict((k, dict(v)) for k, v in items_by_id().items())
    r = cmd("empty out my weekend for a business trip")
    plan = r["plan"]
    assert plan["status"] == "preview" and plan["affected"] == ["fm"], "behaviour 11: affected, saw %r" % plan["affected"]
    ops = plan["ops"]
    assert len([o for o in ops if o["op"] == "delete"]) == 1 and len([o for o in ops if o["op"] == "create"]) == 1, \
        "behaviour 11: ops, saw %r" % ops
    cr = [o for o in ops if o["op"] == "create"][0]["after"]
    assert cr["start"] == "2026-10-24T00:00" and cr["end"] == "2026-10-26T00:00", "behaviour 11: range, saw %r" % cr
    assert "fm" in items_by_id(), "behaviour 11: nothing before apply"
    call("POST", "/api/plan/%s/apply" % plan["id"], {})
    its = items_by_id()
    assert "fm" not in its and cr["id"] in its and its[cr["id"]]["title"] == "Business trip", "behaviour 11: applied"
    call("POST", "/api/plan/%s/undo" % plan["id"], {})
    assert items_by_id() == before, "behaviour 11: undo restores everything"


ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a1@x\r\nSUMMARY:Standup\r\n"
       "DTSTART;TZID=Europe/Berlin:20261022T091500\r\nDTEND;TZID=Europe/Berlin:20261022T094500\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:a2@x\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261026\r\nEND:VEVENT\r\n"
       "END:VCALENDAR\r\n")


def test_12_ics():
    reset()
    r = call("POST", "/api/import", {"filename": "cal.ics", "content": ICS})
    plan = r["plan"]
    cr = [o for o in plan["ops"] if o["op"] == "create"]
    assert plan["status"] == "preview" and len(cr) == 2, "behaviour 12: 2 creates, saw %r" % plan["ops"]
    assert all(o["after"]["source"] == "ics" and o["after"]["kind"] == "event" for o in cr), "behaviour 12: source"
    assert not [x for x in call("GET", "/api/state")["items"] if x["title"] == "Standup"], "behaviour 12: preview only"
    call("POST", "/api/plan/%s/apply" % plan["id"], {})
    its = [x for x in call("GET", "/api/state")["items"]]
    st = [x for x in its if x["title"] == "Standup"]
    hd = [x for x in its if x["title"] == "Holiday"]
    assert st and st[0]["start"] == "2026-10-22T09:15" and st[0]["end"] == "2026-10-22T09:45", \
        "behaviour 12: wall clock kept, saw %r" % st
    assert hd and hd[0]["start"] == "2026-10-26T00:00" and hd[0]["end"] == "2026-10-27T00:00", \
        "behaviour 12: all-day, saw %r" % hd
    r = call("POST", "/api/import", {"filename": "cal.ics", "content": ICS})
    assert len([o for o in r["plan"]["ops"] if o["op"] == "create"]) == 0, "behaviour 12: re-import 0 creates"


def test_12b_rrule():
    reset()
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:r1\nSUMMARY:Weekly sync\nDTSTART:20261021T100000Z\n"
           "RRULE:FREQ=WEEKLY;COUNT=4\nEND:VEVENT\nEND:VCALENDAR\n")
    plan = call("POST", "/api/import", {"filename": "r.ics", "content": ics})["plan"]
    cr = [o for o in plan["ops"] if o["op"] == "create"]
    assert cr, "behaviour 12: rrule event created"
    a = cr[0]["after"]
    assert a["start"] == "2026-10-21T10:00" and a["end"] == "2026-10-21T11:00", "behaviour 12: saw %r" % a
    assert "FREQ=WEEKLY" in str(a.get("recurrence")), "behaviour 12: recurrence stored, saw %r" % a


def test_13_text_import():
    reset()
    before = dict((k, dict(v)) for k, v in items_by_id().items())
    txt = "I'll be away this weekend, please clear it. Also lunch with Ana on Wednesday around noon."
    plan = call("POST", "/api/import", {"filename": "note.txt", "content": txt})["plan"]
    ops = plan["ops"]
    assert plan["status"] == "preview", "behaviour 13: preview"
    assert [o for o in ops if o["op"] in ("delete", "update")], "behaviour 13: delete expected, saw %r" % ops
    cr = [o for o in ops if o["op"] == "create"]
    assert len(cr) == 1 and "lunch" in cr[0]["after"]["title"].lower(), "behaviour 13: create lunch, saw %r" % ops
    assert cr[0]["after"]["start"] == "2026-10-21T12:00", "behaviour 13: wed noon, saw %r" % cr[0]["after"]["start"]
    assert items_by_id() == before, "behaviour 13: nothing before apply"
    call("POST", "/api/plan/%s/apply" % plan["id"], {})
    assert "fm" not in items_by_id() and cr[0]["after"]["id"] in items_by_id(), "behaviour 13: applied"
    call("POST", "/api/plan/%s/undo" % plan["id"], {})
    assert items_by_id() == before, "behaviour 13: undo restores"


def test_14_log():
    reset()
    r = cmd("call mom")
    cid = r["created"][0]
    call("POST", "/api/items/%s/done" % cid, {})
    call("POST", "/api/view", {"type": "day"})
    cmd("what's on thursday?")
    ents = call("GET", "/api/log")["entries"]
    for e in ents:
        for k in ["id", "at", "kind", "item_ids", "payload", "source"]:
            assert k in e, "behaviour 14: entry lacks %s" % k
    kinds = [e["kind"] for e in ents]
    for k in ["create", "done", "view_switch", "query"]:
        assert k in kinds, "behaviour 14: log kind %s missing, saw %r" % (k, kinds)
    cre = [e for e in ents if e["kind"] == "create"][0]
    assert cid in cre["item_ids"] and cre["at"] == NOW, "behaviour 14: create entry, saw %r" % cre
    ids = [e["id"] for e in ents]
    assert len(set(ids)) == len(ids), "behaviour 14: unique log ids"


def test_15_learner():
    reset()
    r = call("POST", "/api/learn", {})
    b = belief("evening_work")
    assert b and b["level"] == "habit", "behaviour 15: evening belief, saw %r" % r["beliefs"]
    assert 0.7 <= b["confidence"] <= 1 and "8" in b["statement"], "behaviour 15: saw %r" % b
    assert b["value"]["evening"] == 8 and b["value"]["total"] == 9, "behaviour 15: value %r" % b["value"]
    ok = set(x["id"] for x in seed()) | set(e["id"] for e in call("GET", "/api/log")["entries"])
    assert b["evidence"] and all(e in ok for e in b["evidence"]), "behaviour 15: evidence ids exist"
    conf = b["confidence"]
    call("POST", "/api/items/c1/done", {})
    for t in ["call mom", "buy milk"]:
        call("POST", "/api/items/%s/done" % cmd(t)["created"][0], {})
    call("POST", "/api/learn", {})
    b2 = belief("evening_work")
    assert b2 and b2["confidence"] < conf, "behaviour 15: counter-evidence lowers, %r -> %r" % (conf, b2)


def test_15b_delete_belief():
    reset()
    call("POST", "/api/learn", {})
    assert belief("evening_work"), "behaviour 20: belief expected"
    assert call("DELETE", "/api/beliefs/evening_work", {}).get("ok") is True, "behaviour 20: delete ok"
    assert belief("evening_work") is None, "behaviour 20: gone"
    call("POST", "/api/learn", {})
    assert belief("evening_work") is None, "behaviour 20: not re-learned"
    assert "error" in call("DELETE", "/api/beliefs/nothing_here", {}), "behaviour 20: unknown -> error"
    assert "belief_deleted" in [e["kind"] for e in call("GET", "/api/log")["entries"]]


def test_16_proposal():
    reset([it("a", "Only", "2026-10-20T10:00", "2026-10-20T11:00", kind="event")])
    r = call("POST", "/api/learn", {})
    assert r["proposal"] is None and call("GET", "/api/proposal")["proposal"] is None, "behaviour 16: sparse -> none"
    reset(weekday_items())
    r = call("POST", "/api/learn", {})
    b = belief("no_weekend")
    assert b and b["confidence"] >= 0.7 and "10 weeks" in b["statement"] and "0 of 50" in b["statement"], \
        "behaviour 15: no_weekend belief, saw %r" % r["beliefs"]
    p = r["proposal"]
    assert p and p["status"] == "shown" and p["novelty"] == "invented", "behaviour 16: invented proposal, saw %r" % p
    assert "10 weeks" in p["reason"], "behaviour 16: reason cites number, saw %r" % p["reason"]
    assert p["belief_keys"] == ["no_weekend"] and "hidden_counts" in p["view"], "behaviour 16: shape"
    assert call("GET", "/api/state")["view"]["type"] == "week", "behaviour 16: view unchanged"
    assert call("GET", "/api/proposal")["proposal"]["id"] == p["id"], "behaviour 16: open proposal"
    assert call("POST", "/api/learn", {})["proposal"]["id"] == p["id"], "behaviour 16: exactly one open"
    conf = belief("no_weekend")["confidence"]
    r = call("POST", "/api/proposals/%s/accept" % p["id"], {})
    assert r["proposal"]["status"] == "accepted" and r["view"]["type"] == p["view"]["type"], "behaviour 17: accept"
    assert call("GET", "/api/state")["view"]["type"] == p["view"]["type"], "behaviour 17: state view"
    r = call("POST", "/api/proposals/%s/undo" % p["id"], {})
    assert r["proposal"]["status"] == "reverted" and r["view"]["type"] == "week", "behaviour 17: undo, saw %r" % r
    assert belief("no_weekend")["confidence"] < conf, "behaviour 17: belief drops"
    assert call("POST", "/api/learn", {})["proposal"] is None, "behaviour 17: not shown again"
    assert "error" in call("POST", "/api/proposals/zz/accept", {}), "behaviour 17: unknown -> error"
    reset(weekday_items())
    p = call("POST", "/api/learn", {})["proposal"]
    r = call("POST", "/api/proposals/%s/dismiss" % p["id"], {})
    assert r["proposal"]["status"] == "dismissed", "behaviour 17: dismiss"
    assert call("POST", "/api/learn", {})["proposal"] is None, "behaviour 17: dismissed not shown again"


def test_18_views():
    reset()
    r = call("POST", "/api/view", {"type": "workweek"})
    hc = r["view"]["hidden_counts"]
    assert r["view"]["type"] == "workweek" and hc.get("2026-10-24", 0) >= 1, "behaviour 18: saw %r" % hc
    st = call("GET", "/api/state")
    assert st["view"]["type"] == "workweek" and st["view"]["hidden_counts"].get("2026-10-24", 0) >= 1
    for t in ["month", "week", "day"]:
        assert call("POST", "/api/view", {"type": t})["view"]["hidden_counts"] == {}, "behaviour 18: %s hides nothing" % t


def meta_seed():
    items = weekday_items()
    for i, day in enumerate([5, 6, 7, 8, 9, 12, 13, 14]):
        items.append(it("e%d" % i, "Evening task", "2026-10-%02dT20:30" % day, "2026-10-%02dT21:30" % day, status="done"))
    items.append(it("mo", "Morning task", "2026-10-15T10:00", "2026-10-15T11:00", status="done"))
    return items


def test_19_meta():
    reset(meta_seed())
    prompts = call("GET", "/api/prompts")["prompts"]
    for a in ["designer", "followthrough", "learner"]:
        v = [p for p in prompts if p["agent"] == a]
        assert len(v) == 1 and v[0]["version"] == 1 and v[0]["active"] and len(v[0]["core_hash"]) == 12, \
            "behaviour 19: initial prompt for %s" % a
    p1 = call("POST", "/api/learn", {})["proposal"]
    assert p1 and p1["novelty"] == "invented", "behaviour 19: first invented, saw %r" % p1
    call("POST", "/api/proposals/%s/undo" % p1["id"], {})
    call("POST", "/api/clock", {"now": "2026-10-19T22:30"})
    p2 = call("POST", "/api/learn", {})["proposal"]
    assert p2 and p2["id"] != p1["id"] and p2["belief_keys"] != p1["belief_keys"], "behaviour 19: second, saw %r" % p2
    assert p2["novelty"] == "invented", "behaviour 19: second still invented"
    call("POST", "/api/proposals/%s/undo" % p2["id"], {})
    meta = call("GET", "/api/state")["meta"]
    assert meta["novelty"] < 0.6, "behaviour 19: novelty lowered, saw %r" % meta
    des = [p for p in call("GET", "/api/prompts")["prompts"] if p["agent"] == "designer"]
    assert len(des) == 2, "behaviour 19: designer v2, saw %d" % len(des)
    v1, v2 = des[0], des[1]
    assert v2["version"] == 2 and v2["active"] and not v1["active"], "behaviour 19: v2 active"
    assert v2["core_hash"] == v1["core_hash"] and v2["diff"] and v2["reason"], "behaviour 19: hash/diff/reason"
    assert v2["evidence"] and v2["replay"]["passed"] is True, "behaviour 19: evidence/replay"
    assert len(v2["learned"].splitlines()) <= 12, "behaviour 19: learned <= 12 lines"
    low = v2["learned"].lower()
    for w in ["ignore", "override", "core rules"]:
        assert w not in low, "behaviour 19: learned contains %s" % w
    ev = call("GET", "/api/activity")["events"]
    kinds = [e["kind"] for e in ev]
    for k in ["belief", "proposal", "prompt"]:
        assert k in kinds, "behaviour 20: activity kind %s missing, saw %r" % (k, kinds)
    pe = [e for e in ev if e["kind"] == "prompt"][0]
    assert v2["diff"].strip() in pe["text"], "behaviour 20: prompt event holds the diff"
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev), "behaviour 20: time order"
    for e in ev:
        for k in ["at", "agent", "kind", "text"]:
            assert k in e, "behaviour 20: event lacks %s" % k
    r = call("POST", "/api/prompts/designer/rollback", {})
    assert r["active"]["version"] == 1, "behaviour 19: rollback, saw %r" % r
    des = [p for p in call("GET", "/api/prompts")["prompts"] if p["agent"] == "designer"]
    assert [p["active"] for p in des] == [True, False], "behaviour 19: v1 active again"
    assert "rollback" in [e["kind"] for e in call("GET", "/api/activity")["events"]], "behaviour 20: rollback event"
    r = call("POST", "/api/prompts/designer/rollback", {})
    assert r["active"]["version"] == 1, "behaviour 19: v1 stays when only v1"


def test_ui():
    html = ""
    try:
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(app.__file__)), "index.html")
        if os.path.exists(p):
            html = open(p, encoding="utf-8").read()
    except Exception:
        html = ""
    if html:
        for i in ["chat-input", "chat-log", "focus", "timeline", "learned", "activity", "proposal", "view-switcher"]:
            assert i in html, "behaviour ui: id %s missing" % i


TESTS = [test_state_shape, test_1_typed, test_2_dump, test_3_complexity, test_4_breakdown, test_4b_revise,
         test_5_focus, test_6_reminders, test_7_postpone, test_8_checkin, test_8b_not_needed, test_8c_wrong_time,
         test_9_noguilt, test_10_crud, test_11_bulk, test_12_ics, test_12b_rrule, test_13_text_import,
         test_14_log, test_15_learner, test_15b_delete_belief, test_16_proposal, test_18_views, test_19_meta,
         test_ui]


def run():
    del AGENT[:]
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            raise AssertionError("%s: %s" % (t.__name__, e))
    for m in AGENT:
        low = m["text"].lower()
        for w in BANNED:
            assert w not in low, "behaviour 9: banned wording %r in agent text %r" % (w, m["text"])
    return True


if __name__ == "__main__":
    run()
    print("ok")
