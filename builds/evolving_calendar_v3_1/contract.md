# Evolving Calendar v3 — frozen app contract (v1 1-9, v2 10-21, v3 22-26: designer + critic views, morphs, adaptive knobs)

This is the interface the team's code must implement. A frozen harness (`harness.py`, not shown to the team in full)
starts the app, drives it over HTTP as a person would, and checks behaviour. Product background: the person we
design for finds planning hard (persona "Iris"): many vague tasks, frequent postponements, gets things done late in
the evening, a big personal goal with no steps. Principles: talk, don't form-fill (one gentle question at a time);
tiny first steps (first step 15 minutes or less); no guilt (missed tasks move forward quietly with kind wording, no
"overdue"); soft time (windows like "this afternoon"); meaning first (ask why a goal matters and show it);
energy-aware (schedule similar work when it usually gets done).

## Files and runtime

- The app is Python 3.12, **standard library only** (http.server, json, sqlite3, urllib, datetime, threading, re ...).
- Entry point `app.py`, started as `python app.py --port <PORT> --data <DIR>` from the app directory. It must answer
  `GET /health` with `{"ok": true}` within 15 s. Keep all state in memory or under `--data` (sqlite3 or JSON).
- `index.html` is served at `GET /`: a single page (inline CSS/JS, no CDN needed) with a chat box (element id
  `chat-input`), the conversation (`chat-log`), a focus card (`focus`) showing only the next tiny step with a
  "Start 5 minutes" button, and a timeline of today's/tomorrow's items (`timeline`). It talks to the API below.
