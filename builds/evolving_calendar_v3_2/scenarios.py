"""Frozen scenarios for the Evolving Calendar v3.1: v3 fit + 12 harder fit scenarios (fit2_*); HOLDOUT identical to v3.

Each scenario: {"id", "persona", "description", "steps": [step, ...]}.
A step is {"op": <name>, "label": <unique check prefix>, ...params}; harness.py interprets the ops:

  reset      now, items                      -> POST /api/reset with seed items
  ui         -                               -> GET / has ids chat-input, chat-log, focus, timeline
  command    text, mode, as?, expect{...}    -> POST /api/command, checks on created items / messages
  decline    goal, text                      -> person says no to the complexity question; nothing broken down
  breakdown  goal, reason, reason_kws, done, deadline_text, deadline, revise?, focus?
                                              -> adaptive chat driver (<= 6 turns) + breakdown checks
  focus      expect_item, expect_parent, why_kws
  done       item                            -> POST /api/items/<id>/done
  reminders  item, kw, start, end, done_item -> clock walk around start / end-10min
  postpone   item, kw, quiet_until, after, next_day, min_hour, undo
  checkin    item, kw, reply                 -> three consecutive postponements -> checkin
  v2: log, move, cancel, query, bulk, ics, text_import, learn, counter, delete_belief, proposal, view, meta,
      activity (see harness.py). Item refs "$name" point at ids saved by an earlier step ("as").
Scenario flags: "mock": True runs the app with EVOLVE_MOCK=1 and no CRUSOE keys; "final": False skips the
final no-guilt / well-formed message checks (scenarios with no agent conversation).

Dates: 2026-10-19 is a Monday. "fit" descriptions are shown to the team; "holdout" is never shown.
"""


def _history(days, slots, prefix="h"):
    """Done tasks on previous days, to learn when this person usually gets things done."""
    titles = ["Answer emails", "Sort photos", "Pay phone bill", "Read two chapters", "Tidy desk",
              "Update budget sheet", "Reply to Ana", "Fold laundry"]
    items, k = [], 0
    for day in days:
        for (s, e) in slots:
            items.append({"id": f"{prefix}{k}", "kind": "task", "title": titles[k % len(titles)],
                          "start": f"{day}T{s}", "end": f"{day}T{e}", "status": "done", "source": "seed"})
            k += 1
    return items


IRIS_DAYS = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
IRIS_HISTORY = _history(IRIS_DAYS, [("20:15", "20:45"), ("21:00", "21:40")])
JORDAN_DAYS = ["2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]
JORDAN_HISTORY = _history(JORDAN_DAYS, [("18:00", "18:40"), ("18:45", "19:30")], prefix="j")
SAM_HISTORY = _history(["2026-10-18", "2026-10-19", "2026-10-20"], [("19:00", "19:45")], prefix="s")


FIT = [
    {
        "id": "fit_typed_command",
        "persona": "Iris",
        "description": "Typed commands: 'dentist thursday 3pm' becomes one event on Thursday 15:00; "
                       "'lunch with Ana around noon tomorrow' keeps a soft window; GET / has the four UI ids.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY},
            {"op": "ui", "label": "ui"},
            {"op": "command", "label": "dentist", "text": "dentist thursday 3pm", "mode": "command",
             "as": "dentist", "as_kw": "dentist",
             "expect": {"n_created": 1, "event": {"kw": "dentist", "start": "2026-10-22T15:00"}}},
            {"op": "log", "label": "log", "expect": [{"kind": "create", "item": "$dentist"}]},
            {"op": "command", "label": "lunch", "text": "lunch with Ana around noon tomorrow", "mode": "command",
             "expect": {"n_created": 1,
                        "window": {"kw": "lunch", "date": "2026-10-20", "hours": [11, 13]}}},
        ],
    },
    {
        "id": "fit_brain_dump",
        "persona": "Iris",
        "description": "Brain dump 'call mom, portfolio, maybe yoga?, dentist thursday 3pm': one item per "
                       "fragment, dentist is a Thursday 15:00 event, at most one question back.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY},
            {"op": "command", "label": "dump", "text": "call mom, portfolio, maybe yoga?, dentist thursday 3pm",
             "mode": "dump",
             "expect": {"min_created": 4, "fragments": ["mom", "portfolio", "yoga", "dentist"],
                        "event": {"kw": "dentist", "start": "2026-10-22T15:00"}, "max_questions": 1}},
        ],
    },
    {
        "id": "fit_complexity_question",
        "persona": "Iris",
        "description": "Vague/big tasks ('get my portfolio together', 'work on the pitch deck') each get exactly "
                       "one question about breaking them down; after a 'no' nothing is broken down.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T10:00", "items": IRIS_HISTORY},
            {"op": "command", "label": "portfolio", "text": "get my portfolio together", "mode": "command",
             "as": "g1", "expect": {"complexity": {"kw": "portfolio"}}},
            {"op": "command", "label": "pitch", "text": "work on the pitch deck", "mode": "command",
             "as": "g2", "expect": {"complexity": {"kw": "pitch"}}},
            {"op": "decline", "label": "decline", "goal": "g2", "text": "no thanks, not right now"},
        ],
        "mock": True,
    },
    {
        "id": "fit_breakdown_chat",
        "persona": "Iris",
        "description": "Say yes to breaking down 'get my portfolio together': why first, <=3 questions, then 2-5 "
                       "verb-first steps (first <=15 min); 'looks good' makes child tasks before Friday; focus shows step 1.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T10:00", "items": IRIS_HISTORY},
            {"op": "command", "label": "goal", "text": "get my portfolio together", "mode": "command",
             "as": "g", "expect": {"complexity": {"kw": "portfolio"}}},
            {"op": "breakdown", "label": "chat", "goal": "g",
             "reason": "I want to apply for design jobs and feel proud of my work again.",
             "reason_kws": ["design", "proud", "job", "apply"],
             "done": "Five finished projects on a simple website I can send to people.",
             "deadline_text": "By Friday, October 23.", "deadline": "2026-10-23", "focus": True},
        ],
    },
    {
        "id": "fit_focus",
        "persona": "Iris",
        "description": "Focus card: the earliest open step of the goal that has a why (not an earlier plain task), "
                       "its parent, the why and 'Start 5 minutes'; after marking it done focus moves to the next step.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY + [
                {"id": "g1", "kind": "task", "title": "Run a half marathon", "why": "to feel strong in my body again",
                 "status": "open", "source": "seed"},
                {"id": "s0", "kind": "task", "title": "Buy running socks", "parent_id": "g1", "status": "done",
                 "start": "2026-10-18T20:00", "end": "2026-10-18T20:15", "estimate_min": 15, "source": "seed"},
                {"id": "s1", "kind": "task", "title": "Lay out running shoes", "parent_id": "g1", "status": "open",
                 "start": "2026-10-19T20:00", "end": "2026-10-19T20:10", "estimate_min": 10, "source": "seed"},
                {"id": "s2", "kind": "task", "title": "Jog for 10 minutes", "parent_id": "g1", "status": "open",
                 "start": "2026-10-20T20:00", "end": "2026-10-20T20:15", "estimate_min": 15, "source": "seed"},
                {"id": "t1", "kind": "task", "title": "Buy stamps", "status": "open",
                 "start": "2026-10-19T12:00", "end": "2026-10-19T12:15", "source": "seed"},
            ]},
            {"op": "focus", "label": "focus1", "expect_item": "s1", "expect_parent": "g1", "why_kws": ["strong"]},
            {"op": "done", "label": "done_s1", "item": "s1"},
            {"op": "focus", "label": "focus2", "expect_item": "s2", "expect_parent": "g1", "why_kws": ["strong"]},
            {"op": "log", "label": "log", "expect": [{"kind": "done", "item": "s1"}]},
        ],
    },
    {
        "id": "fit_reminders",
        "persona": "Iris",
        "description": "Event 'Call with Priya' 20:00-21:00: one reminder when the clock passes 20:00, one when it "
                       "passes 20:50, no duplicates; a done item gets no reminder.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T19:00", "items": IRIS_HISTORY + [
                {"id": "e1", "kind": "event", "title": "Call with Priya", "start": "2026-10-19T20:00",
                 "end": "2026-10-19T21:00", "status": "open", "source": "seed"},
                {"id": "d1", "kind": "task", "title": "Water the plants", "start": "2026-10-19T20:05",
                 "end": "2026-10-19T20:20", "status": "done", "source": "seed"},
            ]},
            {"op": "reminders", "label": "rem", "item": "e1", "kw": "priya", "start": "2026-10-19T20:00",
             "end": "2026-10-19T21:00", "done_item": "d1"},
            {"op": "log", "label": "log", "expect": [{"kind": "reminder", "item": "e1", "min": 2}]},
        ],
        "mock": True,
    },
    {
        "id": "fit_postpone_undo",
        "persona": "Iris",
        "description": "Missed task 'Write cover letter' (14:00-15:00) moves quietly >30 min after its end to "
                       "tomorrow in a free evening slot (Iris finishes after 8 PM), message names task+time with undo_id; undo restores it.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T13:00", "items": IRIS_HISTORY + [
                {"id": "p1", "kind": "task", "title": "Write cover letter", "start": "2026-10-19T14:00",
                 "end": "2026-10-19T15:00", "status": "open", "source": "seed"},
                {"id": "b1", "kind": "event", "title": "Book club", "start": "2026-10-20T20:00",
                 "end": "2026-10-20T21:30", "status": "open", "source": "seed"},
            ]},
            {"op": "postpone", "label": "pp", "item": "p1", "kw": "cover letter", "quiet_until": "2026-10-19T15:20",
             "after": "2026-10-19T15:45", "next_day": "2026-10-20", "min_hour": 17, "undo": True},
            {"op": "log", "label": "log", "expect": [{"kind": "postponed", "item": "p1"}]},
        ],
        "mock": True,
    },
    {
        "id": "fit_checkin",
        "persona": "Iris",
        "description": "'Clean out the garage' is postponed twice; on the third miss the agent sends a checkin with a "
                       "question offering smaller steps; replying 'too big' starts a breakdown.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY + [
                {"id": "c1", "kind": "task", "title": "Clean out the garage", "start": "2026-10-19T10:00",
                 "end": "2026-10-19T11:00", "status": "open", "source": "seed"},
            ]},
            {"op": "checkin", "label": "ci", "item": "c1", "kw": "garage", "reply": "honestly it feels too big"},
            {"op": "log", "label": "log", "expect": [{"kind": ["chat", "checkin_answer"], "item": "c1"}]},
        ],
    },
]


