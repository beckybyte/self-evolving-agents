"""v3 view catalog, mechanical critic (+ repair) and the designer that drafts custom views from the catalog.
Mixed into App (app.py)."""
import copy
import datetime as dt
import json
import re

import llm
from timeparse import from_iso

CATALOG = [
    {"type": "MonthGrid", "props": {"month": "str"}, "children": False},
    {"type": "WeekColumns", "props": {"start": "date", "days": "int"}, "children": False},
    {"type": "DayTimeline", "props": {"date": "date", "from_hour": "int", "to_hour": "int"}, "children": False},
    {"type": "AgendaList", "props": {"from": "date", "to": "date"}, "children": False},
    {"type": "Lane", "props": {"label": "str", "filter": "str"}, "children": True},
    {"type": "Bucket", "props": {"label": "str", "filter": "str"}, "children": False},
]
TYPES = {c["type"]: c for c in CATALOG}
FILTER_KEYS = {"kind", "category", "window", "hour_from", "hour_to"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _is_date(v):
    if not isinstance(v, str) or not DATE_RE.match(v):
        return False
    try:
        dt.date.fromisoformat(v)
        return True
    except ValueError:
        return False


def _kind_ok(kind, v, name):
    if name == "filter":
        return isinstance(v, (dict, str))
    if name == "month":
        return isinstance(v, str) and (bool(MONTH_RE.match(v)) or _is_date(v))
    if kind == "date":
        return _is_date(v)
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "str":
        return isinstance(v, str)
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "[str]":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    return True


def norm_filter(f):
    """Filter as a dict. Strings like 'evening', 'kind=event' are accepted."""
    if isinstance(f, dict):
        return f
    if not f:
        return {}
    s = str(f).strip().lower()
    m = re.match(r"^(\w+)\s*[=:]\s*(.+)$", s)
    if m and m.group(1) in FILTER_KEYS:
        v = m.group(2).strip()
        return {m.group(1): int(v) if v.isdigit() else v}
    if s in ("evening", "evenings", "night"):
        return {"hour_from": 17, "hour_to": 24}
    if s in ("morning", "mornings"):
        return {"hour_from": 5, "hour_to": 12}
    if s in ("afternoon",):
        return {"hour_from": 12, "hour_to": 17}
    if s in ("task", "event", "note", "tasks", "events", "notes"):
        return {"kind": s.rstrip("s")}
    return {"window": s}


def filter_match(f, it, start):
    f = norm_filter(f)
    if f.get("kind") and it["kind"] != f["kind"]:
        return False
    if f.get("category") and (it.get("category") or it["kind"]) != f["category"]:
        return False
    if f.get("window") and f["window"].lower() not in (it.get("window") or "").lower():
        return False
    if "hour_from" in f or "hour_to" in f:
        if not start:
            return False
        h = start.hour + start.minute / 60
        try:
            lo, hi = float(f.get("hour_from", 0)), float(f.get("hour_to", 24))
        except (TypeError, ValueError):
            return False
        if not (lo <= h < hi):
            return False
    return True


class DesignerMixin:
    # ------------------------------------------------------------ coverage
    def _range_items(self, a, b, hours=None):
        """ids of dated items overlapping [a, b) (dates), optionally within hours (from, to)."""
        out = set()
        for it in self.items.values():
            s = from_iso(it["start"])
            if not s:
                continue
            e = self.eff_end(it) or s
            last = (e - dt.timedelta(minutes=1)).date() if e > s else s.date()
            if not (s.date() < b and last >= a):
                continue
            if hours:
                fh, th = hours
                h = s.hour + s.minute / 60
                eh = (e.hour + e.minute / 60) if e.date() == s.date() else 24
                if eh <= fh or h >= th:
                    continue
            out.add(it["id"])
        return out

    def element_items(self, spec, eid, inherited=None, seen=None):
        """(shown ids of non-bucket descendants, list of bucket element ids) for element eid."""
        seen = seen if seen is not None else set()
        el = spec["elements"].get(eid)
        if eid in seen or not isinstance(el, dict):
            return set(), []
        seen.add(eid)
        t, p = el.get("type"), el.get("props") or {}
        shown, buckets = set(), []
        try:
            if t == "MonthGrid":
                m = str(p.get("month") or self.nowdt.strftime("%Y-%m"))[:7]
                a = dt.date.fromisoformat(m + "-01")
                b = (a.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                shown = self._range_items(a, b)
            elif t == "WeekColumns":
                a = dt.date.fromisoformat(p.get("start") or self.nowdt.date().isoformat())
                shown = self._range_items(a, a + dt.timedelta(days=int(p.get("days") or 7)))
            elif t == "DayTimeline":
                a = dt.date.fromisoformat(p.get("date") or self.nowdt.date().isoformat())
                shown = self._range_items(a, a + dt.timedelta(days=1),
                                          (int(p.get("from_hour", 0)), int(p.get("to_hour", 24))))
            elif t == "AgendaList":
                a = dt.date.fromisoformat(p.get("from") or self.nowdt.date().isoformat())
                b = dt.date.fromisoformat(p.get("to") or (a + dt.timedelta(days=6)).isoformat())
                shown = self._range_items(a, b + dt.timedelta(days=1))
            elif t == "Lane":
                kids = el.get("children") or []
                if kids:
                    for c in kids:
                        s, bk = self.element_items(spec, c, None, seen)
                        shown |= s
                        buckets += bk
                else:
                    shown = {it["id"] for it in self.items.values()}
                f = p.get("filter")
                shown = {i for i in shown if filter_match(f, self.items[i], from_iso(self.items[i]["start"]))}
            elif t == "Bucket":
                buckets.append(eid)
        except (ValueError, TypeError):
            return set(), buckets
        return shown, buckets

    def spec_coverage(self, spec):
        """Returns (shown ids, {bucket eid: [ids]}, uncovered ids)."""
        shown, buckets = self.element_items(spec, spec.get("root"))
        rest = [i for i in self.items if i not in shown]
        fill = {}
        for b in buckets:
            f = (spec["elements"][b].get("props") or {}).get("filter")
            fill[b] = [i for i in rest if filter_match(f, self.items[i], from_iso(self.items[i]["start"]))]
        counted = {i for ids in fill.values() for i in ids}
        return shown, fill, [i for i in rest if i not in counted]

    # ------------------------------------------------------------ critic
    def critic(self, spec):
        problems = []
        if not isinstance(spec, dict) or not isinstance(spec.get("elements"), dict):
            return {"ok": False, "problems": ["spec must be an object with 'root' and 'elements'"],
                    "repaired": self.repair(spec)}
        els = spec["elements"]
        if spec.get("root") not in els:
            problems.append(f"root '{spec.get('root')}' is missing from elements")
        for eid, el in els.items():
            if not isinstance(el, dict):
                problems.append(f"element '{eid}' is not an object")
                continue
            t = el.get("type")
            if t not in TYPES:
                problems.append(f"element '{eid}' uses unknown type '{t}' (not in the catalog)")
                continue
            for c in el.get("children") or []:
                if c not in els:
                    problems.append(f"child '{c}' of element '{eid}' is missing from elements")
            if el.get("children") and not TYPES[t]["children"]:
                problems.append(f"element '{eid}' of type {t} cannot have children")
            for name, v in (el.get("props") or {}).items():
                kind = TYPES[t]["props"].get(name)
                if kind is None:
                    continue
                if not _kind_ok(kind, v, name):
                    problems.append(f"prop '{name}' of element '{eid}' ({t}) must be a {kind}"
                                    f"{' (YYYY-MM-DD)' if kind == 'date' else ''}, got {json.dumps(v)}")
                elif name == "filter" and isinstance(v, dict) and set(v) - FILTER_KEYS:
                    problems.append(f"filter of element '{eid}' has unknown keys {sorted(set(v) - FILTER_KEYS)}")
                elif name == "filter" and isinstance(v, dict) and any(
                        k in v and (not isinstance(v[k], (int, float)) or isinstance(v[k], bool))
                        for k in ("hour_from", "hour_to")):
                    problems.append(f"filter of element '{eid}': hour_from/hour_to must be numbers (int)")
        if not problems:
            _, _, unc = self.spec_coverage(spec)
            for i in unc:
                problems.append(f"item '{i}' ({self.items[i]['title']}) would be neither shown nor counted")
        ok = not problems
        return {"ok": ok, "problems": problems, "repaired": None if ok else self.repair(spec)}

    def repair(self, spec):
        today = self.nowdt.date()
        if not isinstance(spec, dict) or not isinstance(spec.get("elements"), dict):
            spec = {"root": "root", "elements": {}}
        spec = copy.deepcopy(spec)
        els = {k: v for k, v in spec["elements"].items() if isinstance(v, dict) and v.get("type") in TYPES}
        for eid, el in els.items():
            t = el["type"]
            el["children"] = [c for c in (el.get("children") or []) if c in els and c != eid] if TYPES[t]["children"] \
                else []
            props = el.setdefault("props", {})
            for name, v in list(props.items()):
                kind = TYPES[t]["props"].get(name)
                if kind is None:
                    continue
                if not _kind_ok(kind, v, name):
                    props[name] = self._default_prop(t, name, today)
                elif name == "filter" and isinstance(v, dict):
                    fx = {}
                    for k, x in v.items():
                        if k not in FILTER_KEYS:
                            continue
                        if k in ("hour_from", "hour_to"):
                            try:
                                x = int(float(x))
                            except (TypeError, ValueError):
                                continue
                        fx[k] = x
                    props[name] = fx
        root = spec.get("root")
        if root not in els:
            kids = [k for k in els if not any(k in (e.get("children") or []) for e in els.values())]
            root = "root"
            while root in els:
                root += "_"
            els[root] = {"type": "Lane", "props": {"label": "My view", "filter": {}}, "children": kids}
        spec = {"root": root, "elements": els}
        _, _, unc = self.spec_coverage(spec)
        if unc:
            if els[root]["type"] != "Lane" or (els[root]["props"].get("filter") or {}):
                new_root = "root_lane"
                while new_root in els:
                    new_root += "_"
                els[new_root] = {"type": "Lane", "props": {"label": "My view", "filter": {}}, "children": [root]}
                root = new_root
            bid = "everything_else"
            while bid in els:
                bid += "_"
            els[bid] = {"type": "Bucket", "props": {"label": "Everything else", "filter": {}}, "children": []}
            els[root]["children"].append(bid)
            spec = {"root": root, "elements": els}
        return spec

    def _default_prop(self, t, name, today):
        mon = today - dt.timedelta(days=today.weekday())
        return {"month": today.strftime("%Y-%m"), "start": mon.isoformat(), "days": 7, "date": today.isoformat(),
                "from_hour": 7, "to_hour": 24, "from": today.isoformat(),
                "to": (today + dt.timedelta(days=6)).isoformat(), "label": t, "filter": {}}.get(name)

    # ------------------------------------------------------------ designer
    def rule_drafts(self, key):
        today = self.nowdt.date()
        mon = today - dt.timedelta(days=today.weekday())
        d = lambda k: (today + dt.timedelta(days=k)).isoformat()  # noqa: E731
        rest = {"type": "Bucket", "props": {"label": "Everything else", "filter": {}}, "children": []}
        if key == "evening_worker":
            return [
                {"root": "evenings", "elements": {
                    "evenings": {"type": "Lane", "props": {"label": "Your evenings", "filter": {}},
                                 "children": ["ev0", "ev1", "ev2", "rest"]},
                    "ev0": {"type": "DayTimeline", "props": {"date": d(0), "from_hour": 17, "to_hour": 24}, "children": []},
                    "ev1": {"type": "DayTimeline", "props": {"date": d(1), "from_hour": 17, "to_hour": 24}, "children": []},
                    "ev2": {"type": "DayTimeline", "props": {"date": d(2), "from_hour": 17, "to_hour": 24}, "children": []},
                    "rest": dict(rest, props={"label": "Daytime and later", "filter": {}})}},
                {"root": "lanes", "elements": {
                    "lanes": {"type": "Lane", "props": {"label": "This week by energy", "filter": {}},
                              "children": ["evening", "day", "rest"]},
                    "evening": {"type": "Lane", "props": {"label": "Evening (when you get things done)",
                                                          "filter": {"hour_from": 17, "hour_to": 24}},
                                "children": ["wk1"]},
                    "wk1": {"type": "WeekColumns", "props": {"start": mon.isoformat(), "days": 7}, "children": []},
                    "day": {"type": "Bucket", "props": {"label": "Daytime", "filter": {"hour_from": 0, "hour_to": 17}},
                            "children": []},
                    "rest": rest}},
            ]
        if key == "no_weekends":
            return [
                {"root": "strip", "elements": {
                    "strip": {"type": "Lane", "props": {"label": "Weekdays", "filter": {}},
                              "children": ["wd", "weekend"]},
                    "wd": {"type": "WeekColumns", "props": {"start": mon.isoformat(), "days": 5}, "children": []},
                    "weekend": {"type": "Bucket", "props": {"label": "Weekend and beyond, folded", "filter": {}},
                                "children": []}}},
                {"root": "agenda", "elements": {
                    "agenda": {"type": "Lane", "props": {"label": "Mon to Fri", "filter": {}},
                               "children": ["list", "rest"]},
                    "list": {"type": "AgendaList", "props": {"from": mon.isoformat(),
                                                             "to": (mon + dt.timedelta(days=4)).isoformat()},
                             "children": []},
                    "rest": dict(rest, props={"label": "Weekend and the rest", "filter": {}})}},
            ]
        if key == "morning_worker":
            return [
                {"root": "mornings", "elements": {
                    "mornings": {"type": "Lane", "props": {"label": "Your mornings", "filter": {}},
                                 "children": ["m0", "m1", "m2", "rest"]},
                    "m0": {"type": "DayTimeline", "props": {"date": d(0), "from_hour": 5, "to_hour": 12}, "children": []},
                    "m1": {"type": "DayTimeline", "props": {"date": d(1), "from_hour": 5, "to_hour": 12}, "children": []},
                    "m2": {"type": "DayTimeline", "props": {"date": d(2), "from_hour": 5, "to_hour": 12}, "children": []},
                    "rest": dict(rest, props={"label": "Afternoons, evenings and later", "filter": {}})}},
                {"root": "lanes", "elements": {
                    "lanes": {"type": "Lane", "props": {"label": "This week by energy", "filter": {}},
                              "children": ["morning", "rest"]},
                    "morning": {"type": "Lane", "props": {"label": "Morning (when you get things done)",
                                                          "filter": {"hour_from": 0, "hour_to": 12}},
                                "children": ["wk1"]},
                    "wk1": {"type": "WeekColumns", "props": {"start": mon.isoformat(), "days": 7}, "children": []},
                    "rest": rest}},
            ]
        if key == "daytime_plans":
            return [
                {"root": "work", "elements": {
                    "work": {"type": "Lane", "props": {"label": "Working hours", "filter": {}},
                             "children": ["today", "rest"]},
                    "today": {"type": "DayTimeline", "props": {"date": d(0), "from_hour": 8, "to_hour": 18},
                              "children": []},
                    "rest": dict(rest, props={"label": "Outside 8 to 6, and other days", "filter": {}})}},
                {"root": "wk", "elements": {
                    "wk": {"type": "Lane", "props": {"label": "Daytime this week", "filter": {"hour_from": 8, "hour_to": 18}},
                           "children": ["cols"]},
                    "cols": {"type": "WeekColumns", "props": {"start": mon.isoformat(), "days": 7}, "children": []}}},
            ]
        if key == "short_tasks":
            return [
                {"root": "q", "elements": {
                    "q": {"type": "Lane", "props": {"label": "Quick wins first", "filter": {}},
                          "children": ["tasks", "cal", "rest"]},
                    "tasks": {"type": "Bucket", "props": {"label": "Open tasks", "filter": {"kind": "task"}},
                              "children": []},
                    "cal": {"type": "AgendaList", "props": {"from": d(0), "to": d(6)}, "children": []},
                    "rest": rest}},
                {"root": "ag", "elements": {
                    "ag": {"type": "Lane", "props": {"label": "Next 3 days", "filter": {}}, "children": ["list", "rest"]},
                    "list": {"type": "AgendaList", "props": {"from": d(0), "to": d(2)}, "children": []},
                    "rest": rest}},
            ]
        return [
            {"root": "wk", "elements": {
                "wk": {"type": "Lane", "props": {"label": "This week", "filter": {}}, "children": ["cols", "rest"]},
                "cols": {"type": "WeekColumns", "props": {"start": mon.isoformat(), "days": 7}, "children": []},
                "rest": rest}},
            {"root": "ag", "elements": {
                "ag": {"type": "Lane", "props": {"label": "Next two weeks", "filter": {}}, "children": ["list", "rest"]},
                "list": {"type": "AgendaList", "props": {"from": d(0), "to": d(13)}, "children": []},
                "rest": rest}},
        ]

    def llm_draft(self, key):
        b = self.beliefs.get(key) if key else None
        got = llm.chat_json([
            {"role": "system", "content": "You design ONE calendar view as JSON from a fixed catalog of building blocks. "
             "Catalog: " + json.dumps(CATALOG) + ". Filter objects use keys kind, category, window, hour_from, hour_to. "
             "Dates are YYYY-MM-DD, month is YYYY-MM. Reply with only JSON: {\"root\": id, \"elements\": {id: "
             "{\"type\": ..., \"props\": {...}, \"children\": [ids]}}}. Always add a Bucket labelled 'Everything else' "
             "so no item is ever hidden."},
            {"role": "user", "content": json.dumps({"today": self.nowdt.date().isoformat(),
                                                    "belief": b["statement"] if b else "no strong habit yet"})}],
            smart=False)
        if isinstance(got, dict) and isinstance(got.get("elements"), dict):
            return got
        return None

    def design(self, key=None):
        # belief-specific rule drafts first (deterministic, tied to the habit); the model adds one more idea
        drafts = self.rule_drafts(key)
        ld = self.llm_draft(key)
        if ld:
            drafts.append(ld)
        critique, repairs, final = [], 0, []
        for dr in drafts:
            try:
                c = self.critic(dr)
            except Exception as e:  # a malformed model draft must never break the request
                c = {"ok": False, "problems": [f"draft could not be checked: {e}"], "repaired": None}
            critique.append({"ok": c["ok"], "problems": c["problems"]})
            if c["ok"]:
                final.append(dr)
            elif c["repaired"] is not None and self._safe_ok(c["repaired"]):
                repairs += 1
                final.append(c["repaired"])
            else:
                final.append(None)
        # prefer a rule draft tied to the belief when the model draft needed repair
        chosen = next((i for i, (dr, c) in enumerate(zip(final, critique)) if dr is not None and c["ok"]), None)
        if chosen is None:
            chosen = next(i for i, dr in enumerate(final) if dr is not None)
        drafts = [f if f is not None else d for f, d in zip(final, drafts)]
        self.act("designer", "design", f"Drafted {len(drafts)} views for {key or 'no belief'}; critic repairs: "
                 f"{repairs}; chose draft {chosen}")
        return {"drafts": drafts, "critique": critique, "repairs": repairs, "chosen": chosen}

    def _safe_ok(self, spec):
        try:
            return self.critic(spec)["ok"]
        except Exception:
            return False

    def design_endpoint(self, key):
        r = self.design(key)
        prop = self.gate(only_key=key, design=r) if key else None
        r["proposal"] = prop
        return r
