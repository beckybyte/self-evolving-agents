import copy
import re
from datetime import datetime, date, timedelta

CATALOG = [
    {"type": "MonthGrid", "props": {"month": "date"}, "children": False},
    {"type": "WeekColumns", "props": {"start": "date", "days": "int"}, "children": False},
    {"type": "DayTimeline", "props": {"date": "date", "from_hour": "int", "to_hour": "int"}, "children": False},
    {"type": "AgendaList", "props": {"from": "date", "to": "date"}, "children": False},
    {"type": "Lane", "props": {"label": "str", "filter": "filter"}, "children": True},
    {"type": "Bucket", "props": {"label": "str", "filter": "filter"}, "children": False},
]

CATALOG_BY_TYPE = {p["type"]: p for p in CATALOG}

FILTER_KEYS = ("kind", "category", "window", "hour_from", "hour_to")


# ---------------------------------------------------------------- helpers

def _now_dt(S):
    try:
        return datetime.strptime(str(S.get("now") or "2026-10-19T20:00")[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 20, 0)


def _items(S):
    try:
        items = S.get("items")
        if isinstance(items, list):
            return items
    except Exception:
        pass
    return []


def _parse_date_str(v):
    """Return a date for a 'YYYY-MM-DD' or 'YYYY-MM' string, else None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _item_date(item):
    s = item.get("start")
    if isinstance(s, str) and len(s) >= 10:
        d = _parse_date_str(s[:10])
        if d is not None:
            return d
    return None


def _item_hour(item):
    s = item.get("start")
    if isinstance(s, str) and len(s) >= 13:
        try:
            return int(s[11:13])
        except Exception:
            return None
    return None


def _matches(flt, item):
    if not isinstance(flt, dict):
        return True
    for k, v in flt.items():
        if k == "kind":
            if item.get("kind") != v:
                return False
        elif k == "category":
            if item.get("category") != v:
                return False
        elif k == "window":
            if item.get("window") != v:
                return False
        elif k == "hour_from":
            h = _item_hour(item)
            if h is None or not (h >= v):
                return False
        elif k == "hour_to":
            h = _item_hour(item)
            if h is None or not (h < v):
                return False
    return True


def _shown_ids(S, spec):
    """Set of item ids shown by the spec. Never raises."""
    shown = set()
    try:
        if not isinstance(spec, dict):
            return shown
        elements = spec.get("elements")
        if not isinstance(elements, dict):
            return shown
        root = spec.get("root")
        if root not in elements:
            return shown
        items = _items(S)
        visited = set()

        def walk(eid):
            if eid in visited:
                return
            visited.add(eid)
            el = elements.get(eid)
            if not isinstance(el, dict):
                return
            etype = el.get("type")
            props = el.get("props") if isinstance(el.get("props"), dict) else {}
            children = el.get("children") if isinstance(el.get("children"), list) else []
            if etype == "MonthGrid":
                md = _parse_date_str(props.get("month"))
                if md is not None:
                    ym = md.strftime("%Y-%m")
                    for it in items:
                        d = _item_date(it)
                        if d is not None and d.strftime("%Y-%m") == ym:
                            shown.add(it.get("id"))
            elif etype == "WeekColumns":
                sd = _parse_date_str(props.get("start"))
                days = props.get("days")
                if not isinstance(days, int) or isinstance(days, bool):
                    days = 7
                if sd is not None:
                    ed = sd + timedelta(days=days)
                    for it in items:
                        d = _item_date(it)
                        if d is not None and sd <= d < ed:
                            shown.add(it.get("id"))
            elif etype == "DayTimeline":
                dd = _parse_date_str(props.get("date"))
                fh = props.get("from_hour")
                th = props.get("to_hour")
                if not isinstance(fh, int) or isinstance(fh, bool):
                    fh = 0
                if not isinstance(th, int) or isinstance(th, bool):
                    th = 24
                if dd is not None:
                    for it in items:
                        d = _item_date(it)
                        h = _item_hour(it)
                        if d is not None and h is not None and d == dd and fh <= h < th:
                            shown.add(it.get("id"))
            elif etype == "AgendaList":
                fd = _parse_date_str(props.get("from"))
                td = _parse_date_str(props.get("to"))
                if fd is not None and td is not None:
                    for it in items:
                        d = _item_date(it)
                        if d is not None and fd <= d <= td:
                            shown.add(it.get("id"))
            elif etype == "Bucket":
                flt = props.get("filter")
                for it in items:
                    if _matches(flt, it):
                        shown.add(it.get("id"))
            elif etype == "Lane":
                flt = props.get("filter")
                if children:
                    before = set(shown)
                    for c in children:
                        walk(c)
                    if isinstance(flt, dict) and flt:
                        for iid in list(shown - before):
                            it = _find_item(items, iid)
                            if it is not None and not _matches(flt, it):
                                shown.discard(iid)
                else:
                    for it in items:
                        if _matches(flt, it):
                            shown.add(it.get("id"))
            for c in children:
                if isinstance(c, str):
                    walk(c)

        walk(root)
    except Exception:
        return shown
    return shown


def _find_item(items, iid):
    for it in items:
        if isinstance(it, dict) and it.get("id") == iid:
            return it
    return None


def _hidden_map(S, spec):
    shown = _shown_ids(S, spec)
    hidden = {}
    for it in _items(S):
        if not isinstance(it, dict):
            continue
        if it.get("id") in shown:
            continue
        d = _item_date(it)
        key = d.strftime("%Y-%m-%d") if d is not None else "unscheduled"
        hidden[key] = hidden.get(key, 0) + 1
    return hidden


# ---------------------------------------------------------------- catalog

def get_catalog(S):
    return {"primitives": copy.deepcopy(CATALOG)}


# ---------------------------------------------------------------- critic

def _check_props(eid, etype, props, problems):
    spec_props = CATALOG_BY_TYPE[etype]["props"]
    if not isinstance(props, dict):
        problems.append("element '%s' props must be an object" % eid)
        return
    for name, val in props.items():
        if name not in spec_props:
            problems.append("element '%s' has unknown prop '%s'" % (eid, name))
            continue
        kind = spec_props[name]
        if kind == "date":
            if _parse_date_str(val) is None:
                problems.append("element '%s' prop '%s' is not a valid date: '%s'" % (eid, name, val))
        elif kind == "int":
            if not isinstance(val, int) or isinstance(val, bool):
                problems.append("element '%s' prop '%s' must be an int" % (eid, name))
            else:
                if name in ("from_hour", "to_hour") and not (0 <= val <= 24):
                    problems.append("element '%s' prop '%s' must be an int between 0 and 24" % (eid, name))
                if name == "days" and not (1 <= val <= 31):
                    problems.append("element '%s' prop '%s' must be an int between 1 and 31" % (eid, name))
        elif kind == "str":
            if not isinstance(val, str):
                problems.append("element '%s' prop '%s' must be a str" % (eid, name))
        elif kind == "filter":
            if not isinstance(val, dict):
                problems.append("element '%s' prop '%s' must be a filter object" % (eid, name))
            else:
                for fk in val.keys():
                    if fk not in FILTER_KEYS:
                        problems.append("element '%s' prop '%s' has unknown filter key '%s'" % (eid, name, fk))


def critic(S, spec):
    problems = []
    try:
        if not isinstance(spec, dict):
            return {"ok": False, "problems": ["spec is not an object"], "repaired": None}
        elements = spec.get("elements")
        if not isinstance(elements, dict):
            return {"ok": False, "problems": ["spec 'elements' is not an object"], "repaired": None}
        root = spec.get("root")
        if root not in elements:
            problems.append("root '%s' missing from elements" % (root,))
        structural = 0
        for eid, el in elements.items():
            if not isinstance(el, dict):
                problems.append("element '%s' is not an object" % (eid,))
                structural += 1
                continue
            etype = el.get("type")
            if etype not in CATALOG_BY_TYPE:
                problems.append("element '%s' has unknown type '%s' (not in the catalog)" % (eid, etype))
                structural += 1
                continue
            children = el.get("children")
            if children is not None and not isinstance(children, list):
                problems.append("element '%s' children must be a list" % (eid,))
                structural += 1
                children = []
            children = children or []
            if children and not CATALOG_BY_TYPE[etype]["children"]:
                problems.append("element '%s' (%s) cannot have children" % (eid, etype))
                structural += 1
            for c in children:
                if c not in elements:
                    problems.append("element '%s' child '%s' missing from elements" % (eid, c))
                    structural += 1
        if structural == 0:
            for eid, el in elements.items():
                if isinstance(el, dict) and el.get("type") in CATALOG_BY_TYPE:
                    _check_props(eid, el.get("type"), el.get("props", {}), problems)
        if not problems:
            shown = _shown_ids(S, spec)
            counted = spec.get("hidden_counts") if isinstance(spec.get("hidden_counts"), dict) else {}
            for it in _items(S):
                if not isinstance(it, dict):
                    continue
                if it.get("id") in shown:
                    continue
                d = _item_date(it)
                key = d.strftime("%Y-%m-%d") if d is not None else "unscheduled"
                cnt = counted.get(key)
                if not (isinstance(cnt, int) and cnt >= 1):
                    problems.append("item '%s' (%s) is hidden: neither shown nor counted"
                                    % (it.get("title"), key))
        ok = not problems
        repaired = None
        if not ok:
            try:
                cand = repair(S, spec)
                if cand is not None and critic(S, cand)["ok"]:
                    repaired = cand
            except Exception:
                repaired = None
        return {"ok": ok, "problems": problems, "repaired": repaired}
    except Exception as e:
        return {"ok": False, "problems": ["critic error: %s" % (e,)], "repaired": None}


# ---------------------------------------------------------------- repair

def repair(S, spec):
    try:
        today_d = _now_dt(S).date()
        if not isinstance(spec, dict):
            spec = {}
        out = copy.deepcopy(spec)
        elements = out.get("elements")
        if not isinstance(elements, dict):
            elements = {}
        # drop unknown types
        for eid in list(elements.keys()):
            el = elements.get(eid)
            if not isinstance(el, dict) or el.get("type") not in CATALOG_BY_TYPE:
                del elements[eid]
        # clean children lists
        for eid, el in elements.items():
            if not isinstance(el, dict):
                continue
            children = el.get("children")
            if not isinstance(children, list):
                children = []
            if not CATALOG_BY_TYPE[el.get("type")]["children"]:
                children = []
            children = [c for c in children if c in elements and c != eid]
            el["children"] = children
        # clean props
        for eid, el in elements.items():
            etype = el.get("type")
            spec_props = CATALOG_BY_TYPE[etype]["props"]
            props = el.get("props")
            if not isinstance(props, dict):
                props = {}
            newprops = {}
            for name, val in props.items():
                if name not in spec_props:
                    continue
                kind = spec_props[name]
                if kind == "date":
                    if _parse_date_str(val) is None:
                        if name == "month":
                            newprops[name] = today_d.strftime("%Y-%m-01")
                        else:
                            newprops[name] = today_d.strftime("%Y-%m-%d")
                    else:
                        newprops[name] = val
                elif kind == "int":
                    if not isinstance(val, int) or isinstance(val, bool):
                        if name == "from_hour":
                            newprops[name] = 0
                        elif name == "to_hour":
                            newprops[name] = 24
                        elif name == "days":
                            newprops[name] = 7
                    else:
                        newprops[name] = val
                elif kind == "str":
                    if isinstance(val, str):
                        newprops[name] = val
                elif kind == "filter":
                    if isinstance(val, dict):
                        newprops[name] = {k: v for k, v in val.items() if k in FILTER_KEYS}
                    else:
                        newprops[name] = {}
            el["props"] = newprops
        out["elements"] = elements
        # root
        root = out.get("root")
        if root not in elements:
            listed = set()
            for el in elements.values():
                for c in el.get("children", []):
                    listed.add(c)
            free = [eid for eid in elements.keys() if eid not in listed]
            newroot = "repair_root"
            elements[newroot] = {"type": "Lane", "props": {"label": "All", "filter": {}},
                                 "children": free}
            out["root"] = newroot
        # hidden items -> catch-all
        shown = _shown_ids(S, out)
        counted = out.get("hidden_counts") if isinstance(out.get("hidden_counts"), dict) else {}
        still_hidden = False
        for it in _items(S):
            if not isinstance(it, dict) or it.get("id") in shown:
                continue
            d = _item_date(it)
            key = d.strftime("%Y-%m-%d") if d is not None else "unscheduled"
            cnt = counted.get(key)
            if not (isinstance(cnt, int) and cnt >= 1):
                still_hidden = True
                break
        if still_hidden:
            old_root = out.get("root")
            elements["repair_rest"] = {"type": "Bucket", "props": {"label": "Everything else", "filter": {}},
                                       "children": []}
            elements["repair_root"] = {"type": "Lane", "props": {"label": "All", "filter": {}},
                                       "children": [old_root, "repair_rest"]}
            out["root"] = "repair_root"
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- designer

def _best_habit_key(S):
    beliefs = S.get("beliefs")
    best_key = None
    best_conf = -1.0
    if isinstance(beliefs, dict):
        for k, b in beliefs.items():
            if not isinstance(b, dict):
                continue
            if b.get("level") != "habit":
                continue
            try:
                conf = float(b.get("confidence", 0))
            except Exception:
                conf = 0.0
            if conf > best_conf:
                best_conf = conf
                best_key = k
    return best_key


def _drafts(S, key):
    today_d = _now_dt(S).date()
    monday_d = today_d - timedelta(days=today_d.weekday())
    saturday_d = monday_d + timedelta(days=5)
    sunday_d = monday_d + timedelta(days=6)
    today_s = today_d.strftime("%Y-%m-%d")
    monday_s = monday_d.strftime("%Y-%m-%d")
    saturday_s = saturday_d.strftime("%Y-%m-%d")
    sunday_s = sunday_d.strftime("%Y-%m-%d")
    month_s = today_d.strftime("%Y-%m-01")
    week_end_s = (today_d + timedelta(days=6)).strftime("%Y-%m-%d")
    friday_s = (monday_d + timedelta(days=4)).strftime("%Y-%m-%d")

    def rest():
        return {"type": "Bucket", "props": {"label": "Everything else", "filter": {}}, "children": []}

    k = (key or "").lower()

    if "evening" in k:
        d0 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Your evenings", "filter": {}},
                     "children": ["timeline", "evening", "rest"]},
            "timeline": {"type": "DayTimeline", "props": {"date": today_s, "from_hour": 18, "to_hour": 24},
                         "children": []},
            "evening": {"type": "Bucket", "props": {"label": "Evening tasks",
                                                    "filter": {"hour_from": 18, "hour_to": 24}},
                        "children": []},
            "rest": rest()}}
        d1 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "This week", "filter": {}},
                     "children": ["lane", "rest"]},
            "lane": {"type": "Lane", "props": {"label": "Evening lane",
                                               "filter": {"hour_from": 18, "hour_to": 24}},
                     "children": ["week"]},
            "week": {"type": "WeekColumns", "props": {"start": monday_s, "days": 7}, "children": []},
            "rest": rest()}}
    elif "morning" in k:
        d0 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Your mornings", "filter": {}},
                     "children": ["timeline", "morning", "rest"]},
            "timeline": {"type": "DayTimeline", "props": {"date": today_s, "from_hour": 6, "to_hour": 12},
                         "children": []},
            "morning": {"type": "Bucket", "props": {"label": "Morning tasks",
                                                    "filter": {"hour_from": 6, "hour_to": 12}},
                        "children": []},
            "rest": rest()}}
        d1 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "This week", "filter": {}},
                     "children": ["lane", "rest"]},
            "lane": {"type": "Lane", "props": {"label": "Morning lane",
                                               "filter": {"hour_from": 6, "hour_to": 12}},
                     "children": ["week"]},
            "week": {"type": "WeekColumns", "props": {"start": monday_s, "days": 7}, "children": []},
            "rest": rest()}}
    elif "weekend" in k:
        d0 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Workweek", "filter": {}},
                     "children": ["week", "agenda", "rest"]},
            "week": {"type": "WeekColumns", "props": {"start": monday_s, "days": 5}, "children": []},
            "agenda": {"type": "AgendaList", "props": {"from": saturday_s, "to": sunday_s}, "children": []},
            "rest": rest()}}
        d1 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Weekdays first", "filter": {}},
                     "children": ["weekdays", "weekend", "rest"]},
            "weekdays": {"type": "AgendaList", "props": {"from": monday_s, "to": friday_s}, "children": []},
            "weekend": {"type": "AgendaList", "props": {"from": saturday_s, "to": sunday_s}, "children": []},
            "rest": rest()}}
    else:
        d0 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Agenda", "filter": {}},
                     "children": ["agenda", "rest"]},
            "agenda": {"type": "AgendaList", "props": {"from": today_s, "to": week_end_s}, "children": []},
            "rest": rest()}}
        d1 = {"root": "root", "elements": {
            "root": {"type": "Lane", "props": {"label": "Month", "filter": {}},
                     "children": ["month", "rest"]},
            "month": {"type": "MonthGrid", "props": {"month": month_s}, "children": []},
            "rest": rest()}}

    d2 = copy.deepcopy(d0)
    try:
        root_el = d2["elements"].get(d2["root"])
        if isinstance(root_el, dict):
            root_el["children"] = [c for c in root_el.get("children", []) if c != "rest"]
        d2["elements"].pop("rest", None)
    except Exception:
        pass
    return [d0, d1, d2]


def design(S, belief_key):
    try:
        key = belief_key or _best_habit_key(S) or "none"
        drafts = _drafts(S, key)
        critique = []
        repairs = 0
        chosen = 0
        for i, d in enumerate(drafts):
            c = critic(S, d)
            critique.append({"ok": c["ok"], "problems": c["problems"]})
            if not c["ok"] and c.get("repaired") is not None:
                repairs += 1
        for i, c in enumerate(critique):
            if c["ok"]:
                chosen = i
                break
        return {"drafts": drafts, "critique": critique, "repairs": repairs,
                "chosen": chosen, "proposal": None}
    except Exception:
        return {"drafts": [], "critique": [], "repairs": 0, "chosen": 0, "proposal": None}


def best_spec(S, belief_key):
    try:
        r = design(S, belief_key)
        drafts = r.get("drafts") or []
        chosen = r.get("chosen", 0)
        if not drafts:
            return None
        if 0 <= chosen < len(drafts):
            return drafts[chosen]
        return drafts[0]
    except Exception:
        return None


def hidden_counts(S, spec):
    try:
        return _hidden_map(S, spec)
    except Exception:
        return {}
