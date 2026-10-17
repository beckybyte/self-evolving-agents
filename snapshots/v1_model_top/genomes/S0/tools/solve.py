import json

from calevo.solver import solve, to_calendar_json
from calevo.verifier import extract_calendar


def run(bb, messages):
    events = extract_calendar(bb.get("extract", "")) or []
    ev, names = {}, {}
    for e in events:
        i = int(e["event_id"])
        ev[i] = {"duration": int(e["duration"]), "constraint": e.get("constraint") or None,
                 "min_time": "10:00", "max_time": "16:00"}
        names[i] = e.get("event_name", f"event {i}")
    try:
        starts = solve(ev)
    except Exception as exc:
        return {"text": "[]", "error": repr(exc)}
    if starts is None:
        return {"text": "[]", "error": "infeasible"}
    return {"text": json.dumps(to_calendar_json(ev, starts, names))}
