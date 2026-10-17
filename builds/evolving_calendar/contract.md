# Evolving Calendar (Iris build) — frozen app contract

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