HOLDOUT = [
    {
        "id": "holdout_sam_commands",
        "persona": "Sam",
        "description": "Sam: 'haircut friday 11am' -> Friday 11:00 event; 'groceries sometime this evening' keeps a "
                       "window; dump 'pack books, call landlord, maybe donate couch?, movers saturday 9am'.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T08:30", "items": SAM_HISTORY},
            {"op": "ui", "label": "ui"},
            {"op": "command", "label": "haircut", "text": "haircut friday 11am", "mode": "command",
             "expect": {"n_created": 1, "event": {"kw": "haircut", "start": "2026-10-23T11:00"}}},
            {"op": "command", "label": "groceries", "text": "groceries sometime this evening", "mode": "command",
             "expect": {"n_created": 1, "window": {"kw": "grocer", "date": "2026-10-21", "hours": [16, 23]}}},
            {"op": "command", "label": "dump", "text": "pack books, call landlord, maybe donate couch?, movers saturday 9am",
             "mode": "dump",
             "expect": {"min_created": 4, "fragments": ["books", "landlord", "couch", "movers"],
                        "event": {"kw": "movers", "start": "2026-10-24T09:00"}, "max_questions": 1}},
        ],
        "mock": True,
    },
    {
        "id": "holdout_sam_breakdown",
        "persona": "Sam",
        "description": "Sam: 'figure out moving' -> one complexity question, yes, why first, steps, asks for a "
                       "smaller first step and gets a revised breakdown, accepts; children before Oct 30; focus.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T12:00", "items": SAM_HISTORY},
            {"op": "command", "label": "goal", "text": "figure out moving", "mode": "command",
             "as": "g", "expect": {"complexity": {"kw": "mov"}}},
            {"op": "breakdown", "label": "chat", "goal": "g",
             "reason": "So the kids can start at their new school calm and settled.",
             "reason_kws": ["kid", "school", "calm", "settled"],
             "done": "Everything packed into boxes and the truck booked.",
             "deadline_text": "By October 30.", "deadline": "2026-10-30", "revise": True, "focus": True},
        ],
    },
    {
        "id": "holdout_jordan_postpone",
        "persona": "Jordan",
        "description": "Jordan (gets things done 18:00-19:30): 'Collect tax receipts' 09:30-10:15 missed -> next "
                       "day evening free slot of 45 min, kind message with undo; undo restores.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00", "items": JORDAN_HISTORY + [
                {"id": "x1", "kind": "task", "title": "Collect tax receipts", "start": "2026-10-20T09:30",
                 "end": "2026-10-20T10:15", "status": "open", "source": "seed"},
                {"id": "x2", "kind": "event", "title": "Dinner with Lee", "start": "2026-10-21T18:00",
                 "end": "2026-10-21T19:00", "status": "open", "source": "seed"},
            ]},
            {"op": "postpone", "label": "pp", "item": "x1", "kw": "receipt", "quiet_until": "2026-10-20T10:40",
             "after": "2026-10-20T11:00", "next_day": "2026-10-21", "min_hour": 17, "undo": True},
            {"op": "log", "label": "log", "expect": [{"kind": "postponed", "item": "x1"}]},
        ],
        "mock": True,
    },
    {
        "id": "holdout_jordan_taxes",
        "persona": "Jordan",
        "description": "Jordan: 'deal with taxes' -> one complexity question, 'no' keeps it whole; reminders for "
                       "'Standup with Lee' 18:30-19:15; 'Renew passport' missed three times -> checkin.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T17:30", "items": JORDAN_HISTORY + [
                {"id": "e1", "kind": "event", "title": "Standup with Lee", "start": "2026-10-20T18:30",
                 "end": "2026-10-20T19:15", "status": "open", "source": "seed"},
                {"id": "d1", "kind": "event", "title": "Gym class", "start": "2026-10-20T18:40",
                 "end": "2026-10-20T19:10", "status": "done", "source": "seed"},
                {"id": "c1", "kind": "task", "title": "Renew passport", "start": "2026-10-20T21:00",
                 "end": "2026-10-20T21:30", "status": "open", "source": "seed"},
            ]},
            {"op": "command", "label": "taxes", "text": "deal with taxes", "mode": "command",
             "as": "g", "expect": {"complexity": {"kw": "tax"}}},
            {"op": "decline", "label": "decline", "goal": "g", "text": "nah, leave it as it is"},
            {"op": "reminders", "label": "rem", "item": "e1", "kw": "lee", "start": "2026-10-20T18:30",
             "end": "2026-10-20T19:15", "done_item": "d1"},
            {"op": "checkin", "label": "ci", "item": "c1", "kw": "passport", "reply": "it's too big for one go"},
            {"op": "log", "label": "log", "expect": [{"kind": "reminder", "item": "e1", "min": 2},
                                                     {"kind": ["chat", "checkin_answer"], "item": "c1"}]},
        ],
    },
]



