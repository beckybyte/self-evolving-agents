"""Backtracking scheduler over 5-minute slots.

Used for (1) feasibility checks of every sample (W1/W3 targets) and (2) as the
reference tool behind the fixed-DSL solver genome (S0).
"""
from __future__ import annotations

from .verifier import check_pref, hard_ok, parse_constraint, to_min


class SearchBudgetExceeded(Exception):
    pass


def solve(events: dict[int, dict], prefs: list[dict] | None = None, step: int = 5, budget: int = 30_000_000):
    """events: id -> {"duration": int, "constraint": str|None, "min_time", "max_time"}.

    Returns id -> start minute satisfying all hard constraints and, if possible, all prefs;
    None if no hard-feasible schedule exists. Raises SearchBudgetExceeded on pathological inputs.
    """
    prefs = prefs or []
    for required in ([prefs, []] if prefs else [[]]):
        sol = _search(events, required, step, budget)
        if sol is not None:
            return sol
    return None


def solve_best(events: dict[int, dict], prefs: list[dict], step: int = 5, budget: int = 200_000):
    """Max-weight subset of prefs that is jointly satisfiable with all hard constraints.

    Returns (starts, best_weight, chosen_pref_indices); starts is None if hard-infeasible.
    Brute force over subsets by descending weight (fine for <= 5 prefs).
    """
    from itertools import combinations
    n = len(prefs)
    subsets = [c for k in range(n, -1, -1) for c in combinations(range(n), k)]
    subsets.sort(key=lambda c: -sum(prefs[i].get("weight", 1) for i in c))
    for c in subsets:
        try:
            sol = _search(events, [prefs[i] for i in c], step, budget)
        except SearchBudgetExceeded:
            continue
        if sol is not None:
            return sol, sum(prefs[i].get("weight", 1) for i in c), list(c)
    return None, 0, []


def _domain(e: dict, step: int) -> list[int]:
    lo, hi, d = to_min(e.get("min_time", "10:00")), to_min(e.get("max_time", "16:00")), int(e["duration"])
    c = parse_constraint(e.get("constraint"))
    if c and c[0] == "at":
        return [c[1]] if lo <= c[1] and c[1] + d <= hi else []
    return [s for s in range(lo, hi - d + 1, step) if hard_ok(c, s, d)]


def _search(events, prefs, step, budget):
    dur = {i: int(e["duration"]) for i, e in events.items()}
    dom = {i: _domain(e, step) for i, e in events.items()}
    ids = sorted(events, key=lambda i: (len(dom[i]), -dur[i]))  # most constrained first
    assign: dict[int, int] = {}
    nodes = 0

    def free(i, s):
        e = s + dur[i]
        return all(e <= assign[j] or s >= assign[j] + dur[j] for j in assign)

    def bt(k):
        nonlocal nodes
        if k == len(ids):
            sched = {i: (assign[i], assign[i] + dur[i]) for i in ids}
            return all(check_pref(p, sched) for p in prefs)
        i = ids[k]
        for s in dom[i]:
            nodes += 1
            if nodes > budget:
                raise SearchBudgetExceeded
            if free(i, s):
                assign[i] = s
                if bt(k + 1):
                    return True
                del assign[i]
        return False

    return dict(assign) if bt(0) else None


def to_calendar_json(events: dict[int, dict], starts: dict[int, int], names: dict[int, str] | None = None):
    names = names or {}
    return [
        {"event_id": i, "event_name": names.get(i, f"event {i}"),
         "start_time": f"{starts[i] // 60:02d}:{starts[i] % 60:02d}", "duration": int(events[i]["duration"])}
        for i in sorted(starts, key=lambda i: starts[i])
    ]
