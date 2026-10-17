import json

from calevo.solver import solve, to_calendar_json
from calevo.verifier import extract_calendar, parse_constraint, to_min, verify

STATE = {
    "0": {"event_id": 0, "duration": 30, "constraint": "between 10:30am and 2pm", "min_time": "10:00", "max_time": "16:00"},
    "1": {"event_id": 1, "duration": 60, "constraint": "before 12pm", "min_time": "10:00", "max_time": "16:00"},
    "2": {"event_id": 2, "duration": 45, "constraint": "at 2:15pm", "min_time": "10:00", "max_time": "16:00"},
}


def cal(*rows):
    return json.dumps([{"event_id": i, "event_name": "x", "start_time": t, "duration": d} for i, t, d in rows])


def test_time_parse():
    assert to_min("2pm") == 840 and to_min("11:30am") == 690 and to_min("12pm") == 720 and to_min("14:05") == 845
    assert parse_constraint("between 10:30am and 2pm") == ("between", 630, 840)


def test_pass_and_failures():
    good = cal((1, "10:00", 60), (0, "11:00", 30), (2, "14:15", 45))
    assert verify("Sure!\n" + good, STATE)["score"] == 1.0
    assert verify(cal((1, "11:30", 60), (0, "11:00", 30), (2, "14:15", 45)), STATE)["reason"] == "constraint_violated"
    assert verify(cal((1, "10:00", 60), (0, "10:30", 30), (2, "14:15", 45)), STATE)["reason"] == "conflicting_events"
    assert verify(cal((1, "10:00", 60), (0, "11:00", 30)), STATE)["reason"] == "different_number_of_events"
    assert verify(cal((1, "10:00", 60), (0, "11:00", 30), (2, "14:20", 45)), STATE)["reason"] == "constraint_violated"
    assert verify(cal((1, "10:00", 60), (0, "11:00", 30), (2, "15:30", 45)), STATE)["reason"] == "constraint_violated"
    assert verify("no calendar", STATE)["reason"] == "no_json_list"


def test_extract_last_list():
    t = 'old [{"event_id":0}] new [{"event_id": 1, "x": [1,2]}]'
    assert extract_calendar(t) == [{"event_id": 1, "x": [1, 2]}]


def test_solver_roundtrip_and_prefs():
    ev = {int(k): v for k, v in STATE.items()}
    s = solve(ev)
    assert verify(json.dumps(to_calendar_json(ev, s)), STATE)["score"] == 1.0
    st = dict(STATE, _prefs=[{"type": "keep_free", "start": "12:00", "end": "13:00"},
                             {"type": "right_after", "first": 1, "second": 0}])
    s = solve(ev, st["_prefs"])
    r = verify(json.dumps(to_calendar_json(ev, s)), st)
    assert r["score"] == 1.0 and r["soft"] == 1.0


def test_c_type_prefs():
    from calevo.verifier import check_pref
    sched = {0: (600, 630), 1: (660, 720)}
    assert check_pref({"type": "avoid_window", "event_id": 0, "start": "10:30", "end": "11:00"}, sched)
    assert not check_pref({"type": "avoid_window", "event_id": 1, "start": "11:30", "end": "12:00"}, sched)
    assert check_pref({"type": "min_gap", "first": 0, "second": 1, "minutes": 30}, sched)
    assert not check_pref({"type": "min_gap", "first": 1, "second": 0, "minutes": 45}, sched)


def test_v2_prefs_and_weighted_score():
    from calevo.solver import solve_best, to_calendar_json
    from calevo.verifier import check_pref
    sched = {0: (600, 630), 1: (660, 720), 2: (720, 750)}
    assert check_pref({"type": "max_gap", "first": 0, "second": 1, "minutes": 30}, sched)
    assert not check_pref({"type": "max_gap", "first": 0, "second": 2, "minutes": 60}, sched)
    assert check_pref({"type": "buffer_around", "event_id": 0, "minutes": 30}, sched)
    assert not check_pref({"type": "buffer_around", "event_id": 1, "minutes": 5}, sched)
    ev = {int(k): v for k, v in STATE.items()}
    # conflicting prefs: event 1 cannot both end before 11:00 window AND sit in 11:00-12:00 (weights 2 vs 1)
    prefs = [{"type": "prefer_window", "event_id": 1, "start": "10:00", "end": "11:00", "weight": 2},
             {"type": "avoid_window", "event_id": 1, "start": "10:00", "end": "11:00", "weight": 1}]
    starts, best, chosen = solve_best(ev, prefs)
    assert best == 2 and chosen == [0]
    st = dict(STATE, _prefs=prefs, _best_w=best)
    r = verify(json.dumps(to_calendar_json(ev, starts)), st)
    assert r["score"] == 1.0, r


def test_math_and_drop_scoring():
    from calevo.tasks import drop_f1, math_equal
    assert math_equal("so the answer is \\boxed{\\frac{1}{2}}", "\\frac12") or math_equal("\\boxed{0.5}", "\\frac{1}{2}")
    assert math_equal("\\boxed{\\dfrac{3}{4}}", "\\frac{3}{4}")
    assert not math_equal("\\boxed{5}", "6")
    assert not math_equal("no box here: 6", "6")
    assert drop_f1("blah\nAnswer: 1,200", [["1200"]]) == 1.0
    assert drop_f1("Answer: the Bears", [["Bears"]]) == 1.0
    assert 0 < drop_f1("Answer: Chicago Bears", [["Bears"]]) < 1