# ---------------------------------------------------------------- v2 seeds

def _evening_history(days, prefix, slot=("20:15", "20:50"), morning=("2026-10-12", "09:00", "09:30")):
    """len(days) done tasks finishing after 20:00, plus one morning one."""
    items = _history(days, [slot], prefix=prefix)
    items.append({"id": f"{prefix}m", "kind": "task", "title": "Return library book", "start": f"{morning[0]}T{morning[1]}",
                  "end": f"{morning[0]}T{morning[2]}", "status": "done", "source": "seed"})
    return items


def _weekday_history(prefix, hour="10:00", end="10:45", weeks=10, first_monday="2026-08-10"):
    """`weeks` weeks of done items on Monday-Friday only (none on a weekend)."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(first_monday)
    titles = ["Team sync", "Write report", "Client call", "Review notes", "Plan sprint"]
    items = []
    for w in range(weeks):
        for k in range(5):
            day = (d0 + timedelta(days=7 * w + k)).isoformat()
            items.append({"id": f"{prefix}{w}_{k}", "kind": "task", "title": titles[k], "start": f"{day}T{hour}",
                          "end": f"{day}T{end}", "status": "done", "source": "seed"})
    return items


EIGHT_DAYS = ["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-13", "2026-10-14", "2026-10-15",
              "2026-10-16"]
EVENING_RE = r"evening|night|after 8|8 ?pm|20:00|after 20|late"
WEEKEND_RE = r"weekend|saturday|sunday"


def _ics(events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sea harness//EN"]
    for ev in events:
        lines += ["BEGIN:VEVENT"] + ev + ["END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


ICS_FIT = _ics([
    ["UID:fit-1@harness", "SUMMARY:Physio appointment", "DTSTART;TZID=America/Los_Angeles:20261022T091500",
     "DTEND;TZID=America/Los_Angeles:20261022T100000"],
    ["UID:fit-2@harness", "SUMMARY:Ana's birthday", "DTSTART;VALUE=DATE:20261024", "DTEND;VALUE=DATE:20261025"],
    ["UID:fit-3@harness", "SUMMARY:Pottery class", "DTSTART:20261020T190000", "DTEND:20261020T203000",
     "RRULE:FREQ=WEEKLY;COUNT=4"],
])
ICS_HOLDOUT = _ics([
    ["UID:ho-1@harness", "SUMMARY:Dentist cleaning", "DTSTART;TZID=America/Los_Angeles:20261023T084500",
     "DTEND;TZID=America/Los_Angeles:20261023T093000", "LOCATION:Main St"],
    ["UID:ho-2@harness", "SUMMARY:Moving day", "DTSTART;VALUE=DATE:20261031", "DTEND;VALUE=DATE:20261101"],
    ["UID:ho-3@harness", "SUMMARY:Spanish lesson", "DTSTART:20261027T183000", "DTEND:20261027T193000",
     "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261117T235959"],
])

FIT += [
    {
        "id": "fit_typed_crud",
        "persona": "Iris",
        "description": "'what's on thursday?' names every Thursday item and changes nothing; 'move my dentist to next "
                       "week' moves exactly that event +7 days; 'cancel yoga' is a preview plan until applied; workweek "
                       "view counts the hidden Saturday item. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY + [
                {"id": "den", "kind": "event", "title": "Dentist", "start": "2026-10-22T15:00",
                 "end": "2026-10-22T16:00", "status": "open", "source": "seed"},
                {"id": "tl", "kind": "event", "title": "Team lunch", "start": "2026-10-22T12:00",
                 "end": "2026-10-22T13:00", "status": "open", "source": "seed"},
                {"id": "yo", "kind": "event", "title": "Yoga class", "start": "2026-10-21T18:00",
                 "end": "2026-10-21T19:00", "status": "open", "source": "seed"},
                {"id": "fm", "kind": "event", "title": "Farmers market", "start": "2026-10-24T09:00",
                 "end": "2026-10-24T10:00", "status": "open", "source": "seed"},
            ]},
            {"op": "query", "label": "query", "text": "what's on thursday?", "kws": ["dentist", "lunch"]},
            {"op": "move", "label": "move", "text": "move my dentist to next week", "item": "den",
             "start": "2026-10-29T15:00"},
            {"op": "cancel", "label": "cancel", "text": "cancel yoga", "item": "yo"},
            {"op": "view", "label": "view", "type": "workweek", "hidden_date": "2026-10-24"},
            {"op": "log", "label": "log", "expect": [{"kind": "update", "item": "den"},
                                                     {"kind": ["delete", "plan_applied"], "item": "yo"},
                                                     {"kind": "query"}]},
        ],
    },
    {
        "id": "fit_bulk_trip",
        "persona": "Iris",
        "description": "'empty out my weekend for a business trip' -> preview plan: deletes exactly the 3 weekend "
                       "items + one trip create; apply removes/adds; undo restores every original field. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY + [
                {"id": "fm", "kind": "event", "title": "Farmers market", "start": "2026-10-24T09:00",
                 "end": "2026-10-24T10:00", "status": "open", "source": "seed"},
                {"id": "hk", "kind": "event", "title": "Hike with Leo", "start": "2026-10-24T14:00",
                 "end": "2026-10-24T17:00", "status": "open", "source": "seed"},
                {"id": "br", "kind": "event", "title": "Brunch with Mo", "start": "2026-10-25T11:00",
                 "end": "2026-10-25T12:30", "status": "open", "source": "seed"},
                {"id": "su1", "kind": "event", "title": "Standup", "start": "2026-10-23T09:00",
                 "end": "2026-10-23T09:15", "status": "open", "source": "seed"},
                {"id": "su2", "kind": "event", "title": "Standup", "start": "2026-10-26T09:00",
                 "end": "2026-10-26T09:15", "status": "open", "source": "seed"},
            ]},
            {"op": "bulk", "label": "bulk", "text": "empty out my weekend for a business trip",
             "affected": ["fm", "hk", "br"], "new_kw": ["trip", "business"]},
            {"op": "log", "label": "log", "expect": [{"kind": "plan_applied"}, {"kind": "plan_undone"}]},
        ],
    },
    {
        "id": "fit_ics_import",
        "persona": "Iris",
        "description": "Import an .ics with a TZID event, an all-day event and a weekly RRULE: preview plan of creates "
                       "(source ics), nothing changes before apply, wall-clock times kept; re-import gives 0 creates.",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY},
            {"op": "ics", "label": "ics", "filename": "calendar.ics", "content": ICS_FIT, "n": 3,
             "titles": ["physio", "birthday", "pottery"],
             "tz": {"kw": "physio", "start": "2026-10-22T09:15"},
             "allday": {"kw": "birthday", "date": "2026-10-24", "last": "2026-10-25"},
             "rrule": {"kw": "pottery", "dates": ["2026-10-20", "2026-10-27", "2026-11-03", "2026-11-10"]}},
            {"op": "log", "label": "log", "expect": [{"kind": ["import", "plan_applied"]}]},
        ],
    },
    {
        "id": "fit_text_import",
        "persona": "Iris",
        "description": "Text import 'I'll be away this weekend, please clear it. Also lunch with Ana on Wednesday "
                       "around noon.' -> preview with a delete/update and a create; apply; undo restores.",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": IRIS_HISTORY + [
                {"id": "gc", "kind": "task", "title": "Garden cleanup", "start": "2026-10-24T10:00",
                 "end": "2026-10-24T12:00", "status": "open", "source": "seed"},
                {"id": "wk", "kind": "event", "title": "Book club", "start": "2026-10-20T19:00",
                 "end": "2026-10-20T20:00", "status": "open", "source": "seed"},
            ]},
            {"op": "text_import", "label": "txt", "filename": "note.txt",
             "content": "I'll be away this weekend, please clear it. Also lunch with Ana on Wednesday around noon.",
             "cleared": ["gc"], "new_kw": ["lunch", "ana"]},
            {"op": "log", "label": "log", "expect": [{"kind": ["import", "plan_applied"]}]},
        ],
    },
    {
        "id": "fit_habit_learner",
        "persona": "Iris",
        "description": "8 done tasks after 20:00 and 1 before -> evening habit belief >= 0.7 whose statement has '8', "
                       "valid evidence; morning done tasks lower it; a deleted belief is not re-learned. [mock mode]",
        "mock": True,
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T06:30",
             "items": _evening_history(EIGHT_DAYS, "ev") + [
                 {"id": "mo1", "kind": "task", "title": "Stretch", "start": "2026-10-19T07:40",
                  "end": "2026-10-19T07:50", "status": "open", "source": "seed"},
                 {"id": "mo2", "kind": "task", "title": "Reply to Kim", "start": "2026-10-19T07:50",
                  "end": "2026-10-19T08:00", "status": "open", "source": "seed"},
                 {"id": "mo3", "kind": "task", "title": "Pay rent", "start": "2026-10-19T08:00",
                  "end": "2026-10-19T08:05", "status": "open", "source": "seed"},
             ]},
            {"op": "learn", "label": "learn", "as": "eve", "belief": {"re": EVENING_RE, "num": "8", "min_conf": 0.7}},
            {"op": "counter", "label": "counter", "belief": "eve", "clock": "2026-10-19T08:05",
             "done": ["mo1", "mo2", "mo3"]},
            {"op": "delete_belief", "label": "del", "belief": "eve"},
        ],
    },
    {
        "id": "fit_proposal",
        "persona": "Iris",
        "description": "Little history -> no proposal; 10 weeks with nothing on weekends -> no-weekend belief and one "
                       "view proposal citing its number; view unchanged until accept; accept/undo; not shown again. [mock mode]",
        "mock": True,
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset_sparse", "now": "2026-10-19T09:00", "items": [
                {"id": "q1", "kind": "task", "title": "Buy milk", "start": "2026-10-17T11:00", "end": "2026-10-17T11:15",
                 "status": "done", "source": "seed"},
                {"id": "q2", "kind": "task", "title": "Call bank", "start": "2026-10-16T21:00", "end": "2026-10-16T21:20",
                 "status": "done", "source": "seed"}]},
            {"op": "learn", "label": "sparse", "no_proposal": True},
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00", "items": _weekday_history("w")},
            {"op": "learn", "label": "learn", "as": "wkd", "belief": {"re": WEEKEND_RE, "num": "10", "min_conf": 0.7}},
            {"op": "proposal", "label": "prop", "flow": "accept_undo"},
        ],
    },
    {
        "id": "fit_meta_learner",
        "persona": "Iris",
        "description": "Revert two 'invented' proposals -> meta.novelty drops, a new designer prompt version (same "
                       "core_hash, diff, reason, evidence, replay passed), next proposal classic, rollback works; activity feed.",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T09:00",
             "items": _weekday_history("m", hour="20:30", end="21:15")},
            {"op": "meta", "label": "meta"},
            {"op": "activity", "label": "activity"},
        ],
    },
]

HOLDOUT += [
    {
        "id": "holdout_jordan_crud_bulk",
        "persona": "Jordan",
        "description": "Jordan: 'anything on friday?', 'push my haircut to next week', 'drop the gym class', workweek "
                       "hides Saturday, 'I'm going camping this weekend, clear everything'. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00", "items": JORDAN_HISTORY + [
                {"id": "hc", "kind": "event", "title": "Haircut", "start": "2026-10-23T16:00",
                 "end": "2026-10-23T16:45", "status": "open", "source": "seed"},
                {"id": "gym", "kind": "event", "title": "Gym class", "start": "2026-10-22T07:00",
                 "end": "2026-10-22T08:00", "status": "open", "source": "seed"},
                {"id": "lee", "kind": "event", "title": "Call with Lee", "start": "2026-10-23T11:00",
                 "end": "2026-10-23T11:30", "status": "open", "source": "seed"},
                {"id": "soc", "kind": "event", "title": "Soccer practice", "start": "2026-10-24T10:00",
                 "end": "2026-10-24T11:30", "status": "open", "source": "seed"},
                {"id": "din", "kind": "event", "title": "Dinner at Mia's", "start": "2026-10-25T19:00",
                 "end": "2026-10-25T21:00", "status": "open", "source": "seed"},
                {"id": "sup", "kind": "event", "title": "Standup", "start": "2026-10-26T09:00",
                 "end": "2026-10-26T09:15", "status": "open", "source": "seed"},
            ]},
            {"op": "query", "label": "query", "text": "anything on friday?", "kws": ["haircut", "lee"]},
            {"op": "move", "label": "move", "text": "push my haircut to next week", "item": "hc",
             "start": "2026-10-30T16:00"},
            {"op": "cancel", "label": "cancel", "text": "drop the gym class", "item": "gym"},
            {"op": "view", "label": "view", "type": "workweek", "hidden_date": "2026-10-24"},
            {"op": "bulk", "label": "bulk", "text": "I'm going camping this weekend, clear everything",
             "affected": ["soc", "din"], "new_kw": ["camp"]},
            {"op": "log", "label": "log", "expect": [{"kind": "update", "item": "hc"}, {"kind": "plan_undone"}]},
        ],
    },
    {
        "id": "holdout_sam_imports",
        "persona": "Sam",
        "description": "Sam: .ics with TZID dentist, all-day 'Moving day', weekly Tuesday Spanish lesson (UNTIL); "
                       "text note clearing the weekend and adding coffee with Priya.",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T08:30", "items": SAM_HISTORY + [
                {"id": "ys", "kind": "event", "title": "Yard sale", "start": "2026-10-24T09:00",
                 "end": "2026-10-24T13:00", "status": "open", "source": "seed"},
            ]},
            {"op": "ics", "label": "ics", "filename": "sam.ics", "content": ICS_HOLDOUT, "n": 3,
             "titles": ["dentist", "moving", "spanish"],
             "tz": {"kw": "dentist", "start": "2026-10-23T08:45"},
             "allday": {"kw": "moving", "date": "2026-10-31", "last": "2026-11-01"},
             "rrule": {"kw": "spanish", "dates": ["2026-10-27", "2026-11-03", "2026-11-10", "2026-11-17"]}},
            {"op": "text_import", "label": "txt", "filename": "heads-up.txt",
             "content": "Heads up: I'm away Saturday and Sunday, so clear those days. And add coffee with Priya on "
                        "Thursday around 10.",
             "cleared": ["ys"], "new_kw": ["coffee", "priya"]},
        ],
    },
    {
        "id": "holdout_sam_learner",
        "persona": "Sam",
        "description": "Sam: evening belief from 8 late finishes, lowered by morning work, deleted and not re-learned; "
                       "then 10 weekday-only weeks -> proposal, dismissed, not shown again. [mock mode]",
        "mock": True,
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T05:00",
             "items": _evening_history(EIGHT_DAYS, "se", slot=("21:10", "21:55"),
                                       morning=("2026-10-14", "10:30", "11:00")) + [
                 {"id": "mo1", "kind": "task", "title": "Email the movers", "start": "2026-10-21T06:30",
                  "end": "2026-10-21T06:45", "status": "open", "source": "seed"},
                 {"id": "mo2", "kind": "task", "title": "Label boxes", "start": "2026-10-21T06:45",
                  "end": "2026-10-21T07:00", "status": "open", "source": "seed"},
                 {"id": "mo3", "kind": "task", "title": "Order tape", "start": "2026-10-21T07:00",
                  "end": "2026-10-21T07:10", "status": "open", "source": "seed"},
             ]},
            {"op": "learn", "label": "learn", "as": "eve", "belief": {"re": EVENING_RE, "num": "8", "min_conf": 0.7}},
            {"op": "counter", "label": "counter", "belief": "eve", "clock": "2026-10-21T07:12",
             "done": ["mo1", "mo2", "mo3"]},
            {"op": "delete_belief", "label": "del", "belief": "eve"},
            {"op": "reset", "label": "reset2", "now": "2026-10-21T09:00",
             "items": _weekday_history("sw", hour="13:00", end="14:00", first_monday="2026-08-10")},
            {"op": "proposal", "label": "prop", "flow": "dismiss"},
        ],
    },
]

UI_DESC = ("Static usability checks on GET /: all ids, a real stylesheet (>= 25 rules covering chat/focus/timeline), "
           "nothing hidden by default except the proposal card, script wired to the API with periodic refresh, "
           "'Start 5 minutes' in #focus and Undo buttons for messages with undo_id.")
FIT.append({"id": "fit_ui", "persona": "Iris", "description": UI_DESC, "mock": True, "final": False,
            "steps": [{"op": "ui_usability", "label": "ui"}]})
HOLDOUT.append({"id": "holdout_ui", "persona": "Sam", "description": UI_DESC, "mock": True, "final": False,
                "steps": [{"op": "ui_usability", "label": "ui"}]})

# ---------------------------------------------------------------- v3 (behaviours 22-26)

def _spec(elements, root="r"):
    return {"root": root, "elements": elements}


def _bad_specs(unknown, ghost, bad_date_el, far_el):
    return {
        "reject_unknown_type": {"cause": unknown.lower() + "|unknown|catalog|type",
                                "spec": _spec({"r": {"type": unknown, "props": {}, "children": []}})},
        "reject_missing_child": {"cause": ghost, "spec": _spec({
            "r": {"type": "Lane", "props": {"label": "All", "filter": {}}, "children": ["a", ghost]},
            "a": {"type": "AgendaList", "props": {"from": "2026-01-01", "to": "2027-12-31"}, "children": []}})},
        "reject_bad_date": {"cause": "date|" + list(bad_date_el["props"])[0], "spec": _spec({"r": bad_date_el})},
        "reject_hidden_item": {"cause": r"hid|left out|not shown|neither|count|cover|missing|item|visible",
                               "spec": _spec({"r": far_el})},
    }


V3_NOW = "2026-10-19T09:00"
V3_OPEN = [
    {"id": "o1", "kind": "event", "title": "Dentist", "start": "2026-10-22T15:00", "end": "2026-10-22T16:00",
     "status": "open", "source": "seed"},
    {"id": "o2", "kind": "task", "title": "Write cover letter", "start": "2026-10-20T20:30", "end": "2026-10-20T21:15",
     "status": "open", "source": "seed"},
    {"id": "o3", "kind": "task", "title": "Sort old photos", "status": "open", "source": "seed"},
]
EVENING_WEEKDAYS = _weekday_history("v", hour="20:30", end="21:15")
APPETITE_ANSWERS = {"reason": "It has been weighing on me and I want it off my mind.",
                    "done": "When the main parts are finished and I can stop thinking about it.",
                    "deadline_text": "By next Friday, October 30.", "reason_kws": ["mind"], "deadline": "2026-10-30"}

FIT += [
    {
        "id": "fit_critic",
        "persona": "Iris",
        "description": "/api/catalog has the six types; /api/critic rejects an unknown type, a missing child, a non-date "
                       "date prop and a view that hides items (naming each cause), accepts the designer's own draft, "
                       "and repaired specs pass. [mock mode]",
        "mock": True, "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": V3_NOW, "items": IRIS_HISTORY + V3_OPEN},
            {"op": "critic", "label": "critic", "bad": _bad_specs(
                "SpiralClock", "ghost",
                {"type": "AgendaList", "props": {"from": "next tuesday-ish", "to": "2027-12-31"}, "children": []},
                {"type": "DayTimeline", "props": {"date": "2030-01-01", "from_hour": 8, "to_hour": 9}, "children": []})},
        ],
    },
    {
        "id": "fit_design",
        "persona": "Iris",
        "description": "/api/design: >= 2 drafts with critiques, the chosen one passes the critic and static catalog "
                       "checks; evening vs no-weekend beliefs give different specs (evening one filtered to evening "
                       "hours); an invented proposal is a valid custom spec. [mock mode]",
        "mock": True, "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": V3_NOW, "items": EVENING_WEEKDAYS + V3_OPEN},
            {"op": "design", "label": "design", "belief_res": [EVENING_RE, WEEKEND_RE]},
        ],
    },
    {
        "id": "fit_view_motion",
        "persona": "Iris",
        "description": "Static checks on GET /: the script renders custom specs and names every catalog type, sets "
                       "data-day / data-item, animates view changes in <= 600 ms, honours prefers-reduced-motion.",
        "mock": True, "final": False,
        "steps": [{"op": "view_motion", "label": "motion"}],
    },
    {
        "id": "fit_knobs",
        "persona": "Iris",
        "description": "meta.threshold starts at 0.7; three dismissed proposals raise threshold/cadence (logged in "
                       "activity, used by the gate); after a reset, an accepted view kept 3+ days lowers the threshold. [mock mode]",
        "mock": True, "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": V3_NOW, "items": EVENING_WEEKDAYS},
            {"op": "knobs_dismiss", "label": "dismiss3"},
            {"op": "reset", "label": "reset2", "now": V3_NOW, "items": EVENING_WEEKDAYS},
            {"op": "knobs_accept", "label": "keep"},
        ],
    },
    {
        "id": "fit_appetite_decline",
        "persona": "Iris",
        "description": "Declining the breakdown offer 3 times lowers meta.breakdown_appetite; the next vague task "
                       "gets no breakdown question. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": V3_NOW, "items": IRIS_HISTORY},
            {"op": "appetite", "label": "appetite", "flow": "decline",
             "tasks": ["work on the pitch deck", "figure out my budget", "deal with the garage"],
             "replies": ["no", "not now", "no thanks"], "next": "work on my novel"},
        ],
    },
    {
        "id": "fit_appetite_accept",
        "persona": "Iris",
        "description": "Accepting the breakdown 3 times raises meta.breakdown_appetite; the next vague task gets "
                       "proposed steps straight away. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": V3_NOW, "items": IRIS_HISTORY},
            dict({"op": "appetite", "label": "appetite", "flow": "accept",
                  "tasks": ["work on the pitch deck", "figure out my budget", "deal with the garage"],
                  "next": "work on my novel"}, **APPETITE_ANSWERS),
        ],
    },
]

HOLDOUT += [
    {
        "id": "holdout_critic",
        "persona": "Sam",
        "description": "Sam: the critic rejects a 'Carousel' type, a 'phantom' child, a DayTimeline date '31/12/2026' "
                       "and a MonthGrid of May 2031 that hides every item; own draft accepted; repairs pass. [mock mode]",
        "mock": True, "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T08:30", "items": SAM_HISTORY + [
                {"id": "ys", "kind": "event", "title": "Yard sale", "start": "2026-10-24T09:00", "end": "2026-10-24T13:00",
                 "status": "open", "source": "seed"},
                {"id": "pb", "kind": "task", "title": "Pack books", "status": "open", "source": "seed"}]},
            {"op": "critic", "label": "critic", "bad": _bad_specs(
                "Carousel", "phantom",
                {"type": "DayTimeline", "props": {"date": "31/12/2026", "from_hour": 9, "to_hour": 17}, "children": []},
                {"type": "MonthGrid", "props": {"month": "2031-05"}, "children": []})},
        ],
    },
    {
        "id": "holdout_design",
        "persona": "Jordan",
        "description": "Jordan (evening worker, never weekends): design drafts, chosen passes critic + static check, "
                       "beliefs differ, invented proposal is a valid custom spec (live models).",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00",
             "items": _weekday_history("jd", hour="19:00", end="19:50") + [
                 {"id": "hc", "kind": "event", "title": "Haircut", "start": "2026-10-23T16:00", "end": "2026-10-23T16:45",
                  "status": "open", "source": "seed"}]},
            {"op": "design", "label": "design", "belief_res": [EVENING_RE + "|after 7|7 ?pm|19:00", WEEKEND_RE]},
        ],
    },
    {
        "id": "holdout_view_motion",
        "persona": "Sam",
        "description": "Static view rendering / morph checks on GET / (same rules as fit).",
        "mock": True, "final": False,
        "steps": [{"op": "view_motion", "label": "motion"}],
    },
    {
        "id": "holdout_appetite",
        "persona": "Sam",
        "description": "Sam declines three offers ('nah', 'not now', 'no'); appetite drops; 'deal with the storage "
                       "unit' gets no breakdown question. [mock mode]",
        "mock": True,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T08:30", "items": SAM_HISTORY},
            {"op": "appetite", "label": "appetite", "flow": "decline",
             "tasks": ["figure out moving", "deal with the utilities", "work on the school forms"],
             "replies": ["nah", "not now", "no"], "next": "deal with the storage unit"},
        ],
    },
    {
        "id": "holdout_knobs",
        "persona": "Jordan",
        "description": "Jordan: three dismissed proposals raise the knobs; an accepted view kept 3+ days lowers the "
                       "threshold. [mock mode]",
        "mock": True, "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00", "items": _weekday_history("jk", hour="19:00", end="19:50")},
            {"op": "knobs_dismiss", "label": "dismiss3"},
            {"op": "reset", "label": "reset2", "now": "2026-10-20T08:00", "items": _weekday_history("jk", hour="19:00", end="19:50")},
            {"op": "knobs_accept", "label": "keep"},
        ],
    },
]

def _render_seed(now_day, next_day):
    return [
        {"id": "u1", "kind": "task", "title": "Pick up dry cleaning", "start": f"{now_day}T17:00",
         "end": f"{now_day}T17:30", "status": "open", "source": "seed"},
        {"id": "u2", "kind": "event", "title": "Book club", "start": f"{now_day}T19:00", "end": f"{now_day}T20:00",
         "status": "open", "source": "seed"},
        {"id": "u3", "kind": "task", "title": "Call the plumber", "start": f"{next_day}T10:00",
         "end": f"{next_day}T10:30", "status": "open", "source": "seed"},
        {"id": "u4", "kind": "event", "title": "Lunch with Ana", "start": f"{next_day}T12:00",
         "end": f"{next_day}T13:00", "status": "open", "source": "seed"},
    ]


RENDER_DESC = ("Rendered DOM (headless Chrome, after reset + brain dump + breakdown chat + /api/learn): seeded item "
               "titles, the latest agent message, the focus step inside #focus, a belief inside #learned and an "
               "activity text inside #activity actually appear on the page. [mock mode]")
FIT.append({"id": "fit_ui_render", "persona": "Iris", "description": RENDER_DESC, "mock": True, "final": False,
            "steps": [
                {"op": "reset", "label": "reset", "now": "2026-10-19T09:00",
                 "items": IRIS_HISTORY + _render_seed("2026-10-19", "2026-10-20")},
                dict({"op": "ui_render", "label": "render", "dump": "water the plants, email Kim, maybe repaint the hallway?",
                      "goal": "get my portfolio together",
                      "seed_titles": ["Pick up dry cleaning", "Book club", "Call the plumber", "Lunch with Ana"]},
                     **APPETITE_ANSWERS)]})
HOLDOUT.append({"id": "holdout_ui_render", "persona": "Sam", "description": RENDER_DESC, "mock": True, "final": False,
                "steps": [
                    {"op": "reset", "label": "reset", "now": "2026-10-21T08:30",
                     "items": SAM_HISTORY + _evening_history(EIGHT_DAYS, "rh") + _render_seed("2026-10-21", "2026-10-22")},
                    dict({"op": "ui_render", "label": "render", "dump": "pack the kitchen, call the landlord, maybe sell the couch?",
                          "goal": "figure out moving",
                          "seed_titles": ["Pick up dry cleaning", "Book club", "Call the plumber", "Lunch with Ana"]},
                         **APPETITE_ANSWERS)]})

# ---------------------------------------------------------------- v3.1: harder fit (new wording, times, personas)
# Personas Noor, Theo, Rosa, Kenji; no string here is shared with HOLDOUT (harness_test checks it).

def _ev(i, title, start, end, kind="event", status="open"):
    return {"id": i, "kind": kind, "title": title, "start": start, "end": end, "status": status, "source": "seed"}


def _morning_history(prefix):
    """7 done tasks finishing before 8 AM and 1 in the evening."""
    days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
    items = [_ev(f"{prefix}{k}", t, f"{d}T07:00", f"{d}T07:40", "task", "done")
             for k, (d, t) in enumerate(zip(days, ["Journal", "Run", "Inbox zero", "Plan the day", "Read",
                                                   "Stretch", "Review budget"]))]
    items.append(_ev(f"{prefix}e", "Water plants", "2026-10-11T20:30", "2026-10-11T20:45", "task", "done"))
    return items


def _no_friday_history(prefix, weeks=10, first_monday="2026-08-10"):
    """`weeks` weeks of meetings Monday-Thursday, never on a Friday or weekend."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(first_monday)
    out = []
    for w in range(weeks):
        for k, t in enumerate(["Design review", "Client sync", "1:1 with Ola", "Planning"]):
            d = (d0 + timedelta(days=7 * w + k)).isoformat()
            out.append(_ev(f"{prefix}{w}_{k}", t, f"{d}T11:00", f"{d}T11:45", "event", "done"))
    return out


