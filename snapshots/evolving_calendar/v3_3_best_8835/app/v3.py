import copy
import re
from datetime import datetime, date, time as dtime, timedelta

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

INT_DEFAULTS = {"from_hour": 0, "to_hour": 24, "days": 7}


# ---------------------------------------------------------------- helpers

def _now_dt(S):
    try:
        return datetime.strptime(str(S.get("now") or "2026-10-19T20:00")[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 20, 0)


def _today_d(S):
    return _now_dt(S).date()


def _items(S):
    items = S.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def _item_date(it):
    s = it.get("start")
    if isinstance(s, str) and len(s) >= 10:
        return s[:10]
    return None


def _item_hour(it):
    s = it.get("start")
    if isinstance(s, str) and len(s) >= 13:
        try:
            return int(s[11:13])
        except Exception:
            return None
    return None


def _parse_date_str(v):
    if not isinstance(v, str):
        return None
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(v, fmt).date()
        except Exception:
            continue
    return None


def _date_str(d):
    return d.strftime("%Y-%m-%d")


def _month_str(d):
    return d.strftime("%Y-%m-01")


def _matches(flt, it):
    if not isinstance(flt, dict):
        return False
    for k, v in flt.items():
        if k == "kind":
            if it.get("kind") != v:
                return False
        elif k == "category":
            if it.get("category") != v:
                return False
        elif k == "window":
            if it.get("window") != v:
                return False
        elif k == "hour_from":
            h = _item_hour(it)
            if h is None or not (h >= v):
                return False
        elif k == "hour_to":
            h = _item_hour(it)
            if h is None or not (h < v):
                return False
        else:
            return False
    return True


def _shown_ids(S, spec):
    """Return set of item ids shown by spec. Never raises."""
    shown = set()
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
            m = props.get("month")
            md = _parse_date_str(m)
            if md is not None:
                key = md.strftime("%Y-%m")
                for it in items:
                    d = _item_date(it)
                    if d and d[:7] == key:
                        shown.add(it.get("id"))
        elif etype == "WeekColumns":
            sd = _parse_date_str(props.get("start"))
            days = props.get("days")
            if sd is not None and isinstance(days, int) and not isinstance(days, bool):
                end_d = sd + timedelta(days=days)
                for it in items:
                    d = _item_date(it)
                    if not d:
                        continue
                    try:
                        dd = datetime.strptime(d, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if sd <= dd < end_d:
                        shown.add(it.get("id"))
        elif etype == "DayTimeline":
            dd = _parse_date_str(props.get("date"))
            fh = props.get("from_hour")
            th = props.get("to_hour")
            if dd is not None and isinstance(fh, int) and isinstance(th, int):
                for it in items:
                    d = _item_date(it)
                    h = _item_hour(it)
                    if d == _date_str(dd) and h is not None and fh <= h < th:
                        shown.add(it.get("id"))
        elif etype == "AgendaList":
            fd = _parse_date_str(props.get("from"))
            td = _parse_date_str(props.get("to"))
            if fd is not None and td is not None:
                for it in items:
                    d = _item_date(it)
                    if not d:
                        continue
                    try:
                        dd = datetime.strptime(d, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if fd <= dd <= td:
                        shown.add(it.get("id"))
        elif etype == "Bucket":
            flt = props.get("filter")
            if not isinstance(flt, dict):
                flt = {}
            for it in items:
                if _matches(flt, it):
                    shown.add(it.get("id"))
        elif etype == "Lane":
            flt = props.get("filter")
            if not isinstance(flt, dict):
                flt = {}
            if children:
                before = set(shown)
                for c in children:
                    walk(c)
                added = shown - before
                for it in items:
                    if it.get("id") in added and not _matches(flt, it):
                        shown.discard(it.get("id"))
            else:
                for it in items:
                    if _matches(flt, it):
                        shown.add(it.get("id"))
        else:
            for c in children:
                walk(c)

    walk(root)
    return shown


def hidden_counts(S, spec):
    shown = _shown_ids(S, spec)
    counts = {}
    for it in _items(S):
        if it.get("id") in shown:
            continue
        d = _item_date(it)
        key = d if d else "unscheduled"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------- catalog

def get_catalog(S):
    return {"primitives": copy.deepcopy(CATALOG)}


# ---------------------------------------------------------------- critic

def _check_props(eid, etype, props, problems):
    cat = CATALOG_BY_TYPE.get(etype)
    if cat is None:
        return
    allowed = cat["props"]
    if not isinstance(props, dict):
        problems.append("element '%s' props must be an object" % eid)
        return
    for name, val in props.items():
        if name not in allowed:
            problems.append("element '%s' has unknown prop '%s'" % (eid, name))
            continue
        kind = allowed[name]
        if kind == "date":
            if not isinstance(val, str):
                problems.append("element '%s' prop '%s' is not a valid date: %r" % (eid, name, val))
                continue
            ok = False
            if etype == "MonthGrid" and name == "month":
                ok = _parse_date_str(val) is not None
            else:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                problems.append("element '%s' prop '%s' is not a valid date: '%s'" % (eid, name, val))
        elif kind == "int":
            if not isinstance(val, int) or isinstance(val, bool):
                problems.append("element '%s' prop '%s' must be an int" % (eid, name))
                continue
            if name in ("from_hour", "to_hour"):
                if not (0 <= val <= 24):
                    problems.append("element '%s' prop '%s' must be an int between 0 and 24" % (eid, name))
            elif name == "days":
                if not (1 <= val <= 31):
                    problems.append("element '%s' prop '%s' must be an int between 1 and 31" % (eid, name))
        elif kind == "str":
            if not isinstance(val, str):
                problems.append("element '%s' prop '%s' must be a str" % (eid, name))
        elif kind == "filter":
            if not isinstance(val, dict):
                problems.append("element '%s' prop '%s' must be a filter object" % (eid, name))
                continue
            for fk in val.keys():
                if fk not in FILTER_KEYS:
                    problems.append("element '%s' prop '%s' has unknown filter key '%s'" % (eid, name, fk))


def critic(S, spec):
    problems = []
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
        cat = CATALOG_BY_TYPE.get(etype)
        if cat is None:
            problems.append("element '%s' has unknown type '%s' (not in the catalog)" % (eid, etype))
            structural += 1
            continue
        children = el.get("children")
        if children is not None and not isinstance(children, list):
            problems.append("element '%s' children must be a list" % (eid,))
            structural += 1
            children = []
        if children:
            if not cat["children"]:
                problems.append("element '%s' (%s) cannot have children" % (eid, etype))
                structural += 1
            for c in children:
                if c not in elements:
                    problems.append("element '%s' child '%s' missing from elements" % (eid, c))
                    structural += 1
        _check_props(eid, etype, el.get("props", {}), problems)

    if structural == 0:
        shown = _shown_ids(S, spec)
        hc = spec.get("hidden_counts")
        if not isinstance(hc, dict):
            hc = {}
        for it in _items(S):
            if it.get("id") in shown:
                continue
            d = _item_date(it)
            key = d if d else "unscheduled"
            cnt = hc.get(key)
            if not (isinstance(cnt, int) and cnt >= 1):
                problems.append("item '%s' (%s) is hidden: neither shown nor counted" % (it.get("title"), d or "unscheduled"))

    ok = len(problems) == 0
    repaired = None
    if not ok:
        rep = _repair(S, spec)
        if rep is not None:
            rc = critic(S, rep)
            if rc["ok"]:
                repaired = rep
    return {"ok": ok, "problems": problems, "repaired": repaired}


def _repair(S, spec):
    try:
        return _repair_inner(S, spec)
    except Exception:
        return None


def _repair_inner(S, spec):
    today_d = _today_d(S)
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
    # clean children + props
    for eid, el in list(elements.items()):
        cat = CATALOG_BY_TYPE[el.get("type")]
        children = el.get("children")
        if not isinstance(children, list):
            children = []
        if not cat["children"]:
            children = []
        children = [c for c in children if c in elements and c != eid]
        el["children"] = children
        props = el.get("props")
        if not isinstance(props, dict):
            props = {}
        newprops = {}
        for name, kind in cat["props"].items():
            if name not in props:
                continue
            val = props[name]
            if kind == "date":
                if isinstance(val, str):
                    if el.get("type") == "MonthGrid" and name == "month":
                        pd = _parse_date_str(val)
                        if pd is not None:
                            newprops[name] = _month_str(pd)
                    else:
                        try:
                            datetime.strptime(val, "%Y-%m-%d")
                            newprops[name] = val
                        except Exception:
                            newprops[name] = _date_str(today_d)
                else:
                    newprops[name] = _date_str(today_d)
            elif kind == "int":
                if isinstance(val, int) and not isinstance(val, bool):
                    if name in ("from_hour", "to_hour") and not (0 <= val <= 24):
                        newprops[name] = INT_DEFAULTS.get(name, 0)
                    elif name == "days" and not (1 <= val <= 31):
                        newprops[name] = INT_DEFAULTS.get(name, 7)
                    else:
                        newprops[name] = val
                else:
                    newprops[name] = INT_DEFAULTS.get(name, 0)
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
        orphans = [eid for eid in elements.keys() if eid not in listed]
        newroot = "root"
        i = 0
        while newroot in elements:
            i += 1
            newroot = "root_%d" % i
        elements[newroot] = {"type": "Lane", "props": {"label": "All", "filter": {}}, "children": orphans}
        out["root"] = newroot
    # ensure nothing hidden
    shown = _shown_ids(S, out)
    missing = [it for it in _items(S) if it.get("id") not in shown]
    if missing:
        old_root = out["root"]
        elements["repair_rest"] = {"type": "Bucket", "props": {"label": "Everything else", "filter": {}}, "children": []}
        elements["repair_root"] = {"type": "Lane", "props": {"label": "All", "filter": {}}, "children": [old_root, "repair_rest"]}
        out["root"] = "repair_root"
    return out


# ---------------------------------------------------------------- designer

def _belief_key(S, belief_key):
    if belief_key:
        return belief_key
    beliefs = S.get("beliefs")
    best = None
    best_conf = -1.0
    if isinstance(beliefs, dict):
        for k, b in beliefs.items():
            if not isinstance(b, dict):
                continue
            if b.get("level") != "habit":
                continue
            c = b.get("confidence", 0)
            if isinstance(c, (int, float)) and c > best_conf:
                best_conf = c
                best = k
    return best or "none"


def _classify(key):
    k = (key or "").lower()
    if "evening" in k or "night" in k:
        return "evening_work"
    if "morning" in k:
        return "morning_work"
    if "weekend" in k:
        return "no_weekend"
    return "generic"


def _drafts(S, key):
    today_d = _today_d(S)
    monday_d = today_d - timedelta(days=today_d.weekday())
    saturday_d = monday_d + timedelta(days=5)
    sunday_d = monday_d + timedelta(days=6)
    friday_d = monday_d + timedelta(days=4)
    cls = _classify(key)

    def rest():
        return {"type": "Bucket", "props": {"label": "Everything else", "filter": {}}, "children": []}

    if cls in ("evening_work", "morning_work"):
        if cls == "evening_work":
            fh, th = 18, 24
            label0, label1 = "Your evenings", "Evening lane"
            b_label = "Evening tasks"
        else:
            fh, th = 6, 12
            label0, label1 = "Your mornings", "Morning lane"
            b_label = "Morning tasks"
        d0 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": label0, "filter": {}}, "children": ["timeline", "bucket", "rest"]},
                "timeline": {"type": "DayTimeline", "props": {"date": _date_str(today_d), "from_hour": fh, "to_hour": th}, "children": []},
                "bucket": {"type": "Bucket", "props": {"label": b_label, "filter": {"hour_from": fh, "hour_to": th}}, "children": []},
                "rest": rest(),
            },
        }
        d1 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": "This week", "filter": {}}, "children": ["lane", "rest"]},
                "lane": {"type": "Lane", "props": {"label": label1, "filter": {"hour_from": fh, "hour_to": th}}, "children": ["week"]},
                "week": {"type": "WeekColumns", "props": {"start": _date_str(monday_d), "days": 7}, "children": []},
                "rest": rest(),
            },
        }
    elif cls == "no_weekend":
        d0 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": "Workweek", "filter": {}}, "children": ["week", "agenda", "rest"]},
                "week": {"type": "WeekColumns", "props": {"start": _date_str(monday_d), "days": 5}, "children": []},
                "agenda": {"type": "AgendaList", "props": {"from": _date_str(saturday_d), "to": _date_str(sunday_d)}, "children": []},
                "rest": rest(),
            },
        }
        d1 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": "Weekdays first", "filter": {}}, "children": ["weekdays", "weekend", "rest"]},
                "weekdays": {"type": "AgendaList", "props": {"from": _date_str(monday_d), "to": _date_str(friday_d)}, "children": []},
                "weekend": {"type": "AgendaList", "props": {"from": _date_str(saturday_d), "to": _date_str(sunday_d)}, "children": []},
                "rest": rest(),
            },
        }
    else:
        d0 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": "Agenda", "filter": {}}, "children": ["agenda", "rest"]},
                "agenda": {"type": "AgendaList", "props": {"from": _date_str(today_d), "to": _date_str(today_d + timedelta(days=6))}, "children": []},
                "rest": rest(),
            },
        }
        d1 = {
            "root": "root",
            "elements": {
                "root": {"type": "Lane", "props": {"label": "Month", "filter": {}}, "children": ["month", "rest"]},
                "month": {"type": "MonthGrid", "props": {"month": _month_str(today_d)}, "children": []},
                "rest": rest(),
            },
        }
    d2 = copy.deepcopy(d0)
    d2["elements"]["root"]["children"] = [c for c in d2["elements"]["root"]["children"] if c != "rest"]
    d2["elements"].pop("rest", None)
    return [d0, d1, d2]


def design(S, belief_key):
    key = _belief_key(S, belief_key)
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
    else:
        chosen = 0
    return {"drafts": drafts, "critique": critique, "repairs": repairs, "chosen": chosen, "proposal": None}


def best_spec(S, belief_key):
    r = design(S, belief_key)
    drafts = r["drafts"]
    chosen = r["chosen"]
    if not drafts:
        return {"root": "root", "elements": {"root": {"type": "Lane", "props": {"label": "All", "filter": {}}, "children": []}}}
    if 0 <= chosen < len(drafts):
        return drafts[chosen]
    return drafts[0]
