"""Personas for the LLM user simulator.

A persona is a plain dict:
  name, summary, traits [str], active_hour (int, when the episode's simulated "now" starts),
  history  [{"title", "days_ago", "hour", "minute", "dur_min"}]   done items the app may learn from
  open     [{"title", "kind", "days_ahead"|None, "hour"|None, "window"|None, "dur_min"}]  open seed items
  reactions {topic: how this person reacts}, e.g. breakdown offers, invented views, bulk changes, imports
  goals    [str]  things this person wants to get done in the app during one session

The 4 doc personas are fixed (Iris is from the contract's product background; Mina, Jordan and Sam are invented to
cover other calendar users). `generate(n, seed)` asks an LLM for new ones built from the design principles and caches
them to cache/personas_<seed>_<n>.json.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

PRINCIPLES = [
    "talk, don't form-fill: one gentle question at a time",
    "tiny first steps: the first step of any plan is 15 minutes or less",
    "no guilt: missed tasks move forward quietly with kind wording",
    "soft time: windows like 'this afternoon' instead of exact slots",
    "meaning first: ask why a goal matters and show it",
    "energy-aware: schedule similar work when this person usually gets it done",
    "few, calm proposals: the app rarely suggests view changes and always explains them with a number",
]

IRIS = {
    "name": "Iris",
    "summary": "finds planning hard; many vague tasks, postpones often, gets things done late in the evening, "
               "has a big personal goal (a design portfolio) with no steps",
    "traits": ["types short vague fragments, often without times", "postpones a lot and feels bad about it",
               "most productive after 8 PM", "wants to change careers into UX design but never starts",
               "gets anxious from long forms or many questions"],
    "active_hour": 19,
    "history": [{"title": t, "days_ago": d, "hour": h, "minute": m, "dur_min": 30} for t, d, h, m in [
        ("reply to Ana", 1, 21, 10), ("laundry", 2, 20, 30), ("pay phone bill", 3, 22, 0),
        ("read chapter 3", 4, 21, 40), ("clean desk", 5, 20, 15), ("email landlord", 6, 23, 0),
        ("order groceries", 8, 20, 50), ("sketch logo idea", 9, 21, 30), ("buy stamps", 10, 10, 0)]],
    "open": [{"title": "get my portfolio together", "kind": "task", "days_ahead": None, "hour": None,
              "window": "someday", "dur_min": 240},
             {"title": "call the dentist", "kind": "task", "days_ahead": 0, "hour": 17, "window": None,
              "dur_min": 15}],
    "reactions": {"breakdown_offer": "usually says yes, a bit unsure ('ok yes, maybe')",
                  "why_question": "answers honestly: wants a job that feels creative",
                  "proposed_steps": "accepts if the first step looks tiny, asks for smaller steps otherwise",
                  "invented_views": "curious, tries them, undoes if they feel busy",
                  "classic_views": "neutral",
                  "postponed_messages": "relieved when wording is kind; undoes if the new time is in the morning",
                  "bulk_changes": "rarely"},
    "goals": ["start on the portfolio", "say a new big goal in one sentence (e.g. learn to swim in the next 3 months)",
              "dump everything on her mind", "let the evening clock run and see what moves",
              "finish one small thing"],
}

MINA = {
    "name": "Mina",
    "summary": "ER nurse on rotating 12-hour shifts; plans in short bursts on her phone before a shift, "
               "energy is best early morning, weekends are normal workdays for her",
    "traits": ["terse, types like a text message", "imports her shift roster as an .ics file",
               "says 'not now' to most breakdown offers", "dislikes clever or decorated views, wants plain lists",
               "early riser: finishes chores 6-9 AM"],
    "active_hour": 7,
    "history": [{"title": t, "days_ago": d, "hour": h, "minute": m, "dur_min": 20} for t, d, h, m in [
        ("renew license", 1, 7, 0), ("gym", 2, 6, 30), ("grocery run", 3, 8, 15), ("call pharmacy", 4, 7, 45),
        ("meal prep", 6, 8, 0), ("pay rent", 7, 6, 50), ("water plants", 8, 9, 0), ("text sis", 9, 22, 0)]],
    "open": [{"title": "night shift", "kind": "event", "days_ahead": 1, "hour": 19, "window": None, "dur_min": 720},
             {"title": "sort out taxes", "kind": "task", "days_ahead": None, "hour": None, "window": "this week",
              "dur_min": 180}],
    "reactions": {"breakdown_offer": "declines: 'not now' or 'no'",
                  "proposed_steps": "accepts only if 3 steps or fewer",
                  "invented_views": "dismisses them", "classic_views": "accepts a day view, dismisses workweek",
                  "postponed_messages": "fine, keeps them", "imports": "imports a shift roster with 3-5 shifts",
                  "bulk_changes": "clears a day when she picks up an extra shift"},
    "goals": ["import next week's shifts", "add errands around the shifts", "clear a day for an extra shift",
              "mark things done"],
}

JORDAN = {
    "name": "Jordan",
    "summary": "freelance illustrator juggling three clients and a part-time course; brain-dumps a lot, changes "
               "plans often, likes trying new interfaces",
    "traits": ["long rambling brain dumps with commas", "uses undo when the app guesses wrong",
               "moves and cancels things frequently", "likes novel, playful views",
               "works in bursts in the afternoon 14-18"],
    "active_hour": 13,
    "history": [{"title": t, "days_ago": d, "hour": h, "minute": m, "dur_min": 45} for t, d, h, m in [
        ("send invoice to Lumen", 1, 15, 0), ("thumbnail sketches", 2, 16, 30), ("client call Rafa", 3, 14, 0),
        ("color pass on cover", 4, 17, 0), ("course reading", 5, 15, 45), ("update website", 7, 16, 0),
        ("tax receipts", 8, 11, 0)]],
    "open": [{"title": "client call Lumen", "kind": "event", "days_ahead": 2, "hour": 15, "window": None,
              "dur_min": 60},
             {"title": "figure out pricing for 2027", "kind": "task", "days_ahead": None, "hour": None,
              "window": "this month", "dur_min": 120},
             {"title": "drinks with Theo", "kind": "event", "days_ahead": 5, "hour": 19, "window": None,
              "dur_min": 120}],
    "reactions": {"breakdown_offer": "says yes about half the time",
                  "proposed_steps": "asks for one change ('make step 2 smaller') then accepts",
                  "invented_views": "accepts eagerly", "classic_views": "dismisses as boring",
                  "postponed_messages": "undoes about half of them", "bulk_changes":
                  "clears the weekend for a trip, then sometimes undoes the whole plan"},
    "goals": ["brain dump the week", "empty out the weekend for a trip", "move a client call",
              "try any new view the app offers"],
}

SAM = {
    "name": "Sam",
    "summary": "parent of two with an office job, rigid weekday routine, keeps weekends free for family, "
               "checks the calendar to ask what is coming",
    "traits": ["asks questions like 'what's on thursday?'", "never plans anything on weekends",
               "likes predictable classic views (workweek)", "imports the school calendar",
               "short calm sentences"],
    "active_hour": 20,
    "history": [{"title": f"{t} (week {w})", "days_ago": 7 * w + d, "hour": h, "minute": 0, "dur_min": 30}
                for w in range(1, 6) for t, d, h in [("school run", 3, 8), ("team sync", 2, 10),
                                                     ("pay bills", 1, 21)]],
    "open": [{"title": "parent-teacher meeting", "kind": "event", "days_ahead": 3, "hour": 17, "window": None,
              "dur_min": 30},
             {"title": "deal with the garage clutter", "kind": "task", "days_ahead": None, "hour": None,
              "window": "this month", "dur_min": 180}],
    "reactions": {"breakdown_offer": "accepts for home projects", "proposed_steps": "accepts",
                  "invented_views": "dismisses", "classic_views": "accepts workweek, keeps it",
                  "postponed_messages": "keeps them", "imports": "imports a school calendar with 2-4 events",
                  "bulk_changes": "asks to clear a weekday afternoon for a school event"},
    "goals": ["ask what is on a day", "import the school calendar", "learn and accept a weekday view",
              "break down the garage project"],
}

DOC_PERSONAS = [IRIS, MINA, JORDAN, SAM]

SCHEMA_HINT = json.dumps(IRIS, indent=None)


def _extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)


def _valid(p: dict) -> bool:
    try:
        assert isinstance(p["name"], str) and p["traits"] and isinstance(p["reactions"], dict)
        assert 0 <= int(p["active_hour"]) <= 23
        for h in p["history"]:
            int(h["days_ago"]), int(h["hour"]), str(h["title"])
        for o in p.get("open", []):
            str(o["title"]), o.get("kind", "task")
        return len(p["history"]) >= 3
    except Exception:
        return False


def generate(n: int, seed: int, model: str = "crusoe:openai/gpt-oss-120b") -> list[dict]:
    """n new personas from the design principles; cached by (seed, n)."""
    path = HERE / "cache" / f"personas_{seed}_{n}.json"
    if path.exists():
        return json.loads(path.read_text())
    from sea import llm
    prompt = (
        f"Invent {n} distinct people who would use a gentle conversational calendar app built on these principles:\n- "
        + "\n- ".join(PRINCIPLES)
        + f"\n\nVariation seed: {seed}. Vary age, job, language style, rhythm of the day, and how they react to "
        "the app (some accept breakdowns, some decline; some like invented views, some dislike them; some import "
        ".ics files or ask for bulk changes like clearing a weekend). Do not reuse the names Iris, Mina, Jordan, Sam.\n"
        "Each person is a JSON object with exactly the keys of this example (history = 6-10 done items with "
        "days_ago 1-14, hour 0-23, that reveal when they usually get things done; open = 1-3 current items, at "
        "least one vague or big task; goals = 3-4 things to try in one session):\n"
        f"{SCHEMA_HINT}\n\nReturn ONLY a JSON array of {n} objects.")
    usage = {}
    out: list[dict] = []
    for _ in range(3):
        text, u = llm.chat(model, [{"role": "user", "content": prompt}], max_tokens=8000, temperature=0.9)
        llm.add_usage(usage, u)
        try:
            out = [p for p in _extract_json(text) if _valid(p)]
        except Exception:
            out = []
        if len(out) >= n:
            break
    if not out:
        raise RuntimeError("persona generation failed")
    for p in out:
        p["generated_seed"] = seed
    out = out[:n]
    path.write_text(json.dumps(out, indent=1))
    (HERE / "cache" / f"personas_{seed}_{n}.usage.json").write_text(json.dumps(usage))
    return out