MORNING_RE = r"morning|before (8|9|10|noon)|early|\bam\b|a\.m\.|before 08|07:"
KENJI = _morning_history("k")
ICS_PLAIN = _ics([
    ["UID:v31-a@harness", "SUMMARY:Team offsite", "DTSTART:20261027T093000", "DTEND:20261027T163000"],
    ["UID:v31-b@harness", "SUMMARY:Wedding anniversary", "DTSTART;VALUE=DATE:20261029"],
    ["UID:v31-c@harness", "SUMMARY:Pottery market", "DTSTART:20261031T100000", "DTEND:20261031T120000"],
])

FIT += [
    {
        "id": "fit2_bulk_saturday", "persona": "Noor", "mock": True,
        "description": "'free up my saturday, I have a family reunion' -> preview deleting exactly the two Saturday "
                       "items plus one create; Friday/Sunday untouched; apply and undo. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00", "items": IRIS_HISTORY + [
                _ev("bk", "Bike repair", "2026-10-24T10:00", "2026-10-24T11:00"),
                _ev("pn", "Picnic with Theo", "2026-10-24T13:00", "2026-10-24T15:00"),
                _ev("ch", "Choir practice", "2026-10-25T10:00", "2026-10-25T11:30"),
                _ev("dm", "Team demo", "2026-10-23T15:00", "2026-10-23T16:00")]},
            {"op": "bulk", "label": "bulk", "text": "free up my saturday, I have a family reunion",
             "affected": ["bk", "pn"], "new_kw": ["reunion", "family"]},
        ],
    },
    {
        "id": "fit2_bulk_sunday", "persona": "Theo", "mock": True,
        "description": "'nothing on sunday please, I'm travelling to Lisbon' -> only Sunday's items are deleted, a "
                       "travel item is created; apply and undo. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T19:00", "items": IRIS_HISTORY + [
                _ev("br", "Brunch at Oslo cafe", "2026-10-25T11:00", "2026-10-25T12:30"),
                _ev("ft", "Football with Kai", "2026-10-25T16:00", "2026-10-25T17:30"),
                _ev("bm", "Bike market", "2026-10-24T09:00", "2026-10-24T10:00"),
                _ev("mo", "Weekly review", "2026-10-26T09:00", "2026-10-26T09:30")]},
            {"op": "bulk", "label": "bulk", "text": "nothing on sunday please, I'm travelling to Lisbon",
             "affected": ["br", "ft"], "new_kw": ["travel", "lisbon", "trip"]},
        ],
    },
    {
        "id": "fit2_bulk_friday_evening", "persona": "Rosa", "mock": True,
        "description": "'wipe friday evening, I got concert tickets' -> deletes only Friday's evening items "
                       "(not the morning one), creates the concert; apply and undo. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T12:00", "items": IRIS_HISTORY + [
                _ev("st", "Morning standup", "2026-10-23T09:00", "2026-10-23T09:15"),
                _ev("dk", "Drinks with Kai", "2026-10-23T18:30", "2026-10-23T20:00"),
                _ev("ld", "Laundry", "2026-10-23T21:00", "2026-10-23T21:30", "task"),
                _ev("sa", "Saturday swim", "2026-10-24T08:00", "2026-10-24T09:00")]},
            {"op": "bulk", "label": "bulk", "text": "wipe friday evening, I got concert tickets",
             "affected": ["dk", "ld"], "new_kw": ["concert"]},
        ],
    },
    {
        "id": "fit2_crud_verbs", "persona": "Kenji", "mock": True,
        "description": "'anything on the 23rd?' names both items; 'push the eye exam back a week' moves only it to "
                       "Oct 30 same time; 'drop pilates' is a preview delete until applied. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T07:30", "items": KENJI + [
                _ev("ey", "Eye exam", "2026-10-23T14:00", "2026-10-23T14:45"),
                _ev("pi", "Piano lesson", "2026-10-23T17:00", "2026-10-23T18:00"),
                _ev("pl", "Pilates", "2026-10-22T07:15", "2026-10-22T08:00")]},
            {"op": "query", "label": "query", "text": "anything on the 23rd?", "kws": ["eye", "piano"]},
            {"op": "move", "label": "move", "text": "push the eye exam back a week", "item": "ey",
             "start": "2026-10-30T14:00"},
            {"op": "cancel", "label": "cancel", "text": "drop pilates", "item": "pl"},
        ],
    },
    {
        "id": "fit2_ics_plain", "persona": "Noor",
        "description": ".ics without TZID (floating times), a DATE-only event without DTEND, and a VEVENT already in "
                       "the calendar (same title + start): preview creates only the 2 new ones; re-import 0.",
        "final": False,
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T08:00", "items": IRIS_HISTORY + [
                _ev("off", "Team offsite", "2026-10-27T09:30", "2026-10-27T16:30")]},
            {"op": "ics_simple", "label": "ics", "filename": "work.ics", "content": ICS_PLAIN,
             "new_titles": ["anniversary", "pottery market"], "existing_kw": "offsite",
             "times": {"pottery market": "2026-10-31T10:00"},
             "allday": {"kw": "anniversary", "date": "2026-10-29"}},
        ],
    },
    {
        "id": "fit2_complexity_verbs", "persona": "Rosa", "mock": True,
        "description": "Other vague verbs: 'sort out my closet', 'handle the insurance claim', 'get on top of my "
                       "inbox' each get exactly one breakdown question; nothing broken down. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T10:00", "items": IRIS_HISTORY},
            {"op": "command", "label": "closet", "text": "sort out my closet", "mode": "command",
             "as": "g1", "expect": {"complexity": {"kw": "closet"}}},
            {"op": "command", "label": "insurance", "text": "handle the insurance claim", "mode": "command",
             "as": "g2", "expect": {"complexity": {"kw": "insurance"}}},
            {"op": "command", "label": "inbox", "text": "get on top of my inbox", "mode": "command",
             "as": "g3", "expect": {"complexity": {"kw": "inbox"}}},
        ],
    },
    {
        "id": "fit2_breakdown_other", "persona": "Theo",
        "description": "Breakdown with other replies: 'sure, go ahead' instead of yes and 'that works for me' to "
                       "accept, for 'work on the grant application' (deadline Nov 6); children, why, focus.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T10:00", "items": IRIS_HISTORY},
            {"op": "command", "label": "goal", "text": "work on the grant application", "mode": "command",
             "as": "g", "expect": {"complexity": {"kw": "grant"}}},
            {"op": "breakdown", "label": "chat", "goal": "g", "yes_text": "sure, go ahead",
             "accept_text": "that works for me",
             "reason": "The grant would fund my community garden project for two years.",
             "reason_kws": ["garden", "fund", "community"],
             "done": "The application is submitted with the budget and two letters of support.",
             "deadline_text": "It is due on Friday, November 6.", "deadline": "2026-11-06", "focus": True},
        ],
    },
    {
        "id": "fit2_postpone_morning", "persona": "Kenji", "mock": True,
        "description": "Kenji gets things done before 8 AM (7 of 8): a missed 13:00 task moves to the next day in a "
                       "morning slot (hour <= 11), names task + time, undo restores it. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T12:00", "items": KENJI + [
                _ev("nl", "Draft newsletter", "2026-10-20T13:00", "2026-10-20T14:00", "task")]},
            {"op": "postpone", "label": "pp", "item": "nl", "kw": "newsletter", "quiet_until": "2026-10-20T14:25",
             "after": "2026-10-20T14:40", "next_day": "2026-10-21", "max_hour": 11, "undo": True},
        ],
    },
    {
        "id": "fit2_checkin_other", "persona": "Rosa",
        "description": "'Call the insurance' (15:00-15:30) is postponed twice; the third miss gives a checkin; "
                       "'it's just way too much in one go' leads to a breakdown.",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-20T14:00", "items": IRIS_HISTORY + [
                _ev("ci", "Call the insurance", "2026-10-20T15:00", "2026-10-20T15:30", "task")]},
            {"op": "checkin", "label": "ci", "item": "ci", "kw": "insurance", "reply": "it's just way too much in one go"},
        ],
    },
    {
        "id": "fit2_learner_other", "persona": "Kenji", "mock": True, "final": False,
        "description": "Other seeds: 7 of 8 tasks done before 8 AM -> a morning habit belief >= 0.7 citing 7; 10 weeks "
                       "of meetings Monday-Thursday only -> a no-weekend belief citing 10 and a proposal; dismiss. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T06:00", "items": KENJI},
            {"op": "learn", "label": "morning", "as": "mor", "belief": {"re": MORNING_RE, "num": "7", "min_conf": 0.7}},
            {"op": "reset", "label": "reset2", "now": "2026-10-19T06:00", "items": _no_friday_history("nf")},
            {"op": "learn", "label": "weekend", "as": "wkd", "belief": {"re": WEEKEND_RE, "num": "10", "min_conf": 0.7}},
            {"op": "proposal", "label": "prop", "flow": "dismiss"},
        ],
    },
    {
        "id": "fit2_design_other", "persona": "Kenji", "mock": True, "final": False,
        "description": "Design with other beliefs: morning person vs no-weekend; drafts pass the critic and static "
                       "checks, the morning spec filters to morning hours, an invented proposal is a valid spec. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-19T06:00",
             "items": _weekday_history("kd", hour="07:00", end="07:40") + [
                 _ev("ey", "Eye exam", "2026-10-23T14:00", "2026-10-23T14:45")]},
            {"op": "design", "label": "design", "belief_res": [MORNING_RE, WEEKEND_RE], "hours": [0, 11]},
        ],
    },
    {
        "id": "fit2_critic_other", "persona": "Theo", "mock": True, "final": False,
        "description": "Critic with other bad specs: type 'Timeline3D', child 'nowhere', WeekColumns start 'Oct 26', "
                       "an AgendaList for January 2031 that hides every item. [mock mode]",
        "steps": [
            {"op": "reset", "label": "reset", "now": "2026-10-21T19:00", "items": IRIS_HISTORY + [
                _ev("ft", "Football with Kai", "2026-10-25T16:00", "2026-10-25T17:30"),
                {"id": "nb", "kind": "task", "title": "Return the drill", "status": "open", "source": "seed"}]},
            {"op": "critic", "label": "critic", "bad": _bad_specs(
                "Timeline3D", "nowhere",
                {"type": "WeekColumns", "props": {"start": "Oct 26", "days": 7}, "children": []},
                {"type": "AgendaList", "props": {"from": "2031-01-01", "to": "2031-01-31"}, "children": []})},
        ],
    },
]

SPLITS = {"fit": FIT, "holdout": HOLDOUT}