- Other files (e.g. `agents.py`, `scheduler.py`) are allowed; they sit next to `app.py`.
- Language-model calls (optional but expected for parsing and breakdowns) go to Crusoe Managed Inference,
  OpenAI-compatible `POST {CRUSOE_BASE_URL}/chat/completions` with header `Authorization: Bearer {CRUSOE_API}`.
  Env vars provided: `CRUSOE_API`, `CRUSOE_BASE_URL` (https://api.inference.crusoecloud.com/v1),
  `MODEL_FAST` (nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B), `MODEL_SMART` (nvidia/NVIDIA-Nemotron-3-Super-120B-A12B).
  Reasoning models think first: use `max_tokens` >= 4000 and read `choices[0].message.content`. Every HTTP request
  to the app must return within 60 s; if a model call fails or times out, fall back to rules so the app still answers.
- **There is no real clock.** "Now" is the simulated time set by `/api/reset` and `/api/clock`. Times are ISO
  local wall-clock strings without zone, minute precision: `"2026-10-19T20:00"`.

## Data shapes

Item:
```json
{"id": "str", "kind": "task|event|note", "title": "str", "start": "iso|null", "end": "iso|null",
 "window": "str|null", "status": "open|done", "why": "str|null", "parent_id": "str|null",
 "estimate_min": "int|null", "postpone_count": 0, "source": "seed|typed|dump|agent"}
```
Message (agent or person):
```json
{"id": "str", "role": "agent|person", "kind": "reply|question|reminder|postponed|checkin|breakdown|info",
 "text": "str", "item_id": "str|null", "at": "iso", "undo_id": "str|null",
 "steps": [{"title": "str", "estimate_min": 10}]}
```
`steps` is present only on `kind: "breakdown"` messages.

## Endpoints (all JSON in and out)

| Method and path | Body | Returns |
|---|---|---|
| `GET /health` | | `{"ok": true}` |
| `POST /api/reset` | `{"now": iso, "items": [Item-like seed objects]}` | full state. Wipes everything, loads the seed items (they keep their ids; missing fields get defaults; `status: "done"` seed items are history the app may learn from, e.g. when tasks usually get done) |
| `GET /api/state` | | `{"now": iso, "items": [Item], "messages": [Message]}` (messages oldest first) |
| `POST /api/command` | `{"text": str, "mode": "command" or "dump"}` | `{"messages": [new Message], "created": [item ids]}` |
| `POST /api/chat` | `{"item_id": str, "text": str}` | `{"messages": [new Message]}` — the person's reply in a breakdown or check-in conversation about that item |
| `POST /api/items/<id>/done` | `{}` | `{"item": Item}` |
| `POST /api/undo` | `{"undo_id": str}` | `{"ok": true}` — reverts the change that message announced |
| `POST /api/clock` | `{"now": iso}` | `{"messages": [new Message]}` — advances simulated time (never backwards) and runs every scheduler tick between the old and new time, in order |
| `GET /api/focus` | | `{"item": Item or null, "parent": Item or null, "why": str or null, "action": "Start 5 minutes"}` |

## Required behaviour (what the harness checks)

1. **Typed command** (`mode: "command"`): "dentist thursday 3pm" creates one event on the next Thursday at 15:00;
   vague times keep their vagueness (`window`, e.g. "around noon" -> window set, start may be approximate).
2. **Brain dump** (`mode: "dump"`): "call mom, portfolio, maybe yoga?, dentist thursday 3pm" creates one item per
   fragment (no fragment lost; clear times become events, vague ones tasks or notes). The response asks **at most
   one** question (one gentle question at a time).
3. **Complexity check**: a new vague or big task ("get my portfolio together", "work on the pitch deck", vague verbs
   like work on / figure out / deal with, or over 2 hours) gets exactly one agent message of kind `question` on that
   item asking whether to break it into smaller steps. Nothing is broken down without a yes.
4. **Breakdown chat** (`/api/chat` on that item): after a yes, the agent asks **why it matters** first, then at most
   two more short questions (what done looks like, the deadline), one question per message, then sends a
   `breakdown` message with 2 to 5 steps. Each step title starts with a concrete verb; the **first step is 15 minutes
   or less**; each step is about an hour or less. When the person accepts ("yes", "looks good", "ok"), the steps
   become task items with `parent_id` = the goal, scheduled (start or window) before the deadline, and the goal's
   `why` holds the person's reason. The person may instead ask for changes and get a revised `breakdown`.
5. **Focus** (`GET /api/focus`): the next open step of the most important goal (the earliest open step of a goal
   with a why, else the earliest open task), its parent, the why, and the action label.
6. **Reminders**: when the clock passes an item's start, one `reminder` message for it; when it passes 10 minutes
   before its end, another one. No duplicates. Done items get no reminders.
7. **Auto-postpone**: a task whose end passed more than 30 minutes ago, not done, moves to the next day, to a free
   slot of the same length that fits when this person usually gets things done (learned from done history; Iris
   finishes tasks after 8 PM). `postpone_count` += 1. One `postponed` message that names the task and the new time,
   in kind wording, with an `undo_id`; undo puts it back.
8. **Check-in**: on the third consecutive postponement of the same task the agent does not move it silently; it sends
   a `checkin` message asking what happened and offering to break it into smaller steps (or a better time, or drop
   it). Replies via `/api/chat`: "too big" -> breakdown, "wrong time" -> moves it, "not needed" -> asks to confirm, then
   deletes.
9. **No guilt, anywhere**: no agent message uses overdue, failed, missed, late, behind, "should have", forgot, lazy,
   or red-alert wording. Every postpone or reminder message still names the task.


## v2 additions

v2 keeps every v1 endpoint and behaviour above unchanged and adds the learning loop from the design doc: interaction
log, imports, bulk changes as previews, a habit learner with beliefs, view proposals that the person accepts or
dismisses, and a meta-learner that rewrites the learned part of the agents' prompts with versions and rollback.
"Model calls" below may be LLM calls or plain code; mock mode must work without network.

### Extra runtime rules
- `EVOLVE_MOCK=1` in the environment = no network at all: every agent must fall back to rules. The harness runs some
  scenarios in mock mode.
- `index.html` additionally has element ids `learned` (the "What I've learned" panel: beliefs and meta-preferences in
  plain language, each deletable), `activity` (the agent activity feed), `proposal` (the proposal card with Try it /
  Not now / Undo), `view-switcher` (buttons month / week / workweek / day).

### Extra data shapes
LogEntry: `{"id": str, "at": iso, "kind": str, "item_ids": [str], "payload": {}, "source": "typed|dump|ics|text|agent|clock|person"}`.
`kind` is one of: create, update, delete, query, done, postponed, reminder, chat, checkin_answer, view_switch,
proposal_shown, proposal_accepted, proposal_dismissed, proposal_reverted, import, plan_applied, plan_undone, belief_deleted.

Belief: `{"key": str, "level": "habit|meta", "statement": str (plain language, contains the number it rests on, e.g. "Finishes tasks after 8 PM (8 of 9)"), "value": any, "confidence": 0..1, "evidence": [LogEntry ids], "updated_at": iso}`.

Plan (a change set): `{"id": str, "origin": "command|import", "status": "preview|applied|undone", "ops": [{"op": "create|update|delete", "item_id": str|null, "before": Item|null, "after": Item|null}], "affected": [item ids]}`.

View spec: `{"type": "month|week|workweek|day|custom", "params": {}, "hidden_counts": {"<date>": int}}` — any view that leaves
an item out (e.g. workweek hides the weekend) reports it in `hidden_counts` (no view ever silently hides an item).

Proposal: `{"id": str, "title": str, "reason": str (cites the belief and its number), "view": View spec, "belief_keys": [str], "novelty": "classic|invented", "prompt_versions": {agent: int}, "status": "shown|accepted|dismissed|reverted"}`.

Prompt version: `{"agent": str, "version": int, "core_hash": str, "learned": str (<= 12 lines), "diff": str, "reason": str, "evidence": [LogEntry ids], "replay": {"passed": bool, "detail": str}, "active": bool}`.
Agents with prompts: at least `designer`, `followthrough`, `learner`.

### Extra endpoints
| Method and path | Body | Returns |
|---|---|---|
| `GET /api/log` | | `{"entries": [LogEntry]}` oldest first — every action of behaviours 1-20 appends here |
| `POST /api/import` | `{"filename": str, "content": str}` (.ics text or plain text) | `{"plan": Plan}` with status preview; nothing changes yet |
| `POST /api/plan/<id>/apply` | `{}` | `{"plan": Plan}` applied in one step |
| `POST /api/plan/<id>/undo` | `{}` | `{"plan": Plan}` undone; every item back as before |
| `GET /api/beliefs` | | `{"beliefs": [Belief]}` (habit and meta) |
| `DELETE /api/beliefs/<key>` | | `{"ok": true}` — gone, and not re-learned from the same evidence |
| `POST /api/learn` | `{}` | `{"beliefs": [Belief], "proposal": Proposal or null}` — runs the habit learner now, then the proposal gate (the app also runs it on its own every 5 new log entries and on clock ticks) |
| `GET /api/proposal` | | `{"proposal": Proposal or null}` the open one |
| `POST /api/proposals/<id>/<action>` | `{}`; action = accept, dismiss, undo | `{"proposal": Proposal, "view": View spec}` |
| `POST /api/view` | `{"type": "month|week|workweek|day"}` | `{"view": View spec}` — a manual switch (logged as view_switch) |
| `GET /api/prompts` | | `{"prompts": [Prompt version]}` all versions of all agents |
| `POST /api/prompts/<agent>/rollback` | `{}` | `{"active": Prompt version}` the previous version is active again |
| `GET /api/activity` | | `{"events": [{"at": iso, "agent": str, "kind": str, "text": str}]}` — belief updates, threshold crossings, proposals, prompt rewrites (with diff), rollbacks |
`GET /api/state` additionally returns `"view": View spec` and `"meta": {"novelty": 0..1, "cadence_hours": number, "breakdown_appetite": 0..1, "tone": "gentle|direct"}`.

### Required behaviour, v2 (what the harness checks in addition to 1-9)
10. **Typed CRUD**: "move my dentist to next week" updates exactly that event (+7 days, same time); "cancel yoga"
    deletes it; "what's on thursday?" answers with a `reply` message that names every item on that day and changes
    nothing. A delete or a change of more than one item is NOT applied at once: the response carries `"plan"` (status
    preview) and the items stay unchanged until `/api/plan/<id>/apply`.
11. **Bulk preview**: "empty out my weekend for a business trip" returns a preview plan whose `affected` is exactly
    the ids of items overlapping that Saturday-Sunday, with delete ops for them plus one create op for the trip;
    apply removes them and adds the trip; undo restores every one with its original fields.
12. **.ics import**: importing a VCALENDAR with N VEVENTs (times as local wall-clock or with TZID, all-day events with
    DATE values, RRULE weekly recurrences expanded or stored with `recurrence`) returns a preview plan of N creates with
    `source: "ics"`; apply adds them; importing the same file again yields a plan with 0 creates (duplicates by
    UID, or same title + start).
13. **Text import**: a plain-text note such as "I'll be away this weekend, please clear it. Also lunch with Ana on
    Wednesday around noon." yields a preview plan with at least one delete (or update) and one create, never only
    creates; same apply/undo as 11.
14. **Interaction log**: every command, chat, done, postpone, reminder, import apply, view switch and proposal action
    appends LogEntries with the simulated time, the right `kind` and the item ids.
15. **Habit learner**: from history the app can see (done items in the seed, and log entries), it keeps beliefs with
    confidence between 0 and 1 and evidence ids that exist in the log or are seed item ids. Seeded with 8 done tasks
    finished after 20:00 and 1 before, it holds a habit belief about evening work with confidence >= 0.7 whose
    statement contains "8"; seeded with 10 weeks of items and none on a weekend, it holds a no-weekend belief >= 0.7.
    Counter-evidence (new done tasks in the morning) lowers the evening belief's confidence.
16. **Proposal gate**: when a habit belief is >= the threshold (default 0.7), no proposal is open, and the last
    proposal was more than `cadence_hours` ago, the gate opens exactly one proposal: one view change whose `reason`
    cites the belief's number (e.g. "No weekend plans in 10 weeks. Hide weekends?"). Nothing in `state.view` changes
    until the person accepts. With no belief above the threshold, no proposal appears.
17. **Proposal actions**: accept → `state.view` becomes the proposal's view (logged); undo → the previous view comes
    back and the proposal is `reverted`, and the confidence of its belief drops; dismiss → closed. A dismissed or
    reverted proposal is not shown again for the same belief unless its confidence rises by more than 0.15.
18. **No hidden items**: in any view, items not shown are counted in `hidden_counts` (a workweek view with a Saturday
    item reports that date with count >= 1).
19. **Meta-learner**: after two reverted proposals with `novelty: "invented"`, `meta.novelty` is lower than before; a
    new prompt version of `designer` appears with a non-empty `diff`, a `reason`, `evidence` ids, `replay.passed`, and
    the same `core_hash` as the version before (only the learned part changed, at most 12 lines); the next proposal
    has `novelty: "classic"`. `POST /api/prompts/designer/rollback` makes the previous version active again. The
    learned text never contains words that try to override the core rules ("ignore", "override", "core rules").
20. **Activity and panel**: `/api/activity` lists the belief updates, the proposal, and the prompt rewrite with its
    diff, in time order; `DELETE /api/beliefs/<key>` removes a belief from `/api/beliefs` and it is not re-created by
    the next `/api/learn` from the same evidence.
21. **Mock mode**: with `EVOLVE_MOCK=1`, behaviours 1-4, 6-7, 10-11, 15-17 still pass.


## v3 additions

v3 keeps everything above unchanged and adds the parts of the design doc that make the interface itself evolve:
a designer that composes NEW views from a fixed catalog of building blocks, a mechanical critic that checks and
repairs every draft, views that morph into each other, and adaptation knobs that are themselves learned.

### View catalog and specs
`GET /api/catalog` returns `{"primitives": [{"type": str, "props": {name: "date|int|str|bool|[str]"}, "children": bool}]}` with at
least these six types: `MonthGrid` (props month), `WeekColumns` (props start, days), `DayTimeline` (props date, from_hour, to_hour),
`AgendaList` (props from, to), `Lane` (props label, filter: {"kind"|"category"|"window"|"hour_from"|"hour_to": ...}, children),
`Bucket` (props label, filter, children: false; shows the items matching its filter as a list).
A view spec (`View spec` with `type: "custom"`) carries `"spec": {"root": id, "elements": {id: {"type": catalog type, "props": {...}, "children": [ids]}}}`.

### Extra endpoints
| Method and path | Body | Returns |
|---|---|---|
| `GET /api/catalog` | | see above |
| `POST /api/critic` | `{"spec": spec}` | `{"ok": bool, "problems": [str], "repaired": spec or null}` — the mechanical critic on any spec |
| `POST /api/design` | `{"belief_key": str or null}` | `{"drafts": [spec], "critique": [{"ok": bool, "problems": [str]}], "repairs": int, "chosen": int, "proposal": Proposal or null}` |

### Required behaviour, v3 (checked in addition to 1-21)
22. **Critic**: `/api/critic` rejects (ok false, each with a problem string naming the cause) a spec that uses a type not in the catalog,
    a child id missing from `elements`, a prop of the wrong kind (e.g. a non-date where a date is required), or a view that would
    leave any current item neither shown nor counted (no view ever hides an item, P17). It accepts the specs it produces itself.
    `repaired` (when not null) passes the critic.
23. **Designer + critic loop**: `/api/design` returns at least 2 drafts, the critique of each, the number of repairs, and the index
    of the chosen draft; the chosen draft passes the critic; when the gate opens an invented proposal (novelty "invented"),
    its view is `type: "custom"` with a spec built only from catalog types, and that spec passes `/api/critic`. Different beliefs
    lead to different specs (e.g. an evening-work belief → a spec with a Lane or DayTimeline filtered to evening hours). Mock mode
    must still produce valid drafts.
24. **Custom views render and morph**: `index.html` renders any custom spec in `state.view` from its `elements` (the page script
    handles every catalog type by name), gives each day / item element a stable identity attribute (`data-day="YYYY-MM-DD"` on
    day cells, `data-item="<id>"` on items) so the same day keeps its element across views, animates view changes (CSS transitions,
    the Web Animations API or a FLIP technique, duration <= 600 ms), and honours `@media (prefers-reduced-motion: reduce)` with a
    plain cross-fade or no motion.
25. **Adaptive knobs (E5)**: `state.meta` additionally has `threshold` (0..1, default 0.7). Three dismissed proposals in a row raise
    `threshold` and/or `cadence_hours` (fewer, later proposals); an accepted proposal whose view is kept for at least 3 simulated days
    (clock advanced, no undo) lowers `threshold` slightly. Each change appears in `/api/activity` with its reason, and the gate uses
    the new values (a belief at the old threshold no longer opens a proposal after the raise).
26. **Breakdown appetite (P33)**: if the person declines the breakdown offer 3 times ("no", "not now"), `meta.breakdown_appetite`
    drops and the next vague task gets no breakdown question (a quiet note instead, or nothing); if they accept 3 times in a row,
    the next vague task gets proposed steps straight away (a `breakdown` message without the yes/no question first).
