"""Invariant checker on fake states (no app, no network): python -m pytest builds/usersim/sim_test.py -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402

NOW = "2026-10-19T19:00"


def item(i, title, start=None, status="open", source="seed", **kw):
    return {"id": i, "kind": "task", "title": title, "start": start, "end": None, "window": None, "status": status,
            "source": source, **kw}


def msg(i, kind, text, item_id=None, undo_id=None, steps=None):
    m = {"id": i, "role": "agent", "kind": kind, "text": text, "item_id": item_id, "undo_id": undo_id}
    if steps is not None:
        m["steps"] = steps
    return m


def state(items, messages=(), view=None):
    return {"now": NOW, "items": list(items), "messages": list(messages), "view": view or {"type": "week"}}


OK = {"status": 200, "secs": 0.1, "json": {}}


def kinds(viols):
    return sorted(v["invariant"] for v in viols)


def test_clean_step_passes():
    c = sim.Checker()
    a = item("a", "call mom")
    v = c.step({"action": "command"}, state([a]), state([a], [msg("m1", "reply", "Added call mom for tonight.")]), OK)
    assert v == [] and c.summary()["pass_rate"] == 1.0


def test_guilt_and_questions():
    c = sim.Checker()
    a = item("a", "yoga")
    v = c.step({"action": "clock"}, state([a]),
               state([a], [msg("m1", "postponed", "You missed yoga, it is overdue. Move it? Or drop it?", "a")]), OK)
    assert kinds(v) == ["no_guilt", "one_question"]
    # "later" is not "late"
    v = c.step({"action": "clock"}, state([a]), state([a], [msg("m2", "postponed", "Moved yoga to later tonight.", "a")]), OK)
    assert v == []


def test_breakdown_shape():
    c = sim.Checker()
    good = [{"title": "Open a doc", "estimate_min": 10}, {"title": "Write intro", "estimate_min": 45}]
    bad_first = [{"title": "Build site", "estimate_min": 60}, {"title": "Ship", "estimate_min": 10}]
    v = c.step({"action": "chat"}, state([]), state([], [msg("b1", "breakdown", "Steps:", steps=good)]), OK)
    assert v == []
    v = c.step({"action": "chat"}, state([]), state([], [msg("b2", "breakdown", "Steps:", steps=bad_first)]), OK)
    assert kinds(v) == ["breakdown_shape"]
    v = c.step({"action": "chat"}, state([]), state([], [msg("b3", "breakdown", "Steps:", steps=good[:1])]), OK)
    assert kinds(v) == ["breakdown_shape"]


def test_bulk_delete_needs_preview():
    a, b = item("a", "hike", "2026-10-24T10:00"), item("b", "brunch", "2026-10-25T11:00")
    c = sim.Checker()
    v = c.step({"action": "command", "text": "clear my weekend"}, state([a, b]), state([]), OK)
    assert kinds(v) == ["delete_preview"]
    # a preview that changes nothing is fine; a preview that already deleted is not
    plan = {"id": "p1", "status": "preview", "affected": ["a", "b"],
            "ops": [{"op": "delete", "item_id": "a", "before": a}, {"op": "delete", "item_id": "b", "before": b}]}
    c = sim.Checker()
    assert c.step({"action": "command"}, state([a, b]), state([a, b]), {**OK, "json": {"plan": plan}}) == []
    c2 = sim.Checker()
    assert kinds(c2.step({"action": "command"}, state([a, b]), state([b]), {**OK, "json": {"plan": plan}})) \
        == ["delete_preview"]
    # apply removes exactly the planned deletes: fine
    assert c.step({"action": "plan", "op": "apply", "plan_id": "p1"}, state([a, b]), state([]),
                  {**OK, "json": {"plan": {**plan, "status": "applied"}}}) == []


def test_single_delete_of_imported_needs_preview():
    a = item("a", "school play", "2026-10-21T18:00", source="ics")
    c = sim.Checker()
    assert kinds(c.step({"action": "command", "text": "cancel school play"}, state([a]), state([]), OK)) == \
        ["delete_preview"]


def test_lost_item_on_clock():
    a = item("a", "yoga", "2026-10-19T18:00")
    c = sim.Checker(seed_ids=["a"])
    assert kinds(c.step({"action": "clock"}, state([a]), state([]), OK)) == ["no_lost_item"]


def test_undo_exact():
    before = item("a", "yoga", "2026-10-19T18:00")
    moved = {**before, "start": "2026-10-20T20:00", "postpone_count": 1}
    c = sim.Checker(seed_ids=["a"])
    m = msg("m1", "postponed", "Moved yoga to tomorrow 8 PM.", "a", undo_id="u1")
    assert c.step({"action": "clock"}, state([before]), state([moved], [m]), OK) == []
    # wrong restore
    half = {**before, "start": "2026-10-19T19:00"}
    assert kinds(c.step({"action": "undo", "undo_id": "u1"}, state([moved], [m]), state([half], [m]), OK)) == \
        ["undo_exact"]
    c2 = sim.Checker(seed_ids=["a"])
    c2.step({"action": "clock"}, state([before]), state([moved], [m]), OK)
    assert c2.step({"action": "undo", "undo_id": "u1"}, state([moved], [m]), state([before], [m]), OK) == []


def test_proposal_reason_number():
    c = sim.Checker()
    p = {"id": "p1", "title": "Hide weekends?", "reason": "You never plan weekends."}
    assert kinds(c.step({"action": "learn"}, state([]), state([]), OK, proposal_after=p)) == ["proposal_number"]
    p2 = {"id": "p2", "title": "Hide weekends?", "reason": "No weekend plans in 10 weeks."}
    assert c.step({"action": "learn"}, state([]), state([]), OK, proposal_after=p2) == []


def test_hidden_items_workweek_and_custom():
    sat = item("s", "hike", "2026-10-24T10:00")
    ww = {"type": "workweek", "hidden_counts": {}}
    c = sim.Checker()
    assert kinds(c.step({"action": "view"}, state([sat]), state([sat], view=ww), OK)) == ["no_hidden_item"]
    c = sim.Checker()
    assert c.step({"action": "view"}, state([sat]),
                  state([sat], view={**ww, "hidden_counts": {"2026-10-24": 1}}), OK) == []
    # custom: an evening lane on Monday hides a 10:00 task unless counted
    morning = item("m", "dentist", "2026-10-19T10:00")
    spec = {"root": "r", "elements": {
        "r": {"type": "Lane", "props": {"label": "evening", "filter": {"hour_from": 18}}, "children": ["d"]},
        "d": {"type": "DayTimeline", "props": {"date": "2026-10-19", "from_hour": 0, "to_hour": 24}, "children": []}}}
    c = sim.Checker()
    v = c.step({"action": "proposal"}, state([morning]),
               state([morning], view={"type": "custom", "spec": spec, "hidden_counts": {}}), OK)
    assert kinds(v) == ["no_hidden_item"]


def test_names_task_and_http():
    a = item("a", "pay phone bill", "2026-10-19T18:00")
    c = sim.Checker()
    v = c.step({"action": "clock"}, state([a]), state([a], [msg("r1", "reminder", "Time for your next thing.", "a")]),
               {"status": 500, "secs": 0.2, "json": None})
    assert kinds(v) == ["http", "names_task"]
    v = c.step({"action": "clock"}, state([a]), state([a], [msg("r2", "reminder", "Time to pay the phone bill.", "a")]),
               {"status": 200, "secs": 61, "json": {}})
    assert kinds(v) == ["http"]


def test_dump_fragments_lost_or_mangled():
    c = sim.Checker()
    text = "call mom, find a template, dentist thursday 3pm"
    good = [item("x1", "Call mom", source="dump"), item("x2", "Find a template", source="dump"),
            item("x3", "Dentist", "2026-10-22T15:00", source="dump")]
    assert c.step({"action": "dump", "text": text}, state([]), state(good), OK) == []
    mangled = [good[0], item("x2", "Find a tempat another time", source="dump"), good[2]]
    c = sim.Checker()
    v = c.step({"action": "dump", "text": text}, state([]), state(mangled), OK)
    assert kinds(v) == ["dump_fragments"] and "template" in v[0]["detail"]


def test_clock_followthrough():
    t = item("t", "pay phone bill", "2026-10-19T19:30", end="2026-10-19T20:00")
    stale = item("s", "call the dentist", "2026-10-19T17:00", end="2026-10-19T17:15")
    b = {**state([t, stale]), "now": "2026-10-19T19:00"}
    a = {**state([t, stale]), "now": "2026-10-19T19:45"}
    c = sim.Checker()
    v = c.step({"action": "clock"}, b, a, OK)
    assert kinds(v) == ["clock_followthrough", "clock_followthrough"]
    moved = {**stale, "start": "2026-10-20T20:00", "end": "2026-10-20T20:15"}
    a2 = {**state([t, moved], [msg("r", "reminder", "Time for pay phone bill.", "t"),
                               msg("p", "postponed", "Moved call the dentist to tomorrow 8 PM.", "s", "u")]),
          "now": "2026-10-19T19:45"}
    c = sim.Checker()
    assert c.step({"action": "clock"}, b, a2, OK) == []


def test_needs_yes_offtopic_reply():
    g = item("g", "get my portfolio together")
    offer = msg("q1", "question", "“get my portfolio together” sounds like a big one. Break it into tiny steps?", "g")
    c = sim.Checker()
    assert c.step({"action": "command"}, state([]), state([g], [offer]), OK) == []
    why = msg("q2", "question", "Lovely. First, why does “get my portfolio together” matter to you?", "g")
    v = c.step({"action": "chat", "item_id": "g", "text": "I want to learn to swim in the next 3 months."},
               state([g], [offer]), state([g], [offer, why]), OK)
    assert kinds(v) == ["needs_yes"]
    # a real yes is fine
    c = sim.Checker()
    c.step({"action": "command"}, state([]), state([g], [offer]), OK)
    assert c.step({"action": "chat", "item_id": "g", "text": "Yes please"}, state([g], [offer]),
                  state([g], [offer, why]), OK) == []


def test_time_phrase_item():
    c = sim.Checker()
    swim, tail = item("a", "learn to swim", source="dump"), item("b", "in the next 3 months", source="dump")
    v = c.step({"action": "dump", "text": "I want to learn to swim in the next 3 months."}, state([]),
               state([swim, tail]), OK)
    assert kinds(v) == ["time_only_item"]
    c = sim.Checker()
    assert c.step({"action": "dump", "text": "learn to swim in the next 3 months"}, state([]),
                  state([item("c", "learn to swim in the next 3 months")]), OK) == []


def test_needs_yes_counts_breakdown_requests_as_yes():
    g = item("g", "sketches")
    offer = msg("q1", "question", "Want me to break 'sketches' into small steps?", "g")
    why = msg("q2", "question", "Why does this matter to you?", "g")
    for text in ["maybe, could you break it into a tiny first step?", "make step 2 smaller"]:
        c = sim.Checker()
        c.step({"action": "command"}, state([]), state([g], [offer]), OK)
        assert c.step({"action": "chat", "item_id": "g", "text": text}, state([g], [offer]),
                      state([g], [offer, why]), OK) == []
