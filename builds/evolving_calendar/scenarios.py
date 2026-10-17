"""Frozen scenarios for the Evolving Calendar (Iris build).

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
             "expect": {"n_created": 1, "event": {"kw": "dentist", "start": "2026-10-22T15:00"}}},
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
        ],
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
        ],
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
        ],
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
        ],
    },
]

SPLITS = {"fit": FIT, "holdout": HOLDOUT}
